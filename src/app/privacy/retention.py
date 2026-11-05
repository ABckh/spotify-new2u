"""Collection records are kept for a fixed window, then removed (PRD P10, B10).

A collection record is one ``ingest_batch`` row: when we checked a source and
what we found. Fixed string W5 states how long they are kept. The window is
``COLLECTION_RECORDS_RETENTION_DAYS``, 90 days by default.

The poller calls ``purge_collection_records`` on every tick, so the removal
needs no user action and no cron entry. Plays are not touched:
``unified_event.ingest_batch_id`` is ``ON DELETE SET NULL`` (schema v1). Nor
are collection gaps (``collection_gap``): they are kept as long as plays are
(CTO decision on WHO-18 Q1), so an old gap is still shown (S2.10l).
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import Connection, text

from app.privacy.audit import live_tables

logger = logging.getLogger("app.privacy.retention")

TABLE = "ingest_batch"


def purge_collection_records(
    conn: Connection, now: datetime, retention: timedelta, schema: str = "public"
) -> int:
    """Delete every collection record that started before ``now - retention``.

    Returns the number of rows deleted. The caller owns the transaction.
    ``now`` must be timezone-aware, so the cutoff is an instant, not a wall time.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    # Before the schema v1 migration (WHO-6) the table does not exist yet.
    if TABLE not in live_tables(conn, schema):
        return 0
    cutoff = now - retention
    result = conn.execute(
        text(f'DELETE FROM "{schema}"."{TABLE}" WHERE started_at < :cutoff'),
        {"cutoff": cutoff},
    )
    # Counts and the cutoff only; a collection record names a user.
    logger.info("collection records purged rows=%d cutoff=%s", result.rowcount, cutoff.isoformat())
    return result.rowcount
