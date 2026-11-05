"""The known-music baseline: what the user already knew before they connected.

Spotify lists only the latest 50 plays, so right after a first connect almost
everything looks "new to you". Once per connection the poller reads what the
user's Spotify account says they already know, and stores it as "Not new"
marks with ``source = 'spotify_library'`` (``app.read.not_new``). Those hide an
item exactly as the user's own mark does, and Undo removes them the same way.

What counts as known, and only from before ``collection_started_at``:

- **Saved albums** (``/me/albums``) added before collection started: the album
  and its own artists. Its tracks follow from the album mark at read time.
- **Saved tracks** (``/me/tracks``) added before collection started: the track
  and its artists. Not its album: one saved song is not the whole album.
- **Followed artists** (``/me/following``): the artist. Spotify gives no follow
  date, so a follow made after connecting counts too.
- **Top artists and tracks** (``/me/top``): ``long_term`` (about a year) always,
  ``medium_term`` and ``short_term`` only when the baseline is read within
  ``FRESH_BASELINE`` of collection start. Read later, those ranges are mostly
  what the user found while the app was collecting, which is new to them.

Bounded: at most ``SAVED_TRACK_PAGES``, ``SAVED_ALBUM_PAGES`` and
``FOLLOWED_PAGES`` pages of 50, newest first, so one baseline is at most 86
calls. Nothing else is read or kept: no dates, no playlists, no counts.

Only IDs and names are stored, in the forms the read side matches on: the
Spotify album ID, ``item_key`` for a track, and the artist name after NFC and
trimming (R-31).

Failures: a 429/5xx that outlasts the retries (``SpotifyUnavailable``) stores
nothing and the next poll tries again. Any other refusal of one source (for
example a 403 from an endpoint Spotify closed to this app) skips that source
only, so a baseline is still stored from the rest. Never raised to the poll.
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.events import keys
from app.read.names import ARTIST_NAME
from app.read.not_new import MAX_ARTIST_NAME_LENGTH, SPOTIFY_ID
from app.spotify.client import (
    LIBRARY_SCOPES,
    PAGE_LIMIT,
    SpotifyClient,
    SpotifyRejected,
    SpotifyUnavailable,
)

logger = logging.getLogger("app.spotify.library_baseline")

SAVED_TRACK_PAGES: Final = 40
SAVED_ALBUM_PAGES: Final = 20
FOLLOWED_PAGES: Final = 20
FRESH_BASELINE: Final = timedelta(days=1)


@dataclass
class Baseline:
    album_ids: set[str] = field(default_factory=set)
    track_keys: set[str] = field(default_factory=set)
    artist_names: set[str] = field(default_factory=set)
    skipped_sources: list[str] = field(default_factory=list)

    def add_artists(self, artists: object) -> None:
        if not isinstance(artists, list):
            return
        for artist in artists:
            name = artist.get("name") if isinstance(artist, dict) else None
            if isinstance(name, str) and 0 < len(name) <= MAX_ARTIST_NAME_LENGTH:
                if "\x00" not in name:
                    self.artist_names.add(name)

    def add_track(self, track: object) -> None:
        if not isinstance(track, dict):
            return
        track_id = track.get("id")
        # A local file has no Spotify ID; it has no stable key here either.
        if isinstance(track_id, str) and SPOTIFY_ID.fullmatch(track_id):
            self.track_keys.add(keys.item_key_from_provider_id(track_id).value)
            self.add_artists(track.get("artists"))

    def add_album(self, album: object) -> None:
        if not isinstance(album, dict):
            return
        album_id = album.get("id")
        if isinstance(album_id, str) and SPOTIFY_ID.fullmatch(album_id):
            self.album_ids.add(album_id)
            self.add_artists(album.get("artists"))


def has_library_scopes(scope: Iterable[str]) -> bool:
    return LIBRARY_SCOPES <= set(scope)


def read_baseline(
    client: SpotifyClient,
    access_token: str,
    *,
    collection_started_at: datetime,
    now: datetime,
) -> Baseline:
    """Everything the baseline keeps, read from Spotify. Raises ``SpotifyUnavailable``."""
    baseline = Baseline()
    fresh = now - collection_started_at <= FRESH_BASELINE
    ranges = ("long_term", "medium_term", "short_term") if fresh else ("long_term",)

    def source(name: str, read: Callable[[], None]) -> None:
        try:
            read()
        except SpotifyRejected as exc:
            baseline.skipped_sources.append(exc.kind)
            logger.warning(
                "library baseline source skipped source=%s error_kind=%s", name, exc.kind
            )

    def top() -> None:
        for time_range in ranges:
            for artist in client.top(access_token, "artists", time_range):
                baseline.add_artists([artist])
            for track in client.top(access_token, "tracks", time_range):
                baseline.add_track(track)

    def saved(kind: str, pages: int) -> None:
        for page in range(pages):
            items, has_next = client.saved_page(access_token, kind, page * PAGE_LIMIT)
            for item in items:
                if not _added_before(item, collection_started_at):
                    continue
                if kind == "tracks":
                    baseline.add_track(item.get("track"))
                else:
                    baseline.add_album(item.get("album"))
            if not has_next:
                return

    def followed() -> None:
        after: str | None = None
        for _ in range(FOLLOWED_PAGES):
            items, after = client.followed_artists_page(access_token, after)
            baseline.add_artists(items)
            if after is None:
                return

    source("top", top)
    source("saved_albums", lambda: saved("albums", SAVED_ALBUM_PAGES))
    source("saved_tracks", lambda: saved("tracks", SAVED_TRACK_PAGES))
    source("followed_artists", followed)
    return baseline


def _added_before(item: Mapping[str, Any], instant: datetime) -> bool:
    added = item.get("added_at")
    if not isinstance(added, str):
        return False
    try:
        added_at = datetime.fromisoformat(added.replace("Z", "+00:00"))
    except ValueError:
        return False
    return added_at.tzinfo is not None and added_at < instant


_INSERT_ALBUMS = text(
    "INSERT INTO not_new_mark (user_id, kind, item_id, source) "
    "SELECT :user_id, 'album', item_id, 'spotify_library' "
    "FROM unnest(CAST(:items AS text[])) AS item(item_id) "
    "ON CONFLICT (user_id, kind, item_id) DO NOTHING"
)
_INSERT_TRACKS = text(
    "INSERT INTO not_new_mark (user_id, kind, item_id, source) "
    "SELECT :user_id, 'track', item_id, 'spotify_library' "
    "FROM unnest(CAST(:items AS text[])) AS item(item_id) "
    "ON CONFLICT (user_id, kind, item_id) DO NOTHING"
)
# Names are stored in the R-31 form the lists serve, so a mark matches.
_INSERT_ARTISTS = text(
    "INSERT INTO not_new_mark (user_id, kind, item_id, source) "
    f"SELECT DISTINCT :user_id, 'artist', {ARTIST_NAME}, 'spotify_library' "
    "FROM unnest(CAST(:items AS text[])) AS creator(raw) "
    f"WHERE {ARTIST_NAME} <> '' "
    "ON CONFLICT (user_id, kind, item_id) DO NOTHING"
)


def store_baseline(conn: Connection, user_id: UUID, baseline: Baseline, now: datetime) -> bool:
    """Write the marks and ``library_baseline_at``, once. False if not due any more.

    A mark the user already made is kept as theirs (``ON CONFLICT DO NOTHING``).
    """
    due = conn.execute(
        text(
            "SELECT library_baseline_at IS NULL FROM spotify_connection "
            "WHERE user_id = :user_id AND status = 'active' FOR UPDATE"
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()
    if not due:
        return False
    for statement, items in (
        (_INSERT_ALBUMS, baseline.album_ids),
        (_INSERT_TRACKS, baseline.track_keys),
        (_INSERT_ARTISTS, baseline.artist_names),
    ):
        if items:
            conn.execute(statement, {"user_id": user_id, "items": sorted(items)})
    conn.execute(
        text("UPDATE spotify_connection SET library_baseline_at = :now WHERE user_id = :user_id"),
        {"now": now, "user_id": user_id},
    )
    return True


def build_library_baseline(
    engine: Engine,
    client: SpotifyClient,
    access_token: str,
    user_id: UUID,
    *,
    collection_started_at: datetime,
    now: datetime,
) -> bool:
    """Read and store the baseline. True when stored; a failure is logged, not raised."""
    try:
        baseline = read_baseline(
            client, access_token, collection_started_at=collection_started_at, now=now
        )
    except SpotifyUnavailable as exc:
        logger.warning("library baseline failed user_id=%s error_kind=%s", user_id, exc.kind)
        return False
    with engine.begin() as conn:
        stored = store_baseline(conn, user_id, baseline, now)
    # Counts only, never what is in the library (P12).
    logger.info(
        "library baseline stored=%s user_id=%s albums=%d tracks=%d artists=%d skipped=%d",
        stored,
        user_id,
        len(baseline.album_ids),
        len(baseline.track_keys),
        len(baseline.artist_names),
        len(baseline.skipped_sources),
    )
    return stored
