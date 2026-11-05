"""The read API the app consumes, plus export and delete (WHO-7).

The full contract for the app is the ``api-contract`` document on WHO-7.

- ``GET /v1/timeline``, ``GET /v1/summary``: one period, resolved by
  ``app.read.periods`` from ``period`` and ``anchor`` | ``offset`` | ``from``/``to``.
  Collection gaps (WHO-44, D-7) come as ``gaps`` rows on the timeline and as
  ``collection_gap_overlap`` on the summary, never as events or counts.
- ``GET /v1/new-to-you``: the albums, singles and EPs, and tracks first played
  in one period (S14, F24), resolved exactly as the summary's period. ``tracks``
  is deprecated (WHO-115) and goes in the next release. ``singles`` items are
  album items with ``release_type`` ``single`` or ``ep``.
- ``GET /v1/new-to-you/albums/{album_id}/tracks``: the tracks of one album the
  user played in that same period (WHO-116). ``GET /v1/albums/{album_id}/tracks``
  is the same route under the album list's path (WHO-133).
- ``GET /v1/albums``: every album listened in one period, new and old, with
  ``is_new`` by the same rule as "New to you" (WHO-133). Listened means more
  than half of its tracks played in the period (WHO-160). Same period query and
  ``all`` as ``/v1/new-to-you``.
  The list ranks by plays, then listening time.
- ``GET /v1/singles``: the same list and answer for the singles and EPs, each
  with ``release_type`` ``single`` or ``ep`` (every ``/v1/albums`` item has
  ``album``). Listened by the same rule: more than half of the tracks.
- ``PUT`` / ``DELETE /v1/new-to-you/not-new/{kind}/{item_id}``: mark an album
  (single and EP included) or track "Not new", or remove the mark (Undo).
  ``204``, idempotent (WHO-124). Undo also removes a mark the library baseline made.
- ``POST /v1/new-to-you/not-new``: mark up to ``MAX_BATCH_MARKS`` items at once
  (the review's "Mark as known"). ``200 {"marked", "not_played"}``; an item with
  no play is skipped, not an error, so one stale row never fails the rest.
- ``GET /v1/me/review``: "Did you already know these?", the albums, singles and
  EPs first played in the first two weeks that are still new (``app.read.review``).
  ``POST /v1/me/review/done {"through"}``: everything before ``through`` was
  reviewed; the rest stays new. ``204``.
- ``PUT /v1/albums/{album_id}/rating {"rating": 1..10}`` / ``DELETE``: the
  user's own rating of an album, single or EP they played. ``204``, idempotent;
  ``404`` for one with no play. Every album item (``/v1/new-to-you``, singles
  included, ``/v1/albums``, album tracks, the review) carries ``rating``: the
  user's, or ``null``.
- ``PUT /v1/albums/{album_id}/comment {"comment": "..."}`` / ``DELETE``: the
  user's own words about an album, single or EP they played: 1 to 2000
  characters, not blank, no NUL. ``204``, idempotent; ``404`` for one with no
  play. Independent of the rating: either can be set or cleared alone. Every
  album item carries ``comment``: the user's, or ``null``.
- ``PUT /v1/tracks/{item_key}/comment {"comment": "..."}`` / ``DELETE``: the
  user's own words about a track they played, by the album comment's rules
  (``404`` for one with no play). Every play of the timeline, every track of an
  album screen and every song of search carries ``comment``: the user's, or
  ``null``.
- ``GET /v1/ratings/albums``: everything the user rated, all time, best first,
  each with ``release_type`` ``album``, ``single`` or ``ep`` and its ``comment``
  (``app.read.ratings``). ``commented=true`` also lists, after them, everything
  with only a comment (``rating`` ``null``); without it, ``rating`` is never
  ``null``, as older app builds expect.
- ``GET /v1/search?q=...``: the user's albums (singles and EPs included) and
  songs over the whole history whose name, artists or, for an album, the user's
  comment hold every word of ``q`` (``app.read.search``). Each list is most
  played first, at most ``search.LIMIT`` items, with ``count``. ``q`` is 1 to
  200 characters and at most 10 words, or ``422``.
- ``GET /v1/export``: everything we hold about the caller, streamed JSON.
- ``DELETE /v1/me``: everything we hold about the caller, gone. ``204``.
- ``GET /v1/config``: the server settings the app shows (poll interval, S1.2a).

Every song and album item carries ``artwork_url`` (WHO-149): its album's cover,
a ``https://i.scdn.co/image/...`` URL, or ``null`` when there is none. A stored value of
any other form is sent as ``null``.

Singles (Spotify's ``album_type`` ``single``, EPs included) are in no album list
and no album ``count``: not in "New to you" albums, not in ``/v1/albums``
(WHO-159, WHO-162). They have their own "New to you" list, ``singles``. Their
tracks still count. A release whose type is not stored yet is listed as an album.

There is no artist list: ``/v1/artists`` and the artist lists of "New to you"
and the review were removed (the code is on the ``archive/artists`` branch).

The user id only ever comes from the session (``current_user``), and every SQL
statement filters on it. No parameter names a user. A failed session check is
``401`` before any query runs, and a parameter that cannot be read exactly is
``422`` with ``{"detail": {"code", "message"}}``, never a guess.

Every ``/v1/...`` response, errors included, carries ``Cache-Control: no-store``
and ``Pragma: no-cache`` (``app.api.no_store``, WHO-84).
"""

import re
from collections.abc import Generator, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, BeforeValidator, PlainSerializer, StrictInt
from sqlalchemy import Connection, Engine, text

from app.api.auth import EngineDep, NowDep, UserDep
from app.api.rate_limit import Busy, check_per_user, export_streams
from app.core.config import get_settings
from app.privacy.delete import delete_all_user_data
from app.read import album_tracks as album_queries
from app.read import albums as played_queries
from app.read import new_to_you as new_queries
from app.read import not_new
from app.read import ratings as rating_queries
from app.read import review as review_queries
from app.read import search as search_queries
from app.read import summary as summary_queries
from app.read.export import stream_export
from app.read.gaps import GapState, gap_rows, overlapping_gaps, rows_on_page
from app.read.periods import Period, PeriodError, PeriodQuery, PeriodType, resolve_period
from app.read.timeline import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, read_timeline
from app.spotify.artwork import safe_artwork_url

router = APIRouter(prefix="/v1", tags=["read"])


def _utc_millis(value: datetime) -> str:
    """Every instant on the wire: UTC, exactly three fractional digits, ``Z``."""
    utc = value.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


UtcInstant = Annotated[datetime, PlainSerializer(_utc_millis, return_type=str)]

# A cover URL on the wire: a Spotify image URL or null, never anything else.
ArtworkUrl = Annotated[str | None, BeforeValidator(safe_artwork_url)]


# --- wire models ------------------------------------------------------------


class PeriodOut(BaseModel):
    type: PeriodType
    offset: int | None
    anchor: date | None
    time_zone: str
    week_start: Literal["monday"]
    start: UtcInstant
    end: UtcInstant
    local_start_date: date
    local_end_date: date
    is_partial: bool

    @classmethod
    def of(cls, period: Period) -> "PeriodOut":
        return cls(
            type=period.type,
            offset=period.offset,
            anchor=period.anchor,
            time_zone=period.time_zone,
            week_start="monday",
            start=period.start,
            end=period.end,
            local_start_date=period.local_start_date,
            local_end_date=period.local_end_date,
            is_partial=period.is_partial,
        )


class EventOut(BaseModel):
    id: UUID
    source: str
    kind: str
    occurred_at: UtcInstant
    occurred_at_precision: Literal["second", "day"]
    local_date: date
    duration_ms: int | None
    played_ms: int | None
    title: str
    creators: list[str]
    collection: str | None
    artwork_url: ArtworkUrl
    item_key: str
    # The user's own comment on the track, or null.
    comment: str | None = None


class GapRowOut(BaseModel):
    """One collection-gap row under one local day: G1 if closed, G2 if open (S2.10d).

    Merge into the newest-first list by ``sort_at``: after every event with
    ``occurred_at >= sort_at``, before every event with ``occurred_at < sort_at``.
    """

    source: str
    state: GapState
    started_at: UtcInstant
    ended_at: UtcInstant | None
    local_date: date
    sort_at: UtcInstant


class TimelineResponse(BaseModel):
    period: PeriodOut
    events: list[EventOut]
    gaps: list[GapRowOut]
    next_cursor: str | None
    is_empty: bool
    empty_reason: summary_queries.EmptyReason | None
    coverage_start: UtcInstant | None


class TotalsOut(BaseModel):
    play_count: int
    distinct_tracks: int
    distinct_artists: int
    total_ms: int
    events_without_duration: int


class TopTrackOut(BaseModel):
    item_key: str
    title: str
    creators: list[str]
    artwork_url: ArtworkUrl
    play_count: int
    total_ms: int


class TopArtistOut(BaseModel):
    name: str
    play_count: int
    total_ms: int


class SummaryResponse(BaseModel):
    period: PeriodOut
    totals: TotalsOut
    top_tracks: list[TopTrackOut]
    top_artists: list[TopArtistOut]
    is_empty: bool
    empty_reason: summary_queries.EmptyReason | None
    coverage_start: UtcInstant | None
    collection_gap_overlap: bool


class NewAlbumOut(BaseModel):
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: ArtworkUrl
    # The play that took the album past half of its tracks (WHO-130).
    first_played_at: UtcInstant
    first_played_local_date: date
    tracks_played_in_period: int  # distinct tracks played in the period only
    # The rule's count (WHO-155): distinct tracks played over the whole history
    # up to the period's end, of the album's track count. Always more than half.
    tracks_listened: int
    total_tracks: int
    # The user's own rating, 1 to 10, or null when not rated.
    rating: int | None = None
    # The user's own comment, or null. Independent of the rating.
    comment: str | None = None


class NewSingleOut(NewAlbumOut):
    # 1 to 3 tracks is a single, more an EP (Spotify calls both "single").
    release_type: new_queries.ReleaseType


class NewTrackOut(BaseModel):
    item_key: str
    title: str
    creators: list[str]
    artwork_url: ArtworkUrl
    first_played_at: UtcInstant
    first_played_local_date: date


class NewAlbumsOut(BaseModel):
    count: int  # every new album in the period, however many ``items`` holds
    items: list[NewAlbumOut]


class NewSinglesOut(BaseModel):
    count: int  # every new single and EP in the period, however many ``items`` holds
    items: list[NewSingleOut]


class NewTracksOut(BaseModel):
    count: int
    items: list[NewTrackOut]


class NewToYouResponse(BaseModel):
    period: PeriodOut
    albums: NewAlbumsOut
    singles: NewSinglesOut
    tracks: NewTracksOut  # deprecated (WHO-115): the app lists tracks per album now
    coverage_start: UtcInstant | None
    coverage_start_local_date: date | None
    period_contains_coverage_start: bool
    nothing_new: bool
    is_empty: bool
    empty_reason: summary_queries.EmptyReason | None
    collection_gap_overlap: bool


class AlbumTrackOut(BaseModel):
    item_key: str
    title: str
    creators: list[str]
    artwork_url: ArtworkUrl
    play_count: int
    first_played_at: UtcInstant
    first_played_local_date: date
    is_new: bool
    # The user's own comment on the track, or null.
    comment: str | None = None


class AlbumOut(BaseModel):
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: ArtworkUrl
    rating: int | None = None
    comment: str | None = None


class AlbumTracksResponse(BaseModel):
    period: PeriodOut
    album: AlbumOut
    tracks: list[AlbumTrackOut]


class PlayedAlbumOut(BaseModel):
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: ArtworkUrl
    play_count: int
    # Distinct tracks played in the period, of the album's track count: always
    # more than half of it (WHO-160).
    tracks_played_in_period: int
    total_tracks: int
    total_ms: int
    is_new: bool
    # ``album`` in ``/v1/albums``; ``single`` or ``ep`` in ``/v1/singles``.
    release_type: rating_queries.ReleaseType
    rating: int | None = None
    comment: str | None = None


class PlayedAlbumsOut(BaseModel):
    count: int  # every album listened in the period, however many ``items`` holds
    items: list[PlayedAlbumOut]


class AlbumsResponse(BaseModel):
    period: PeriodOut
    albums: PlayedAlbumsOut
    is_empty: bool
    empty_reason: summary_queries.EmptyReason | None
    coverage_start: UtcInstant | None
    collection_gap_overlap: bool


class FoundAlbumOut(BaseModel):
    album_id: str
    # null only for an album found by its comment whose plays are gone.
    name: str | None
    artists: list[str]
    artwork_url: ArtworkUrl
    release_type: rating_queries.ReleaseType
    rating: int | None
    comment: str | None
    # Plays over the whole history: 0 and null for an album without plays.
    play_count: int
    last_played_at: UtcInstant | None
    last_played_local_date: date | None


class FoundTrackOut(BaseModel):
    item_key: str
    title: str
    creators: list[str]
    album: str | None  # the album name stored with the most recent play
    album_id: str | None  # the album its plays resolve to, if any
    artwork_url: ArtworkUrl
    comment: str | None  # the user's own comment on the track
    play_count: int  # over the whole history
    first_played_at: UtcInstant
    last_played_at: UtcInstant
    last_played_local_date: date


class FoundAlbumsOut(BaseModel):
    count: int  # every match, however many ``items`` holds
    items: list[FoundAlbumOut]


class FoundTracksOut(BaseModel):
    count: int  # every match, however many ``items`` holds
    items: list[FoundTrackOut]


class SearchResponse(BaseModel):
    albums: FoundAlbumsOut
    tracks: FoundTracksOut


class ConfigResponse(BaseModel):
    poll_interval_seconds: int


# --- shared helpers ---------------------------------------------------------


def _unprocessable(code: str, message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"code": code, "message": message}
    )


def _bounded_int(value: str | None, name: str, default: int, maximum: int) -> int:
    """A whole number from 1 to ``maximum``, or 422 ``invalid_parameter``."""
    if value is None:
        return default
    # Bound the length before int(): past 4300 digits int() raises, which would be a 500.
    if not re.fullmatch(f"[0-9]{{1,{len(str(maximum))}}}", value) or not 1 <= int(value) <= maximum:
        raise _unprocessable("invalid_parameter", f"{name} is a whole number from 1 to {maximum}.")
    return int(value)


def _show_all(value: str | None) -> bool:
    """``all=true`` or ``all=false`` exactly, or 422 ``invalid_parameter``."""
    return _flag(value, "all")


def _flag(value: str | None, name: str) -> bool:
    """``true`` or ``false`` exactly (absent is false), or 422 ``invalid_parameter``."""
    if value is None or value == "false":
        return False
    if value == "true":
        return True
    raise _unprocessable("invalid_parameter", f"{name} is true or false.")


def _user_time_zone(conn: Connection, user_id: UUID) -> str:
    zone: str | None = conn.execute(
        text("SELECT home_timezone FROM app_user WHERE id = :user_id"), {"user_id": user_id}
    ).scalar_one_or_none()
    if zone is None:  # the account went away after the session check
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return zone


def _resolve(conn: Connection, user_id: UUID, query: PeriodQuery, now: datetime) -> Period:
    try:
        return resolve_period(query, _user_time_zone(conn, user_id), now)
    except PeriodError as exc:
        raise _unprocessable(exc.code, exc.message) from None


def _snapshot(engine: Engine) -> Connection:
    """A connection whose queries all see one moment, even while the poller writes."""
    return engine.connect().execution_options(isolation_level="REPEATABLE READ")


# Plain strings, read exactly by the period resolver, so FastAPI never answers
# with its own error shape and nothing is coerced before we look at it.
PeriodParam = Annotated[str | None, Query()]
AnchorParam = Annotated[str | None, Query()]
OffsetParam = Annotated[str | None, Query()]
FromParam = Annotated[str | None, Query(alias="from")]
ToParam = Annotated[str | None, Query()]


# --- routes -----------------------------------------------------------------


@router.get("/timeline", response_model=TimelineResponse)
def timeline(
    user_id: UserDep,
    engine: EngineDep,
    now: NowDep,
    period: PeriodParam = None,
    anchor: AnchorParam = None,
    offset: OffsetParam = None,
    from_: FromParam = None,
    to: ToParam = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[str | None, Query()] = None,
) -> TimelineResponse:
    check_per_user("timeline", user_id, get_settings().read_rate_limit_per_minute)
    page_size = _bounded_int(limit, "limit", DEFAULT_LIMIT, MAX_LIMIT)
    query = PeriodQuery(period=period, anchor=anchor, offset=offset, from_=from_, to=to)
    with _snapshot(engine) as conn, conn.begin():
        resolved = _resolve(conn, user_id, query, now)
        try:
            page = read_timeline(
                conn, user_id=user_id, period=resolved, cursor=cursor, limit=page_size
            )
        except InvalidCursor:
            raise _unprocessable(
                "invalid_cursor", "This cursor is not from this period. Start from the first page."
            ) from None
        coverage = summary_queries.coverage_start(conn, user_id)
        gaps = overlapping_gaps(conn, user_id=user_id, period=resolved, now=now)
        track_words = rating_queries.track_comments_for(
            conn, user_id=user_id, item_keys=(e.item_key for e in page.events)
        )
    # Only a first page can show the period is empty; a cursor means it is not.
    is_empty = cursor is None and not page.events
    last = page.events[-1].occurred_at if page.next_cursor is not None else None
    rows = rows_on_page(gap_rows(gaps, resolved, now), after=page.after, last=last)
    return TimelineResponse(
        period=PeriodOut.of(resolved),
        events=[
            EventOut.model_validate(event, from_attributes=True).model_copy(
                update={"comment": track_words.get(event.item_key)}
            )
            for event in page.events
        ],
        gaps=[GapRowOut.model_validate(row, from_attributes=True) for row in rows],
        next_cursor=page.next_cursor,
        is_empty=is_empty,
        empty_reason=(
            summary_queries.empty_reason(resolved, coverage, overlaps_gap=bool(gaps))
            if is_empty
            else None
        ),
        coverage_start=coverage,
    )


@router.get("/summary", response_model=SummaryResponse)
def summary(
    user_id: UserDep,
    engine: EngineDep,
    now: NowDep,
    period: PeriodParam = None,
    anchor: AnchorParam = None,
    offset: OffsetParam = None,
    from_: FromParam = None,
    to: ToParam = None,
    top: Annotated[str | None, Query()] = None,
) -> SummaryResponse:
    check_per_user("summary", user_id, get_settings().read_rate_limit_per_minute)
    top_n = _bounded_int(top, "top", summary_queries.DEFAULT_TOP, summary_queries.MAX_TOP)
    query = PeriodQuery(period=period, anchor=anchor, offset=offset, from_=from_, to=to)
    with _snapshot(engine) as conn, conn.begin():
        resolved = _resolve(conn, user_id, query, now)
        result = summary_queries.read_summary(
            conn, user_id=user_id, period=resolved, top=top_n, now=now
        )
    return SummaryResponse(
        period=PeriodOut.of(resolved),
        totals=TotalsOut.model_validate(result.totals, from_attributes=True),
        top_tracks=[TopTrackOut.model_validate(t, from_attributes=True) for t in result.top_tracks],
        top_artists=[
            TopArtistOut.model_validate(a, from_attributes=True) for a in result.top_artists
        ],
        is_empty=result.is_empty,
        empty_reason=result.empty_reason,
        coverage_start=result.coverage_start,
        collection_gap_overlap=result.collection_gap_overlap,
    )


@router.get("/new-to-you", response_model=NewToYouResponse)
def new_to_you(
    user_id: UserDep,
    engine: EngineDep,
    now: NowDep,
    period: PeriodParam = None,
    anchor: AnchorParam = None,
    offset: OffsetParam = None,
    from_: FromParam = None,
    to: ToParam = None,
    all_: Annotated[str | None, Query(alias="all")] = None,
) -> NewToYouResponse:
    check_per_user("new_to_you", user_id, get_settings().read_rate_limit_per_minute)
    limit = None if _show_all(all_) else new_queries.DEFAULT_LIMIT
    query = PeriodQuery(period=period, anchor=anchor, offset=offset, from_=from_, to=to)
    with _snapshot(engine) as conn, conn.begin():
        resolved = _resolve(conn, user_id, query, now)
        result = new_queries.read_new_to_you(
            conn, user_id=user_id, period=resolved, limit=limit, now=now
        )
        own = _own_words(conn, user_id, [*result.albums, *result.singles])
    return NewToYouResponse(
        period=PeriodOut.of(resolved),
        albums=NewAlbumsOut(
            count=result.album_count,
            items=[_rated(NewAlbumOut, a, own) for a in result.albums],
        ),
        singles=NewSinglesOut(
            count=result.single_count,
            items=[_rated(NewSingleOut, a, own) for a in result.singles],
        ),
        tracks=NewTracksOut(
            count=result.track_count,
            items=[NewTrackOut.model_validate(t, from_attributes=True) for t in result.tracks],
        ),
        coverage_start=result.coverage_start,
        coverage_start_local_date=result.coverage_start_local_date,
        period_contains_coverage_start=result.period_contains_coverage_start,
        nothing_new=result.nothing_new,
        is_empty=result.is_empty,
        empty_reason=result.empty_reason,
        collection_gap_overlap=result.collection_gap_overlap,
    )


@router.get("/albums", response_model=AlbumsResponse)
def albums(
    user_id: UserDep,
    engine: EngineDep,
    now: NowDep,
    period: PeriodParam = None,
    anchor: AnchorParam = None,
    offset: OffsetParam = None,
    from_: FromParam = None,
    to: ToParam = None,
    all_: Annotated[str | None, Query(alias="all")] = None,
) -> AlbumsResponse:
    check_per_user("albums", user_id, get_settings().read_rate_limit_per_minute)
    query = PeriodQuery(period=period, anchor=anchor, offset=offset, from_=from_, to=to)
    return _played(user_id, engine, now, query, all_, singles=False)


@router.get("/singles", response_model=AlbumsResponse)
def singles(
    user_id: UserDep,
    engine: EngineDep,
    now: NowDep,
    period: PeriodParam = None,
    anchor: AnchorParam = None,
    offset: OffsetParam = None,
    from_: FromParam = None,
    to: ToParam = None,
    all_: Annotated[str | None, Query(alias="all")] = None,
) -> AlbumsResponse:
    check_per_user("singles", user_id, get_settings().read_rate_limit_per_minute)
    query = PeriodQuery(period=period, anchor=anchor, offset=offset, from_=from_, to=to)
    return _played(user_id, engine, now, query, all_, singles=True)


def _played(
    user_id: UUID,
    engine: Engine,
    now: datetime,
    query: PeriodQuery,
    all_: str | None,
    *,
    singles: bool,
) -> AlbumsResponse:
    """``/v1/albums``, or with ``singles`` ``/v1/singles``: the same answer."""
    limit = None if _show_all(all_) else new_queries.DEFAULT_LIMIT
    with _snapshot(engine) as conn, conn.begin():
        resolved = _resolve(conn, user_id, query, now)
        result = played_queries.read_albums(
            conn, user_id=user_id, period=resolved, limit=limit, now=now, singles=singles
        )
        own = _own_words(conn, user_id, result.albums)
    return AlbumsResponse(
        period=PeriodOut.of(resolved),
        albums=PlayedAlbumsOut(
            count=result.album_count,
            items=[_rated(PlayedAlbumOut, a, own) for a in result.albums],
        ),
        is_empty=result.is_empty,
        empty_reason=result.empty_reason,
        coverage_start=result.coverage_start,
        collection_gap_overlap=result.collection_gap_overlap,
    )


@router.get("/new-to-you/albums/{album_id}/tracks", response_model=AlbumTracksResponse)
def album_tracks(
    user_id: UserDep,
    engine: EngineDep,
    now: NowDep,
    album_id: str,
    period: PeriodParam = None,
    anchor: AnchorParam = None,
    offset: OffsetParam = None,
    from_: FromParam = None,
    to: ToParam = None,
) -> AlbumTracksResponse:
    check_per_user("album_tracks", user_id, get_settings().read_rate_limit_per_minute)
    if not not_new.SPOTIFY_ID.fullmatch(album_id):
        raise _unprocessable("invalid_parameter", "album_id is a Spotify album ID.")
    query = PeriodQuery(period=period, anchor=anchor, offset=offset, from_=from_, to=to)
    with _snapshot(engine) as conn, conn.begin():
        resolved = _resolve(conn, user_id, query, now)
        result = album_queries.read_album_tracks(
            conn, user_id=user_id, album_id=album_id, period=resolved
        )
        own = _own_words(conn, user_id, [result])
        track_words = rating_queries.track_comments_for(
            conn, user_id=user_id, item_keys=(t.item_key for t in result.tracks)
        )
    return AlbumTracksResponse(
        period=PeriodOut.of(resolved),
        album=_rated(AlbumOut, result, own),
        tracks=[
            AlbumTrackOut.model_validate(t, from_attributes=True).model_copy(
                update={"comment": track_words.get(t.item_key)}
            )
            for t in result.tracks
        ],
    )


@router.get("/albums/{album_id}/tracks", response_model=AlbumTracksResponse)
def played_album_tracks(
    user_id: UserDep,
    engine: EngineDep,
    now: NowDep,
    album_id: str,
    period: PeriodParam = None,
    anchor: AnchorParam = None,
    offset: OffsetParam = None,
    from_: FromParam = None,
    to: ToParam = None,
) -> AlbumTracksResponse:
    """The same as ``/v1/new-to-you/albums/{album_id}/tracks``, for an album of any age."""
    return album_tracks(user_id, engine, now, album_id, period, anchor, offset, from_, to)


def _mark_target(kind: str, item_id: str) -> tuple[not_new.Kind, str]:
    """The checked (kind, item_id) of a mark route, or 422 ``invalid_parameter``."""
    for known in not_new.MARKABLE:
        if kind == known:
            if not not_new.is_valid_item_id(known, item_id):
                raise _unprocessable(
                    "invalid_parameter", "item_id is a Spotify album ID or a track item_key."
                )
            return known, item_id
    raise _unprocessable("invalid_parameter", "kind is album or track.")


# ``:path`` so a malformed ID with "/" in it still reaches the route and gets a 422.
_MARK_PATH = "/new-to-you/not-new/{kind}/{item_id:path}"


@router.put(_MARK_PATH, status_code=204)
def mark_not_new(user_id: UserDep, engine: EngineDep, kind: str, item_id: str) -> Response:
    check_per_user("not_new", user_id, get_settings().read_rate_limit_per_minute)
    checked_kind, checked_id = _mark_target(kind, item_id)
    with engine.begin() as conn:
        if not not_new.has_played(conn, user_id=user_id, kind=checked_kind, item_id=checked_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "You have no play of this item."},
            )
        not_new.add_mark(conn, user_id=user_id, kind=checked_kind, item_id=checked_id)
    return Response(status_code=204)


MAX_BATCH_MARKS = 200


class MarkIn(BaseModel):
    kind: str
    item_id: str


class BatchMarksIn(BaseModel):
    items: list[MarkIn]


class BatchMarksOut(BaseModel):
    marked: int
    not_played: int


@router.post("/new-to-you/not-new", response_model=BatchMarksOut)
def mark_not_new_batch(user_id: UserDep, engine: EngineDep, body: BatchMarksIn) -> BatchMarksOut:
    check_per_user("not_new", user_id, get_settings().read_rate_limit_per_minute)
    if not 0 < len(body.items) <= MAX_BATCH_MARKS:
        raise _unprocessable("invalid_parameter", f"items holds 1 to {MAX_BATCH_MARKS} marks.")
    # Every item is checked before anything is written: one bad item fails all.
    targets = list(dict.fromkeys(_mark_target(m.kind, m.item_id) for m in body.items))
    marked = 0
    with engine.begin() as conn:
        for kind, item_id in targets:
            if not_new.has_played(conn, user_id=user_id, kind=kind, item_id=item_id):
                not_new.add_mark(conn, user_id=user_id, kind=kind, item_id=item_id)
                marked += 1
    return BatchMarksOut(marked=marked, not_played=len(targets) - marked)


@router.delete(_MARK_PATH, status_code=204)
def unmark_not_new(user_id: UserDep, engine: EngineDep, kind: str, item_id: str) -> Response:
    check_per_user("not_new", user_id, get_settings().read_rate_limit_per_minute)
    checked_kind, checked_id = _mark_target(kind, item_id)
    with engine.begin() as conn:
        not_new.remove_mark(conn, user_id=user_id, kind=checked_kind, item_id=checked_id)
    return Response(status_code=204)


class ReviewAlbumsOut(BaseModel):
    count: int
    items: list[NewAlbumOut]


class ReviewSinglesOut(BaseModel):
    count: int
    items: list[NewSingleOut]


class ReviewResponse(BaseModel):
    # False once the review is over for good: the app shows nothing.
    open: bool
    window_start: UtcInstant | None
    window_end: UtcInstant | None
    # Send it back to POST /v1/me/review/done.
    through: UtcInstant | None
    albums: ReviewAlbumsOut
    singles: ReviewSinglesOut


@router.get("/me/review", response_model=ReviewResponse)
def review(user_id: UserDep, engine: EngineDep, now: NowDep) -> ReviewResponse:
    check_per_user("review", user_id, get_settings().read_rate_limit_per_minute)
    with _snapshot(engine) as conn, conn.begin():
        result = review_queries.read_review(conn, user_id=user_id, now=now)
        own = _own_words(conn, user_id, [*result.albums, *result.singles])
    return ReviewResponse(
        open=result.open,
        window_start=result.window_start,
        window_end=result.window_end,
        through=result.through,
        albums=ReviewAlbumsOut(
            count=result.album_count,
            items=[_rated(NewAlbumOut, a, own) for a in result.albums],
        ),
        singles=ReviewSinglesOut(
            count=result.single_count,
            items=[_rated(NewSingleOut, a, own) for a in result.singles],
        ),
    )


class ReviewDoneIn(BaseModel):
    through: datetime


@router.post("/me/review/done", status_code=204)
def review_done(user_id: UserDep, engine: EngineDep, now: NowDep, body: ReviewDoneIn) -> Response:
    check_per_user("review", user_id, get_settings().read_rate_limit_per_minute)
    if body.through.tzinfo is None:
        raise _unprocessable("invalid_parameter", "through is an instant with a UTC offset.")
    with engine.begin() as conn:
        review_queries.clear_review(conn, user_id=user_id, through=body.through, now=now)
    return Response(status_code=204)


class _HasAlbumId(Protocol):
    @property
    def album_id(self) -> str: ...


@dataclass(frozen=True)
class _OwnWords:
    """The user's rating and comment of each album that has one, independently."""

    ratings: dict[str, int]
    comments: dict[str, str]


def _own_words(conn: Connection, user_id: UUID, albums: Iterable[_HasAlbumId]) -> _OwnWords:
    ids = [a.album_id for a in albums]
    return _OwnWords(
        ratings=rating_queries.ratings_for(conn, user_id=user_id, album_ids=ids),
        comments=rating_queries.comments_for(conn, user_id=user_id, album_ids=ids),
    )


def _rated[M: BaseModel](model: type[M], album: _HasAlbumId, own: _OwnWords) -> M:
    """``album`` on the wire, with the user's rating and comment of it (each or null)."""
    out = model.model_validate(album, from_attributes=True)
    return out.model_copy(
        update={
            "rating": own.ratings.get(album.album_id),
            "comment": own.comments.get(album.album_id),
        }
    )


class RatingIn(BaseModel):
    # Strict: "8" or 7.5 is refused, never read as a rating.
    rating: StrictInt


_RATING_PATH = "/albums/{album_id}/rating"


def _rating_target(album_id: str) -> str:
    if not not_new.SPOTIFY_ID.fullmatch(album_id):
        raise _unprocessable("invalid_parameter", "album_id is a Spotify album ID.")
    return album_id


@router.put(_RATING_PATH, status_code=204)
def rate_album(user_id: UserDep, engine: EngineDep, album_id: str, body: RatingIn) -> Response:
    check_per_user("rating", user_id, get_settings().read_rate_limit_per_minute)
    checked = _rating_target(album_id)
    if not rating_queries.MIN_RATING <= body.rating <= rating_queries.MAX_RATING:
        raise _unprocessable("invalid_parameter", "rating is a whole number from 1 to 10.")
    with engine.begin() as conn:
        if not not_new.has_played(conn, user_id=user_id, kind="album", item_id=checked):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "You have no play of this album."},
            )
        rating_queries.set_rating(conn, user_id=user_id, album_id=checked, rating=body.rating)
    return Response(status_code=204)


@router.delete(_RATING_PATH, status_code=204)
def unrate_album(user_id: UserDep, engine: EngineDep, album_id: str) -> Response:
    check_per_user("rating", user_id, get_settings().read_rate_limit_per_minute)
    checked = _rating_target(album_id)
    with engine.begin() as conn:
        rating_queries.clear_rating(conn, user_id=user_id, album_id=checked)
    return Response(status_code=204)


_COMMENT_PATH = "/albums/{album_id}/comment"


def _comment_text(body: object) -> str:
    """``body["comment"]``, checked, or 422 ``invalid_parameter``.

    Read by hand, not by a model: FastAPI's own 422 repeats the input it could
    not read, and a comment must never be in an error message (PRD P12). Every
    refusal is the same fixed text.
    """
    comment = body.get("comment") if isinstance(body, dict) else None
    if not isinstance(comment, str) or not rating_queries.is_valid_comment(comment):
        raise _unprocessable(
            "invalid_parameter",
            f'The body is {{"comment": "..."}}: 1 to {rating_queries.MAX_COMMENT_LENGTH} '
            "characters, not blank, no NUL. DELETE clears a comment.",
        )
    return comment


@router.put(_COMMENT_PATH, status_code=204)
def comment_album(
    user_id: UserDep,
    engine: EngineDep,
    album_id: str,
    body: Annotated[Any, Body(examples=[{"comment": "Better than I remembered."}])] = None,
) -> Response:
    check_per_user("comment", user_id, get_settings().read_rate_limit_per_minute)
    checked = _rating_target(album_id)
    comment = _comment_text(body)
    with engine.begin() as conn:
        if not not_new.has_played(conn, user_id=user_id, kind="album", item_id=checked):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "You have no play of this album."},
            )
        rating_queries.set_comment(conn, user_id=user_id, album_id=checked, comment=comment)
    return Response(status_code=204)


@router.delete(_COMMENT_PATH, status_code=204)
def uncomment_album(user_id: UserDep, engine: EngineDep, album_id: str) -> Response:
    check_per_user("comment", user_id, get_settings().read_rate_limit_per_minute)
    checked = _rating_target(album_id)
    with engine.begin() as conn:
        rating_queries.clear_comment(conn, user_id=user_id, album_id=checked)
    return Response(status_code=204)


# ``:path`` so a malformed key with "/" in it still reaches the route and gets a 422.
_TRACK_COMMENT_PATH = "/tracks/{item_key:path}/comment"


def _track_target(item_key: str) -> str:
    """The checked ``item_key`` of a track route, or 422 ``invalid_parameter``."""
    if not not_new.is_valid_item_id("track", item_key):
        raise _unprocessable("invalid_parameter", "item_key is a track item_key.")
    return item_key


@router.put(_TRACK_COMMENT_PATH, status_code=204)
def comment_track(
    user_id: UserDep,
    engine: EngineDep,
    item_key: str,
    body: Annotated[Any, Body(examples=[{"comment": "The bridge at 2:10."}])] = None,
) -> Response:
    check_per_user("comment", user_id, get_settings().read_rate_limit_per_minute)
    checked = _track_target(item_key)
    comment = _comment_text(body)
    with engine.begin() as conn:
        if not not_new.has_played(conn, user_id=user_id, kind="track", item_id=checked):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found", "message": "You have no play of this track."},
            )
        rating_queries.set_track_comment(conn, user_id=user_id, item_key=checked, comment=comment)
    return Response(status_code=204)


@router.delete(_TRACK_COMMENT_PATH, status_code=204)
def uncomment_track(user_id: UserDep, engine: EngineDep, item_key: str) -> Response:
    check_per_user("comment", user_id, get_settings().read_rate_limit_per_minute)
    checked = _track_target(item_key)
    with engine.begin() as conn:
        rating_queries.clear_track_comment(conn, user_id=user_id, item_key=checked)
    return Response(status_code=204)


class RatedAlbumOut(BaseModel):
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: ArtworkUrl
    # "album", or "single" / "ep" for what Spotify calls a single.
    release_type: rating_queries.ReleaseType
    # Null only with ``commented=true``, for a release with only a comment.
    rating: int | None
    rated_at: UtcInstant | None
    comment: str | None
    commented_at: UtcInstant | None


class RatedAlbumsResponse(BaseModel):
    count: int
    items: list[RatedAlbumOut]


@router.get("/ratings/albums", response_model=RatedAlbumsResponse)
def rated_albums(
    user_id: UserDep,
    engine: EngineDep,
    commented: Annotated[str | None, Query()] = None,
) -> RatedAlbumsResponse:
    check_per_user("ratings", user_id, get_settings().read_rate_limit_per_minute)
    with_comments = _flag(commented, "commented")
    with _snapshot(engine) as conn, conn.begin():
        rated = rating_queries.read_all_ratings(conn, user_id=user_id, commented=with_comments)
    return RatedAlbumsResponse(
        count=len(rated),
        items=[RatedAlbumOut.model_validate(r, from_attributes=True) for r in rated],
    )


@router.get("/search", response_model=SearchResponse)
def search(
    user_id: UserDep, engine: EngineDep, q: Annotated[str | None, Query()] = None
) -> SearchResponse:
    check_per_user("search", user_id, get_settings().read_rate_limit_per_minute)
    try:
        words = search_queries.search_words(q)
    except search_queries.SearchQueryError as exc:
        raise _unprocessable(exc.code, exc.message) from None
    with _snapshot(engine) as conn, conn.begin():
        zone = _user_time_zone(conn, user_id)
        found = search_queries.read_search(conn, user_id=user_id, words=words, time_zone=zone)
    return SearchResponse(
        albums=FoundAlbumsOut(
            count=found.album_count,
            items=[FoundAlbumOut.model_validate(a, from_attributes=True) for a in found.albums],
        ),
        tracks=FoundTracksOut(
            count=found.track_count,
            items=[FoundTrackOut.model_validate(t, from_attributes=True) for t in found.tracks],
        ),
    )


@router.get(
    "/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/json": {}}}},
)
def export(user_id: UserDep, engine: EngineDep, now: NowDep) -> StreamingResponse:
    settings = get_settings()
    check_per_user("export", user_id, settings.export_rate_limit_per_minute)
    rows = stream_export(engine, user_id, now, settings.export_idle_timeout_seconds)
    body = _one_export_at_a_time(user_id, rows)
    # Run it to its first yield here: that takes the user's slot, or says it is
    # taken while we can still answer 429. Once started, closing or dropping
    # the generator releases the slot, whether or not the body was sent.
    try:
        next(body)
    except Busy:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(settings.export_idle_timeout_seconds)},
        ) from None
    return StreamingResponse(
        body,
        media_type="application/json",
        headers={
            # Personal data: no cache may keep a copy (WHO-25 R5) ...
            "Cache-Control": "no-store",
            # ... and nginx must not spool it to proxy_temp_path on disk (P13).
            "X-Accel-Buffering": "no",
            "Content-Disposition": 'attachment; filename="new2u-export.json"',
        },
    )


def _one_export_at_a_time(user_id: UUID, rows: Iterator[bytes]) -> Generator[bytes]:
    """``rows``, while holding the user's one export slot (WHO-48 F1).

    Yields nothing first, once the slot is held and before any DB work.
    """
    with export_streams.hold(str(user_id)):
        yield b""
        yield from rows


@router.delete("/me", status_code=204)
def delete_me(user_id: UserDep, engine: EngineDep) -> Response:
    check_per_user("delete", user_id, get_settings().read_rate_limit_per_minute)
    with engine.begin() as conn:
        deleted = delete_all_user_data(conn, user_id)
        if deleted != 1:
            # Not exactly this one account: roll back and say the session is gone.
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
            )
    return Response(status_code=204)


@router.get("/config", response_model=ConfigResponse)
def config(user_id: UserDep) -> ConfigResponse:
    # The same setting the poller sleeps on, so the connect screen states the real interval.
    return ConfigResponse(poll_interval_seconds=get_settings().poller_interval_seconds)
