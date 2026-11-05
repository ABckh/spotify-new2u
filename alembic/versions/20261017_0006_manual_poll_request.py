"""Manual poll requests: when the user last asked for a poll now (WHO-95).

``POST /v1/me/spotify/poll`` sets ``spotify_connection.poll_requested_at``. The
poller polls a connection whose request is newer than its last poll attempt
(``ingest_batch.started_at``) between ticks, so each request causes one poll.
The column also holds the manual-poll cooldown in the database, so it counts
across restarts and across the API and poller processes.

Not part of the frozen contract. The row goes with ``spotify_connection``,
which the ``app_user`` delete already reaches.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE spotify_connection ADD COLUMN poll_requested_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE spotify_connection DROP COLUMN poll_requested_at")
