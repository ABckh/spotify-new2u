"""The retention purge removes collection records, never plays (WHO-34, from WHO-32 R2).

``purge_collection_records`` deletes old ``ingest_batch`` rows. That is only
safe because ``unified_event.ingest_batch_id`` is ``ON DELETE SET NULL``
(schema v1, WHO-2 §5.1). With ``CASCADE`` the purge would delete every play
older than the window; with ``RESTRICT`` it would fail on every poll tick.

``test_purge_keeps_plays_in_the_migrated_schema`` runs against the real
migrated schema once the schema v1 migration (WHO-6) creates ``unified_event``.
Until then it skips, and the throwaway-schema tests below run the same
scenario on the schema v1 DDL, and prove the check catches both wrong rules.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.privacy.audit import live_tables
from app.privacy.retention import purge_collection_records

SCHEMA = "retention_plays_test"
WINDOW = timedelta(days=90)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

# pg_constraint.confdeltype values.
DELETE_RULES = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def _ingest_batch_delete_rule(conn: Connection, schema: str) -> str | None:
    """The ON DELETE rule of the unified_event.ingest_batch_id foreign key."""
    row = conn.execute(
        text(
            "SELECT con.confdeltype FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
            "JOIN pg_class ref ON ref.oid = con.confrelid "
            "JOIN pg_attribute att "
            "  ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey) "
            "WHERE con.contype = 'f' AND ns.nspname = :schema "
            "AND rel.relname = 'unified_event' AND ref.relname = 'ingest_batch' "
            "AND att.attname = 'ingest_batch_id'"
        ),
        {"schema": schema},
    ).one_or_none()
    return None if row is None else DELETE_RULES[row.confdeltype]


def _create_v1_tables(conn: Connection, schema: str, ingest_batch_on_delete: str) -> None:
    """Schema v1 DDL (WHO-2 §5.1), columns only; CHECKs and indexes are not needed here."""
    conn.execute(text(f"CREATE SCHEMA {schema}"))
    conn.execute(text(f"CREATE TABLE {schema}.app_user (id uuid PRIMARY KEY)"))
    conn.execute(
        text(
            f"CREATE TABLE {schema}.ingest_batch ("
            "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  user_id uuid NOT NULL REFERENCES {schema}.app_user (id) ON DELETE CASCADE,"
            "  source text NOT NULL,"
            "  source_artifact text NOT NULL,"
            "  ingest_method text NOT NULL,"
            "  started_at timestamptz NOT NULL DEFAULT now(),"
            "  finished_at timestamptz,"
            "  status text NOT NULL DEFAULT 'running')"
        )
    )
    conn.execute(
        text(
            f"CREATE TABLE {schema}.unified_event ("
            "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
            f"  user_id uuid NOT NULL REFERENCES {schema}.app_user (id) ON DELETE CASCADE,"
            "  source text NOT NULL,"
            "  source_artifact text NOT NULL,"
            "  ingest_method text NOT NULL,"
            f"  ingest_batch_id uuid REFERENCES {schema}.ingest_batch (id)"
            f"    ON DELETE {ingest_batch_on_delete},"
            "  kind text NOT NULL,"
            "  occurred_at timestamptz NOT NULL,"
            "  occurred_at_precision text NOT NULL,"
            "  title text NOT NULL,"
            "  item_key text NOT NULL,"
            "  item_key_source text NOT NULL,"
            "  dedupe_key bytea NOT NULL,"
            "  day_key bytea NOT NULL)"
        )
    )


def _insert_user(conn: Connection, schema: str) -> UUID:
    # app_user's columns beyond id land with WHO-6; extend this insert to match.
    user_id = uuid4()
    conn.execute(text(f"INSERT INTO {schema}.app_user (id) VALUES (:id)"), {"id": user_id})
    return user_id


def _insert_batch(conn: Connection, schema: str, user_id: UUID, started_at: datetime) -> UUID:
    batch_id: UUID = conn.execute(
        text(
            f"INSERT INTO {schema}.ingest_batch "
            "(user_id, source, source_artifact, ingest_method, started_at, status) "
            "VALUES (:user_id, 'spotify', 'spotify_recently_played', 'api_poll', :started_at, "
            "'succeeded') RETURNING id"
        ),
        {"user_id": user_id, "started_at": started_at},
    ).scalar_one()
    return batch_id


def _insert_play(
    conn: Connection, schema: str, user_id: UUID, batch_id: UUID, occurred_at: datetime
) -> UUID:
    # Synthetic data only. The keys are 32 bytes, as schema v1 requires.
    key = sha256(f"{user_id}|{occurred_at.isoformat()}".encode()).digest()
    event_id: UUID = conn.execute(
        text(
            f"INSERT INTO {schema}.unified_event "
            "(user_id, source, source_artifact, ingest_method, ingest_batch_id, kind, "
            " occurred_at, occurred_at_precision, title, item_key, item_key_source, "
            " dedupe_key, day_key) "
            "VALUES (:user_id, 'spotify', 'spotify_recently_played', 'api_poll', :batch_id, "
            " 'listen', :occurred_at, 'millisecond', 'Synthetic track', 'spotify:track:test', "
            " 'provider', :key, :key) RETURNING id"
        ),
        {"user_id": user_id, "batch_id": batch_id, "occurred_at": occurred_at, "key": key},
    ).scalar_one()
    return event_id


def _plays(conn: Connection, schema: str, user_id: UUID) -> dict[UUID, UUID | None]:
    """event id -> ingest_batch_id for the user's plays."""
    rows = conn.execute(
        text(f"SELECT id, ingest_batch_id FROM {schema}.unified_event WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    return {row.id: row.ingest_batch_id for row in rows}


def _assert_purge_keeps_plays(conn: Connection, schema: str) -> None:
    """One batch older than the window and one inside it, each with a play.

    After the purge both plays remain; the old one has lost its batch link.
    """
    user_id = _insert_user(conn, schema)
    old_started = NOW - WINDOW - timedelta(days=1)
    old_batch = _insert_batch(conn, schema, user_id, old_started)
    new_batch = _insert_batch(conn, schema, user_id, NOW - timedelta(hours=1))
    old_play = _insert_play(conn, schema, user_id, old_batch, old_started - timedelta(minutes=5))
    new_play = _insert_play(conn, schema, user_id, new_batch, NOW - timedelta(hours=2))

    deleted = purge_collection_records(conn, now=NOW, retention=WINDOW, schema=schema)

    assert deleted >= 1
    assert _plays(conn, schema, user_id) == {old_play: None, new_play: new_batch}


def test_purge_keeps_plays_in_the_migrated_schema(db: Connection) -> None:
    if "unified_event" not in live_tables(db):
        pytest.skip("unified_event not migrated yet (WHO-6)")

    assert _ingest_batch_delete_rule(db, "public") == "SET NULL"
    _assert_purge_keeps_plays(db, "public")


def test_purge_keeps_plays_on_the_schema_v1_ddl(db: Connection) -> None:
    _create_v1_tables(db, SCHEMA, "SET NULL")

    assert _ingest_batch_delete_rule(db, SCHEMA) == "SET NULL"
    _assert_purge_keeps_plays(db, SCHEMA)


def test_selftest_cascade_would_delete_old_plays(db: Connection) -> None:
    _create_v1_tables(db, SCHEMA, "CASCADE")

    assert _ingest_batch_delete_rule(db, SCHEMA) == "CASCADE"
    with pytest.raises(AssertionError):
        _assert_purge_keeps_plays(db, SCHEMA)


def test_selftest_restrict_would_fail_the_purge(db: Connection) -> None:
    _create_v1_tables(db, SCHEMA, "RESTRICT")

    assert _ingest_batch_delete_rule(db, SCHEMA) == "RESTRICT"
    with pytest.raises(IntegrityError):
        _assert_purge_keeps_plays(db, SCHEMA)
