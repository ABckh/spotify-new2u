"""One Spotify poll for one connection.

Per tick and per active connection:

1. Refresh the access token. ``invalid_grant`` (revoked, or past the 6-month
   expiry) marks the connection ``reauth_required``, deletes the dead token and
   records a failed collection run. It is never retried. A rotated refresh
   token is stored before anything else, because the old one may already be dead.
2. Ask ``recently-played`` for plays after the cursor. The cursor is the latest
   play we stored for this user, or the collection start if there is none, so
   it is derived from stored data and cannot drift from it.
3. Drop plays from before ``collection_started_at`` (WHO-29), then write each
   play through ``app.events.store``. A play already stored is a no-op, so an
   overlapping window adds zero rows.
4. Record the run in ``ingest_batch`` and ``last_polled_at`` on the connection,
   in the same transaction as the plays.

History import (WHO-89): if the user chose it at their first connect
(``history_import_requested``) and it has not succeeded yet
(``history_imported_at`` is NULL), step 2 is preceded by one call to
``recently-played`` with no ``after``: the latest 50 plays, whenever they were
made. Every readable one is written, the WHO-29 filter does not apply, in its
own ``ingest_batch`` with ``ingest_method = 'api_backfill'``, and
``history_imported_at`` is set in the same transaction. The normal poll then
runs as above; its cursor is the latest stored play, so it does not ask again
for what the import stored. A failed import is a failed ``api_backfill`` batch
and is tried again on the next poll. It never stops the normal poll. A
reconnect after reauth cancels an import that has not succeeded yet, so it
never stores plays from the gap (``app.spotify.connection.connect``, WHO-93 F1).

Known-music baseline: while ``library_baseline_at`` is NULL and the grant has
the library scopes, the history import is followed by one read of the user's
saved albums and tracks, followed artists and top lists, stored as "Not new"
marks (``app.spotify.library_baseline``). A grant from before those scopes
skips it until the user signs in again. It never stops the normal poll.

Any failure is recorded as a failed run with a classified ``error_kind`` and
raised to no one: the next tick tries again, and Spotify keeps 50 plays.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.core.config import get_settings
from app.core.crypto import DecryptionError
from app.events.store import IngestMethod, WriteCounts, lock_user_source, write_precise_event
from app.spotify.client import (
    RECENTLY_PLAYED_LIMIT,
    InvalidGrant,
    RecentlyPlayed,
    SpotifyClient,
    SpotifyError,
)
from app.spotify.collection import plays_made_while_connected
from app.spotify.connection import (
    decrypt_refresh_token,
    grant_expires_at,
    mark_reauth_required,
    store_rotated_refresh_token,
)
from app.spotify.history_import import stored_near, within_window
from app.spotify.library_baseline import build_library_baseline, has_library_scopes
from app.spotify.plays import SOURCE, SOURCE_ARTIFACT, SpotifyPlay, UnreadablePlay, play_from_item

logger = logging.getLogger("app.poller.spotify")

_ONE_MS = timedelta(milliseconds=1)


@dataclass(frozen=True)
class PollResult:
    status: str  # succeeded | failed | reauth_required
    error_kind: str | None = None
    rows_seen: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0


def active_connections(engine: Engine) -> list[UUID]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT user_id FROM spotify_connection WHERE status = 'active'")
            ).scalars()
        )


def connections_awaiting_first_poll(engine: Engine) -> list[UUID]:
    """Active connections with no poll attempt since the user last connected (WHO-87).

    A connect sets ``authorized_at``; any poll attempt, failed or not, records an
    ``ingest_batch`` row. So a connection is picked up once per connect, and a
    failing one waits for the regular tick instead of being retried every check.
    A history file import (``file_import``, WHO-104) is not a poll attempt.
    """
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT c.user_id FROM spotify_connection c "
                    "WHERE c.status = 'active' AND NOT EXISTS ("
                    "  SELECT 1 FROM ingest_batch b WHERE b.user_id = c.user_id "
                    "  AND b.source = :source AND b.ingest_method <> 'file_import' "
                    "  AND b.started_at >= c.authorized_at)"
                ),
                {"source": SOURCE},
            ).scalars()
        )


def connections_with_poll_request(engine: Engine) -> list[UUID]:
    """Active connections whose manual poll request is newer than their last attempt (WHO-95).

    ``POST /v1/me/spotify/poll`` sets ``poll_requested_at``; any poll attempt,
    failed or not, records an ``ingest_batch`` row started after it. So each
    request causes exactly one poll attempt. A history file import is not one.
    """
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT c.user_id FROM spotify_connection c "
                    "WHERE c.status = 'active' AND c.poll_requested_at IS NOT NULL "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM ingest_batch b WHERE b.user_id = c.user_id "
                    "  AND b.source = :source AND b.ingest_method <> 'file_import' "
                    "  AND b.started_at >= c.poll_requested_at)"
                ),
                {"source": SOURCE},
            ).scalars()
        )


def poll_connection(
    engine: Engine, client: SpotifyClient, user_id: UUID, now: datetime
) -> PollResult:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT c.refresh_token_ciphertext, c.collection_started_at, c.authorized_at, "
                "  c.history_import_requested AND c.history_imported_at IS NULL "
                "    AS history_import_due, "
                "  c.library_baseline_at IS NULL AS library_baseline_due, "
                "  u.home_timezone "
                "FROM spotify_connection c JOIN app_user u ON u.id = c.user_id "
                "WHERE c.user_id = :user_id AND c.status = 'active'"
            ),
            {"user_id": user_id},
        ).one_or_none()
    if row is None:
        return PollResult("failed", "not_active")
    _warn_if_grant_expires_soon(user_id, row.authorized_at, now)

    read_ciphertext = bytes(row.refresh_token_ciphertext)
    try:
        refresh_token = decrypt_refresh_token(user_id, read_ciphertext)
    except DecryptionError:
        # A key problem, not a dead grant: fixing TOKEN_ENCRYPTION_KEY recovers it.
        return _record_failure(engine, user_id, now, "token_decrypt_failed")

    try:
        grant = client.refresh(refresh_token)
    except InvalidGrant:
        with engine.begin() as conn:
            marked = mark_reauth_required(
                conn, user_id, "invalid_grant", read_ciphertext=read_ciphertext
            )
            _insert_failed_batch(conn, user_id, now, "invalid_grant")
        if not marked:
            # The user signed in again while we refreshed: the next tick uses the new grant.
            return PollResult("failed", "invalid_grant")
        return PollResult("reauth_required", "invalid_grant")
    except SpotifyError as exc:
        return _record_failure(engine, user_id, now, exc.kind)

    if grant.refresh_token is not None and grant.refresh_token != refresh_token:
        with engine.begin() as conn:
            store_rotated_refresh_token(
                conn, user_id, grant.refresh_token, read_ciphertext=read_ciphertext
            )

    if row.history_import_due:
        import_recent_history(engine, client, grant.access_token, user_id, row.home_timezone, now)

    if row.library_baseline_due and has_library_scopes(grant.scope):
        build_library_baseline(
            engine,
            client,
            grant.access_token,
            user_id,
            collection_started_at=row.collection_started_at,
            now=now,
        )

    cursor = _cursor(engine, user_id, row.collection_started_at)
    try:
        page = client.recently_played(grant.access_token, after=cursor)
    except SpotifyError as exc:
        return _record_failure(engine, user_id, now, exc.kind)

    plays = _readable_plays(page, user_id, "api_poll")
    kept = plays_made_while_connected(plays, row.collection_started_at)

    with engine.begin() as conn:
        lock_user_source(conn, user_id, SOURCE)
        batch_id = _insert_batch(conn, user_id, now, "api_poll")
        counts, skipped = _write_plays(
            conn, user_id, row.home_timezone, page, plays, kept, batch_id, now
        )
        conn.execute(
            text("UPDATE spotify_connection SET last_polled_at = :now WHERE user_id = :user_id"),
            {"now": now, "user_id": user_id},
        )

    # Counts only, never what was played (P12).
    logger.info(
        "spotify poll succeeded user_id=%s seen=%d inserted=%d skipped=%d",
        user_id,
        len(page.items),
        counts.inserted,
        skipped,
    )
    if len(page.items) >= RECENTLY_PLAYED_LIMIT:
        # Schema Q8: a full page may mean plays older than it were never listed.
        logger.warning("spotify poll returned a full page user_id=%s: possible gap", user_id)
    return PollResult("succeeded", None, len(page.items), counts.inserted, skipped)


def import_recent_history(
    engine: Engine,
    client: SpotifyClient,
    access_token: str,
    user_id: UUID,
    home_timezone: str,
    now: datetime,
) -> PollResult:
    """Store the latest 50 plays Spotify lists, once, as an ``api_backfill`` batch.

    Only reached when the user chose the import at their first connect. A play
    already stored is a no-op, so a second import, or one overlapping a poll,
    adds zero rows. Returns the outcome; a failure is recorded, not raised.
    """
    try:
        page = client.recently_played(access_token, after=None)
    except SpotifyError as exc:
        with engine.begin() as conn:
            _insert_failed_batch(conn, user_id, now, exc.kind, "api_backfill")
        logger.warning("spotify history import failed user_id=%s error_kind=%s", user_id, exc.kind)
        return PollResult("failed", exc.kind)

    plays = _readable_plays(page, user_id, "api_backfill")
    with engine.begin() as conn:
        lock_user_source(conn, user_id, SOURCE)
        # Read again under the lock: another poll may have imported meanwhile,
        # or the user may have disconnected.
        due = conn.execute(
            text(
                "SELECT history_import_requested AND history_imported_at IS NULL "
                "FROM spotify_connection WHERE user_id = :user_id AND status = 'active' "
                "FOR UPDATE"
            ),
            {"user_id": user_id},
        ).scalar_one_or_none()
        if not due:
            return PollResult("failed", "import_not_due")
        batch_id = _insert_batch(conn, user_id, now, "api_backfill")
        counts, skipped = _write_plays(
            conn, user_id, home_timezone, page, plays, plays, batch_id, now
        )
        conn.execute(
            text(
                "UPDATE spotify_connection SET history_imported_at = :now "
                "WHERE user_id = :user_id"
            ),
            {"now": now, "user_id": user_id},
        )

    # Counts only, never what was played (P12).
    logger.info(
        "spotify history import succeeded user_id=%s seen=%d inserted=%d skipped=%d",
        user_id,
        len(page.items),
        counts.inserted,
        skipped,
    )
    return PollResult("succeeded", None, len(page.items), counts.inserted, skipped)


def _readable_plays(
    page: RecentlyPlayed, user_id: UUID, ingest_method: IngestMethod
) -> list[SpotifyPlay]:
    plays: list[SpotifyPlay] = []
    for item in page.items:
        try:
            plays.append(play_from_item(item, ingest_method))
        except UnreadablePlay as exc:
            # The missing field's name only; the item may carry a title.
            logger.warning("spotify play skipped user_id=%s missing=%s", user_id, exc)
    return plays


def _write_plays(
    conn: Connection,
    user_id: UUID,
    home_timezone: str,
    page: RecentlyPlayed,
    plays: list[SpotifyPlay],
    kept: list[SpotifyPlay],
    batch_id: UUID,
    now: datetime,
) -> tuple[WriteCounts, int]:
    """Write ``kept`` and finish the batch with its counts. Caller holds the lock.

    A play is not stored when a ``file_import`` row of the same item lies within
    ``DEDUPE_WINDOW_SECONDS`` of it: the export's ``ts`` and ``played_at`` for
    the same play may differ by a few seconds, so the dedupe key, truncated to
    the second, can differ too (WHO-112). Rows from polls are left to the key.

    The window covers every readable play Spotify returned (``plays``), kept or not.
    Returns the counts and the number of items not stored.
    """
    counts = WriteCounts()
    imported = stored_near(conn, user_id, [p.event for p in kept], "file_import")
    for play in kept:
        if within_window(imported.get(play.event.item_key.value, []), play.event.occurred_at):
            continue
        write_precise_event(
            conn,
            user_id=user_id,
            home_timezone=home_timezone,
            event=play.event,
            ingest_batch_id=batch_id,
            counts=counts,
        )
    skipped = len(page.items) - counts.inserted - counts.upgraded
    played = [p.played_at for p in plays]
    conn.execute(
        text(
            "UPDATE ingest_batch SET status = 'succeeded', finished_at = :now, "
            "  window_start = :window_start, window_end = :window_end, "
            "  rows_seen = :seen, rows_inserted = :inserted, rows_upgraded = :upgraded, "
            "  rows_skipped = :skipped WHERE id = :id"
        ),
        {
            "now": now,
            "window_start": min(played, default=None),
            "window_end": max(played, default=None),
            "seen": len(page.items),
            "inserted": counts.inserted,
            "upgraded": counts.upgraded,
            "skipped": skipped,
            "id": batch_id,
        },
    )
    return counts, skipped


def _cursor(engine: Engine, user_id: UUID, collection_started_at: datetime) -> datetime:
    """The latest stored play, never earlier than just before the collection start.

    Plays from a file import (WHO-104) do not count: the export may hold plays
    after the latest poll, and a cursor past them would skip the plays between.
    """
    with engine.connect() as conn:
        last_play: datetime | None = conn.execute(
            text(
                "SELECT max(occurred_at) FROM unified_event "
                "WHERE user_id = :user_id AND source = :source "
                "AND ingest_method <> 'file_import'"
            ),
            {"user_id": user_id, "source": SOURCE},
        ).scalar_one()
    # Spotify's `after` is exclusive; one millisecond back keeps a play made
    # exactly at the collection start.
    floor = collection_started_at - _ONE_MS
    return floor if last_play is None else max(last_play, floor)


def _insert_batch(
    conn: Connection, user_id: UUID, now: datetime, ingest_method: IngestMethod
) -> UUID:
    batch_id: UUID = conn.execute(
        text(
            "INSERT INTO ingest_batch (user_id, source, source_artifact, ingest_method, "
            "  started_at, status) "
            "VALUES (:user_id, :source, :artifact, :method, :now, 'running') RETURNING id"
        ),
        {
            "user_id": user_id,
            "source": SOURCE,
            "artifact": SOURCE_ARTIFACT,
            "method": ingest_method,
            "now": now,
        },
    ).scalar_one()
    return batch_id


def _insert_failed_batch(
    conn: Connection,
    user_id: UUID,
    now: datetime,
    error_kind: str,
    ingest_method: IngestMethod = "api_poll",
) -> None:
    conn.execute(
        text(
            "INSERT INTO ingest_batch (user_id, source, source_artifact, ingest_method, "
            "  started_at, finished_at, status, error_kind) "
            "VALUES (:user_id, :source, :artifact, :method, :now, :now, 'failed', :kind)"
        ),
        {
            "user_id": user_id,
            "source": SOURCE,
            "artifact": SOURCE_ARTIFACT,
            "method": ingest_method,
            "now": now,
            "kind": error_kind,
        },
    )


def _record_failure(engine: Engine, user_id: UUID, now: datetime, error_kind: str) -> PollResult:
    with engine.begin() as conn:
        _insert_failed_batch(conn, user_id, now, error_kind)
    logger.warning("spotify poll failed user_id=%s error_kind=%s", user_id, error_kind)
    return PollResult("failed", error_kind)


# One reconnect warning per user per day, not one per tick.
_warned_on: dict[UUID, date] = {}


def _warn_if_grant_expires_soon(user_id: UUID, authorized_at: datetime, now: datetime) -> None:
    expires_at = grant_expires_at(authorized_at)
    if now < expires_at - timedelta(days=get_settings().reauth_warning_days):
        return
    if _warned_on.get(user_id) == now.date():
        return
    _warned_on[user_id] = now.date()
    logger.warning(
        "spotify grant expires soon user_id=%s expires_at=%s: the user must reconnect",
        user_id,
        expires_at.isoformat(),
    )
