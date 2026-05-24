"""
Fairness stats computation for the shift scheduler.
"""

from models import (
    db, Shift, ShiftAssignment, Doctor, LeaveRequest, MonthlySchedule,
)


def compute_fairness(schedule_id, db_session):
    """
    Compute per-doctor and per-seniority fairness stats for a schedule.
    Returns structured dict with by_doctor and by_seniority sections.
    """
    try:
        schedule = db_session.query(MonthlySchedule).get(schedule_id)
        if schedule is None:
            return {"by_doctor": [], "by_seniority": {}}

        shifts = (
            db_session.query(Shift)
            .filter_by(schedule_id=schedule_id)
            .order_by(Shift.date)
            .all()
        )
        doctors = (
            db_session.query(Doctor)
            .filter_by(admin_id=schedule.admin_id)
            .all()
        )

        shift_ids = [s.id for s in shifts]
        date_map = {s.id: s for s in shifts}

        all_assignments = (
            db_session.query(ShiftAssignment)
            .filter(ShiftAssignment.shift_id.in_(shift_ids))
            .all()
        )

        # Build per-doctor data
        doctor_stats = {}
        for doc in doctors:
            approved_leaves_count = (
                db_session.query(LeaveRequest)
                .filter(
                    LeaveRequest.doctor_id == doc.id,
                    LeaveRequest.date.in_([s.date for s in shifts]),
                    LeaveRequest.status == "Approved",
                )
                .count()
            )

            # If doctor has target=0, they are on a full-month leave (set "behind the scenes"
            # via target instead of explicit LeaveRequest rows). Show total shift days as leaves.
            is_full_month_leave = (doc.target_shifts_per_month is not None and
                                   doc.target_shifts_per_month == 0)
            if is_full_month_leave and approved_leaves_count == 0:
                approved_leaves_count = len(shifts)

            doctor_stats[doc.id] = {
                "doctor_id": doc.id,
                "doctor_name": doc.full_name,
                "seniority": doc.seniority_level,
                "total_shifts": 0,
                "target_shifts": doc.target_shifts_per_month,
                "delta": 0,
                "weekend_shifts": 0,
                "holiday_shifts": 0,
                "consecutive_occurrences": 0,
                "approved_leaves": approved_leaves_count,
                "is_full_month_leave": is_full_month_leave,
                "weekday_shifts": 0,
                "friday_shifts": 0,
                "saturday_sunday_shifts": 0,
                "shift_dates": [],
            }

        for assignment in all_assignments:
            shift = date_map.get(assignment.shift_id)
            if shift is None:
                continue

            doc_id = assignment.doctor_id
            if doc_id not in doctor_stats:
                continue

            doctor_stats[doc_id]["total_shifts"] += 1
            doctor_stats[doc_id]["shift_dates"].append(shift.date)

            if shift.day_type == "weekend":
                doctor_stats[doc_id]["weekend_shifts"] += 1
            elif shift.day_type == "holiday":
                doctor_stats[doc_id]["holiday_shifts"] += 1
                
            # Detailed day tracking
            if shift.date.weekday() == 4: # Friday
                doctor_stats[doc_id]["friday_shifts"] += 1
            elif shift.date.weekday() in (5, 6): # Saturday/Sunday
                doctor_stats[doc_id]["saturday_sunday_shifts"] += 1
            else: # Monday-Thursday
                doctor_stats[doc_id]["weekday_shifts"] += 1

        # Compute delta and consecutive occurrences
        for doc_id, stats in doctor_stats.items():
            stats["delta"] = stats["total_shifts"] - stats["target_shifts"]

            # Count consecutive shift pairs
            sorted_dates = sorted(stats["shift_dates"])
            consec = 0
            for i in range(len(sorted_dates) - 1):
                diff = (sorted_dates[i + 1] - sorted_dates[i]).days
                if diff == 1:
                    consec += 1
            stats["consecutive_occurrences"] = consec

        # Remove internal shift_dates before returning
        by_doctor = []
        for doc_id, stats in doctor_stats.items():
            entry = {k: v for k, v in stats.items() if k != "shift_dates"}
            by_doctor.append(entry)

        # Per-seniority stats
        by_seniority = {}
        for level in ("Senior", "Mid", "Junior"):
            docs_in_level = [
                s for s in by_doctor if s["seniority"] == level
            ]
            if not docs_in_level:
                by_seniority[level] = {
                    "avg_weekend_shifts": 0,
                    "max_weekend_shifts": 0,
                    "min_weekend_shifts": 0,
                    "imbalance_flag": False,
                }
                continue

            weekend_counts = [d["weekend_shifts"] + d["holiday_shifts"] for d in docs_in_level]
            max_wh = max(weekend_counts)
            min_wh = min(weekend_counts)

            by_seniority[level] = {
                "avg_weekend_shifts": round(
                    sum(weekend_counts) / len(weekend_counts), 2
                ),
                "max_weekend_shifts": max_wh,
                "min_weekend_shifts": min_wh,
                "imbalance_flag": (max_wh - min_wh) > 1,
            }

        return {
            "by_doctor": by_doctor,
            "by_seniority": by_seniority,
        }

    except Exception as e:
        return {
            "by_doctor": [],
            "by_seniority": {},
            "error": str(e),
        }
