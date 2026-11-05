"""Read the live schema so the privacy tests never trust a hardcoded table list.

Every function takes the Postgres schema name so the tests can prove the checks
themselves against throwaway tables, without touching ``public``.

The queries read ``pg_catalog``, not ``information_schema``: the latter lists
only objects the current role holds a privilege on, so a role without table
privileges would see no tables and every check would pass (WHO-24 R2).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from app.privacy.provider_extras import PROVIDER_EXTRAS_ALLOWLIST
from app.privacy.stored_fields import NOT_USER_DATA, STORED_FIELDS, SYSTEM_TABLES

USER_TABLE = "app_user"

# Every relkind that stores rows: table, partitioned table, materialized view.
# Partitions are left out because their parent's rows already include theirs.
_STORED_RELATIONS = (
    "FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace "
    "WHERE ns.nspname = :schema AND c.relkind IN ('r', 'p', 'm') AND NOT c.relispartition"
)


class AuditSawNothing(RuntimeError):
    """The schema is migrated but the audit cannot see ``app_user``.

    A check that sees no tables reports no problems, so seeing nothing has to be
    an error rather than a pass.
    """


def live_tables(conn: Connection, schema: str = "public") -> set[str]:
    return set(_relkinds(conn, schema))


def _relkinds(conn: Connection, schema: str) -> dict[str, str]:
    """Relation name -> ``pg_class.relkind`` for every relation that stores rows."""
    rows = conn.execute(
        text(f"SELECT c.relname, c.relkind {_STORED_RELATIONS}"), {"schema": schema}
    )
    return {row.relname: row.relkind for row in rows}


def live_columns(conn: Connection, schema: str = "public") -> set[tuple[str, str]]:
    """Every (table, column) that stores rows in the schema, system tables excluded."""
    rows = conn.execute(
        text(
            "SELECT c.relname, a.attname "
            "FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
            "WHERE ns.nspname = :schema AND c.relkind IN ('r', 'p', 'm') "
            "AND NOT c.relispartition"
        ),
        {"schema": schema},
    )
    return {(row.relname, row.attname) for row in rows if row.relname not in SYSTEM_TABLES}


def undeclared_columns(conn: Connection, schema: str = "public") -> set[tuple[str, str]]:
    """Live columns missing from both STORED_FIELDS and NOT_USER_DATA."""
    declared = {(f.table, f.column) for f in STORED_FIELDS} | set(NOT_USER_DATA)
    return live_columns(conn, schema) - declared


def stale_declarations(conn: Connection, schema: str = "public") -> set[tuple[str, str]]:
    """Declared columns of an existing table that the table does not have.

    Only tables that exist are checked, so the declarations can land before the
    migration that creates them. Once ``app_user`` exists every declared table
    must exist too; the test enforces that separately.
    """
    tables = live_tables(conn, schema)
    declared = {(f.table, f.column) for f in STORED_FIELDS} | set(NOT_USER_DATA)
    return {tc for tc in declared if tc[0] in tables} - live_columns(conn, schema)


@dataclass(frozen=True)
class DeleteCoverageProblem:
    table: str
    problem: str


def delete_coverage_problems(
    conn: Connection, schema: str = "public"
) -> list[DeleteCoverageProblem]:
    """Every non-system table must be reached by ``DELETE FROM app_user``.

    The rule is deliberately strict and checkable: each table other than
    ``app_user`` has a NOT NULL ``user_id`` with a foreign key to ``app_user(id)``
    in the same schema that is ``ON DELETE CASCADE``. A table that hangs off
    another table instead of off the user would be invisible to the per-user row
    count, so it is rejected here rather than trusted.

    A materialized view is always a problem: a cascade does not reach it, so it
    keeps a deleted user's rows until it is refreshed. Allowing one means making
    ``delete_all_user_data`` refresh it first.
    """
    relkinds = _relkinds(conn, schema)
    if USER_TABLE not in relkinds and "alembic_version" in relkinds:
        raise AuditSawNothing(f"{schema}.alembic_version exists but {schema}.{USER_TABLE} does not")
    problems: list[DeleteCoverageProblem] = []
    nullable = {
        row.relname: not row.attnotnull
        for row in conn.execute(
            text(
                "SELECT c.relname, a.attnotnull "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
                "WHERE ns.nspname = :schema AND a.attname = 'user_id' AND NOT a.attisdropped"
            ),
            {"schema": schema},
        )
    }
    cascading = {
        row.table_name
        for row in conn.execute(
            text(
                "SELECT src.relname AS table_name "
                "FROM pg_constraint con "
                "JOIN pg_class src ON src.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = src.relnamespace "
                "JOIN pg_class dst ON dst.oid = con.confrelid "
                "JOIN pg_attribute att "
                "  ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1] "
                "JOIN pg_attribute dst_id "
                "  ON dst_id.attrelid = dst.oid AND dst_id.attname = 'id' "
                "WHERE con.contype = 'f' AND ns.nspname = :schema "
                "  AND dst.relname = :user_table AND dst.relnamespace = src.relnamespace "
                "  AND con.confkey = ARRAY[dst_id.attnum] "
                "  AND array_length(con.conkey, 1) = 1 "
                "  AND att.attname = 'user_id' AND con.confdeltype = 'c'"
            ),
            {"schema": schema, "user_table": USER_TABLE},
        )
    }
    for table in sorted(set(relkinds) - SYSTEM_TABLES - {USER_TABLE}):
        if relkinds[table] == "m":
            problems.append(
                DeleteCoverageProblem(table, "materialized view, not reached by delete")
            )
            continue
        if table not in nullable:
            problems.append(DeleteCoverageProblem(table, "has no user_id column"))
            continue
        if nullable[table]:
            problems.append(DeleteCoverageProblem(table, "user_id is nullable"))
        if table not in cascading:
            problems.append(
                DeleteCoverageProblem(
                    table, f"user_id has no foreign key to {USER_TABLE}(id) ON DELETE CASCADE"
                )
            )
    return problems


def rows_held_for_user(conn: Connection, user_id: UUID, schema: str = "public") -> dict[str, int]:
    """Row count per user table for one user, tables read from pg_catalog."""
    counts: dict[str, int] = {}
    for table in sorted(live_tables(conn, schema) - SYSTEM_TABLES):
        column = "id" if table == USER_TABLE else "user_id"
        # Identifiers come from pg_catalog, never from input; quote anyway.
        sql = f'SELECT count(*) FROM "{schema}"."{table}" WHERE "{column}" = :user_id'
        counts[table] = conn.execute(text(sql), {"user_id": user_id}).scalar_one()
    return counts


def unlisted_provider_extras_keys(
    conn: Connection,
    schema: str = "public",
    allowlist: Mapping[str, Mapping[str, object]] = PROVIDER_EXTRAS_ALLOWLIST,
) -> set[tuple[str, str]]:
    """(source, key) pairs stored in provider_extras that the allowlist does not permit."""
    if "unified_event" not in live_tables(conn, schema):
        return set()
    rows = conn.execute(
        text(
            f"SELECT DISTINCT source, jsonb_object_keys(provider_extras) AS key "
            f'FROM "{schema}"."unified_event"'
        )
    )
    return {(row.source, row.key) for row in rows if row.key not in allowlist.get(row.source, {})}
