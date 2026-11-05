"""The one-time import of the Spotify extended streaming history (WHO-94, WHO-104).

Lifecycle of a ``history_import`` row:

1. ``POST /v1/spotify/history-import`` saves the upload to
   ``<HISTORY_IMPORT_DIR>/<import id>.upload`` (0600) and inserts the row
   ``queued`` (:func:`create_import`). At most one row per user is queued,
   running or succeeded (a partial unique index): that is the one-time rule.
2. The poller's import worker claims the oldest queued row (``running``) and
   runs :func:`run_import`. It reads the whole file once to check every limit,
   so a file that breaks one stores nothing. It then reads it again and writes
   the plays in chunks of ``CHUNK_ROWS``, each in its own transaction under the
   per-(user, source) advisory lock, so a poll and the import never write at
   the same moment.
3. The row ends ``succeeded`` or ``failed`` with an error code. A file with no
   music play to store (every row skipped) ends ``failed`` with
   ``no_music_plays`` (R6). A failed import
   keeps the rows it wrote (they are valid plays, and a retry is idempotent),
   and the user may upload again. The upload file is deleted in ``finally``.

The import writes through ``app.events.store`` with the frozen dedupe key. On
top of it, a play is skipped when a Spotify row of the same item already exists
within ``DEDUPE_WINDOW_SECONDS``: the export's ``ts`` and the Web API's
``played_at`` for the same play may differ by a few seconds (decision 9).
The poller applies the same window the other way round: it skips a play when
a ``file_import`` row of the same item is stored within it (WHO-112).

The poll cursor is the latest *polled* play (``app.poller.spotify._cursor``),
so imported rows never move it, and a ``file_import`` collection record is not
a poll attempt for the first-poll and manual-poll checks.

Logs carry the import id, the user id, counts and error codes. Never a row,
a title, a file name or an exception message: a database error's text can
quote the row it failed on.
"""

import bisect
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.core.config import get_settings
from app.events.store import (
    IngestMethod,
    PreciseEvent,
    WriteCounts,
    lock_user_source,
    write_precise_event,
)
from app.spotify.extended_history import (
    SOURCE_ARTIFACT,
    ExportPlay,
    FileKind,
    ImportRejected,
    Skip,
    iter_rows,
    play_from_row,
)
from app.spotify.plays import SOURCE

logger = logging.getLogger("app.spotify.history_import")

# Decision 9. Start value until the spike on a real export sets it (WHO-94).
DEDUPE_WINDOW_SECONDS: Final = 10
# Decision 10: rows per transaction.
CHUNK_ROWS: Final = 5_000
# Decision 6: an upload file older than this is deleted whatever its state.
# 23 h, not 24 h: the poller sweeps hourly, so a file lives at most
# MAX_FILE_AGE + SWEEP_INTERVAL_SECONDS. The consent screen promises "never
# more than 24 hours" (WHO-103 §4 rule 3). The upload route also sweeps, so
# the limit holds while the poller is down.
MAX_FILE_AGE: Final = timedelta(hours=23)
SWEEP_INTERVAL_SECONDS: Final = 3600

Status = Literal["queued", "running", "succeeded", "failed"]
_DEDUPE_WINDOW = timedelta(seconds=DEDUPE_WINDOW_SECONDS)
_UPLOAD_SUFFIX = ".upload"
_PARTIAL_SUFFIX = ".part"


# --- files ------------------------------------------------------------------


def upload_dir() -> Path:
    """The private upload directory, created 0700 if missing."""
    configured = get_settings().history_import_dir
    path = (
        Path(configured) if configured else Path(tempfile.gettempdir()) / ("new2u-history-imports")
    )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def upload_path(import_id: UUID) -> Path:
    return upload_dir() / f"{import_id}{_UPLOAD_SUFFIX}"


def partial_path(import_id: UUID) -> Path:
    """Where the API writes an upload before its row exists."""
    return upload_dir() / f"{import_id}{_PARTIAL_SUFFIX}"


def open_private(path: Path) -> int:
    """Create ``path`` for writing, readable by this user only. Fails if it exists."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def sweep_old_files(now_epoch: float | None = None) -> int:
    """Delete upload files older than ``MAX_FILE_AGE`` (a crash left them). Returns how many."""
    cutoff = (time.time() if now_epoch is None else now_epoch) - MAX_FILE_AGE.total_seconds()
    removed = 0
    for path in upload_dir().iterdir():
        if path.suffix not in (_UPLOAD_SUFFIX, _PARTIAL_SUFFIX) or not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    if removed:
        logger.info("history import sweep removed=%d", removed)
    return removed


# --- rows -------------------------------------------------------------------


@dataclass(frozen=True)
class ImportCounts:
    rows_seen: int = 0
    rows_inserted: int = 0
    rows_duplicate: int = 0
    rows_skipped_not_music: int = 0
    rows_skipped_incognito: int = 0
    rows_skipped_short: int = 0
    rows_unreadable: int = 0


@dataclass(frozen=True)
class HistoryImport:
    id: UUID
    status: Status
    error_kind: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    counts: ImportCounts


_COUNT_COLUMNS = (
    "rows_seen, rows_inserted, rows_duplicate, rows_skipped_not_music, "
    "rows_skipped_incognito, rows_skipped_short, rows_unreadable"
)


def blocking_status(conn: Connection, user_id: UUID) -> Status | None:
    """``queued``/``running``/``succeeded`` if one stops a new upload, else ``None``."""
    status: Status | None = conn.execute(
        text(
            "SELECT status FROM history_import WHERE user_id = :user_id "
            "AND status IN ('queued', 'running', 'succeeded')"
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()
    return status


def create_import(
    conn: Connection, import_id: UUID, user_id: UUID, file_kind: FileKind, now: datetime
) -> Status | None:
    """Insert the row ``queued``. Returns ``None``, or the status of the import in the way."""
    created = conn.execute(
        text(
            "INSERT INTO history_import (id, user_id, file_kind, status, created_at) "
            "VALUES (:id, :user_id, :kind, 'queued', :now) "
            "ON CONFLICT (user_id) WHERE status IN ('queued', 'running', 'succeeded') "
            "DO NOTHING RETURNING id"
        ),
        {"id": import_id, "user_id": user_id, "kind": file_kind, "now": now},
    ).scalar_one_or_none()
    if created is not None:
        return None
    return blocking_status(conn, user_id) or "queued"


def latest_import(conn: Connection, user_id: UUID) -> HistoryImport | None:
    """The user's import that counts: the one not failed if any (at most one), else the latest."""
    row = conn.execute(
        text(
            "SELECT id, status, error_kind, created_at, started_at, finished_at, "
            f"{_COUNT_COLUMNS} FROM history_import WHERE user_id = :user_id "
            "ORDER BY status = 'failed', created_at DESC, id DESC LIMIT 1"
        ),
        {"user_id": user_id},
    ).one_or_none()
    if row is None:
        return None
    return HistoryImport(
        id=row.id,
        status=row.status,
        error_kind=row.error_kind,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        counts=ImportCounts(
            rows_seen=row.rows_seen,
            rows_inserted=row.rows_inserted,
            rows_duplicate=row.rows_duplicate,
            rows_skipped_not_music=row.rows_skipped_not_music,
            rows_skipped_incognito=row.rows_skipped_incognito,
            rows_skipped_short=row.rows_skipped_short,
            rows_unreadable=row.rows_unreadable,
        ),
    )


# --- the worker -------------------------------------------------------------


@dataclass(frozen=True)
class Claimed:
    id: UUID
    user_id: UUID
    file_kind: FileKind


class Stopped(Exception):
    """The poller is shutting down: the import goes back to the queue."""


def fail_interrupted(engine: Engine, now: datetime) -> int:
    """At worker start: imports left ``running`` by a crash end ``failed``.

    One poller process runs imports, so at its start none can be running. A
    crash may come from the file itself, so the import is not retried by
    itself; the user may upload again. Returns how many were failed.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "UPDATE history_import SET status = 'failed', error_kind = 'interrupted', "
                "  finished_at = :now WHERE status = 'running' RETURNING id, ingest_batch_id"
            ),
            {"now": now},
        ).all()
        for row in rows:
            _fail_batch(conn, row.ingest_batch_id, now, "interrupted")
    for row in rows:
        upload_path(row.id).unlink(missing_ok=True)
        logger.warning("history import failed import_id=%s error_kind=interrupted", row.id)
    return len(rows)


def claim_next(engine: Engine, now: datetime) -> Claimed | None:
    """Move the oldest queued import to ``running``; ``None`` if none is queued."""
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE history_import SET status = 'running', started_at = :now "
                "WHERE id = (SELECT id FROM history_import WHERE status = 'queued' "
                "            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING id, user_id, file_kind"
            ),
            {"now": now},
        ).one_or_none()
    return None if row is None else Claimed(row.id, row.user_id, row.file_kind)


@dataclass
class _Tally:
    seen: int = 0
    duplicate: int = 0
    skipped: dict[Skip, int] = field(default_factory=lambda: dict.fromkeys(Skip, 0))
    writes: WriteCounts = field(default_factory=WriteCounts)
    window_start: datetime | None = None
    window_end: datetime | None = None

    def counts(self) -> ImportCounts:
        return ImportCounts(
            rows_seen=self.seen,
            rows_inserted=self.writes.inserted + self.writes.upgraded,
            rows_duplicate=self.duplicate + self.writes.skipped,
            rows_skipped_not_music=self.skipped[Skip.NOT_MUSIC],
            rows_skipped_incognito=self.skipped[Skip.INCOGNITO],
            rows_skipped_short=self.skipped[Skip.SHORT],
            rows_unreadable=self.skipped[Skip.UNREADABLE],
        )


def run_import(
    engine: Engine,
    claimed: Claimed,
    clock: Callable[[], datetime],
    stop: threading.Event | None = None,
) -> Status:
    """Run one claimed import to its end. Returns ``succeeded``, ``failed`` or ``queued``.

    ``queued`` means ``stop`` was set: the import goes back to the queue with
    its file, and the next worker start runs it again (writes are idempotent).
    """
    path = upload_path(claimed.id)
    requeued = False
    batch_id: UUID | None = None
    try:
        if not path.is_file():
            raise ImportRejected("upload_missing")
        # Pass 1: every limit, before anything is stored (decision 5).
        for _ in iter_rows(path, claimed.file_kind):
            pass
        home_timezone, batch_id = _start_batch(engine, claimed, clock())
        tally = _write_all(engine, claimed, path, home_timezone, batch_id, stop)
        counts = tally.counts()
        if counts.rows_inserted + counts.rows_duplicate == 0:
            # R6: no music play at all (only podcasts, private sessions or
            # short plays). Failed, so the one import is not used up.
            raise ImportRejected("no_music_plays")
        _finish(engine, claimed, batch_id, tally, clock())
        logger.info(
            "history import succeeded import_id=%s user_id=%s seen=%d inserted=%d "
            "duplicate=%d skipped=%d",
            claimed.id,
            claimed.user_id,
            tally.seen,
            tally.counts().rows_inserted,
            tally.counts().rows_duplicate,
            sum(tally.skipped.values()),
        )
        return "succeeded"
    except Stopped:
        _requeue(engine, claimed, batch_id, clock())
        requeued = True
        logger.info("history import requeued for shutdown import_id=%s", claimed.id)
        return "queued"
    except ImportRejected as exc:
        _fail(engine, claimed, batch_id, clock(), exc.code)
        return "failed"
    except Exception as exc:
        # The type only: a database error's message can quote the failing row.
        logger.error(
            "history import crashed import_id=%s user_id=%s error=%s",
            claimed.id,
            claimed.user_id,
            type(exc).__name__,
        )
        _fail(engine, claimed, batch_id, clock(), "internal_error")
        return "failed"
    finally:
        if not requeued:
            path.unlink(missing_ok=True)


def _start_batch(engine: Engine, claimed: Claimed, now: datetime) -> tuple[str, UUID]:
    with engine.begin() as conn:
        home_timezone: str = conn.execute(
            text("SELECT home_timezone FROM app_user WHERE id = :user_id"),
            {"user_id": claimed.user_id},
        ).scalar_one()
        batch_id: UUID = conn.execute(
            text(
                "INSERT INTO ingest_batch (user_id, source, source_artifact, ingest_method, "
                "  started_at, status) "
                "VALUES (:user_id, :source, :artifact, 'file_import', :now, 'running') "
                "RETURNING id"
            ),
            {"user_id": claimed.user_id, "source": SOURCE, "artifact": SOURCE_ARTIFACT, "now": now},
        ).scalar_one()
        conn.execute(
            text("UPDATE history_import SET ingest_batch_id = :batch WHERE id = :id"),
            {"batch": batch_id, "id": claimed.id},
        )
    return home_timezone, batch_id


def _write_all(
    engine: Engine,
    claimed: Claimed,
    path: Path,
    home_timezone: str,
    batch_id: UUID,
    stop: threading.Event | None,
) -> _Tally:
    """Pass 2: map every row and write the plays, ``CHUNK_ROWS`` per transaction."""
    tally = _Tally()
    chunk: list[ExportPlay] = []
    for row in iter_rows(path, claimed.file_kind):
        tally.seen += 1
        mapped = play_from_row(row)
        if isinstance(mapped, Skip):
            tally.skipped[mapped] += 1
        else:
            chunk.append(mapped)
        if tally.seen % CHUNK_ROWS == 0:
            _write_chunk(engine, claimed, home_timezone, batch_id, chunk, tally)
            chunk = []
            if stop is not None and stop.is_set():
                raise Stopped
    _write_chunk(engine, claimed, home_timezone, batch_id, chunk, tally)
    return tally


def _write_chunk(
    engine: Engine,
    claimed: Claimed,
    home_timezone: str,
    batch_id: UUID,
    plays: list[ExportPlay],
    tally: _Tally,
) -> None:
    with engine.begin() as conn:
        if plays:
            lock_user_source(conn, claimed.user_id, SOURCE)
            nearby = stored_near(conn, claimed.user_id, [p.event for p in plays])
            for play in plays:
                event = play.event
                times = nearby.setdefault(event.item_key.value, [])
                if within_window(times, event.occurred_at):
                    tally.duplicate += 1
                    continue
                write_precise_event(
                    conn,
                    user_id=claimed.user_id,
                    home_timezone=home_timezone,
                    event=event,
                    ingest_batch_id=batch_id,
                    counts=tally.writes,
                )
                bisect.insort(times, event.occurred_at)
                tally.window_start = min(tally.window_start or event.occurred_at, event.occurred_at)
                tally.window_end = max(tally.window_end or event.occurred_at, event.occurred_at)
        # Counts so far, so the status route shows progress.
        _store_counts(conn, claimed.id, tally.counts())


def stored_near(
    conn: Connection,
    user_id: UUID,
    events: Sequence[PreciseEvent],
    ingest_method: IngestMethod | None = None,
) -> dict[str, list[datetime]]:
    """Stored Spotify play times per item key, for the events' items and time span.

    ``ingest_method`` limits it to rows written that way (the poller asks for
    ``file_import`` rows only, WHO-112). The times are sorted, for :func:`within_window`.
    """
    if not events:
        return {}
    rows = conn.execute(
        text(
            "SELECT item_key, occurred_at FROM unified_event "
            "WHERE user_id = :user_id AND source = :source "
            "  AND occurred_at BETWEEN :start AND :end "
            "  AND item_key = ANY(:keys) "
            "  AND (CAST(:ingest_method AS text) IS NULL OR ingest_method = :ingest_method)"
        ),
        {
            "user_id": user_id,
            "source": SOURCE,
            "start": min(e.occurred_at for e in events) - _DEDUPE_WINDOW,
            "end": max(e.occurred_at for e in events) + _DEDUPE_WINDOW,
            "keys": list({e.item_key.value for e in events}),
            "ingest_method": ingest_method,
        },
    )
    nearby: dict[str, list[datetime]] = {}
    for row in rows:
        nearby.setdefault(row.item_key, []).append(row.occurred_at)
    for times in nearby.values():
        times.sort()
    return nearby


def within_window(times: list[datetime], at: datetime) -> bool:
    """Whether a time in sorted ``times`` lies within ``DEDUPE_WINDOW_SECONDS`` of ``at``."""
    i = bisect.bisect_left(times, at - _DEDUPE_WINDOW)
    return i < len(times) and times[i] <= at + _DEDUPE_WINDOW


def _store_counts(conn: Connection, import_id: UUID, counts: ImportCounts) -> None:
    conn.execute(
        text(
            "UPDATE history_import SET rows_seen = :rows_seen, rows_inserted = :rows_inserted, "
            "  rows_duplicate = :rows_duplicate, "
            "  rows_skipped_not_music = :rows_skipped_not_music, "
            "  rows_skipped_incognito = :rows_skipped_incognito, "
            "  rows_skipped_short = :rows_skipped_short, rows_unreadable = :rows_unreadable "
            "WHERE id = :id"
        ),
        {"id": import_id, **counts.__dict__},
    )


def _finish(engine: Engine, claimed: Claimed, batch_id: UUID, tally: _Tally, now: datetime) -> None:
    writes = tally.writes
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ingest_batch SET status = 'succeeded', finished_at = :now, "
                "  window_start = :window_start, window_end = :window_end, "
                "  rows_seen = :seen, rows_inserted = :inserted, rows_upgraded = :upgraded, "
                "  rows_skipped = :skipped WHERE id = :id"
            ),
            {
                "now": now,
                "window_start": tally.window_start,
                "window_end": tally.window_end,
                "seen": tally.seen,
                "inserted": writes.inserted,
                "upgraded": writes.upgraded,
                "skipped": tally.seen - writes.inserted - writes.upgraded,
                "id": batch_id,
            },
        )
        _store_counts(conn, claimed.id, tally.counts())
        conn.execute(
            text(
                "UPDATE history_import SET status = 'succeeded', finished_at = :now "
                "WHERE id = :id"
            ),
            {"now": now, "id": claimed.id},
        )


def _fail(
    engine: Engine, claimed: Claimed, batch_id: UUID | None, now: datetime, error_kind: str
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE history_import SET status = 'failed', error_kind = :kind, "
                "  finished_at = :now WHERE id = :id"
            ),
            {"kind": error_kind, "now": now, "id": claimed.id},
        )
        _fail_batch(conn, batch_id, now, error_kind)
    logger.warning(
        "history import failed import_id=%s user_id=%s error_kind=%s",
        claimed.id,
        claimed.user_id,
        error_kind,
    )


def _requeue(engine: Engine, claimed: Claimed, batch_id: UUID | None, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE history_import SET status = 'queued', started_at = NULL, "
                f"  ingest_batch_id = NULL, ({_COUNT_COLUMNS}) = (0, 0, 0, 0, 0, 0, 0) "
                "WHERE id = :id"
            ),
            {"id": claimed.id},
        )
        _fail_batch(conn, batch_id, now, "interrupted")


def _fail_batch(conn: Connection, batch_id: UUID | None, now: datetime, error_kind: str) -> None:
    if batch_id is None:
        return
    conn.execute(
        text(
            "UPDATE ingest_batch SET status = 'failed', finished_at = :now, error_kind = :kind "
            "WHERE id = :id AND status = 'running'"
        ),
        {"now": now, "kind": error_kind, "id": batch_id},
    )
