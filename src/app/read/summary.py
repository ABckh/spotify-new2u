"""Totals and top lists for one period of the calling user's plays (R-25 to R-36, R-52).

- ``total_ms`` sums ``coalesce(played_ms, duration_ms, 0)`` (R-25). For Spotify
  ``played_ms`` is always NULL, so this is the sum of full track lengths.
- ``play_count`` counts rows, repeats included (R-28). An event with neither
  duration adds 1 play, 0 ms, and 1 to ``events_without_duration`` (R-27).
- Artists are creator strings after NFC and trimming whitespace, nothing else
  (R-31). An event adds one play to each of its distinct artists (R-33), so
  artist plays can add up to more than ``play_count`` (R-34).
- Both top lists end their ``ORDER BY`` on a key unique in the group, compared
  by code point (``COLLATE "C"``), so the order never depends on the planner (R-35).
- A collection gap never changes a total (R-4, R-55). It only sets
  ``collection_gap_overlap`` (N1, S3.8b) and, for an empty period, the
  ``collection_gap`` empty reason (E4).

Every statement is scoped by ``user_id = :user_id`` from the session.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.read.album_sql import track_artwork
from app.read.gaps import overlapping_gaps
from app.read.names import ARTIST_NAME
from app.read.periods import Period

DEFAULT_TOP = 5
MAX_TOP = 50

EmptyReason = Literal["no_data_yet", "before_coverage", "collection_gap", "no_plays"]

# The user's plays in the period, one row each, with the R-25 duration.
_PERIOD_EVENTS = """
    period_events AS (
        SELECT id, item_key, title, creators, artwork_url, occurred_at,
               COALESCE(played_ms, duration_ms, 0)::bigint AS ms,
               (played_ms IS NULL AND duration_ms IS NULL) AS without_duration
        FROM unified_event
        WHERE user_id = :user_id AND occurred_at >= :start AND occurred_at < :end
    )
"""


# One row per (event, artist). DISTINCT: an event listing an artist twice still
# adds one play to that artist.
_EVENT_ARTISTS = f"""
    event_artists AS (
        SELECT DISTINCT e.id, e.ms, artist.name
        FROM period_events e
        CROSS JOIN LATERAL unnest(e.creators) AS creator(raw)
        CROSS JOIN LATERAL (SELECT {ARTIST_NAME} AS name) AS artist
        WHERE artist.name <> ''
    )
"""

_TOTALS = text(
    f"WITH {_PERIOD_EVENTS}, {_EVENT_ARTISTS} "
    "SELECT count(*) AS play_count, "
    "       count(DISTINCT item_key) AS distinct_tracks, "
    "       (SELECT count(DISTINCT name) FROM event_artists) AS distinct_artists, "
    "       COALESCE(sum(ms), 0) AS total_ms, "
    "       count(*) FILTER (WHERE without_duration) AS events_without_duration "
    "FROM period_events"
)

# Title and creators shown for a track are those of its latest play in the period,
# the cover that of its latest play in the period with one (WHO-149).
_TOP_TRACKS = text(
    f"WITH {_PERIOD_EVENTS}, "
    f"{track_artwork('period_events', 'SELECT item_key FROM period_events')}, "
    "latest AS ("
    "    SELECT DISTINCT ON (item_key) item_key, title, creators FROM period_events "
    "    ORDER BY item_key, occurred_at DESC, id"
    "), "
    "tracks AS ("
    "    SELECT item_key, count(*) AS play_count, sum(ms) AS total_ms "
    "    FROM period_events GROUP BY item_key"
    ") "
    "SELECT t.item_key, l.title, l.creators, a.artwork_url, t.play_count, t.total_ms "
    "FROM tracks t JOIN latest l USING (item_key) LEFT JOIN track_artwork a USING (item_key) "
    'ORDER BY t.play_count DESC, t.total_ms DESC, l.title COLLATE "C", t.item_key COLLATE "C" '
    "LIMIT :top"
)

_TOP_ARTISTS = text(
    f"WITH {_PERIOD_EVENTS}, {_EVENT_ARTISTS} "
    "SELECT name, count(*) AS play_count, sum(ms) AS total_ms "
    "FROM event_artists GROUP BY name "
    'ORDER BY play_count DESC, total_ms DESC, name COLLATE "C" '
    "LIMIT :top"
)

_COVERAGE_START = text("SELECT min(occurred_at) FROM unified_event WHERE user_id = :user_id")


@dataclass(frozen=True)
class Totals:
    play_count: int
    distinct_tracks: int
    distinct_artists: int
    total_ms: int
    events_without_duration: int


@dataclass(frozen=True)
class TopTrack:
    item_key: str
    title: str
    creators: list[str]
    artwork_url: str | None
    play_count: int
    total_ms: int


@dataclass(frozen=True)
class TopArtist:
    name: str
    play_count: int
    total_ms: int


@dataclass(frozen=True)
class Summary:
    totals: Totals
    top_tracks: list[TopTrack]
    top_artists: list[TopArtist]
    is_empty: bool
    empty_reason: EmptyReason | None
    coverage_start: datetime | None
    collection_gap_overlap: bool


def coverage_start(conn: Connection, user_id: UUID) -> datetime | None:
    """The user's earliest stored play, or None if there is none (R-52)."""
    value: datetime | None = conn.execute(_COVERAGE_START, {"user_id": user_id}).scalar_one()
    return value


def empty_reason(period: Period, coverage: datetime | None, *, overlaps_gap: bool) -> EmptyReason:
    """Why an empty period is empty (R-52, rows checked in order, first match wins).

    Only meaningful when the period is empty.
    """
    if coverage is None:
        return "no_data_yet"
    if period.end <= coverage:
        return "before_coverage"
    if overlaps_gap:
        return "collection_gap"
    return "no_plays"


def read_summary(
    conn: Connection, *, user_id: UUID, period: Period, top: int, now: datetime
) -> Summary:
    params = {"user_id": user_id, "start": period.start, "end": period.end, "top": top}
    row = conn.execute(_TOTALS, params).one()
    totals = Totals(
        play_count=row.play_count,
        distinct_tracks=row.distinct_tracks,
        distinct_artists=row.distinct_artists,
        total_ms=int(row.total_ms),
        events_without_duration=row.events_without_duration,
    )
    top_tracks = [
        TopTrack(
            item_key=r.item_key,
            title=r.title,
            creators=list(r.creators),
            artwork_url=r.artwork_url,
            play_count=r.play_count,
            total_ms=int(r.total_ms),
        )
        for r in conn.execute(_TOP_TRACKS, params)
    ]
    top_artists = [
        TopArtist(name=r.name, play_count=r.play_count, total_ms=int(r.total_ms))
        for r in conn.execute(_TOP_ARTISTS, params)
    ]
    coverage = coverage_start(conn, user_id)
    overlaps_gap = bool(overlapping_gaps(conn, user_id=user_id, period=period, now=now))
    is_empty = totals.play_count == 0
    return Summary(
        totals=totals,
        top_tracks=top_tracks,
        top_artists=top_artists,
        is_empty=is_empty,
        empty_reason=(
            empty_reason(period, coverage, overlaps_gap=overlaps_gap) if is_empty else None
        ),
        coverage_start=coverage,
        collection_gap_overlap=overlaps_gap,
    )
