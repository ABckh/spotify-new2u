"""Allow ``provider_extras.spotify_album_artists`` on Spotify rows (WHO-135, WHO-128).

The album's own artists, so "New to you" no longer names a compilation after the
artists of its last played track. ``app.privacy.provider_extras`` holds the label
and the PRD P8 reason.

Replaces the 0004 constraint ``provider_extras = '{}'`` with the per-source form
0004 describes: Spotify rows may carry only this key, every other source still
carries nothing. The value is bounded by ``spotify_album_artists_ok``: a JSON
array of 1 to 20 strings, each 1 to 500 characters (the same bounds as
``SPOTIFY_ALBUM_ARTISTS_MAX_ITEMS`` and ``SPOTIFY_ALBUM_ARTIST_MAX_CHARS``). A
function because a CHECK cannot hold the subquery that looks at each element.

A column-level constraint only; no field or its meaning changes. Existing rows
all hold ``{}`` (0004), so they satisfy the new constraint. Downgrade removes the
key from every row first, because 0004's constraint rejects it; the backfill
(``python -m app.spotify.album_artists_backfill``) can collect it again.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-18
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION spotify_album_artists_ok(value jsonb) RETURNS boolean "
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path = pg_catalog AS $$ "
        "  SELECT jsonb_typeof(value) = 'array' "
        "    AND jsonb_array_length(value) BETWEEN 1 AND 20 "
        "    AND NOT EXISTS ("
        "      SELECT 1 FROM jsonb_array_elements(value) AS e(name) "
        "      WHERE jsonb_typeof(e.name) <> 'string' "
        "         OR length(e.name #>> '{}') NOT BETWEEN 1 AND 500) "
        "$$"
    )
    op.execute(
        "ALTER TABLE unified_event DROP CONSTRAINT unified_event_provider_extras_allowlist_chk"
    )
    op.execute(
        "ALTER TABLE unified_event ADD CONSTRAINT unified_event_provider_extras_allowlist_chk "
        "CHECK (CASE source "
        "  WHEN 'spotify' THEN (provider_extras - 'spotify_album_artists') = '{}'::jsonb "
        "    AND (NOT provider_extras ? 'spotify_album_artists' "
        "      OR spotify_album_artists_ok(provider_extras -> 'spotify_album_artists')) "
        "  ELSE provider_extras = '{}'::jsonb END)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE unified_event DROP CONSTRAINT unified_event_provider_extras_allowlist_chk"
    )
    op.execute(
        "UPDATE unified_event SET provider_extras = provider_extras - 'spotify_album_artists' "
        "WHERE provider_extras ? 'spotify_album_artists'"
    )
    op.execute(
        "ALTER TABLE unified_event ADD CONSTRAINT unified_event_provider_extras_allowlist_chk "
        "CHECK (provider_extras = '{}'::jsonb)"
    )
    op.execute("DROP FUNCTION spotify_album_artists_ok(jsonb)")
