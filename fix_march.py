"""
Fix March 2026 Setup — Correct doctor targets and attending config.
Run from backend directory: python fix_march.py
"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date
from app import create_app
from models import db, Doctor, User, LeaveRequest, Shift, MonthlySchedule

YEAR = 2026
MONTH = 3  # March

def fix_march():
    app = create_app()
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            print("[ERROR] Admin user not found!")
            return

        doctors = {d.full_name: d for d in Doctor.query.filter_by(admin_id=admin.id).all()}
        print(f"[INFO] Found {len(doctors)} doctors")

        # ============================================================
        # 1. FIX TARGET SHIFTS - ÖŞ must be 0!
        # ============================================================
        shift_targets = {
            "EKG": 6,   # Senior, rank 1 — full primer
            "İBB": 7,   # Senior, rank 2 — full primer
            "BBÖ": 7,   # Senior, rank 3 — full primer
            "BSK": 7,   # Senior, rank 4 — full primer
            "ÖŞ": 0,    # Mid — NOT AVAILABLE (yok)
            "EZ": 7,    # Mid — 3 primer
            "ABR": 7,   # Mid — 3 primer
            "ZÖ": 8,    # Mid
            "AK": 8,    # Mid
            "ÇB": 8,    # Junior
            "EBT": 8,   # Junior
            "SYİ": 8,   # Junior
            "BN": 8,    # Junior
            "AH": 8,    # Junior
            "CA": 8,    # Junior
            "AKB": 8,   # Junior
            "AP": 8,    # Junior
            "AMH": 8,   # Junior
        }

        total = 0
        for name, target in shift_targets.items():
            if name in doctors:
                doctors[name].target_shifts_per_month = target
                total += target
                print(f"  [OK] {name}: target = {target}")
            else:
                print(f"  [WARN] {name} not found in database!")
        db.session.commit()

        print(f"\n[INFO] Total target shifts: {total}")
        print(f"[INFO] March has 31 days")
        print(f"[INFO] Base capacity: {total // 31} per day + {total % 31} extra days")
        print()

        # ============================================================
        # 2. ENSURE ÖŞ LEAVES FOR ENTIRE MARCH (target=0)
        # ============================================================
        print("[INFO] Setting up leaves for ÖŞ...")
        doc_os = doctors.get("ÖŞ")
        if doc_os:
            created = 0
            for day_num in range(1, 32):
                d = date(YEAR, MONTH, day_num)
                existing = LeaveRequest.query.filter_by(doctor_id=doc_os.id, date=d).first()
                if existing:
                    if existing.status != "Approved":
                        existing.status = "Approved"
                        created += 1
                else:
                    leave = LeaveRequest(
                        doctor_id=doc_os.id,
                        date=d,
                        reason="Not available this month",
                        status="Approved",
                        submitted_by_admin=True,
                    )
                    db.session.add(leave)
                    created += 1
            db.session.commit()
            print(f"  [OK] ÖŞ: {created} leave days created/updated (31 total)")
        print()

        # ============================================================
        # 3. SET ATTENDING NAMES AND DEGREES FROM THE SCHEDULE IMAGE
        # Using the image data: N.Uzm and S.Uzm columns
        # Professor days need EKG (rank 1) to work
        # ============================================================
        print("[INFO] Setting up attending configuration...")
        
        sched = MonthlySchedule.query.filter_by(year=YEAR, month=MONTH, admin_id=admin.id).first()
        if not sched:
            print("[ERROR] No March schedule found! Create one from Monthly Setup first.")
            return

        # Attending data from the image
        # Format: day: (N.Uzm, S.Uzm, attending_name, attending_degree)
        # N.Uzm column is the nöbetçi uzman (on-call specialist)
        # S.Uzm column is the sorumlu uzman
        # For the scheduler, we use attending_name and attending_degree
        attending_data = {
            1:  ("TEŞ", "TEŞ", None, None),       # Weekend - red
            2:  ("AB", "AB", None, None),
            3:  ("ŞEA", "MAZ", None, None),
            4:  ("FS", "FS", None, "Professor"),    # Professor day
            5:  ("HBT", "TEŞ", None, None),
            6:  ("RA", "BK", None, None),
            7:  ("MAZ", "MAZ", None, None),         # Weekend
            8:  ("ŞEA", "ŞEA", None, None),        # Weekend
            9:  ("MY", "MY", None, None),
            10: ("MAN", "MAN", None, None),
            11: ("BK", "BK", None, "Professor"),    # Professor day
            12: ("RA", "MY", None, None),
            13: ("FS", "FS", None, "Professor"),    # Professor day
            14: ("MY", "MY", None, None),           # Weekend
            15: ("AB", "AB", None, None),           # Weekend
            16: ("ŞEA", "FS", None, "Specialist"),
            17: ("BK", "BK", None, None),
            18: ("HBT", "MY", None, None),
            19: ("TEŞ", "TEŞ", None, None),
            20: ("MAN", "MAN", None, None),
            21: ("RA", "RA", None, None),           # Weekend
            22: ("MAZ", "MAZ", None, None),         # Weekend
            23: ("BK", "BK", None, None),
            24: ("HBT", "MY", None, None),
            25: ("MY", "MY", None, None),
            26: ("TEŞ", "TEŞ", None, None),
            27: ("ŞEA", "AB", None, None),
            28: ("AB", "AB", None, None),           # Weekend
            29: ("HBT", "HBT", None, None),         # Weekend
            30: ("MAN", "MY", None, None),
            31: ("MAZ", "MAZ", None, None),
        }

        shifts = {s.date.day: s for s in Shift.query.filter_by(schedule_id=sched.id).all()}
        
        for day_num, (n_uzm, s_uzm, att_name, att_degree) in attending_data.items():
            if day_num in shifts:
                shift = shifts[day_num]
                # Use N.Uzm as attending name, and the degree from the image
                shift.attending_name = n_uzm
                shift.attending_degree = att_degree
                print(f"  Day {day_num}: {n_uzm} ({att_degree or 'None'})")
        
        db.session.commit()
        print()

        # ============================================================
        # 4. SUMMARY
        # ============================================================
        print("=" * 50)
        print(f"  March {YEAR} fix complete!")
        print(f"  Total target shifts: {total}")
        print(f"  ÖŞ target: 0 (not available)")
        print(f"  Capacity: {total // 31}/day + {total % 31} days with {total // 31 + 1}")
        print()
        print("  Professor days: 4, 11, 13")
        print("  EKG (rank 1) must work those days")
        print()
        print("  Next steps:")
        print("  1. Restart backend server")
        print("  2. Go to Monthly Setup, click 'Save Setup'")
        print("  3. Set Primer: EZ=3, ABR=3")
        print("  4. Generate Schedule")
        print("=" * 50)


if __name__ == "__main__":
    fix_march()
