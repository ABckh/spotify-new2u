"""Allowlist for ``unified_event.provider_extras``.

``provider_extras`` is ``jsonb``, so its keys cannot be enumerated from the
schema. This constant is the only place a key may be permitted, and the "what we
collect" field list renders one line per key from it. Adding a key needs a
written reason in the PRD (P8) and a label here, in the same change.

Keys today:

- ``spotify`` / ``spotify_album_artists`` (WHO-135): the album's own artists,
  ``track.album.artists[].name`` from recently-played or ``GET /v1/albums/{id}``.
  A JSON array of 1 to 20 non-empty strings of at most 500 characters.
  PRD P8 reason: "New to you" names an album by its own artist. The track
  artists of a compilation are not the album artist (WHO-128), and Spotify
  sends this field in the response we already read, so no new call or scope is
  needed for new plays.
- ``spotify`` / ``spotify_album_total_tracks`` (WHO-141): the album's track
  count, ``track.album.total_tracks`` from recently-played or
  ``GET /v1/albums/{id}``. A JSON integer from 1 to 100000.
  PRD P8 reason: an album counts as listened, and so can be "new to you", only
  when more than half of its tracks were played (WHO-130). Spotify sends this
  field in the response we already read, so no new call or scope is needed for
  new plays.
- ``spotify`` / ``spotify_album_type`` (WHO-162): Spotify's release type,
  ``track.album.album_type`` from recently-played or ``GET /v1/albums/{id}``.
  One of the JSON strings ``"album"``, ``"single"`` or ``"compilation"``; any
  other value is not stored. PRD P8 reason: singles (Spotify counts EPs as
  singles) are left out of every album list (WHO-159). Spotify sends this field
  in the response we already read, so no new call or scope is needed for new
  plays.

The database enforces the same rule: the CHECK constraint
``unified_event_provider_extras_allowlist_chk`` (migrations 0004, 0009, 0010 and
0011) mirrors this constant, and a test fails if the two disagree. A new key
therefore needs a migration as well, and that migration also bounds the key's value shape.
"""

from collections.abc import Mapping
from typing import Final

from app.privacy.stored_fields import Category

SPOTIFY_ALBUM_ARTISTS: Final = "spotify_album_artists"
# Mirrored by the database function spotify_album_artists_ok (migration 0009).
SPOTIFY_ALBUM_ARTISTS_MAX_ITEMS: Final = 20
SPOTIFY_ALBUM_ARTIST_MAX_CHARS: Final = 500
SPOTIFY_ALBUM_TOTAL_TRACKS: Final = "spotify_album_total_tracks"
# Mirrored by the database function spotify_album_total_tracks_ok (migration 0010).
SPOTIFY_ALBUM_TOTAL_TRACKS_MAX: Final = 100_000
SPOTIFY_ALBUM_TYPE: Final = "spotify_album_type"
# Mirrored by the database function spotify_album_type_ok (migration 0011).
SPOTIFY_ALBUM_TYPES: Final = frozenset({"album", "single", "compilation"})

# source -> key -> (category, plain-language label shown to the user)
PROVIDER_EXTRAS_ALLOWLIST: Final[Mapping[str, Mapping[str, tuple[Category, str]]]] = {
    "spotify": {
        SPOTIFY_ALBUM_ARTISTS: (Category.TRACK_DETAILS, "The album's artists"),
        SPOTIFY_ALBUM_TOTAL_TRACKS: (Category.TRACK_DETAILS, "How many tracks the album has"),
        SPOTIFY_ALBUM_TYPE: (
            Category.TRACK_DETAILS,
            "Whether Spotify calls the release an album, a single or a compilation",
        ),
    },
    "youtube": {},
    "netflix": {},
}


class ProviderExtrasKeyNotAllowed(ValueError):
    """Raised before a row with an unlisted ``provider_extras`` key is written."""

    def __init__(self, source: str, keys: frozenset[str]) -> None:
        self.source = source
        self.keys = keys
        # Key names only. Values may be user data and never go into an error.
        super().__init__(
            f"provider_extras keys not in the allowlist for source {source!r}: {sorted(keys)}"
        )


def check_provider_extras(source: str, extras: Mapping[str, object]) -> None:
    """Reject any key not allowlisted for ``source``.

    A source with no allowlist entry permits nothing. Every writer of
    ``unified_event`` calls this before the insert: it names the key, which the
    database constraint behind it does not.
    """
    allowed = PROVIDER_EXTRAS_ALLOWLIST.get(source, {})
    unlisted = frozenset(extras) - frozenset(allowed)
    if unlisted:
        raise ProviderExtrasKeyNotAllowed(source, unlisted)
