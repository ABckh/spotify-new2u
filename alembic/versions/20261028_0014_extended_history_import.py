"""One-time import of the Spotify "Extended streaming history" export (WHO-94, WHO-104).

``history_import``: one row per upload of the export. ``POST
/v1/spotify/history-import`` creates it ``queued``; the poller's import worker
moves it to ``running`` and then ``succeeded`` or ``failed``. The partial
unique index is the one-time rule: at most one row per user that is queued,
running or succeeded. A failed import does not count, so the user may upload
again.

The counts say what the import did with the rows it read: added, already
stored, skipped by reason, or unreadable. ``ingest_batch_id`` is the import's
own collection record; it is set NULL when retention removes that record.

Nothing from the file itself is kept here: no file name, no path, no content.
The upload lives in a private temp file named after the row id until the job
ends.

Not part of the frozen contract; ``unified_event`` and ``ingest_batch`` are
unchanged (``spotify_extended_history`` and ``file_import`` are data, not
columns). The row goes with ``app_user`` (ON DELETE CASCADE), so delete-all
removes it and a user who deleted everything may import again.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-17
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE history_import (
            id                     uuid PRIMARY KEY,
            user_id                uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
            file_kind              text NOT NULL,
            status                 text NOT NULL DEFAULT 'queued',
            error_kind             text,
            created_at             timestamptz NOT NULL,
            started_at             timestamptz,
            finished_at            timestamptz,
            ingest_batch_id        uuid REFERENCES ingest_batch (id) ON DELETE SET NULL,
            rows_seen              integer NOT NULL DEFAULT 0,
            rows_inserted          integer NOT NULL DEFAULT 0,
            rows_duplicate         integer NOT NULL DEFAULT 0,
            rows_skipped_not_music integer NOT NULL DEFAULT 0,
            rows_skipped_incognito integer NOT NULL DEFAULT 0,
            rows_skipped_short     integer NOT NULL DEFAULT 0,
            rows_unreadable        integer NOT NULL DEFAULT 0,
            CONSTRAINT history_import_status_chk
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
            CONSTRAINT history_import_file_kind_chk
                CHECK (file_kind IN ('zip', 'json')),
            CONSTRAINT history_import_counts_chk
                CHECK (rows_seen >= 0 AND rows_inserted >= 0 AND rows_duplicate >= 0
                       AND rows_skipped_not_music >= 0 AND rows_skipped_incognito >= 0
                       AND rows_skipped_short >= 0 AND rows_unreadable >= 0)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX history_import_one_per_user_uq ON history_import (user_id) "
        "WHERE status IN ('queued', 'running', 'succeeded')"
    )
    op.execute(
        "CREATE INDEX history_import_user_created_idx "
        "ON history_import (user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX history_import_queued_idx ON history_import (created_at) "
        "WHERE status = 'queued'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE history_import")
