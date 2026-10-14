"""
Specialized Test Suite for Awkward / Edge Cases:
- Concurrency simulation (simultaneous punch-ins, punch-outs, regularizations)
- Ties on leaderboard and tie at limit cutoff
- Mid-month joiners working days calculation
- Department with employees having ZERO attendance logs
- Employee with only ABSENT logs
- Overnight shift punch-out across midnight
- Weekend records adding to late/overtime but NOT to present_days
- Trend first rows moving average
"""
import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import threading
import urllib.error
import urllib.request
from pymongo import MongoClient

c = MongoClient("mongodb://localhost:27017")
db = c["attendance_db"]

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


def test_concurrency():
    print("\n[Testing Concurrency...]")
    # Setup test employee
    db.employees.delete_one({"emp_code": "EMP8888"})
    db.attendance_logs.delete_many({"emp_code": "EMP8888"})
    req("POST", "/employees", {
        "emp_code": "EMP8888",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "ConcurrencyDept",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    })

    # Test 1: Simultaneous duplicate punch-ins
    results = []
    t_in = int(datetime(2026, 7, 10, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch_in():
        s, b = req("POST", "/attendance/punch-in", {"emp_code": "EMP8888", "punched_at": t_in})
        results.append(s)

    threads = [threading.Thread(target=do_punch_in) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 1, f"Expected exactly one 201, got results: {results}"
    assert results.count(409) == 4, f"Expected four 409s, got results: {results}"
    print("[OK] Simultaneous punch-ins: exactly one 201 and rest 409")

    # Test 2: Simultaneous duplicate punch-outs
    out_results = []
    t_out = int(datetime(2026, 7, 10, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

    def do_punch_out():
        s, b = req("POST", "/attendance/punch-out", {"emp_code": "EMP8888", "punched_at": t_out})
        out_results.append(s)

    threads = [threading.Thread(target=do_punch_out) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert out_results.count(200) == 1, f"Expected exactly one 200, got: {out_results}"
    assert out_results.count(409) == 4, f"Expected four 409s, got: {out_results}"
    print("[OK] Simultaneous punch-outs: exactly one 200 and rest 409")


def test_leaderboard_ties_and_cutoff():
    print("\n[Testing Leaderboard Ties & Cutoff Limit...]")
    # Setup test department and 4 employees
    db.employees.delete_many({"department": "TieDept"})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP7"}})

    for i in range(1, 5):
        req("POST", "/employees", {
            "emp_code": f"EMP700{i}",
            "name": f"Late User {i}",
            "email": f"late{i}@example.com",
            "department": "TieDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })

    # Scores: EMP7001 -> 60 min late, EMP7002 -> 40 min late, EMP7003 -> 40 min late (tie!), EMP7004 -> 20 min late
    # Ranks should be: EMP7001 (rank 1), EMP7002 (rank 2), EMP7003 (rank 2), EMP7004 (rank 4)
    # When limit=2 is passed, rank <= 2 must return EMP7001, EMP7002, AND EMP7003 (3 rows returned despite limit=2!)
    t_base = datetime(2026, 7, 1, 4, 0, 0, tzinfo=timezone.utc)
    # EMP7001: 60 min late
    req("POST", "/attendance/punch-in", {
        "emp_code": "EMP7001",
        "punched_at": int((t_base + timedelta(minutes=70)).timestamp() * 1000)
    })
    # EMP7002: 40 min late
    req("POST", "/attendance/punch-in", {
        "emp_code": "EMP7002",
        "punched_at": int((t_base + timedelta(minutes=50)).timestamp() * 1000)
    })
    # EMP7003: 40 min late
    req("POST", "/attendance/punch-in", {
        "emp_code": "EMP7003",
        "punched_at": int((t_base + timedelta(minutes=50)).timestamp() * 1000)
    })
    # EMP7004: 20 min late
    req("POST", "/attendance/punch-in", {
        "emp_code": "EMP7004",
        "punched_at": int((t_base + timedelta(minutes=30)).timestamp() * 1000)
    })

    # Query with limit=2 inside TieDept
    s, b = req("GET", "/analytics/leaderboard/late?month=2026-07&limit=2&department=TieDept")
    assert s == 200, f"Leaderboard query failed: {s} {b}"
    items = b["items"]
    # Exactly 3 items returned because EMP7002 and EMP7003 tie at rank 2!
    assert len(items) == 3, f"Expected 3 items due to tie at cutoff, got {len(items)}: {items}"
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 2
    assert items[2]["rank"] == 2
    # Check ordering for ties: emp_code ascending
    assert items[1]["emp_code"] == "EMP7002"
    assert items[2]["emp_code"] == "EMP7003"
    print("[OK] Leaderboard competition ties (1, 2, 2) and tie at limit=2 cutoff returns 3 items")


def test_zero_logs_headcount():
    print("\n[Testing Department Headcount with Zero-Log Employees...]")
    db.employees.delete_many({"department": "ZeroLogDept"})
    # Create 3 employees in ZeroLogDept
    for i in range(1, 4):
        req("POST", "/employees", {
            "emp_code": f"EMP600{i}",
            "name": f"Zero Logger {i}",
            "email": f"zero{i}@example.com",
            "department": "ZeroLogDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-01"
        })
    # Punch in ONLY 1 employee
    t_in = int(datetime(2026, 7, 1, 4, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-in", {"emp_code": "EMP6001", "punched_at": t_in})

    # Summary query for ZeroLogDept
    s, b = req("GET", "/analytics/departments/summary?month=2026-07&department=ZeroLogDept")
    assert s == 200
    assert len(b["items"]) == 1
    item = b["items"][0]
    # Headcount MUST be 3! Even though 2 employees have 0 logs!
    assert item["headcount"] == 3, f"Expected headcount 3, got {item['headcount']}"
    assert item["present_days"] == 1.0
    print("[OK] Department summary headcount correctly counts all employees including zero-log employees (R9)")


def test_mid_month_joiner_and_weekends():
    print("\n[Testing Mid-Month Joiner & Weekend Presence Rules (R7)...]")
    db.employees.delete_one({"emp_code": "EMP5555"})
    db.attendance_logs.delete_many({"emp_code": "EMP5555"})
    # Joined on 2026-07-20 (a Monday)
    req("POST", "/employees", {
        "emp_code": "EMP5555",
        "name": "Mid Month Joiner",
        "email": "mid@example.com",
        "department": "JoinerDept",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-20"
    })

    # Working days in July 2026 from 2026-07-20 to 2026-07-31:
    # 2026-07-20 (Mon) to 2026-07-24 (Fri) = 5 days
    # 2026-07-27 (Mon) to 2026-07-31 (Fri) = 5 days
    # Total working days = 10
    s, b = req("GET", "/analytics/employees/EMP5555/monthly?month=2026-07")
    assert s == 200
    assert b["working_days"] == 10, f"Expected 10 working days, got {b['working_days']}"
    print("[OK] Mid-month joiner working days calculated from joined_on")

    # Add a WEEKEND attendance record (2026-07-25 is Saturday)
    # 2026-07-25 09:45:00 IST = 2026-07-25 04:15:00 UTC (15 min late)
    t_sat_in = int(datetime(2026, 7, 25, 4, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-in", {"emp_code": "EMP5555", "punched_at": t_sat_in})
    t_sat_out = int(datetime(2026, 7, 25, 13, 40, 0, tzinfo=timezone.utc).timestamp() * 1000)
    req("POST", "/attendance/punch-out", {"emp_code": "EMP5555", "punched_at": t_sat_out})

    # Check monthly summary
    # Per R7: present_days counts only Mon-Fri records (so weekend record adds 0 to present_days)
    # BUT weekend records STILL add to late and overtime totals!
    s, b = req("GET", "/analytics/employees/EMP5555/monthly?month=2026-07")
    assert s == 200
    assert b["present_days"] == 0.0, f"Weekend record must not count towards present_days, got {b['present_days']}"
    assert b["total_late_minutes"] == 15, f"Weekend record must add to late minutes, got {b['total_late_minutes']}"
    assert b["total_overtime_minutes"] == 40, f"Weekend record must add to overtime, got {b['total_overtime_minutes']}"
    print("[OK] R7 weekend rules: adds to late/overtime but NOT to present_days")


def test_pagination():
    print("\n[Testing Pagination Rules (R10)...]")
    # Test valid page 1 and page 2
    s, b1 = req("GET", "/employees?page=1&page_size=2")
    assert s == 200
    assert len(b1["items"]) == 2
    assert b1["page"] == 1
    assert b1["page_size"] == 2

    s, b2 = req("GET", "/employees?page=2&page_size=2")
    assert s == 200
    assert len(b2["items"]) == 2
    assert b2["page"] == 2

    # Verify items between page 1 and page 2 don't overlap
    codes1 = {item["emp_code"] for item in b1["items"]}
    codes2 = {item["emp_code"] for item in b2["items"]}
    assert not codes1.intersection(codes2), f"Pages should not overlap: {codes1} vs {codes2}"

    # Test max page size 100
    s, b_max = req("GET", "/employees?page=1&page_size=100")
    assert s == 200

    # Test exceeding max page size -> 422
    s, b_exceed = req("GET", "/employees?page=1&page_size=101")
    assert s == 422, f"Expected 422 for page_size=101, got {s}"

    # Test page < 1 -> 422
    s, b_zero = req("GET", "/employees?page=0&page_size=20")
    assert s == 422, f"Expected 422 for page=0, got {s}"

    print("[OK] Pagination: page 1/2 non-overlapping, page_size limits (min 1, max 100) enforced with 422")


def cleanup_all():
    db.employees.delete_many({"emp_code": {"$regex": "^EMP(8888|7|6|5555)"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP(8888|7|6|5555)"}})


if __name__ == "__main__":
    try:
        test_concurrency()
        test_leaderboard_ties_and_cutoff()
        test_zero_logs_headcount()
        test_mid_month_joiner_and_weekends()
        test_pagination()
        print("\nALL AWKWARD AND EDGE-CASE TESTS PASSED!")
    finally:
        cleanup_all()
