"""Map a recently-played item to a unified event (schema revision 3 §3.1).

Kept: ``played_at``, track id, name, artists, album name and id, the album's
own artists (``provider_extras.spotify_album_artists``, WHO-135), the album's
track count (``provider_extras.spotify_album_total_tracks``, WHO-141), its
release type (``provider_extras.spotify_album_type``, WHO-162), ISRC,
duration, and the album cover URL (``artwork_url``, WHO-149: one image picked by
``app.spotify.artwork``, NULL unless it is a Spotify image URL). Not kept:
``context`` (Q4) and everything else in §6. ``played_ms`` is always None:
Spotify does not report how much of a track was played.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.events import keys
from app.events.store import IngestMethod, PreciseEvent
from app.privacy.provider_extras import (
    SPOTIFY_ALBUM_ARTIST_MAX_CHARS,
    SPOTIFY_ALBUM_ARTISTS,
    SPOTIFY_ALBUM_ARTISTS_MAX_ITEMS,
    SPOTIFY_ALBUM_TOTAL_TRACKS,
    SPOTIFY_ALBUM_TOTAL_TRACKS_MAX,
    SPOTIFY_ALBUM_TYPE,
    SPOTIFY_ALBUM_TYPES,
)
from app.spotify.artwork import pick_album_image

SOURCE = "spotify"
SOURCE_ARTIFACT = "spotify_recently_played"


class UnreadablePlay(ValueError):
    """A recently-played item is missing a field we need. Names the field only."""


@dataclass(frozen=True)
class SpotifyPlay:
    played_at: datetime
    event: PreciseEvent


def play_from_item(
    item: Mapping[str, Any], ingest_method: IngestMethod = "api_poll"
) -> SpotifyPlay:
    """``ingest_method`` is ``api_backfill`` for the history import, else ``api_poll``."""
    played_at = _played_at(item.get("played_at"))
    track = item.get("track")
    if not isinstance(track, Mapping):
        raise UnreadablePlay("track")
    title = track.get("name")
    if not isinstance(title, str) or not title:
        raise UnreadablePlay("track.name")

    track_id = track.get("id")
    external_ids: dict[str, str] = {}
    if isinstance(track_id, str) and track_id:
        item_key = keys.item_key_from_provider_id(track_id)
        external_ids["spotify_track_id"] = track_id
    else:
        # Local files have no Spotify id. The schema's fallback identity
        # (§4.1) keeps the play rather than dropping it.
        item_key = keys.item_key_from_title(title)

    album_value = track.get("album")
    album: Mapping[str, Any] = album_value if isinstance(album_value, Mapping) else {}
    album_id = album.get("id")
    if isinstance(album_id, str) and album_id:
        external_ids["spotify_album_id"] = album_id
    track_ids = track.get("external_ids")
    isrc = track_ids.get("isrc") if isinstance(track_ids, Mapping) else None
    if isinstance(isrc, str) and isrc:
        external_ids["isrc"] = isrc

    artists = track.get("artists") or []
    creators = tuple(
        a["name"] for a in artists if isinstance(a, Mapping) and isinstance(a.get("name"), str)
    )
    album_name = album.get("name")
    duration_ms = track.get("duration_ms")
    extras = album_extras(album)

    return SpotifyPlay(
        played_at=played_at,
        event=PreciseEvent(
            source=SOURCE,
            source_artifact=SOURCE_ARTIFACT,
            ingest_method=ingest_method,
            kind="listen",
            occurred_at=played_at,
            title=title,
            item_key=item_key,
            duration_ms=duration_ms if isinstance(duration_ms, int) else None,
            played_ms=None,
            creators=creators,
            collection=album_name if isinstance(album_name, str) and album_name else None,
            artwork_url=pick_album_image(album),
            external_ids=external_ids,
            provider_extras=extras,
        ),
    )


def album_extras(album: Mapping[str, Any]) -> dict[str, object]:
    """The ``provider_extras`` kept from a Spotify album object; a key is left out when unknown."""
    extras: dict[str, object] = {}
    artists = album_artist_names(album)
    if artists:
        extras[SPOTIFY_ALBUM_ARTISTS] = artists
    total_tracks = album_total_tracks(album)
    if total_tracks is not None:
        extras[SPOTIFY_ALBUM_TOTAL_TRACKS] = total_tracks
    album_type = album.get("album_type")
    # Only the three values the database allows (migration 0011); anything else
    # is left out, and the release stays in the album lists (WHO-162).
    if isinstance(album_type, str) and album_type in SPOTIFY_ALBUM_TYPES:
        extras[SPOTIFY_ALBUM_TYPE] = album_type
    return extras


def album_total_tracks(album: Mapping[str, Any]) -> int | None:
    """``album.total_tracks`` when it is within the bounds the database checks (migration 0010).

    Anything else is None, never a guess: an album with an unknown track count
    is not "new to you" until the count is known (WHO-141).
    """
    value = album.get("total_tracks")
    # bool is an int in Python; JSON true is not a track count.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= SPOTIFY_ALBUM_TOTAL_TRACKS_MAX else None


def album_artist_names(album: Mapping[str, Any]) -> list[str]:
    """``album.artists[].name``, kept within the bounds the database checks (migration 0009).

    A name that is empty or too long is left out, never cut: a shortened name
    would be a guess. An empty list means the album artists are unknown.
    """
    artists = album.get("artists")
    if not isinstance(artists, list):
        return []
    names = [
        a["name"]
        for a in artists
        if isinstance(a, Mapping)
        and isinstance(a.get("name"), str)
        and 0 < len(a["name"]) <= SPOTIFY_ALBUM_ARTIST_MAX_CHARS
    ]
    return names[:SPOTIFY_ALBUM_ARTISTS_MAX_ITEMS]


def _played_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise UnreadablePlay("played_at")
    try:
        played_at = datetime.fromisoformat(value)
    except ValueError:
        raise UnreadablePlay("played_at") from None
    if played_at.tzinfo is None:
        # Spotify documents played_at as UTC with a "Z". Without an offset we
        # would be guessing the zone, so the item is rejected instead.
        raise UnreadablePlay("played_at")
    return played_at
