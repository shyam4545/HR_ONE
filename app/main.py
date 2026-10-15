"""
Employee Attendance & Analytics API
FastAPI service managing employee records, attendance punches, and monthly aggregations.
"""
import math
import os
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Startup - indexes
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_asc")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], name="emp_code_date_asc")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="date_late_minutes")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def round_half_up(value: float, decimals: int = 2) -> float:
    """R8: half-up rounding (not banker rounding)."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime, truncated to seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def ensure_utc(dt: datetime) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: IST calendar date of punch-in.
    Overnight shift (shift_end <= shift_start): if IST punch-in time < shift_end -> previous day.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_ist.date()
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if (eh * 60 + em) <= (sh * 60 + sm):
        if (punch_ist.hour * 60 + punch_ist.minute) < (eh * 60 + em):
            ist_date = ist_date - timedelta(days=1)
    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: Grace 10 min. Late only if strictly > 10 min after shift_start.
    late_minutes = floor(total minutes from shift_start).
    Examples (shift 09:30): 09:40:00->0, 09:40:01->10, 10:15:59->45
    """
    h, m = map(int, shift_start.split(":"))
    d = date_type.fromisoformat(att_date)
    start_ist = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)
    diff_min = (punch_in_utc - start_ist).total_seconds() / 60
    return math.floor(diff_min) if diff_min > 10 else 0


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, att_date: str, shift_start: str) -> int:
    """R3: floor(min after shift_end), only if >= 30, else 0. Overnight: shift_end on next day."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    d = date_type.fromisoformat(att_date)
    end_date = d + timedelta(days=1) if (eh * 60 + em) <= (sh * 60 + sm) else d
    end_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    diff_min = (punch_out_utc - end_ist).total_seconds() / 60
    return math.floor(diff_min) if diff_min >= 30 else 0


def compute_work_hours(pi: datetime, po: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, 2 decimals half-up."""
    return round_half_up((po - pi).total_seconds() / 3600, 2)


def compute_all_derived(pi, po, shift_start, shift_end, att_date):
    late = compute_late_minutes(pi, shift_start, att_date)
    if po is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(pi, po)
    return {
        "late_minutes": late,
        "work_hours": wh,
        "overtime_minutes": compute_overtime_minutes(po, shift_end, att_date, shift_start),
        "half_day": wh < 4.50,  # R5
    }


def serialize_record(doc: dict) -> dict:
    doc.pop("_id", None)
    if doc.get("punch_in") is not None:
        doc["punch_in"] = dt_to_ms(ensure_utc(doc["punch_in"]))
    if doc.get("punch_out") is not None:
        doc["punch_out"] = dt_to_ms(ensure_utc(doc["punch_out"]))
    for entry in doc.get("history", []):
        if "at" in entry:
            entry["at"] = dt_to_ms(ensure_utc(entry["at"]))
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                if change.get("from") is not None:
                    change["from"] = dt_to_ms(ensure_utc(change["from"]))
                if change.get("to") is not None:
                    change["to"] = dt_to_ms(ensure_utc(change["to"]))
    return doc


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if doc.get("created_at") is not None:
        doc["created_at"] = dt_to_ms(ensure_utc(doc["created_at"]))
    return doc


def parse_month(month: str):
    year, m = int(month[:4]), int(month[5:7])
    first = date_type(year, m, 1)
    last = date_type(year + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def count_working_days(period_start: str, period_end: str, joined_on: str) -> int:
    """R7: Mon-Fri from max(period_start, joined_on) to period_end inclusive."""
    start = date_type.fromisoformat(max(period_start, joined_on))
    end = date_type.fromisoformat(period_end)
    count, current = 0, start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def shift_times_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_ms(cls, v):
        if v is not None and (v < 100_000_000_000 or v > 4_102_444_800_000):
            raise ValueError("punched_at must be epoch milliseconds")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_ms(cls, v):
        if v is not None and (v < 100_000_000_000 or v > 4_102_444_800_000):
            raise ValueError("punched_at must be epoch milliseconds")
        return v


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    regularized_by: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=5, max_length=200)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_ms(cls, v):
        if v is not None and (v < 100_000_000_000 or v > 4_102_444_800_000):
            raise ValueError("Timestamps must be epoch milliseconds")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(ALL_STATUSES)}")
        return v


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/health", tags=["system"])
def health():
    """Readiness probe checking database connectivity."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create a new employee record."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees with pagination and optional department filter."""
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for item in items:
        serialize_employee(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record employee punch-in following rules R1 and R2."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    pi_utc = ms_to_dt(body.punched_at) if body.punched_at else datetime.now(tz=timezone.utc).replace(microsecond=0)
    att_date = get_attendance_date(pi_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(pi_utc, emp["shift_start"], att_date)
    doc = {
        "emp_code": body.emp_code, "date": att_date, "status": body.status,
        "punch_in": pi_utc, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes, "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this attendance date")
    return serialize_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """R3 overtime, R4 work_hours, R5 half_day. Atomic to prevent double punch-out."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    po_utc = ms_to_dt(body.punched_at) if body.punched_at else datetime.now(tz=timezone.utc).replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": po_utc}, "punch_out": None, "status": {"$in": list(PRESENCE_STATUSES)}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="No open punch-in found")
    pi_utc = ensure_utc(record["punch_in"])
    diff = (po_utc - pi_utc).total_seconds()
    if diff <= 0:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if diff > 86400:
        raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")
    wh = compute_work_hours(pi_utc, po_utc)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": po_utc, "work_hours": wh,
            "overtime_minutes": compute_overtime_minutes(po_utc, emp["shift_end"], record["date"], emp["shift_start"]),
            "half_day": wh < 4.50,
        }},
        return_document=True,
    )
    if not updated:
        raise HTTPException(409, detail="Record already punched out")
    return serialize_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records with filtering and pagination."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    q: dict[str, Any] = {}
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
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """Manual correction. Recomputes derived fields (R2-R5). Appends history entry."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="Cannot supply punch_in/punch_out with ABSENT/LEAVE")
        new_pi, new_po = None, None
    else:
        new_pi = ms_to_dt(body.punch_in) if body.punch_in is not None else ensure_utc(record.get("punch_in"))
        new_po = ms_to_dt(body.punch_out) if body.punch_out is not None else ensure_utc(record.get("punch_out"))
        if new_pi is None:
            raise HTTPException(422, detail="Presence status requires punch_in")
        if get_attendance_date(new_pi, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, detail="punch_in must remain on the record attendance date (R1)")
        if new_po is not None:
            diff = (new_po - new_pi).total_seconds()
            if diff <= 0:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if diff > 86400:
                raise HTTPException(422, detail="punch_out must be within 24 hours")

    derived = compute_all_derived(new_pi, new_po, emp["shift_start"], emp["shift_end"], date) \
        if new_status in PRESENCE_STATUSES and new_pi is not None \
        else {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    new_state = {"status": new_status, "punch_in": new_pi, "punch_out": new_po, **derived}

    TRACKABLE = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes: dict[str, Any] = {}
    for field in TRACKABLE:
        old_val = ensure_utc(record.get(field)) if field in ("punch_in", "punch_out") else record.get(field)
        new_val = new_state.get(field)
        if field == "half_day":
            old_val = old_val if old_val is not None else False
        if field in ("late_minutes", "overtime_minutes"):
            old_val = old_val if old_val is not None else 0
        if old_val != new_val:
            if field in ("punch_in", "punch_out"):
                changes[field] = {"from": dt_to_ms(old_val), "to": dt_to_ms(new_val)}
            else:
                changes[field] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, detail="Request changes nothing")

    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"]},
        {"$set": new_state, "$push": {"history": history_entry}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(409, detail="Concurrent modification - please retry")
    return serialize_record(updated)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
    first_day, last_day = parse_month(month)
    working_days = count_working_days(first_day, last_day, emp["joined_on"])
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            "is_weekday": {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}}}, [1, 7]]}]},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "is_leave": {"$eq": ["$status", "LEAVE"]},
            "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$is_weekday"]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0]}},
            "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
            "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    r = rows[0] if rows else {}
    present_days = r.get("present_days", 0)
    attendance_pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": round_half_up(present_days, 2), "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first_day, last_day = parse_month(month)
    emp_filter: dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        emp_filter["department"] = department
    pipeline = [
        {"$match": emp_filter},
        {"$lookup": {
            "from": "attendance_logs", "let": {"ec": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$ec"]}, {"$gte": ["$date", first_day]}, {"$lte": ["$date", last_day]}]}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "log_status": {"$ifNull": ["$logs.status", ""]},
            "log_late": {"$ifNull": ["$logs.late_minutes", 0]},
            "log_half": {"$ifNull": ["$logs.half_day", False]},
            "is_weekday": {"$cond": {
                "if": {"$ne": ["$logs.date", None]},
                "then": {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "timezone": "UTC"}}}, [1, 7]]}]},
                "else": False,
            }},
        }},
        {"$addFields": {
            "is_present": {"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "is_leave": {"$eq": ["$log_status", "LEAVE"]},
            "is_on_duty": {"$eq": ["$log_status", "ON_DUTY"]},
            "is_late": {"$gt": ["$log_late", 0]},
            "has_wh": {"$and": [{"$in": ["$log_status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]},
        }},
        {"$group": {
            "_id": "$department",
            "emp_set": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$is_weekday"]}, {"$cond": ["$log_half", 0.5, 1.0]}, 0]}},
            "wh_sum": {"$sum": {"$cond": ["$has_wh", "$logs.work_hours", 0]}},
            "wh_count": {"$sum": {"$cond": ["$has_wh", 1, 0]}},
            "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": ["$is_late", "$log_late", 0]}},
            "leave_count": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": ["$is_on_duty", 1, 0]}},
        }},
        {"$addFields": {"headcount": {"$size": "$emp_set"}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": ASCENDING}},
    ]
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(r["wh_sum"] / r["wh_count"], 2) if r["wh_count"] > 0 else None,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Standard competition ranking (1, 2, 2, 4). Returns all with rank <= limit."""
    first_day, last_day = parse_month(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$emp.name"},
            "department": {"$first": "$emp.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ]
    all_rows = list(db.attendance_logs.aggregate(pipeline))

    # Pre-compute late_count per employee from the aggregation (need separate group for count)
    late_count_map: dict[str, int] = {}
    for row in all_rows:
        late_count_map[row["_id"]] = row.get("late_count", 0)

    ranked, rank, i = [], 1, 0
    while i < len(all_rows):
        j = i
        while j < len(all_rows) and all_rows[j]["total_late_minutes"] == all_rows[i]["total_late_minutes"]:
            j += 1
        for k in range(i, j):
            ranked.append({
                "rank": rank,
                "emp_code": all_rows[k]["_id"],
                "name": all_rows[k]["name"],
                "department": all_rows[k]["department"],
                "total_late_minutes": all_rows[k]["total_late_minutes"],
                "late_count": all_rows[k].get("late_count", 0),
            })
        rank = j + 1
        i = j
    return {"month": month, "items": [r for r in ranked if r["rank"] <= limit]}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """One row per calendar day. $densify fills gaps. moving_avg_7d = 7-day rolling mean of non-null rates."""
    if to < from_:
        raise HTTPException(422, detail="to must be >= from")
    from_date = date_type.fromisoformat(from_)
    to_date = date_type.fromisoformat(to)
    if (to_date - from_date).days > 91:
        raise HTTPException(422, detail="Date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, detail="Department not found")

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]
    joined_dates = [e["joined_on"] for e in dept_emps]

    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}}},
        {"$addFields": {
            "day_dt": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}},
            "present_val": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0]},
            "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
        }},
        {"$group": {"_id": "$day_dt", "present_count": {"$sum": "$present_val"}, "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}}}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt]}}},
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "UTC"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"_id": ASCENDING}},
    ]
    raw_rows = list(db.attendance_logs.aggregate(pipeline))

    rows = []
    for raw in raw_rows:
        date_str = raw["date_str"]
        is_working_day = date_type.fromisoformat(date_str).weekday() < 5
        headcount = sum(1 for jd in joined_dates if jd <= date_str)
        present_count = raw["present_count"]
        attendance_rate = round_half_up(present_count / headcount, 4) if is_working_day and headcount > 0 else None
        rows.append({
            "date": date_str, "is_working_day": is_working_day, "headcount": headcount,
            "present_count": present_count, "late_count": raw["late_count"],
            "attendance_rate": attendance_rate, "moving_avg_7d": None,
        })

    for i, row in enumerate(rows):
        rates = [r["attendance_rate"] for r in rows[max(0, i - 6): i + 1] if r["attendance_rate"] is not None]
        if rates:
            row["moving_avg_7d"] = round_half_up(sum(rates) / len(rates), 4)

    return {"department": department, "items": rows}


# ---------------------------------------------------------------------------
# Admin - explain
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    VALID = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of {VALID}")
    skip = (page - 1) * page_size

    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
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
        explain = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        first_day, last_day = parse_month(month)
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": [{"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}}], "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        _, last_day = parse_month(month)
        f: dict[str, Any] = {"joined_on": {"$lte": last_day}}
        if department:
            f["department"] = department
        explain = db.command("explain", {"aggregate": "employees", "pipeline": [{"$match": f}], "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        first_day, last_day = parse_month(month)
        explain = db.command("explain", {"find": "attendance_logs", "filter": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, detail="department, from, and to are required")
        explain = db.command("explain", {"find": "attendance_logs", "filter": {"date": {"$gte": from_, "$lte": to}}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
