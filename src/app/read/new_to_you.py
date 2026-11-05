""" "New to you" for one period: albums, singles and EPs, and tracks first played in it (S14, F24).

- An item is new in a period when its **earliest stored play for this user,
  over the whole history**, is inside the period (S14.2). Plays outside the
  period are read only to find that earliest play. Because of L1 that means
  "first played since collection started" (L7), which the app says next to the
  lists (V1) using ``coverage_start``.
- Albums are Spotify album IDs (``external_ids.spotify_album_id``); a play with
  no album ID has no album. The album rules are shared with every album list
  (``app.read.album_sql``). Tracks are ``item_key``, as everywhere else.
- An album is new in the period holding the play that took it past half of its
  tracks, over the whole history, and never while its track count is unknown
  (WHO-130, ``app.read.album_sql``). Its ``first_played_at`` is that play, and
  ``tracks_played_in_period`` still counts the period only;
  ``tracks_listened`` of ``total_tracks`` is the count the rule used (WHO-155).
- Each list is ordered by the first play's local date, newest first, then by
  name compared by code point, then by ID, so the order never depends on the
  planner (S14.6, R-60). Names shown are those of the most recent stored play (R-59).
- A single (Spotify's ``album_type`` ``single``, EPs included) is not a new
  album and is not in the album count (WHO-159); its tracks still are. A
  release whose type is not stored yet counts as an album.
- Singles and EPs are their own list, new by the same rule as albums
  (``ALBUM_IS_NEW``), each with ``release_type`` ``single`` (1 to 3 tracks) or
  ``ep`` (more). A single is left out when every song the user played from it
  is also on an album new in the same period: the song shows with its album
  only. Songs match by ISRC, or by title and first artist after NFC, trimming
  (R-31) and lower case, since a single and its album give the same song two track IDs.
- An album's artists are its own, not its tracks' (``app.read.album_artists``,
  WHO-128).
- ``count`` is the full count; ``limit`` cuts only the items (S14.7).
- Spotify plays only: Netflix and YouTube get their own lists in slice N (S14.18).
- An item the user marked "Not new" is left out, in every period, and so are
  the tracks of a marked album or single (WHO-123, ``app.read.not_new``).
  ``count`` and ``nothing_new`` are after that exclusion.
- No derived table: everything is read from ``unified_event`` and the user's
  marks in ``not_new_mark`` (S14.12).

Every statement is scoped by ``user_id = :user_id`` from the session.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Row, text

from app.read.album_sql import (
    ALBUM_EVENTS,
    ALBUM_IS_LISTED,
    ALBUM_IS_NEW,
    ALBUM_PLAYS,
    album_latest,
    release_kind,
    track_artwork,
)
from app.read.gaps import overlapping_gaps
from app.read.names import normalized_name
from app.read.not_new import NOT_NEW
from app.read.periods import Period
from app.read.summary import (
    EmptyReason,
    coverage_start,
    empty_reason,
)

ReleaseType = Literal["single", "ep"]

# S14.7: each list shows at most 10 entries until "Show all".
DEFAULT_LIMIT = 10

# R-60: first-play local date newest first, then the R-31 name by code point,
# then the ID, so the order is total. ``:zone`` is the resolved display zone.
_LOCAL_DATE = "(first_played_at AT TIME ZONE :zone)::date"


def _order_keys(name: str, item_id: str) -> str:
    return f'{_LOCAL_DATE} DESC, {normalized_name(name)} COLLATE "C", {item_id} COLLATE "C"'


def _order_by(name: str, item_id: str) -> str:
    return f"ORDER BY {_order_keys(name, item_id)} "


# R-59: the title and creators shown are those of the most recent stored play.
# ``total`` is computed before LIMIT, so it is the full count of new items.
# A song without an album is new too, so every play counts here, read as stored
# (``spotify_events``). Albums matter only for the "Not new" album marks, which
# read ``user_events`` from ALBUM_EVENTS: the plays without an album are never
# kept in a CTE. ``spotify_events`` is NOT MATERIALIZED so each use is its own
# scan, filtered before anything is kept.
# A song's first play is in the period when it was played in the period and
# never before it, so only the period's songs are looked up, each in
# ``unified_event_item_idx``.
_NEW_TRACKS = text(
    f"WITH {ALBUM_EVENTS}, {NOT_NEW}, "
    "spotify_events AS NOT MATERIALIZED ("
    "    SELECT id, item_key, title, creators, artwork_url, occurred_at FROM unified_event "
    "    WHERE user_id = :user_id AND source = 'spotify'"
    "), "
    "first_play AS ("
    "    SELECT item_key, min(occurred_at) AS first_played_at FROM spotify_events "
    "    WHERE occurred_at >= :start AND occurred_at < :end GROUP BY item_key"
    "), "
    "new AS ("
    "    SELECT * FROM first_play f WHERE NOT EXISTS ("
    "        SELECT 1 FROM spotify_events b "
    "        WHERE b.item_key = f.item_key AND b.occurred_at < :start"
    "    ) AND item_key NOT IN (SELECT item_key FROM not_new_tracks)"
    "), "
    "latest AS ("
    "    SELECT DISTINCT ON (item_key) item_key, title, creators FROM spotify_events "
    "    WHERE item_key IN (SELECT item_key FROM new) "
    "    ORDER BY item_key, occurred_at DESC, id DESC"
    "), "
    f"{track_artwork('spotify_events', 'SELECT item_key FROM new')} "
    "SELECT item_key, title, creators, track_artwork.artwork_url, first_played_at, "
    "       count(*) OVER () AS total "
    "FROM new JOIN latest USING (item_key) LEFT JOIN track_artwork USING (item_key) "
    f"{_order_by('title', 'item_key')}"
    "LIMIT :limit"
)

# Albums, and singles and EPs, in one statement: both lists read the same
# album plays, and finding those is most of the work. ``release_list`` says
# which list a row is in, and each list is ordered, counted and cut on its own.
#
# The new rule, the counts and the name and artists shown are the shared ones
# in ``app.read.album_sql``, so every album list agrees with this one.
#
# Singles and EPs: new by the album rule, and not a single whose every played
# song is on an album new in the period. ``event_songs`` gives each play of
# those releases its song keys: the ISRC, and the R-31 title and first artist
# in lower case.
# A song key in ``album_songs`` is on a new album; ``covered`` are the single's
# tracks with one. Only the candidate singles and the new albums are keyed, and
# only when there is a candidate.
_SONG_KEYS = (
    "CROSS JOIN LATERAL (VALUES ('isrc:' || e.isrc), "
    f"('name:' || lower({normalized_name('e.title')}) || chr(31) "
    f"|| lower({normalized_name('e.creators[1]')}))"
    ") AS song(key)"
)
_NEW_RELEASES = text(
    f"WITH {ALBUM_EVENTS}, {NOT_NEW}, {ALBUM_PLAYS}, "
    f"new_albums AS (SELECT * FROM album_plays WHERE {ALBUM_IS_NEW} AND {ALBUM_IS_LISTED}), "
    f"candidates AS (SELECT * FROM album_plays WHERE {ALBUM_IS_NEW} AND NOT ({ALBUM_IS_LISTED})), "
    "event_songs AS ("
    "    SELECT e.album_id, e.item_key, song.key FROM user_events e "
    f"    {_SONG_KEYS} "
    "    WHERE song.key IS NOT NULL AND EXISTS (SELECT 1 FROM candidates) "
    "    AND (e.album_id IN (SELECT album_id FROM candidates) "
    "    OR e.album_id IN (SELECT album_id FROM new_albums))"
    "), "
    "album_songs AS ("
    "    SELECT DISTINCT key FROM event_songs WHERE album_id IN (SELECT album_id FROM new_albums)"
    "), "
    "covered AS ("
    "    SELECT DISTINCT album_id, item_key FROM event_songs "
    "    WHERE album_id IN (SELECT album_id FROM candidates) "
    "    AND key IN (SELECT key FROM album_songs)"
    "), "
    "new_singles AS ("
    "    SELECT * FROM candidates c WHERE EXISTS ("
    "        SELECT 1 FROM user_events e WHERE e.album_id = c.album_id "
    "        AND NOT EXISTS (SELECT 1 FROM covered "
    "                        WHERE covered.album_id = e.album_id AND covered.item_key = e.item_key)"
    "    )"
    "), "
    "new AS ("
    "    SELECT *, 'albums' AS release_list FROM new_albums "
    "    UNION ALL SELECT *, 'singles' AS release_list FROM new_singles"
    "), "
    f"{album_latest('SELECT album_id FROM new')}, "
    "ranked AS ("
    "    SELECT release_list, album_id, name, artists, artwork_url, first_played_at, "
    "           tracks_played, tracks_listened, total_tracks, "
    f"           {release_kind('total_tracks')} AS release_type, "
    "           count(*) OVER (PARTITION BY release_list) AS total, "
    "           row_number() OVER ("
    f"               PARTITION BY release_list ORDER BY {_order_keys('name', 'album_id')}"
    "           ) AS n "
    "    FROM new JOIN album_latest USING (album_id)"
    ") "
    "SELECT * FROM ranked WHERE n <= COALESCE(CAST(:limit AS bigint), n) "
    "ORDER BY release_list, n"
)

# Any play at all in the period, any source: the summary's empty test (S14.15).
PLAYS_IN_PERIOD = text(
    "SELECT EXISTS (SELECT 1 FROM unified_event "
    "WHERE user_id = :user_id AND occurred_at >= :start AND occurred_at < :end)"
)


@dataclass(frozen=True)
class NewAlbum:
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: str | None
    first_played_at: datetime
    first_played_local_date: date
    tracks_played_in_period: int
    tracks_listened: int  # distinct tracks played before the period's end (WHO-155)
    total_tracks: int


@dataclass(frozen=True)
class NewSingle(NewAlbum):
    release_type: ReleaseType


@dataclass(frozen=True)
class NewTrack:
    item_key: str
    title: str
    creators: list[str]
    artwork_url: str | None
    first_played_at: datetime
    first_played_local_date: date


@dataclass(frozen=True)
class NewToYou:
    albums: list[NewAlbum]
    album_count: int
    singles: list[NewSingle]
    single_count: int
    tracks: list[NewTrack]
    track_count: int
    coverage_start: datetime | None
    coverage_start_local_date: date | None
    period_contains_coverage_start: bool  # V2 (S14.9)
    nothing_new: bool  # V3 (S14.14)
    is_empty: bool  # S14.15: same as the summary
    empty_reason: EmptyReason | None
    collection_gap_overlap: bool


def _album_fields(r: Row[Any], zone: ZoneInfo) -> dict[str, Any]:
    """The ``NewAlbum`` fields of an ``_NEW_RELEASES`` row."""
    return {
        "album_id": r.album_id,
        "name": r.name,
        "artists": list(r.artists),
        "artwork_url": r.artwork_url,
        "first_played_at": r.first_played_at,
        "first_played_local_date": r.first_played_at.astimezone(zone).date(),
        "tracks_played_in_period": r.tracks_played,
        "tracks_listened": r.tracks_listened,
        "total_tracks": r.total_tracks,
    }


def read_new_to_you(
    conn: Connection,
    *,
    user_id: UUID,
    period: Period,
    limit: int | None,
    now: datetime,
    with_tracks: bool = True,
) -> NewToYou:
    """``limit`` cuts each list to its first entries; None returns them all.

    ``with_tracks=False`` skips the songs query (the review shows no songs):
    ``tracks`` is then empty and ``nothing_new`` reads the albums and singles only.
    """
    zone = ZoneInfo(period.time_zone)
    coverage = coverage_start(conn, user_id)
    overlaps_gap = bool(overlapping_gaps(conn, user_id=user_id, period=period, now=now))
    params = {
        "user_id": user_id,
        "start": period.start,
        "end": period.end,
        "zone": period.time_zone,
        "limit": limit,
    }
    is_empty = not conn.execute(PLAYS_IN_PERIOD, params).scalar_one()

    albums: list[NewAlbum] = []
    singles: list[NewSingle] = []
    tracks: list[NewTrack] = []
    album_count = single_count = track_count = 0
    if not is_empty:
        for r in conn.execute(_NEW_RELEASES, params):
            if r.release_list == "albums":
                album_count = r.total
                albums.append(NewAlbum(**_album_fields(r, zone)))
            else:
                single_count = r.total
                singles.append(NewSingle(**_album_fields(r, zone), release_type=r.release_type))
        for r in conn.execute(_NEW_TRACKS, params) if with_tracks else ():
            track_count = r.total
            tracks.append(
                NewTrack(
                    item_key=r.item_key,
                    title=r.title,
                    creators=list(r.creators),
                    artwork_url=r.artwork_url,
                    first_played_at=r.first_played_at,
                    first_played_local_date=r.first_played_at.astimezone(zone).date(),
                )
            )

    return NewToYou(
        albums=albums,
        album_count=album_count,
        singles=singles,
        single_count=single_count,
        tracks=tracks,
        track_count=track_count,
        coverage_start=coverage,
        coverage_start_local_date=coverage.astimezone(zone).date() if coverage else None,
        period_contains_coverage_start=(
            coverage is not None and period.start <= coverage < period.end
        ),
        nothing_new=not is_empty and album_count == single_count == track_count == 0,
        is_empty=is_empty,
        empty_reason=(
            empty_reason(period, coverage, overlaps_gap=overlaps_gap) if is_empty else None
        ),
        collection_gap_overlap=overlaps_gap,
    )
