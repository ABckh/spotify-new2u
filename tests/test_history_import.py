"""The one-time Spotify extended history import, end to end (WHO-104).

Against the real database: the one-time rule is a partial unique index, the
dedupe is the frozen key plus a time-window guard, and both live there. Every
upload is a synthetic export built in ``tmp_path`` (``history_export``); the
worker is driven directly (``claim_next`` + ``run_import``), as the poller's
import thread does.
"""

import logging
import os
import stat
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api import history_import as history_import_api
from app.api.auth import utc_now
from app.api.main import app
from app.core.config import get_settings
from app.events.keys import ItemKey
from app.events.store import PreciseEvent, WriteCounts, write_precise_event
from app.poller import main as poller_main
from app.poller import spotify as poller_spotify
from app.privacy.audit import live_tables
from app.privacy.stored_fields import STORED_FIELDS, SYSTEM_TABLES, Category
from app.spotify import extended_history as eh
from app.spotify import history_import as hi
from app.spotify.connection import connect
from app.spotify.manual_poll import poll_state
from app.spotify.profile import SpotifyProfile
from fake_spotify import FakeSpotify
from history_export import (
    AUDIO_ENTRY,
    SENTINELS,
    export_zip,
    json_bytes,
    podcast_row,
    row,
    track_id,
    write_zip,
)
from read_helpers import add_session

URL = "/v1/spotify/history-import"
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # the connect moment
NOW = T0 + timedelta(days=3)


@pytest.fixture
def imports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "history-imports"
    monkeypatch.setattr(get_settings(), "history_import_dir", str(path))
    return path


@pytest.fixture
def client(api: TestClient, imports_dir: Path) -> Iterator[TestClient]:
    app.dependency_overrides[utc_now] = lambda: NOW
    yield api


class Account:
    def __init__(self, engine: Engine, account_id: str = "synthetic-account-1") -> None:
        token = f"synthetic-session-{account_id}"
        with engine.begin() as conn:
            self.id: UUID = connect(
                conn,
                profile=SpotifyProfile(account_id, "Synthetic Listener"),
                refresh_token="synthetic-refresh-0",
                home_timezone="Europe/Helsinki",
                now=T0,
            ).user_id
            add_session(conn, self.id, token)
        self.auth = {"Authorization": f"Bearer {token}"}


@pytest.fixture
def account(engine: Engine) -> Account:
    return Account(engine)


def upload(client: TestClient, account: Account, path: Path, filename: str | None = None) -> Any:
    return client.post(
        URL,
        headers=account.auth,
        files={"file": (filename or path.name, path.read_bytes(), "application/octet-stream")},
    )


def accepted(client: TestClient, account: Account, path: Path) -> UUID:
    response = upload(client, account, path)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    return UUID(response.json()["id"])


def run_worker(engine: Engine, stop: threading.Event | None = None) -> str | None:
    claimed = hi.claim_next(engine, NOW)
    if claimed is None:
        return None
    return hi.run_import(engine, claimed, lambda: NOW, stop)


def latest(client: TestClient, account: Account) -> dict[str, Any]:
    response = client.get(URL, headers=account.auth)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()["latest"]
    return body


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def events(engine: Engine, user: UUID) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT item_key, occurred_at, title, creators, collection, played_ms, "
                    "  duration_ms, source, source_artifact, ingest_method, ingest_batch_id, "
                    "  external_ids "
                    "FROM unified_event WHERE user_id = :u ORDER BY occurred_at, item_key"
                ),
                {"u": user},
            )
        )


def api_play(engine: Engine, account: Account, at: datetime, n: int) -> None:
    """A play stored by the poller, as ``recently-played`` reports it."""
    with engine.begin() as conn:
        write_precise_event(
            conn,
            user_id=account.id,
            home_timezone="Europe/Helsinki",
            event=PreciseEvent(
                source="spotify",
                source_artifact="spotify_recently_played",
                ingest_method="api_poll",
                kind="listen",
                occurred_at=at,
                title=f"Synthetic Song {n}",
                item_key=ItemKey(f"id:{track_id(n)}", "provider_id"),
                duration_ms=200_000,
            ),
            ingest_batch_id=None,
            counts=WriteCounts(),
        )


def files_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def mixed_export(path: Path) -> Path:
    """Three music plays, a repeated one, and one row for each skip reason."""
    first = [
        row("2024-01-01T10:00:00Z", 1),
        row("2024-01-01T10:05:00Z", 2),
        podcast_row("2024-01-01T10:06:00Z"),
        row("2024-01-01T10:09:00Z", 4, incognito_mode=True),
        row("2024-01-01T10:10:00Z", 5, ms_played=10_000),
    ]
    second = [
        row("2024-02-01T10:00:00Z", 3),
        row("2024-01-01T10:00:00Z", 1),  # the same play again, in the next file
        row("2024-02-01T10:05:00Z", 6) | {"ts": None},
    ]
    return export_zip(path, first, second)


MIXED_COUNTS = {
    "rows_seen": 8,
    "rows_inserted": 3,
    "rows_duplicate": 1,
    "rows_skipped_not_music": 1,
    "rows_skipped_incognito": 1,
    "rows_skipped_short": 1,
    "rows_unreadable": 1,
}


# --- the happy path ---------------------------------------------------------


def test_a_zip_is_queued_then_imported(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    import_id = accepted(client, account, mixed_export(tmp_path / "my_spotify_data.zip"))

    # Saved privately while it waits.
    saved = imports_dir / f"{import_id}.upload"
    assert files_in(imports_dir) == [saved.name]
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(imports_dir.stat().st_mode) == 0o700
    assert latest(client, account)["status"] == "queued"
    assert events(engine, account.id) == []

    assert run_worker(engine) == "succeeded"

    status = latest(client, account)
    assert status["id"] == str(import_id)
    assert status["status"] == "succeeded"
    assert status["error_kind"] is None
    assert status["counts"] == MIXED_COUNTS
    stored = events(engine, account.id)
    assert [(e.item_key, e.occurred_at) for e in stored] == [
        (f"id:{track_id(1)}", datetime(2024, 1, 1, 10, 0, tzinfo=UTC)),
        (f"id:{track_id(2)}", datetime(2024, 1, 1, 10, 5, tzinfo=UTC)),
        (f"id:{track_id(3)}", datetime(2024, 2, 1, 10, 0, tzinfo=UTC)),
    ]
    first = stored[0]
    assert first.title == "Synthetic Song 1"
    assert first.creators == ["Nova Kett"]
    assert first.collection == "Synthetic Album"
    assert first.played_ms == 200_000
    assert first.duration_ms is None
    assert (first.source, first.source_artifact, first.ingest_method) == (
        "spotify",
        "spotify_extended_history",
        "file_import",
    )
    assert first.external_ids == {"spotify_track_id": track_id(1)}

    # One collection record for the whole import, linked from the import.
    with engine.connect() as conn:
        batch = conn.execute(
            text("SELECT * FROM ingest_batch WHERE user_id = :u"), {"u": account.id}
        ).one()
        linked = conn.execute(
            text("SELECT ingest_batch_id FROM history_import WHERE id = :id"), {"id": import_id}
        ).scalar_one()
    assert linked == batch.id
    assert {e.ingest_batch_id for e in stored} == {batch.id}
    assert (batch.source_artifact, batch.ingest_method, batch.status) == (
        "spotify_extended_history",
        "file_import",
        "succeeded",
    )
    assert (batch.rows_seen, batch.rows_inserted, batch.rows_upgraded, batch.rows_skipped) == (
        8,
        3,
        0,
        5,
    )
    assert batch.window_start == datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    assert batch.window_end == datetime(2024, 2, 1, 10, 0, tzinfo=UTC)

    # The upload is gone once the job ends.
    assert files_in(imports_dir) == []


def test_a_single_json_file_is_imported(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "Streaming_History_Audio_2024.json"
    path.write_bytes(json_bytes([row("2024-01-01T10:00:00Z", 1), row("2024-01-02T10:00:00Z", 2)]))
    accepted(client, account, path)

    assert run_worker(engine) == "succeeded"

    assert latest(client, account)["counts"]["rows_inserted"] == 2
    assert len(events(engine, account.id)) == 2
    assert files_in(imports_dir) == []


def test_no_import_yet_is_null(client: TestClient, account: Account) -> None:
    response = client.get(URL, headers=account.auth)

    assert response.status_code == 200
    assert response.json() == {"latest": None}


def test_the_routes_need_a_session(client: TestClient, tmp_path: Path) -> None:
    path = export_zip(tmp_path / "e.zip", [row("2024-01-01T10:00:00Z")])

    assert client.get(URL).status_code == 401
    response = client.post(URL, files={"file": ("e.zip", path.read_bytes(), "application/zip")})
    assert response.status_code == 401


# --- privacy ----------------------------------------------------------------


def test_dropped_fields_never_reach_the_database(
    client: TestClient, engine: Engine, account: Account, tmp_path: Path
) -> None:
    accepted(client, account, mixed_export(tmp_path / "e.zip"))
    assert run_worker(engine) == "succeeded"

    with engine.connect() as conn:
        tables = sorted(live_tables(conn) - SYSTEM_TABLES)
        dump = "\n".join(
            str(value)
            for table in tables
            for value in conn.execute(text(f'SELECT t::text FROM "{table}" t')).scalars()
        )
    for field, value in SENTINELS.items():
        assert str(value) not in dump, field
    # Neither the video history nor Userdata.json was read (track 999 lives there).
    assert track_id(999) not in dump
    assert "Synthetic Episode" not in dump


def test_logs_carry_no_file_content(
    client: TestClient,
    engine: Engine,
    account: Account,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    accepted(client, account, mixed_export(tmp_path / "e.zip"))
    assert run_worker(engine) == "succeeded"

    # A crash whose message quotes the row, as a database error's can.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM history_import"))

    def crash(*_args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            f"Failing row contains ({kwargs['event'].title}, {SENTINELS['ip_addr']})"
        )

    monkeypatch.setattr(hi, "write_precise_event", crash)
    other_plays = [row("2024-05-01T10:00:00Z", 50), row("2024-05-01T10:05:00Z", 51)]
    accepted(client, account, export_zip(tmp_path / "e2.zip", other_plays))
    assert run_worker(engine) == "failed"

    assert latest(client, account)["error_kind"] == "internal_error"
    assert "history import crashed" in caplog.text
    assert "Synthetic Song" not in caplog.text
    assert "Synthetic Album" not in caplog.text
    for value in SENTINELS.values():
        assert str(value) not in caplog.text
    assert "my_spotify_data" not in caplog.text and "e2.zip" not in caplog.text


def test_delete_account_removes_the_import_and_its_waiting_file(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    accepted(client, account, mixed_export(tmp_path / "e.zip"))
    assert files_in(imports_dir) != []

    assert client.delete("/v1/me", headers=account.auth).status_code == 204

    assert scalar(engine, "SELECT count(*) FROM history_import") == 0
    assert files_in(imports_dir) == []
    assert run_worker(engine) is None


def test_the_export_lists_the_import(
    client: TestClient, engine: Engine, account: Account, tmp_path: Path
) -> None:
    import_id = accepted(client, account, mixed_export(tmp_path / "e.zip"))
    assert run_worker(engine) == "succeeded"

    response = client.get("/v1/export", headers=account.auth)

    assert response.status_code == 200
    (entry,) = response.json()["history_imports"]
    assert entry["id"] == str(import_id)
    assert entry["status"] == "succeeded"
    assert entry["file_kind"] == "zip"
    assert {k: entry[k] for k in MIXED_COUNTS} == MIXED_COUNTS


def test_imported_plays_before_the_connect_draw_the_timeline_marker(
    client: TestClient, engine: Engine, account: Account, tmp_path: Path
) -> None:
    before = client.get("/v1/me/connection", headers=account.auth).json()
    assert before["has_imported_plays"] is False
    accepted(client, account, mixed_export(tmp_path / "e.zip"))
    assert run_worker(engine) == "succeeded"

    after = client.get("/v1/me/connection", headers=account.auth).json()

    assert after["has_imported_plays"] is True


# --- one time per user ------------------------------------------------------


def test_a_file_without_music_plays_fails_and_can_be_retried(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    """R6 (WHO-106 F3): a file that stores nothing does not use up the one import."""
    only_skipped = export_zip(
        tmp_path / "e.zip",
        [
            podcast_row("2024-01-01T10:00:00Z"),
            row("2024-01-01T10:05:00Z", 1, incognito_mode=True),
            row("2024-01-01T10:10:00Z", 2, ms_played=10_000),
        ],
    )
    accepted(client, account, only_skipped)

    assert run_worker(engine) == "failed"

    status = latest(client, account)
    assert status["status"] == "failed"
    assert status["error_kind"] == "no_music_plays"
    assert status["counts"] == {
        "rows_seen": 3,
        "rows_inserted": 0,
        "rows_duplicate": 0,
        "rows_skipped_not_music": 1,
        "rows_skipped_incognito": 1,
        "rows_skipped_short": 1,
        "rows_unreadable": 0,
    }
    assert events(engine, account.id) == []
    assert files_in(imports_dir) == []
    assert scalar(engine, "SELECT status FROM ingest_batch WHERE user_id = :u", u=account.id) == (
        "failed"
    )

    accepted(client, account, mixed_export(tmp_path / "full.zip"))
    assert run_worker(engine) == "succeeded"


def test_a_second_import_after_success_is_refused(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    path = mixed_export(tmp_path / "e.zip")
    accepted(client, account, path)
    assert run_worker(engine) == "succeeded"

    response = upload(client, account, path)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "history_import_already_done"
    assert scalar(engine, "SELECT count(*) FROM history_import") == 1
    assert files_in(imports_dir) == []


def test_an_upload_while_one_is_queued_or_running_is_refused(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    path = mixed_export(tmp_path / "e.zip")
    first = accepted(client, account, path)

    queued = upload(client, account, path)
    assert queued.status_code == 409
    assert queued.json()["detail"]["code"] == "history_import_in_progress"

    claimed = hi.claim_next(engine, NOW)
    assert claimed is not None and claimed.id == first
    assert latest(client, account)["status"] == "running"
    running = upload(client, account, path)
    assert running.status_code == 409
    assert running.json()["detail"]["code"] == "history_import_in_progress"

    # Only the first upload's file is on disk.
    assert files_in(imports_dir) == [f"{first}.upload"]


def test_the_one_time_rule_holds_in_the_database(engine: Engine, account: Account) -> None:
    """Two uploads racing past the route's check: the index lets one in."""
    from uuid import uuid4

    with engine.begin() as conn:
        assert hi.create_import(conn, uuid4(), account.id, "zip", NOW) is None
        assert hi.create_import(conn, uuid4(), account.id, "zip", NOW) == "queued"
        assert scalar_in(conn, "SELECT count(*) FROM history_import") == 1


def scalar_in(conn: Any, sql: str) -> Any:
    return conn.execute(text(sql)).scalar_one()


def test_another_user_may_import_meanwhile(
    client: TestClient, engine: Engine, account: Account, tmp_path: Path
) -> None:
    path = mixed_export(tmp_path / "e.zip")
    accepted(client, account, path)
    other = Account(engine, "synthetic-account-2")

    accepted(client, other, path)


def test_a_failed_import_can_be_retried_without_duplicates(
    client: TestClient,
    engine: Engine,
    account: Account,
    imports_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [row(f"2024-01-0{n}T10:00:00Z", n) for n in range(1, 6)]
    path = export_zip(tmp_path / "e.zip", rows)
    monkeypatch.setattr(hi, "CHUNK_ROWS", 2)
    real_write_chunk = hi._write_chunk
    calls = 0

    def fail_on_second_chunk(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic failure mid-import")
        real_write_chunk(*args)

    monkeypatch.setattr(hi, "_write_chunk", fail_on_second_chunk)
    accepted(client, account, path)
    assert run_worker(engine) == "failed"

    failed = latest(client, account)
    assert (failed["status"], failed["error_kind"]) == ("failed", "internal_error")
    # The first chunk stays: valid plays, and the retry is idempotent.
    assert len(events(engine, account.id)) == 2
    assert files_in(imports_dir) == []

    monkeypatch.setattr(hi, "_write_chunk", real_write_chunk)
    accepted(client, account, path)
    assert run_worker(engine) == "succeeded"

    assert len(events(engine, account.id)) == 5
    retried = latest(client, account)
    assert retried["counts"]["rows_inserted"] == 3
    assert retried["counts"]["rows_duplicate"] == 2
    with engine.connect() as conn:
        statuses = conn.execute(
            text("SELECT status, error_kind FROM ingest_batch WHERE user_id = :u"),
            {"u": account.id},
        ).all()
    assert sorted(statuses) == [("failed", "internal_error"), ("succeeded", None)]


# --- dedupe with polled plays (decision 9) ----------------------------------


def test_the_dedupe_window_is_ten_seconds_until_the_spike() -> None:
    assert hi.DEDUPE_WINDOW_SECONDS == 10


def test_plays_the_poller_stored_are_not_imported_again(
    client: TestClient, engine: Engine, account: Account, tmp_path: Path
) -> None:
    base = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    api_play(engine, account, base + timedelta(seconds=5.5), 1)  # export ts 5.5 s earlier
    api_play(engine, account, base + timedelta(minutes=10), 2)  # export ts 11 s later
    api_play(engine, account, base + timedelta(minutes=20, milliseconds=400), 3)  # same second
    api_play(engine, account, base + timedelta(minutes=30), 4)  # same time, other track
    api_play(engine, account, base + timedelta(minutes=40, seconds=10), 6)  # exactly 10 s
    export = [
        row("2024-01-01T10:00:00Z", 1),
        row("2024-01-01T10:10:11Z", 2),
        row("2024-01-01T10:20:00Z", 3),
        row("2024-01-01T10:30:00Z", 5),
        row("2024-01-01T10:40:00Z", 6),
    ]
    accepted(client, account, export_zip(tmp_path / "e.zip", export))

    assert run_worker(engine) == "succeeded"

    counts = latest(client, account)["counts"]
    assert (counts["rows_inserted"], counts["rows_duplicate"]) == (2, 3)
    assert len(events(engine, account.id)) == 7
    imported = [e for e in events(engine, account.id) if e.ingest_method == "file_import"]
    assert [(e.item_key, e.occurred_at) for e in imported] == [
        (f"id:{track_id(2)}", datetime(2024, 1, 1, 10, 10, 11, tzinfo=UTC)),
        (f"id:{track_id(5)}", datetime(2024, 1, 1, 10, 30, tzinfo=UTC)),
    ]


def test_the_import_does_not_move_the_poll_cursor_or_count_as_a_poll(
    client: TestClient, engine: Engine, account: Account, tmp_path: Path
) -> None:
    polled = T0 + timedelta(hours=1)
    api_play(engine, account, polled, 1)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE spotify_connection SET poll_requested_at = :t WHERE user_id = :u"),
            {"t": NOW - timedelta(minutes=1), "u": account.id},
        )
    # The export was made after the last poll, so it holds a later play.
    later = row((T0 + timedelta(hours=5)).isoformat().replace("+00:00", "Z"), 2)
    accepted(client, account, export_zip(tmp_path / "e.zip", [later]))
    assert run_worker(engine) == "succeeded"
    assert len(events(engine, account.id)) == 2

    assert poller_spotify._cursor(engine, account.id, T0) == polled
    assert account.id in poller_spotify.connections_with_poll_request(engine)
    assert account.id in poller_spotify.connections_awaiting_first_poll(engine)
    with engine.connect() as conn:
        state = poll_state(conn, account.id)
    assert state is not None and state.last_poll_attempt_at is None


def test_the_poller_skips_a_play_imported_within_ten_seconds(
    client: TestClient,
    engine: Engine,
    account: Account,
    fake_spotify: FakeSpotify,
    tmp_path: Path,
) -> None:
    """WHO-112: the export's ts and played_at may differ, so the key alone misses it."""
    base = T0 + timedelta(hours=2)
    export = [row(z(base), 1), row(z(base + timedelta(minutes=10)), 2)]
    accepted(client, account, export_zip(tmp_path / "e.zip", export))
    assert run_worker(engine) == "succeeded"
    api_play(engine, account, base - timedelta(minutes=1), 3)  # the last poll: the cursor
    fake_spotify.play(track_id(1), "Synthetic Song 1", base - timedelta(seconds=3))  # dup
    fake_spotify.play(track_id(2), "Synthetic Song 2", base + timedelta(minutes=10, seconds=11))
    # A poll-only duplicate is left to the frozen key, as before WHO-112.
    fake_spotify.play(track_id(3), "Synthetic Song 3", base - timedelta(seconds=57))

    result = poller_spotify.poll_connection(engine, fake_spotify.client(), account.id, NOW)

    assert (result.status, result.rows_seen, result.rows_inserted) == ("succeeded", 3, 2)
    assert result.rows_skipped == 1
    polled = [e for e in events(engine, account.id) if e.ingest_method == "api_poll"]
    assert [(e.item_key, e.occurred_at) for e in polled] == [
        (f"id:{track_id(3)}", base - timedelta(minutes=1)),
        (f"id:{track_id(3)}", base - timedelta(seconds=57)),
        (f"id:{track_id(2)}", base + timedelta(minutes=10, seconds=11)),
    ]


def test_the_recent_history_import_skips_a_play_imported_within_ten_seconds(
    client: TestClient,
    engine: Engine,
    account: Account,
    fake_spotify: FakeSpotify,
    tmp_path: Path,
) -> None:
    """WHO-91's one-time 50-play import may run after the file import (WHO-112)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE spotify_connection SET history_import_requested = true WHERE user_id = :u"
            ),
            {"u": account.id},
        )
    before = T0 - timedelta(hours=1)  # before the connect: only the backfill stores it
    accepted(client, account, export_zip(tmp_path / "e.zip", [row(z(before), 1)]))
    assert run_worker(engine) == "succeeded"
    fake_spotify.play(track_id(1), "Synthetic Song 1", before + timedelta(seconds=4))
    fake_spotify.play(track_id(2), "Synthetic Song 2", before + timedelta(minutes=5))

    result = poller_spotify.poll_connection(engine, fake_spotify.client(), account.id, NOW)

    assert result.status == "succeeded"
    stored = [(e.item_key, e.ingest_method) for e in events(engine, account.id)]
    assert stored == [(f"id:{track_id(1)}", "file_import"), (f"id:{track_id(2)}", "api_backfill")]
    with engine.connect() as conn:
        batch = conn.execute(
            text(
                "SELECT rows_seen, rows_inserted, rows_skipped FROM ingest_batch "
                "WHERE user_id = :u AND ingest_method = 'api_backfill'"
            ),
            {"u": account.id},
        ).one()
    assert tuple(batch) == (2, 1, 1)


def z(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


# --- rejected uploads store nothing -----------------------------------------


def _traversal(tmp_path: Path) -> Path:
    return write_zip(
        tmp_path / "t.zip",
        {AUDIO_ENTRY: json_bytes([row("2024-01-01T10:00:00Z")]), "../evil.json": b"[]"},
    )


def _nested(tmp_path: Path) -> Path:
    inner = write_zip(tmp_path / "inner.zip", {AUDIO_ENTRY: b"[]"}).read_bytes()
    return write_zip(tmp_path / "n.zip", {AUDIO_ENTRY: b"[]", "inner.zip": inner})


def _bomb(tmp_path: Path) -> Path:
    return write_zip(tmp_path / "b.zip", {AUDIO_ENTRY: b"[" + b" " * (20 * 1024 * 1024) + b"]"})


def _wrong_format(tmp_path: Path) -> Path:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    return path


def _no_audio(tmp_path: Path) -> Path:
    return write_zip(tmp_path / "a.zip", {"Spotify Account Data/Userdata.json": b"{}"})


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (_traversal, "zip_unsafe_path"),
        (_nested, "zip_nested_archive"),
        (_bomb, "zip_ratio_exceeded"),
        (_wrong_format, "unsupported_file"),
        (_no_audio, "no_streaming_history"),
    ],
)
def test_a_rejected_upload_stores_nothing(
    client: TestClient,
    engine: Engine,
    account: Account,
    imports_dir: Path,
    tmp_path: Path,
    build: Any,
    code: str,
) -> None:
    response = upload(client, account, build(tmp_path))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == code
    assert scalar(engine, "SELECT count(*) FROM history_import") == 0
    assert files_in(imports_dir) == []
    # Rejected is not failed: nothing counts against the one-time rule.
    accepted(client, account, mixed_export(tmp_path / "good.zip"))


def test_an_upload_over_200_mb_is_413(
    client: TestClient,
    engine: Engine,
    account: Account,
    imports_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_import_api, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(eh, "MAX_UPLOAD_BYTES", 1000)
    rows = [row(f"2024-01-01T10:{n:02d}:00Z", n) for n in range(30)]
    path = export_zip(tmp_path / "big.zip", rows)
    assert path.stat().st_size > 1000

    response = upload(client, account, path)

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_too_large"
    assert scalar(engine, "SELECT count(*) FROM history_import") == 0
    assert files_in(imports_dir) == []


def test_a_declared_length_over_the_limit_is_refused_before_reading(
    client: TestClient, account: Account, imports_dir: Path
) -> None:
    response = client.post(
        URL,
        headers={
            **account.auth,
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(eh.MAX_UPLOAD_BYTES + 10 * 1024 * 1024),
        },
        content=b"--x--\r\n",
    )

    assert response.status_code == 413
    assert files_in(imports_dir) == []


def test_a_body_that_is_not_multipart_is_415(client: TestClient, account: Account) -> None:
    response = client.post(
        URL, headers={**account.auth, "Content-Type": "application/zip"}, content=b"PK\x03\x04"
    )

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "not_multipart"


def test_a_form_without_the_file_is_422(
    client: TestClient, account: Account, imports_dir: Path
) -> None:
    response = client.post(URL, headers=account.auth, data={"other": "x"}, files={"x": ("a", b"b")})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "missing_file"
    assert files_in(imports_dir) == []


def test_two_files_are_422(
    client: TestClient, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    data = mixed_export(tmp_path / "e.zip").read_bytes()
    response = client.post(
        URL, headers=account.auth, files=[("file", ("a.zip", data)), ("file", ("b.zip", data))]
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "too_many_files"
    assert files_in(imports_dir) == []


def test_a_limit_broken_in_the_job_stores_nothing(
    client: TestClient,
    engine: Engine,
    account: Account,
    imports_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(eh, "MAX_ROWS", 2)
    accepted(client, account, mixed_export(tmp_path / "e.zip"))

    assert run_worker(engine) == "failed"

    status = latest(client, account)
    assert (status["status"], status["error_kind"]) == ("failed", "too_many_rows")
    assert events(engine, account.id) == []
    assert scalar(engine, "SELECT count(*) FROM ingest_batch") == 0
    assert files_in(imports_dir) == []


def test_broken_json_fails_the_job_and_removes_the_file(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    path = tmp_path / "Streaming_History_Audio_2024.json"
    path.write_bytes(b'[{"ts": "2024-01-01T10:00:00Z"},')
    accepted(client, account, path)

    assert run_worker(engine) == "failed"

    assert latest(client, account)["error_kind"] == "invalid_json"
    assert files_in(imports_dir) == []


# --- the worker's crash and shutdown paths ----------------------------------


def test_a_missing_upload_file_fails_the_import(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    import_id = accepted(client, account, mixed_export(tmp_path / "e.zip"))
    (imports_dir / f"{import_id}.upload").unlink()

    assert run_worker(engine) == "failed"

    assert latest(client, account)["error_kind"] == "upload_missing"


def test_an_import_left_running_by_a_crash_fails_at_worker_start(
    client: TestClient, engine: Engine, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    path = mixed_export(tmp_path / "e.zip")
    accepted(client, account, path)
    assert hi.claim_next(engine, NOW) is not None  # then the process dies

    assert hi.fail_interrupted(engine, NOW) == 1

    status = latest(client, account)
    assert (status["status"], status["error_kind"]) == ("failed", "interrupted")
    assert files_in(imports_dir) == []
    accepted(client, account, path)


def test_shutdown_puts_the_import_back_in_the_queue(
    client: TestClient,
    engine: Engine,
    account: Account,
    imports_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hi, "CHUNK_ROWS", 1)
    import_id = accepted(client, account, mixed_export(tmp_path / "e.zip"))
    stop = threading.Event()
    stop.set()

    assert run_worker(engine, stop) == "queued"

    status = latest(client, account)
    assert status["status"] == "queued"
    assert status["counts"]["rows_seen"] == 0
    assert files_in(imports_dir) == [f"{import_id}.upload"]
    assert len(events(engine, account.id)) == 1  # the first chunk

    assert run_worker(engine) == "succeeded"

    assert latest(client, account)["counts"] == MIXED_COUNTS | {
        "rows_inserted": 2,
        "rows_duplicate": 2,
    }
    assert len(events(engine, account.id)) == 3
    with engine.connect() as conn:
        batches = conn.execute(
            text("SELECT status, error_kind FROM ingest_batch WHERE user_id = :u"),
            {"u": account.id},
        ).all()
    assert sorted(batches) == [("failed", "interrupted"), ("succeeded", None)]


def test_the_sweep_removes_upload_files_older_than_23_hours(imports_dir: Path) -> None:
    """With the hourly sweep, a file lives at most 24 h (WHO-103 §4 rule 3, WHO-106 F1)."""
    hi.upload_dir()
    now = time.time()
    ages = {
        "old.upload": 23.5 * 3600,
        "old.part": 23.5 * 3600,
        "new.upload": 22.5 * 3600,
        "unrelated.txt": 48 * 3600,
    }
    for name, age in ages.items():
        path = imports_dir / name
        path.write_bytes(b"x")
        os.utime(path, (now - age, now - age))

    assert hi.sweep_old_files(now) == 2

    assert files_in(imports_dir) == ["new.upload", "unrelated.txt"]
    assert hi.MAX_FILE_AGE + timedelta(seconds=hi.SWEEP_INTERVAL_SECONDS) <= timedelta(hours=24)


def test_an_upload_sweeps_old_files_while_the_poller_is_down(
    client: TestClient, account: Account, imports_dir: Path, tmp_path: Path
) -> None:
    hi.upload_dir()
    orphan = imports_dir / "orphan.upload"
    orphan.write_bytes(b"x")
    os.utime(orphan, (time.time() - 23.5 * 3600,) * 2)

    import_id = accepted(client, account, mixed_export(tmp_path / "e.zip"))

    assert files_in(imports_dir) == [f"{import_id}.upload"]


def test_every_history_import_field_is_a_connection_field() -> None:
    """Kept until the user deletes, not 90 days like collection records (WHO-103 §6.1)."""
    fields = [f for f in STORED_FIELDS if f.table == "history_import"]

    assert fields
    assert {f.category for f in fields} == {Category.CONNECTION}


def test_the_poller_import_worker_recovers_sweeps_and_runs_the_queue(
    client: TestClient,
    engine: Engine,
    account: Account,
    imports_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poller_main, "get_engine", lambda: engine)
    accepted(client, account, mixed_export(tmp_path / "e.zip"))
    stale = imports_dir / "stale.upload"
    stale.write_bytes(b"x")
    os.utime(stale, (time.time() - 25 * 3600,) * 2)

    class OneRound(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            self.set()
            return True

    poller_main.import_worker(OneRound(), check_seconds=1)

    assert latest(client, account)["status"] == "succeeded"
    assert files_in(imports_dir) == []
