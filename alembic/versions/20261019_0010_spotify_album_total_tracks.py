"""Allow ``provider_extras.spotify_album_total_tracks`` on Spotify rows (WHO-141, WHO-130).

The album's track count, so an album counts as listened only when more than half
of its tracks were played. ``app.privacy.provider_extras`` holds the label and
the PRD P8 reason.

Replaces the 0009 constraint with one that allows this key next to
``spotify_album_artists``; every other source still carries nothing. The value is
bounded by ``spotify_album_total_tracks_ok``: a JSON integer from 1 to 100000
(``SPOTIFY_ALBUM_TOTAL_TRACKS_MAX``), so the read can cast it to ``int``.

A column-level constraint only; no field or its meaning changes. Every row that
satisfies 0009 satisfies this one. Downgrade removes the key from every row
first, because 0009's constraint rejects it; the backfill
(``python -m app.spotify.album_artists_backfill``) can collect it again.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION spotify_album_total_tracks_ok(value jsonb) RETURNS boolean "
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path = pg_catalog AS $$ "
        "  SELECT jsonb_typeof(value) = 'number' "
        "    AND (value #>> '{}')::numeric = trunc((value #>> '{}')::numeric) "
        "    AND (value #>> '{}')::numeric BETWEEN 1 AND 100000 "
        "$$"
    )
    op.execute(
        "ALTER TABLE unified_event DROP CONSTRAINT unified_event_provider_extras_allowlist_chk"
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


def downgrade() -> None:
    op.execute(
        "ALTER TABLE unified_event DROP CONSTRAINT unified_event_provider_extras_allowlist_chk"
    )
    op.execute(
        "UPDATE unified_event "
        "SET provider_extras = provider_extras - 'spotify_album_total_tracks' "
        "WHERE provider_extras ? 'spotify_album_total_tracks'"
    )
    op.execute(
        "ALTER TABLE unified_event ADD CONSTRAINT unified_event_provider_extras_allowlist_chk "
        "CHECK (CASE source "
        "  WHEN 'spotify' THEN (provider_extras - 'spotify_album_artists') = '{}'::jsonb "
        "    AND (NOT provider_extras ? 'spotify_album_artists' "
        "      OR spotify_album_artists_ok(provider_extras -> 'spotify_album_artists')) "
        "  ELSE provider_extras = '{}'::jsonb END)"
    )
    op.execute("DROP FUNCTION spotify_album_total_tracks_ok(jsonb)")
