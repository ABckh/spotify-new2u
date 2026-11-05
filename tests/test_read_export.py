"""``GET /v1/export``: complete, round-trips every field, streamed, never cached (WHO-25 R5)."""

import json
import time
from collections.abc import Generator, Iterator
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError

from app.api.rate_limit import Busy, export_streams
from app.api.read import _one_export_at_a_time
from app.core.config import get_settings
from app.privacy.stored_fields import NOT_USER_DATA, STORED_FIELDS
from app.read import export as export_module
from app.read.export import SECTIONS, WITHHELD, export_chunks, json_value, stream_export
from app.spotify.connection import encrypt_refresh_token
from read_helpers import NOW, User, add_play, make_user

SYNTHETIC_REFRESH_TOKEN = "synthetic-refresh-token-0c41"


def seed_everything(engine: Engine, user: User) -> None:
    """One row in every user table, every nullable column filled in."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO spotify_connection (user_id, spotify_account_id, "
                "  spotify_display_name, collection_started_at, authorized_at, "
                "  refresh_token_ciphertext, status, reauth_reason, last_polled_at) "
                "VALUES (:u, 'synthetic-account', 'Synthetic Name', :now, :now, :token, "
                "  'active', NULL, :now)"
            ),
            {
                "u": user.id,
                "now": NOW,
                "token": encrypt_refresh_token(user.id, SYNTHETIC_REFRESH_TOKEN),
            },
        )
        batch = conn.execute(
            text(
                "INSERT INTO ingest_batch (user_id, source, source_artifact, ingest_method, "
                "  started_at, finished_at, window_start, window_end, rows_seen, rows_inserted, "
                "  status, error_kind) "
                "VALUES (:u, 'spotify', 'spotify_recently_played', 'api_poll', :now, :now, "
                "  :now, :now, 2, 1, 'failed', 'synthetic_error') RETURNING id"
            ),
            {"u": user.id, "now": NOW},
        ).scalar_one()
        add_gap(conn, user)
        login_id = conn.execute(
            text(
                "INSERT INTO oauth_login (state_hash, code_verifier_ciphertext, "
                "  claim_secret_hash, home_timezone, expires_at) "
                "VALUES ('\\x01', '\\x02', '\\x03', 'Europe/Warsaw', :now) RETURNING id"
            ),
            {"now": NOW},
        ).scalar_one()
        conn.execute(
            text("INSERT INTO spotify_renewal (login_id, user_id) VALUES (:login, :u)"),
            {"login": login_id, "u": user.id},
        )
        conn.execute(
            text(
                "INSERT INTO not_new_mark (user_id, kind, item_id, created_at) "
                "VALUES (:u, 'artist', 'Nova Kett', :now), (:u, 'track', 'id:BBB', :now)"
            ),
            {"u": user.id, "now": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO album_rating (user_id, album_id, rating, rated_at) "
                "VALUES (:u, 'ALB1', 8, :now)"
            ),
            {"u": user.id, "now": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO album_comment (user_id, album_id, comment, commented_at) "
                "VALUES (:u, 'ALB1', :comment, :now)"
            ),
            {"u": user.id, "comment": 'Synthetic cömment ☃\n"second line"', "now": NOW},
        )
    add_play(
        engine,
        user,
        "2026-10-11T08:00:00.123456Z",
        item="AAA",
        title='Synthetic Tïtle ☃ "quoted"',
        creators=("Nova Kett", "Pine Arc"),
        duration_ms=200_000,
        collection="Synthetic Album",
        external_ids={"spotify_track_id": "AAA", "isrc": "XX0000000001"},
    )
    add_play(engine, user, "2026-10-11T09:00:00Z", item="BBB", duration_ms=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE unified_event SET ingest_batch_id = :b, "
                "  artwork_url = 'https://i.scdn.co/image/ab67616d00001e02synthetic' "
                "WHERE user_id = :u AND item_key = 'id:AAA'"
            ),
            {"b": batch, "u": user.id},
        )


def add_gap(conn: Connection, user: User) -> None:
    """One closed collection gap, from 02:00 to 05:30 UTC on the export day."""
    conn.execute(
        text(
            "INSERT INTO collection_gap (user_id, source, started_at, ended_at) "
            "VALUES (:u, 'spotify', :start, :end)"
        ),
        {"u": user.id, "start": NOW.replace(hour=2), "end": NOW.replace(hour=5, minute=30)},
    )


def export(api: TestClient, user: User) -> dict[str, Any]:
    response = api.get("/v1/export", headers=user.auth)
    assert response.status_code == 200, response.text
    document: dict[str, Any] = json.loads(response.text)
    return document


def stored_rows(conn: Connection, table: str, owner: str, user_id: UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(f"SELECT * FROM {table} WHERE {owner} = :u ORDER BY 1"), {"u": user_id}
    ).mappings()
    return [dict(row) for row in rows]


def test_export_round_trips_every_field_of_every_event(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    seed_everything(engine, user)

    document = export(read_api, user)

    with engine.connect() as conn:
        stored = stored_rows(conn, "unified_event", "user_id", user.id)
    exported = {e["id"]: e for e in document["events"]}
    assert len(exported) == len(stored) == 2
    for row in stored:
        assert exported[str(row["id"])] == {k: json_value(v) for k, v in row.items()}
    event = exported[str(next(r["id"] for r in stored if r["item_key"] == "id:AAA"))]
    # Full stored precision, read back to the exact same instant.
    assert event["occurred_at"] == "2026-10-11T08:00:00.123456Z"
    assert datetime.fromisoformat(event["occurred_at"]) == next(
        r["occurred_at"] for r in stored if r["item_key"] == "id:AAA"
    )
    assert event["title"] == 'Synthetic Tïtle ☃ "quoted"'
    assert event["external_ids"] == {"spotify_track_id": "AAA", "isrc": "XX0000000001"}
    assert bytes.fromhex(event["dedupe_key"]) == next(
        r["dedupe_key"] for r in stored if r["item_key"] == "id:AAA"
    )


def test_every_other_section_matches_its_table(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    seed_everything(engine, user)

    document = export(read_api, user)

    with engine.connect() as conn:
        for section in SECTIONS:
            rows = stored_rows(conn, section.table, section.owner_column, user.id)
            expected = [
                {k: json_value(v) for k, v in row.items() if (section.table, k) not in WITHHELD}
                for row in rows
            ]
            got = document[section.key]
            got = [got] if section.single else got
            for item in got:
                item.pop("refresh_token_stored", None)
            key = "id" if "id" in section.columns else "user_id"
            assert sorted(got, key=lambda r: str(r[key])) == sorted(
                expected, key=lambda r: str(r[key])
            )


def test_export_covers_every_stored_field_except_withheld_secrets() -> None:
    """A column added to STORED_FIELDS fails here until the export carries it."""
    exported = {(s.table, column) for s in SECTIONS for column in s.columns}
    # NOT_USER_DATA columns hold nothing today; exported anyway, so no field is missed.
    declared = {(f.table, f.column) for f in STORED_FIELDS} | set(NOT_USER_DATA)

    assert exported | set(WITHHELD) == declared
    assert exported & set(WITHHELD) == set()


def test_the_token_is_never_exported_only_whether_one_is_stored(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    seed_everything(engine, user)

    response = read_api.get("/v1/export", headers=user.auth)

    connection = json.loads(response.text)["spotify_connection"]
    assert connection["refresh_token_stored"] is True
    assert "refresh_token_ciphertext" not in connection
    assert SYNTHETIC_REFRESH_TOKEN not in response.text
    with engine.connect() as conn:
        ciphertext = conn.execute(
            text("SELECT refresh_token_ciphertext FROM spotify_connection WHERE user_id = :u"),
            {"u": user.id},
        ).scalar_one()
    assert bytes(ciphertext).hex() not in response.text
    for session in json.loads(response.text)["sessions"]:
        assert set(session) == {
            "id",
            "user_id",
            "login_id",
            "created_at",
            "expires_at",
            "refresh_expires_at",
        }


def test_a_closed_gap_is_exported_once_in_its_own_section_and_never_as_an_event(
    read_api: TestClient, engine: Engine
) -> None:
    """WHO-47 (3): W2 says we keep gaps, so export carries them; S2.10f: not as events."""
    user = make_user(engine)
    add_play(engine, user, "2026-10-11T08:00:00Z", item="AAA")
    events_before = export(read_api, user)["events"]
    with engine.begin() as conn:
        add_gap(conn, user)

    document = export(read_api, user)

    assert len(document["collection_gaps"]) == 1
    gap = document["collection_gaps"][0]
    assert (gap["source"], gap["started_at"], gap["ended_at"]) == (
        "spotify",
        "2026-10-11T02:00:00Z",
        "2026-10-11T05:30:00Z",
    )
    assert document["events"] == events_before


def test_a_new_user_exports_empty_sections(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    document = export(read_api, user)

    assert document["format"] == "new2u-export"
    assert document["format_version"] == 1
    assert document["exported_at"] == "2026-10-11T12:00:00Z"
    assert document["account"] == {"id": str(user.id), "home_timezone": "Europe/Warsaw"}
    assert document["spotify_connection"] is None
    assert document["collection_records"] == []
    assert document["collection_gaps"] == []
    assert document["renewals"] == []
    assert document["not_new_marks"] == []
    assert document["events"] == []
    assert len(document["sessions"]) == 1


def test_export_is_not_cached_or_spooled(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    response = read_api.get("/v1/export", headers=user.auth)

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert response.headers["Content-Type"] == "application/json"
    assert "attachment" in response.headers["Content-Disposition"]
    assert "content-length" not in response.headers  # streamed, not built first


def test_events_are_read_through_a_server_side_cursor(engine: Engine) -> None:
    """P13: rows stream from Postgres in batches, never a full copy in memory."""
    user = make_user(engine)
    add_play(engine, user, "2026-10-11T08:00:00Z", item="a")
    add_play(engine, user, "2026-10-11T09:00:00Z", item="b")

    with engine.connect() as conn, conn.begin():
        chunks = cast(Generator[str], export_chunks(conn, user.id, NOW))
        for chunk in chunks:
            if chunk.startswith('{"id"') and "unified_event" not in chunk and '"kind"' in chunk:
                break  # the first event row is out; the rest are still in Postgres
        open_cursors = conn.execute(text("SELECT statement FROM pg_cursors")).scalars().all()
        chunks.close()

    assert len(open_cursors) == 1
    assert "FROM unified_event WHERE user_id = $1" in open_cursors[0]


def test_a_failure_mid_stream_aborts_the_response(
    read_api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client must never receive a short document that parses as complete."""
    user = make_user(engine)
    add_play(engine, user, "2026-10-11T08:00:00Z", item="a")
    add_play(engine, user, "2026-10-11T09:00:00Z", item="b")
    real = export_module.json_value
    seen: list[object] = []

    def failing_json_value(value: object) -> object:
        if isinstance(value, datetime) and value.minute == 0 and value.hour == 9:
            seen.append(value)
            raise RuntimeError("synthetic mid-stream failure")
        return real(value)

    monkeypatch.setattr(export_module, "json_value", failing_json_value)

    with pytest.raises(RuntimeError, match="synthetic mid-stream failure"):
        read_api.get("/v1/export", headers=user.auth)
    assert seen

    # And the bytes produced before the failure are not a valid document.
    partial = ""
    with engine.connect() as conn, conn.begin():
        try:
            for chunk in export_chunks(conn, user.id, NOW):
                partial += chunk
        except RuntimeError:
            pass
    with pytest.raises(json.JSONDecodeError):
        json.loads(partial)


# --- a slow reader cannot hold the database (WHO-48 F1) ------------------------


def test_the_export_transaction_carries_the_idle_timeout_setting(
    read_api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user(engine)
    monkeypatch.setattr(get_settings(), "export_idle_timeout_seconds", 7)

    def show_timeout(conn: Connection, user_id: UUID, now: datetime) -> Iterator[str]:
        yield conn.execute(text("SHOW idle_in_transaction_session_timeout")).scalar_one()

    monkeypatch.setattr(export_module, "export_chunks", show_timeout)

    assert read_api.get("/v1/export", headers=user.auth).text == "7s"


def test_a_stalled_export_is_ended_by_postgres(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client stops reading; after the timeout the stream fails, so it is truncated."""
    user = make_user(engine)

    def two_chunks(conn: Connection, user_id: UUID, now: datetime) -> Iterator[str]:
        yield "x" * export_module.CHUNK_BYTES
        conn.execute(text("SELECT 1"))  # the next fetch from Postgres
        yield "never sent"

    monkeypatch.setattr(export_module, "export_chunks", two_chunks)
    body = stream_export(engine, user.id, NOW, idle_timeout_seconds=1)

    assert len(next(body)) == export_module.CHUNK_BYTES
    time.sleep(1.5)  # the reader stalls, the session idles in its transaction
    with pytest.raises(OperationalError, match="idle-in-transaction timeout"):
        next(body)


def test_one_export_at_a_time_per_user(read_api: TestClient, engine: Engine) -> None:
    exporting, other = make_user(engine), make_user(engine)

    with export_streams.hold(str(exporting.id)):  # as while a stream is being read
        busy = read_api.get("/v1/export", headers=exporting.auth)
        assert read_api.get("/v1/export", headers=other.auth).status_code == 200

    assert busy.status_code == 429
    assert int(busy.headers["Retry-After"]) == get_settings().export_idle_timeout_seconds
    assert read_api.get("/v1/export", headers=exporting.auth).status_code == 200


def test_the_export_slot_is_released_however_the_stream_ends(engine: Engine) -> None:
    user = make_user(engine)
    started: list[bool] = []

    def rows() -> Iterator[bytes]:
        started.append(True)
        yield b"{}"

    # Taken before any row is read, and refused without reading any.
    body = _one_export_at_a_time(user.id, rows())
    assert next(body) == b""
    with pytest.raises(Busy):
        next(_one_export_at_a_time(user.id, rows()))
    assert started == []

    body.close()  # the client went away mid-stream
    with export_streams.hold(str(user.id)):
        pass

    def failing() -> Iterator[bytes]:
        raise RuntimeError("synthetic failure")
        yield b""

    body = _one_export_at_a_time(user.id, failing())
    next(body)
    with pytest.raises(RuntimeError):
        next(body)
    with export_streams.hold(str(user.id)):
        pass

    assert list(_one_export_at_a_time(user.id, rows())) == [b"", b"{}"]
    with export_streams.hold(str(user.id)):
        pass
