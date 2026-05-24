import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from models import db, Doctor, User, ShiftAssignment, MonthlySchedule

def update_doctors():
    app = create_app()
    with app.app_context():
        # Get admin
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            print("Admin not found.")
            return

        # Delete old shifts to avoid foreign key cascading issues or orphaned assignments
        # But wait, SQLAlchemy normally cascades. Let's just delete doctors, which will cascade to shift assignments if configured.
        # Let's delete doctors matching admin_id
        Doctor.query.filter_by(admin_id=admin.id).delete(synchronize_session=False)
        
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
        print(f"Added {len(doctors_data)} new doctors successfully.")

if __name__ == "__main__":
    update_doctors()
