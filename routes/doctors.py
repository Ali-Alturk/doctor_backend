"""
Doctor CRUD routes.
"""

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from models import db, Doctor, ShiftAssignment, Shift, MonthlySchedule
from utils.fairness import compute_fairness
from datetime import date

doctors_bp = Blueprint("doctors", __name__)


@doctors_bp.route("", methods=["GET"])
@jwt_required()
def get_doctors():
    """Return list of all doctors for this admin."""
    try:
        admin_id = int(get_jwt_identity())
        doctors = Doctor.query.filter_by(admin_id=admin_id).all()
        return jsonify([d.to_dict() for d in doctors]), 200
    except Exception as e:
        return jsonify({"error": f"Failed to fetch doctors: {str(e)}"}), 500


@doctors_bp.route("", methods=["POST"])
@jwt_required()
def create_doctor():
    """Create a new doctor."""
    try:
        admin_id = int(get_jwt_identity())
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        full_name = data.get("full_name", "").strip()
        seniority_level = data.get("seniority_level", "").strip()
        seniority_rank = data.get("seniority_rank")
        target_shifts = data.get("target_shifts_per_month", 8)

        if not full_name:
            return jsonify({"error": "full_name is required"}), 400

        if seniority_level not in ("Senior", "Mid", "Junior"):
            return jsonify({"error": "seniority_level must be Senior, Mid, or Junior"}), 400

        # Seniority rank validation for Seniors
        if seniority_level == "Senior":
            if seniority_rank is None:
                return jsonify({"error": "seniority_rank is required for Senior doctors"}), 400
            existing = Doctor.query.filter_by(
                admin_id=admin_id,
                seniority_level="Senior",
                seniority_rank=seniority_rank,
            ).first()
            if existing:
                return jsonify({
                    "error": f"Seniority rank {seniority_rank} is already assigned to {existing.full_name}"
                }), 409
        else:
            seniority_rank = None

        doctor = Doctor(
            full_name=full_name,
            seniority_level=seniority_level,
            seniority_rank=seniority_rank,
            target_shifts_per_month=target_shifts,
            admin_id=admin_id,
        )
        db.session.add(doctor)
        db.session.commit()

        return jsonify(doctor.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to create doctor: {str(e)}"}), 500


@doctors_bp.route("/<int:doctor_id>", methods=["PUT"])
@jwt_required()
def update_doctor(doctor_id):
    """Update doctor fields."""
    try:
        admin_id = int(get_jwt_identity())
        doctor = Doctor.query.filter_by(id=doctor_id, admin_id=admin_id).first()
        if doctor is None:
            return jsonify({"error": "Doctor not found"}), 404

        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        if "full_name" in data:
            doctor.full_name = data["full_name"].strip()

        if "seniority_level" in data:
            new_level = data["seniority_level"].strip()
            if new_level not in ("Senior", "Mid", "Junior"):
                return jsonify({"error": "seniority_level must be Senior, Mid, or Junior"}), 400
            doctor.seniority_level = new_level

        if "seniority_rank" in data:
            if doctor.seniority_level == "Senior":
                new_rank = data["seniority_rank"]
                if new_rank is not None:
                    existing = Doctor.query.filter(
                        Doctor.admin_id == admin_id,
                        Doctor.seniority_level == "Senior",
                        Doctor.seniority_rank == new_rank,
                        Doctor.id != doctor_id,
                    ).first()
                    if existing:
                        return jsonify({
                            "error": f"Seniority rank {new_rank} is already assigned to {existing.full_name}"
                        }), 409
                doctor.seniority_rank = new_rank
            else:
                doctor.seniority_rank = None

        if "target_shifts_per_month" in data:
            doctor.target_shifts_per_month = data["target_shifts_per_month"]

        # Clear rank if not Senior
        if doctor.seniority_level != "Senior":
            doctor.seniority_rank = None

        db.session.commit()
        return jsonify(doctor.to_dict()), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to update doctor: {str(e)}"}), 500


@doctors_bp.route("/<int:doctor_id>", methods=["DELETE"])
@jwt_required()
def delete_doctor(doctor_id):
    """Delete a doctor with warning if they have current-month assignments."""
    try:
        admin_id = int(get_jwt_identity())
        doctor = Doctor.query.filter_by(id=doctor_id, admin_id=admin_id).first()
        if doctor is None:
            return jsonify({"error": "Doctor not found"}), 404

        # Check for current month assignments
        today = date.today()
        current_schedules = MonthlySchedule.query.filter_by(
            year=today.year, month=today.month, admin_id=admin_id,
        ).all()

        has_assignments = False
        for sched in current_schedules:
            shifts = Shift.query.filter_by(schedule_id=sched.id).all()
            for shift in shifts:
                assignment = ShiftAssignment.query.filter_by(
                    shift_id=shift.id, doctor_id=doctor_id,
                ).first()
                if assignment:
                    has_assignments = True
                    break
            if has_assignments:
                break

        db.session.delete(doctor)
        db.session.commit()

        response = {
            "message": f"Doctor {doctor.full_name} deleted successfully.",
            "warning": None,
        }
        if has_assignments:
            response["warning"] = (
                f"{doctor.full_name} had assignments in the current month's schedule. "
                "Those assignments have been removed."
            )

        return jsonify(response), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to delete doctor: {str(e)}"}), 500


@doctors_bp.route("/<int:doctor_id>/profile", methods=["GET"])
@jwt_required()
def doctor_profile(doctor_id):
    """Return doctor detail + current month stats."""
    try:
        admin_id = int(get_jwt_identity())
        doctor = Doctor.query.filter_by(id=doctor_id, admin_id=admin_id).first()
        if doctor is None:
            return jsonify({"error": "Doctor not found"}), 404

        profile = doctor.to_dict()

        # Get current month schedule
        today = date.today()
        schedule = MonthlySchedule.query.filter_by(
            year=today.year, month=today.month, admin_id=admin_id,
        ).first()

        stats = {
            "total_shifts": 0,
            "target_shifts": doctor.target_shifts_per_month,
            "delta": 0,
            "weekday_shifts": 0,
            "friday_shifts": 0,
            "saturday_sunday_shifts": 0,
            "weekend_shifts": 0,
            "holiday_shifts": 0,
            "consecutive_occurrences": 0,
            "approved_leaves": 0,
            "shift_dates": [],
            "leave_dates": [],
        }

        if schedule:
            fairness_data = compute_fairness(schedule.id, db.session)
            for doc_stat in fairness_data.get("by_doctor", []):
                if doc_stat["doctor_id"] == doctor_id:
                    stats["total_shifts"] = doc_stat["total_shifts"]
                    stats["delta"] = doc_stat["delta"]
                    stats["weekday_shifts"] = doc_stat.get("weekday_shifts", 0)
                    stats["friday_shifts"] = doc_stat.get("friday_shifts", 0)
                    stats["saturday_sunday_shifts"] = doc_stat.get("saturday_sunday_shifts", 0)
                    stats["weekend_shifts"] = doc_stat["weekend_shifts"]
                    stats["holiday_shifts"] = doc_stat["holiday_shifts"]
                    stats["consecutive_occurrences"] = doc_stat["consecutive_occurrences"]
                    stats["approved_leaves"] = doc_stat["approved_leaves"]
                    break

            # Get shift dates
            shifts = Shift.query.filter_by(schedule_id=schedule.id).all()
            for shift in shifts:
                assignment = ShiftAssignment.query.filter_by(
                    shift_id=shift.id, doctor_id=doctor_id,
                ).first()
                if assignment:
                    stats["shift_dates"].append(shift.date.isoformat())

        # Get leave dates for current month
        from models import LeaveRequest
        import calendar
        _, last_day = calendar.monthrange(today.year, today.month)
        start_date = date(today.year, today.month, 1)
        end_date = date(today.year, today.month, last_day)

        leaves = LeaveRequest.query.filter(
            LeaveRequest.doctor_id == doctor_id,
            LeaveRequest.date >= start_date,
            LeaveRequest.date <= end_date,
        ).all()
        stats["leave_dates"] = [
            {"date": lv.date.isoformat(), "status": lv.status}
            for lv in leaves
        ]

        profile["stats"] = stats
        return jsonify(profile), 200

    except Exception as e:
        return jsonify({"error": f"Failed to get doctor profile: {str(e)}"}), 500
