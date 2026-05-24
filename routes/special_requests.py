"""
Special Requests CRUD routes and validation endpoint.
"""

import json
from datetime import datetime, date
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from models import (
    db, SpecialRequest, Doctor, LeaveRequest, Shift, MonthlySchedule,
)
from utils.special_request_validator import validate_special_requests

special_requests_bp = Blueprint("special_requests", __name__)

VALID_TYPES = ("must_work", "must_not_work", "must_work_with", "weekend_off_after_duty")


# -----------------------------------------------------------------------
# LIST
# -----------------------------------------------------------------------

@special_requests_bp.route("/<int:month>/<int:year>", methods=["GET"])
@jwt_required()
def list_requests(month, year):
    """Return all special requests for a given month/year."""
    try:
        admin_id = int(get_jwt_identity())
        reqs = (
            SpecialRequest.query
            .filter_by(admin_id=admin_id, year=year, month=month)
            .order_by(SpecialRequest.created_at.desc())
            .all()
        )
        return jsonify([r.to_dict() for r in reqs]), 200
    except Exception as e:
        return jsonify({"error": f"Failed to fetch special requests: {str(e)}"}), 500


# -----------------------------------------------------------------------
# CREATE
# -----------------------------------------------------------------------

@special_requests_bp.route("", methods=["POST"])
@jwt_required()
def create_request():
    """Create a new special request."""
    try:
        admin_id = int(get_jwt_identity())
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        # --- Validate required fields ---
        request_type = data.get("request_type", "").strip()
        if request_type not in VALID_TYPES:
            return jsonify({"error": f"Invalid request_type. Must be one of: {', '.join(VALID_TYPES)}"}), 400

        doctor_id = data.get("doctor_id")
        if not doctor_id:
            return jsonify({"error": "doctor_id is required"}), 400

        year = data.get("year")
        month = data.get("month")
        if not year or not month:
            return jsonify({"error": "year and month are required"}), 400

        # Verify doctor belongs to this admin
        doctor = Doctor.query.filter_by(id=doctor_id, admin_id=admin_id).first()
        if not doctor:
            return jsonify({"error": f"Doctor with id {doctor_id} not found"}), 404

        # --- Parse date ---
        req_date = None
        if data.get("date"):
            try:
                req_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

        # Date is required for must_work, must_not_work, and weekend_off_after_duty
        if request_type in ("must_work", "must_not_work", "weekend_off_after_duty") and not req_date:
            return jsonify({"error": f"date is required for request type '{request_type}'"}), 400

        # --- Parse required_people ---
        required_people = data.get("required_people", [])
        if request_type == "must_work_with":
            if not required_people or not isinstance(required_people, list):
                return jsonify({"error": "required_people (list of doctor IDs) is required for 'must_work_with'"}), 400
            # Verify all companions exist
            for comp_id in required_people:
                comp = Doctor.query.filter_by(id=comp_id, admin_id=admin_id).first()
                if not comp:
                    return jsonify({"error": f"Companion doctor with id {comp_id} not found"}), 404

        only_when_not_primer = data.get("only_when_not_primer", False)
        only_when_primer = data.get("only_when_primer", False)
        attending_name = data.get("attending_name")
        attending_name = attending_name.strip() if attending_name else None
        note = data.get("note")
        note = note.strip() if note else None

        # Mutual exclusivity check
        if only_when_not_primer and only_when_primer:
            return jsonify({"error": "Cannot set both only_when_not_primer and only_when_primer."}), 400

        sr = SpecialRequest(
            admin_id=admin_id,
            year=year,
            month=month,
            request_type=request_type,
            doctor_id=doctor_id,
            date=req_date,
            attending_name=attending_name,
            only_when_not_primer=only_when_not_primer,
            only_when_primer=only_when_primer,
            note=note,
            is_active=True,
        )
        sr.set_required_people(required_people if required_people else None)

        db.session.add(sr)
        db.session.commit()

        return jsonify(sr.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to create special request: {str(e)}"}), 500


# -----------------------------------------------------------------------
# UPDATE
# -----------------------------------------------------------------------

@special_requests_bp.route("/<int:req_id>", methods=["PUT"])
@jwt_required()
def update_request(req_id):
    """Update an existing special request."""
    try:
        admin_id = int(get_jwt_identity())
        sr = SpecialRequest.query.filter_by(id=req_id, admin_id=admin_id).first()
        if not sr:
            return jsonify({"error": "Special request not found"}), 404

        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        if "request_type" in data:
            rt = data["request_type"].strip()
            if rt not in VALID_TYPES:
                return jsonify({"error": f"Invalid request_type. Must be one of: {', '.join(VALID_TYPES)}"}), 400
            sr.request_type = rt

        if "doctor_id" in data:
            doc = Doctor.query.filter_by(id=data["doctor_id"], admin_id=admin_id).first()
            if not doc:
                return jsonify({"error": f"Doctor with id {data['doctor_id']} not found"}), 404
            sr.doctor_id = data["doctor_id"]

        if "date" in data:
            if data["date"]:
                try:
                    sr.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
                except ValueError:
                    return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
            else:
                sr.date = None

        if "required_people" in data:
            rp = data["required_people"]
            if rp and isinstance(rp, list):
                for comp_id in rp:
                    comp = Doctor.query.filter_by(id=comp_id, admin_id=admin_id).first()
                    if not comp:
                        return jsonify({"error": f"Companion doctor with id {comp_id} not found"}), 404
            sr.set_required_people(rp if rp else None)

        if "only_when_not_primer" in data:
            sr.only_when_not_primer = bool(data["only_when_not_primer"])

        if "only_when_primer" in data:
            sr.only_when_primer = bool(data["only_when_primer"])

        # Mutual exclusivity check
        if sr.only_when_not_primer and sr.only_when_primer:
            return jsonify({"error": "Cannot set both only_when_not_primer and only_when_primer."}), 400

        if "attending_name" in data:
            val = data["attending_name"]
            sr.attending_name = val.strip() if val else None

        if "note" in data:
            val = data["note"]
            sr.note = val.strip() if val else None

        if "is_active" in data:
            sr.is_active = bool(data["is_active"])

        db.session.commit()
        return jsonify(sr.to_dict()), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to update special request: {str(e)}"}), 500


# -----------------------------------------------------------------------
# DELETE
# -----------------------------------------------------------------------

@special_requests_bp.route("/<int:req_id>", methods=["DELETE"])
@jwt_required()
def delete_request(req_id):
    """Delete a special request."""
    try:
        admin_id = int(get_jwt_identity())
        sr = SpecialRequest.query.filter_by(id=req_id, admin_id=admin_id).first()
        if not sr:
            return jsonify({"error": "Special request not found"}), 404

        db.session.delete(sr)
        db.session.commit()
        return jsonify({"message": "Special request deleted successfully."}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to delete special request: {str(e)}"}), 500


# -----------------------------------------------------------------------
# TOGGLE ACTIVE
# -----------------------------------------------------------------------

@special_requests_bp.route("/<int:req_id>/toggle", methods=["PATCH"])
@jwt_required()
def toggle_request(req_id):
    """Toggle is_active status."""
    try:
        admin_id = int(get_jwt_identity())
        sr = SpecialRequest.query.filter_by(id=req_id, admin_id=admin_id).first()
        if not sr:
            return jsonify({"error": "Special request not found"}), 404

        sr.is_active = not sr.is_active
        db.session.commit()
        return jsonify(sr.to_dict()), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to toggle special request: {str(e)}"}), 500


# -----------------------------------------------------------------------
# VALIDATE
# -----------------------------------------------------------------------

@special_requests_bp.route("/validate/<int:month>/<int:year>", methods=["POST"])
@jwt_required()
def validate_requests(month, year):
    """Validate all active special requests against leaves, shifts, and each other."""
    try:
        admin_id = int(get_jwt_identity())

        # Load active requests
        active_requests = (
            SpecialRequest.query
            .filter_by(admin_id=admin_id, year=year, month=month, is_active=True)
            .all()
        )

        if not active_requests:
            return jsonify({"conflicts": [], "message": "No active special requests."}), 200

        # Load doctors
        doctors = Doctor.query.filter_by(admin_id=admin_id).all()
        doctors_by_id = {d.id: d for d in doctors}

        # Load shifts for the month
        schedule = MonthlySchedule.query.filter_by(
            year=year, month=month, admin_id=admin_id,
        ).first()

        shifts = []
        if schedule:
            shifts = (
                Shift.query
                .filter_by(schedule_id=schedule.id)
                .order_by(Shift.date)
                .all()
            )

        # Load approved leaves
        import calendar
        _, last_day = calendar.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        all_dates = [s.date for s in shifts] if shifts else []
        doctor_ids = [d.id for d in doctors]

        leaves = (
            LeaveRequest.query
            .filter(
                LeaveRequest.doctor_id.in_(doctor_ids),
                LeaveRequest.date >= start_date,
                LeaveRequest.date <= end_date,
                LeaveRequest.status == "Approved",
            )
            .all()
        )
        leaves_set = {(lv.doctor_id, lv.date) for lv in leaves}

        # Run validation
        conflicts = validate_special_requests(
            active_requests, leaves_set, shifts, doctors_by_id, year, month
        )

        return jsonify({
            "conflicts": conflicts,
            "total_active": len(active_requests),
            "message": (
                f"Found {len(conflicts)} conflict(s) in {len(active_requests)} active requests."
                if conflicts else
                f"All {len(active_requests)} active requests passed validation."
            ),
        }), 200

    except Exception as e:
        return jsonify({"error": f"Validation failed: {str(e)}"}), 500
