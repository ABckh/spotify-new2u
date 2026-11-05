"""``collection_gap``: stretches in which plays of a source may be missing (D-7).

In the Spotify slice a gap is a revoked or disconnected connection (WHO-18
``gap-label`` §1):

- **Start**: the last successful poll before collection stopped, never earlier
  than the current collection start (:func:`gap_start`). Everything up to it was
  collected. After a reconnect ``last_polled_at`` still holds the previous
  stretch's last poll, so without the clamp a revoke before the next successful
  poll would reach back over the previous gap (WHO-47 condition 5, S2.10k).
- **End**: the moment the user completed the reconnect, which is the new
  collection start (S1.8a). NULL while the connection is still revoked or
  disconnected (an open gap, G2).

A revoke opens a gap in ``app.spotify.connection.mark_reauth_required``, and a
reconnect closes it in ``app.spotify.connection.connect``. The disconnect action
(S10) does not exist yet; when it lands it calls :func:`open_gap` with the same
start, and its reconnect calls :func:`close_gap`.

At most one open gap per user and source (a partial unique index), so opening a
gap twice is a no-op. Gaps are never derived from collection records, so the
purge of old records cannot create or remove one (S2.10l).

Log lines carry the gap id, the user id and the source, never a start or end
time: next to a user id that would be listening behaviour in the logs (W6,
WHO-47 condition 6). The times are in the table, the export and the read API.
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

logger = logging.getLogger("app.events.gaps")


def gap_start(last_polled_at: datetime | None, collection_started_at: datetime) -> datetime:
    """Where a gap starts: the last successful poll, never before the collection start."""
    if last_polled_at is None:
        return collection_started_at
    return max(last_polled_at, collection_started_at)


def open_gap(conn: Connection, user_id: UUID, source: str, started_at: datetime) -> bool:
    """Open a gap for ``source`` at ``started_at``, unless one is already open.

    Returns whether a gap was opened. The caller owns the transaction.
    """
    if started_at.tzinfo is None:
        raise ValueError("started_at must be timezone-aware")
    gap_id: UUID | None = conn.execute(
        text(
            "INSERT INTO collection_gap (user_id, source, started_at) "
            "VALUES (:user_id, :source, :started_at) "
            "ON CONFLICT (user_id, source) WHERE ended_at IS NULL DO NOTHING "
            "RETURNING id"
        ),
        {"user_id": user_id, "source": source, "started_at": started_at},
    ).scalar_one_or_none()
    if gap_id is None:
        return False
    logger.info("collection gap opened gap_id=%s user_id=%s source=%s", gap_id, user_id, source)
    return True


def close_gap(conn: Connection, user_id: UUID, source: str, ended_at: datetime) -> bool:
    """Close the open gap of ``source`` at ``ended_at``. Returns whether one was open.

    An end before the start is clamped to the start, so a reconnect never fails
    on a clock step. The caller owns the transaction.
    """
    if ended_at.tzinfo is None:
        raise ValueError("ended_at must be timezone-aware")
    gap_id: UUID | None = conn.execute(
        text(
            "UPDATE collection_gap SET ended_at = GREATEST(started_at, :ended_at) "
            "WHERE user_id = :user_id AND source = :source AND ended_at IS NULL "
            "RETURNING id"
        ),
        {"user_id": user_id, "source": source, "ended_at": ended_at},
    ).scalar_one_or_none()
    if gap_id is None:
        return False
    logger.info("collection gap closed gap_id=%s user_id=%s source=%s", gap_id, user_id, source)
    return True
