"""The calling user's events in one period, newest first, keyset-paged.

Order is ``occurred_at`` descending, then ``item_key`` ascending, then ``id``
ascending (R-47), so two plays at the same instant always come back in the same
order. Text is compared with ``COLLATE "C"`` (code point order) in both the
``ORDER BY`` and the cursor predicate: the two must agree, or a page boundary
inside a tie skips or repeats a row.

Every statement carries ``user_id = :user_id`` from the session. A cursor only
says where to continue inside that user's own rows; it never names a user.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.read.periods import Period

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_COLUMNS = (
    "id, source, kind, occurred_at, occurred_at_precision, duration_ms, played_ms, "
    "title, creators, collection, artwork_url, item_key"
)
_ORDER = 'ORDER BY occurred_at DESC, item_key COLLATE "C" ASC, id ASC'

_FIRST_PAGE = text(
    f"SELECT {_COLUMNS} FROM unified_event "
    "WHERE user_id = :user_id AND occurred_at >= :start AND occurred_at < :end "
    f"{_ORDER} LIMIT :limit"
)
_NEXT_PAGE = text(
    f"SELECT {_COLUMNS} FROM unified_event "
    "WHERE user_id = :user_id AND occurred_at >= :start AND occurred_at < :end "
    "  AND (occurred_at < :after_at "
    "       OR (occurred_at = :after_at "
    '           AND (item_key COLLATE "C" > :after_key '
    "                OR (item_key = :after_key AND id > :after_id)))) "
    f"{_ORDER} LIMIT :limit"
)


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class Cursor:
    """The last row of the previous page, and the period it was a page of."""

    period_start: datetime
    period_end: datetime
    occurred_at: datetime
    item_key: str
    id: UUID

    def encode(self) -> str:
        payload = {
            "s": self.period_start.isoformat(),
            "e": self.period_end.isoformat(),
            "t": self.occurred_at.isoformat(),
            "k": self.item_key,
            "i": str(self.id),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "Cursor":
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            payload = json.loads(raw)
            cursor = cls(
                period_start=datetime.fromisoformat(payload["s"]),
                period_end=datetime.fromisoformat(payload["e"]),
                occurred_at=datetime.fromisoformat(payload["t"]),
                item_key=str(payload["k"]),
                id=UUID(payload["i"]),
            )
        except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise InvalidCursor from None
        for moment in (cursor.period_start, cursor.period_end, cursor.occurred_at):
            if moment.tzinfo is None:
                raise InvalidCursor
        return cursor


@dataclass(frozen=True)
class TimelineEvent:
    id: UUID
    source: str
    kind: str
    occurred_at: datetime
    occurred_at_precision: str
    local_date: date
    duration_ms: int | None
    played_ms: int | None
    title: str
    creators: list[str]
    collection: str | None
    artwork_url: str | None
    item_key: str


@dataclass(frozen=True)
class TimelinePage:
    events: list[TimelineEvent]
    next_cursor: str | None
    # The previous page's last play, None on the first page. Gap rows are paged by it.
    after: datetime | None


def read_timeline(
    conn: Connection,
    *,
    user_id: UUID,
    period: Period,
    cursor: str | None,
    limit: int,
) -> TimelinePage:
    """One page of ``user_id``'s events in ``period``. Raises ``InvalidCursor``.

    A cursor from a different period is refused rather than reinterpreted: for
    ``offset=0`` that happens when the day rolls over between two pages, and the
    app starts again from the first page.
    """
    params: dict[str, Any] = {
        "user_id": user_id,
        "start": period.start,
        "end": period.end,
        "limit": limit + 1,  # one extra row says whether there is a next page
    }
    after_at: datetime | None = None
    if cursor is None:
        statement = _FIRST_PAGE
    else:
        after = Cursor.decode(cursor)
        if (after.period_start, after.period_end) != (period.start, period.end):
            raise InvalidCursor
        statement = _NEXT_PAGE
        after_at = after.occurred_at
        params |= {"after_at": after.occurred_at, "after_key": after.item_key, "after_id": after.id}

    rows = conn.execute(statement, params).all()
    zone = ZoneInfo(period.time_zone)
    events = [
        TimelineEvent(
            id=row.id,
            source=row.source,
            kind=row.kind,
            occurred_at=row.occurred_at,
            occurred_at_precision=row.occurred_at_precision,
            # The same zone and the same instant as the period bounds, so a day
            # header holds exactly what that day's summary counts (R-48).
            local_date=row.occurred_at.astimezone(zone).date(),
            duration_ms=row.duration_ms,
            played_ms=row.played_ms,
            title=row.title,
            creators=list(row.creators),
            collection=row.collection,
            artwork_url=row.artwork_url,
            item_key=row.item_key,
        )
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = events[-1]
        next_cursor = Cursor(
            period.start, period.end, last.occurred_at, last.item_key, last.id
        ).encode()
    return TimelinePage(events=events, next_cursor=next_cursor, after=after_at)
