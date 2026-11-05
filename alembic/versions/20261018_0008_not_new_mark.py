"""``not_new_mark``: items the user marked "Not new" in "New to you" (WHO-124).

One row per mark, per user, with no period: a marked item is never new again
(WHO-123 rule 1). ``item_id`` is the Spotify album ID for ``album``, the
artist name after NFC and trimming (R-31) for ``artist``, and the ``item_key``
for ``track``. What an album mark also hides (its songs and its artists) is
computed at read time, never written here (rule 2).

Personal data: ``user_id`` cascades from ``app_user``, so ``DELETE /v1/me``
removes the marks, and ``GET /v1/export`` lists them. Not part of the frozen
contract; ``unified_event`` is unchanged.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-18
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE not_new_mark ("
        "  user_id uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE, "
        "  kind text NOT NULL CHECK (kind IN ('album', 'artist', 'track')), "
        "  item_id text NOT NULL CHECK (length(item_id) > 0), "
        "  created_at timestamptz NOT NULL DEFAULT now(), "
        "  PRIMARY KEY (user_id, kind, item_id)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP TABLE not_new_mark")
