"""The album reads use the indexes of migration 0017.

An index on an expression is used only when the query writes the expression
the same way. If ``app.read.names.normalized_name`` or a read's filters change
and stop matching, the reads still answer correctly but go back to reading
every play, which after a history import is ~100k rows per request. Each read
is planned over a few thousand plays with sequential scans off, and each index
must be used for what it is for, not only to find the user's plays.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Connection, TextClause, text

from app.read import album_tracks, albums, new_to_you, ratings
from app.read.album_sql import ALBUM_EVENTS

PARAMS = {
    "user_id": uuid4(),
    "album_id": "ALB1",
    "start": datetime(2026, 10, 1, tzinfo=UTC),
    "end": datetime(2026, 11, 1, tzinfo=UTC),
    "zone": "Europe/Warsaw",
    "limit": 10,
    "commented": True,
}
# Each index, and what its condition must look up: with only ``user_id`` the
# planner could use any of them for a whole user's plays. The partial index is
# used at all only when the query implies its ``WHERE``.
LOOKUPS = {
    "unified_event_spotify_album_idx": "user_id",
    "unified_event_item_idx": "item_key",
    "unified_event_album_name_idx": "regexp_replace(NORMALIZE(collection, NFC)",
}


# A history import's shape, small: 3,000 plays of 300 albums by name only, and
# 60 live plays with an album ID. Planned on an empty table, the reads would
# look up only ``user_id`` and filter the rest. Every value is synthetic.
_PLAYS = text(
    "INSERT INTO unified_event ("
    "  user_id, source, source_artifact, ingest_method, kind, occurred_at,"
    "  occurred_at_precision, title, creators, collection, external_ids,"
    "  item_key, item_key_source, dedupe_key, day_key) "
    "SELECT :user_id, 'spotify', 'test', CASE WHEN n <= 60 THEN 'api_poll' ELSE 'file_import' END,"
    "  'listen', timestamptz '2026-10-30' - n * interval '1 hour', 'second', 'Track ' || n,"
    "  ARRAY['Artist ' || n % 300], 'Album ' || n % 300,"
    "  CASE WHEN n <= 60 THEN jsonb_build_object('spotify_album_id', 'A' || n % 300)"
    "       ELSE '{}'::jsonb END,"
    "  'id:T' || n % 1500, 'provider_id', sha256(n::text::bytea), sha256(n::text::bytea) "
    "FROM generate_series(1, 3060) AS n"
)


def _index_lookups(db: Connection, statement: TextClause) -> dict[str, list[str]]:
    """Every index scan in the plan of ``statement``: index name -> its conditions."""
    db.execute(
        text("INSERT INTO app_user (id, home_timezone) VALUES (:user_id, 'Europe/Warsaw')"),
        PARAMS,
    )
    db.execute(_PLAYS, PARAMS)
    db.execute(text("ANALYZE unified_event"))
    db.execute(text("SET LOCAL enable_seqscan = off"))
    params = {k: v for k, v in PARAMS.items() if f":{k}" in statement.text}
    plan = db.execute(text(f"EXPLAIN (FORMAT JSON) {statement.text}"), params).scalar_one()
    found: dict[str, list[str]] = {}

    def walk(node: dict[str, Any]) -> None:
        if "Index Name" in node:
            found.setdefault(node["Index Name"], []).append(node.get("Index Cond", ""))
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    return found


def _uses(lookups: dict[str, list[str]], index: str) -> bool:
    return any(LOOKUPS[index] in cond for cond in lookups.get(index, []))


@pytest.mark.parametrize(
    "statement",
    [
        text(f"WITH {ALBUM_EVENTS} SELECT count(*) FROM user_events"),
        new_to_you._NEW_RELEASES,
        albums._ALBUMS,
        album_tracks._TRACKS,
        ratings._ALL_RATINGS,
    ],
    ids=["album_events", "new_releases", "albums", "album_tracks", "ratings"],
)
def test_album_reads_find_the_plays_with_an_album_through_indexes(
    db: Connection, statement: TextClause
) -> None:
    lookups = _index_lookups(db, statement)
    assert {index for index in LOOKUPS if _uses(lookups, index)} == set(LOOKUPS)


def test_new_songs_look_up_earlier_plays_by_track(db: Connection) -> None:
    assert _uses(_index_lookups(db, new_to_you._NEW_TRACKS), "unified_event_item_idx")
