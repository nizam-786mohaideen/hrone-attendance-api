"""Pure attendance calculations shared by API routes and tests."""
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
HALF_DAY_HOURS = float(os.getenv("HALF_DAY_HOURS", "4.5"))
LATE_GRACE_MINUTES = 10
OVERTIME_GRACE_MINUTES = 30


def as_utc(value: datetime) -> datetime:
    """Mongo stores UTC; legacy naive datetimes are treated as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def shift_bounds(attendance_date: str | date, shift_start: str, shift_end: str):
    day = date.fromisoformat(attendance_date) if isinstance(attendance_date, str) else attendance_date
    start_time = time.fromisoformat(shift_start)
    end_time = time.fromisoformat(shift_end)
    start = datetime.combine(day, start_time, tzinfo=IST)
    end = datetime.combine(day, end_time, tzinfo=IST)
    if end_time <= start_time:
        end += timedelta(days=1)
    return start, end


def attendance_metrics(attendance_date, employee, punch_in, punch_out):
    """Return calculated fields. Missing either punch means no completed duration."""
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}

    punch_in = as_utc(punch_in)
    start, end = shift_bounds(attendance_date, employee["shift_start"], employee["shift_end"])
    local_in = punch_in.astimezone(IST)
    late_seconds = int((local_in - start).total_seconds())
    late = late_seconds // 60 if late_seconds > LATE_GRACE_MINUTES * 60 else 0

    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}

    punch_out = as_utc(punch_out)
    seconds = int((punch_out - punch_in).total_seconds())
    if seconds <= 0 or seconds > 24 * 60 * 60:
        raise ValueError("Punch-out must be after punch-in and within 24 hours")
    hours = float(Decimal(str(seconds / 3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    overtime_seconds = int((punch_out.astimezone(IST) - end).total_seconds())
    overtime = overtime_seconds // 60 if overtime_seconds >= OVERTIME_GRACE_MINUTES * 60 else 0
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime,
            "half_day": hours < HALF_DAY_HOURS}
