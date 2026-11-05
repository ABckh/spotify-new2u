"""PRD P10 / B10: collection records older than the window are removed without user action.

``ingest_batch`` lands with the schema v1 migration (WHO-6). Until then these
tests create it in a throwaway schema from the schema v1 DDL (WHO-2 §5),
without the ``app_user`` foreign key, which the purge does not depend on.
"""

import threading
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import Connection, text

from app.poller import main as poller
from app.privacy.retention import purge_collection_records

SCHEMA = "retention_test"
WINDOW = timedelta(days=90)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def batches(db: Connection) -> Connection:
    db.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    db.execute(
        text(
            f"CREATE TABLE {SCHEMA}.ingest_batch ("
            "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
            "  user_id uuid NOT NULL,"
            "  source text NOT NULL,"
            "  source_artifact text NOT NULL,"
            "  ingest_method text NOT NULL,"
            "  started_at timestamptz NOT NULL DEFAULT now(),"
            "  finished_at timestamptz,"
            "  status text NOT NULL DEFAULT 'running')"
        )
    )
    return db


def _insert(conn: Connection, started_at: datetime) -> None:
    conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.ingest_batch "
            "(user_id, source, source_artifact, ingest_method, started_at, status) "
            "VALUES (:user_id, 'spotify', 'recently_played', 'api_poll', :started_at, "
            "'succeeded')"
        ),
        {"user_id": uuid4(), "started_at": started_at},
    )


def _started_ats(conn: Connection) -> list[datetime]:
    rows = conn.execute(text(f"SELECT started_at FROM {SCHEMA}.ingest_batch ORDER BY 1"))
    return [row.started_at for row in rows]


def test_purge_removes_collection_records_older_than_the_window(batches: Connection) -> None:
    _insert(batches, NOW - WINDOW - timedelta(days=1))
    _insert(batches, NOW - WINDOW - timedelta(seconds=1))
    _insert(batches, NOW - WINDOW)
    _insert(batches, NOW - timedelta(days=1))

    deleted = purge_collection_records(batches, now=NOW, retention=WINDOW, schema=SCHEMA)

    assert deleted == 2
    assert _started_ats(batches) == [NOW - WINDOW, NOW - timedelta(days=1)]


def test_purge_cutoff_is_an_instant_whatever_the_timezones(batches: Connection) -> None:
    """A session in US Pacific and a ``now`` in UTC+3 must give the same cutoff."""
    batches.execute(text("SET LOCAL TimeZone = 'America/Los_Angeles'"))
    _insert(batches, NOW - WINDOW + timedelta(minutes=1))
    _insert(batches, NOW - WINDOW - timedelta(minutes=1))
    now_in_vilnius = NOW.astimezone(timezone(timedelta(hours=3)))

    deleted = purge_collection_records(batches, now=now_in_vilnius, retention=WINDOW, schema=SCHEMA)

    assert deleted == 1
    assert _started_ats(batches) == [NOW - WINDOW + timedelta(minutes=1)]


def test_purge_refuses_a_naive_now(batches: Connection) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        purge_collection_records(
            batches, now=NOW.replace(tzinfo=None), retention=WINDOW, schema=SCHEMA
        )


def test_purge_is_a_no_op_before_the_table_exists(db: Connection) -> None:
    db.execute(text(f"CREATE SCHEMA {SCHEMA}"))

    assert purge_collection_records(db, now=NOW, retention=WINDOW, schema=SCHEMA) == 0


class _OneTick(threading.Event):
    """Stops the poller loop after its first tick, without waiting."""

    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return True


def test_poller_purges_on_every_tick_even_when_the_poll_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purges: list[None] = []

    def failing_poll() -> None:
        raise RuntimeError("synthetic poll failure")

    monkeypatch.setattr(poller, "_shutdown", _OneTick())
    monkeypatch.setattr(poller, "poll_once", failing_poll)
    monkeypatch.setattr(poller, "purge_once", lambda: purges.append(None))

    assert poller.run() == 0
    assert purges == [None]
