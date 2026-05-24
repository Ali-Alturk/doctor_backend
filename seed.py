"""
Seed script — creates default admin + 8 sample doctors.
Run independently: python seed.py
"""

import sys
import os

# Ensure the backend directory is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import string
import secrets
from app import create_app
from models import db, User, Doctor


def seed():
    app = create_app()
    with app.app_context():
        # Ensure tables exist
        db.create_all()

        # Create admin if not present
        admin = User.query.filter_by(username="admin").first()
        if admin is None:
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
        else:
            print("Admin user already exists.")

        # Check if doctors already seeded
        existing = Doctor.query.filter_by(admin_id=admin.id).count()
        if existing >= 8:
            print(f"Doctors already seeded ({existing} found). Skipping.")
            return

        doctors_data = [
            # Seniors
            {"full_name": "EKG", "seniority_level": "Senior", "seniority_rank": 1, "target_shifts_per_month": 8},
            {"full_name": "İBB", "seniority_level": "Senior", "seniority_rank": 2, "target_shifts_per_month": 8},
            {"full_name": "BBÖ", "seniority_level": "Senior", "seniority_rank": 3, "target_shifts_per_month": 8},
            {"full_name": "BSK", "seniority_level": "Senior", "seniority_rank": 4, "target_shifts_per_month": 8},
            # Mids
            {"full_name": "ÖŞ", "seniority_level": "Mid", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "EZ", "seniority_level": "Mid", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "ABR", "seniority_level": "Mid", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "ZÖ", "seniority_level": "Mid", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "AK", "seniority_level": "Mid", "seniority_rank": None, "target_shifts_per_month": 9},
            # Juniors
            {"full_name": "ÇB", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "EBT", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "SYİ", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "BN", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "AH", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "CA", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "AKB", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "AP", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
            {"full_name": "AMH", "seniority_level": "Junior", "seniority_rank": None, "target_shifts_per_month": 9},
        ]

        for doc_data in doctors_data:
            doc = Doctor(admin_id=admin.id, **doc_data)
            db.session.add(doc)

        db.session.commit()
        print(f"Seeded {len(doctors_data)} doctors successfully.")
        print("  - 4 Seniors (rank 1 to 4), target 8 shifts")
        print("  - 5 Mids, target 9 shifts")
        print("  - 9 Juniors, target 9 shifts")


if __name__ == "__main__":
    seed()
