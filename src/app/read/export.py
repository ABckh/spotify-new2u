"""``GET /v1/export``: everything we hold about the calling user, as one JSON document.

Streamed straight from server-side cursors into the response (PRD P13): no
file, no temporary table, no full copy in memory. One read-only REPEATABLE READ
transaction, so every section shows the same moment even while the poller writes.

Format (``format_version`` 1), documented for the user in ``api-contract``::

    {"format": "new2u-export", "format_version": 1, "exported_at": "...",
     "user_id": "...",
     "account": {...} | null,
     "spotify_connection": {..., "refresh_token_stored": true} | null,
     "sessions": [...], "renewals": [...], "collection_records": [...],
     "history_imports": [...], "collection_gaps": [...], "not_new_marks": [...],
     "album_ratings": [...], "album_comments": [...], "track_comments": [...],
     "events": [...]}

A collection gap is not a play, so it is never in ``events`` (S2.10f); it has
its own ``collection_gaps`` section because W2 says we keep it (WHO-47).

Each section holds every stored column of its table under the column's own
name, except the secrets in ``WITHHELD``: the export says whether a Spotify
token is stored, never the token. ``tests/test_read_export.py`` checks this
against ``STORED_FIELDS``, so a new column cannot be left out silently.
``events[].provider_extras`` is exported whole, so every allowlisted key is in it,
``spotify_album_total_tracks`` (WHO-141) and ``spotify_album_type`` (WHO-162)
included.

Values: instants are UTC RFC 3339 with full stored precision and ``Z``; ``bytea``
is lowercase hex; ``jsonb`` and arrays are JSON. If anything fails mid-stream the
generator raises, the server drops the connection without the final chunk, and
the client sees a truncated, unparseable document, never a short valid one.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import Connection, Engine, RowMapping, text

from app.spotify.artwork import safe_artwork_url

FORMAT = "new2u-export"
FORMAT_VERSION = 1
# Rows fetched from Postgres per round trip, and bytes per response chunk.
BATCH_ROWS = 500
CHUNK_BYTES = 64 * 1024

# Stored columns deliberately not exported, each with its reason.
WITHHELD: Final[dict[tuple[str, str], str]] = {
    ("spotify_connection", "refresh_token_ciphertext"): (
        "The Spotify token, encrypted. Never leaves the server; "
        "refresh_token_stored says whether one is held."
    ),
    ("auth_session", "claim_secret_hash"): "A keyed hash of a one-time sign-in secret.",
    ("auth_session", "token_hash"): "A keyed hash of a session key.",
    ("auth_session", "refresh_token_hash"): "A keyed hash of a refresh key.",
    ("auth_session", "previous_refresh_hash"): "A keyed hash of the refresh key it replaced.",
}


@dataclass(frozen=True)
class Section:
    key: str
    table: str
    columns: tuple[str, ...]
    owner_column: str
    order_by: str
    single: bool = False
    extra: str = ""  # computed columns appended to the SELECT


SECTIONS: Final[tuple[Section, ...]] = (
    Section("account", "app_user", ("id", "home_timezone"), "id", "id", single=True),
    Section(
        "spotify_connection",
        "spotify_connection",
        (
            "user_id",
            "spotify_account_id",
            "spotify_display_name",
            "collection_started_at",
            "authorized_at",
            "status",
            "reauth_reason",
            "last_polled_at",
            "history_import_requested",
            "history_imported_at",
            "poll_requested_at",
            "library_baseline_at",
            "review_cleared_at",
        ),
        "user_id",
        "user_id",
        single=True,
        extra="refresh_token_ciphertext IS NOT NULL AS refresh_token_stored",
    ),
    Section(
        "sessions",
        "auth_session",
        ("id", "user_id", "login_id", "created_at", "expires_at", "refresh_expires_at"),
        "user_id",
        "created_at, id",
    ),
    Section("renewals", "spotify_renewal", ("login_id", "user_id"), "user_id", "login_id"),
    Section(
        "collection_records",
        "ingest_batch",
        (
            "id",
            "user_id",
            "source",
            "source_artifact",
            "ingest_method",
            "started_at",
            "finished_at",
            "window_start",
            "window_end",
            "rows_seen",
            "rows_inserted",
            "rows_upgraded",
            "rows_skipped",
            "status",
            "error_kind",
        ),
        "user_id",
        "started_at, id",
    ),
    Section(
        "history_imports",
        "history_import",
        (
            "id",
            "user_id",
            "file_kind",
            "status",
            "error_kind",
            "created_at",
            "started_at",
            "finished_at",
            "ingest_batch_id",
            "rows_seen",
            "rows_inserted",
            "rows_duplicate",
            "rows_skipped_not_music",
            "rows_skipped_incognito",
            "rows_skipped_short",
            "rows_unreadable",
        ),
        "user_id",
        "created_at, id",
    ),
    Section(
        "collection_gaps",
        "collection_gap",
        ("id", "user_id", "source", "started_at", "ended_at"),
        "user_id",
        "started_at, id",
    ),
    Section(
        "not_new_marks",
        "not_new_mark",
        ("user_id", "kind", "item_id", "created_at", "source"),
        "user_id",
        "created_at, kind, item_id",
    ),
    Section(
        "album_ratings",
        "album_rating",
        ("user_id", "album_id", "rating", "rated_at"),
        "user_id",
        "rated_at, album_id",
    ),
    Section(
        "album_comments",
        "album_comment",
        ("user_id", "album_id", "comment", "commented_at"),
        "user_id",
        "commented_at, album_id",
    ),
    Section(
        "track_comments",
        "track_comment",
        ("user_id", "item_key", "comment", "commented_at"),
        "user_id",
        "commented_at, item_key",
    ),
    Section(
        "events",
        "unified_event",
        (
            "id",
            "user_id",
            "source",
            "source_artifact",
            "source_event_id",
            "ingest_method",
            "ingest_batch_id",
            "ingested_at",
            "kind",
            "occurred_at",
            "occurred_at_precision",
            "duration_ms",
            "played_ms",
            "title",
            "creators",
            "collection",
            "external_ids",
            "artwork_url",
            "provider_extras",
            "item_key",
            "item_key_source",
            "dedupe_key",
            "day_key",
            "dedupe_key_version",
        ),
        "user_id",
        "occurred_at, id",
    ),
)


def _row_json(row: RowMapping) -> str:
    """One row as JSON. ``artwork_url`` is checked again, as in the read API (WHO-148 rule 10)."""
    values = {k: json_value(v) for k, v in row.items()}
    if "artwork_url" in values:
        values["artwork_url"] = safe_artwork_url(values["artwork_url"])
    return _dumps(values)


def json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | memoryview):
        return bytes(value).hex()
    return value


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _select(section: Section) -> str:
    columns = ", ".join(section.columns + ((section.extra,) if section.extra else ()))
    # Names come from SECTIONS above, never from the request.
    return (
        f"SELECT {columns} FROM {section.table} "
        f"WHERE {section.owner_column} = :user_id ORDER BY {section.order_by}"
    )


def export_chunks(conn: Connection, user_id: UUID, now: datetime) -> Iterator[str]:
    """The export document for ``user_id``, in pieces. Caller owns the transaction."""
    yield (
        "{"
        f'"format":{_dumps(FORMAT)},"format_version":{FORMAT_VERSION},'
        f'"exported_at":{_dumps(json_value(now))},"user_id":{_dumps(str(user_id))}'
    )
    # Per statement: Connection.execution_options would change the caller's connection.
    streaming = {"stream_results": True, "yield_per": BATCH_ROWS}
    for section in SECTIONS:
        yield f",{_dumps(section.key)}:"
        # One server-side cursor per section, closed before the next opens.
        statement = text(_select(section))
        with conn.execute(statement, {"user_id": user_id}, execution_options=streaming) as result:
            if section.single:
                row = result.mappings().one_or_none()
                yield "null" if row is None else _row_json(row)
                continue
            yield "["
            for index, row in enumerate(result.mappings()):
                prefix = "," if index else ""
                yield prefix + _row_json(row)
            yield "]"
    yield "}\n"


def stream_export(
    engine: Engine, user_id: UUID, now: datetime, idle_timeout_seconds: int
) -> Iterator[bytes]:
    """``export_chunks`` inside its own read-only snapshot, as response bytes.

    While the client is slow to read, the session sits idle in this transaction.
    After ``idle_timeout_seconds`` of that, Postgres ends the session, and the
    next fetch fails: the body is truncated, as for any failure mid-stream.
    """
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
        conn.begin(),
    ):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        # SET LOCAL, as a function so the value is a bound parameter.
        conn.execute(
            text("SELECT set_config('idle_in_transaction_session_timeout', :ms, true)"),
            {"ms": str(idle_timeout_seconds * 1000)},
        )
        buffer = bytearray()
        for chunk in export_chunks(conn, user_id, now):
            buffer += chunk.encode()
            if len(buffer) >= CHUNK_BYTES:
                yield bytes(buffer)
                buffer.clear()
        yield bytes(buffer)
