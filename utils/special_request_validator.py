"""
Validation logic for special requests.
Checks for internal conflicts between requests, leaves, holidays, and schedule rules.
"""

import json
from datetime import date, timedelta
from collections import defaultdict


def validate_special_requests(requests, leaves_set, shifts, doctors_by_id, year, month):
    """
    Validate all active special requests for a given month.

    Args:
        requests: list of SpecialRequest model instances (active only)
        leaves_set: set of (doctor_id, date) for approved leaves
        shifts: list of Shift model instances for the month
        doctors_by_id: dict {doctor_id: Doctor instance}
        year: int
        month: int

    Returns:
        list of conflict dicts:
        {
            "type": str,
            "severity": "error" | "warning",
            "request_ids": [int, ...],
            "message": str,
            "suggestion": str,
        }
    """
    conflicts = []
    shift_dates = {s.date for s in shifts}
    date_to_shift = {s.date: s for s in shifts}

    # Index requests by (doctor_id, date) for cross-checking
    by_doctor_date = defaultdict(list)
    for req in requests:
        if req.date:
            by_doctor_date[(req.doctor_id, req.date)].append(req)

    # --- Check 1: must_work + must_not_work on same date ---
    for (doc_id, dt), reqs in by_doctor_date.items():
        types = {r.request_type for r in reqs}
        if "must_work" in types and "must_not_work" in types:
            work_ids = [r.id for r in reqs if r.request_type == "must_work"]
            not_work_ids = [r.id for r in reqs if r.request_type == "must_not_work"]
            doc_name = doctors_by_id.get(doc_id)
            doc_name = doc_name.full_name if doc_name else f"Doctor #{doc_id}"
            conflicts.append({
                "type": "WORK_AND_NOT_WORK_SAME_DATE",
                "severity": "error",
                "request_ids": work_ids + not_work_ids,
                "message": (
                    f"{doc_name} has both 'must work' and 'must not work' "
                    f"requests on {dt.isoformat()}."
                ),
                "suggestion": "Remove or deactivate one of the conflicting requests.",
            })

    # --- Check 2: must_work on a leave day ---
    for req in requests:
        if req.request_type == "must_work" and req.date:
            if (req.doctor_id, req.date) in leaves_set:
                doc_name = doctors_by_id.get(req.doctor_id)
                doc_name = doc_name.full_name if doc_name else f"Doctor #{req.doctor_id}"
                conflicts.append({
                    "type": "WORK_ON_LEAVE_DAY",
                    "severity": "error",
                    "request_ids": [req.id],
                    "message": (
                        f"{doc_name} is requested to work on "
                        f"{req.date.isoformat()}, but has an approved leave that day."
                    ),
                    "suggestion": (
                        f"Either revoke the leave for {req.date.isoformat()} "
                        f"or deactivate this request."
                    ),
                })

    # --- Check 3: Date outside schedule month ---
    for req in requests:
        if req.date and req.date not in shift_dates:
            doc_name = doctors_by_id.get(req.doctor_id)
            doc_name = doc_name.full_name if doc_name else f"Doctor #{req.doctor_id}"
            conflicts.append({
                "type": "DATE_OUTSIDE_MONTH",
                "severity": "error",
                "request_ids": [req.id],
                "message": (
                    f"Request for {doc_name} on {req.date.isoformat()} "
                    f"is outside the schedule month ({month}/{year}) or has no shift."
                ),
                "suggestion": "Update the date to fall within the current schedule month.",
            })

    # --- Check 4: must_work_with — companion on leave ---
    for req in requests:
        if req.request_type == "must_work_with":
            companions = req.get_required_people()
            doc_name = doctors_by_id.get(req.doctor_id)
            doc_name = doc_name.full_name if doc_name else f"Doctor #{req.doctor_id}"

            if req.attending_name:
                matching_shifts = [
                    shift for shift in shifts
                    if shift.attending_name
                    and shift.attending_name.strip().lower() == req.attending_name.strip().lower()
                ]

                if not matching_shifts:
                    conflicts.append({
                        "type": "ATTENDING_REQUEST_NO_MATCH",
                        "severity": "error",
                        "request_ids": [req.id],
                        "message": (
                            f"{doc_name}'s 'must work with' request targets attending "
                            f"'{req.attending_name}', but no matching shift exists in {month}/{year}."
                        ),
                        "suggestion": "Fix the attending name or update the monthly setup days.",
                    })

                eligible_shifts = []
                skipped_dates = []
                doc = doctors_by_id.get(req.doctor_id)
                for shift in matching_shifts:
                    skip_reasons = []
                    if (req.doctor_id, shift.date) in leaves_set:
                        skip_reasons.append(f"{doc_name} is on leave")
                    if doc and doc.seniority_level == "Senior" and shift.day_type == "holiday":
                        skip_reasons.append(f"{doc_name} is Senior on a holiday")

                    for comp_id in companions:
                        comp = doctors_by_id.get(comp_id)
                        comp_name = comp.full_name if comp else f"Doctor #{comp_id}"
                        if (comp_id, shift.date) in leaves_set:
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
                        "type": "ATTENDING_REQUEST_NO_ELIGIBLE_DATE",
                        "severity": "error",
                        "request_ids": [req.id],
                        "message": (
                            f"{doc_name}'s 'must work with' request targets attending "
                            f"'{req.attending_name}', but every matching shift is unavailable. "
                            f"Skipped dates: {date_details}."
                        ),
                        "suggestion": (
                            "Add another matching attending day, revoke the blocking leave, "
                            "or change the request doctors."
                        ),
                    })

            for comp_id in companions:
                comp = doctors_by_id.get(comp_id)
                comp_name = comp.full_name if comp else f"Doctor #{comp_id}"

                if comp_id not in doctors_by_id:
                    conflicts.append({
                        "type": "COMPANION_UNAVAILABLE",
                        "severity": "error",
                        "request_ids": [req.id],
                        "message": (
                            f"Required companion {comp_name} for {doc_name}'s "
                            f"'must work with' request does not exist."
                        ),
                        "suggestion": "Remove this companion or verify the doctor list.",
                    })
                    continue

                # If date-specific, check leave on that date
                if req.date and (comp_id, req.date) in leaves_set:
                    conflicts.append({
                        "type": "COMPANION_ON_LEAVE",
                        "severity": "error",
                        "request_ids": [req.id],
                        "message": (
                            f"Required companion {comp_name} has approved leave on "
                            f"{req.date.isoformat()}, but {doc_name} must work with them."
                        ),
                        "suggestion": (
                            f"Revoke {comp_name}'s leave on {req.date.isoformat()} "
                            f"or remove them from the companion list."
                        ),
                    })

    # --- Check 5: weekend_off_after_duty conflicts ---
    weekend_off_requests = [
        r for r in requests if r.request_type == "weekend_off_after_duty" and r.date
    ]
    must_work_requests = [
        r for r in requests if r.request_type == "must_work" and r.date
    ]

    for wk_req in weekend_off_requests:
        # Calculate the weekend days that should be off
        blocked_dates = _get_weekend_after(wk_req.date)
        doc_name = doctors_by_id.get(wk_req.doctor_id)
        doc_name = doc_name.full_name if doc_name else f"Doctor #{wk_req.doctor_id}"

        for mw_req in must_work_requests:
            if mw_req.doctor_id == wk_req.doctor_id and mw_req.date in blocked_dates:
                conflicts.append({
                    "type": "WEEKEND_OFF_CONFLICTS_MUST_WORK",
                    "severity": "error",
                    "request_ids": [wk_req.id, mw_req.id],
                    "message": (
                        f"{doc_name} has a 'weekend off after duty on {wk_req.date.isoformat()}' "
                        f"request, but also a 'must work' request on "
                        f"{mw_req.date.isoformat()} which falls in that weekend."
                    ),
                    "suggestion": "Remove one of the conflicting requests.",
                })

    # --- Check 6: must_not_work on a day where doctor has must_work_with as companion ---
    must_not_work_by_doctor_date = {}
    for req in requests:
        if req.request_type == "must_not_work" and req.date:
            must_not_work_by_doctor_date[(req.doctor_id, req.date)] = req

    for req in requests:
        if req.request_type == "must_work_with" and req.date:
            companions = req.get_required_people()
            for comp_id in companions:
                key = (comp_id, req.date)
                if key in must_not_work_by_doctor_date:
                    mnw = must_not_work_by_doctor_date[key]
                    doc_name = doctors_by_id.get(req.doctor_id)
                    doc_name = doc_name.full_name if doc_name else f"Doctor #{req.doctor_id}"
                    comp_name = doctors_by_id.get(comp_id)
                    comp_name = comp_name.full_name if comp_name else f"Doctor #{comp_id}"
                    conflicts.append({
                        "type": "COMPANION_MUST_NOT_WORK",
                        "severity": "error",
                        "request_ids": [req.id, mnw.id],
                        "message": (
                            f"{doc_name} must work with {comp_name} on "
                            f"{req.date.isoformat()}, but {comp_name} has a "
                            f"'must not work' request on the same date."
                        ),
                        "suggestion": "Remove or deactivate one of the conflicting requests.",
                    })

    # --- Check 7: must_work on a holiday when doctor is Senior ---
    for req in requests:
        if req.request_type == "must_work" and req.date:
            doc = doctors_by_id.get(req.doctor_id)
            if doc and doc.seniority_level == "Senior":
                shift = date_to_shift.get(req.date)
                if shift and shift.day_type == "holiday":
                    conflicts.append({
                        "type": "SENIOR_MUST_WORK_HOLIDAY",
                        "severity": "error",
                        "request_ids": [req.id],
                        "message": (
                            f"{doc.full_name} (Senior) is requested to work on "
                            f"{req.date.isoformat()}, which is a holiday. "
                            f"Seniors are prohibited from working on holidays."
                        ),
                        "suggestion": (
                            "Remove this request or change the day type. "
                            "Senior doctors cannot work on holidays."
                        ),
                    })

    return conflicts


def _get_weekend_after(dt):
    """
    Return the Saturday and Sunday of the weekend following the given date.
    If dt is Saturday, only Sunday of the same weekend is returned.
    If dt is Sunday, the next weekend (Sat+Sun) is returned.
    """
    weekday = dt.weekday()  # Mon=0 .. Sun=6
    blocked = set()

    if weekday == 5:  # Saturday — block Sunday
        blocked.add(dt + timedelta(days=1))
    elif weekday == 6:  # Sunday — block next Saturday + Sunday
        days_to_sat = 6  # 6 days to next Saturday
        next_sat = dt + timedelta(days=days_to_sat)
        blocked.add(next_sat)
        blocked.add(next_sat + timedelta(days=1))
    else:
        # Weekday — find next Saturday
        days_to_sat = 5 - weekday
        next_sat = dt + timedelta(days=days_to_sat)
        blocked.add(next_sat)
        blocked.add(next_sat + timedelta(days=1))

    return blocked
