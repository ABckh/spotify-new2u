"""Poller entrypoint.

Run with: python -m app.poller

A separate process from the API on purpose: the API can be restarted or can fall
over without stopping ingestion. Spotify only exposes the last 50 plays, so every
hour this process is down is history that is lost for good. The loop therefore
never exits on a per-tick error — it logs and waits for the next tick — and it
shuts down cleanly on SIGTERM so a redeploy does not lose a tick mid-write.

Each tick polls every active Spotify connection (``app.poller.spotify``), then
removes collection records past their retention window (PRD P10) and expired
sign-ins and sessions. Between ticks it checks every few seconds for a
connection not polled since its user connected, and polls that one at once, so
a new user does not wait up to a whole interval for the first poll (WHO-87).
The same check polls a connection whose user asked for a poll now
(``POST /v1/me/spotify/poll``, ``spotify_connection.poll_requested_at``) and
has had no poll attempt since (WHO-95). Every attempt, failed or not, writes an
``ingest_batch`` row, so each request or connect causes exactly one poll. The
API enforces the cooldown between requests; the poller only acts on them.

A second thread runs the extended history file imports (WHO-104,
``app.spotify.history_import``): it picks queued imports every
``HISTORY_IMPORT_CHECK_SECONDS``, and deletes upload files older than 23 h at
its start and every hour. An import can take minutes, so it never runs on the
polling thread: polls are worth more than an import, which can wait. On
SIGTERM it stops between chunks and puts the import back in the queue.
"""

import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import FrameType

import httpx

from app.auth.login import purge_expired_logins
from app.auth.sessions import purge_expired_sessions
from app.core.config import get_settings
from app.core.db import get_engine
from app.core.log import configure_logging
from app.poller.spotify import (
    active_connections,
    connections_awaiting_first_poll,
    connections_with_poll_request,
    poll_connection,
)
from app.privacy.retention import purge_collection_records
from app.spotify import history_import
from app.spotify.client import SpotifyClient, build_spotify_client

logger = logging.getLogger("app.poller")

_shutdown = threading.Event()


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    logger.info(
        "received signal %s, finishing current tick then exiting",
        signal.Signals(signum).name,
    )
    _shutdown.set()


def poll_once(client: SpotifyClient) -> None:
    """Poll every active Spotify connection once. One user's failure never stops another's."""
    engine = get_engine()
    for user_id in active_connections(engine):
        try:
            poll_connection(engine, client, user_id, now=datetime.now(UTC))
        except Exception:
            logger.exception("spotify poll crashed user_id=%s", user_id)


def poll_new_connections(client: SpotifyClient) -> None:
    """Poll the connections not polled since their user connected or asked for a poll."""
    engine = get_engine()
    # dict.fromkeys: one poll for a user who both just connected and asked.
    due = dict.fromkeys(
        connections_awaiting_first_poll(engine) + connections_with_poll_request(engine)
    )
    for user_id in due:
        try:
            poll_connection(engine, client, user_id, now=datetime.now(UTC))
        except Exception:
            logger.exception("spotify between-ticks poll crashed user_id=%s", user_id)


def purge_once() -> None:
    """Remove collection records past retention (PRD P10), and expired sign-ins and sessions."""
    retention = timedelta(days=get_settings().collection_records_retention_days)
    now = datetime.now(UTC)
    with get_engine().begin() as conn:
        purge_collection_records(conn, now=now, retention=retention)
        purge_expired_logins(conn, now)
        purge_expired_sessions(conn, now)


def run_queued_imports(stop: threading.Event) -> None:
    """Run queued history imports one after another until none is left or ``stop``."""
    engine = get_engine()
    while not stop.is_set():
        claimed = history_import.claim_next(engine, datetime.now(UTC))
        if claimed is None:
            return
        history_import.run_import(engine, claimed, lambda: datetime.now(UTC), stop)


def import_worker(stop: threading.Event, check_seconds: int) -> None:
    """The import thread's loop. Like the poll loop, it never exits on an error."""
    try:
        history_import.fail_interrupted(get_engine(), datetime.now(UTC))
    except Exception as exc:
        logger.error("history import recovery failed error=%s", type(exc).__name__)
    last_sweep = -float("inf")
    while not stop.is_set():
        if time.monotonic() - last_sweep >= history_import.SWEEP_INTERVAL_SECONDS:
            try:
                history_import.sweep_old_files()
            except Exception as exc:
                logger.error("history import sweep failed error=%s", type(exc).__name__)
            last_sweep = time.monotonic()
        try:
            run_queued_imports(stop)
        except Exception as exc:
            # The type only: the message of a database error can quote a row.
            logger.error("history import check failed error=%s", type(exc).__name__)
        stop.wait(timeout=check_seconds)


def run() -> int:
    settings = get_settings()
    configure_logging()
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    logger.info("poller starting, interval=%ss", settings.poller_interval_seconds)
    # Its own event, set once the poll loop has stopped: the poll loop's
    # shutdown is never decided by the import thread.
    stop_imports = threading.Event()
    imports = threading.Thread(
        target=import_worker,
        args=(stop_imports, settings.history_import_check_seconds),
        name="history-import",
        daemon=True,
    )
    imports.start()
    try:
        with httpx.Client(timeout=30.0) as http:
            return _loop(
                http, settings.poller_interval_seconds, settings.poller_new_connection_check_seconds
            )
    finally:
        stop_imports.set()
        # Stops after the current chunk (CHUNK_ROWS rows), well inside the grace period.
        imports.join(timeout=20)


def _loop(http: httpx.Client, interval_seconds: int, check_seconds: int) -> int:
    while not _shutdown.is_set():
        try:
            # Built per tick, so a missing SPOTIFY_* setting is logged every tick
            # instead of killing the process; fixing .env and restarting recovers.
            poll_once(build_spotify_client(http))
        except Exception:
            # A failed tick must never kill the process: the next tick can still
            # catch the plays this one missed, as long as we are back inside the
            # 50-play window.
            logger.exception("poll tick failed")
        # Separate from the poll: a purge failure must not cost a poll, and a
        # poll failure must not stop the purge.
        try:
            purge_once()
        except Exception:
            logger.exception("collection records purge failed")
        _wait_for_next_tick(http, interval_seconds, check_seconds)

    logger.info("poller stopped")
    return 0


def _wait_for_next_tick(http: httpx.Client, interval_seconds: int, check_seconds: int) -> None:
    """Wait out the interval, polling new connections and requests every ``check_seconds``."""
    deadline = time.monotonic() + interval_seconds
    while not _shutdown.wait(timeout=max(0.0, min(check_seconds, deadline - time.monotonic()))):
        if time.monotonic() >= deadline:
            return
        try:
            poll_new_connections(build_spotify_client(http))
        except Exception:
            logger.exception("new connection check failed")


if __name__ == "__main__":
    sys.exit(run())
