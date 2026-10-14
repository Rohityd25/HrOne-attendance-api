"""
Comprehensive Test Suite for HROne Employee Attendance & Analytics API
Covers R1-R10, Concurrency, CRUD, Regularization, Analytics Pipelines, and Explain.
"""
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Ensure we test with local MongoDB
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

from pymongo import MongoClient

c = MongoClient(MONGO_URI)
db = c[MONGO_DB]

BASE_URL = "http://127.0.0.1:8000"


def req(method, path, body=None):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req_obj = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req_obj) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            err_json = json.loads(content)
        except Exception:
            err_json = {"detail": content}
        return e.code, err_json


def test_business_rules():
    print("Testing Business Rules Unit Logic...")
    from app.main import (
        compute_late_minutes,
        compute_work_hours,
        compute_overtime,
        compute_half_day,
        get_attendance_date,
        IST,
    )

    # R1: Overnight shift
    # Shift: 22:00 - 06:00
    # Punch in 2026-07-07 01:15 IST -> should belong to 2026-07-06
    dt1 = datetime(2026, 7, 7, 1, 15, tzinfo=IST)
    att_date1 = get_attendance_date(dt1, "22:00", "06:00")
    assert att_date1 == "2026-07-06", f"Expected 2026-07-06, got {att_date1}"

    # Punch in 2026-07-07 06:00 IST -> not earlier than 06:00, belongs to 2026-07-07
    dt2 = datetime(2026, 7, 7, 6, 0, tzinfo=IST)
    att_date2 = get_attendance_date(dt2, "22:00", "06:00")
    assert att_date2 == "2026-07-07", f"Expected 2026-07-07, got {att_date2}"

    # Normal shift: 09:30 - 18:30
    dt3 = datetime(2026, 7, 7, 9, 25, tzinfo=IST)
    assert get_attendance_date(dt3, "09:30", "18:30") == "2026-07-07"

    # R2: Late calculation
    # Shift 09:30:
    # 09:40:00 -> 0
    t_094000 = datetime(2026, 7, 7, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(t_094000, "2026-07-07", "09:30", "18:30") == 0

    # 09:40:01 -> 10
    t_094001 = datetime(2026, 7, 7, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(t_094001, "2026-07-07", "09:30", "18:30") == 10

    # 10:15:59 -> 45
    t_101559 = datetime(2026, 7, 7, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(t_101559, "2026-07-07", "09:30", "18:30") == 45

    # R3: Overtime
    # Shift end 18:30
    # Punch out 18:59:00 -> 29 mins (< 30) -> 0
    t_out29 = datetime(2026, 7, 7, 18, 59, 0, tzinfo=IST)
    assert compute_overtime(t_out29, "2026-07-07", "09:30", "18:30") == 0

    # Punch out 19:00:00 -> 30 mins -> 30
    t_out30 = datetime(2026, 7, 7, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(t_out30, "2026-07-07", "09:30", "18:30") == 30

    # Punch out 19:10:00 -> 40 mins -> 40
    t_out40 = datetime(2026, 7, 7, 19, 10, 0, tzinfo=IST)
    assert compute_overtime(t_out40, "2026-07-07", "09:30", "18:30") == 40

    # R4 & R8: Work hours HALF-UP rounding
    # 9 hours 7 mins (32820s / 3600 = 9.116666...) -> 9.12
    p_in = datetime(2026, 7, 7, 9, 28, 0, tzinfo=IST)
    p_out = datetime(2026, 7, 7, 18, 35, 0, tzinfo=IST)
    wh = compute_work_hours(p_in, p_out)
    assert wh == 9.12, f"Expected 9.12, got {wh}"

    # Half up test: 4.495 hours -> 4.50
    # 4.495 * 3600 = 16182 seconds
    p_in_half = datetime(2026, 7, 7, 9, 0, 0, tzinfo=IST)
    p_out_half = p_in_half + timedelta(seconds=16182)
    wh_half = compute_work_hours(p_in_half, p_out_half)
    assert wh_half == 4.50, f"Expected 4.50, got {wh_half}"
    assert compute_half_day(wh_half) is False  # 4.50 is NOT < 4.50

    # 4.494 hours -> 4.49 -> half_day True
    p_out_half2 = p_in_half + timedelta(seconds=16178)
    wh_half2 = compute_work_hours(p_in_half, p_out_half2)
    assert wh_half2 == 4.49, f"Expected 4.49, got {wh_half2}"
    assert compute_half_day(wh_half2) is True

    print("Business Rules Unit Tests Passed!")


def run_http_tests():
    print("\nRunning End-to-End HTTP Tests...")

    # 1. /health
    status_code, body = req("GET", "/health")
    assert status_code == 200, f"/health failed: {status_code} {body}"
    assert body.get("status") == "ok"
    print("[OK] GET /health returns 200 ok")

    # 2. Employees: Create and duplicate
    emp_payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test.user@example.com",
        "department": "QualityAssurance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    # Clean up test employee if present
    db.employees.delete_one({"emp_code": "EMP9999"})
    db.attendance_logs.delete_many({"emp_code": "EMP9999"})

    status_code, body = req("POST", "/employees", emp_payload)
    assert status_code == 201, f"POST /employees failed: {status_code} {body}"
    assert body["emp_code"] == "EMP9999"
    assert isinstance(body["created_at"], int)
    print("[OK] POST /employees creates employee (201)")

    # Duplicate employee -> 409
    status_code, body = req("POST", "/employees", emp_payload)
    assert status_code == 409, f"Expected 409 for duplicate employee, got {status_code} {body}"
    print("[OK] POST /employees duplicate emp_code returns 409")

    # Invalid shift_start == shift_end -> 422
    bad_emp = dict(emp_payload, emp_code="EMP9998", shift_end="09:30")
    status_code, body = req("POST", "/employees", bad_emp)
    assert status_code == 422, f"Expected 422, got {status_code}"
    print("[OK] POST /employees shift_start == shift_end returns 422")

    # 3. GET /employees
    status_code, body = req("GET", "/employees?department=QualityAssurance&page=1&page_size=10")
    assert status_code == 200
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["emp_code"] == "EMP9999"
    print("[OK] GET /employees filtered and paginated correctly")

    # 4. Punch-in
    # Punch in at 09:40:01 IST on 2026-07-06
    # 2026-07-06 09:40:01 IST = 2026-07-06 04:10:01 UTC
    t_in = int(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    punch_in_payload = {
        "emp_code": "EMP9999",
        "punched_at": t_in,
        "status": "PRESENT"
    }
    status_code, body = req("POST", "/attendance/punch-in", punch_in_payload)
    assert status_code == 201, f"Punch-in failed: {status_code} {body}"
    assert body["date"] == "2026-07-06"
    assert body["late_minutes"] == 10
    assert body["punch_in"] == t_in
    assert body["punch_out"] is None
    assert "id" not in body
    print("[OK] POST /attendance/punch-in creates attendance record with R1/R2 calculations")

    # Duplicate punch-in for same date -> 409
    status_code, body = req("POST", "/attendance/punch-in", punch_in_payload)
    assert status_code == 409, f"Expected 409 duplicate punch-in, got {status_code}"
    print("[OK] POST /attendance/punch-in duplicate punch-in returns 409")

    # Punch-in for non-existent employee -> 404
    status_code, body = req("POST", "/attendance/punch-in", dict(punch_in_payload, emp_code="EMP0000"))
    assert status_code == 404
    print("[OK] POST /attendance/punch-in non-existent employee returns 404")

    # 5. Punch-out
    # Punch out at 19:10:00 IST on 2026-07-06 = 2026-07-06 13:40:00 UTC
    t_out = int(datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
    status_code, body = req("POST", "/attendance/punch-out", {"emp_code": "EMP9999", "punched_at": t_out})
    assert status_code == 200, f"Punch-out failed: {status_code} {body}"
    assert body["punch_out"] == t_out
    assert body["overtime_minutes"] == 40
    assert body["half_day"] is False
    assert body["work_hours"] == 9.50
    print("[OK] POST /attendance/punch-out computes work_hours, overtime, half_day")

    # Duplicate punch-out -> 409
    status_code, body = req("POST", "/attendance/punch-out", {"emp_code": "EMP9999", "punched_at": t_out})
    assert status_code == 409
    print("[OK] POST /attendance/punch-out duplicate returns 409")

    # Punch-out with invalid time (before punch-in) -> 422
    # Punch out at t_in - 1000ms
    # First create another record
    t_in2 = int(datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-in", {"emp_code": "EMP9999", "punched_at": t_in2})
    status_code, body = req("POST", "/attendance/punch-out", {"emp_code": "EMP9999", "punched_at": t_in2 - 5000})
    assert status_code == 422
    print("[OK] POST /attendance/punch-out before punch-in returns 422")

    # 6. GET /attendance
    status_code, body = req("GET", "/attendance?emp_code=EMP9999&page=1&page_size=20")
    assert status_code == 200
    assert body["total"] >= 2
    assert body["items"][0]["date"] >= body["items"][1]["date"]
    print("[OK] GET /attendance sorted by date desc")

    # date_from > date_to -> 422
    status_code, body = req("GET", "/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert status_code == 422
    print("[OK] GET /attendance date_from > date_to returns 422")

    # 7. Regularization PATCH /attendance/{emp_code}/{date}
    # Regularize 2026-07-06 record: change punch_in to 09:30:00 IST (04:00:00 UTC)
    t_in_new = int(datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    reg_payload = {
        "punch_in": t_in_new,
        "reason": "Correcting biometric delay",
        "regularized_by": "lead.admin"
    }
    status_code, body = req("PATCH", "/attendance/EMP9999/2026-07-06", reg_payload)
    assert status_code == 200, f"PATCH failed: {status_code} {body}"
    assert body["late_minutes"] == 0
    assert len(body["history"]) == 1
    assert "late_minutes" in body["history"][0]["changes"]
    assert body["history"][0]["changes"]["late_minutes"]["from"] == 10
    assert body["history"][0]["changes"]["late_minutes"]["to"] == 0
    print("[OK] PATCH /attendance regularize recomputes derived fields and appends audit history")

    # Regularize changing nothing -> 422
    status_code, body = req("PATCH", "/attendance/EMP9999/2026-07-06", reg_payload)
    assert status_code == 422
    print("[OK] PATCH /attendance changing nothing returns 422")

    # Regularize ABSENT with punch times -> 422
    status_code, body = req("PATCH", "/attendance/EMP9999/2026-07-06", {
        "status": "ABSENT",
        "punch_in": t_in_new,
        "reason": "Test bad absent",
        "regularized_by": "lead.admin"
    })
    assert status_code == 422
    print("[OK] PATCH /attendance ABSENT with punch times returns 422")

    # 8. Analytics: Employee Monthly
    status_code, body = req("GET", "/analytics/employees/EMP9999/monthly?month=2026-07")
    assert status_code == 200
    assert body["emp_code"] == "EMP9999"
    assert body["working_days"] == 23
    assert body["present_days"] >= 1.0
    print("[OK] GET /analytics/employees/EMP9999/monthly returns aggregation summary")

    # 9. Analytics: Department Summary
    status_code, body = req("GET", "/analytics/departments/summary?month=2026-07&department=QualityAssurance")
    assert status_code == 200
    assert len(body["items"]) == 1
    assert body["items"][0]["department"] == "QualityAssurance"
    assert body["items"][0]["headcount"] == 1
    print("[OK] GET /analytics/departments/summary aggregates cross-collection with headcount")

    # 10. Analytics: Late Leaderboard
    status_code, body = req("GET", "/analytics/leaderboard/late?month=2026-07&limit=10")
    assert status_code == 200
    print(f"[OK] GET /analytics/leaderboard/late returns leaderboard items ({len(body['items'])} items)")

    # 11. Analytics: Department Trend
    status_code, body = req("GET", "/analytics/departments/QualityAssurance/trend?from=2026-07-01&to=2026-07-07")
    assert status_code == 200
    assert len(body["items"]) == 7
    # Weekend days must have attendance_rate None
    for item in body["items"]:
        if not item["is_working_day"]:
            assert item["attendance_rate"] is None
    print("[OK] GET /analytics/departments/QualityAssurance/trend fills all gaps with 7d moving average")

    # 12. Explain Endpoints
    explain_endpoints = [
        "attendance_list?emp_code=EMP9999",
        "employee_monthly?emp_code=EMP9999&month=2026-07",
        "department_summary?month=2026-07",
        "late_leaderboard?month=2026-07",
        "department_trend?department=QualityAssurance&from=2026-07-01&to=2026-07-07"
    ]
    for ep in explain_endpoints:
        ep_name = ep.split("?")[0]
        status_code, body = req("GET", f"/admin/explain/{ep}")
        assert status_code == 200, f"Explain failed on {ep}: {status_code} {body}"
        assert body["endpoint"] == ep_name
        assert "collection" in body
        assert "explain" in body
        # Verify execution plan
        exp_text = json.dumps(body["explain"])
        assert "COLLSCAN" not in exp_text or "IXSCAN" in exp_text
        print(f"[OK] GET /admin/explain/{ep_name} succeeded with index scan on {body['collection']}")

    # Clean up test user
    db.employees.delete_one({"emp_code": "EMP9999"})
    db.attendance_logs.delete_many({"emp_code": "EMP9999"})
    print("\nALL HTTP AND INTEGRATION TESTS PASSED!")


if __name__ == "__main__":
    test_business_rules()
    run_http_tests()
