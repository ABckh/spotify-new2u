""" "Did you already know these?": the first-two-weeks review.

In the first two weeks after connecting, most of what is "new to you" is only
new to the app. The review lists, in one place, every album, single and EP
first played in that time that is still new, so the user can mark the ones they knew
in a few taps (``POST /v1/new-to-you/not-new``) and keep the rest.

- The window runs from the earliest stored play (the imported recent plays
  come before ``collection_started_at``) to ``REVIEW_DAYS`` after collection
  started. The review is offered until ``REVIEW_OPEN_DAYS`` after that, then
  never again.
- Albums, singles and EPs only: the songs list of "New to you" is deprecated
  (WHO-115). An album mark already hides its songs.
- What is listed, and every rule behind it, is ``app.read.new_to_you`` over
  that window, so the review never disagrees with "New to you": a marked item
  ("Not new", the user's or the library baseline's) is left out.
- ``review_cleared_at`` is how far the user has reviewed: "Done" stores the
  ``through`` instant of the list they saw, and the next review starts there.
  Items first played after it come up next time; the rest stay new.

Every statement is scoped by ``user_id = :user_id`` from the session.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.read.new_to_you import NewAlbum, NewSingle, read_new_to_you
from app.read.periods import Period
from app.read.summary import coverage_start

REVIEW_DAYS: Final = timedelta(days=14)
REVIEW_OPEN_DAYS: Final = timedelta(days=14)


@dataclass(frozen=True)
class Review:
    open: bool
    window_start: datetime | None
    window_end: datetime | None
    # Send this back with "Done": the end of what this list covers.
    through: datetime | None
    albums: list[NewAlbum]
    album_count: int
    singles: list[NewSingle]
    single_count: int


_CLOSED = Review(False, None, None, None, [], 0, [], 0)

_CONNECTION = text(
    "SELECT c.collection_started_at, c.review_cleared_at, u.home_timezone "
    "FROM spotify_connection c JOIN app_user u ON u.id = c.user_id "
    "WHERE c.user_id = :user_id"
)


def window_end(collection_started_at: datetime) -> datetime:
    return collection_started_at + REVIEW_DAYS


def read_review(conn: Connection, *, user_id: UUID, now: datetime) -> Review:
    row = conn.execute(_CONNECTION, {"user_id": user_id}).one_or_none()
    if row is None:
        return _CLOSED
    end_of_window = window_end(row.collection_started_at)
    if now >= end_of_window + REVIEW_OPEN_DAYS:
        return _CLOSED
    coverage = coverage_start(conn, user_id)
    start_of_window = min(coverage, row.collection_started_at) if coverage else None
    if start_of_window is None:
        return Review(True, None, end_of_window, None, [], 0, [], 0)
    start = max(start_of_window, row.review_cleared_at or start_of_window)
    end = min(end_of_window, now)
    if start >= end:
        return Review(True, start_of_window, end_of_window, end, [], 0, [], 0)

    zone = ZoneInfo(row.home_timezone)
    period = Period(
        type="custom",
        offset=None,
        anchor=None,
        time_zone=row.home_timezone,
        start=start,
        end=end,
        local_start_date=start.astimezone(zone).date(),
        local_end_date=(end - timedelta(microseconds=1)).astimezone(zone).date(),
        is_partial=end < end_of_window,
    )
    new = read_new_to_you(
        conn, user_id=user_id, period=period, limit=None, now=now, with_tracks=False
    )
    return Review(
        open=True,
        window_start=start_of_window,
        window_end=end_of_window,
        through=end,
        albums=new.albums,
        album_count=new.album_count,
        singles=new.singles,
        single_count=new.single_count,
    )


def clear_review(conn: Connection, *, user_id: UUID, through: datetime, now: datetime) -> None:
    """Record that the user reviewed everything first played before ``through``.

    Never moves backwards, never past ``now`` or the end of the window, so a
    stale or forged ``through`` can only skip what the user was already shown.
    """
    conn.execute(
        text(
            "UPDATE spotify_connection SET review_cleared_at = GREATEST("
            "  review_cleared_at, LEAST(:through, :now, collection_started_at + :days)"
            ") WHERE user_id = :user_id"
        ),
        {"user_id": user_id, "through": through, "now": now, "days": REVIEW_DAYS},
    )
