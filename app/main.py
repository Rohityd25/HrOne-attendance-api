"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import pymongo
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at application startup."""
    try:
        # Drop conflicting non-unique indexes if any existed from previous runs
        emp_indexes = db.employees.index_information()
        if "emp_code_1" in emp_indexes and not emp_indexes["emp_code_1"].get("unique"):
            db.employees.drop_index("emp_code_1")

        att_indexes = db.attendance_logs.index_information()
        if "emp_code_1_date_1" in att_indexes and not att_indexes["emp_code_1_date_1"].get("unique"):
            db.attendance_logs.drop_index("emp_code_1_date_1")

        # 1. employees indexes
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1), ("emp_code", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1)])

        # 2. attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])
        db.attendance_logs.create_index([("date", 1), ("status", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)])
    except Exception as e:
        print(f"Warning during index initialization: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Precision Rounding & Datetime Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def quantize_2(val: Any) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def quantize_4(val: Any) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncates datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON UTC datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> datetime:
    """Converts epoch milliseconds to a UTC datetime truncated to seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """Returns True if shift_end <= shift_start (R1)."""
    return shift_end <= shift_start


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start): a punch-in whose IST
    time is earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    cal_date = dt_ist.date()
    if is_overnight(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        if (dt_ist.hour, dt_ist.minute, dt_ist.second) < (end_h, end_m, 0):
            return (cal_date - timedelta(days=1)).isoformat()
    return cal_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_ist = datetime.fromisoformat(attendance_date).replace(
        hour=start_h, minute=start_m, second=0, microsecond=0, tzinfo=IST
    )
    punch_in_ist = punch_in_dt.astimezone(IST)
    elapsed_seconds = int((punch_in_ist - shift_start_ist).total_seconds())
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    sec = Decimal(int((punch_out_dt - punch_in_dt).total_seconds()))
    hours = (sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out_dt: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    end_h, end_m = map(int, shift_end.split(":"))
    if is_overnight(shift_start, shift_end):
        end_date = datetime.fromisoformat(attendance_date).date() + timedelta(days=1)
    else:
        end_date = datetime.fromisoformat(attendance_date).date()

    shift_end_ist = datetime.combine(end_date, datetime.min.time()).replace(
        hour=end_h, minute=end_m, second=0, microsecond=0, tzinfo=IST
    )
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = int((punch_out_ist - shift_end_ist).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Pydantic Schemas Matching openapi.yaml Exactly
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("invalid date format YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = Field(default=PresenceStatus.PRESENT)


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class HistoryChange(BaseModel):
    model_config = ConfigDict(extra="ignore")
    from_: Any = Field(None, alias="from")
    to: Any = None

    def model_dump(self, *args, **kwargs):
        return {"from": self.from_, "to": self.to}


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


# --------------------------------------------------------------------------- #
# Document Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(h: dict) -> dict:
    at_val = to_epoch_ms(h["at"]) if isinstance(h["at"], datetime) else h["at"]
    changes_out = {}
    for k, v in h.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = to_epoch_ms(to_val)
        changes_out[k] = {"from": from_val, "to": to_val}
    return {
        "at": at_val,
        "by": h["by"],
        "reason": h["reason"],
        "changes": changes_out,
    }


def serialize_attendance(doc: dict) -> dict:
    hist = [serialize_history_entry(e) for e in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": quantize_2(doc.get("work_hours")),
        "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)),
        "half_day": bool(doc.get("half_day", False)),
        "history": hist,
    }


# --------------------------------------------------------------------------- #
# Endpoints Implementation
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, response_model=Employee, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        ts = from_epoch_ms(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts, attendance_date, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        punched_at_dt = from_epoch_ms(body.punched_at)
    else:
        punched_at_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # First check if the employee has any records
    latest_record = db.attendance_logs.find_one({"emp_code": body.emp_code}, sort=[("punch_in", -1)])
    if not latest_record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if latest_record.get("punch_out") is None and punched_at_dt <= latest_record["punch_in"]:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at_dt}},
        sort=[("punch_in", -1)]
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_dt = record["punch_in"]
    if punched_at_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    if (punched_at_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    attendance_date = record["date"]
    work_hours = compute_work_hours(punch_in_dt, punched_at_dt)
    overtime_minutes = compute_overtime(punched_at_dt, attendance_date, emp["shift_start"], emp["shift_end"])
    half_day = compute_half_day(work_hours)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record was punched out by a concurrent request")

    return serialize_attendance(updated_doc)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="No attendance record for that date")

    new_status = body.status.value if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE records cannot have punch times")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        final_punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else record.get("punch_out")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        cal_att_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if cal_att_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to the record's attendance date")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, date, emp["shift_start"], emp["shift_end"])
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, date, emp["shift_start"], emp["shift_end"])

    changes = {}
    if record["status"] != new_status:
        changes["status"] = {"from": record["status"], "to": new_status}
    if record.get("punch_in") != final_punch_in:
        changes["punch_in"] = {"from": record.get("punch_in"), "to": final_punch_in}
    if record.get("punch_out") != final_punch_out:
        changes["punch_out"] = {"from": record.get("punch_out"), "to": final_punch_out}
    if quantize_2(record.get("work_hours")) != quantize_2(final_work_hours):
        changes["work_hours"] = {"from": quantize_2(record.get("work_hours")), "to": quantize_2(final_work_hours)}
    if record.get("late_minutes", 0) != final_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": final_late_minutes}
    if record.get("overtime_minutes", 0) != final_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": final_overtime_minutes}
    if bool(record.get("half_day", False)) != final_half_day:
        changes["half_day"] = {"from": bool(record.get("half_day", False)), "to": final_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Correction changes nothing")

    correction_time = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": correction_time,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance(updated_doc)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    year, mon = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{days_in_month:02d}"

    working_days = 0
    emp_joined = emp["joined_on"]
    if emp_joined <= end_date:
        effective_start = max(emp_joined, start_date)
        cur = datetime.strptime(effective_start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end_date, "%Y-%m-%d").date()
        while cur <= end_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$addFields": {
                "dow": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$dow", 2]},
                                    {"$lte": ["$dow", 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }
        }
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg.get("present_days", 0.0))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = quantize_2((Decimal(str(present_days)) / Decimal(working_days)) * 100)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{days_in_month:02d}"

    emp_match = {"joined_on": {"$lte": end_date}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "log_dow": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        0
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$log_dow", 2]},
                                    {"$lte": ["$log_dow", 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_codes"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count"
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for r in raw_items:
        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": quantize_2(r["present_days"]),
            "avg_work_hours": quantize_2(r["avg_work_hours"]),
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, mon)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{days_in_month:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Unknown department")

    try:
        f_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        t_d = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format YYYY-MM-DD")

    if t_d < f_d:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")

    if (t_d - f_d).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    from_dt = datetime.combine(f_d, datetime.min.time(), tzinfo=timezone.utc)
    to_dt = datetime.combine(t_d, datetime.min.time(), tzinfo=timezone.utc)
    end_bound_dt = to_dt + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, end_bound_dt]
                }
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}}
                ],
                "as": "dept_emps"
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$let": {
                        "vars": {"dow": {"$dayOfWeek": "$date_dt"}},
                        "in": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]}
                    }
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]

    raw_items = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in raw_items:
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": quantize_2(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": quantize_4(r["attendance_rate"]),
            "moving_avg_7d": quantize_4(r["moving_avg_7d"]),
        })

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainEndpointEnum = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == ExplainEndpointEnum.attendance_list:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status.value

        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        exp = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint.value, "collection": "attendance_logs", "explain": exp}

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        year, mon = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, mon)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{days_in_month:02d}"

        pipe = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_date, "$lte": end_date}}},
            {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
            {"$group": {"_id": None, "present_days": {"$sum": 1}}}
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint.value, "collection": "attendance_logs", "explain": exp}

    elif endpoint == ExplainEndpointEnum.department_summary:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year, mon = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, mon)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{days_in_month:02d}"

        emp_match = {"joined_on": {"$lte": end_date}}
        if department is not None:
            emp_match["department"] = department

        pipe = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"ecode": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$ecode"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            }
        ]
        exp = db.command("explain", {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint.value, "collection": "employees", "explain": exp}

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year, mon = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, mon)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{days_in_month:02d}"

        pipe = [
            {"$match": {"date": {"$gte": start_date, "$lte": end_date}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"}
        ]
        if department is not None:
            pipe.append({"$match": {"emp.department": department}})
        pipe.append({"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}})

        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint.value, "collection": "attendance_logs", "explain": exp}

    elif endpoint == ExplainEndpointEnum.department_trend:
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' dates are required for department_trend")

        pipe = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}}
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint.value, "collection": "attendance_logs", "explain": exp}

    else:
        raise HTTPException(status_code=422, detail="Invalid endpoint")
