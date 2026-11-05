"""Every album the user played in one period, new and old (WHO-133, WHO-129).

- An album is a Spotify album ID (``external_ids.spotify_album_id``), as in
  ``app.read.new_to_you``. It is listed when more than half of its tracks
  were played in the period (``ALBUM_LISTENED_IN_PERIOD``, WHO-160): 10 tracks
  need 6 distinct ones in the period. Plays before or after the period do not
  count, and an album with an unknown track count is not listed.
- ``play_count`` and ``total_ms`` count the album's plays in the period,
  ``tracks_played_in_period`` its distinct tracks (``item_key``) in the period,
  of ``total_tracks``.
- ``is_new``, ``name`` and ``artists`` use the same SQL as the "New to you"
  albums (``app.read.album_sql``), and a "Not new" mark gives ``is_new`` false.
  "New to you" counts tracks over the whole history, this list only in the
  period, so a new album whose tracks were split across periods (3 of 10 in
  September, 3 in October) is new in October but not in October's list here.
- Order: ``play_count`` descending, then ``total_ms`` descending (WHO-144),
  then the name after NFC and trimming by code point, then
  the album ID, so the order never depends on the planner (R-60).
- ``count`` is the full count; ``limit`` cuts only the items, as in "New to you".
- A single (Spotify's ``album_type`` ``single``, EPs included) is left out of
  the list and ``count`` (WHO-159, ``ALBUM_IS_LISTED``). A release whose type is
  not stored yet is listed.
- ``read_albums(..., singles=True)`` is the same list of the singles and EPs
  only, by the same rules (listened: more than half of the tracks played in the
  period), each with ``release_type`` ``single`` or ``ep`` (``release_kind``).
  Every album of the album list has ``release_type`` ``album``.
- Spotify plays only, as "New to you" (S14.18). ``is_empty`` is the summary's
  test: no play at all in the period, any source (S14.15).

Every statement is scoped by ``user_id = :user_id`` from the session.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, TextClause, text

from app.read.album_sql import (
    ALBUM_EVENTS,
    ALBUM_IS_LISTED,
    ALBUM_IS_NEW,
    ALBUM_LISTENED_IN_PERIOD,
    ALBUM_PLAYS,
    album_latest,
    release_kind,
)
from app.read.gaps import overlapping_gaps
from app.read.names import normalized_name
from app.read.new_to_you import PLAYS_IN_PERIOD
from app.read.not_new import NOT_NEW
from app.read.periods import Period
from app.read.ratings import ReleaseType
from app.read.summary import EmptyReason, coverage_start, empty_reason


def _played(listed: str) -> TextClause:
    """The releases listened in the period that ``listed`` (SQL over an
    ``album_plays`` row) keeps."""
    return text(
        f"WITH {ALBUM_EVENTS}, {NOT_NEW}, {ALBUM_PLAYS}, "
        f"{album_latest(f'SELECT album_id FROM album_plays WHERE {ALBUM_LISTENED_IN_PERIOD}')} "
        "SELECT album_id, name, artists, artwork_url, play_count, tracks_played, total_tracks, "
        f"       total_ms, {ALBUM_IS_NEW} AS is_new, "
        "       CASE WHEN album_type = 'single' "
        f"            THEN {release_kind('total_tracks')} ELSE 'album' END AS release_type, "
        "       count(*) OVER () AS total "
        "FROM album_plays JOIN album_latest USING (album_id) "
        f"WHERE {ALBUM_LISTENED_IN_PERIOD} AND {listed} "
        "ORDER BY play_count DESC, total_ms DESC, "
        f'{normalized_name("name")} COLLATE "C", album_id COLLATE "C" '
        "LIMIT :limit"
    )


# An album is in the list when more than half of its tracks were played in the
# period, ``ALBUM_LISTENED_IN_PERIOD``, and it is not a single
# (``ALBUM_IS_LISTED``, WHO-159); a single or EP is in the singles list by the
# same rule.
_ALBUMS = _played(ALBUM_IS_LISTED)
_SINGLES = _played(f"NOT ({ALBUM_IS_LISTED})")


@dataclass(frozen=True)
class PlayedAlbum:
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: str | None
    play_count: int
    tracks_played_in_period: int
    total_tracks: int
    total_ms: int
    is_new: bool
    release_type: ReleaseType


@dataclass(frozen=True)
class PlayedAlbums:
    albums: list[PlayedAlbum]
    album_count: int
    is_empty: bool
    empty_reason: EmptyReason | None
    coverage_start: datetime | None
    collection_gap_overlap: bool


def read_albums(
    conn: Connection,
    *,
    user_id: UUID,
    period: Period,
    limit: int | None,
    now: datetime,
    singles: bool = False,
) -> PlayedAlbums:
    """``limit`` cuts the list to its first entries; None returns them all.

    ``singles``: the singles and EPs instead of the albums.
    """
    coverage = coverage_start(conn, user_id)
    overlaps_gap = bool(overlapping_gaps(conn, user_id=user_id, period=period, now=now))
    params = {"user_id": user_id, "start": period.start, "end": period.end, "limit": limit}
    is_empty = not conn.execute(PLAYS_IN_PERIOD, params).scalar_one()

    albums: list[PlayedAlbum] = []
    album_count = 0
    if not is_empty:
        for r in conn.execute(_SINGLES if singles else _ALBUMS, params):
            album_count = r.total
            albums.append(
                PlayedAlbum(
                    album_id=r.album_id,
                    name=r.name,
                    artists=list(r.artists),
                    artwork_url=r.artwork_url,
                    play_count=r.play_count,
                    tracks_played_in_period=r.tracks_played,
                    total_tracks=r.total_tracks,
                    total_ms=r.total_ms,
                    is_new=r.is_new,
                    release_type=r.release_type,
                )
            )

    return PlayedAlbums(
        albums=albums,
        album_count=album_count,
        is_empty=is_empty,
        empty_reason=(
            empty_reason(period, coverage, overlaps_gap=overlaps_gap) if is_empty else None
        ),
        coverage_start=coverage,
        collection_gap_overlap=overlaps_gap,
    )
