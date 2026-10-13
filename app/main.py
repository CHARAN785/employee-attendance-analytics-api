"""Employee Attendance & Analytics API."""

import os
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
logger = logging.getLogger(__name__)


def create_indexes() -> None:
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uniq_employee_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_employee_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined_on",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uniq_employee_attendance_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name="attendance_employee_date_desc",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_employee_punch_in",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    logger.warning(
        "MongoDB request failed at %s (%s)", request.url.path, type(exc).__name__
    )
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


def _validate_date(value: str, field_name: str = "date") -> date:
    if not DATE_PATTERN.fullmatch(value):
        raise HTTPException(422, f"{field_name} must be in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{field_name} must be a valid date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be in YYYY-MM-DD format")
    return parsed


def _month_bounds(month: str) -> tuple[str, str, date, date]:
    if not MONTH_PATTERN.fullmatch(month):
        raise HTTPException(422, "month must be in YYYY-MM format")
    try:
        first = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(422, "month must be in YYYY-MM format") from exc
    if first.strftime("%Y-%m") != month:
        raise HTTPException(422, "month must be in YYYY-MM format")
    next_month = (
        date(first.year + 1, 1, 1)
        if first.month == 12
        else date(first.year, first.month + 1, 1)
    )
    return first.isoformat(), next_month.isoformat(), first, next_month - timedelta(days=1)


def _parse_shift(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def _utc_datetime_from_ms(value: int) -> datetime:
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be between 100000000000 and 4102444800000")
    return (EPOCH + timedelta(milliseconds=value)).replace(microsecond=0)


def _datetime_to_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    delta = value - EPOCH
    return (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)):
        return EPOCH + timedelta(milliseconds=value)
    raise HTTPException(422, "stored punch timestamps must be dates")


def _attendance_date(punch_in: datetime, employee: dict[str, Any]) -> date:
    local = punch_in.astimezone(IST)
    attendance_day = local.date()
    shift_start = _parse_shift(employee["shift_start"])
    shift_end = _parse_shift(employee["shift_end"])
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < shift_end:
        attendance_day -= timedelta(days=1)
    return attendance_day


def _late_minutes(punch_in: datetime, attendance_day: date, shift_start: str) -> int:
    local = punch_in.astimezone(IST)
    start = datetime.combine(attendance_day, _parse_shift(shift_start), tzinfo=IST)
    elapsed = int((local - start).total_seconds())
    if elapsed <= 10 * 60:
        return 0
    return elapsed // 60


def _overtime_minutes(
    punch_out: datetime, attendance_day: date, employee: dict[str, Any]
) -> int:
    start = _parse_shift(employee["shift_start"])
    end = _parse_shift(employee["shift_end"])
    end_day = attendance_day + (timedelta(days=1) if end <= start else timedelta())
    shift_end = datetime.combine(end_day, end, tzinfo=IST)
    overtime = int((punch_out.astimezone(IST) - shift_end).total_seconds()) // 60
    return overtime if overtime >= 30 else 0


def _work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def _round_decimal(value: Any, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _public_employee(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": _datetime_to_ms(document.get("created_at")),
    }


def _public_attendance(document: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in document.get("history") or []:
        changes: dict[str, Any] = {}
        for field, values in (entry.get("changes") or {}).items():
            before = values.get("from")
            after = values.get("to")
            if field in ("punch_in", "punch_out"):
                before = _datetime_to_ms(before)
                after = _datetime_to_ms(after)
            changes[field] = {"from": before, "to": after}
        history.append(
            {
                "at": _datetime_to_ms(entry.get("at")),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": changes,
            }
        )

    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": _datetime_to_ms(document.get("punch_in")),
        "punch_out": _datetime_to_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": history,
    }


def _date_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None:
        _validate_date(date_from, "date_from")
    if date_to is not None:
        _validate_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from or date_to:
        date_filter: dict[str, str] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        query["date"] = date_filter
    return query


def _monthly_pipeline(
    emp_code: str, start: str, next_month: str, month_end: str
) -> list[dict[str, Any]]:
    weekday = {"$and": [
        {"$gte": [{"$dayOfWeek": "$_date_value"}, 2]},
        {"$lte": [{"$dayOfWeek": "$_date_value"}, 6]},
    ]}
    is_present = {"$in": ["$logs.status", list(PRESENCE_STATUSES)]}
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$addFields": {
                "_period_start": {
                    "$cond": [
                        {"$gt": ["$joined_on", start]},
                        {"$dateFromString": {"dateString": "$joined_on"}},
                        {"$dateFromString": {"dateString": start}},
                    ]
                },
                "_period_end": {"$dateFromString": {"dateString": month_end}},
            }
        },
        {
            "$addFields": {
                "_calendar_days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": "$_period_start",
                                                "endDate": "$_period_end",
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_period_start",
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {
            "$addFields": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_calendar_days",
                            "as": "calendar_day",
                            "cond": {
                                "$and": [
                                    {"$gte": [{"$dayOfWeek": "$$calendar_day"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$$calendar_day"}, 6]},
                                ]
                            },
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_date_value": {"$dateFromString": {"dateString": "$logs.date"}}}},
        {
            "$group": {
                "_id": "$emp_code",
                "working_days": {"$first": "$_working_days"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [is_present, weekday]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
            }
        },
    ]


def _department_summary_pipeline(
    start: str, next_month: str, month_end: str, department: Optional[str]
) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    present = {"$in": ["$logs.status", list(PRESENCE_STATUSES)]}
    weekday = {"$and": [
        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
    ]}
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [present, weekday]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    present,
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            {"$ifNull": ["$logs.work_hours", 0]},
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    present,
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def _leaderboard_pipeline(
    start: str, next_month: str, department: Optional[str], limit: int
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lt": next_month}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": "$emp_code",
                    "name": {"$first": "$employee.name"},
                    "department": {"$first": "$employee.department"},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def _trend_pipeline(
    department: str, first_day: date, day_count: int
) -> list[dict[str, Any]]:
    return [
        {"$match": {"department": department}},
        {
            "$addFields": {
                "_trend_dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": datetime.combine(first_day, time.min, tzinfo=UTC),
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_trend_dates"},
        {
            "$addFields": {
                "_trend_date_string": {
                    "$dateToString": {
                        "date": "$_trend_dates",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "day": "$_trend_date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$eq": ["$date", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "_day_logs",
            }
        },
        {"$unwind": {"path": "$_day_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$_trend_dates",
                "headcount": {
                    "$sum": {"$cond": [{"$lte": ["$joined_on", "$_trend_date_string"]}, 1, 0]}
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$_day_logs.status", list(PRESENCE_STATUSES)]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$_day_logs.half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$_day_logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


EpochMillis = Annotated[
    StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
]


class EmployeeIn(InputModel):
    emp_code: StrictStr
    name: StrictStr
    email: StrictStr
    department: StrictStr
    shift_start: StrictStr = "09:30"
    shift_end: StrictStr = "18:30"
    joined_on: StrictStr

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must be EMP followed by 4 to 6 digits")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not 1 <= len(value) <= 100:
            raise ValueError("name must contain 1 to 100 characters")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if len(value) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email is invalid")
        return value

    @field_validator("department")
    @classmethod
    def validate_department(cls, value: str) -> str:
        if not 1 <= len(value) <= 50:
            raise ValueError("department must contain 1 to 50 characters")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not TIME_PATTERN.fullmatch(value):
            raise ValueError("shift times must use 24-hour HH:MM format")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_distinct_shift_times(cls, value: str, info: Any) -> str:
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start must differ from shift_end")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        _validate_date(value, "joined_on")
        return value


class PunchInIn(InputModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(InputModel):
    emp_code: StrictStr
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(InputModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[StrictStr, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[StrictStr, Field(min_length=1, max_length=50)]


@app.get("/health")
def health() -> dict[str, str]:
    db.command("ping")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    document = body.model_dump()
    document["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return _public_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    documents = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_public_employee(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an integer epoch timestamp")
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punch_in_at = (
        _utc_datetime_from_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    attendance_day = _attendance_date(punch_in_at, employee)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punch_in_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": _late_minutes(punch_in_at, attendance_day, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return _public_attendance(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an integer epoch timestamp")
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punch_out_at = (
        _utc_datetime_from_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_at}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "punch-in record not found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_at = _as_datetime(record.get("punch_in"))
    if punch_in_at is None:
        raise HTTPException(404, "punch-in record not found")
    elapsed = punch_out_at - punch_in_at
    if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")

    attendance_day = _validate_date(record["date"])
    hours = _work_hours(punch_in_at, punch_out_at)
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_at,
                "work_hours": hours,
                "half_day": hours < 4.50,
                "overtime_minutes": _overtime_minutes(punch_out_at, attendance_day, employee),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return _public_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = _date_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    documents = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_public_attendance(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str, date: str, body: RegularizeIn
) -> dict[str, Any]:
    attendance_day = _validate_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    if "status" in supplied and body.status is None:
        raise HTTPException(422, "status cannot be null")
    if any(field in supplied and getattr(body, field) is None for field in ("punch_in", "punch_out")):
        raise HTTPException(422, "punch timestamps cannot be null")
    next_status = body.status if "status" in supplied else record["status"]
    absence_status = next_status in ("ABSENT", "LEAVE")
    if absence_status and ({"punch_in", "punch_out"} & supplied):
        raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")

    old_punch_in = _as_datetime(record.get("punch_in"))
    old_punch_out = _as_datetime(record.get("punch_out"))
    if absence_status:
        next_punch_in = None
        next_punch_out = None
    else:
        next_punch_in = (
            _utc_datetime_from_ms(body.punch_in)
            if "punch_in" in supplied and body.punch_in is not None
            else old_punch_in
        )
        next_punch_out = (
            _utc_datetime_from_ms(body.punch_out)
            if "punch_out" in supplied and body.punch_out is not None
            else old_punch_out
        )
        if next_punch_in is None:
            raise HTTPException(422, "presence status requires a punch_in")
        if _attendance_date(next_punch_in, employee).isoformat() != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if next_punch_out is not None:
            elapsed = next_punch_out - next_punch_in
            if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if absence_status:
        next_work_hours = None
        next_late_minutes = 0
        next_overtime_minutes = 0
        next_half_day = False
    else:
        next_late_minutes = _late_minutes(
            next_punch_in, attendance_day, employee["shift_start"]
        )
        if next_punch_out is None:
            next_work_hours = None
            next_overtime_minutes = 0
            next_half_day = False
        else:
            next_work_hours = _work_hours(next_punch_in, next_punch_out)
            next_overtime_minutes = _overtime_minutes(
                next_punch_out, attendance_day, employee
            )
            next_half_day = next_work_hours < 4.50

    new_values = {
        "status": next_status,
        "punch_in": next_punch_in,
        "punch_out": next_punch_out,
        "work_hours": next_work_hours,
        "late_minutes": next_late_minutes,
        "overtime_minutes": next_overtime_minutes,
        "half_day": next_half_day,
    }
    old_values = {
        "status": record.get("status"),
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changed_fields = {
        key: {"from": old_values[key], "to": new_values[key]}
        for key in new_values
        if old_values[key] != new_values[key]
    }
    if not changed_fields:
        raise HTTPException(422, "regularization does not change the record")

    changes_for_history = {
        key: {
            "from": value["from"],
            "to": value["to"],
        }
        for key, value in changed_fields.items()
    }
    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_for_history,
    }
    history_filter: dict[str, Any]
    if "history" in record:
        history_filter = {"history": record["history"]}
    else:
        history_filter = {"history": {"$exists": False}}
    compare_and_set = {
        "emp_code": emp_code,
        "date": date,
        "status": record.get("status"),
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        **history_filter,
    }
    updated = db.attendance_logs.find_one_and_update(
        compare_and_set,
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed; retry the correction")
    return _public_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    start, next_month, _, last = _month_bounds(month)
    result = next(
        iter(
            db.employees.aggregate(
                _monthly_pipeline(emp_code, start, next_month, last.isoformat())
            )
        ),
        {},
    )
    working_days = result.get("working_days", 0)
    present_days = result.get("present_days", 0)
    attendance_pct = (
        _round_decimal(Decimal(str(present_days)) * Decimal(100) / Decimal(working_days), 4)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str, department: Optional[str] = None
) -> dict[str, Any]:
    start, next_month, _, last = _month_bounds(month)
    pipeline = _department_summary_pipeline(start, next_month, last.isoformat(), department)
    items = []
    for result in db.employees.aggregate(pipeline):
        item = {
            "department": result["department"],
            "headcount": result["headcount"],
            "present_days": result["present_days"],
            "avg_work_hours": (
                _round_decimal(result["avg_work_hours"])
                if result["avg_work_hours"] is not None
                else None
            ),
            "late_count": result["late_count"],
            "total_late_minutes": result["total_late_minutes"],
            "leave_count": result["leave_count"],
            "on_duty_count": result["on_duty_count"],
        }
        items.append(item)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    start, next_month, _, _ = _month_bounds(month)
    pipeline = _leaderboard_pipeline(start, next_month, department, limit)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> dict[str, Any]:
    first_day = _validate_date(from_date, "from")
    last_day = _validate_date(to_date, "to")
    if last_day < first_day:
        raise HTTPException(422, "to must be on or after from")
    day_count = (last_day - first_day).days + 1
    if day_count > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")

    pipeline = _trend_pipeline(department, first_day, day_count)
    items = []
    for result in db.employees.aggregate(pipeline):
        result["attendance_rate"] = (
            _round_decimal(result["attendance_rate"], 4)
            if result.get("attendance_rate") is not None
            else None
        )
        result["moving_avg_7d"] = (
            _round_decimal(result["moving_avg_7d"], 4)
            if result.get("moving_avg_7d") is not None
            else None
        )
        items.append(result)
    return {"department": department, "items": items}


def _explain_aggregate(collection: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
) -> dict[str, Any]:
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(422, f"unsupported endpoint; choose one of: {sorted(allowed)}")

    if endpoint == "attendance_list":
        query = _date_query(emp_code, date_from, date_to, status)
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "projection": {"_id": 0},
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    else:
        if not month and endpoint in {
            "employee_monthly",
            "department_summary",
            "late_leaderboard",
        }:
            raise HTTPException(422, "month is required")
        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")
            start, next_month, _, last = _month_bounds(month)
            pipeline = _monthly_pipeline(emp_code, start, next_month, last.isoformat())
            collection = "employees"
        elif endpoint == "department_summary":
            start, next_month, _, last = _month_bounds(month)
            pipeline = _department_summary_pipeline(
                start, next_month, last.isoformat(), department
            )
            collection = "employees"
        elif endpoint == "late_leaderboard":
            start, next_month, _, _ = _month_bounds(month)
            pipeline = _leaderboard_pipeline(start, next_month, department, limit)
            collection = "attendance_logs"
        else:
            if department is None or from_date is None or to_date is None:
                raise HTTPException(422, "department, from and to are required")
            first_day = _validate_date(from_date, "from")
            last_day = _validate_date(to_date, "to")
            if last_day < first_day:
                raise HTTPException(422, "to must be on or after from")
            day_count = (last_day - first_day).days + 1
            if day_count > 92:
                raise HTTPException(422, "date range must not exceed 92 days")
            pipeline = _trend_pipeline(department, first_day, day_count)
            collection = "employees"
        explain = _explain_aggregate(collection, pipeline)

    return {"endpoint": endpoint, "collection": collection, "explain": explain}
