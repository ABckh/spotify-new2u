"""Manual poll requests and their cooldown (WHO-95).

The API never calls Spotify for a manual poll. It records the request in
``spotify_connection.poll_requested_at``; the poller picks it up between ticks
(``app.poller.spotify.connections_with_poll_request``).

A request is refused while the cooldown runs: less than
``MANUAL_POLL_COOLDOWN_SECONDS`` since the newest poll attempt for the user
(any ``ingest_batch`` row, scheduled or manual, failed or not, except a
history file import, WHO-104) or since the last request. Both live in the
database, so restarts and both processes count.
The check and the write are one ``UPDATE``: of two requests at the same time,
the second waits on the row lock, then sees the first one's
``poll_requested_at`` and matches nothing.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, text

from app.spotify.plays import SOURCE


@dataclass(frozen=True)
class PollState:
    status: str  # active | reauth_required
    last_poll_attempt_at: datetime | None
    poll_requested_at: datetime | None

    def next_manual_poll_at(self, cooldown: timedelta) -> datetime | None:
        """When a manual poll is next allowed; ``None`` if nothing has started a cooldown."""
        latest = [t for t in (self.last_poll_attempt_at, self.poll_requested_at) if t is not None]
        return max(latest) + cooldown if latest else None


@dataclass(frozen=True)
class Accepted:
    requested_at: datetime
    next_allowed_at: datetime


@dataclass(frozen=True)
class CoolingDown:
    next_allowed_at: datetime


@dataclass(frozen=True)
class NotConnected:
    """No connection, or one that is not ``active``: nothing to poll."""


def poll_state(conn: Connection, user_id: UUID) -> PollState | None:
    row = conn.execute(
        text(
            "SELECT c.status, c.poll_requested_at, ("
            "  SELECT max(b.started_at) FROM ingest_batch b "
            "  WHERE b.user_id = c.user_id AND b.source = :source "
            "  AND b.ingest_method <> 'file_import'"
            ") AS last_poll_attempt_at "
            "FROM spotify_connection c WHERE c.user_id = :user_id"
        ),
        {"user_id": user_id, "source": SOURCE},
    ).one_or_none()
    if row is None:
        return None
    return PollState(row.status, row.last_poll_attempt_at, row.poll_requested_at)


def request_poll(
    conn: Connection, user_id: UUID, now: datetime, cooldown: timedelta
) -> Accepted | CoolingDown | NotConnected:
    """Record a manual poll request, unless the connection is not active or cooling down."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    since = now - cooldown
    requested_at: datetime | None = conn.execute(
        text(
            "UPDATE spotify_connection c SET poll_requested_at = :now "
            "WHERE c.user_id = :user_id AND c.status = 'active' "
            "  AND (c.poll_requested_at IS NULL OR c.poll_requested_at <= :since) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM ingest_batch b WHERE b.user_id = c.user_id "
            "    AND b.source = :source AND b.ingest_method <> 'file_import' "
            "    AND b.started_at > :since) "
            "RETURNING c.poll_requested_at"
        ),
        {"user_id": user_id, "now": now, "since": since, "source": SOURCE},
    ).scalar_one_or_none()
    if requested_at is not None:
        return Accepted(requested_at, requested_at + cooldown)
    state = poll_state(conn, user_id)
    if state is None or state.status != "active":
        return NotConnected()
    # Matched nothing while active, so a poll or a request started a cooldown.
    # Only a reconnect racing this request leaves neither; wait a full one then.
    return CoolingDown(state.next_manual_poll_at(cooldown) or now + cooldown)
