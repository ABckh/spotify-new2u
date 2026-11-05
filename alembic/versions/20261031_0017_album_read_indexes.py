"""Indexes for the album reads, so none of them goes through every play.

After a history import (WHO-104) a user has ~100k plays, nearly all without an
album ID, and every album read (``app.read.album_sql.ALBUM_EVENTS``) resolved
the album of each of them on every request. The reads now look up only the
plays that can have an album:

- ``unified_event_spotify_album_idx``: the plays with their own album, a few
  per day of polling. Partial, so it holds only those.
- ``unified_event_item_idx``: a track's plays, oldest first. The plays that take
  their album from the track, and "New to you" asking whether a song of the
  period was played before it.
- ``unified_event_album_name_idx``: plays by album name after NFC and trimming
  (R-31), the plays that take their album from the name. The expression is
  ``app.read.names.normalized_name`` exactly; a read that writes it otherwise
  does not use the index (``tests/test_album_read_indexes.py``).
- ``unified_event_spotify_album_stx``: statistics on the album ID, without
  which the planner takes nearly every play for one with an album and picks
  plans that read them all.

Indexes and statistics only: no table, column or constraint changes, so the
release before this one runs unchanged on the migrated database.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX unified_event_spotify_album_idx ON unified_event (user_id) "
        "WHERE (external_ids ->> 'spotify_album_id') IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX unified_event_item_idx ON unified_event (user_id, item_key, occurred_at)"
    )
    op.execute(
        "CREATE INDEX unified_event_album_name_idx ON unified_event (user_id, "
        "(regexp_replace(normalize(collection, NFC), '^[[:space:]]+|[[:space:]]+$', '', 'g')))"
    )
    op.execute(
        "CREATE STATISTICS unified_event_spotify_album_stx "
        "ON (external_ids ->> 'spotify_album_id') FROM unified_event"
    )
    # Without this the new statistics stay empty until autovacuum gets to it.
    op.execute("ANALYZE unified_event")


def downgrade() -> None:
    op.execute("DROP STATISTICS unified_event_spotify_album_stx")
    op.execute("DROP INDEX unified_event_album_name_idx")
    op.execute("DROP INDEX unified_event_item_idx")
    op.execute("DROP INDEX unified_event_spotify_album_idx")
