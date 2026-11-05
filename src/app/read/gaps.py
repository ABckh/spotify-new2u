"""Collection gaps in one period: the timeline's gap rows and the summary's N1 flag.

A gap is a stretch in which plays of a source may be missing (WHO-18
``gap-label``, WHO-13 R-55), stored in ``collection_gap``. A closed gap (G1) has
an end; an open gap (G2) runs to ``now`` while the source is still revoked or
disconnected. A gap never changes a count (R-4, S2.10f): this module only says
where gaps are, it never touches the play queries.

**Gap rows (S2.10d, 10j).** One row per local day of the period that the gap
touches, in the user's display zone. Every row of one gap carries the same real
``started_at`` and ``ended_at``; only ``local_date`` and ``sort_at`` differ.

**Where a row sits (S2.10e).** ``sort_at`` places the row in the newest-first
list: the row goes after every play with ``occurred_at >= sort_at`` and before
every play with ``occurred_at < sort_at``. ``sort_at`` is the gap's end, or the
end of the row's local day if the gap runs past it. No play of the source is
stored strictly inside a gap, so the row lands below every play at or after the
end and above every play at or before the start. ``sort_at`` never depends on
the request time, so paging is stable.

**Paging (S2.10f).** A row goes on the page whose span holds its ``sort_at``,
the same rule as the plays (:func:`rows_on_page`), so it is on exactly one page.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.read.periods import Period, local_midnight

GapState = Literal["closed", "open"]

_ONE_MICROSECOND = timedelta(microseconds=1)

# An open gap counts as running until now. Empty stretches (start == end) are no gap.
_OVERLAPPING = text(
    "SELECT source, started_at, ended_at FROM collection_gap "
    "WHERE user_id = :user_id AND started_at < :end "
    "  AND COALESCE(ended_at, :now) > :start "
    "  AND COALESCE(ended_at, :now) > started_at "
    'ORDER BY started_at, source COLLATE "C"'
)


@dataclass(frozen=True)
class Gap:
    source: str
    started_at: datetime
    ended_at: datetime | None  # None while open


@dataclass(frozen=True)
class GapRow:
    source: str
    state: GapState
    started_at: datetime
    ended_at: datetime | None
    local_date: date
    sort_at: datetime


def overlapping_gaps(
    conn: Connection, *, user_id: UUID, period: Period, now: datetime
) -> list[Gap]:
    """``user_id``'s gaps that overlap ``period``, an open one counted up to ``now``."""
    rows = conn.execute(
        _OVERLAPPING, {"user_id": user_id, "start": period.start, "end": period.end, "now": now}
    )
    return [Gap(row.source, row.started_at, row.ended_at) for row in rows]


def gap_rows(gaps: Sequence[Gap], period: Period, now: datetime) -> list[GapRow]:
    """One row per gap and local day it touches inside ``period``, newest first."""
    zone = ZoneInfo(period.time_zone)
    rows: list[GapRow] = []
    for gap in gaps:
        visible_start = max(gap.started_at, period.start)
        visible_end = min(gap.ended_at or now, period.end)
        if visible_end <= visible_start:
            continue
        day = visible_start.astimezone(zone).date()
        last_day = (visible_end - _ONE_MICROSECOND).astimezone(zone).date()
        while day <= last_day:
            day_end = local_midnight(day + timedelta(days=1), zone)
            rows.append(
                GapRow(
                    source=gap.source,
                    state="open" if gap.ended_at is None else "closed",
                    started_at=gap.started_at,
                    ended_at=gap.ended_at,
                    local_date=day,
                    sort_at=day_end if gap.ended_at is None else min(gap.ended_at, day_end),
                )
            )
            day += timedelta(days=1)
    rows.sort(key=lambda r: (r.sort_at, r.started_at, r.source), reverse=True)
    return rows


def rows_on_page(
    rows: Sequence[GapRow], *, after: datetime | None, last: datetime | None
) -> list[GapRow]:
    """The gap rows of one timeline page.

    ``after`` is the ``occurred_at`` of the previous page's last play (None on
    the first page); ``last`` is this page's last play when another page follows
    (None on the final page). A play sorts before a gap row at the same instant,
    so a row at exactly ``after`` belongs here and a row at exactly ``last``
    belongs to the next page.
    """
    return [
        row
        for row in rows
        if (after is None or row.sort_at <= after) and (last is None or row.sort_at > last)
    ]
