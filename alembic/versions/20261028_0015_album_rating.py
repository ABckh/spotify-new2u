"""``album_rating``: the user's own rating of an album, 1 to 10.

One row per user and album, with no period: a rating is the user's opinion
of the album, so it shows in every period and in the all-time list.
``album_id`` is the Spotify album ID, as everywhere else; only an album the
user has a play of can be rated (``app.read.ratings``).

Personal data: ``user_id`` cascades from ``app_user``, so ``DELETE /v1/me``
removes the ratings, and ``GET /v1/export`` lists them. ``unified_event`` is
unchanged.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-28
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE album_rating ("
        "  user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE, "
        "  album_id text NOT NULL CHECK (album_id ~ '^[0-9A-Za-z]{1,64}$'), "
        "  rating smallint NOT NULL CONSTRAINT album_rating_range_chk "
        "    CHECK (rating BETWEEN 1 AND 10), "
        "  rated_at timestamptz NOT NULL DEFAULT now(), "
        "  PRIMARY KEY (user_id, album_id)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP TABLE album_rating")
