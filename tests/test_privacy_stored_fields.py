"""The "what we collect" field list matches the live database (PRD P8, story S6.2b).

Adding a column without adding it to ``STORED_FIELDS`` fails
``test_every_live_column_is_declared``. The ``selftest`` tests prove the checks
catch that, against throwaway tables, so they are not passing vacuously while
the schema is still small.
"""

from collections import Counter

import pytest
from sqlalchemy import Connection, text

from app.privacy.audit import (
    USER_TABLE,
    live_columns,
    live_tables,
    stale_declarations,
    undeclared_columns,
)
from app.privacy.provider_extras import PROVIDER_EXTRAS_ALLOWLIST
from app.privacy.stored_fields import NOT_USER_DATA, STORED_FIELDS, Category

SELFTEST_SCHEMA = "privacy_selftest"


def test_every_live_column_is_declared(db: Connection) -> None:
    missing = undeclared_columns(db)

    assert missing == set(), (
        "Columns stored but not in app.privacy.stored_fields.STORED_FIELDS "
        f"(the 'what we collect' list would be incomplete): {sorted(missing)}"
    )


def test_declared_columns_exist_in_their_table(db: Connection) -> None:
    stale = stale_declarations(db)

    assert stale == set(), f"Declared but not stored (the screen would overclaim): {sorted(stale)}"


def test_every_declared_table_exists_once_the_schema_does(db: Connection) -> None:
    tables = live_tables(db)
    if USER_TABLE not in tables:
        pytest.skip(f"{USER_TABLE} not migrated yet (WHO-6); declarations may land first")

    declared = {f.table for f in STORED_FIELDS} | {table for table, _ in NOT_USER_DATA}

    assert declared - tables == set()


def test_each_field_is_declared_once_with_one_category_and_a_label() -> None:
    keys = [(f.table, f.column) for f in STORED_FIELDS]
    duplicates = [key for key, n in Counter(keys).items() if n > 1]

    assert duplicates == []
    assert set(keys) & set(NOT_USER_DATA) == set()
    assert all(isinstance(f.category, Category) and f.label.strip() for f in STORED_FIELDS)
    assert all(reason.strip() for reason in NOT_USER_DATA.values())


def test_every_provider_extras_key_has_a_category_and_a_label() -> None:
    for keys in PROVIDER_EXTRAS_ALLOWLIST.values():
        for category, label in keys.values():
            assert isinstance(category, Category)
            assert label.strip()


def test_search_tsv_is_not_stored(db: Connection) -> None:
    """CTO decision on WHO-17: no search in the MVP, so no derived search data.

    Search is PRD F19 (Could, slice L). Bring it back with the F19 story, a P8
    reason in the PRD and a STORED_FIELDS entry, and delete this test then.
    """
    trgm = db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).first()

    assert "search_tsv" not in {column for _, column in live_columns(db)}
    assert "search_tsv" not in {f.column for f in STORED_FIELDS}
    assert trgm is None


def test_selftest_an_added_column_is_reported(db: Connection) -> None:
    db.execute(text(f"CREATE SCHEMA {SELFTEST_SCHEMA}"))
    db.execute(text(f"CREATE TABLE {SELFTEST_SCHEMA}.ingest_batch (id uuid, device_name text)"))

    assert undeclared_columns(db, SELFTEST_SCHEMA) == {("ingest_batch", "device_name")}


def test_selftest_an_added_table_is_reported(db: Connection) -> None:
    db.execute(text(f"CREATE SCHEMA {SELFTEST_SCHEMA}"))
    db.execute(text(f"CREATE TABLE {SELFTEST_SCHEMA}.listening_notes (user_id uuid, note text)"))

    assert undeclared_columns(db, SELFTEST_SCHEMA) == {
        ("listening_notes", "user_id"),
        ("listening_notes", "note"),
    }


def test_selftest_a_declared_column_missing_from_its_table_is_reported(db: Connection) -> None:
    db.execute(text(f"CREATE SCHEMA {SELFTEST_SCHEMA}"))
    db.execute(text(f"CREATE TABLE {SELFTEST_SCHEMA}.ingest_batch (id uuid)"))

    stale = stale_declarations(db, SELFTEST_SCHEMA)

    assert ("ingest_batch", "user_id") in stale
    assert ("ingest_batch", "id") not in stale
    assert all(table == "ingest_batch" for table, _ in stale)


# WHO-90 `copy` §2.5, as Product wrote and Security reviewed it (WHO-93 F3). A
# change here needs Product's re-approval first: the code must not drift from it.
APPROVED_COPY_2_5 = {
    ("spotify_connection", "collection_started_at"): (
        Category.CONNECTION,
        "When collection started: plays from before it are stored only if we imported "
        "them from your recent Spotify plays or your Spotify history file",
    ),
    ("unified_event", "ingest_method"): (
        Category.COLLECTION_RECORDS,
        "Whether we collected it, imported it from your recent Spotify plays, or you "
        "imported it from a file",
    ),
    ("spotify_connection", "history_import_requested"): (
        Category.CONNECTION,
        "Whether we import your recent Spotify plays when you first connect. Always yes "
        "for new accounts; accounts that connected before this was automatic may say no",
    ),
    ("spotify_connection", "history_imported_at"): (
        Category.CONNECTION,
        "When we imported them, empty if we have not",
    ),
}


@pytest.mark.parametrize(("table", "column"), list(APPROVED_COPY_2_5))
def test_history_import_labels_match_the_approved_copy(table: str, column: str) -> None:
    (field,) = [f for f in STORED_FIELDS if (f.table, f.column) == (table, column)]

    assert (field.category, field.label) == APPROVED_COPY_2_5[(table, column)]


def test_no_label_says_the_user_chose_to_import() -> None:
    # The recent-plays import is always on (WHO-114), so "you chose" is false (WHO-120).
    chose = [(f.table, f.column) for f in STORED_FIELDS if "chose to import" in f.label]

    assert chose == []
