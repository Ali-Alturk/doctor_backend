"""
CP-SAT scheduling engine for generating monthly on-call schedules.
"""

import time
from datetime import timedelta
from ortools.sat.python import cp_model

from models import db, Shift, ShiftAssignment, Doctor, LeaveRequest, MonthlySchedule, SpecialRequest


# ---------------------------------------------------------------------------
# Soft-constraint weights (tune here)
# ---------------------------------------------------------------------------
WEIGHT_WEEKEND_FAIRNESS = 10
WEIGHT_SHIFT_SPACING = 5
WEIGHT_CONSECUTIVE_PENALTY = 8
WEIGHT_WEEKLY_OVERLOAD = 4
WEIGHT_MID_WEEKEND_HOLIDAY = 12   # Mid'lerin hafta sonu/tatil tercih edilmesi için reward
WEIGHT_MID_FRIDAY_PREFERENCE = 8  # Cuma günleri Mid tercih edilmesi için reward


def generate_schedule(schedule_id, db_session, last_month_data=None, primer_config=None):
    """
    Main entry point. Generates a monthly on-call schedule using CP-SAT.
    primer_config: dict {doctor_id: primer_count} for Mid doctors.
    Returns a result dict with status, stats, adjustments, or conflicts.
    """
    try:
        inputs = _load_inputs(schedule_id, db_session)
        if inputs is None:
            return {
                "status": "INFEASIBLE",
                "schedule_id": None,
                "message": "Schedule or related data not found.",
                "conflicts": [],
            }

        if primer_config:
            primer_conflicts = _analyze_primer_config_conflicts(inputs, primer_config)
            if primer_conflicts:
                return {
                    "status": "INFEASIBLE",
                    "schedule_id": None,
                    "message": "Schedule could not be generated.",
                    "conflicts": primer_conflicts,
                    "adjustments": [],
                }

        model_obj, variables, adjustments = _build_model(inputs, last_month_data, primer_config)
        solver, status, elapsed = _solve_cp_model(model_obj)

        if _has_solution(solver, status):
            return _success_response(
                solver, status, elapsed, variables, inputs, db_session,
                schedule_id, adjustments,
            )

        from conflict_analyzer import analyze_conflicts

        conflicts = analyze_conflicts(schedule_id, db_session)
        if primer_config:
            conflicts.extend(_analyze_primer_config_conflicts(inputs, primer_config))
        return {
            "status": "INFEASIBLE",
            "schedule_id": None,
            "message": "Schedule could not be generated.",
            "conflicts": conflicts,
            "adjustments": adjustments,
        }

    except Exception as e:
        return {
            "status": "INFEASIBLE",
            "schedule_id": None,
            "message": f"Scheduler error: {str(e)}",
            "conflicts": [],
        }


def _solve_cp_model(model_obj, stop_after_first_solution=False):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 120.0
    solver.parameters.num_workers = 4
    solver.parameters.stop_after_first_solution = stop_after_first_solution
    start_time = time.time()
    status = solver.Solve(model_obj)
    elapsed = round(time.time() - start_time, 2)
    return solver, status, elapsed


def _has_solution(solver, status):
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return True
    if status == cp_model.UNKNOWN:
        try:
            solver.ObjectiveValue()
            return True
        except Exception:
            return False
    return False


def _success_response(solver, status, elapsed, variables, inputs, db_session,
                      schedule_id, adjustments):
    _extract_solution(solver, None, variables, inputs, db_session)
    if status == cp_model.OPTIMAL:
        status_str = "OPTIMAL"
    elif status == cp_model.FEASIBLE:
        status_str = "FEASIBLE"
    else:
        status_str = "FEASIBLE"
        adjustments.append({
            "date": "all",
            "type": "TIMEOUT_SOLUTION",
            "from": None,
            "to": None,
            "reason": (
                f"Solver timed out after {elapsed}s but found a valid solution. "
                f"Solution may not be globally optimal."
            ),
        })
    return {
        "status": status_str,
        "schedule_id": schedule_id,
        "message": "Schedule generated successfully.",
        "solver_stats": {
            "time_seconds": round(elapsed, 2),
            "objective_value": solver.ObjectiveValue(),
        },
        "adjustments": adjustments,
    }


def _analyze_primer_config_conflicts(inputs, primer_config):
    conflicts = []
    doctors = inputs["doctors"]
    shifts = inputs["shifts"]
    special_requests = inputs.get("special_requests", [])
    num_days = len(shifts)

    normalized_config = {
        int(doctor_id): max(0, int(value))
        for doctor_id, value in primer_config.items()
    }

    total_requested = sum(normalized_config.values())
    if total_requested != num_days:
        conflicts.append({
            "type": "PRIMER_CONFIG_CONFLICT",
            "dates": [],
            "message": (
                f"Primer targets sum to {total_requested}, but the schedule has {num_days} days. "
                f"Because primer targets are hard constraints, the total must be exactly {num_days}."
            ),
            "suggestion": "Adjust primer counts so their total equals the number of days.",
        })

    doctor_idx = {doc.id: idx for idx, doc in enumerate(doctors)}
    effective_targets = _calculate_effective_targets(inputs)
    leave_set = inputs["leave_set"]
    forced_non_primer_days = {doc.id: set() for doc in doctors}
    forced_primer_days = {doc.id: set() for doc in doctors}

    for sr in special_requests:
        if sr.request_type != "must_work_with" or not getattr(sr, "attending_name", None):
            continue
        target = sr.attending_name.strip().lower()
        companion_ids = sr.get_required_people()
        matching_dates = [
            shift.date for shift in shifts
            if shift.attending_name
            and shift.attending_name.strip().lower() == target
            and (sr.doctor_id, shift.date) not in leave_set
            and all((companion_id, shift.date) not in leave_set for companion_id in companion_ids)
        ]
        if sr.only_when_primer:
            forced_primer_days.setdefault(sr.doctor_id, set()).update(matching_dates)
            for companion_id in sr.get_required_people():
                forced_non_primer_days.setdefault(companion_id, set()).update(matching_dates)
        elif sr.only_when_not_primer:
            forced_non_primer_days.setdefault(sr.doctor_id, set()).update(matching_dates)

    for doc in doctors:
        requested_primers = normalized_config.get(doc.id, 0)
        idx = doctor_idx[doc.id]
        max_total_shifts = effective_targets.get(idx, 0) + 1
        primer_days = forced_primer_days.get(doc.id, set())
        non_primer_days = forced_non_primer_days.get(doc.id, set())
        forced_primer_count = len(primer_days)
        forced_non_primer_count = len(non_primer_days)

        overlapping_dates = primer_days & non_primer_days
        if overlapping_dates:
            conflicts.append({
                "type": "PRIMER_CONFIG_CONFLICT",
                "dates": sorted(dt.isoformat() for dt in overlapping_dates),
                "message": (
                    f"{doc.full_name} is forced to be both primer and non-primer "
                    f"on the same attending day(s) by special requests."
                ),
                "suggestion": "Change one of the special requests so the primer condition does not overlap.",
            })

        if forced_primer_count > requested_primers:
            conflicts.append({
                "type": "PRIMER_CONFIG_CONFLICT",
                "dates": sorted(dt.isoformat() for dt in primer_days),
                "message": (
                    f"{doc.full_name} is requested for {requested_primers} primer shifts, "
                    f"but special requests force at least {forced_primer_count} primer shifts."
                ),
                "suggestion": (
                    f"Increase {doc.full_name}'s primer target to at least {forced_primer_count}, "
                    "or relax the special requests forcing primer duty."
                ),
            })

        min_total_if_requested = requested_primers + forced_non_primer_count
        if min_total_if_requested > max_total_shifts:
            conflicts.append({
                "type": "PRIMER_CONFIG_CONFLICT",
                "dates": sorted(
                    dt.isoformat() for dt in non_primer_days
                ),
                "message": (
                    f"{doc.full_name} is requested for {requested_primers} primer shifts, "
                    f"but has at least {forced_non_primer_count} forced non-primer shifts from special requests. "
                    f"That requires at least {min_total_if_requested} total shifts, while this doctor's hard target limit is {max_total_shifts}."
                ),
                "suggestion": (
                    f"Reduce {doc.full_name}'s primer target to at most "
                    f"{max(0, max_total_shifts - forced_non_primer_count)}, "
                    f"or reduce the special requests forcing non-primer duties."
                ),
            })

    return conflicts


def _calculate_effective_targets(inputs):
    shifts = inputs["shifts"]
    doctors = inputs["doctors"]
    leave_set = inputs["leave_set"]
    effective_targets = {}
    for i, doc in enumerate(doctors):
        target = doc.target_shifts_per_month or 0
        if target == 0:
            effective_targets[i] = 0
            continue
        available_days = sum(
            1 for shift in shifts
            if (doc.id, shift.date) not in leave_set
        )
        max_possible_with_rest = (available_days + 1) // 2
        effective_targets[i] = min(target, max_possible_with_rest)
    return effective_targets


# ---------------------------------------------------------------------------
# _load_inputs
# ---------------------------------------------------------------------------
def _load_inputs(schedule_id, db_session):
    """Load schedule, shifts, doctors, and approved leaves."""
    schedule = db_session.query(MonthlySchedule).get(schedule_id)
    if schedule is None:
        return None

    shifts = (
        db_session.query(Shift)
        .filter_by(schedule_id=schedule_id)
        .order_by(Shift.date)
        .all()
    )
    if not shifts:
        return None

    doctors = db_session.query(Doctor).filter_by(admin_id=schedule.admin_id).all()
    if not doctors:
        return None

    all_dates = [s.date for s in shifts]
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

    # Load active special requests for this month
    special_requests = (
        db_session.query(SpecialRequest)
        .filter_by(
            admin_id=schedule.admin_id,
            year=schedule.year,
            month=schedule.month,
            is_active=True,
        )
        .all()
    )

    return {
        "schedule": schedule,
        "shifts": shifts,
        "doctors": doctors,
        "leave_set": leave_set,
        "dates": all_dates,
        "special_requests": special_requests,
    }


# ---------------------------------------------------------------------------
# _build_model
# ---------------------------------------------------------------------------
def _build_model(inputs, last_month_data, primer_config=None):
    """Build CP-SAT model with all variables, hard and soft constraints."""
    model = cp_model.CpModel()
    shifts = inputs["shifts"]
    doctors = inputs["doctors"]
    dates = inputs["dates"]
    leave_set = inputs["leave_set"]

    num_days = len(shifts)
    num_doctors = len(doctors)

    doctor_ids = [d.id for d in doctors]
    doctor_idx = {d.id: i for i, d in enumerate(doctors)}

    # Build unavailable set from last month carry-over
    carryover_ids = set()
    if last_month_data and "doctor_ids_who_worked_last_day" in last_month_data:
        carryover_ids = set(last_month_data["doctor_ids_who_worked_last_day"])

    # Shift day mapping
    shift_map = {i: shifts[i] for i in range(num_days)}
    date_to_idx = {shifts[i].date: i for i in range(num_days)}

    # --- Decision variables ---
    x = {}
    for d in range(num_days):
        for i in range(num_doctors):
            x[(d, i)] = model.NewBoolVar(f"x_d{d}_i{i}")

    # --- Primer decision variables ---
    p = {}
    for d in range(num_days):
        for i in range(num_doctors):
            p[(d, i)] = model.NewBoolVar(f"p_d{d}_i{i}")

    adjustments = []

    # --- Pre-calculate effective targets considering leaves ---
    # This must be done before any constraints that depend on targets
    effective_targets = {}
    for i, doc in enumerate(doctors):
        target = doc.target_shifts_per_month or 0
        if target == 0:
            effective_targets[i] = 0
            continue
        
        # Count how many days this doctor is available (not on leave)
        available_days = sum(
            1 for d in range(num_days)
            if (doc.id, shift_map[d].date) not in leave_set
        )
        
        # Maximum possible shifts considering post-call rest (can work at most every other day)
        max_possible_with_rest = (available_days + 1) // 2
        
        # Effective target is the minimum of original target and what's actually possible
        effective_target = min(target, max_possible_with_rest)
        effective_targets[i] = effective_target
        
        if effective_target < target:
            if effective_target == 0:
                adjustments.append({
                    "date": "all",
                    "type": "DOCTOR_UNAVAILABLE",
                    "from": f"{doc.full_name} target={target}",
                    "to": "0 shifts",
                    "reason": (
                        f"{doc.full_name} has {num_days - available_days} leave days "
                        f"(available: {available_days}), cannot meet target of {target} shifts. "
                        f"Excluded from scheduling this month."
                    ),
                })
            else:
                adjustments.append({
                    "date": "all",
                    "type": "TARGET_REDUCED",
                    "from": f"{doc.full_name} target={target}",
                    "to": f"{effective_target} shifts",
                    "reason": (
                        f"{doc.full_name} has {num_days - available_days} leave days. "
                        f"Available days: {available_days}, max possible with rest: {max_possible_with_rest}. "
                        f"Target reduced from {target} to {effective_target}."
                    ),
                })

    # --- Hard Constraints ---

    # HC4 — Leave Exclusion (İzin ve müsait olmama)
    for d in range(num_days):
        shift = shift_map[d]
        for i, doc in enumerate(doctors):
            if (doc.id, shift.date) in leave_set:
                model.Add(x[(d, i)] == 0)

    # Previous Month Carry-Over
    if dates and carryover_ids:
        for i, doc in enumerate(doctors):
            if doc.id in carryover_ids:
                model.Add(x[(0, i)] == 0)

    # HC5 — Post-Call Rest (Nöbet sonrası dinlenme)
    # Doktor d günü çalıştıysa, d+1 günü çalışamaz
    for d in range(num_days - 1):
        for i in range(num_doctors):
            model.Add(x[(d, i)] + x[(d + 1, i)] <= 1)

    # HC6 — No 3 consecutive close shifts (Post-call zaten bunu önler)
    for d in range(num_days - 2):
        for i in range(num_doctors):
            model.Add(x[(d, i)] + x[(d + 1, i)] + x[(d + 2, i)] <= 2)

    # --- Seniority indices (using effective targets, not original) ---
    senior_indices = [i for i, doc in enumerate(doctors) 
                      if doc.seniority_level == "Senior" and effective_targets.get(i, 0) > 0]
    mid_indices = [i for i, doc in enumerate(doctors)
                   if doc.seniority_level == "Mid"
                   and effective_targets.get(i, 0) > 0]
    junior_indices = [i for i, doc in enumerate(doctors)
                      if doc.seniority_level == "Junior"
                      and effective_targets.get(i, 0) > 0]
    non_senior_indices = mid_indices + junior_indices

    total_senior_targets = sum(
        effective_targets.get(i, 0) for i in senior_indices
    )

    # --- Rank-sorted lists within each seniority category ---
    # Lower seniority_rank = more senior (e.g., rank 1 is the most senior)
    senior_by_rank = sorted(
        senior_indices,
        key=lambda i: (doctors[i].seniority_rank or 999),
    )
    mid_by_rank = sorted(
        mid_indices,
        key=lambda i: (doctors[i].seniority_rank or 999),
    )
    junior_by_rank = sorted(
        junior_indices,
        key=lambda i: (doctors[i].seniority_rank or 999),
    )

    # -----------------------------------------------------------------------
    # HC_SENIOR_NO_HOLIDAY — Seniorlar tatil günlerinde KESİNLİKLE çalışamaz
    # ABSOLUTE CONSTRAINT: No exceptions — including Professor/Specialist days.
    # This constraint takes priority over ALL other rules (including HC3).
    # -----------------------------------------------------------------------
    holiday_day_indices = [
        d for d in range(num_days)
        if shift_map[d].day_type == "holiday"
    ]
    for d in holiday_day_indices:
        for i in senior_indices:
            model.Add(x[(d, i)] == 0)

    if holiday_day_indices and senior_indices:
        adjustments.append({
            "date": "all",
            "type": "SENIOR_NO_HOLIDAY",
            "from": None,
            "to": None,
            "reason": (
                f"ABSOLUTE hard constraint: Senior doktorlar tatil günlerinde ({len(holiday_day_indices)} gün) "
                f"KESİNLİKLE çalışamaz — Professor/Specialist günleri dahil. "
                f"Tatil nöbetleri Mid/Junior'lara dağıtılır."
            ),
        })

    # -----------------------------------------------------------------------
    # HC_SENIOR_MAX1_WEEKEND — Her Senior ayda en fazla 1 hafta sonu nöbeti
    # Cumartesi (weekday=5) veya Pazar (weekday=6) olan VEYA day_type=="weekend"
    # olan tüm günler dahil — veritabanı tutarsızlığına karşı çift güvence
    # -----------------------------------------------------------------------
    weekend_only_indices = [
        d for d in range(num_days)
        if shift_map[d].day_type == "weekend"
        or dates[d].weekday() in (5, 6)  # Sat=5, Sun=6
    ]
    for i in senior_indices:
        model.Add(sum(x[(d, i)] for d in weekend_only_indices) <= 1)

    if weekend_only_indices and senior_indices:
        adjustments.append({
            "date": "all",
            "type": "SENIOR_MAX1_WEEKEND",
            "from": None,
            "to": None,
            "reason": (
                f"Hard constraint: Her Senior doktor ayda en fazla 1 hafta sonu "
                f"({len(weekend_only_indices)} hafta sonu günü mevcut) nöbeti tutabilir."
            ),
        })

    # -----------------------------------------------------------------------
    # HC_HOLIDAY_MID_REQUIRED — Tatil günlerinde en az 1 Mid zorunlu (müsaitse)
    # -----------------------------------------------------------------------
    for d in holiday_day_indices:
        shift = shift_map[d]
        avail_mid_holiday = [
            i for i in mid_indices
            if (doctors[i].id, shift.date) not in leave_set
        ]
        if avail_mid_holiday:
            model.Add(sum(x[(d, i)] for i in avail_mid_holiday) >= 1)
        else:
            # Tatilde müsait Mid yoksa adjustments'a yaz
            adjustments.append({
                "date": shift.date.isoformat(),
                "type": "HOLIDAY_NO_MID_AVAILABLE",
                "from": "Mid zorunlu",
                "to": "Mid yok (Junior ile devam)",
                "reason": (
                    f"{shift.date.isoformat()} tatil günü için müsait Mid doktor bulunamadı. "
                    f"Nöbet Junior doktorlarla karşılanacak."
                ),
            })

    # HC_HIERARCHY — Daily Hierarchical Team Composition
    # القاعدة الأولى: يجب أن يتكون الفريق الطبي بشكل تدريجي (Senior → Mid → Junior)
    # القاعدة الثانية: إذا لم يتوفر Mid، يُعيّن Senior + Junior حسب الأقدمية
    for d in range(num_days):
        shift = shift_map[d]

        # Determine which doctors from each level are available today
        avail_senior = [i for i in senior_indices
                        if (doctors[i].id, shift.date) not in leave_set]
        avail_mid = [i for i in mid_indices
                     if (doctors[i].id, shift.date) not in leave_set]
        avail_junior = [i for i in junior_indices
                        if (doctors[i].id, shift.date) not in leave_set]

        # Calendar-based detection: holidays and weekends have special senior rules
        is_holiday = shift.day_type == "holiday"
        is_weekend = (shift.day_type == "weekend" or dates[d].weekday() in (5, 6))

        if not is_holiday and not is_weekend:
            # WORKDAYS: hierarchy rules apply
            # If total senior capacity < total days, senior requirement becomes soft
            # (handled by SC0 penalty in _build_objective)
            senior_hard = total_senior_targets >= num_days

            if avail_senior and avail_mid and avail_junior:
                if senior_hard:
                    model.Add(sum(x[(d, i)] for i in avail_senior) >= 1)
                model.Add(sum(x[(d, i)] for i in avail_mid) >= 1)
                model.Add(sum(x[(d, i)] for i in avail_junior) >= 1)
            elif avail_senior and not avail_mid and avail_junior:
                if senior_hard:
                    model.Add(sum(x[(d, i)] for i in avail_senior) >= 1)
                model.Add(sum(x[(d, i)] for i in avail_junior) >= 1)
                adjustments.append({
                    "date": shift.date.isoformat(),
                    "type": "MID_FALLBACK",
                    "from": "Senior + Mid + Junior",
                    "to": "Senior + Junior (no Mid available)",
                    "reason": (
                        f"No Mid-level doctor available on {shift.date.isoformat()}. "
                        f"Assigning Senior + Junior pair based on seniority rank."
                    ),
                })
            elif avail_senior and avail_mid and not avail_junior:
                if senior_hard:
                    model.Add(sum(x[(d, i)] for i in avail_senior) >= 1)
                model.Add(sum(x[(d, i)] for i in avail_mid) >= 1)
            else:
                has_senior = model.NewBoolVar(f"has_senior_d{d}")
                has_mid = model.NewBoolVar(f"has_mid_d{d}")
                has_junior = model.NewBoolVar(f"has_junior_d{d}")

                if avail_senior:
                    s_sum = sum(x[(d, i)] for i in avail_senior)
                    model.Add(s_sum >= 1).OnlyEnforceIf(has_senior)
                    model.Add(s_sum == 0).OnlyEnforceIf(has_senior.Not())
                else:
                    model.Add(has_senior == 0)

                if avail_mid:
                    m_sum = sum(x[(d, i)] for i in avail_mid)
                    model.Add(m_sum >= 1).OnlyEnforceIf(has_mid)
                    model.Add(m_sum == 0).OnlyEnforceIf(has_mid.Not())
                else:
                    model.Add(has_mid == 0)

                if avail_junior:
                    j_sum = sum(x[(d, i)] for i in avail_junior)
                    model.Add(j_sum >= 1).OnlyEnforceIf(has_junior)
                    model.Add(j_sum == 0).OnlyEnforceIf(has_junior.Not())
                else:
                    model.Add(has_junior == 0)

                total_levels = len([g for g in [avail_senior, avail_mid, avail_junior] if g])
                if total_levels >= 2:
                    model.Add(has_senior + has_mid + has_junior >= 2)
        elif is_weekend:
            # WEEKENDS: Senior is OPTIONAL (max 1 weekend/month per HC_SENIOR_MAX1_WEEKEND).
            # Requiring >= 1 Senior on every weekend would conflict with the cap.
            # Hierarchy: Mid + Junior required, Senior is a bonus.
            if avail_mid:
                model.Add(sum(x[(d, i)] for i in avail_mid) >= 1)
            if avail_junior:
                model.Add(sum(x[(d, i)] for i in avail_junior) >= 1)
        else:
            # HOLIDAYS: Senior is FORBIDDEN (HC_SENIOR_NO_HOLIDAY enforces x=0)
            # Hierarchy is Mid + Junior only (HC_HOLIDAY_MID_REQUIRED handles Mid >= 1)
            if avail_mid:
                model.Add(sum(x[(d, i)] for i in avail_mid) >= 1)
            if avail_junior:
                model.Add(sum(x[(d, i)] for i in avail_junior) >= 1)

    if total_senior_targets < num_days:
        adjustments.append({
            "date": "all",
            "type": "SENIOR_RELAXED",
            "from": f"{total_senior_targets} senior shifts",
            "to": f"{num_days} days",
            "reason": (
                f"Toplam Senior nöbet hedefi ({total_senior_targets}) < gün sayısı ({num_days}). "
                f"Bazı günlerde Senior bulunmayabilir."
            ),
        })

    # HC3 — Professor/Specialist Rule (Hoca Kuralı)
    # Profesör VEYA Uzman günlerinde (FS veya BK attending), rank 1 veya rank 2 olmalı
    # İkisi birden de olabilir (2 Senior aynı gün mümkün)
    # Sadece rank 1 ve rank 2 ikisi de izinliyse, rank 3+ devreye girer
    # NOT: Tatil günlerinde HC_SENIOR_NO_HOLIDAY MUTLAK öncelik alır.
    # HC3 tatil günlerinde Senior atamaz — Mid/Junior ile karşılanır.
    rank1_idx = None
    rank2_idx = None
    seniors_by_rank = sorted(
        [(i, doc) for i, doc in enumerate(doctors)
         if doc.seniority_level == "Senior" and effective_targets.get(i, 0) > 0],
        key=lambda t: (t[1].seniority_rank or 999),
    )
    for idx, doc in seniors_by_rank:
        if doc.seniority_rank == 1:
            rank1_idx = idx
        elif doc.seniority_rank == 2:
            rank2_idx = idx

    for d in range(num_days):
        shift = shift_map[d]
        if shift.attending_degree not in ("Professor", "Specialist"):
            continue

        # ABSOLUTE: HC_SENIOR_NO_HOLIDAY takes priority over HC3.
        # On holidays, seniors are forbidden (x=0 already set above).
        # HC3 cannot override this — Professor/Specialist coverage on
        # holidays must be handled by Mid/Junior doctors.
        if shift.day_type == "holiday":
            adjustments.append({
                "date": shift.date.isoformat(),
                "type": "HC3_HOLIDAY_OVERRIDE",
                "from": "Senior (Professor/Specialist rule)",
                "to": "Mid/Junior (holiday constraint takes priority)",
                "reason": (
                    f"{shift.date.isoformat()} is a holiday with {shift.attending_degree} attending. "
                    f"HC_SENIOR_NO_HOLIDAY takes absolute priority — Senior doctors cannot work. "
                    f"Professor/Specialist coverage falls to Mid/Junior doctors."
                ),
            })
            continue

        # Check availability of rank 1 and rank 2
        r1_available = rank1_idx is not None and (
            doctors[rank1_idx].id, shift.date
        ) not in leave_set
        r2_available = rank2_idx is not None and (
            doctors[rank2_idx].id, shift.date
        ) not in leave_set

        if r1_available and r2_available:
            model.Add(x[(d, rank1_idx)] + x[(d, rank2_idx)] >= 1)
        elif r1_available:
            model.Add(x[(d, rank1_idx)] == 1)
        elif r2_available:
            model.Add(x[(d, rank2_idx)] == 1)
        else:
            for idx, doc in seniors_by_rank:
                if (doc.id, shift.date) not in leave_set:
                    model.Add(x[(d, idx)] == 1)
                    break

    # -----------------------------------------------------------------------
    # HC_SPECIAL_REQUESTS — User-defined special scheduling constraints
    # Must be applied BEFORE HC8 (target shifts) so that forced attending
    # days can be accounted for in the target calculation.
    # -----------------------------------------------------------------------
    special_requests = inputs.get("special_requests", [])

    # Pre-calculate forced days from attending_name special requests
    # so we can adjust effective_targets before HC8 locks them in.
    forced_extra_days = {}  # {doctor_index: set of day indices forced by attending}
    if special_requests:
        for sr in special_requests:
            if sr.request_type == "must_work_with" and getattr(sr, "attending_name", None):
                att_target = sr.attending_name.strip().lower()
                att_days = [
                    d for d in range(num_days)
                    if shift_map[d].attending_name
                    and shift_map[d].attending_name.strip().lower() == att_target
                ]
                # Primary doctor (only if not on leave)
                i = doctor_idx.get(sr.doctor_id)
                if i is not None and effective_targets.get(i, 0) > 0:
                    if i not in forced_extra_days:
                        forced_extra_days[i] = set()
                    for d in att_days:
                        if (doctors[i].id, shift_map[d].date) not in leave_set:
                            forced_extra_days[i].add(d)
                # Companion doctors (only if not on leave)
                for comp_id in sr.get_required_people():
                    ci = doctor_idx.get(comp_id)
                    if ci is not None and effective_targets.get(ci, 0) > 0:
                        if ci not in forced_extra_days:
                            forced_extra_days[ci] = set()
                        for d in att_days:
                            if (doctors[ci].id, shift_map[d].date) not in leave_set:
                                forced_extra_days[ci].add(d)

        # Also count must_work forced days
        for sr in special_requests:
            if sr.request_type == "must_work" and sr.date:
                i = doctor_idx.get(sr.doctor_id)
                d_idx = date_to_idx.get(sr.date)
                if i is not None and d_idx is not None and effective_targets.get(i, 0) > 0:
                    if i not in forced_extra_days:
                        forced_extra_days[i] = set()
                    forced_extra_days[i].add(d_idx)

    # Adjust effective_targets: if forced days exceed the original target,
    # increase the target to accommodate.
    for i, forced_days in forced_extra_days.items():
        original_target = effective_targets.get(i, 0)
        if original_target == 0:
            continue
        # Count how many of these forced days are NOT already "expected"
        # (i.e., would the doctor have worked those days anyway?)
        # Conservative approach: ensure target >= number of forced days
        num_forced = len(forced_days)
        if num_forced > original_target:
            new_target = num_forced
            adjustments.append({
                "date": "all",
                "type": "TARGET_INCREASED_BY_SPECIAL_REQ",
                "from": f"{doctors[i].full_name} target={original_target}",
                "to": f"{new_target} shifts",
                "reason": (
                    f"{doctors[i].full_name} is forced to work on {num_forced} days "
                    f"by special requests (attending/must_work), which exceeds the "
                    f"original target of {original_target}. Target increased to {new_target}."
                ),
            })
            effective_targets[i] = new_target

    if special_requests:
        _apply_special_requests(
            model, x, p, special_requests, inputs,
            num_days, num_doctors, doctors, doctor_idx, shift_map,
            date_to_idx, leave_set, adjustments,
        )

    # HC8 — Per-Doctor Target Shifts (flexible hard constraint)
    # Uses pre-calculated effective_targets that account for leaves AND special requests
    # Allow ±1 flexibility to prevent infeasibility from post-call rest + leave interactions
    for i, doc in enumerate(doctors):
        effective_target = effective_targets.get(i, 0)
        if effective_target == 0:
            for d in range(num_days):
                model.Add(x[(d, i)] == 0)
        else:
            total_shifts_i = sum(x[(d, i)] for d in range(num_days))
            model.Add(total_shifts_i >= max(1, effective_target - 1))
            model.Add(total_shifts_i <= effective_target + 1)

    # HC1 — Dynamic Daily Capacity (calculated from EFFECTIVE targets after leave adjustment)
    # Total shifts = sum of effective targets (not original targets)
    total_target_shifts = sum(effective_targets.values())
    base_capacity = total_target_shifts // num_days
    remainder = total_target_shifts % num_days

    # Priority: distribute extra slots with 3-tier ordering:
    #   1. Weekend + Holiday days get extra slots first (most need)
    #   2. Workdays (non-Friday) get extra slots next
    #   3. Fridays get extra slots last (least need)
    # Calendar-based weekend detection (weekday 5=Sat, 6=Sun) is used
    # alongside day_type for robustness against DB inconsistencies.
    day_priority = sorted(
        range(num_days),
        key=lambda d: (
            0 if shift_map[d].day_type in ("weekend", "holiday")
                 or dates[d].weekday() in (5, 6) else
            2 if dates[d].weekday() == 4 else  # Friday = last priority
            1,  # regular workday
            d,
        ),
    )

    day_capacities = [base_capacity] * num_days
    for idx in range(remainder):
        day_capacities[day_priority[idx]] += 1

    adjustments.append({
        "date": "all",
        "type": "DYNAMIC_CAPACITY",
        "from": None,
        "to": None,
        "reason": (
            f"Total target shifts: {total_target_shifts}. "
            f"Base daily capacity: {base_capacity}. "
            f"Extra slot on {remainder} days "
            f"(priority: weekends/holidays → workdays → Fridays last)."
        ),
    })

    for d in range(num_days):
        daily_sum = sum(x[(d, i)] for i in range(num_doctors))
        target_cap = day_capacities[d]
        model.Add(daily_sum >= max(1, target_cap - 1))
        model.Add(daily_sum <= target_cap + 1)

    # --- Primer Constraints ---
    # Can only be primer if working that shift
    for d in range(num_days):
        for i in range(num_doctors):
            model.Add(p[(d, i)] <= x[(d, i)])

    # Exactly 1 primer per day
    # Cache senior-working indicator per day (reused for Mid/Junior primer ban)
    senior_working_vars = {}
    for d in range(num_days):
        model.Add(sum(p[(d, i)] for i in range(num_doctors)) == 1)

        # If any senior works on day d, the primer MUST be a senior
        if senior_indices:
            senior_working_d = model.NewBoolVar(f"any_senior_works_d{d}")
            sum_senior_x = sum(x[(d, i)] for i in senior_indices)
            model.Add(sum_senior_x >= 1).OnlyEnforceIf(senior_working_d)
            model.Add(sum_senior_x == 0).OnlyEnforceIf(senior_working_d.Not())

            sum_senior_p = sum(p[(d, i)] for i in senior_indices)
            model.Add(sum_senior_p == 1).OnlyEnforceIf(senior_working_d)
            senior_working_vars[d] = senior_working_d

    if primer_config:
        normalized_primer_config = {
            doc.id: max(0, int(primer_config.get(doc.id, 0)))
            for doc in doctors
        }
        configured_total = sum(normalized_primer_config.values())
        adjustments.append({
            "date": "all",
            "type": "PRIMER_CONFIG_HARD",
            "from": f"{configured_total} configured primers",
            "to": f"{num_days} required primers",
            "reason": "Primer counts are enforced as hard constraints.",
        })
    else:
        normalized_primer_config = None

    for i, doc in enumerate(doctors):
        if normalized_primer_config and doc.id in normalized_primer_config:
            target_primer = normalized_primer_config[doc.id]
            model.Add(sum(p[(d, i)] for d in range(num_days)) == target_primer)
        if doc.seniority_level in ("Junior", "Mid"):
            # Mid/Junior can only be primer if NO senior works that day
            for d in range(num_days):
                if d in senior_working_vars:
                    model.Add(p[(d, i)] == 0).OnlyEnforceIf(senior_working_vars[d])

    # --- Soft Constraints / Objective ---
    _build_objective(
        model, x, p, inputs, num_days, num_doctors, doctors, shift_map, dates,
        senior_indices=senior_indices,
        mid_indices=mid_indices,
        junior_indices=junior_indices,
        senior_by_rank=senior_by_rank,
        mid_by_rank=mid_by_rank,
        junior_by_rank=junior_by_rank,
        total_senior_targets=total_senior_targets,
        effective_targets=effective_targets,
    )

    return model, (x, p), adjustments


# ---------------------------------------------------------------------------
# _build_objective
# ---------------------------------------------------------------------------
def _build_objective(model, x, p, inputs, num_days, num_doctors, doctors, shift_map, dates,
                     senior_indices=None, mid_indices=None, junior_indices=None,
                     senior_by_rank=None, mid_by_rank=None, junior_by_rank=None,
                     total_senior_targets=0, effective_targets=None):
    """Build the weighted-sum objective from soft constraints."""
    penalties = []
    
    if effective_targets is None:
        effective_targets = {i: (doc.target_shifts_per_month or 0) for i, doc in enumerate(doctors)}

    # SC_TARGET_DEVIATION — Penalize doctors working ≠ their target
    for i, doc in enumerate(doctors):
        eff = effective_targets.get(i, 0)
        if eff > 0:
            total_shifts_i = sum(x[(d, i)] for d in range(num_days))
            dev = model.NewIntVar(0, 2, f"target_dev_{i}")
            model.Add(dev >= total_shifts_i - eff)
            model.Add(dev >= eff - total_shifts_i)
            penalties.append(dev * 50)

    # SC_CAPACITY_DEVIATION — Penalize daily capacity deviation
    total_target_shifts = sum(effective_targets.values())
    base_cap = total_target_shifts // num_days if num_days > 0 else 3
    rem = total_target_shifts % num_days if num_days > 0 else 0
    day_priority = sorted(
        range(num_days),
        key=lambda d: (
            0 if shift_map[d].day_type in ("weekend", "holiday")
                 or dates[d].weekday() in (5, 6) else
            2 if dates[d].weekday() == 4 else 1,
            d,
        ),
    )
    day_caps = [base_cap] * num_days
    for idx in range(rem):
        day_caps[day_priority[idx]] += 1

    for d in range(num_days):
        daily_sum = sum(x[(d, i)] for i in range(num_doctors))
        cap_dev = model.NewIntVar(0, 2, f"cap_dev_d{d}")
        model.Add(cap_dev >= daily_sum - day_caps[d])
        model.Add(cap_dev >= day_caps[d] - daily_sum)
        penalties.append(cap_dev * 40)

    # Build seniority groups index (used by multiple constraints below)
    seniority_groups = {}
    for i, doc in enumerate(doctors):
        if effective_targets.get(i, 0) > 0:
            seniority_groups.setdefault(doc.seniority_level, []).append(i)

    # SC0 — Senior Coverage Soft Penalty
    # When total senior targets < num_days, penalize days without any senior
    if senior_indices and total_senior_targets < num_days:
        WEIGHT_NO_SENIOR = 15
        for d in range(num_days):
            # Skip holidays and weekends — seniors are optional/forbidden there
            if (shift_map[d].day_type in ("holiday", "weekend")
                    or dates[d].weekday() in (5, 6)):
                continue
            no_senior = model.NewBoolVar(f"no_senior_d{d}")
            senior_sum = sum(x[(d, i)] for i in senior_indices)
            model.Add(senior_sum == 0).OnlyEnforceIf(no_senior)
            model.Add(senior_sum >= 1).OnlyEnforceIf(no_senior.Not())
            penalties.append(no_senior * WEIGHT_NO_SENIOR)

    # --- Classify days by type ---
    leave_set = inputs["leave_set"]
    weekend_holiday_day_indices = [
        d for d in range(num_days)
        if shift_map[d].day_type in ("weekend", "holiday")
    ]
    workday_indices = [
        d for d in range(num_days)
        if shift_map[d].day_type == "workday"
    ]
    friday_indices = [
        d for d in range(num_days) if dates[d].weekday() == 4
    ]
    holiday_indices = [
        d for d in range(num_days)
        if shift_map[d].day_type == "holiday"
    ]
    weekend_indices = [
        d for d in range(num_days)
        if shift_map[d].day_type == "weekend"
        or dates[d].weekday() in (5, 6)
    ]

    # NOTE: SC_HOLIDAY_REVERSE and SC_WORKDAY_SENIORITY soft constraints have been
    # REMOVED because they are now covered by hard constraints:
    #   HC_SENIOR_NO_HOLIDAY  — seniors cannot work on holidays (absolute)
    #   HC_SENIOR_MAX1_WEEKEND — each senior works at most 1 weekend day per month

    # SC_MID_WEEKEND_HOLIDAY — Hafta sonu ve tatil günlerinde Mid tercih edilsin
    # Mid çalışırsa reward (negatif penalty), Senior/Junior çalışırsa hafif penalty
    if mid_indices:
        for d in weekend_holiday_day_indices:
            # Mid çalışırsa ödüllendir (penalty minimize edildiği için negatif = iyi)
            for i in mid_indices:
                penalties.append(x[(d, i)] * (-WEIGHT_MID_WEEKEND_HOLIDAY))
            # Senior hafta sonunda çalışırsa hafif ek penalty (tatilde zaten HC ile yasak)
            for i in senior_indices:
                if shift_map[d].day_type == "weekend":
                    penalties.append(x[(d, i)] * WEIGHT_MID_WEEKEND_HOLIDAY)

    # SC_FRIDAY — Friday Seniority Avoidance (graduated by rank/level)
    # Senior için penalty devam eder, Mid için cuma SOFT tercih (penalty kaldırıldı)
    WEIGHT_FRIDAY_SENIOR = 2
    for d in friday_indices:
        if senior_indices:
            for i in senior_indices:
                rank = doctors[i].seniority_rank or 1
                rank_multiplier = max(1, 3 - rank)  # rank1->2, rank2->1, rank3+->1
                penalties.append(x[(d, i)] * WEIGHT_FRIDAY_SENIOR * rank_multiplier)
        # Mid için cuma günü hafif REWARD — cuma soft tercih
        if mid_indices:
            for i in mid_indices:
                penalties.append(x[(d, i)] * (-WEIGHT_MID_FRIDAY_PREFERENCE))

    # SC_FRIDAY_FAIRNESS — Balance Friday shifts equally within each seniority group
    WEIGHT_FRIDAY_FAIRNESS = 20
    for level, indices in seniority_groups.items():
        if len(indices) < 2 or not friday_indices:
            continue

        friday_counts = []
        for i in indices:
            fri_count = model.NewIntVar(0, len(friday_indices), f"fri_{level}_{i}")
            model.Add(
                fri_count == sum(x[(d, i)] for d in friday_indices)
            )
            friday_counts.append(fri_count)

        max_fri = model.NewIntVar(0, len(friday_indices), f"max_fri_{level}")
        min_fri = model.NewIntVar(0, len(friday_indices), f"min_fri_{level}")
        model.AddMaxEquality(max_fri, friday_counts)
        model.AddMinEquality(min_fri, friday_counts)

        fri_diff = model.NewIntVar(0, len(friday_indices), f"fri_diff_{level}")
        model.Add(fri_diff == max_fri - min_fri)
        penalties.append(fri_diff * WEIGHT_FRIDAY_FAIRNESS)

    # SC_RANK_ORDER — Within each level per day, prefer lower rank (more senior)
    WEIGHT_RANK_ORDER = 3
    for level_ranks in [senior_by_rank, mid_by_rank, junior_by_rank]:
        if not level_ranks or len(level_ranks) < 2:
            continue
        for d in workday_indices:
            shift = shift_map[d]
            for pos in range(len(level_ranks) - 1):
                senior_doc_i = level_ranks[pos]
                junior_doc_i = level_ranks[pos + 1]
                s_avail = (doctors[senior_doc_i].id, shift.date) not in leave_set
                j_avail = (doctors[junior_doc_i].id, shift.date) not in leave_set
                if not (s_avail and j_avail):
                    continue
                junior_works = x[(d, junior_doc_i)]
                senior_off = model.NewBoolVar(f"rank_off_{d}_{senior_doc_i}_{junior_doc_i}")
                model.Add(x[(d, senior_doc_i)] == 0).OnlyEnforceIf(senior_off)
                model.Add(x[(d, senior_doc_i)] == 1).OnlyEnforceIf(senior_off.Not())
                violation = model.NewBoolVar(f"rank_viol_{d}_{senior_doc_i}_{junior_doc_i}")
                model.AddMultiplicationEquality(violation, [junior_works, senior_off])
                penalties.append(violation * WEIGHT_RANK_ORDER)

    # SC_DIVERSITY — Penalize senior(primer) + all-junior teams
    WEIGHT_DIVERSITY = 6
    if senior_indices and mid_indices:
        for d in range(num_days):
            no_mid = model.NewBoolVar(f"no_mid_d{d}")
            mid_sum = sum(x[(d, i)] for i in mid_indices)
            model.Add(mid_sum == 0).OnlyEnforceIf(no_mid)
            model.Add(mid_sum >= 1).OnlyEnforceIf(no_mid.Not())
            
            has_senior_primer = model.NewBoolVar(f"senior_primer_d{d}")
            senior_primer_sum = sum(p[(d, i)] for i in senior_indices)
            model.Add(senior_primer_sum >= 1).OnlyEnforceIf(has_senior_primer)
            model.Add(senior_primer_sum == 0).OnlyEnforceIf(has_senior_primer.Not())
            
            both = model.NewBoolVar(f"senior_nomid_d{d}")
            model.AddMultiplicationEquality(both, [has_senior_primer, no_mid])
            penalties.append(both * WEIGHT_DIVERSITY)

    # SC1 — Weekend/Holiday Fairness (minimize max diff within seniority)
    # For seniors: only count weekend days (holiday is now forbidden for seniors)
    # For mid/junior: count both weekend and holiday days
    for level, indices in seniority_groups.items():
        if len(indices) < 2:
            continue

        if level == "Senior":
            # Seniors can only work weekends (not holidays), so fairness over weekends only
            relevant_indices = weekend_indices
        else:
            # Mid and Junior cover both weekends and holidays
            relevant_indices = weekend_holiday_day_indices

        if not relevant_indices:
            continue

        wh_counts = []
        for i in indices:
            wh_count = model.NewIntVar(0, num_days, f"wh_{level}_{i}")
            model.Add(
                wh_count == sum(x[(d, i)] for d in relevant_indices)
            )
            wh_counts.append(wh_count)

        max_wh = model.NewIntVar(0, num_days, f"max_wh_{level}")
        min_wh = model.NewIntVar(0, num_days, f"min_wh_{level}")
        model.AddMaxEquality(max_wh, wh_counts)
        model.AddMinEquality(min_wh, wh_counts)

        diff = model.NewIntVar(0, num_days, f"wh_diff_{level}")
        model.Add(diff == max_wh - min_wh)
        penalties.append(diff * WEIGHT_WEEKEND_FAIRNESS)

    # SC2 — Shift Spacing (penalize shifts within 3 days of each other)
    for i in range(num_doctors):
        for d in range(num_days):
            for d2 in range(d + 1, min(d + 4, num_days)):
                if d2 == d + 1:
                    continue
                pair = model.NewBoolVar(f"spacing_{i}_{d}_{d2}")
                model.AddMultiplicationEquality(pair, [x[(d, i)], x[(d2, i)]])
                penalties.append(pair * WEIGHT_SHIFT_SPACING)

    # SC3 — Consecutive Shift Penalty
    for i in range(num_doctors):
        for d in range(num_days - 1):
            consec = model.NewBoolVar(f"consec_{i}_{d}")
            model.AddMultiplicationEquality(consec, [x[(d, i)], x[(d + 1, i)]])
            penalties.append(consec * WEIGHT_CONSECUTIVE_PENALTY)

    # SC4 — Weekly Overload Penalty (>2 shifts in same ISO week)
    from collections import defaultdict

    weeks = defaultdict(list)
    for d in range(num_days):
        iso_week = dates[d].isocalendar()[1]
        iso_year = dates[d].isocalendar()[0]
        weeks[(iso_year, iso_week)].append(d)

    for i in range(num_doctors):
        for (iy, iw), day_indices in weeks.items():
            if len(day_indices) < 3:
                continue
            week_total = model.NewIntVar(0, len(day_indices), f"week_{iy}_{iw}_{i}")
            model.Add(week_total == sum(x[(d, i)] for d in day_indices))
            excess = model.NewIntVar(0, len(day_indices), f"weekex_{iy}_{iw}_{i}")
            model.AddMaxEquality(excess, [week_total - 2, model.NewConstant(0)])
            penalties.append(excess * WEIGHT_WEEKLY_OVERLOAD)

    # Minimize total penalty
    if penalties:
        model.Minimize(sum(penalties))


# ---------------------------------------------------------------------------
# _extract_solution
# ---------------------------------------------------------------------------
def _extract_solution(solver, model, variables, inputs, db_session):
    """Read solver output, persist ShiftAssignment rows, return result."""
    x, p = variables
    shifts = inputs["shifts"]
    doctors = inputs["doctors"]
    schedule = inputs["schedule"]
    num_days = len(shifts)
    num_doctors = len(doctors)

    # Clear existing assignments for this schedule
    for shift in shifts:
        db_session.query(ShiftAssignment).filter_by(shift_id=shift.id).delete()

    # Create new assignments
    total_primers = 0
    for d in range(num_days):
        for i in range(num_doctors):
            if solver.Value(x[(d, i)]) == 1:
                is_primer = bool(solver.Value(p[(d, i)]))
                assignment = ShiftAssignment(
                    shift_id=shifts[d].id,
                    doctor_id=doctors[i].id,
                    is_manual_override=False,
                    is_primer=is_primer,
                )
                db_session.add(assignment)
                if is_primer:
                    total_primers += 1

    db_session.commit()

    return {
        "schedule_id": schedule.id,
        "total_assignments": sum(
            1
            for d in range(num_days)
            for i in range(num_doctors)
            if solver.Value(x[(d, i)]) == 1
        ),
        "total_primers": total_primers,
    }


# ---------------------------------------------------------------------------
# _apply_special_requests
# ---------------------------------------------------------------------------
def _apply_special_requests(model, x, p, special_requests, inputs,
                            num_days, num_doctors, doctors, doctor_idx,
                            shift_map, date_to_idx, leave_set, adjustments):
    """
    Translate active SpecialRequest records into CP-SAT hard constraints.

    Request types:
        must_work            — doctor MUST be on duty on the specified date
        must_not_work        — doctor MUST NOT be on duty on the specified date
        must_work_with       — when doctor works (and optionally is not primer),
                               all companions must also work that day
        weekend_off_after_duty — doctor must NOT work on the weekend following
                                 the specified duty date
    """
    from datetime import timedelta

    dates = inputs["dates"]

    for sr in special_requests:
        i = doctor_idx.get(sr.doctor_id)
        if i is None:
            # Doctor not in the active pool (e.g., target=0)
            adjustments.append({
                "date": sr.date.isoformat() if sr.date else "all",
                "type": "SPECIAL_REQUEST_SKIPPED",
                "from": None,
                "to": None,
                "reason": (
                    f"Special request #{sr.id} ({sr.request_type}) for doctor_id={sr.doctor_id} "
                    f"skipped: doctor not found in active pool."
                ),
            })
            continue

        doc = doctors[i]

        if sr.request_type == "must_work":
            d = date_to_idx.get(sr.date)
            if d is None:
                adjustments.append({
                    "date": sr.date.isoformat() if sr.date else "N/A",
                    "type": "SPECIAL_REQUEST_SKIPPED",
                    "from": None, "to": None,
                    "reason": (
                        f"Special request #{sr.id}: must_work for {doc.full_name} on "
                        f"{sr.date.isoformat()} skipped — date not in schedule."
                    ),
                })
                continue

            model.Add(x[(d, i)] == 1)
            adjustments.append({
                "date": sr.date.isoformat(),
                "type": "SPECIAL_REQ_MUST_WORK",
                "from": None, "to": None,
                "reason": (
                    f"Hard constraint: {doc.full_name} MUST be on duty on "
                    f"{sr.date.isoformat()} (Special Request #{sr.id})."
                ),
            })

        elif sr.request_type == "must_not_work":
            d = date_to_idx.get(sr.date)
            if d is None:
                continue

            model.Add(x[(d, i)] == 0)
            adjustments.append({
                "date": sr.date.isoformat(),
                "type": "SPECIAL_REQ_MUST_NOT_WORK",
                "from": None, "to": None,
                "reason": (
                    f"Hard constraint: {doc.full_name} MUST NOT be on duty on "
                    f"{sr.date.isoformat()} (Special Request #{sr.id})."
                ),
            })

        elif sr.request_type == "must_work_with":
            companions = sr.get_required_people()
            companion_indices = []
            for comp_id in companions:
                ci = doctor_idx.get(comp_id)
                if ci is not None:
                    companion_indices.append(ci)

            if not companion_indices:
                adjustments.append({
                    "date": sr.date.isoformat() if sr.date else "all",
                    "type": "SPECIAL_REQUEST_SKIPPED",
                    "from": None, "to": None,
                    "reason": (
                        f"Special request #{sr.id}: must_work_with for {doc.full_name} "
                        f"skipped — no valid companions in active pool."
                    ),
                })
                continue

            comp_names = ", ".join(
                doctors[ci].full_name for ci in companion_indices
            )

            # Determine which days to apply the constraint
            if getattr(sr, "attending_name", None):
                att_target = sr.attending_name.strip().lower()
                target_days = [
                    d for d in range(num_days)
                    if shift_map[d].attending_name and shift_map[d].attending_name.strip().lower() == att_target
                ]
            elif sr.date:
                target_days = [date_to_idx.get(sr.date)]
                target_days = [d for d in target_days if d is not None]
            else:
                # Apply to all days in the month
                target_days = list(range(num_days))

            for d in target_days:
                if getattr(sr, "attending_name", None):
                    blocked_names = []
                    if (doc.id, shift_map[d].date) in leave_set:
                        blocked_names.append(f"{doc.full_name} is on leave")
                    if doc.seniority_level == "Senior" and shift_map[d].day_type == "holiday":
                        blocked_names.append(f"{doc.full_name} is Senior on a holiday")

                    for cj in companion_indices:
                        comp_doc = doctors[cj]
                        if (comp_doc.id, shift_map[d].date) in leave_set:
                            blocked_names.append(f"{comp_doc.full_name} is on leave")
                        if comp_doc.seniority_level == "Senior" and shift_map[d].day_type == "holiday":
                            blocked_names.append(f"{comp_doc.full_name} is Senior on a holiday")

                    if blocked_names:
                        adjustments.append({
                            "date": shift_map[d].date.isoformat(),
                            "type": "SPECIAL_REQ_ATTENDING_SKIP",
                            "from": None, "to": None,
                            "reason": (
                                f"SR#{sr.id}: Skipping attending {sr.attending_name} day on "
                                f"{shift_map[d].date.isoformat()} (attending {sr.attending_name}) "
                                f"— {'; '.join(blocked_names)}."
                            ),
                        })
                        continue

                    # Force primary doctor to work
                    model.Add(x[(d, i)] == 1)
                    if sr.only_when_not_primer:
                        model.Add(p[(d, i)] == 0)
                    elif sr.only_when_primer:
                        model.Add(p[(d, i)] == 1)
                    
                    # Force companions to work.
                    for cj in companion_indices:
                        model.Add(x[(d, cj)] == 1)
                else:
                    if sr.only_when_not_primer:
                        # doctor works AND is NOT primer → companions must work
                        # x[d,i] == 1 AND p[d,i] == 0 → x[d,cj] == 1 for each companion
                        works_not_primer = model.NewBoolVar(
                            f"sr{sr.id}_wnp_d{d}"
                        )
                        model.Add(x[(d, i)] == 1).OnlyEnforceIf(works_not_primer)
                        model.Add(p[(d, i)] == 0).OnlyEnforceIf(works_not_primer)

                        # We need: works_not_primer == 1 iff x[d,i]=1 AND p[d,i]=0
                        not_primer = model.NewBoolVar(f"sr{sr.id}_np_d{d}")
                        model.Add(p[(d, i)] == 0).OnlyEnforceIf(not_primer)
                        model.Add(p[(d, i)] == 1).OnlyEnforceIf(not_primer.Not())
                        model.AddMultiplicationEquality(works_not_primer, [x[(d, i)], not_primer])

                        for cj in companion_indices:
                            model.Add(x[(d, cj)] >= 1).OnlyEnforceIf(works_not_primer)

                    elif sr.only_when_primer:
                        # doctor IS the primer → companions must work
                        # p[d,i] == 1 → x[d,cj] == 1 for each companion
                        for cj in companion_indices:
                            model.Add(x[(d, cj)] >= 1).OnlyEnforceIf(p[(d, i)])

                    else:
                        # doctor works → companions must work (regardless of primer)
                        for cj in companion_indices:
                            model.Add(x[(d, cj)] >= x[(d, i)])

            if getattr(sr, "attending_name", None):
                date_desc = f"Attending {sr.attending_name}"
            else:
                date_desc = sr.date.isoformat() if sr.date else "all days"
            
            if sr.only_when_not_primer:
                primer_note = " (as non-primer)"
            elif sr.only_when_primer:
                primer_note = " (as primer)"
            else:
                primer_note = ""
            
            if getattr(sr, "attending_name", None):
                reason = (
                    f"Hard constraint: When Attending is {sr.attending_name}, "
                    f"{doc.full_name} MUST work{primer_note} alongside {comp_names} "
                    f"(Special Request #{sr.id})."
                )
            else:
                reason = (
                    f"Hard constraint: When {doc.full_name} works{primer_note}, "
                    f"{comp_names} must also work (Special Request #{sr.id})."
                )

            adjustments.append({
                "date": date_desc,
                "type": "SPECIAL_REQ_MUST_WORK_WITH",
                "from": None, "to": None,
                "reason": reason,
            })

        elif sr.request_type == "weekend_off_after_duty":
            d = date_to_idx.get(sr.date)
            if d is None:
                continue

            # Calculate weekend days to block
            blocked_dates = _get_weekend_after_date(sr.date)
            blocked_day_indices = [
                date_to_idx[bd] for bd in blocked_dates
                if bd in date_to_idx
            ]

            for bd_idx in blocked_day_indices:
                model.Add(x[(bd_idx, i)] == 0)

            blocked_strs = ", ".join(
                bd.isoformat() for bd in sorted(blocked_dates) if bd in date_to_idx
            )
            adjustments.append({
                "date": sr.date.isoformat(),
                "type": "SPECIAL_REQ_WEEKEND_OFF",
                "from": None, "to": None,
                "reason": (
                    f"Hard constraint: {doc.full_name} must NOT work on weekend "
                    f"after duty on {sr.date.isoformat()} → blocked: {blocked_strs} "
                    f"(Special Request #{sr.id})."
                ),
            })


def _get_weekend_after_date(dt):
    """
    Return the Saturday and Sunday of the weekend following the given date.
    If dt is Saturday, only Sunday of the same weekend.
    If dt is Sunday, the next weekend (Sat+Sun).
    """
    weekday = dt.weekday()  # Mon=0 .. Sun=6
    blocked = set()

    if weekday == 5:  # Saturday — block Sunday
        blocked.add(dt + timedelta(days=1))
    elif weekday == 6:  # Sunday — block next Sat + Sun
        next_sat = dt + timedelta(days=6)
        blocked.add(next_sat)
        blocked.add(next_sat + timedelta(days=1))
    else:
        # Weekday — find next Saturday
        days_to_sat = 5 - weekday
        next_sat = dt + timedelta(days=days_to_sat)
        blocked.add(next_sat)
        blocked.add(next_sat + timedelta(days=1))

    return blocked