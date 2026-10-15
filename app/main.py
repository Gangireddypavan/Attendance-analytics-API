"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override real environment variables

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                  # datetimes come back as timezone-aware UTC
    serverSelectionTimeoutMS=3000,  # fail fast if the DB is down
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE = ("ABSENT", "LEAVE")
# [0-9], not \d: Pydantic's regex engine treats \d as any Unicode digit (e.g. Arabic-Indic).
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"

# An epoch-millis integer: strict=True rejects floats, strings and booleans (422).
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Idempotent: create_index does nothing if the index already exists."""
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    # Department summary without a department filter: employees who joined by month end.
    db.employees.create_index("joined_on")
    # One record per employee per day; also makes punch-in race-safe.
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    # GET /attendance default sort, and date-range analytics.
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # Punch-out: latest punch_in for an employee.
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    # GET /attendance?status=...: filter, sort and count without scanning every record.
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )


@asynccontextmanager
async def lifespan(app):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """datetime -> epoch milliseconds (int). Naive datetimes are treated as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST calendar date of punch_in; overnight shift + punch before shift_end -> previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        d -= timedelta(days=1)
    return d


def shift_start_at(d: date, emp: dict) -> datetime:
    return datetime.combine(d, parse_hhmm(emp["shift_start"]), IST)


def shift_end_at(d: date, emp: dict) -> datetime:
    end = datetime.combine(d, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)  # overnight shift ends the next calendar day
    return end


def round_half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)  # 0.01 for places=2
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R2-R5
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, emp: dict, d: date) -> int:
    """R2: late only if strictly more than 10:00 after shift start; floor minutes since shift start."""
    seconds = (punch_in - shift_start_at(d, emp)).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals (exact Decimal maths)."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, emp: dict, d: date) -> int:
    """R3: floor minutes after shift end, only counted when >= 30."""
    minutes = int((punch_out - shift_end_at(d, emp)).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived_fields(status: str, punch_in, punch_out, emp: dict, d: date) -> dict:
    """All four derived fields for a record (see DATA_MODEL.md table)."""
    if status in NON_PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "late_minutes": compute_late_minutes(punch_in, emp, d),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        wh = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(punch_out, emp, d)
        out["half_day"] = wh < 4.5  # R5 uses the rounded value
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @model_validator(mode="after")
    def check(self):
        date.fromisoformat(self.joined_on)  # rejects impossible dates like 2026-02-30
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Unknown keys (e.g. a client sending work_hours) are ignored by default.
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def no_explicit_null(cls, v):
        # Runs only for keys the client actually sent: omitting a field means "unchanged",
        # but sending null is not a valid status or timestamp.
        if v is None:
            raise ValueError("may be omitted, but not null")
        return v


# --------------------------------------------------------------------------- #
# Response shaping (never leak _id; instants as epoch ms; defaults for legacy docs)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        else:
            changes[field] = {"from": change.get("from"), "to": change.get("to")}
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def invalid(msg: str, *loc: str):
    """Raise a 422 with FastAPI's standard validation body: {"detail": [{"loc", "msg", "type"}]}."""
    raise RequestValidationError([{"type": "value_error", "loc": loc or ("body",), "msg": msg}])


def parse_date_or_422(value: str, name: str = "date", where: str = "query") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        invalid(f"{name} must be a valid YYYY-MM-DD date", where, name)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(timezone.utc)
    doc["created_at"] = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    try:
        db.employees.insert_one(doc)  # the unique index decides; no find() first
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp, d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index -> race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before this punch-out (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24):
        invalid("punch-out must be after punch-in and within 24 hours", "body", "punched_at")

    d = date.fromisoformat(rec["date"])
    fields = derived_fields(rec["status"], rec["punch_in"], ts, emp, d)
    fields["punch_out"] = ts
    # Atomic: only succeeds if still open. Two parallel punch-outs -> one wins, other gets 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(list(ATTENDANCE_SORT.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


ATTENDANCE_SORT = {"date": DESCENDING, "emp_code": ASCENDING}  # served by the (date -1, emp_code 1) index


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and its explain endpoint."""
    if date_from:
        parse_date_or_422(date_from, "date_from")
    if date_to:
        parse_date_or_422(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        invalid("date_from must be <= date_to", "query", "date_from")
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
        q["status"] = status
    return q


@app.patch("/attendance/{emp_code}/{date}")
def regularize(
    body: RegularizeIn,
    emp_code: str,
    date: str = Path(pattern=DATE_RE),
):
    d = parse_date_or_422(date, "date", "path")
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status or rec["status"]
    pi, po = rec.get("punch_in"), rec.get("punch_out")
    if new_status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            invalid(f"{new_status} cannot have punch times", "body", "status")
        pi = po = None
    else:
        if body.punch_in is not None:
            pi = from_ms(body.punch_in)
        if body.punch_out is not None:
            po = from_ms(body.punch_out)
        if pi is None:
            invalid("a presence status requires punch_in", "body", "punch_in")
        if attendance_date(pi, emp) != d:
            invalid("punch_in must stay on the record's attendance date", "body", "punch_in")
        if po is not None and (po <= pi or po - pi > timedelta(hours=24)):
            invalid("punch_out must be after punch_in and within 24 hours", "body", "punch_out")

    new = {"status": new_status, "punch_in": pi, "punch_out": po}
    new.update(derived_fields(new_status, pi, po, emp, d))
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        invalid("request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic lock: only apply if the record is still exactly as we read it - nobody added
    # a history entry (another PATCH) and nobody changed the punches (e.g. a punch-out).
    seen = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, seen]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics: shared pieces
# --------------------------------------------------------------------------- #
def month_range(month: str) -> tuple[date, date]:
    """'2026-07' -> (2026-07-01, 2026-07-31)."""
    first = date.fromisoformat(month + "-01")
    next_month = (first + timedelta(days=32)).replace(day=1)
    return first, next_month - timedelta(days=1)


def count_weekdays(start: date, end: date) -> int:
    """R7: Mon-Fri days from start to end inclusive (calendar maths on at most 31 days, not data)."""
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def half_up(expr, places: int) -> dict:
    """Round half-up inside MongoDB. ($round is banker's rounding: 0.125 -> 0.12.)
    Working in Decimal128 keeps it exact: floor(x * 10^places + 0.5) / 10^places. null stays null."""
    factor = 10**places
    scaled = {"$add": [{"$multiply": [{"$toDecimal": expr}, factor]}, 0.5]}
    return {"$toDouble": {"$divide": [{"$floor": scaled}, factor]}}


def count_if(condition) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


# Per-record expressions. Missing derived fields mean 0 / false (legacy records).
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}  # R6
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}  # R7
DAY_VALUE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}  # R5: half day = 0.5
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}

# One employee-month of logs -> totals. Used by the employee monthly summary and,
# inside a $lookup, by the department summary (so both count things the same way).
MONTH_STATS_GROUP = {
    "$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, DAY_VALUE, 0]}},
        "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
        "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
        "late_count": count_if({"$gt": [LATE_MINUTES, 0]}),  # any day, weekends included
        "total_late_minutes": {"$sum": LATE_MINUTES},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # For avg_work_hours: sum and count over records, Decimal so the sum is exact.
        "work_hours_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
        "work_hours_n": count_if(HAS_WORK_HOURS),
    }
}


def month_match(month: str) -> dict:
    first, last = month_range(month)
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (also used by /admin/explain, so both run the same query)
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": month_match(month)}},
        MONTH_STATS_GROUP,
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    _, last = month_range(month)
    # Start from employees, not logs: people with zero logs must still be counted (R9).
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": month_match(month)}}, MONTH_STATS_GROUP],
                "as": "stats",  # [] for an employee with no logs this month
            }
        },
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one document per employee at this point
                "present_days": {"$sum": "$stats.present_days"},
                "work_hours_sum": {"$sum": "$stats.work_hours_sum"},
                "work_hours_n": {"$sum": "$stats.work_hours_n"},
                "late_count": {"$sum": "$stats.late_count"},
                "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
                "leave_count": {"$sum": "$stats.leave_count"},
                "on_duty_count": {"$sum": "$stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                # Mean over records (total hours / number of records), not a mean of per-employee means.
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_n", 0]},
                        half_up({"$divide": ["$work_hours_sum", "$work_hours_n"]}, 2),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": month_match(month), "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # name/department come from employees; $unwind drops logs whose employee does not exist.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department
    pipeline += [
        # Standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        # limit applies AFTER ranking, so everyone tied at the cutoff stays in.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_range(from_: str, to: str) -> tuple[date, date]:
    start = parse_date_or_422(from_, "from")
    end = parse_date_or_422(to, "to")
    if end < start:
        invalid("to must be on or after from", "query", "to")
    if (end - start).days + 1 > 92:
        invalid("range must not exceed 92 days", "query", "to")
    return start, end


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    lo, hi = start.isoformat(), end.isoformat()
    first_day = datetime.combine(start, time(0), timezone.utc)
    last_day = datetime.combine(end, time(0), timezone.utc)
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": lo, "$lte": hi}}},
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "present": {"$cond": [IS_PRESENT, DAY_VALUE, 0]},  # any day of the week
                            "late": {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        # Turn each employee into events: one "joined" event (dated `from` if they joined
        # earlier) plus one event per log. Summing per day then gives everything we need.
        {
            "$project": {
                "_id": 0,
                "events": {
                    "$concatArrays": [
                        [{"date": {"$max": ["$joined_on", lo]}, "joins": 1, "present": 0, "late": 0}],
                        "$logs",
                    ]
                },
            }
        },
        {"$unwind": "$events"},
        {
            "$group": {
                "_id": "$events.date",
                "joins": {"$sum": "$events.joins"},
                "present_count": {"$sum": "$events.present"},
                "late_count": {"$sum": "$events.late"},
            }
        },
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # Gap filling: add a document for every calendar day in [from, to] that has no data.
        {
            "$densify": {
                "field": "day",
                "range": {"step": 1, "unit": "day", "bounds": [first_day, last_day + timedelta(days=1)]},
            }
        },
        {"$match": {"day": {"$lte": last_day}}},  # drops people who join after `to`
        # R9 headcount: running total of "joined" events up to and including this day.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        # Mean of the non-null rates in this row and up to 6 rows before it. Only rows inside
        # [from, to] exist, so the first rows get a shorter window; $avg skips nulls.
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": {"$toDecimal": "$attendance_rate"},
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": half_up("$moving_avg_7d", 4),
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    emp = get_employee_or_404(emp_code)
    stats = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)), {})

    first, last = month_range(month)
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)  # 0 if they join after this month
    present = stats.get("present_days", 0)
    pct = None
    if working_days:
        pct = round_half_up(Decimal(str(present)) * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": stats.get("leave_count", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=DATE_RE),
    to: str = Query(pattern=DATE_RE),
):
    start, end = trend_range(from_, to)
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(department_trend_pipeline(department, start, end)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: prove the queries use indexes
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs explain (executionStats) on the endpoint's main query, built by the same function
    the endpoint uses, so the plan shown is the plan the endpoint really gets."""

    def require(**params):
        for name, value in params.items():
            if not value:
                invalid(f"{name.rstrip('_')} is required for {endpoint}", "query", name.rstrip("_"))

    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:  # department_trend
            require(department=department, from_=from_, to=to)
            start, end = trend_range(from_, to)
            collection, pipeline = "employees", department_trend_pipeline(department, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command("explain", command, verbosity="executionStats")
    # BSON (dates, Int64, Timestamp...) -> plain JSON
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
