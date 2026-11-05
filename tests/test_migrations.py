"""Migrations run forward on an empty database and on one that holds rows.

Each test migrates its own throwaway database, so it never depends on, or
disturbs, the shared test database. The frozen tables are compared with schema
revision 3 (WHO-2 §5.1-§5.2) column by column, constraint by constraint, index
by index: a migration that drifts from the signed-off contract fails here.
"""

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, make_url, text
from sqlalchemy.exc import IntegrityError

from app.core.db import UTC_SESSION
from app.events.keys import item_key_from_provider_id
from app.events.store import PreciseEvent, WriteCounts, write_precise_event

FROZEN_TABLES = ("app_user", "ingest_batch", "unified_event")

# (table, column, type, nullable, default), schema revision 3.
FROZEN_COLUMNS = [
    ("app_user", "id", "uuid", "NO", "gen_random_uuid()"),
    ("app_user", "home_timezone", "text", "NO", "'UTC'::text"),
    ("ingest_batch", "id", "uuid", "NO", "gen_random_uuid()"),
    ("ingest_batch", "user_id", "uuid", "NO", None),
    ("ingest_batch", "source", "text", "NO", None),
    ("ingest_batch", "source_artifact", "text", "NO", None),
    ("ingest_batch", "ingest_method", "text", "NO", None),
    ("ingest_batch", "started_at", "timestamp with time zone", "NO", "now()"),
    ("ingest_batch", "finished_at", "timestamp with time zone", "YES", None),
    ("ingest_batch", "window_start", "timestamp with time zone", "YES", None),
    ("ingest_batch", "window_end", "timestamp with time zone", "YES", None),
    ("ingest_batch", "rows_seen", "integer", "NO", "0"),
    ("ingest_batch", "rows_inserted", "integer", "NO", "0"),
    ("ingest_batch", "rows_upgraded", "integer", "NO", "0"),
    ("ingest_batch", "rows_skipped", "integer", "NO", "0"),
    ("ingest_batch", "status", "text", "NO", "'running'::text"),
    ("ingest_batch", "error_kind", "text", "YES", None),
    ("unified_event", "id", "uuid", "NO", "gen_random_uuid()"),
    ("unified_event", "user_id", "uuid", "NO", None),
    ("unified_event", "source", "text", "NO", None),
    ("unified_event", "source_artifact", "text", "NO", None),
    ("unified_event", "source_event_id", "text", "YES", None),
    ("unified_event", "ingest_method", "text", "NO", None),
    ("unified_event", "ingest_batch_id", "uuid", "YES", None),
    ("unified_event", "ingested_at", "timestamp with time zone", "NO", "now()"),
    ("unified_event", "kind", "text", "NO", None),
    ("unified_event", "occurred_at", "timestamp with time zone", "NO", None),
    ("unified_event", "occurred_at_precision", "text", "NO", None),
    ("unified_event", "duration_ms", "integer", "YES", None),
    ("unified_event", "played_ms", "integer", "YES", None),
    ("unified_event", "title", "text", "NO", None),
    ("unified_event", "creators", "ARRAY", "NO", "'{}'::text[]"),
    ("unified_event", "collection", "text", "YES", None),
    ("unified_event", "external_ids", "jsonb", "NO", "'{}'::jsonb"),
    ("unified_event", "artwork_url", "text", "YES", None),
    ("unified_event", "provider_extras", "jsonb", "NO", "'{}'::jsonb"),
    ("unified_event", "item_key", "text", "NO", None),
    ("unified_event", "item_key_source", "text", "NO", None),
    ("unified_event", "dedupe_key", "bytea", "NO", None),
    ("unified_event", "day_key", "bytea", "NO", None),
    ("unified_event", "dedupe_key_version", "smallint", "NO", "1"),
]

FROZEN_CONSTRAINTS = {
    ("app_user", "PRIMARY KEY (id)"),
    ("ingest_batch", "PRIMARY KEY (id)"),
    ("ingest_batch", "FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE"),
    (
        "ingest_batch",
        "CHECK ((status = ANY (ARRAY['running'::text, 'succeeded'::text, 'failed'::text])))",
    ),
    (
        "ingest_batch",
        "CHECK (((rows_seen >= 0) AND (rows_inserted >= 0) AND (rows_upgraded >= 0) "
        "AND (rows_skipped >= 0)))",
    ),
    ("unified_event", "PRIMARY KEY (id)"),
    ("unified_event", "FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE"),
    (
        "unified_event",
        "FOREIGN KEY (ingest_batch_id) REFERENCES ingest_batch(id) ON DELETE SET NULL",
    ),
    ("unified_event", "CHECK (((duration_ms IS NULL) OR (duration_ms >= 0)))"),
    ("unified_event", "CHECK (((played_ms IS NULL) OR (played_ms >= 0)))"),
    ("unified_event", "CHECK ((octet_length(dedupe_key) = 32))"),
    ("unified_event", "CHECK ((octet_length(day_key) = 32))"),
    ("unified_event", "CHECK ((length(title) > 0))"),
    ("unified_event", "CHECK ((jsonb_typeof(external_ids) = 'object'::text))"),
    ("unified_event", "CHECK ((jsonb_typeof(provider_extras) = 'object'::text))"),
}

# Added after the frozen contract; no field or its meaning changes.
LATER_CONSTRAINTS = {
    # 0004 (WHO-24 R3), replaced by 0009 (WHO-135), 0010 (WHO-141) and 0011
    # (WHO-162): mirrors PROVIDER_EXTRAS_ALLOWLIST.
    (
        "unified_event",
        "CHECK (\nCASE source\n"
        "    WHEN 'spotify'::text THEN (((((provider_extras - 'spotify_album_artists'::text) "
        "- 'spotify_album_total_tracks'::text) - 'spotify_album_type'::text) = '{}'::jsonb) "
        "AND ((NOT (provider_extras ? 'spotify_album_artists'::text)) "
        "OR spotify_album_artists_ok((provider_extras -> 'spotify_album_artists'::text))) "
        "AND ((NOT (provider_extras ? 'spotify_album_total_tracks'::text)) "
        "OR spotify_album_total_tracks_ok("
        "(provider_extras -> 'spotify_album_total_tracks'::text))) "
        "AND ((NOT (provider_extras ? 'spotify_album_type'::text)) "
        "OR spotify_album_type_ok((provider_extras -> 'spotify_album_type'::text))))\n"
        "    ELSE (provider_extras = '{}'::jsonb)\nEND)",
    ),
}

# Schema §5.2 indexes 1-5 and 7 (6 and 8 removed in revision 2), plus primary keys.
FROZEN_INDEXES = {
    "ingest_batch_user_source_started_idx ON public.ingest_batch "
    "USING btree (user_id, source, started_at DESC)",
    "unified_event_user_dedupe_uq ON public.unified_event USING btree (user_id, dedupe_key)",
    "unified_event_timeline_idx ON public.unified_event "
    "USING btree (user_id, occurred_at DESC, id DESC)",
    "unified_event_source_timeline_idx ON public.unified_event "
    "USING btree (user_id, source, occurred_at DESC, id DESC)",
    "unified_event_day_key_idx ON public.unified_event USING btree (user_id, day_key)",
    "unified_event_ingest_batch_idx ON public.unified_event "
    "USING btree (ingest_batch_id) WHERE (ingest_batch_id IS NOT NULL)",
    "unified_event_creators_idx ON public.unified_event USING gin (creators)",
    "app_user_pkey ON public.app_user USING btree (id)",
    "ingest_batch_pkey ON public.ingest_batch USING btree (id)",
    "unified_event_pkey ON public.unified_event USING btree (id)",
}

# Added after the frozen contract for the album reads (0017); no field or its
# meaning changes.
LATER_INDEXES = {
    "unified_event_spotify_album_idx ON public.unified_event USING btree (user_id) "
    "WHERE ((external_ids ->> 'spotify_album_id'::text) IS NOT NULL)",
    "unified_event_item_idx ON public.unified_event "
    "USING btree (user_id, item_key, occurred_at)",
    "unified_event_album_name_idx ON public.unified_event USING btree (user_id, "
    "regexp_replace(NORMALIZE(collection, NFC), '^[[:space:]]+|[[:space:]]+$'::text, "
    "''::text, 'g'::text))",
}


@pytest.fixture
def fresh_database(migrated_engine: Engine) -> Iterator[str]:
    """The URL of a new, empty database, dropped afterwards."""
    name = f"migration_test_{uuid4().hex[:12]}"
    admin = migrated_engine.execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(os.environ["DATABASE_URL"]).set(database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def alembic(url: str, action: str, revision: str) -> None:
    config = AlembicConfig("alembic.ini")
    config.attributes["url"] = url
    getattr(alembic_command, action)(config, revision)


def connect(url: str) -> Engine:
    return create_engine(url, connect_args=UTC_SESSION)


def catalog(conn: Connection) -> tuple[list[tuple[str, ...]], set[tuple[str, str]], set[str]]:
    columns = [
        tuple(row)
        for row in conn.execute(
            text(
                "SELECT table_name, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY (:t) "
                "ORDER BY table_name, ordinal_position"
            ),
            {"t": list(FROZEN_TABLES)},
        )
    ]
    constraints = {
        (row.table_name, row.definition)
        for row in conn.execute(
            text(
                "SELECT conrelid::regclass::text AS table_name, "
                "  pg_get_constraintdef(oid) AS definition "
                "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                "AND conrelid::regclass::text = ANY (:t)"
            ),
            {"t": list(FROZEN_TABLES)},
        )
    }
    indexes = {
        row.indexdef.removeprefix("CREATE UNIQUE INDEX ").removeprefix("CREATE INDEX ")
        for row in conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = ANY (:t)"
            ),
            {"t": list(FROZEN_TABLES)},
        )
    }
    return columns, constraints, indexes


def test_forward_on_an_empty_database_gives_exactly_the_frozen_schema(fresh_database: str) -> None:
    alembic(fresh_database, "upgrade", "head")

    engine = connect(fresh_database)
    try:
        with engine.connect() as conn:
            columns, constraints, indexes = catalog(conn)
            extensions = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
    finally:
        engine.dispose()

    assert columns == FROZEN_COLUMNS
    assert constraints == FROZEN_CONSTRAINTS | LATER_CONSTRAINTS
    assert indexes == FROZEN_INDEXES | LATER_INDEXES
    assert "pg_trgm" not in extensions


def test_forward_on_a_database_with_rows_keeps_them(fresh_database: str) -> None:
    """Rows written at head survive re-running every migration step forward.

    With one revision the only earlier state is the empty database, which the
    test above covers; ``test_each_revision_runs_forward_over_rows`` below picks
    up every later revision automatically.
    """
    alembic(fresh_database, "upgrade", "head")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)

        alembic(fresh_database, "upgrade", "head")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM unified_event")).scalar_one() == 1
            assert conn.execute(text("SELECT user_id FROM unified_event")).scalar_one() == user_id
    finally:
        engine.dispose()


def _revisions_after_the_first() -> list[str]:
    script = ScriptDirectory.from_config(AlembicConfig("alembic.ini"))
    return [rev.revision for rev in script.walk_revisions() if rev.down_revision is not None]


LATER_REVISIONS = _revisions_after_the_first()


def test_revisions_form_one_chain_with_unique_ids() -> None:
    """Two branches that each add the next revision number must not both merge.

    Alembic only warns on a duplicate revision id and keeps one of the files,
    so the other migration would silently never run. Read the ids from the
    files themselves, then require a single head.
    """
    versions = Path("alembic/versions")
    ids = [
        match.group(1)
        for path in sorted(versions.glob("*.py"))
        if (match := re.search(r'^revision = "([^"]+)"$', path.read_text(), re.MULTILINE))
    ]
    assert len(ids) == len(list(versions.glob("*.py")))
    assert sorted(ids) == sorted(set(ids)), f"duplicate revision ids: {ids}"
    script = ScriptDirectory.from_config(AlembicConfig("alembic.ini"))
    assert len(script.get_heads()) == 1


@pytest.mark.skipif(not LATER_REVISIONS, reason="only one revision so far")
@pytest.mark.parametrize("revision", LATER_REVISIONS)
def test_each_revision_runs_forward_over_rows(fresh_database: str, revision: str) -> None:
    script = ScriptDirectory.from_config(AlembicConfig("alembic.ini"))
    previous = script.get_revision(revision).down_revision
    assert isinstance(previous, str)
    alembic(fresh_database, "upgrade", previous)
    engine = connect(fresh_database)
    try:
        _write_a_play(engine)
        alembic(fresh_database, "upgrade", revision)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM unified_event")).scalar_one() == 1
    finally:
        engine.dispose()


def test_downgrade_then_upgrade_round_trips(fresh_database: str) -> None:
    alembic(fresh_database, "upgrade", "head")
    alembic(fresh_database, "downgrade", "base")
    alembic(fresh_database, "upgrade", "head")


def _write_a_play(engine: Engine) -> UUID:
    with engine.begin() as conn:
        user_id: UUID = conn.execute(
            text("INSERT INTO app_user (home_timezone) VALUES ('Europe/Helsinki') RETURNING id")
        ).scalar_one()
        write_precise_event(
            conn,
            user_id=user_id,
            home_timezone="Europe/Helsinki",
            event=PreciseEvent(
                source="spotify",
                source_artifact="spotify_recently_played",
                ingest_method="api_poll",
                kind="listen",
                occurred_at=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
                title="Synthetic Track",
                item_key=item_key_from_provider_id("synthetic-track"),
            ),
            ingest_batch_id=None,
            counts=WriteCounts(),
        )
    return user_id


def test_0003_opens_a_gap_for_each_connection_already_needing_reauth(
    fresh_database: str,
) -> None:
    """A revoke from before the upgrade still shows as an open gap (WHO-44).

    Its start is the last successful poll, clamped to the collection start (WHO-47 (5)).
    """
    alembic(fresh_database, "upgrade", "0002")
    started = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    polled = datetime(2026, 10, 11, 9, 30, tzinfo=UTC)
    stale_poll = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)  # before a later reconnect
    engine = connect(fresh_database)
    try:
        with engine.begin() as conn:
            users = {}
            for name, status, last_polled_at in (
                ("polled", "reauth_required", polled),
                ("never_polled", "reauth_required", None),
                ("stale_poll", "reauth_required", stale_poll),
                ("active", "active", polled),
            ):
                users[name] = conn.execute(
                    text("INSERT INTO app_user DEFAULT VALUES RETURNING id")
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO spotify_connection (user_id, spotify_account_id, "
                        "  collection_started_at, authorized_at, refresh_token_ciphertext, "
                        "  status, last_polled_at) "
                        "VALUES (:u, :account, :started, :started, :token, :status, :polled)"
                    ),
                    {
                        "u": users[name],
                        "account": f"synthetic-{name}",
                        "started": started,
                        "token": b"synthetic" if status == "active" else None,
                        "status": status,
                        "polled": last_polled_at,
                    },
                )

        alembic(fresh_database, "upgrade", "0003")

        with engine.connect() as conn:
            gaps = {
                row.user_id: (row.source, row.started_at, row.ended_at)
                for row in conn.execute(
                    text("SELECT user_id, source, started_at, ended_at FROM collection_gap")
                )
            }
    finally:
        engine.dispose()

    assert gaps == {
        users["polled"]: ("spotify", polled, None),
        users["never_polled"]: ("spotify", started, None),
        users["stale_poll"]: ("spotify", started, None),
    }


def test_0005_keeps_sign_ins_in_progress_and_ties_renewals_to_both_ends(
    fresh_database: str,
) -> None:
    """WHO-61: a sign-in started before the upgrade survives it, and a renewal row
    goes with either its sign-in or its user."""
    alembic(fresh_database, "upgrade", "0004")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)
        with engine.begin() as conn:
            logins = [
                conn.execute(
                    text(
                        "INSERT INTO oauth_login (state_hash, code_verifier_ciphertext, "
                        "  claim_secret_hash, home_timezone, expires_at) "
                        "VALUES (:state, '\\x02', '\\x03', 'UTC', now()) RETURNING id"
                    ),
                    {"state": bytes([n])},
                ).scalar_one()
                for n in (1, 2)
            ]

        alembic(fresh_database, "upgrade", "0005")

        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM oauth_login")).scalar_one() == 2
            assert conn.execute(text("SELECT count(*) FROM spotify_renewal")).scalar_one() == 0
            # A sign-in started before the upgrade is not a renewal.
            assert conn.execute(
                text("SELECT array_agg(is_renewal) FROM oauth_login")
            ).scalar_one() == [False, False]
            for login_id in logins:
                conn.execute(
                    text("INSERT INTO spotify_renewal (login_id, user_id) VALUES (:l, :u)"),
                    {"l": login_id, "u": user_id},
                )
            conn.execute(text("DELETE FROM oauth_login WHERE id = :l"), {"l": logins[0]})
            assert conn.execute(text("SELECT count(*) FROM spotify_renewal")).scalar_one() == 1
            conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": user_id})
            assert conn.execute(text("SELECT count(*) FROM spotify_renewal")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM oauth_login")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0007_leaves_existing_connections_and_sign_ins_without_an_import(
    fresh_database: str,
) -> None:
    """WHO-89: a connection or sign-in from before the upgrade never imports history."""
    alembic(fresh_database, "upgrade", "0006")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO spotify_connection (user_id, spotify_account_id, "
                    "  collection_started_at, authorized_at, refresh_token_ciphertext, status) "
                    "VALUES (:u, 'synthetic-account', now(), now(), '\\x01', 'active')"
                ),
                {"u": user_id},
            )
            conn.execute(
                text(
                    "INSERT INTO oauth_login (state_hash, code_verifier_ciphertext, "
                    "  claim_secret_hash, home_timezone, expires_at) "
                    "VALUES ('\\x01', '\\x02', '\\x03', 'UTC', now())"
                )
            )

        alembic(fresh_database, "upgrade", "0007")

        with engine.connect() as conn:
            connection = conn.execute(
                text("SELECT history_import_requested, history_imported_at FROM spotify_connection")
            ).one()
            assert tuple(connection) == (False, None)
            assert (
                conn.execute(text("SELECT import_recent_history FROM oauth_login")).scalar_one()
                is False
            )
            assert conn.execute(text("SELECT count(*) FROM unified_event")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0009_allows_album_artists_and_downgrade_removes_them(fresh_database: str) -> None:
    """WHO-135: rows from before 0009 stay; downgrading strips the key 0004 forbids."""
    alembic(fresh_database, "upgrade", "0008")
    engine = connect(fresh_database)
    try:
        _write_a_play(engine)
        alembic(fresh_database, "upgrade", "0009")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE unified_event SET provider_extras = "
                    """'{"spotify_album_artists": ["Synthetic Label"]}'"""
                )
            )

        alembic(fresh_database, "downgrade", "0008")

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT provider_extras FROM unified_event")).scalar_one() == {}
            )
    finally:
        engine.dispose()


def test_0010_allows_album_total_tracks_and_downgrade_removes_them(fresh_database: str) -> None:
    """WHO-141: rows from before 0010 stay; downgrading strips only the key 0009 forbids."""
    alembic(fresh_database, "upgrade", "0009")
    engine = connect(fresh_database)
    try:
        _write_a_play(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE unified_event SET provider_extras = "
                    """'{"spotify_album_artists": ["Synthetic Label"]}'"""
                )
            )
        alembic(fresh_database, "upgrade", "0010")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE unified_event SET provider_extras = provider_extras || "
                    """'{"spotify_album_total_tracks": 12}'"""
                )
            )

        alembic(fresh_database, "downgrade", "0009")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT provider_extras FROM unified_event")).scalar_one() == {
                "spotify_album_artists": ["Synthetic Label"]
            }
    finally:
        engine.dispose()


def test_0011_allows_album_type_and_downgrade_removes_it(fresh_database: str) -> None:
    """WHO-162: rows from before 0011 stay; downgrading strips only the key 0010 forbids."""
    alembic(fresh_database, "upgrade", "0010")
    engine = connect(fresh_database)
    try:
        _write_a_play(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE unified_event SET provider_extras = "
                    """'{"spotify_album_total_tracks": 12}'"""
                )
            )
        alembic(fresh_database, "upgrade", "0011")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE unified_event SET provider_extras = provider_extras || "
                    """'{"spotify_album_type": "single"}'"""
                )
            )

        alembic(fresh_database, "downgrade", "0010")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT provider_extras FROM unified_event")).scalar_one() == {
                "spotify_album_total_tracks": 12
            }
    finally:
        engine.dispose()


def test_0013_keeps_marks_as_the_users_and_downgrade_drops_library_marks(
    fresh_database: str,
) -> None:
    """Known-music baseline: an old mark is the user's; a library mark goes with its column."""
    alembic(fresh_database, "upgrade", "0012")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO spotify_connection (user_id, spotify_account_id, "
                    "  collection_started_at, authorized_at, refresh_token_ciphertext, status) "
                    "VALUES (:u, 'synthetic-account', now(), now(), '\\x01', 'active')"
                ),
                {"u": user_id},
            )
            conn.execute(
                text(
                    "INSERT INTO not_new_mark (user_id, kind, item_id) "
                    "VALUES (:u, 'artist', 'Synthetic Artist')"
                ),
                {"u": user_id},
            )

        alembic(fresh_database, "upgrade", "0013")

        with engine.begin() as conn:
            assert conn.execute(text("SELECT source FROM not_new_mark")).scalar_one() == "user"
            assert tuple(
                conn.execute(
                    text("SELECT library_baseline_at, review_cleared_at FROM spotify_connection")
                ).one()
            ) == (None, None)
            conn.execute(
                text(
                    "INSERT INTO not_new_mark (user_id, kind, item_id, source) "
                    "VALUES (:u, 'album', 'SyntheticAlbum', 'spotify_library')"
                ),
                {"u": user_id},
            )
            with pytest.raises(Exception, match="not_new_mark_source_chk"):
                with conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO not_new_mark (user_id, kind, item_id, source) "
                            "VALUES (:u, 'album', 'Other', 'guess')"
                        ),
                        {"u": user_id},
                    )

        alembic(fresh_database, "downgrade", "0012")

        with engine.connect() as conn:
            assert [
                tuple(r) for r in conn.execute(text("SELECT kind, item_id FROM not_new_mark"))
            ] == [("artist", "Synthetic Artist")]
    finally:
        engine.dispose()


def test_0014_adds_history_import_over_existing_rows(fresh_database: str) -> None:
    """WHO-104: existing plays stay; one import per user unless it failed."""
    alembic(fresh_database, "upgrade", "0013")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)

        alembic(fresh_database, "upgrade", "0014")

        insert = text(
            "INSERT INTO history_import (id, user_id, file_kind, status, created_at) "
            "VALUES (gen_random_uuid(), :u, 'zip', :status, now())"
        )
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM unified_event")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM history_import")).scalar_one() == 0
            conn.execute(insert, {"u": user_id, "status": "failed"})
            conn.execute(insert, {"u": user_id, "status": "failed"})
            conn.execute(insert, {"u": user_id, "status": "succeeded"})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"u": user_id, "status": "queued"})
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM app_user"))
            assert conn.execute(text("SELECT count(*) FROM history_import")).scalar_one() == 0
    finally:
        engine.dispose()


def test_0015_adds_album_ratings_from_1_to_10(fresh_database: str) -> None:
    """Album ratings: existing rows stay; a rating outside 1..10 is refused."""
    alembic(fresh_database, "upgrade", "0014")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)
        alembic(fresh_database, "upgrade", "0015")
        insert = text("INSERT INTO album_rating (user_id, album_id, rating) VALUES (:u, :a, :r)")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM unified_event")).scalar_one() == 1
            conn.execute(insert, {"u": user_id, "a": "ALB1", "r": 10})
        for album_id, rating in (("ALB2", 0), ("ALB2", 11), ("bad id", 5)):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(insert, {"u": user_id, "a": album_id, "r": rating})
        alembic(fresh_database, "downgrade", "0014")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('album_rating')")).scalar_one() is None
    finally:
        engine.dispose()


def test_0016_adds_album_comments_apart_from_ratings(fresh_database: str) -> None:
    """Album comments: ratings stay as they were; a comment is 1 to 2000 characters."""
    alembic(fresh_database, "upgrade", "0015")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)
        rate = text("INSERT INTO album_rating (user_id, album_id, rating) VALUES (:u, 'ALB1', 7)")
        with engine.begin() as conn:
            conn.execute(rate, {"u": user_id})

        alembic(fresh_database, "upgrade", "0016")

        insert = text("INSERT INTO album_comment (user_id, album_id, comment) VALUES (:u, :a, :c)")
        with engine.begin() as conn:
            # The release before reads album_rating exactly as it did.
            assert conn.execute(text("SELECT album_id, rating FROM album_rating")).one() == (
                "ALB1",
                7,
            )
            conn.execute(insert, {"u": user_id, "a": "ALB1", "c": "x" * 2000})
            conn.execute(insert, {"u": user_id, "a": "ALB2", "c": "Only a comment"})
        for album_id, comment in (("ALB3", ""), ("ALB3", "x" * 2001), ("bad id", "Words")):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(insert, {"u": user_id, "a": album_id, "c": comment})
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM app_user"))
            assert conn.execute(text("SELECT count(*) FROM album_comment")).scalar_one() == 0
        alembic(fresh_database, "downgrade", "0015")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('album_comment')")).scalar_one() is None
            assert conn.execute(text("SELECT to_regclass('album_rating')")).scalar_one() is not None
    finally:
        engine.dispose()


def test_0017_adds_album_read_indexes_and_statistics(fresh_database: str) -> None:
    """Indexes and statistics only: the plays stay, and downgrade removes them all."""
    alembic(fresh_database, "upgrade", "0016")
    engine = connect(fresh_database)
    added = text(
        "SELECT (SELECT count(*) FROM pg_indexes WHERE indexname IN ("
        "  'unified_event_spotify_album_idx', 'unified_event_item_idx', "
        "  'unified_event_album_name_idx')) + "
        "(SELECT count(*) FROM pg_statistic_ext "
        "  WHERE stxname = 'unified_event_spotify_album_stx')"
    )
    try:
        _write_a_play(engine)
        alembic(fresh_database, "upgrade", "0017")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM unified_event")).scalar_one() == 1
            assert conn.execute(added).scalar_one() == 4
        alembic(fresh_database, "downgrade", "0016")
        with engine.connect() as conn:
            assert conn.execute(added).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM unified_event")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0018_adds_track_comments(fresh_database: str) -> None:
    """Track comments: one per user and track key, 1 to 2000 characters, gone with the user."""
    alembic(fresh_database, "upgrade", "0017")
    engine = connect(fresh_database)
    try:
        user_id = _write_a_play(engine)

        alembic(fresh_database, "upgrade", "0018")

        insert = text("INSERT INTO track_comment (user_id, item_key, comment) VALUES (:u, :k, :c)")
        with engine.begin() as conn:
            conn.execute(insert, {"u": user_id, "k": "id:TRK1", "c": "x" * 2000})
            conn.execute(insert, {"u": user_id, "k": "nt:" + "0" * 32, "c": "A local file"})
        for key, comment in (
            ("id:TRK2", ""),
            ("id:TRK2", "x" * 2001),
            ("TRK2", "Words"),
            ("id:a/b", "Words"),
            ("id:TRK1", "Twice"),
        ):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(insert, {"u": user_id, "k": key, "c": comment})
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM app_user"))
            assert conn.execute(text("SELECT count(*) FROM track_comment")).scalar_one() == 0
        alembic(fresh_database, "downgrade", "0017")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('track_comment')")).scalar_one() is None
            assert (
                conn.execute(text("SELECT to_regclass('album_comment')")).scalar_one() is not None
            )
    finally:
        engine.dispose()
