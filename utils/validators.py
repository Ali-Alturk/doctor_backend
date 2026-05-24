"""
Manual override validation — checks all hard constraints for proposed swaps.
"""

from datetime import timedelta
from models import (
    db, Shift, ShiftAssignment, Doctor, LeaveRequest, MonthlySchedule,
)


def validate_manual_override(shift_id, new_doctor_ids, db_session):
    """
    Validates if the proposed manual override satisfies all hard constraints.
    Returns dict with valid, blocking, violations, and warnings.
    """
    result = {
        "valid": True,
        "blocking": False,
        "violations": [],
        "warnings": [],
    }

    try:
        shift = db_session.query(Shift).get(shift_id)
        if shift is None:
            result["valid"] = False
            result["blocking"] = True
            result["violations"].append({
                "rule": "SHIFT_NOT_FOUND",
                "doctor_id": None,
                "doctor_name": None,
                "message": f"Shift with id {shift_id} not found.",
            })
            return result

        schedule = db_session.query(MonthlySchedule).get(shift.schedule_id)
        if schedule is None:
            result["valid"] = False
            result["blocking"] = True
            result["violations"].append({
                "rule": "SCHEDULE_NOT_FOUND",
                "doctor_id": None,
                "doctor_name": None,
                "message": "Associated schedule not found.",
            })
            return result

        doctors = (
            db_session.query(Doctor)
            .filter(Doctor.id.in_(new_doctor_ids))
            .all()
        )
        doctor_map = {d.id: d for d in doctors}

        if len(doctors) != len(new_doctor_ids):
            missing = set(new_doctor_ids) - set(doctor_map.keys())
            result["valid"] = False
            result["blocking"] = True
            result["violations"].append({
                "rule": "DOCTOR_NOT_FOUND",
                "doctor_id": None,
                "doctor_name": None,
                "message": f"Doctors with ids {list(missing)} not found.",
            })
            return result

        shift_date = shift.date
        prev_date = shift_date - timedelta(days=1)
        next_date = shift_date + timedelta(days=1)
        prev_prev_date = shift_date - timedelta(days=2)
        next_next_date = shift_date + timedelta(days=2)

        # Get all shifts in this schedule for adjacency checks
        all_shifts = (
            db_session.query(Shift)
            .filter_by(schedule_id=shift.schedule_id)
            .all()
        )
        date_to_shift = {s.date: s for s in all_shifts}

        # Get assignments for adjacent days
        def get_assigned_doctor_ids(target_date):
            target_shift = date_to_shift.get(target_date)
            if target_shift is None:
                return set()
            assignments = (
                db_session.query(ShiftAssignment)
                .filter_by(shift_id=target_shift.id)
                .all()
            )
            return {a.doctor_id for a in assignments}

        prev_day_doctors = get_assigned_doctor_ids(prev_date)
        next_day_doctors = get_assigned_doctor_ids(next_date)
        prev_prev_doctors = get_assigned_doctor_ids(prev_prev_date)
        next_next_doctors = get_assigned_doctor_ids(next_next_date)

        for doc_id in new_doctor_ids:
            doc = doctor_map[doc_id]

            # HC2: Leave Exclusion
            leave = (
                db_session.query(LeaveRequest)
                .filter_by(
                    doctor_id=doc_id,
                    date=shift_date,
                    status="Approved",
                )
                .first()
            )
            if leave:
                result["valid"] = False
                result["blocking"] = True
                result["violations"].append({
                    "rule": "LEAVE_CONFLICT",
                    "doctor_id": doc_id,
                    "doctor_name": doc.full_name,
                    "message": (
                        f"{doc.full_name} has approved leave on "
                        f"{shift_date.isoformat()}."
                    ),
                })

            # HC3: Post-Call Rest (worked day before → can't work today)
            if doc_id in prev_day_doctors:
                result["valid"] = False
                result["blocking"] = True
                result["violations"].append({
                    "rule": "POST_CALL_REST",
                    "doctor_id": doc_id,
                    "doctor_name": doc.full_name,
                    "message": (
                        f"{doc.full_name} worked on {prev_date.isoformat()}. "
                        f"Cannot assign {shift_date.isoformat()}."
                    ),
                })

            # HC3: Post-Call Rest (working today → can't already be assigned tomorrow)
            if doc_id in next_day_doctors:
                result["valid"] = False
                result["blocking"] = True
                result["violations"].append({
                    "rule": "POST_CALL_REST",
                    "doctor_id": doc_id,
                    "doctor_name": doc.full_name,
                    "message": (
                        f"{doc.full_name} is assigned on {next_date.isoformat()}. "
                        f"Cannot assign {shift_date.isoformat()}."
                    ),
                })

            # HC4: No 3 Consecutive
            # Check if assigning today creates 3 consecutive with prev+prev_prev or next+next_next
            if doc_id in prev_day_doctors and doc_id in prev_prev_doctors:
                result["valid"] = False
                result["blocking"] = True
                result["violations"].append({
                    "rule": "THREE_CONSECUTIVE",
                    "doctor_id": doc_id,
                    "doctor_name": doc.full_name,
                    "message": (
                        f"{doc.full_name} would have 3 consecutive shifts "
                        f"({prev_prev_date.isoformat()} to {shift_date.isoformat()})."
                    ),
                })

            if doc_id in next_day_doctors and doc_id in next_next_doctors:
                result["valid"] = False
                result["blocking"] = True
                result["violations"].append({
                    "rule": "THREE_CONSECUTIVE",
                    "doctor_id": doc_id,
                    "doctor_name": doc.full_name,
                    "message": (
                        f"{doc.full_name} would have 3 consecutive shifts "
                        f"({shift_date.isoformat()} to {next_next_date.isoformat()})."
                    ),
                })

            if doc_id in prev_day_doctors and doc_id in next_day_doctors:
                result["valid"] = False
                result["blocking"] = True
                result["violations"].append({
                    "rule": "THREE_CONSECUTIVE",
                    "doctor_id": doc_id,
                    "doctor_name": doc.full_name,
                    "message": (
                        f"{doc.full_name} would have 3 consecutive shifts "
                        f"({prev_date.isoformat()} to {next_date.isoformat()})."
                    ),
                })

        # HC5: Seniority Mix
        has_senior = any(
            doctor_map[did].seniority_level == "Senior" for did in new_doctor_ids
        )
        has_non_senior = any(
            doctor_map[did].seniority_level != "Senior" for did in new_doctor_ids
        )

        if not has_senior:
            result["valid"] = False
            result["blocking"] = True
            result["violations"].append({
                "rule": "SENIORITY_MIX",
                "doctor_id": None,
                "doctor_name": None,
                "message": "Proposed assignment has no Senior doctor.",
            })

        if not has_non_senior:
            result["valid"] = False
            result["blocking"] = True
            result["violations"].append({
                "rule": "SENIORITY_MIX",
                "doctor_id": None,
                "doctor_name": None,
                "message": "Proposed assignment has no Mid or Junior doctor.",
            })

        # HC6: Professor Rule
        if shift.attending_degree == "Professor":
            rank1 = (
                db_session.query(Doctor)
                .filter_by(
                    seniority_level="Senior",
                    seniority_rank=1,
                    admin_id=schedule.admin_id,
                )
                .first()
            )
            if rank1 and rank1.id not in new_doctor_ids:
                result["valid"] = False
                result["blocking"] = True
                result["violations"].append({
                    "rule": "PROFESSOR_RULE",
                    "doctor_id": rank1.id if rank1 else None,
                    "doctor_name": rank1.full_name if rank1 else None,
                    "message": (
                        f"Professor attending day requires {rank1.full_name} "
                        f"(rank-1 Senior) to be assigned."
                    ),
                })

        # --- Soft constraint warnings ---
        for doc_id in new_doctor_ids:
            doc = doctor_map[doc_id]
            # Check spacing: any other shift within 3 days (but not violating hard)
            for delta in [-3, -2, 2, 3]:
                check_date = shift_date + timedelta(days=delta)
                check_shift = date_to_shift.get(check_date)
                if check_shift:
                    check_assignments = (
                        db_session.query(ShiftAssignment)
                        .filter_by(shift_id=check_shift.id, doctor_id=doc_id)
                        .first()
                    )
                    if check_assignments:
                        result["warnings"].append({
                            "rule": "SOFT_SPACING",
                            "message": (
                                f"This shift is {abs(delta)} days from "
                                f"{doc.full_name}'s shift on {check_date.isoformat()}."
                            ),
                        })
                        break  # one warning per doctor is enough

    except Exception as e:
        result["valid"] = False
        result["blocking"] = True
        result["violations"].append({
            "rule": "VALIDATION_ERROR",
            "doctor_id": None,
            "doctor_name": None,
            "message": f"Validation error: {str(e)}",
        })

    return result
