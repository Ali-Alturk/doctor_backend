"""
Conflict analyzer for diagnosing scheduling infeasibility.
Called only when the CP-SAT solver returns INFEASIBLE.
"""

from datetime import timedelta
from models import (
    db, Shift, Doctor, LeaveRequest, MonthlySchedule, ShiftAssignment,
    SpecialRequest,
)


def analyze_conflicts(schedule_id, db_session):
    """
    Independently checks for common causes of infeasibility.
    Returns a list of conflict dicts with type, dates, message, suggestion.
    """
    conflicts = []

    try:
        schedule = db_session.query(MonthlySchedule).get(schedule_id)
        if schedule is None:
            return [{
                "type": "INSUFFICIENT_CAPACITY",
                "dates": [],
                "message": "Schedule not found.",
                "suggestion": "Ensure the schedule exists before generating.",
            }]

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

        all_dates = [s.date for s in shifts]
        date_to_shift = {s.date: s for s in shifts}

        approved_leaves = (
            db_session.query(LeaveRequest)
            .filter(
                LeaveRequest.doctor_id.in_([d.id for d in doctors]),
                LeaveRequest.date.in_(all_dates),
                LeaveRequest.status == "Approved",
            )
            .all()
        )

        leave_set = set()
        for lv in approved_leaves:
            leave_set.add((lv.doctor_id, lv.date))

        # --- Check 1: Professor days where rank-1 Senior has approved leave ---
        rank1_doc = None
        for d in doctors:
            if d.seniority_level == "Senior" and d.seniority_rank == 1:
                rank1_doc = d
                break

        for shift in shifts:
            if shift.attending_degree == "Professor" and rank1_doc:
                if (rank1_doc.id, shift.date) in leave_set:
                    conflicts.append({
                        "type": "PROFESSOR_RULE_VIOLATION",
                        "dates": [shift.date.isoformat()],
                        "message": (
                            f"{rank1_doc.full_name} (rank-1 Senior) has approved leave on "
                            f"{shift.date.isoformat()}, but this day requires Professor attending "
                            f"which mandates rank-1 Senior presence."
                        ),
                        "suggestion": (
                            f"Either revoke {rank1_doc.full_name}'s leave for "
                            f"{shift.date.isoformat()} or change the attending degree to Specialist."
                        ),
                    })

        # --- Check 2: Days with insufficient eligible doctors ---
        total_target_shifts = sum(
            (d.target_shifts_per_month or 0) for d in doctors
        )
        base_capacity = total_target_shifts // len(shifts) if shifts else 3

        for idx, shift in enumerate(shifts):
            eligible = 0
            for doc in doctors:
                if (doc.id, shift.date) in leave_set:
                    continue
                eligible += 1

            min_capacity = base_capacity
            if eligible < min_capacity:
                conflicts.append({
                    "type": "INSUFFICIENT_CAPACITY",
                    "dates": [shift.date.isoformat()],
                    "message": (
                        f"Only {eligible} doctors eligible on {shift.date.isoformat()} "
                        f"(need at least {min_capacity})."
                    ),
                    "suggestion": (
                        "Reduce leaves on this date, add more doctors, or "
                        "reduce target shifts."
                    ),
                })

        # --- Check 3: Cascading post-call ---
        consecutive_low = []
        for idx in range(len(shifts) - 2):
            dates_window = [shifts[idx + j].date for j in range(3)]
            all_low = True
            for dt in dates_window:
                eligible = sum(
                    1 for doc in doctors
                    if (doc.id, dt) not in leave_set
                )
                if eligible > 4:
                    all_low = False
                    break
            if all_low:
                consecutive_low.append(dates_window)

        if consecutive_low:
            seen_dates = set()
            for window in consecutive_low:
                date_strs = [d.isoformat() for d in window if d.isoformat() not in seen_dates]
                seen_dates.update(date_strs)
            if seen_dates:
                conflicts.append({
                    "type": "CASCADING_POSTCALL",
                    "dates": sorted(seen_dates),
                    "message": (
                        "Multiple consecutive days have limited doctor availability. "
                        "Post-call rest constraints cascade, making scheduling impossible."
                    ),
                    "suggestion": (
                        "Reduce leaves in this window, increase the doctor pool, "
                        "or allow temporary capacity reductions."
                    ),
                })

        # --- Check 4: Hierarchy Composition Check ---
        # Check if each day can form a proper Senior → Mid → Junior hierarchy
        for shift in shifts:
            eligible_seniors = [
                doc for doc in doctors
                if doc.seniority_level == "Senior"
                and (doc.id, shift.date) not in leave_set
                and (doc.target_shifts_per_month or 0) > 0
            ]
            eligible_mids = [
                doc for doc in doctors
                if doc.seniority_level == "Mid"
                and (doc.id, shift.date) not in leave_set
                and (doc.target_shifts_per_month or 0) > 0
            ]
            eligible_juniors = [
                doc for doc in doctors
                if doc.seniority_level == "Junior"
                and (doc.id, shift.date) not in leave_set
                and (doc.target_shifts_per_month or 0) > 0
            ]

            levels_available = sum([
                len(eligible_seniors) > 0,
                len(eligible_mids) > 0,
                len(eligible_juniors) > 0,
            ])

            if levels_available == 0:
                conflicts.append({
                    "type": "HIERARCHY_IMPOSSIBLE",
                    "dates": [shift.date.isoformat()],
                    "message": (
                        f"No doctors from any seniority level available on "
                        f"{shift.date.isoformat()}."
                    ),
                    "suggestion": "Adjust leave approvals or add more doctors.",
                })
            elif levels_available == 1:
                present_level = (
                    "Senior" if eligible_seniors else
                    "Mid" if eligible_mids else "Junior"
                )
                conflicts.append({
                    "type": "HIERARCHY_IMPOSSIBLE",
                    "dates": [shift.date.isoformat()],
                    "message": (
                        f"Only {present_level} doctors available on "
                        f"{shift.date.isoformat()}. Cannot form a hierarchical team "
                        f"(need at least 2 seniority levels)."
                    ),
                    "suggestion": (
                        f"Adjust leave approvals for non-{present_level} doctors "
                        f"on this date, or add doctors of other seniority levels."
                    ),
                })
            elif not eligible_mids and eligible_seniors and eligible_juniors:
                # Mid fallback — not a conflict, just informational
                pass  # Handled by the scheduler's MID_FALLBACK adjustment

        # --- Check 5: Holiday Senior Coverage ---
        # On holidays/weekends, check if least-senior Seniors are available
        senior_doctors_sorted = sorted(
            [d for d in doctors if d.seniority_level == "Senior"],
            key=lambda d: (d.seniority_rank or 999),
        )
        if len(senior_doctors_sorted) >= 2:
            # Identify least-senior Seniors (rank 3+)
            least_senior = [
                d for d in senior_doctors_sorted
                if (d.seniority_rank or 1) > 2
            ]
            for shift in shifts:
                if shift.day_type not in ("weekend", "holiday"):
                    continue
                if shift.attending_degree in ("Professor", "Specialist"):
                    continue  # Professor rule takes priority
                if not least_senior:
                    continue
                available_least = [
                    d for d in least_senior
                    if (d.id, shift.date) not in leave_set
                ]
                if not available_least:
                    most_senior_names = ", ".join(
                        d.full_name for d in senior_doctors_sorted[:2]
                    )
                    conflicts.append({
                        "type": "HOLIDAY_SENIOR_COVERAGE",
                        "dates": [shift.date.isoformat()],
                        "message": (
                            f"No least-senior Seniors (rank 3+) available on "
                            f"holiday {shift.date.isoformat()}. "
                            f"Only most-senior Seniors ({most_senior_names}) can cover."
                        ),
                        "suggestion": (
                            "Adjust leave approvals for junior-ranked Senior doctors "
                            "on this holiday, or accept that most-senior Seniors will "
                            "cover this day."
                        ),
                    })

        # --- Check 6: Special Request Conflicts ---
        special_reqs = (
            db_session.query(SpecialRequest)
            .filter_by(
                admin_id=schedule.admin_id,
                year=schedule.year,
                month=schedule.month,
                is_active=True,
            )
            .all()
        )

        if special_reqs:
            doctor_map = {d.id: d for d in doctors}

            for sr in special_reqs:
                doc = doctor_map.get(sr.doctor_id)
                doc_name = doc.full_name if doc else f"Doctor #{sr.doctor_id}"

                # must_work on a leave day
                if sr.request_type == "must_work" and sr.date:
                    if (sr.doctor_id, sr.date) in leave_set:
                        conflicts.append({
                            "type": "SPECIAL_REQ_CONFLICT",
                            "dates": [sr.date.isoformat()],
                            "message": (
                                f"Special Request #{sr.id}: {doc_name} must work on "
                                f"{sr.date.isoformat()}, but has approved leave that day."
                            ),
                            "suggestion": (
                                f"Revoke {doc_name}'s leave on {sr.date.isoformat()} "
                                f"or deactivate/delete this special request."
                            ),
                        })

                # must_work + must_not_work on same date
                if sr.request_type == "must_work" and sr.date:
                    for sr2 in special_reqs:
                        if (sr2.request_type == "must_not_work"
                                and sr2.doctor_id == sr.doctor_id
                                and sr2.date == sr.date
                                and sr2.id != sr.id):
                            conflicts.append({
                                "type": "SPECIAL_REQ_CONFLICT",
                                "dates": [sr.date.isoformat()],
                                "message": (
                                    f"Special Requests #{sr.id} and #{sr2.id}: "
                                    f"{doc_name} has both must_work and must_not_work "
                                    f"on {sr.date.isoformat()}."
                                ),
                                "suggestion": "Deactivate or delete one of the conflicting requests.",
                            })
                            break

                # must_work_with companion on leave
                if sr.request_type == "must_work_with" and sr.date:
                    for comp_id in sr.get_required_people():
                        if (comp_id, sr.date) in leave_set:
                            comp = doctor_map.get(comp_id)
                            comp_name = comp.full_name if comp else f"Doctor #{comp_id}"
                            conflicts.append({
                                "type": "SPECIAL_REQ_CONFLICT",
                                "dates": [sr.date.isoformat()],
                                "message": (
                                    f"Special Request #{sr.id}: {doc_name} must work with "
                                    f"{comp_name} on {sr.date.isoformat()}, but {comp_name} "
                                    f"has approved leave that day."
                                ),
                                "suggestion": (
                                    f"Revoke {comp_name}'s leave on {sr.date.isoformat()} "
                                    f"or remove them from the companion list."
                                ),
                            })

                if sr.request_type == "must_work_with" and getattr(sr, "attending_name", None):
                    matching_shifts = [
                        shift for shift in shifts
                        if shift.attending_name
                        and shift.attending_name.strip().lower() == sr.attending_name.strip().lower()
                    ]

                    if not matching_shifts:
                        conflicts.append({
                            "type": "SPECIAL_REQ_CONFLICT",
                            "dates": [],
                            "message": (
                                f"Special Request #{sr.id}: {doc_name} targets attending "
                                f"'{sr.attending_name}', but no matching shift exists."
                            ),
                            "suggestion": "Fix the attending name or update the monthly setup.",
                        })

                    eligible_shifts = []
                    skipped_dates = []
                    for shift in matching_shifts:
                        skip_reasons = []
                        if (sr.doctor_id, shift.date) in leave_set:
                            skip_reasons.append(f"{doc_name} is on leave")
                        if doc and doc.seniority_level == "Senior" and shift.day_type == "holiday":
                            skip_reasons.append(f"{doc_name} is Senior on a holiday")

                        for comp_id in sr.get_required_people():
                            comp = doctor_map.get(comp_id)
                            comp_name = comp.full_name if comp else f"Doctor #{comp_id}"
                            if (comp_id, shift.date) in leave_set:
                                skip_reasons.append(f"{comp_name} is on leave")
                            if comp and comp.seniority_level == "Senior" and shift.day_type == "holiday":
                                skip_reasons.append(f"{comp_name} is Senior on a holiday")

                        if skip_reasons:
                            skipped_dates.append((shift.date, skip_reasons))
                        else:
                            eligible_shifts.append(shift)

                    if matching_shifts and not eligible_shifts:
                        date_details = "; ".join(
                            f"{dt.isoformat()}: {', '.join(reasons)}"
                            for dt, reasons in skipped_dates
                        )
                        conflicts.append({
                            "type": "SPECIAL_REQ_CONFLICT",
                            "dates": [shift.date.isoformat() for shift in matching_shifts],
                            "message": (
                                f"Special Request #{sr.id}: {doc_name} targets attending "
                                f"'{sr.attending_name}', but every matching shift is unavailable. "
                                f"Skipped dates: {date_details}."
                            ),
                            "suggestion": (
                                "Add another matching attending day, revoke the blocking leave, "
                                "or change the request doctors."
                            ),
                        })


    except Exception as e:
        conflicts.append({
            "type": "INSUFFICIENT_CAPACITY",
            "dates": [],
            "message": f"Error analyzing conflicts: {str(e)}",
            "suggestion": "Check database connectivity and data integrity.",
        })

    return conflicts
