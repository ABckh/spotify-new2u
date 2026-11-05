"""``album_comment``: the user's own words about an album, free text.

One row per user and album, with no period, like ``album_rating`` (0015). A
table of its own, not a column there: a rating and a comment are independent,
each can exist without the other, and setting or clearing one never touches
the other. ``album_rating`` is unchanged, so the release before this one
keeps working on the migrated database and simply never reads comments.

``comment`` is 1 to 2000 characters. The API also refuses a blank comment and
NUL (``app.read.ratings``). Only an album the user has a play of can get one.

Personal data: ``user_id`` cascades from ``app_user``, so ``DELETE /v1/me``
removes the comments, and ``GET /v1/export`` lists them. ``unified_event`` is
unchanged.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-31
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE album_comment ("
        "  user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE, "
        "  album_id text NOT NULL CHECK (album_id ~ '^[0-9A-Za-z]{1,64}$'), "
        "  comment text NOT NULL CONSTRAINT album_comment_length_chk "
        "    CHECK (char_length(comment) BETWEEN 1 AND 2000), "
        "  commented_at timestamptz NOT NULL DEFAULT now(), "
        "  PRIMARY KEY (user_id, album_id)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP TABLE album_comment")
