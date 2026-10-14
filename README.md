# Employee Attendance & Analytics API

A FastAPI backend for employee attendance tracking, manual regularization, and reporting built over MongoDB.

---

## Prerequisites

- **Python**: 3.11 or newer
- **MongoDB**: 6.0 or newer (tested with MongoDB 7.0 and 8.2)

---

## Environment Variables

The application dynamically reads configuration from environment variables (overriding any local `.env` file):

| Variable | Default Value | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGO_DB` | `attendance_db` | MongoDB database name |

---

## Installation & Setup

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate    # On Windows: .venv\Scripts\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Seed initial sample data (optional):**
   ```bash
   python sample_seed.py
   ```

---

## Running the API

Start the service using `uvicorn`:

```bash
uvicorn app.main:app --port 8000
```

The service will automatically connect to MongoDB, verify connectivity, and create all necessary indexes idempotently on startup.

- **API Documentation**: Available at `http://localhost:8000/docs`
- **Readiness Probe**: `GET http://localhost:8000/health` (returns `200` only when MongoDB responds)

---

## Running Tests

Run the comprehensive test suites covering business rules R1–R10, status codes, concurrency, edge cases, and query plans:

```bash
# Main E2E test suite (CRUD, business rules, explain endpoints):
python test_full_suite.py

# Awkward edge cases suite (race conditions, leaderboard ties, zero-log headcount, mid-month joiners):
python test_awkward_cases.py
```

---

## Architecture & Implementation Notes

- **Single-File Architecture**: All application code lives inside `app/main.py`.
- **Database Indexes**: Indexes on `employees` and `attendance_logs` are registered automatically on application startup via the FastAPI `lifespan` handler.
- **Concurrency & Races**:
  - Duplicate punch-in attempts are prevented by a database-level unique compound index on `(emp_code, date)`. Colliding requests receive HTTP 409 Conflict.
  - Punch-outs use atomic conditional updates (`find_one_and_update` filtering on `punch_out: None`), guaranteeing exactly one success (200) and 409 for concurrent duplicates.
  - Regularizations use atomic `$push` to maintain an immutable audit history trail.
- **Business Rules (R1–R10)**:
  - Dates and shifts use Indian Standard Time (IST, UTC+05:30).
  - Overnight shifts (`shift_end <= shift_start`) punch-ins before `shift_end` belong to the previous day's shift date.
  - Late calculation applies a strict 10-minute grace period from `shift_start`.
  - Overtime requires a minimum threshold of 30 minutes past `shift_end`.
  - Work hours and report figures use exact Decimal arithmetic with `ROUND_HALF_UP`.
- **High-Performance Analytics**:
  - `department_summary`: Originates on `employees` to count headcount including zero-log employees, computing record-level averages via cross-collection `$lookup`.
  - `late_leaderboard`: Implements standard competition ranking (1, 2, 2, 4) using MongoDB's `$setWindowFields` / `$rank`, applying `limit` after ranking.
  - `department_trend`: Uses MongoDB `$densify` for gap filling across all calendar days and `$setWindowFields` for rolling 7-day moving averages.
- **Query Optimization (`/admin/explain/{endpoint}`)**:
  - All query and pipeline paths are backed by compound indexes (`IXSCAN`), avoiding collection scans (`COLLSCAN`).
