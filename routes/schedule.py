"""
Schedule, fairness, export, and validation routes.
"""

import io
from datetime import datetime, date
from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity
from models import (
    db, MonthlySchedule, Shift, ShiftAssignment, Doctor, LeaveRequest
)
from scheduler import generate_schedule
from utils.validators import validate_manual_override
from utils.fairness import compute_fairness
from utils.exports import generate_pdf, generate_excel

schedule_bp = Blueprint("schedule", __name__)


# -----------------------------------------------------------------------
# SCHEDULE SETUP
# -----------------------------------------------------------------------

@schedule_bp.route("/schedule/reset/<int:month>/<int:year>", methods=["DELETE"])
@jwt_required()
def reset_month_data(month, year):
    """Delete all schedules, shifts, assignments, and leaves for a given month and year."""
    try:
        admin_id = int(get_jwt_identity())

        # 1. Delete MonthlySchedule (cascades where configured)
        schedule = MonthlySchedule.query.filter_by(
            year=year, month=month, admin_id=admin_id,
        ).first()

        if schedule:
            db.session.delete(schedule)

        # 2. Delete Leave Requests
        doctor_ids = [
            d.id for d in Doctor.query.filter_by(admin_id=admin_id).all()
        ]

        import calendar
        _, last_day = calendar.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        LeaveRequest.query.filter(
            LeaveRequest.doctor_id.in_(doctor_ids),
            LeaveRequest.date >= start_date,
            LeaveRequest.date <= end_date,
        ).delete(synchronize_session=False)

        db.session.commit()

        return jsonify({"message": f"Successfully deleted all schedules and leaves for {month}/{year}."}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to reset month data: {str(e)}"}), 500


@schedule_bp.route("/schedule/setup", methods=["POST"])
@jwt_required()
def setup_schedule():
    """Create or update MonthlySchedule and Shift rows."""
    try:
        admin_id = int(get_jwt_identity())
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        year = data.get("year")
        month = data.get("month")
        days = data.get("days", [])

        if not year or not month:
            return jsonify({"error": "year and month are required"}), 400
        if not days:
            return jsonify({"error": "days array is required"}), 400

        # Create or get schedule
        schedule = MonthlySchedule.query.filter_by(
            year=year, month=month, admin_id=admin_id,
        ).first()

        existing_shifts = {}
        existing_dates = set()

        if schedule is None:
            schedule = MonthlySchedule(
                year=year,
                month=month,
                admin_id=admin_id,
                status="draft",
                is_final=False,
            )
            db.session.add(schedule)
            db.session.flush()
        else:
            # Load existing shifts for update (preserves assignments!)
            existing_shifts = {s.date: s for s in Shift.query.filter_by(schedule_id=schedule.id).all()}
            existing_dates = set(existing_shifts.keys())

        # Create/update shift rows
        incoming_dates = set()
        for day_data in days:
            try:
                shift_date = datetime.strptime(day_data["date"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                return jsonify({"error": f"Invalid date format in days: {day_data.get('date')}"}), 400

            incoming_dates.add(shift_date)

            day_type = day_data.get("day_type", "workday")
            if day_type not in ("workday", "weekend", "holiday"):
                return jsonify({"error": f"Invalid day_type: {day_type}"}), 400

            attending_degree = day_data.get("attending_degree")
            if attending_degree and attending_degree not in ("Professor", "Specialist"):
                return jsonify({"error": f"Invalid attending_degree: {attending_degree}"}), 400

            capacity = day_data.get("capacity", 3)

            if schedule and shift_date in existing_shifts:
                # Update existing shift (preserves assignments!)
                shift = existing_shifts[shift_date]
                shift.day_type = day_type
                shift.attending_name = day_data.get("attending_name")
                shift.attending_degree = attending_degree
                shift.capacity = capacity
            else:
                # Create new shift
                shift = Shift(
                    schedule_id=schedule.id,
                    date=shift_date,
                    day_type=day_type,
                    attending_name=day_data.get("attending_name"),
                    attending_degree=attending_degree,
                    capacity=capacity,
                )
                db.session.add(shift)

        # Remove shifts for dates no longer in the setup
        if existing_dates:
            for old_date in existing_dates - incoming_dates:
                old_shift = existing_shifts[old_date]
                db.session.delete(old_shift)

        db.session.commit()

        return jsonify({
            "schedule_id": schedule.id,
            "message": f"Schedule setup for {month}/{year} completed.",
            "shifts_created": len(days),
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to setup schedule: {str(e)}"}), 500


# -----------------------------------------------------------------------
# SCHEDULE GENERATE
# -----------------------------------------------------------------------

@schedule_bp.route("/schedule/generate", methods=["POST"])
@jwt_required()
def generate():
    """Run the CP-SAT scheduler."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        schedule_id = data.get("schedule_id")
        last_month_data = data.get("last_month_data")
        raw_primer_config = data.get("primer_config")

        if not schedule_id:
            return jsonify({"error": "schedule_id is required"}), 400

        # Convert primer_config keys from strings to ints
        primer_config = None
        if raw_primer_config and isinstance(raw_primer_config, dict):
            primer_config = {int(k): int(v) for k, v in raw_primer_config.items()}

        # Verify schedule belongs to admin
        admin_id = int(get_jwt_identity())
        schedule = MonthlySchedule.query.filter_by(
            id=schedule_id, admin_id=admin_id,
        ).first()
        if schedule is None:
            return jsonify({"error": "Schedule not found"}), 404

        result = generate_schedule(schedule_id, db.session, last_month_data, primer_config)

        if result["status"] == "INFEASIBLE":
            return jsonify(result), 409

        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": f"Failed to generate schedule: {str(e)}"}), 500


# -----------------------------------------------------------------------
# GET SCHEDULE
# -----------------------------------------------------------------------

@schedule_bp.route("/schedule/<int:month>/<int:year>", methods=["GET"])
@jwt_required()
def get_schedule(month, year):
    """Return full schedule with assigned doctors and fairness summary."""
    try:
        admin_id = int(get_jwt_identity())
        schedule = MonthlySchedule.query.filter_by(
            year=year, month=month, admin_id=admin_id,
        ).first()

        if schedule is None:
            return jsonify({
                "schedule": None,
                "shifts": [],
                "fairness_summary": None,
            }), 200

        shifts = (
            Shift.query
            .filter_by(schedule_id=schedule.id)
            .order_by(Shift.date)
            .all()
        )

        shifts_data = []
        for shift in shifts:
            shift_dict = shift.to_dict()
            shifts_data.append(shift_dict)

        fairness_summary = compute_fairness(schedule.id, db.session)

        return jsonify({
            "schedule": schedule.to_dict(),
            "shifts": shifts_data,
            "fairness_summary": fairness_summary,
        }), 200

    except Exception as e:
        return jsonify({"error": f"Failed to get schedule: {str(e)}"}), 500


# -----------------------------------------------------------------------
# VALIDATE SWAP
# -----------------------------------------------------------------------

@schedule_bp.route("/validate-swap", methods=["POST"])
@jwt_required()
def validate_swap():
    """Validate a proposed manual override."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        shift_id = data.get("shift_id")
        new_doctor_ids = data.get("new_doctor_ids", [])

        if not shift_id:
            return jsonify({"error": "shift_id is required"}), 400
        if not new_doctor_ids:
            return jsonify({"error": "new_doctor_ids is required"}), 400

        result = validate_manual_override(shift_id, new_doctor_ids, db.session)

        if not result["valid"] and result["blocking"]:
            return jsonify(result), 409
        elif result["valid"] and result.get("warnings"):
            return jsonify(result), 207
        else:
            return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": f"Validation failed: {str(e)}"}), 500


# -----------------------------------------------------------------------
# MANUAL OVERRIDE
# -----------------------------------------------------------------------

@schedule_bp.route("/schedule/manual-override", methods=["PUT"])
@jwt_required()
def manual_override():
    """Apply manual override to a shift's assignments."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        shift_id = data.get("shift_id")
        new_doctor_ids = data.get("new_doctor_ids", [])
        force = data.get("force", False)

        if not shift_id:
            return jsonify({"error": "shift_id is required"}), 400
        if not new_doctor_ids:
            return jsonify({"error": "new_doctor_ids is required"}), 400

        # Always validate
        validation = validate_manual_override(shift_id, new_doctor_ids, db.session)

        # Hard violations always block, regardless of force
        hard_violations = [
            v for v in validation.get("violations", [])
            if v["rule"] not in ("SOFT_SPACING",)
        ]

        if hard_violations:
            return jsonify({
                "error": "Hard constraint violations prevent this override.",
                "violations": hard_violations,
            }), 409

        # Check warnings — if force=false and there are warnings, reject
        if not force and validation.get("warnings"):
            return jsonify({
                "message": "Warnings exist. Set force=true to override.",
                "warnings": validation["warnings"],
            }), 207

        # Apply the override
        shift = db.session.get(Shift, shift_id)
        if shift is None:
            return jsonify({"error": "Shift not found"}), 404

        # Remove existing assignments
        ShiftAssignment.query.filter_by(shift_id=shift_id).delete()

        # Create new assignments
        for doc_id in new_doctor_ids:
            assignment = ShiftAssignment(
                shift_id=shift_id,
                doctor_id=doc_id,
                is_manual_override=True,
            )
            db.session.add(assignment)

        db.session.commit()

        return jsonify({
            "message": "Override applied successfully.",
            "shift_id": shift_id,
            "doctor_ids": new_doctor_ids,
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Override failed: {str(e)}"}), 500


# -----------------------------------------------------------------------
# PUBLISH
# -----------------------------------------------------------------------

@schedule_bp.route("/schedule/<int:schedule_id>/publish", methods=["POST"])
@jwt_required()
def publish_schedule(schedule_id):
    """Set schedule as final and published."""
    try:
        admin_id = int(get_jwt_identity())
        schedule = MonthlySchedule.query.filter_by(
            id=schedule_id, admin_id=admin_id,
        ).first()

        if schedule is None:
            return jsonify({"error": "Schedule not found"}), 404

        schedule.is_final = True
        schedule.status = "published"
        db.session.commit()

        return jsonify({
            "message": "Schedule published successfully.",
            "schedule": schedule.to_dict(),
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to publish schedule: {str(e)}"}), 500


# -----------------------------------------------------------------------
# FAIRNESS
# -----------------------------------------------------------------------

@schedule_bp.route("/fairness/<int:month>/<int:year>", methods=["GET"])
@jwt_required()
def get_fairness(month, year):
    """Get fairness stats for a month."""
    try:
        admin_id = int(get_jwt_identity())
        schedule = MonthlySchedule.query.filter_by(
            year=year, month=month, admin_id=admin_id,
        ).first()

        if schedule is None:
            return jsonify({"by_doctor": [], "by_seniority": {}}), 200

        result = compute_fairness(schedule.id, db.session)
        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": f"Failed to get fairness data: {str(e)}"}), 500


# -----------------------------------------------------------------------
# EXPORT
# -----------------------------------------------------------------------

@schedule_bp.route("/export/pdf/<int:month>/<int:year>", methods=["GET"])
@jwt_required()
def export_pdf(month, year):
    """Export schedule as PDF."""
    try:
        admin_id = int(get_jwt_identity())
        schedule = MonthlySchedule.query.filter_by(
            year=year, month=month, admin_id=admin_id,
        ).first()

        if schedule is None:
            return jsonify({"error": "Schedule not found"}), 404

        pdf_bytes = generate_pdf(schedule.id, db.session)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"schedule_{year}_{month:02d}.pdf",
        )

    except Exception as e:
        return jsonify({"error": f"PDF export failed: {str(e)}"}), 500


@schedule_bp.route("/export/excel/<int:month>/<int:year>", methods=["GET"])
@jwt_required()
def export_excel(month, year):
    """Export schedule as Excel."""
    try:
        admin_id = int(get_jwt_identity())
        schedule = MonthlySchedule.query.filter_by(
            year=year, month=month, admin_id=admin_id,
        ).first()

        if schedule is None:
            return jsonify({"error": "Schedule not found"}), 404

        excel_bytes = generate_excel(schedule.id, db.session)
        return send_file(
            io.BytesIO(excel_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"schedule_{year}_{month:02d}.xlsx",
        )

    except Exception as e:
        return jsonify({"error": f"Excel export failed: {str(e)}"}), 500
