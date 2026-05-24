"""Test schedule generation for April 2026 with EZ having full month leave."""
from models import db, Doctor, LeaveRequest, MonthlySchedule, Shift
from app import create_app
from datetime import date
import traceback

# Import directly from scheduler for debugging
from ortools.sat.python import cp_model

app = create_app()
with app.app_context():
    # Find April 2026 schedule
    sched = MonthlySchedule.query.filter_by(year=2026, month=4).first()
    if not sched:
        print("No April 2026 schedule found!")
        exit(1)
    
    print(f"Testing schedule generation for April 2026 (ID={sched.id})...")
    print()
    
    # First, let's check the data
    doctors = Doctor.query.filter_by(admin_id=sched.admin_id).all()
    shifts = Shift.query.filter_by(schedule_id=sched.id).all()
    num_days = len(shifts)
    
    print(f"Number of days: {num_days}")
    print(f"Number of doctors: {len(doctors)}")
    print()
    
    # Check leaves and effective capacity
    all_dates = [s.date for s in shifts]
    leaves = LeaveRequest.query.filter(
        LeaveRequest.doctor_id.in_([d.id for d in doctors]),
        LeaveRequest.date.in_(all_dates),
        LeaveRequest.status == "Approved"
    ).all()
    
    leave_set = set((lv.doctor_id, lv.date) for lv in leaves)
    
    print("Doctor availability:")
    total_effective = 0
    for doc in doctors:
        available = sum(1 for d in all_dates if (doc.id, d) not in leave_set)
        max_shifts = (available + 1) // 2  # with post-call rest
        effective = min(doc.target_shifts_per_month or 0, max_shifts)
        total_effective += effective
        print(f"  {doc.full_name} ({doc.seniority_level}): target={doc.target_shifts_per_month}, available={available}, effective={effective}")
    
    print()
    print(f"Total effective target shifts: {total_effective}")
    print(f"Days to fill: {num_days}")
    
    print()
    print("="*50)
    
    # Run the scheduler with error tracing
    try:
        from scheduler import generate_schedule
        result = generate_schedule(sched.id, db.session)
        
        print("="*50)
        print(f"STATUS: {result['status']}")
        print("="*50)
        
        if result['status'] in ('OPTIMAL', 'FEASIBLE'):
            print("SUCCESS! Schedule generated.")
            if 'adjustments' in result:
                print()
                print("Adjustments made:")
                for adj in result['adjustments']:
                    print(f"  - {adj['type']}: {adj.get('reason', '')}")
        else:
            print(f"FAILED: {result.get('message', 'Unknown error')}")
            if 'conflicts' in result and result['conflicts']:
                print()
                print("Conflicts:")
                for conflict in result['conflicts']:
                    print(f"  - {conflict['type']}: {conflict['message']}")
    except Exception as e:
        print(f"EXCEPTION: {e}")
        traceback.print_exc()
