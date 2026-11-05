"""Delete leaves no row behind in any user table (PRD P5, story S8.7, Security 4d).

Tables are read from ``pg_catalog``, never listed by hand. A new table
fails here until it is reachable by ``DELETE FROM app_user`` and has a seeder in
``SEEDERS``, so the delete test always covers every table, provider tokens
included.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.core.crypto import keyed_hash
from app.events.keys import item_key_from_provider_id
from app.events.store import PreciseEvent, WriteCounts, write_precise_event
from app.privacy.audit import (
    USER_TABLE,
    AuditSawNothing,
    DeleteCoverageProblem,
    delete_coverage_problems,
    live_tables,
    rows_held_for_user,
)
from app.privacy.delete import delete_all_user_data
from app.privacy.stored_fields import SYSTEM_TABLES
from app.spotify.connection import encrypt_refresh_token

SELFTEST_SCHEMA = "privacy_selftest"
OTHER_SCHEMA = "privacy_selftest_other"

Seeder = Callable[[Connection, UUID], None]

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _seed_app_user(db: Connection, user: UUID) -> None:
    db.execute(
        text("INSERT INTO app_user (id, home_timezone) VALUES (:u, 'Europe/Helsinki')"),
        {"u": user},
    )


def _seed_spotify_connection(db: Connection, user: UUID) -> None:
    db.execute(
        text(
            "INSERT INTO spotify_connection (user_id, spotify_account_id, spotify_display_name, "
            "  collection_started_at, authorized_at, refresh_token_ciphertext, last_polled_at) "
            "VALUES (:u, :account, 'Synthetic Name', :now, :now, :token, :now)"
        ),
        {
            "u": user,
            "account": f"synthetic-{user}",
            "now": NOW,
            "token": encrypt_refresh_token(user, "synthetic-refresh-token"),
        },
    )


def _seed_auth_session(db: Connection, user: UUID) -> None:
    db.execute(
        text(
            "INSERT INTO auth_session (user_id, login_id, token_hash, expires_at) "
            "VALUES (:u, :login, :hash, :expires)"
        ),
        {
            "u": user,
            "login": uuid4(),
            "hash": keyed_hash(str(uuid4()), purpose="claim"),
            "expires": NOW,
        },
    )


def _seed_ingest_batch_and_unified_event(db: Connection, user: UUID) -> None:
    batch = db.execute(
        text(
            "INSERT INTO ingest_batch (user_id, source, source_artifact, ingest_method, status) "
            "VALUES (:u, 'spotify', 'spotify_recently_played', 'api_poll', 'succeeded') "
            "RETURNING id"
        ),
        {"u": user},
    ).scalar_one()
    write_precise_event(
        db,
        user_id=user,
        home_timezone="Europe/Helsinki",
        event=PreciseEvent(
            source="spotify",
            source_artifact="spotify_recently_played",
            ingest_method="api_poll",
            kind="listen",
            occurred_at=NOW,
            title="Synthetic Track",
            item_key=item_key_from_provider_id("synthetic-track-id"),
        ),
        ingest_batch_id=batch,
        counts=WriteCounts(),
    )


def _seed_collection_gap(db: Connection, user: UUID) -> None:
    db.execute(
        text(
            "INSERT INTO collection_gap (user_id, source, started_at, ended_at) "
            "VALUES (:u, 'spotify', :start, :now)"
        ),
        {"u": user, "start": NOW.replace(hour=9), "now": NOW},
    )


def _seed_spotify_renewal(db: Connection, user: UUID) -> None:
    """A renewal in progress; its oauth_login row is a system table, linked to no user."""
    login = uuid4()
    db.execute(
        text(
            "INSERT INTO oauth_login (id, state_hash, code_verifier_ciphertext, "
            "  claim_secret_hash, home_timezone, expires_at) "
            "VALUES (:id, :state, :verifier, :claim, 'Europe/Helsinki', :now)"
        ),
        {
            "id": login,
            "state": keyed_hash(str(uuid4()), purpose="state"),
            "verifier": b"synthetic-verifier",
            "claim": keyed_hash(str(uuid4()), purpose="claim"),
            "now": NOW,
        },
    )
    db.execute(
        text("INSERT INTO spotify_renewal (login_id, user_id) VALUES (:login, :u)"),
        {"login": login, "u": user},
    )


def _seed_history_import(db: Connection, user: UUID) -> None:
    """A succeeded extended history import, linked to the user's collection record."""
    db.execute(
        text(
            "INSERT INTO history_import (id, user_id, file_kind, status, created_at, "
            "  finished_at, ingest_batch_id, rows_seen, rows_inserted) "
            "SELECT :id, :u, 'zip', 'succeeded', :now, :now, "
            "  (SELECT id FROM ingest_batch WHERE user_id = :u LIMIT 1), 1, 1"
        ),
        {"id": uuid4(), "u": user, "now": NOW},
    )


def _seed_not_new_mark(db: Connection, user: UUID) -> None:
    db.execute(
        text(
            "INSERT INTO not_new_mark (user_id, kind, item_id) "
            "VALUES (:u, 'album', 'ALB1'), (:u, 'artist', 'Nova Kett'), (:u, 'track', 'id:AAA')"
        ),
        {"u": user},
    )


def _seed_album_rating(db: Connection, user: UUID) -> None:
    db.execute(
        text("INSERT INTO album_rating (user_id, album_id, rating) VALUES (:u, 'ALB1', 8)"),
        {"u": user},
    )


def _seed_album_comment(db: Connection, user: UUID) -> None:
    db.execute(
        text(
            "INSERT INTO album_comment (user_id, album_id, comment) "
            "VALUES (:u, 'ALB1', 'Synthetic comment')"
        ),
        {"u": user},
    )


def _seed_track_comment(db: Connection, user: UUID) -> None:
    db.execute(
        text(
            "INSERT INTO track_comment (user_id, item_key, comment) "
            "VALUES (:u, 'id:TRK1', 'Synthetic comment')"
        ),
        {"u": user},
    )


def _noop(db: Connection, user: UUID) -> None:
    """Rows written by the seeder before it in SEEDERS."""


# table -> function that inserts at least one row for the given user, synthetic
# data only. Insertion order follows this dict, so app_user comes first.
SEEDERS: dict[str, Seeder] = {
    "app_user": _seed_app_user,
    "spotify_connection": _seed_spotify_connection,
    "auth_session": _seed_auth_session,
    "ingest_batch": _seed_ingest_batch_and_unified_event,
    "unified_event": _noop,
    "collection_gap": _seed_collection_gap,
    "spotify_renewal": _seed_spotify_renewal,
    "not_new_mark": _seed_not_new_mark,
    "album_rating": _seed_album_rating,
    "album_comment": _seed_album_comment,
    "track_comment": _seed_track_comment,
    "history_import": _seed_history_import,
}


def test_every_table_is_reached_by_user_delete(db: Connection) -> None:
    assert delete_coverage_problems(db) == []


def test_every_user_table_has_a_seeder(db: Connection) -> None:
    unseeded = live_tables(db) - SYSTEM_TABLES - set(SEEDERS)

    assert unseeded == set(), f"Add a seeder to SEEDERS so delete is tested on: {sorted(unseeded)}"


def test_delete_leaves_zero_rows_for_the_user_in_every_table(db: Connection) -> None:
    if USER_TABLE not in live_tables(db):
        pytest.skip(f"{USER_TABLE} not migrated yet (WHO-6)")
    _assert_delete_removes_everything(db, SEEDERS)


def test_selftest_a_table_without_cascade_or_user_id_is_reported(db: Connection) -> None:
    _create_selftest_schema(db)
    s = SELFTEST_SCHEMA
    db.execute(text(f"CREATE TABLE {s}.no_cascade (user_id uuid NOT NULL REFERENCES {s}.app_user)"))
    db.execute(
        text(
            f"CREATE TABLE {s}.nullable_owner "
            f"(user_id uuid REFERENCES {s}.app_user ON DELETE CASCADE)"
        )
    )
    db.execute(
        text(
            f"CREATE TABLE {s}.hangs_off_token "
            f"(token_id uuid NOT NULL REFERENCES {s}.provider_token ON DELETE CASCADE)"
        )
    )

    assert delete_coverage_problems(db, s) == [
        DeleteCoverageProblem("hangs_off_token", "has no user_id column"),
        DeleteCoverageProblem(
            "no_cascade", "user_id has no foreign key to app_user(id) ON DELETE CASCADE"
        ),
        DeleteCoverageProblem("nullable_owner", "user_id is nullable"),
    ]


def test_only_expected_schemas_exist(db: Connection) -> None:
    """The audit reads ``public``; a table in another schema would sit outside it."""
    schemas = set(
        db.execute(
            text(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'"
            )
        ).scalars()
    )

    assert schemas == {"public"}


def test_audit_sees_every_table_as_a_role_without_table_privileges(db: Connection) -> None:
    """``information_schema`` hides tables the role has no privilege on (WHO-24 R2)."""
    as_owner = live_tables(db)
    role = f"privacy_selftest_{uuid4().hex[:12]}"
    db.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    db.execute(text(f'SET LOCAL ROLE "{role}"'))

    assert USER_TABLE in as_owner
    assert live_tables(db) == as_owner
    assert delete_coverage_problems(db) == []


def test_selftest_an_audit_that_sees_no_user_table_raises(db: Connection) -> None:
    s = SELFTEST_SCHEMA
    db.execute(text(f"CREATE SCHEMA {s}"))
    db.execute(text(f"CREATE TABLE {s}.alembic_version (version_num text PRIMARY KEY)"))

    with pytest.raises(AuditSawNothing):
        delete_coverage_problems(db, s)


def test_selftest_a_foreign_key_to_the_wrong_app_user_is_reported(db: Connection) -> None:
    """Another schema's ``app_user``, or a column other than ``app_user.id`` (WHO-24 R1)."""
    _create_selftest_schema(db)
    s, other = SELFTEST_SCHEMA, OTHER_SCHEMA
    db.execute(text(f"CREATE SCHEMA {other}"))
    db.execute(text(f"CREATE TABLE {other}.app_user (id uuid PRIMARY KEY)"))
    db.execute(
        text(
            f"CREATE TABLE {s}.other_schema "
            f"(user_id uuid NOT NULL REFERENCES {other}.app_user ON DELETE CASCADE)"
        )
    )
    db.execute(text(f"ALTER TABLE {s}.app_user ADD COLUMN alt_id uuid UNIQUE"))
    db.execute(
        text(
            f"CREATE TABLE {s}.other_column "
            f"(user_id uuid NOT NULL REFERENCES {s}.app_user (alt_id) ON DELETE CASCADE)"
        )
    )

    assert delete_coverage_problems(db, s) == [
        DeleteCoverageProblem(
            "other_column", "user_id has no foreign key to app_user(id) ON DELETE CASCADE"
        ),
        DeleteCoverageProblem(
            "other_schema", "user_id has no foreign key to app_user(id) ON DELETE CASCADE"
        ),
    ]


def test_selftest_a_materialized_view_is_reported(db: Connection) -> None:
    """A matview keeps its rows after the user is deleted (WHO-24 R2)."""
    _create_selftest_schema(db)
    s = SELFTEST_SCHEMA
    db.execute(
        text(f"CREATE MATERIALIZED VIEW {s}.titles AS SELECT user_id, title FROM {s}.unified_event")
    )

    assert delete_coverage_problems(db, s) == [
        DeleteCoverageProblem("titles", "materialized view, not reached by delete")
    ]


def test_selftest_delete_reaches_every_table_including_tokens(db: Connection) -> None:
    _create_selftest_schema(db)

    assert delete_coverage_problems(db, SELFTEST_SCHEMA) == []
    _assert_delete_removes_everything(db, _selftest_seeders(), SELFTEST_SCHEMA)


def test_selftest_a_table_without_cascade_blocks_delete(db: Connection) -> None:
    _create_selftest_schema(db)
    s = SELFTEST_SCHEMA
    db.execute(text(f"CREATE TABLE {s}.no_cascade (user_id uuid NOT NULL REFERENCES {s}.app_user)"))
    user = uuid4()
    for seed in _selftest_seeders().values():
        seed(db, user)
    db.execute(text(f"INSERT INTO {s}.no_cascade VALUES (:u)"), {"u": user})

    with pytest.raises(IntegrityError):
        delete_all_user_data(db, user, s)


def _assert_delete_removes_everything(
    db: Connection, seeders: dict[str, Seeder], schema: str = "public"
) -> None:
    deleted, kept = uuid4(), uuid4()
    for user in (deleted, kept):
        for seed in seeders.values():
            seed(db, user)
    before = rows_held_for_user(db, deleted, schema)
    kept_before = rows_held_for_user(db, kept, schema)
    assert all(n > 0 for n in before.values()), f"A seeder wrote nothing: {before}"

    delete_all_user_data(db, deleted, schema)

    after = rows_held_for_user(db, deleted, schema)
    assert after == dict.fromkeys(before, 0)
    assert rows_held_for_user(db, kept, schema) == kept_before


def _create_selftest_schema(db: Connection) -> None:
    """A user table, an event table and a token table, wired the way v1 is."""
    s = SELFTEST_SCHEMA
    db.execute(text(f"CREATE SCHEMA {s}"))
    db.execute(text(f"CREATE TABLE {s}.app_user (id uuid PRIMARY KEY)"))
    db.execute(
        text(
            f"CREATE TABLE {s}.provider_token (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
            f"user_id uuid NOT NULL REFERENCES {s}.app_user ON DELETE CASCADE, "
            "ciphertext bytea NOT NULL)"
        )
    )
    db.execute(
        text(
            f"CREATE TABLE {s}.unified_event ("
            f"user_id uuid NOT NULL REFERENCES {s}.app_user ON DELETE CASCADE, title text)"
        )
    )


def _selftest_seeders() -> dict[str, Seeder]:
    s = SELFTEST_SCHEMA

    def app_user(db: Connection, user: UUID) -> None:
        db.execute(text(f"INSERT INTO {s}.app_user VALUES (:u)"), {"u": user})

    def provider_token(db: Connection, user: UUID) -> None:
        db.execute(
            text(f"INSERT INTO {s}.provider_token (user_id, ciphertext) VALUES (:u, 'x')"),
            {"u": user},
        )

    def unified_event(db: Connection, user: UUID) -> None:
        db.execute(
            text(f"INSERT INTO {s}.unified_event VALUES (:u, 'Synthetic Track')"), {"u": user}
        )

    return {"app_user": app_user, "provider_token": provider_token, "unified_event": unified_event}
