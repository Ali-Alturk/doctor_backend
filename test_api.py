"""
Comprehensive API test script — tests all endpoints end-to-end.
Run inside the container: docker exec backend-app python test_api.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from models import db, User, Doctor, LeaveRequest, MonthlySchedule, Shift, ShiftAssignment, SpecialRequest
from flask_jwt_extended import create_access_token
from datetime import date, timedelta
import json

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

results = {"passed": 0, "failed": 0}

app = create_app()

def check(label, condition, detail=""):
    if condition:
        print(f"{PASS} {label}")
        results["passed"] += 1
    else:
        print(f"{FAIL} {label}  {detail}")
        results["failed"] += 1

with app.app_context():
    client = app.test_client()

    # ── Get JWT token ────────────────────────────────────────────────────
    print(f"\n{INFO} ── AUTH ──────────────────────────────────────────")

    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    check("POST /api/auth/login — valid credentials", r.status_code == 200, r.data)
    token = r.get_json().get("access_token", "") if r.status_code == 200 else ""
    headers = {"Authorization": f"Bearer {token}"}

    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrongpass"})
    check("POST /api/auth/login — wrong password returns 401", r.status_code == 401)

    r = client.get("/api/auth/me", headers=headers)
    check("GET /api/auth/me — returns current user", r.status_code == 200 and r.get_json().get("username") == "admin")

    r = client.post("/api/auth/logout", headers=headers)
    check("POST /api/auth/logout — returns 200", r.status_code == 200)

    # ── DOCTORS ──────────────────────────────────────────────────────────
    print(f"\n{INFO} ── DOCTORS ───────────────────────────────────────")

    r = client.get("/api/doctors", headers=headers)
    check("GET /api/doctors — returns list", r.status_code == 200 and isinstance(r.get_json(), list))
    doctors = r.get_json()
    print(f"       Found {len(doctors)} doctors in DB")

    r = client.post("/api/doctors", headers=headers, json={
        "full_name": "TEST_DOC",
        "seniority_level": "Junior",
        "target_shifts_per_month": 8,
    })
    check("POST /api/doctors — create Junior doctor", r.status_code == 201, r.data)
    new_doc_id = r.get_json().get("id") if r.status_code == 201 else None

    if new_doc_id:
        r = client.put(f"/api/doctors/{new_doc_id}", headers=headers, json={"full_name": "TEST_DOC_UPDATED"})
        check("PUT /api/doctors/<id> — update doctor name", r.status_code == 200)

        r = client.get(f"/api/doctors/{new_doc_id}/profile", headers=headers)
        check("GET /api/doctors/<id>/profile — returns profile", r.status_code == 200 and "stats" in r.get_json())

        r = client.delete(f"/api/doctors/{new_doc_id}", headers=headers)
        check("DELETE /api/doctors/<id> — delete doctor", r.status_code == 200)

    r = client.post("/api/doctors", headers=headers, json={"full_name": "", "seniority_level": "Junior"})
    check("POST /api/doctors — missing full_name returns 400", r.status_code == 400)

    r = client.post("/api/doctors", headers=headers, json={"full_name": "X", "seniority_level": "God"})
    check("POST /api/doctors — invalid seniority returns 400", r.status_code == 400)

    # ── LEAVES ───────────────────────────────────────────────────────────
    print(f"\n{INFO} ── LEAVES ────────────────────────────────────────")

    first_doc_id = doctors[0]["id"] if doctors else None

    r = client.get("/api/leaves", headers=headers)
    check("GET /api/leaves — returns list", r.status_code == 200 and isinstance(r.get_json(), list))

    leave_id = None
    if first_doc_id:
        test_date = (date.today() + timedelta(days=10)).isoformat()
        r = client.post("/api/leaves", headers=headers, json={
            "doctor_id": first_doc_id,
            "date": test_date,
            "reason": "Test leave",
        })
        check("POST /api/leaves — create leave request", r.status_code == 201, r.data)
        leave_id = r.get_json().get("id") if r.status_code == 201 else None

        # Duplicate should return 409
        r2 = client.post("/api/leaves", headers=headers, json={
            "doctor_id": first_doc_id,
            "date": test_date,
        })
        check("POST /api/leaves — duplicate returns 409", r2.status_code == 409)

        # Bulk leaves
        bulk_dates = [
            (date.today() + timedelta(days=20)).isoformat(),
            (date.today() + timedelta(days=21)).isoformat(),
        ]
        r = client.post("/api/leaves/bulk", headers=headers, json={
            "doctor_id": first_doc_id,
            "dates": bulk_dates,
        })
        check("POST /api/leaves/bulk — create bulk leaves", r.status_code == 201, r.data)
        bulk_data = r.get_json() if r.status_code == 201 else {}
        check("POST /api/leaves/bulk — correct created count", bulk_data.get("created_count") == 2)

        if leave_id:
            r = client.put(f"/api/leaves/{leave_id}/status", headers=headers, json={"status": "Approved"})
            check("PUT /api/leaves/<id>/status — approve leave", r.status_code == 200 and r.get_json().get("status") == "Approved")

            r = client.put(f"/api/leaves/{leave_id}/status", headers=headers, json={"status": "BadStatus"})
            check("PUT /api/leaves/<id>/status — invalid status returns 400", r.status_code == 400)

            r = client.delete(f"/api/leaves/{leave_id}", headers=headers)
            check("DELETE /api/leaves/<id> — delete leave", r.status_code == 200)

        # Clean up bulk leaves
        for bd in bulk_dates:
            leaves_list = client.get(f"/api/leaves?doctor_id={first_doc_id}", headers=headers).get_json()
            for lv in leaves_list:
                if lv["date"] == bd:
                    client.delete(f"/api/leaves/{lv['id']}", headers=headers)

    # ── SCHEDULE ─────────────────────────────────────────────────────────
    print(f"\n{INFO} ── SCHEDULE ──────────────────────────────────────")

    test_year, test_month = 2026, 6

    # Reset first to ensure clean state
    client.delete(f"/api/schedule/reset/{test_month}/{test_year}", headers=headers)

    r = client.get(f"/api/schedule/{test_month}/{test_year}", headers=headers)
    check("GET /api/schedule/<month>/<year> — no schedule returns empty", r.status_code == 200 and r.get_json()["schedule"] is None)

    days = [{"date": f"{test_year}-{test_month:02d}-{d:02d}", "day_type": "workday", "capacity": 3}
            for d in range(1, 6)]
    r = client.post("/api/schedule/setup", headers=headers, json={
        "year": test_year, "month": test_month, "days": days
    })
    check("POST /api/schedule/setup — creates schedule", r.status_code == 200, r.data)
    schedule_id = r.get_json().get("schedule_id") if r.status_code == 200 else None

    r = client.get(f"/api/schedule/{test_month}/{test_year}", headers=headers)
    check("GET /api/schedule/<month>/<year> — returns schedule after setup",
          r.status_code == 200 and r.get_json()["schedule"] is not None)

    if schedule_id:
        r = client.post("/api/schedule/generate", headers=headers, json={"schedule_id": schedule_id})
        check("POST /api/schedule/generate — runs scheduler", r.status_code in (200, 409), r.data[:200] if r.data else "")

        r = client.post(f"/api/schedule/{schedule_id}/publish", headers=headers)
        check("POST /api/schedule/<id>/publish — publishes schedule", r.status_code == 200)

    r = client.get(f"/api/fairness/{test_month}/{test_year}", headers=headers)
    check("GET /api/fairness/<month>/<year> — returns fairness data", r.status_code == 200)

    # Reset test month
    r = client.delete(f"/api/schedule/reset/{test_month}/{test_year}", headers=headers)
    check("DELETE /api/schedule/reset/<month>/<year> — resets month", r.status_code == 200)

    # ── SPECIAL REQUESTS ─────────────────────────────────────────────────
    print(f"\n{INFO} ── SPECIAL REQUESTS ──────────────────────────────")

    r = client.get(f"/api/special-requests/{test_month}/{test_year}", headers=headers)
    check("GET /api/special-requests/<month>/<year> — returns list", r.status_code == 200 and isinstance(r.get_json(), list))

    sr_id = None
    if first_doc_id:
        r = client.post("/api/special-requests", headers=headers, json={
            "request_type": "must_not_work",
            "doctor_id": first_doc_id,
            "year": test_year,
            "month": test_month,
            "date": f"{test_year}-{test_month:02d}-15",
        })
        check("POST /api/special-requests — create must_not_work", r.status_code == 201, r.data)
        sr_id = r.get_json().get("id") if r.status_code == 201 else None

        if sr_id:
            r = client.put(f"/api/special-requests/{sr_id}", headers=headers, json={"note": "updated note"})
            check("PUT /api/special-requests/<id> — update request", r.status_code == 200)

            r = client.patch(f"/api/special-requests/{sr_id}/toggle", headers=headers)
            check("PATCH /api/special-requests/<id>/toggle — toggle active", r.status_code == 200)

            r = client.post(f"/api/special-requests/validate/{test_month}/{test_year}", headers=headers)
            check("POST /api/special-requests/validate/<month>/<year> — validate", r.status_code == 200)

            r = client.delete(f"/api/special-requests/{sr_id}", headers=headers)
            check("DELETE /api/special-requests/<id> — delete request", r.status_code == 200)

    # ── SUMMARY ──────────────────────────────────────────────────────────
    total = results["passed"] + results["failed"]
    print(f"\n{'='*55}")
    print(f"  Results: {results['passed']}/{total} passed  |  {results['failed']} failed")
    print(f"{'='*55}\n")
    if results["failed"] > 0:
        sys.exit(1)
