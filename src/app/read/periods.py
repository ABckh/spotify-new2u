"""The one period resolver: ``period`` + ``anchor | offset | from/to`` -> UTC bounds.

Implements the WHO-13 ``period-model`` document; rule numbers (R-n) refer to it.
Every endpoint that takes a period goes through ``resolve_period``, and the app
never computes a boundary itself (R-37).

- A period is half-open: ``start <= occurred_at < end`` (R-11).
- Bounds are local midnights in the display zone, converted to UTC (R-13). Days
  are added to *dates*, never as 24 hours of seconds, so a DST day is 23 or 25
  hours long and a DST week is 167 or 169 (R-16).
- Weeks start on Monday, always (R-15).
- Nothing is clamped or guessed: anything ambiguous raises ``PeriodError`` with a
  stable code, which the API answers with 422 (R-22).

The display zone is ``app_user.home_timezone`` (schema rev 3 §1.1), passed in by
the caller. The client's own clock and zone are never inputs (R-8).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PeriodType = Literal["day", "week", "month", "custom"]
PERIOD_TYPES: tuple[PeriodType, ...] = ("day", "week", "month", "custom")

# R-15: fixed, not a setting.
WEEK_START = "monday"
# R-21: from..to, both included, counted as calendar dates.
MAX_CUSTOM_DAYS = 366
# Far beyond any history we hold; keeps date arithmetic inside datetime's range.
MAX_ABS_OFFSET = 10_000

ErrorCode = Literal[
    "period_range_reversed",
    "period_range_too_long",
    "period_invalid_date",
    "period_range_incomplete",
    "period_anchor_conflict",
    "period_invalid_time_zone",
    "period_invalid_parameter",
]

# [0-9], not \d: \d also matches non-ASCII digits, which int() would accept.
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_OFFSET = re.compile(r"[+-]?[0-9]{1,5}")


class PeriodError(ValueError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PeriodQuery:
    """The raw period parameters, exactly as the client sent them (None if absent)."""

    period: str | None = None
    anchor: str | None = None
    offset: str | None = None
    from_: str | None = None
    to: str | None = None


@dataclass(frozen=True)
class Period:
    type: PeriodType
    offset: int | None
    anchor: date | None
    time_zone: str
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive
    local_start_date: date
    local_end_date: date  # the last included date (R-43)
    is_partial: bool  # end is after now (R-18)


def display_zone(name: str) -> ZoneInfo:
    """The user's stored zone, or ``period_invalid_time_zone`` (R-22)."""
    try:
        if not name or name.startswith("/") or ".." in name:
            raise ValueError(name)
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise PeriodError(
            "period_invalid_time_zone", "The stored time zone is not a known IANA zone."
        ) from None


def local_midnight(day: date, zone: ZoneInfo) -> datetime:
    """The first instant of ``day`` in ``zone``, in UTC (R-13, R-17).

    Usually local 00:00. If 00:00 happens twice, the earlier one. If it does not
    happen (a DST gap at midnight), the first instant that does exist after it.
    """
    naive = datetime(day.year, day.month, day.day)
    candidates = [naive.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    existing = [c for c in candidates if c.astimezone(zone).replace(tzinfo=None) == naive]
    if existing:
        return min(existing)
    # In a gap: fold=1 maps before the jump, fold=0 after it. The first instant
    # whose local time is at or after midnight lies between; search to the second.
    low, high = (int(c.timestamp()) for c in sorted(candidates))
    while high - low > 1:
        middle = (low + high) // 2
        if datetime.fromtimestamp(middle, zone).replace(tzinfo=None) >= naive:
            high = middle
        else:
            low = middle
    return datetime.fromtimestamp(high, UTC)


def resolve_period(query: PeriodQuery, time_zone: str, now: datetime) -> Period:
    """Turn the request's period parameters into bounds. Raises ``PeriodError``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    period_type = _period_type(query.period)

    if period_type == "custom":
        if query.anchor is not None or query.offset is not None:
            raise PeriodError(
                "period_invalid_parameter", "anchor and offset are not used with period=custom."
            )
        if query.from_ is None or query.to is None:
            raise PeriodError("period_range_incomplete", "period=custom needs both from and to.")
        first, last = _parse_date(query.from_), _parse_date(query.to)
        if last < first:
            raise PeriodError("period_range_reversed", "to is before from.")
        if (last - first).days + 1 > MAX_CUSTOM_DAYS:
            raise PeriodError(
                "period_range_too_long", f"A custom range is at most {MAX_CUSTOM_DAYS} days."
            )
        zone = display_zone(time_zone)
        return _period("custom", None, None, first, last, zone, time_zone, now)

    if query.from_ is not None or query.to is not None:
        raise PeriodError("period_invalid_parameter", "from and to are only used with custom.")
    if query.anchor is not None and query.offset is not None:
        raise PeriodError("period_anchor_conflict", "Send anchor or offset, not both.")
    anchor = _parse_date(query.anchor) if query.anchor is not None else None
    offset = _parse_offset(query.offset) if anchor is None else None
    zone = display_zone(time_zone)
    try:
        if anchor is None:
            assert offset is not None
            today = now.astimezone(zone).date()
            first, last = _shift(_containing(period_type, today), period_type, offset)
        else:
            first, last = _containing(period_type, anchor)
    except (OverflowError, ValueError):  # past year 9999 or before year 1
        raise PeriodError("period_invalid_parameter", "The period is out of range.") from None
    return _period(period_type, offset, anchor, first, last, zone, time_zone, now)


def _period_type(value: str | None) -> PeriodType:
    for period_type in PERIOD_TYPES:
        if value == period_type:
            return period_type
    raise PeriodError("period_invalid_parameter", "period must be day, week, month or custom.")


def _parse_date(value: str) -> date:
    # date.fromisoformat also takes "20260413" and "2026-W15-1"; only YYYY-MM-DD is valid.
    if not _DATE.fullmatch(value):
        raise PeriodError("period_invalid_date", "Dates are YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise PeriodError("period_invalid_date", "That date does not exist.") from None


def _parse_offset(value: str | None) -> int:
    if value is None:
        return 0
    if not _OFFSET.fullmatch(value) or abs(int(value)) > MAX_ABS_OFFSET:
        raise PeriodError(
            "period_invalid_parameter",
            f"offset is a whole number from -{MAX_ABS_OFFSET} to {MAX_ABS_OFFSET}.",
        )
    return int(value)


def _containing(period_type: PeriodType, day: date) -> tuple[date, date]:
    """First and last local date of the day, week or month containing ``day`` (R-14)."""
    if period_type == "day":
        return day, day
    if period_type == "week":
        monday = day - timedelta(days=day.weekday())
        return monday, monday + timedelta(days=6)
    first = day.replace(day=1)
    return first, _next_month(first) - timedelta(days=1)


def _shift(bounds: tuple[date, date], period_type: PeriodType, offset: int) -> tuple[date, date]:
    first, _ = bounds
    if period_type == "day":
        return _containing("day", first + timedelta(days=offset))
    if period_type == "week":
        return _containing("week", first + timedelta(weeks=offset))
    months = first.year * 12 + first.month - 1 + offset
    return _containing("month", date(months // 12, months % 12 + 1, 1))


def _next_month(first: date) -> date:
    return date(first.year + first.month // 12, first.month % 12 + 1, 1)


def _period(
    period_type: PeriodType,
    offset: int | None,
    anchor: date | None,
    first: date,
    last: date,
    zone: ZoneInfo,
    time_zone: str,
    now: datetime,
) -> Period:
    try:
        start = local_midnight(first, zone)
        end = local_midnight(last + timedelta(days=1), zone)
    except (OverflowError, ValueError):
        raise PeriodError("period_invalid_parameter", "The period is out of range.") from None
    return Period(
        type=period_type,
        offset=offset,
        anchor=anchor,
        time_zone=time_zone,
        start=start,
        end=end,
        local_start_date=first,
        local_end_date=last,
        is_partial=end > now,
    )
