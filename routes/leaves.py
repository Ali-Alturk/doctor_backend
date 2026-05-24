"""
Leave management routes.
"""

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from datetime import date, datetime, timedelta
from models import db, LeaveRequest, Doctor

leaves_bp = Blueprint("leaves", __name__)


@leaves_bp.route("", methods=["GET"])
@jwt_required()
def get_leaves():
    """Get leave requests with optional filters."""
    try:
        admin_id = int(get_jwt_identity())
        doctor_id = request.args.get("doctor_id", type=int)
        status = request.args.get("status")
        month = request.args.get("month", type=int)
        year = request.args.get("year", type=int)

        # Base query: only leaves for doctors belonging to this admin
        doctor_ids = [
            d.id for d in Doctor.query.filter_by(admin_id=admin_id).all()
        ]
        query = LeaveRequest.query.filter(LeaveRequest.doctor_id.in_(doctor_ids))

        if doctor_id:
            query = query.filter(LeaveRequest.doctor_id == doctor_id)

        if status:
            query = query.filter(LeaveRequest.status == status)

        if month and year:
            import calendar
            _, last_day = calendar.monthrange(year, month)
            start_date = date(year, month, 1)
            end_date = date(year, month, last_day)
            query = query.filter(
                LeaveRequest.date >= start_date,
                LeaveRequest.date <= end_date,
            )

        leaves = query.order_by(LeaveRequest.date).all()
        return jsonify([lv.to_dict() for lv in leaves]), 200

    except Exception as e:
        return jsonify({"error": f"Failed to fetch leaves: {str(e)}"}), 500


@leaves_bp.route("", methods=["POST"])
@jwt_required()
def create_leave():
    """Create a leave request for a doctor (single date)."""
    try:
        admin_id = int(get_jwt_identity())
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        doctor_id = data.get("doctor_id")
        leave_date_str = data.get("date", "").strip()
        reason = data.get("reason", "").strip()

        if not doctor_id:
            return jsonify({"error": "doctor_id is required"}), 400
        if not leave_date_str:
            return jsonify({"error": "date is required"}), 400

        # Verify doctor belongs to admin
        doctor = Doctor.query.filter_by(id=doctor_id, admin_id=admin_id).first()
        if doctor is None:
            return jsonify({"error": "Doctor not found"}), 404

        try:
            leave_date = datetime.strptime(leave_date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

        # Check for duplicate
        existing = LeaveRequest.query.filter_by(
            doctor_id=doctor_id, date=leave_date,
        ).first()
        if existing:
            return jsonify({"error": "A leave request already exists for this doctor on this date."}), 409

        leave = LeaveRequest(
            doctor_id=doctor_id,
            date=leave_date,
            reason=reason or None,
            status="Pending",
            submitted_by_admin=True,
        )
        db.session.add(leave)
        db.session.commit()

        return jsonify(leave.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to create leave: {str(e)}"}), 500


@leaves_bp.route("/bulk", methods=["POST"])
@jwt_required()
def create_bulk_leaves():
    """Create leave requests for multiple dates at once (date range or list of dates)."""
    try:
        admin_id = int(get_jwt_identity())
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        doctor_id = data.get("doctor_id")
        reason = data.get("reason", "").strip()
        dates = data.get("dates", [])           # List of date strings
        start_date_str = data.get("start_date")  # Or use date range
        end_date_str = data.get("end_date")

        if not doctor_id:
            return jsonify({"error": "doctor_id is required"}), 400

        # Verify doctor belongs to admin
        doctor = Doctor.query.filter_by(id=doctor_id, admin_id=admin_id).first()
        if doctor is None:
            return jsonify({"error": "Doctor not found"}), 404

        # Build list of dates from either explicit list or date range
        leave_dates = []

        if start_date_str and end_date_str:
            try:
                start = datetime.strptime(start_date_str.strip(), "%Y-%m-%d").date()
                end = datetime.strptime(end_date_str.strip(), "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
            if end < start:
                return jsonify({"error": "end_date must be >= start_date"}), 400
            if (end - start).days > 90:
                return jsonify({"error": "Date range cannot exceed 90 days."}), 400
            current = start
            while current <= end:
                leave_dates.append(current)
                current += timedelta(days=1)
        elif dates:
            for d_str in dates:
                try:
                    leave_dates.append(datetime.strptime(d_str.strip(), "%Y-%m-%d").date())
                except ValueError:
                    return jsonify({"error": f"Invalid date format: '{d_str}'. Use YYYY-MM-DD."}), 400
        else:
            return jsonify({"error": "Provide either 'dates' array or 'start_date'+'end_date'."}), 400

        created = []
        skipped = []

        for ld in leave_dates:
            existing = LeaveRequest.query.filter_by(
                doctor_id=doctor_id, date=ld,
            ).first()
            if existing:
                skipped.append(ld.isoformat())
                continue

            leave = LeaveRequest(
                doctor_id=doctor_id,
                date=ld,
                reason=reason or None,
                status="Pending",
                submitted_by_admin=True,
            )
            db.session.add(leave)
            created.append(ld.isoformat())

        db.session.commit()

        return jsonify({
            "created_count": len(created),
            "skipped_count": len(skipped),
            "created_dates": created,
            "skipped_dates": skipped,
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to create bulk leaves: {str(e)}"}), 500


@leaves_bp.route("/<int:leave_id>/status", methods=["PUT"])
@jwt_required()
def update_leave_status(leave_id):
    """Update leave request status (Approve/Reject/reset to Pending). Admin can change any status."""
    try:
        admin_id = int(get_jwt_identity())
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        new_status = data.get("status", "").strip()
        if new_status not in ("Approved", "Rejected", "Pending"):
            return jsonify({"error": "Status must be 'Approved', 'Rejected', or 'Pending'."}), 400

        leave = db.session.get(LeaveRequest, leave_id)
        if leave is None:
            return jsonify({"error": "Leave request not found"}), 404

        # Verify leave belongs to admin's doctor
        doctor = Doctor.query.filter_by(
            id=leave.doctor_id, admin_id=admin_id,
        ).first()
        if doctor is None:
            return jsonify({"error": "Leave request not found"}), 404

        # Admin can change status regardless of current status
        leave.status = new_status
        db.session.commit()

        return jsonify(leave.to_dict()), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to update leave status: {str(e)}"}), 500


@leaves_bp.route("/<int:leave_id>", methods=["DELETE"])
@jwt_required()
def delete_leave(leave_id):
    """Delete a leave request. Admin can delete any leave regardless of status."""
    try:
        admin_id = int(get_jwt_identity())

        leave = db.session.get(LeaveRequest, leave_id)
        if leave is None:
            return jsonify({"error": "Leave request not found"}), 404

        # Verify leave belongs to admin's doctor
        doctor = Doctor.query.filter_by(
            id=leave.doctor_id, admin_id=admin_id,
        ).first()
        if doctor is None:
            return jsonify({"error": "Leave request not found"}), 404

        db.session.delete(leave)
        db.session.commit()

        return jsonify({"message": "Leave request deleted successfully."}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to delete leave: {str(e)}"}), 500
