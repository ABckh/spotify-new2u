"""Collection gaps: when collection of a source stopped and started again.

WHO-44, the D-7 gap label (WHO-18 ``gap-label``; WHO-13 R-52, R-55). One row
per stretch in which plays of a source may be missing. In the Spotify slice
that is a revoked or disconnected connection: ``started_at`` is the last
successful poll (or the collection start if none succeeded), ``ended_at`` is
the reconnect, NULL while the stretch is still open.

Kept as long as plays are kept (CTO decision on WHO-18 Q1): the 90-day purge of
collection records never touches it, only the ``app_user`` delete does. W2 line
"Gaps in collection". Not part of the frozen contract.

At most one open gap per user and source. A connection that is already
``reauth_required`` when this runs gets its open gap here, so a revoke from
before the upgrade still shows. Its start is the last successful poll, never
before the collection start (``app.events.gaps.gap_start``; GREATEST skips NULL).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE collection_gap (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
            source     text NOT NULL,
            started_at timestamptz NOT NULL,
            ended_at   timestamptz,
            CONSTRAINT collection_gap_order_chk
                CHECK (ended_at IS NULL OR ended_at >= started_at)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX collection_gap_one_open_uq "
        "ON collection_gap (user_id, source) WHERE ended_at IS NULL"
    )
    op.execute("CREATE INDEX collection_gap_user_idx ON collection_gap (user_id, started_at)")
    op.execute(
        """
        INSERT INTO collection_gap (user_id, source, started_at)
        SELECT user_id, 'spotify', GREATEST(last_polled_at, collection_started_at)
        FROM spotify_connection
        WHERE status = 'reauth_required'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE collection_gap")
