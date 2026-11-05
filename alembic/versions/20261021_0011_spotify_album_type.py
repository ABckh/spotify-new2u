"""Allow ``provider_extras.spotify_album_type`` on Spotify rows (WHO-162, WHO-159).

Spotify's own release type, ``album.album_type``, so the album lists can leave
out singles (Spotify counts EPs as singles). ``app.privacy.provider_extras``
holds the label and the PRD P8 reason.

Replaces the 0010 constraint with one that allows this key next to
``spotify_album_artists`` and ``spotify_album_total_tracks``; every other source
still carries nothing. The value is bounded by ``spotify_album_type_ok``: one of
the JSON strings ``"album"``, ``"single"`` or ``"compilation"``
(``SPOTIFY_ALBUM_TYPES``).

A column-level constraint only; no field or its meaning changes. Every row that
satisfies 0010 satisfies this one. Downgrade removes the key from every row
first, because 0010's constraint rejects it; the backfill
(``python -m app.spotify.album_artists_backfill``) can collect it again.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION spotify_album_type_ok(value jsonb) RETURNS boolean "
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path = pg_catalog AS $$ "
        "  SELECT jsonb_typeof(value) = 'string' "
        "    AND (value #>> '{}') IN ('album', 'single', 'compilation') "
        "$$"
    )
    op.execute(
        "ALTER TABLE unified_event DROP CONSTRAINT unified_event_provider_extras_allowlist_chk"
    )
    op.execute(
        "ALTER TABLE unified_event ADD CONSTRAINT unified_event_provider_extras_allowlist_chk "
        "CHECK (CASE source "
        "  WHEN 'spotify' THEN "
        "    (provider_extras - 'spotify_album_artists' - 'spotify_album_total_tracks' "
        "      - 'spotify_album_type') = '{}'::jsonb "
        "    AND (NOT provider_extras ? 'spotify_album_artists' "
        "      OR spotify_album_artists_ok(provider_extras -> 'spotify_album_artists')) "
        "    AND (NOT provider_extras ? 'spotify_album_total_tracks' "
        "      OR spotify_album_total_tracks_ok(provider_extras -> 'spotify_album_total_tracks')) "
        "    AND (NOT provider_extras ? 'spotify_album_type' "
        "      OR spotify_album_type_ok(provider_extras -> 'spotify_album_type')) "
        "  ELSE provider_extras = '{}'::jsonb END)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE unified_event DROP CONSTRAINT unified_event_provider_extras_allowlist_chk"
    )
    op.execute(
        "UPDATE unified_event "
        "SET provider_extras = provider_extras - 'spotify_album_type' "
        "WHERE provider_extras ? 'spotify_album_type'"
    )
    op.execute(
        "ALTER TABLE unified_event ADD CONSTRAINT unified_event_provider_extras_allowlist_chk "
        "CHECK (CASE source "
        "  WHEN 'spotify' THEN "
        "    (provider_extras - 'spotify_album_artists' - 'spotify_album_total_tracks') "
        "      = '{}'::jsonb "
        "    AND (NOT provider_extras ? 'spotify_album_artists' "
        "      OR spotify_album_artists_ok(provider_extras -> 'spotify_album_artists')) "
        "    AND (NOT provider_extras ? 'spotify_album_total_tracks' "
        "      OR spotify_album_total_tracks_ok(provider_extras -> 'spotify_album_total_tracks')) "
        "  ELSE provider_extras = '{}'::jsonb END)"
    )
    op.execute("DROP FUNCTION spotify_album_type_ok(jsonb)")
