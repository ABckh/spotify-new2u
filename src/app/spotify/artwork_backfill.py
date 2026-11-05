"""One-off: collect the album covers of plays stored before WHO-149.

Run with: python -m app.spotify.artwork_backfill

Plays stored before WHO-149 have ``artwork_url`` NULL. This asks Spotify for
each such album once and writes its cover onto that user's rows of the album.

- Per active connection: refresh the access token as the poller does (a rotated
  refresh token is stored at once), list the user's album IDs that have a row
  without a cover, and call ``GET /v1/albums/{id}`` for each, one at a time,
  ``pause`` seconds apart. The several-albums endpoint is not available to us
  (see ``app.spotify.client``).
- The image is picked as at ingest (``app.spotify.artwork``). Only rows with
  no cover are written, and a stored cover is never replaced, so a second run,
  or a run racing the poller, changes nothing more.
- ``429`` waits for ``Retry-After`` and ``5xx`` backs off, inside the client. A
  call that still fails stops this user's run; the next user is tried, and
  running the script again picks up what is left. An album Spotify no longer
  has (``404``), one with no usable image, or a stored album ID that is not
  base62 (not sent to Spotify), keeps no cover; a later run asks again.
- ``401`` or ``403`` on an album call (access revoked or refused) stops this
  user's run at once, as a failure; the next user is tried (WHO-148 rule 14).
- A dead grant (``invalid_grant``) is skipped, not marked: the poller's next
  tick does that, with its gap record.
- The log line holds counts only, never an album, a URL, or a token (P12).
"""

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import Connection, Engine, text

from app.core.db import get_engine
from app.core.log import configure_logging
from app.events.store import lock_user_source
from app.poller.spotify import active_connections
from app.spotify.album_artists_backfill import ACCESS_REFUSED, TokenError, access_token_or_error
from app.spotify.artwork import pick_album_image
from app.spotify.client import SpotifyClient, SpotifyError, SpotifyRejected, build_spotify_client
from app.spotify.plays import SOURCE

logger = logging.getLogger("app.spotify.artwork_backfill")


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
    "AND external_ids ? 'spotify_album_id' AND artwork_url IS NULL "
    "ORDER BY 1"
)

_FILL = text(
    "UPDATE unified_event SET artwork_url = :url "
    "WHERE user_id = :user_id AND source = :source "
    "AND external_ids ->> 'spotify_album_id' = :album_id AND artwork_url IS NULL"
)


def albums_missing_artwork(conn: Connection, user_id: UUID) -> list[str]:
    return list(conn.execute(_ALBUMS_MISSING, {"user_id": user_id, "source": SOURCE}).scalars())


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
        missing = albums_missing_artwork(conn, user_id)
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
            # 404 or unreadable: this album keeps no cover.
            continue
        except ValueError:
            # An ID that is not base62 (never put in a URL): this album keeps no cover.
            continue
        except SpotifyError as exc:
            return BackfillResult("failed", exc.kind, len(missing), albums_filled, rows_filled)
        url = pick_album_image(album)
        if url is None:
            continue
        rows = _fill(engine, user_id, album_id, url)
        albums_filled += 1 if rows else 0
        rows_filled += rows
    return BackfillResult("succeeded", None, len(missing), albums_filled, rows_filled)


def _fill(engine: Engine, user_id: UUID, album_id: str, url: str) -> int:
    with engine.begin() as conn:
        lock_user_source(conn, user_id, SOURCE)
        result = conn.execute(
            _FILL, {"url": url, "user_id": user_id, "source": SOURCE, "album_id": album_id}
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
            "artwork backfill user_id=%s status=%s error_kind=%s "
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
