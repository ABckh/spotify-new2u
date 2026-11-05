"""Import the recent plays Spotify already lists, if the user chose it (WHO-89, WHO-91).

``oauth_login.import_recent_history`` carries the user's choice from
``POST /auth/spotify/login`` to the callback. ``connect`` copies it to
``spotify_connection.history_import_requested`` on a first connect only; a
renewal or a reconnect after reauth never sets it.

``spotify_connection.history_imported_at`` is set by the poller in the same
transaction as the ``api_backfill`` batch that imported the plays. While it is
NULL and an import was requested, every poll tries the import again.

Existing connections get ``false`` and NULL: they never import. Not part of
the frozen contract; ``unified_event`` and ``ingest_batch`` are unchanged (the
new ``ingest_method`` value ``api_backfill`` is data, not a column).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE oauth_login ADD COLUMN import_recent_history boolean NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE spotify_connection "
        "  ADD COLUMN history_import_requested boolean NOT NULL DEFAULT false, "
        "  ADD COLUMN history_imported_at timestamptz"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE spotify_connection "
        "  DROP COLUMN history_imported_at, DROP COLUMN history_import_requested"
    )
    op.execute("ALTER TABLE oauth_login DROP COLUMN import_recent_history")
