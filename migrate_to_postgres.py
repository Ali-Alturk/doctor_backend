"""
migrate_to_postgres.py — Reads your local SQLite database and imports all data
into a PostgreSQL database on Render (or any PostgreSQL).

Usage:
    python migrate_to_postgres.py "postgresql://user:pass@host:port/dbname"

This script:
1. Reads all data from local SQLite (data/scheduler.db)
2. Connects to the target PostgreSQL database
3. Creates all tables using SQLAlchemy models
4. Inserts all data preserving original IDs
5. Resets PostgreSQL sequences so new inserts get correct IDs
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
from datetime import datetime


def main():
    if len(sys.argv) < 2:
        print("Usage: python migrate_to_postgres.py <POSTGRESQL_URL>")
        print('Example: python migrate_to_postgres.py "postgresql://user:pass@host/dbname"')
        sys.exit(1)

    pg_url = sys.argv[1]

    # Fix Render's postgres:// URL
    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    # Connect to SQLite
    sqlite_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "scheduler.db")
    if not os.path.exists(sqlite_path):
        print(f"ERROR: SQLite database not found at {sqlite_path}")
        sys.exit(1)

    print(f"Reading from SQLite: {sqlite_path}")
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    # Set up Flask app with PostgreSQL
    os.environ["DATABASE_URL"] = pg_url
    os.environ.setdefault("JWT_SECRET_KEY", "migration-temp-key")

    from app import create_app
    from models import db, User, Doctor, MonthlySchedule, Shift, ShiftAssignment, LeaveRequest, SpecialRequest
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text

    app = create_app()

    with app.app_context():
        print("\n=== Creating PostgreSQL tables ===")
        db.create_all()
        print("Tables created successfully.")

        # Check if data already exists
        existing_users = User.query.count()
        if existing_users > 0:
            print(f"\nWARNING: PostgreSQL already has {existing_users} user(s).")
            response = input("Do you want to DROP all data and re-import? (yes/no): ").strip().lower()
            if response != "yes":
                print("Aborted.")
                sys.exit(0)
            # Drop all data in correct order (respecting foreign keys)
            print("Dropping existing data...")
            db.session.execute(text("DELETE FROM shift_assignments"))
            db.session.execute(text("DELETE FROM shifts"))
            db.session.execute(text("DELETE FROM monthly_schedules"))
            db.session.execute(text("DELETE FROM leave_requests"))
            db.session.execute(text("DELETE FROM special_requests"))
            db.session.execute(text("DELETE FROM doctors"))
            db.session.execute(text("DELETE FROM users"))
            db.session.commit()
            print("Existing data cleared.")

        # ── USERS ──────────────────────────────────────────
        print("\n--- Importing Users ---")
        rows = sqlite_conn.execute("SELECT * FROM users").fetchall()
        for r in rows:
            user = User(
                id=r["id"],
                username=r["username"],
                password_hash=r["password_hash"],
                role=r["role"],
                hospital_name=r["hospital_name"],
                department_name=r["department_name"],
            )
            db.session.add(user)
        db.session.commit()
        print(f"  Imported {len(rows)} user(s)")

        # ── DOCTORS ────────────────────────────────────────
        print("--- Importing Doctors ---")
        rows = sqlite_conn.execute("SELECT * FROM doctors").fetchall()
        for r in rows:
            doc = Doctor(
                id=r["id"],
                full_name=r["full_name"],
                seniority_level=r["seniority_level"],
                seniority_rank=r["seniority_rank"],
                target_shifts_per_month=r["target_shifts_per_month"],
                admin_id=r["admin_id"],
            )
            db.session.add(doc)
        db.session.commit()
        print(f"  Imported {len(rows)} doctor(s)")

        # ── MONTHLY SCHEDULES ──────────────────────────────
        print("--- Importing Monthly Schedules ---")
        rows = sqlite_conn.execute("SELECT * FROM monthly_schedules").fetchall()
        for r in rows:
            created_at = None
            if r["created_at"]:
                try:
                    created_at = datetime.fromisoformat(r["created_at"])
                except (ValueError, TypeError):
                    try:
                        created_at = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
                    except (ValueError, TypeError):
                        created_at = datetime.utcnow()

            sched = MonthlySchedule(
                id=r["id"],
                year=r["year"],
                month=r["month"],
                status=r["status"],
                is_final=bool(r["is_final"]),
                created_at=created_at,
                admin_id=r["admin_id"],
            )
            db.session.add(sched)
        db.session.commit()
        print(f"  Imported {len(rows)} schedule(s)")

        # ── SHIFTS ─────────────────────────────────────────
        print("--- Importing Shifts ---")
        rows = sqlite_conn.execute("SELECT * FROM shifts").fetchall()
        for r in rows:
            shift = Shift(
                id=r["id"],
                schedule_id=r["schedule_id"],
                date=r["date"],
                day_type=r["day_type"],
                attending_name=r["attending_name"],
                attending_degree=r["attending_degree"],
                capacity=r["capacity"],
            )
            db.session.add(shift)
        db.session.commit()
        print(f"  Imported {len(rows)} shift(s)")

        # ── SHIFT ASSIGNMENTS ──────────────────────────────
        print("--- Importing Shift Assignments ---")
        rows = sqlite_conn.execute("SELECT * FROM shift_assignments").fetchall()
        for r in rows:
            cols = r.keys()
            assign = ShiftAssignment(
                id=r["id"],
                shift_id=r["shift_id"],
                doctor_id=r["doctor_id"],
                is_manual_override=bool(r["is_manual_override"]),
                is_primer=bool(r["is_primer"]) if "is_primer" in cols else False,
            )
            db.session.add(assign)
        db.session.commit()
        print(f"  Imported {len(rows)} assignment(s)")

        # ── LEAVE REQUESTS ─────────────────────────────────
        print("--- Importing Leave Requests ---")
        rows = sqlite_conn.execute("SELECT * FROM leave_requests").fetchall()
        for r in rows:
            leave = LeaveRequest(
                id=r["id"],
                doctor_id=r["doctor_id"],
                date=r["date"],
                reason=r["reason"],
                status=r["status"],
                submitted_by_admin=bool(r["submitted_by_admin"]),
            )
            db.session.add(leave)
        db.session.commit()
        print(f"  Imported {len(rows)} leave request(s)")

        # ── SPECIAL REQUESTS ──────────────────────────────
        print("--- Importing Special Requests ---")
        rows = sqlite_conn.execute("SELECT * FROM special_requests").fetchall()
        for r in rows:
            cols = r.keys()
            created_at = None
            if "created_at" in cols and r["created_at"]:
                try:
                    created_at = datetime.fromisoformat(r["created_at"])
                except (ValueError, TypeError):
                    created_at = datetime.utcnow()

            updated_at = None
            if "updated_at" in cols and r["updated_at"]:
                try:
                    updated_at = datetime.fromisoformat(r["updated_at"])
                except (ValueError, TypeError):
                    updated_at = datetime.utcnow()

            sr = SpecialRequest(
                id=r["id"],
                admin_id=r["admin_id"],
                year=r["year"],
                month=r["month"],
                request_type=r["request_type"],
                doctor_id=r["doctor_id"],
                date=r["date"] if "date" in cols else None,
                required_people=r["required_people"] if "required_people" in cols else None,
                attending_name=r["attending_name"] if "attending_name" in cols else None,
                only_when_not_primer=bool(r["only_when_not_primer"]) if "only_when_not_primer" in cols else True,
                only_when_primer=bool(r["only_when_primer"]) if "only_when_primer" in cols else False,
                note=r["note"] if "note" in cols else None,
                is_active=bool(r["is_active"]) if "is_active" in cols else True,
                created_at=created_at,
                updated_at=updated_at,
            )
            db.session.add(sr)
        db.session.commit()
        print(f"  Imported {len(rows)} special request(s)")

        # ── RESET SEQUENCES ───────────────────────────────
        print("\n=== Resetting PostgreSQL sequences ===")
        tables_with_sequences = [
            "users", "doctors", "monthly_schedules", "shifts",
            "shift_assignments", "leave_requests", "special_requests",
        ]
        for table in tables_with_sequences:
            try:
                db.session.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
                ))
            except Exception as e:
                print(f"  Warning: Could not reset sequence for {table}: {e}")
                db.session.rollback()
        db.session.commit()
        print("Sequences reset.")

        # ── VERIFY ─────────────────────────────────────────
        print("\n=== Verification ===")
        print(f"  Users:            {User.query.count()}")
        print(f"  Doctors:          {Doctor.query.count()}")
        print(f"  Schedules:        {MonthlySchedule.query.count()}")
        print(f"  Shifts:           {Shift.query.count()}")
        print(f"  Assignments:      {ShiftAssignment.query.count()}")
        print(f"  Leave Requests:   {LeaveRequest.query.count()}")
        print(f"  Special Requests: {SpecialRequest.query.count()}")

    sqlite_conn.close()
    print("\n✅ Migration completed successfully!")
    print("Your PostgreSQL database now has all the data from SQLite.")


if __name__ == "__main__":
    main()
