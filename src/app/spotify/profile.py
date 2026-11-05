"""What we keep from the Spotify profile (PRD P8, S1.4a, B7).

``GET /v1/me`` returns more than we need: images, follower count, and with
broader scopes country, product tier and email. We keep the account ID and the
display name, and this module is the only reader of that response, so nothing
else can be picked up by accident. The two fields are the
``SPOTIFY_PROFILE_COLUMNS`` entries in the field list.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.privacy.stored_fields import SPOTIFY_PROFILE_COLUMNS


@dataclass(frozen=True)
class SpotifyProfile:
    """One field per ``SPOTIFY_PROFILE_COLUMNS`` entry, and no others."""

    account_id: str
    # Spotify may return null; the app then shows the account ID (S1.4a).
    display_name: str | None


def profile_from_me(payload: Mapping[str, object]) -> SpotifyProfile:
    """Read the two kept fields from a ``GET /v1/me`` response body."""
    account_id = payload.get("id")
    display_name = payload.get("display_name")
    # Messages name the key only. Profile values never go into an error.
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("Spotify profile has no usable 'id'")
    if display_name is not None and not isinstance(display_name, str):
        raise ValueError("Spotify profile 'display_name' is not a string")
    return SpotifyProfile(account_id=account_id, display_name=display_name or None)


def profile_columns(profile: SpotifyProfile) -> dict[str, str | None]:
    """The profile as ``spotify_connection`` column values, for the connect upsert."""
    return {
        SPOTIFY_PROFILE_COLUMNS["id"]: profile.account_id,
        SPOTIFY_PROFILE_COLUMNS["display_name"]: profile.display_name,
    }
