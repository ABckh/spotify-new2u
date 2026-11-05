"""``track_comment``: the user's own words about a track, free text.

The same as ``album_comment`` (0016), per track: one row per user and track
(``item_key``, the track's identity everywhere else), with no period. A table
of its own, so the release before this one keeps working on the migrated
database and simply never reads track comments.

``comment`` is 1 to 2000 characters. The API also refuses a blank comment and
NUL (``app.read.ratings``). Only a track the user has a play of can get one.

Personal data: ``user_id`` cascades from ``app_user``, so ``DELETE /v1/me``
removes the comments, and ``GET /v1/export`` lists them. ``unified_event`` is
unchanged.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-31
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE track_comment ("
        "  user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE, "
        "  item_key text NOT NULL "
        "    CHECK (item_key ~ '^(id:[0-9A-Za-z]{1,64}|nt:[0-9a-f]{32})$'), "
        "  comment text NOT NULL CONSTRAINT track_comment_length_chk "
        "    CHECK (char_length(comment) BETWEEN 1 AND 2000), "
        "  commented_at timestamptz NOT NULL DEFAULT now(), "
        "  PRIMARY KEY (user_id, item_key)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP TABLE track_comment")
