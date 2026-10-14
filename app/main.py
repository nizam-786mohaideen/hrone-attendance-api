
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from app.attendance import IST, attendance_metrics, as_utc

# Load .env without overriding real environment variables
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "hrone_attendance")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing from environment/.env")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

employees = db["employees"]
attendance_logs = db["attendance_logs"]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------- Startup: database indexes ----------

@app.on_event("startup")
def create_indexes():
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_employee",
    )

    employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="joined_department",
    )

    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )

    attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_status_employee",
    )


# ---------- Shared helper functions ----------

def epoch_ms(value):
    """Convert a MongoDB datetime to epoch milliseconds."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return int(value.timestamp() * 1000)


def utc_datetime_from_ms(value: int):
    """Convert epoch milliseconds to a UTC datetime, truncated to seconds."""
    seconds = value // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(
        quantum, rounding=ROUND_HALF_UP
    ))


def employee_output(doc):
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": epoch_ms(doc["created_at"]),
    }



def attendance_output(doc):
    history_output = []

    for entry in (doc.get("history") or []):
        if not isinstance(entry, dict):
            entry = {}
        changed_fields = entry.get("changes") or entry.get("changed_fields") or {}
        if not isinstance(changed_fields, dict):
            changed_fields = {}

        changes_output = {}

        for field, values in changed_fields.items():
            if not isinstance(values, dict):
                values = {"from": None, "to": values}
            old_value = values.get("from", values.get("old"))
            new_value = values.get("to", values.get("new"))

            if field in ("punch_in", "punch_out"):
                old_value = epoch_ms(old_value) if old_value is not None else None
                new_value = epoch_ms(new_value) if new_value is not None else None

            changes_output[field] = {
                "from": old_value,
                "to": new_value,
            }

        history_output.append({
            "at": (
                epoch_ms(entry.get("at") or entry.get("changed_at"))
                if entry.get("at") or entry.get("changed_at")
                else None
            ),
            "by": entry.get("by") or entry.get("regularized_by") or "unknown",
            "reason": entry.get("reason") or "",
            "changes": changes_output,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_output,
    }


# ---------- Request validation ----------

class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email format")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM format")
        return value

    @field_validator("joined_on", mode="before")
    @classmethod
    def validate_joined_on(cls, value):
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                raise ValueError("Date must use YYYY-MM-DD format")
            if parsed.isoformat() != value:
                raise ValueError("Date must use YYYY-MM-DD format")
            return parsed
        return value


# ---------- System endpoint ----------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )


# ---------- Employee endpoints ----------

@app.post("/employees", status_code=201)
def create_employee(payload: EmployeeCreate):
    if payload.shift_start == payload.shift_end:
        raise HTTPException(
            status_code=422,
            detail="shift_start and shift_end must differ",
        )

    document = payload.model_dump()
    document["joined_on"] = payload.joined_on.isoformat()

    # Set creation time on the server, never from the client.
    document["created_at"] = datetime.now(timezone.utc).replace(
        microsecond=0
    )

    try:
        employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Employee code already exists",
        )

    return employee_output(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filters = {}

    if department is not None:
        filters["department"] = department

    total = employees.count_documents(filters)

    cursor = (
        employees.find(filters, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [employee_output(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }

from datetime import timedelta


def get_ist_datetime(value):
    """Convert a UTC datetime to Indian Standard Time."""
    return as_utc(value).astimezone(IST)


def get_attendance_date(punch_in, employee):
    """Find the attendance date using the employee's shift."""
    local_time = get_ist_datetime(punch_in)
    shift_start = employee["shift_start"]
    shift_end = employee["shift_end"]
    punch_time = local_time.strftime("%H:%M")

    # For overnight shifts, early-morning punches belong
    # to the previous attendance date.
    if shift_end <= shift_start and punch_time < shift_end:
        return (local_time.date() - timedelta(days=1)).isoformat()

    return local_time.date().isoformat()

from typing import Literal
from datetime import time


class PunchInRequest(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: int | None = Field(default=None, ge=0)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


@app.post("/attendance/punch-in", status_code=201)
def punch_in(request: PunchInRequest):
    employee = employees.find_one({"emp_code": request.emp_code})

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    if request.punched_at is None:
        punch_time = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        try:
            punch_time = utc_datetime_from_ms(request.punched_at)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail="punched_at must be a valid epoch-millisecond timestamp")

    attendance_date = get_attendance_date(punch_time, employee)

    metrics = attendance_metrics(attendance_date, employee, punch_time, None)

    attendance = {
        "emp_code": request.emp_code,
        "date": attendance_date,
        "status": request.status,
        "punch_in": punch_time,
        "punch_out": None,
        "work_hours": None,
        **metrics,
        "history": []
    }

    try:
        result = attendance_logs.insert_one(attendance)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this date"
        )

    attendance["_id"] = result.inserted_id
    return attendance_output(attendance)

class PunchOutRequest(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: int | None = Field(default=None, ge=0)


@app.post("/attendance/punch-out")
def punch_out(request: PunchOutRequest):
    employee = employees.find_one({"emp_code": request.emp_code})

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if request.punched_at is None:
        punch_time = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        try:
            punch_time = utc_datetime_from_ms(request.punched_at)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail="punched_at must be a valid epoch-millisecond timestamp")

    record = attendance_logs.find_one(
        {
            "emp_code": request.emp_code,
            "punch_in": {"$lte": punch_time}
        },
        sort=[("punch_in", DESCENDING)]
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found"
        )

    if record["punch_out"] is not None:
        raise HTTPException(
            status_code=409,
            detail="Already punched out"
        )

    try:
        metrics = attendance_metrics(record["date"], employee, record.get("punch_in"), punch_time)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    result = attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_time,
                **metrics
            }
        }
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already updated"
        )

    updated_record = attendance_logs.find_one({"_id": record["_id"]})
    return attendance_output(updated_record)

@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    from datetime import date as date_type

    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page must be >= 1 and page_size must be between 1 and 100"
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        allowed_statuses = {
            "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
        }
        if status not in allowed_statuses:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status

    try:
        if date_from:
            date_type.fromisoformat(date_from)
            query.setdefault("date", {})["$gte"] = date_from

        if date_to:
            date_type.fromisoformat(date_to)
            query.setdefault("date", {})["$lte"] = date_to
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format"
        )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to"
        )

    total = attendance_logs.count_documents(query)

    records = list(
        attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [attendance_output(record) for record in records],
        "total": total,
        "page": page,
        "page_size": page_size
    }

from pydantic import BaseModel, Field, model_validator
from typing import Literal


class RegularizeRequest(BaseModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: int | None = Field(default=None, ge=0)
    punch_out: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1)
    regularized_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_fields(self):
        if self.status is None and self.punch_in is None and self.punch_out is None:
            raise ValueError("Provide at least one field to update")
        return self


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: str,
    request: RegularizeRequest
):
    from datetime import date as date_type

    try:
        date_type.fromisoformat(attendance_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD")

    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = attendance_logs.find_one({
        "emp_code": emp_code,
        "date": attendance_date
    })
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    changes = {}
    if request.status is not None:
        changes["status"] = request.status
    if request.punch_in is not None:
        try:
            changes["punch_in"] = utc_datetime_from_ms(request.punch_in)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail="punch_in must be a valid epoch-millisecond timestamp")
    if request.punch_out is not None:
        try:
            changes["punch_out"] = utc_datetime_from_ms(request.punch_out)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail="punch_out must be a valid epoch-millisecond timestamp")

    final_status = changes.get("status", record["status"])
    final_in = changes.get("punch_in", record["punch_in"])
    final_out = changes.get("punch_out", record["punch_out"])

    if final_status in ("ABSENT", "LEAVE"):
        final_in = None
        final_out = None
        changes["punch_in"] = None
        changes["punch_out"] = None
        changes["work_hours"] = None
        changes["late_minutes"] = 0
        changes["overtime_minutes"] = 0
        changes["half_day"] = False
    else:
        if final_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in"
            )

        if get_attendance_date(final_in, employee) != attendance_date:
            raise HTTPException(
                status_code=422,
                detail="Punch-in must be on the attendance date"
            )

        if final_out is not None:
            try:
                changes.update(attendance_metrics(attendance_date, employee, final_in, final_out))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        else:
            changes.update(attendance_metrics(attendance_date, employee, final_in, None))

    history_entry = {
        "changes": {
            key: {
                "from": record.get(key),
                "to": value
            }
            for key, value in changes.items()
            if record.get(key) != value
        },
        "reason": request.reason,
        "by": request.regularized_by,
        "at": datetime.now(timezone.utc).replace(microsecond=0)
    }

    if not history_entry["changes"]:
        raise HTTPException(status_code=422, detail="No actual changes to save")

    result = attendance_logs.update_one(
        {"_id": record["_id"]},
        {
            "$set": changes,
            "$push": {"history": history_entry}
        }
    )

    updated = attendance_logs.find_one({"_id": record["_id"]})
    return attendance_output(updated)
