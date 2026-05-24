"""
SQLAlchemy models for the Shift Scheduler application.
"""

import json
import os
import secrets
import string
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default="admin", nullable=False)
    hospital_name = db.Column(db.String(200), nullable=True)
    department_name = db.Column(db.String(200), nullable=True)

    doctors = db.relationship("Doctor", backref="admin", lazy=True)
    schedules = db.relationship("MonthlySchedule", backref="admin", lazy=True)
    special_requests = db.relationship("SpecialRequest", backref="admin", lazy=True,
                                       foreign_keys="SpecialRequest.admin_id")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "hospital_name": self.hospital_name,
            "department_name": self.department_name,
        }


class Doctor(db.Model):
    __tablename__ = "doctors"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(200), nullable=False)
    seniority_level = db.Column(db.String(10), nullable=False)
    seniority_rank = db.Column(db.Integer, nullable=True)
    target_shifts_per_month = db.Column(db.Integer, nullable=False, default=8)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    leave_requests = db.relationship(
        "LeaveRequest", backref="doctor", lazy=True, cascade="all, delete-orphan"
    )
    shift_assignments = db.relationship(
        "ShiftAssignment", backref="doctor", lazy=True, cascade="all, delete-orphan"
    )
    special_requests = db.relationship(
        "SpecialRequest", backref="doctor", lazy=True, cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.CheckConstraint(
            "seniority_level IN ('Senior', 'Mid', 'Junior')",
            name="ck_doctor_seniority_level",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "full_name": self.full_name,
            "seniority_level": self.seniority_level,
            "seniority_rank": self.seniority_rank,
            "target_shifts_per_month": self.target_shifts_per_month,
            "admin_id": self.admin_id,
        }


class MonthlySchedule(db.Model):
    __tablename__ = "monthly_schedules"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="draft", nullable=False)
    is_final = db.Column(db.Boolean, default=False)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    shifts = db.relationship(
        "Shift", backref="schedule", lazy=True, cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint("year", "month", "admin_id", name="uq_schedule_year_month_admin"),
        db.CheckConstraint(
            "status IN ('draft', 'published')",
            name="ck_schedule_status",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "year": self.year,
            "month": self.month,
            "status": self.status,
            "is_final": self.is_final,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "admin_id": self.admin_id,
        }


class Shift(db.Model):
    __tablename__ = "shifts"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(
        db.Integer, db.ForeignKey("monthly_schedules.id"), nullable=False
    )
    date = db.Column(db.Date, nullable=False)
    day_type = db.Column(db.String(20), nullable=False)
    attending_name = db.Column(db.String(200), nullable=True)
    attending_degree = db.Column(db.String(20), nullable=True)
    capacity = db.Column(db.Integer, default=3, nullable=False)

    assignments = db.relationship(
        "ShiftAssignment", backref="shift", lazy=True, cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint("schedule_id", "date", name="uq_shift_schedule_date"),
        db.CheckConstraint(
            "day_type IN ('workday', 'weekend', 'holiday')",
            name="ck_shift_day_type",
        ),
        db.CheckConstraint(
            "attending_degree IS NULL OR attending_degree IN ('Professor', 'Specialist')",
            name="ck_shift_attending_degree",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "schedule_id": self.schedule_id,
            "date": self.date.isoformat() if self.date else None,
            "day_type": self.day_type,
            "attending_name": self.attending_name,
            "attending_degree": self.attending_degree,
            "capacity": self.capacity,
            "assignments": [a.to_dict() for a in self.assignments],
        }


class ShiftAssignment(db.Model):
    __tablename__ = "shift_assignments"

    id = db.Column(db.Integer, primary_key=True)
    shift_id = db.Column(db.Integer, db.ForeignKey("shifts.id"), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False)
    is_manual_override = db.Column(db.Boolean, default=False)
    is_primer = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.UniqueConstraint("shift_id", "doctor_id", name="uq_assignment_shift_doctor"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "shift_id": self.shift_id,
            "doctor_id": self.doctor_id,
            "doctor_name": self.doctor.full_name if self.doctor else None,
            "seniority_level": self.doctor.seniority_level if self.doctor else None,
            "is_manual_override": self.is_manual_override,
            "is_primer": self.is_primer,
        }


class LeaveRequest(db.Model):
    __tablename__ = "leave_requests"

    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), default="Pending", nullable=False)
    submitted_by_admin = db.Column(db.Boolean, default=True)

    __table_args__ = (
        db.UniqueConstraint("doctor_id", "date", name="uq_leave_doctor_date"),
        db.CheckConstraint(
            "status IN ('Pending', 'Approved', 'Rejected')",
            name="ck_leave_status",
        ),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "doctor_id": self.doctor_id,
            "doctor_name": self.doctor.full_name if self.doctor else None,
            "date": self.date.isoformat() if self.date else None,
            "reason": self.reason,
            "status": self.status,
            "submitted_by_admin": self.submitted_by_admin,
        }

class SpecialRequest(db.Model):
    """Special scheduling requests applied as hard constraints during generation."""
    __tablename__ = "special_requests"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    request_type = db.Column(db.String(30), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False)
    date = db.Column(db.Date, nullable=True)
    required_people = db.Column(db.Text, nullable=True)  # JSON array of doctor_ids
    attending_name = db.Column(db.String(100), nullable=True)
    only_when_not_primer = db.Column(db.Boolean, default=True)
    only_when_primer = db.Column(db.Boolean, default=False)
    note = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.CheckConstraint(
            "request_type IN ('must_work', 'must_not_work', 'must_work_with', 'weekend_off_after_duty')",
            name="ck_special_request_type",
        ),
    )

    def get_required_people(self):
        """Return required_people as a Python list of doctor IDs."""
        if not self.required_people:
            return []
        try:
            return json.loads(self.required_people)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_required_people(self, ids):
        """Store a list of doctor IDs as JSON."""
        self.required_people = json.dumps(ids) if ids else None

    def to_dict(self):
        return {
            "id": self.id,
            "admin_id": self.admin_id,
            "year": self.year,
            "month": self.month,
            "request_type": self.request_type,
            "doctor_id": self.doctor_id,
            "doctor_name": self.doctor.full_name if self.doctor else None,
            "date": self.date.isoformat() if self.date else None,
            "required_people": self.get_required_people(),
            "attending_name": self.attending_name,
            "only_when_not_primer": self.only_when_not_primer,
            "only_when_primer": self.only_when_primer,
            "note": self.note,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }




def _is_postgres(engine):
    """Check if the database engine is PostgreSQL."""
    return engine.dialect.name == "postgresql"


def init_db(app):
    """Initialize database tables and create default admin if none exists."""
    with app.app_context():
        db.create_all()

        is_pg = _is_postgres(db.engine)

        # Auto-migration: only needed for SQLite upgrades from older schemas.
        # PostgreSQL gets the correct schema from db.create_all() above.
        if not is_pg:
            try:
                from sqlalchemy import text, inspect
                inspector = inspect(db.engine)

                # Track completed migrations
                db.session.execute(text(
                    "CREATE TABLE IF NOT EXISTS _migrations "
                    "(name VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                ))
                db.session.commit()

                applied = set(
                    row[0] for row in
                    db.session.execute(text("SELECT name FROM _migrations")).fetchall()
                )

                if "add_is_primer" not in applied:
                    columns = [c["name"] for c in inspector.get_columns("shift_assignments")]
                    if "is_primer" not in columns:
                        db.session.execute(
                            text("ALTER TABLE shift_assignments ADD COLUMN is_primer BOOLEAN DEFAULT 0")
                        )
                        print("Migration: added is_primer column to shift_assignments.")
                    db.session.execute(
                        text("INSERT OR IGNORE INTO _migrations (name) VALUES ('add_is_primer')")
                    )
                    db.session.commit()

                if "remove_capacity_constraint" not in applied:
                    try:
                        db.session.execute(text("""
                            CREATE TABLE IF NOT EXISTS shifts_new (
                                id INTEGER PRIMARY KEY,
                                schedule_id INTEGER NOT NULL REFERENCES monthly_schedules(id),
                                date DATE NOT NULL,
                                day_type VARCHAR(20) NOT NULL CHECK(day_type IN ('workday', 'weekend', 'holiday')),
                                attending_name VARCHAR(200),
                                attending_degree VARCHAR(20) CHECK(attending_degree IS NULL OR attending_degree IN ('Professor', 'Specialist')),
                                capacity INTEGER NOT NULL DEFAULT 3,
                                UNIQUE(schedule_id, date)
                            )
                        """))
                        db.session.execute(text("""
                            INSERT OR IGNORE INTO shifts_new
                            SELECT id, schedule_id, date, day_type, attending_name, attending_degree, capacity
                            FROM shifts
                        """))
                        db.session.execute(text("DROP TABLE shifts"))
                        db.session.execute(text("ALTER TABLE shifts_new RENAME TO shifts"))
                        print("Migration: rebuilt shifts table (removed capacity constraint).")
                    except Exception as me:
                        db.session.rollback()
                        print(f"Shifts table migration note: {me}")
                    db.session.execute(
                        text("INSERT OR IGNORE INTO _migrations (name) VALUES ('remove_capacity_constraint')")
                    )
                    db.session.commit()

                if "add_special_requests" not in applied:
                    existing_tables = inspector.get_table_names()
                    if "special_requests" not in existing_tables:
                        db.session.execute(text("""
                            CREATE TABLE special_requests (
                                id INTEGER PRIMARY KEY,
                                admin_id INTEGER NOT NULL REFERENCES users(id),
                                year INTEGER NOT NULL,
                                month INTEGER NOT NULL,
                                request_type VARCHAR(30) NOT NULL CHECK(request_type IN ('must_work', 'must_not_work', 'must_work_with', 'weekend_off_after_duty')),
                                doctor_id INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
                                date DATE,
                                required_people TEXT,
                                only_when_not_primer BOOLEAN DEFAULT 1,
                                note VARCHAR(500),
                                is_active BOOLEAN DEFAULT 1,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                        """))
                        print("Migration: created special_requests table.")
                    db.session.execute(
                        text("INSERT OR IGNORE INTO _migrations (name) VALUES ('add_special_requests')")
                    )
                    db.session.commit()

                if "rebuild_special_requests_v2" not in applied:
                    existing_tables = inspector.get_table_names()
                    if "special_requests" in existing_tables:
                        cols = [c["name"] for c in inspector.get_columns("special_requests")]
                        if "required_people" not in cols:
                            db.session.execute(text("DROP TABLE special_requests"))
                            db.session.execute(text("""
                                CREATE TABLE special_requests (
                                    id INTEGER PRIMARY KEY,
                                    admin_id INTEGER NOT NULL REFERENCES users(id),
                                    year INTEGER NOT NULL,
                                    month INTEGER NOT NULL,
                                    request_type VARCHAR(30) NOT NULL CHECK(request_type IN ('must_work', 'must_not_work', 'must_work_with', 'weekend_off_after_duty')),
                                    doctor_id INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
                                    date DATE,
                                    required_people TEXT,
                                    only_when_not_primer BOOLEAN DEFAULT 1,
                                    note VARCHAR(500),
                                    is_active BOOLEAN DEFAULT 1,
                                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                )
                            """))
                            print("Migration: rebuilt special_requests table with new schema.")
                    db.session.execute(
                        text("INSERT OR IGNORE INTO _migrations (name) VALUES ('rebuild_special_requests_v2')")
                    )
                    db.session.commit()

                if "add_only_when_primer" not in applied:
                    existing_tables = inspector.get_table_names()
                    if "special_requests" in existing_tables:
                        cols = [c["name"] for c in inspector.get_columns("special_requests")]
                        if "only_when_primer" not in cols:
                            db.session.execute(
                                text("ALTER TABLE special_requests ADD COLUMN only_when_primer BOOLEAN DEFAULT 0")
                            )
                            print("Migration: added only_when_primer to special_requests.")
                    db.session.execute(
                        text("INSERT OR IGNORE INTO _migrations (name) VALUES ('add_only_when_primer')")
                    )
                    db.session.commit()

                if "add_attending_name" not in applied:
                    existing_tables = inspector.get_table_names()
                    if "special_requests" in existing_tables:
                        cols = [c["name"] for c in inspector.get_columns("special_requests")]
                        if "attending_name" not in cols:
                            db.session.execute(
                                text("ALTER TABLE special_requests ADD COLUMN attending_name VARCHAR(100)")
                            )
                            print("Migration: added attending_name to special_requests.")
                    db.session.execute(
                        text("INSERT OR IGNORE INTO _migrations (name) VALUES ('add_attending_name')")
                    )
                    db.session.commit()

            except Exception as e:
                print(f"Migration check skipped: {e}")

        # Seed admin user (works on both SQLite and PostgreSQL)
        try:
            existing_user = User.query.filter_by(username="admin").first()
            if existing_user is None:
                admin = User(
                    username="admin",
                    role="admin",
                    hospital_name="City Hospital",
                    department_name="Pediatric Surgery",
                )
                
                default_password = os.environ.get("DEFAULT_ADMIN_PASSWORD")
                if not default_password:
                    alphabet = string.ascii_letters + string.digits
                    default_password = ''.join(secrets.choice(alphabet) for i in range(12))
                    print(f"WARNING: No DEFAULT_ADMIN_PASSWORD provided. Generated random password: {default_password}")
                else:
                    print("Admin user created using DEFAULT_ADMIN_PASSWORD from environment.")
                    
                admin.set_password(default_password)
                db.session.add(admin)
                db.session.commit()
                
                if not os.environ.get("DEFAULT_ADMIN_PASSWORD"):
                    print("IMPORTANT: Please save this password or change it immediately.")
        except Exception as e:
            db.session.rollback()
            print(f"Admin seed skipped (already exists or error): {e}")

