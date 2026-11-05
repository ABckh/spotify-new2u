"""Account delete writes one ``account_deleted user_id=<id>`` line and nothing else
The restore replay in docs/self-hosting.md greps for exactly this.
"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from app.privacy import delete
from app.privacy.audit import USER_TABLE, live_tables
from app.privacy.delete import delete_all_user_data

SCHEMA = "privacy_delete_log"
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LINE = re.compile(rf"account_deleted user_id={UUID_RE}")


def test_delete_writes_one_line_holding_only_the_id(
    db: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    deleted, kept = _create_users(db, 2)
    caplog.set_level(logging.DEBUG)

    assert delete_all_user_data(db, deleted, SCHEMA) == 1

    lines = [r.getMessage() for r in caplog.records if r.name == delete.logger.name]
    assert lines == [f"account_deleted user_id={deleted}"]
    assert LINE.fullmatch(lines[0])
    # No other log record anywhere mentions the user, and the kept user is untouched.
    assert [r.getMessage() for r in caplog.records if str(deleted) in r.getMessage()] == lines
    assert db.execute(text(f"SELECT id FROM {SCHEMA}.app_user")).scalars().all() == [kept]


def test_delete_of_an_absent_account_writes_no_line(
    db: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    _create_users(db, 0)
    caplog.set_level(logging.DEBUG)

    assert delete_all_user_data(db, uuid4(), SCHEMA) == 0

    assert [r for r in caplog.records if r.name == delete.logger.name] == []


def test_delete_line_survives_a_raised_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.ERROR)

    assert delete.logger.isEnabledFor(logging.INFO)


def test_cli_replays_deletes_and_logs_each(
    db: Connection, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    if USER_TABLE in live_tables(db):
        pytest.skip("public.app_user exists; the SEEDERS delete test covers the real table")
    db.execute(text("CREATE TABLE public.app_user (id uuid PRIMARY KEY)"))
    present = uuid4()
    db.execute(text("INSERT INTO public.app_user VALUES (:u)"), {"u": present})
    monkeypatch.setattr(delete, "get_engine", lambda: _EngineOn(db))
    caplog.set_level(logging.DEBUG)

    assert delete.main([str(present), str(uuid4())]) == 0

    lines = [r.getMessage() for r in caplog.records if r.name == delete.logger.name]
    assert lines == [f"account_deleted user_id={present}"]
    assert db.execute(text("SELECT count(*) FROM public.app_user")).scalar_one() == 0


def test_cli_rejects_a_malformed_id_before_deleting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_engine() -> None:
        raise AssertionError("must not touch the database")

    monkeypatch.setattr(delete, "get_engine", no_engine)

    with pytest.raises(SystemExit) as exc:
        delete.main([str(uuid4()), "not-a-uuid"])
    assert exc.value.code == 2


class _EngineOn:
    """Stands in for the engine so the CLI runs inside the test's rolled-back transaction."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        yield self._conn


def _create_users(db: Connection, n: int) -> list[UUID]:
    db.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    db.execute(text(f"CREATE TABLE {SCHEMA}.app_user (id uuid PRIMARY KEY)"))
    users = [uuid4() for _ in range(n)]
    for user in users:
        db.execute(text(f"INSERT INTO {SCHEMA}.app_user VALUES (:u)"), {"u": user})
    return users
