"""One-off: collect the album details of plays stored before WHO-135, WHO-141 and WHO-162.

Run with: python -m app.spotify.album_artists_backfill

Plays stored before migration 0009 have no ``provider_extras.spotify_album_artists``,
so "New to you" falls back to the artists shared by every played track
(``app.read.album_artists``). Plays stored before migration 0010 have no
``provider_extras.spotify_album_total_tracks``, so their album is never "new to
you" (``app.read.album_sql``). Plays stored before migration 0011 have no
``provider_extras.spotify_album_type``, so a single among them is still listed
as an album. This asks Spotify for each album lacking any of these keys once and
writes what it lacks onto that user's rows of the album: all three come from the
same ``GET /v1/albums/{id}`` answer.

- Per active connection: refresh the access token as the poller does (a rotated
  refresh token is stored at once), list the user's album IDs whose rows lack
  any of the keys, and call ``GET /v1/albums/{id}`` for each, one at a time,
  ``pause`` seconds apart. The several-albums endpoint is not available to us (see
  ``app.spotify.client``).
- Only rows lacking a key are written, and a stored value is never replaced,
  so a second run, or a run racing the poller, changes nothing more.
- ``429`` waits for ``Retry-After`` and ``5xx`` backs off, inside the client. A
  call that still fails stops this user's run; the next user is tried, and
  running the script again picks up what is left. An album Spotify no longer
  has (``404``), one with no usable value for any key, or a stored album ID
  that is not base62 (not sent to Spotify), is left as it is.
- ``401`` or ``403`` on an album call (access revoked or refused) stops this
  user's run at once, as a failure; the next user is tried (WHO-148 rule 14).
- A dead grant (``invalid_grant``) is skipped, not marked: the poller's next
  tick does that, with its gap record.
- The log line holds counts only, never an album, a name, or a token (P12).
"""

import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import Connection, Engine, text

from app.core.crypto import DecryptionError
from app.core.db import get_engine
from app.core.log import configure_logging
from app.events.store import lock_user_source
from app.poller.spotify import active_connections
from app.privacy.provider_extras import (
    SPOTIFY_ALBUM_ARTISTS,
    SPOTIFY_ALBUM_TOTAL_TRACKS,
    SPOTIFY_ALBUM_TYPE,
    check_provider_extras,
)
from app.spotify.client import (
    InvalidGrant,
    SpotifyClient,
    SpotifyError,
    SpotifyRejected,
    build_spotify_client,
)
from app.spotify.connection import decrypt_refresh_token, store_rotated_refresh_token
from app.spotify.plays import SOURCE, album_extras

logger = logging.getLogger("app.spotify.album_artists_backfill")

# The album call's kinds for 401 and 403 (``SpotifyClient._json_or_reject``).
ACCESS_REFUSED = frozenset({"album_http_401", "album_http_403"})


@dataclass(frozen=True)
class BackfillResult:
    status: str  # succeeded | failed
    error_kind: str | None = None
    albums_missing: int = 0
    albums_filled: int = 0
    rows_filled: int = 0


_ALBUMS_MISSING = text(
    "SELECT DISTINCT external_ids ->> 'spotify_album_id' AS album_id FROM unified_event "
    "WHERE user_id = :user_id AND source = :source "
    "AND external_ids ? 'spotify_album_id' AND NOT provider_extras ?& CAST(:keys AS text[]) "
    "ORDER BY 1"
)

# ``||`` keeps the right-hand value on a shared key, so a stored value is never replaced.
_FILL = text(
    "UPDATE unified_event "
    "SET provider_extras = CAST(:extras AS jsonb) || provider_extras "
    "WHERE user_id = :user_id AND source = :source "
    "AND external_ids ->> 'spotify_album_id' = :album_id "
    "AND NOT provider_extras ?& CAST(:keys AS text[])"
)

BACKFILLED_KEYS = [SPOTIFY_ALBUM_ARTISTS, SPOTIFY_ALBUM_TOTAL_TRACKS, SPOTIFY_ALBUM_TYPE]


def albums_missing_artists(conn: Connection, user_id: UUID) -> list[str]:
    """The user's album IDs with a row lacking the album artists, track count or type."""
    return list(
        conn.execute(
            _ALBUMS_MISSING, {"user_id": user_id, "source": SOURCE, "keys": BACKFILLED_KEYS}
        ).scalars()
    )


def backfill_user(
    engine: Engine,
    client: SpotifyClient,
    user_id: UUID,
    *,
    pause: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillResult:
    with engine.connect() as conn:
        ciphertext = conn.execute(
            text(
                "SELECT refresh_token_ciphertext FROM spotify_connection "
                "WHERE user_id = :user_id AND status = 'active'"
            ),
            {"user_id": user_id},
        ).scalar_one_or_none()
        missing = albums_missing_artists(conn, user_id)
    if ciphertext is None:
        return BackfillResult("failed", "not_active")
    if not missing:
        return BackfillResult("succeeded")

    access_token = access_token_or_error(engine, client, user_id, bytes(ciphertext))
    if isinstance(access_token, TokenError):
        return BackfillResult("failed", access_token.kind, len(missing))

    albums_filled = rows_filled = 0
    for n, album_id in enumerate(missing):
        if n:
            sleep(pause)
        try:
            album = client.album(access_token, album_id)
        except SpotifyRejected as exc:
            if exc.kind in ACCESS_REFUSED:
                # Access revoked or refused: stop at once (WHO-148 rule 14).
                return BackfillResult("failed", exc.kind, len(missing), albums_filled, rows_filled)
            # 404 or unreadable: this album keeps the read fallback.
            continue
        except ValueError:
            # An ID that is not base62 (never put in a URL): this album keeps the read fallback.
            continue
        except SpotifyError as exc:
            return BackfillResult("failed", exc.kind, len(missing), albums_filled, rows_filled)
        extras = album_extras(album)
        if not extras:
            continue
        rows = _fill(engine, user_id, album_id, extras)
        albums_filled += 1 if rows else 0
        rows_filled += rows
    return BackfillResult("succeeded", None, len(missing), albums_filled, rows_filled)


@dataclass(frozen=True)
class TokenError:
    kind: str  # token_decrypt_failed | invalid_grant | a SpotifyError kind


def access_token_or_error(
    engine: Engine, client: SpotifyClient, user_id: UUID, read_ciphertext: bytes
) -> str | TokenError:
    """An access token, refreshed as the poller does. A rotated refresh token is stored at once.

    Shared by the one-off backfills (this one and ``app.spotify.artwork_backfill``).
    """
    try:
        refresh_token = decrypt_refresh_token(user_id, read_ciphertext)
        grant = client.refresh(refresh_token)
    except DecryptionError:
        return TokenError("token_decrypt_failed")
    except InvalidGrant:
        return TokenError("invalid_grant")
    except SpotifyError as exc:
        return TokenError(exc.kind)
    if grant.refresh_token is not None and grant.refresh_token != refresh_token:
        with engine.begin() as conn:
            store_rotated_refresh_token(
                conn, user_id, grant.refresh_token, read_ciphertext=read_ciphertext
            )
    return grant.access_token


def _fill(engine: Engine, user_id: UUID, album_id: str, extras: dict[str, object]) -> int:
    check_provider_extras(SOURCE, extras)
    with engine.begin() as conn:
        lock_user_source(conn, user_id, SOURCE)
        result = conn.execute(
            _FILL,
            {
                "extras": json.dumps(extras),
                "keys": list(extras),
                "user_id": user_id,
                "source": SOURCE,
                "album_id": album_id,
            },
        )
        return result.rowcount


def backfill_all(
    engine: Engine,
    client: SpotifyClient,
    *,
    pause: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[UUID, BackfillResult]:
    results: dict[UUID, BackfillResult] = {}
    for user_id in active_connections(engine):
        result = backfill_user(engine, client, user_id, pause=pause, sleep=sleep)
        results[user_id] = result
        # Counts only (P12).
        logger.info(
            "album artists backfill user_id=%s status=%s error_kind=%s "
            "albums_missing=%d albums_filled=%d rows_filled=%d",
            user_id,
            result.status,
            result.error_kind,
            result.albums_missing,
            result.albums_filled,
            result.rows_filled,
        )
    return results


def run() -> int:
    """Exit 0 when every user succeeded, 1 otherwise. Safe to run again."""
    configure_logging()
    with httpx.Client(timeout=30.0) as http:
        results = backfill_all(get_engine(), build_spotify_client(http))
    return 0 if all(r.status == "succeeded" for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(run())
