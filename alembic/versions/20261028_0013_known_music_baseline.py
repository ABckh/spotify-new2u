"""Known-music baseline and the first-two-weeks review.

- ``not_new_mark.source``: ``user`` for a mark the user made (every existing
  row), ``spotify_library`` for one read from the user's Spotify library and
  top lists when they connect (``app.spotify.library_baseline``). Both hide the
  item from "New to you" the same way, and Undo removes either.
- ``spotify_connection.library_baseline_at``: when that baseline was read;
  NULL until it is. Read once per connection.
- ``spotify_connection.review_cleared_at``: the newest first play the user has
  reviewed in "Did you already know these?" (``app.read.review``); NULL until
  they finish it once.

Personal data: both tables already cascade from ``app_user``, so
``DELETE /v1/me`` removes the new values, and ``GET /v1/export`` lists them.
``unified_event`` is unchanged.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-28
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE not_new_mark ADD COLUMN source text NOT NULL DEFAULT 'user' "
        "CONSTRAINT not_new_mark_source_chk CHECK (source IN ('user', 'spotify_library'))"
    )
    op.execute("ALTER TABLE spotify_connection ADD COLUMN library_baseline_at timestamptz")
    op.execute("ALTER TABLE spotify_connection ADD COLUMN review_cleared_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE spotify_connection DROP COLUMN review_cleared_at")
    op.execute("ALTER TABLE spotify_connection DROP COLUMN library_baseline_at")
    # A library mark was never the user's choice: it goes with its column.
    op.execute("DELETE FROM not_new_mark WHERE source = 'spotify_library'")
    op.execute("ALTER TABLE not_new_mark DROP COLUMN source")
