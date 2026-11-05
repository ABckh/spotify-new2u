"""Enforce the ``provider_extras`` allowlist in the database.

WHO-24 R3. ``check_provider_extras`` runs only in writers that remember to call
it; this constraint covers every writer. It mirrors
``app.privacy.provider_extras.PROVIDER_EXTRAS_ALLOWLIST``, which today permits no
key for any source, so the only accepted value is ``{}``.
``tests/test_privacy_provider_extras.py`` checks the two against each other.

When the first key is allowlisted, a new revision replaces this constraint with
one ``CASE source WHEN '<source>' THEN (provider_extras - ARRAY[<keys>]) = '{}'``
branch per source, ``ELSE provider_extras = '{}'``, and also bounds the value of
each new key (scalar type, maximum length): an allowed key name does not limit
what is stored under it.

A column-level constraint only; no field or its meaning changes. On a database
that already holds a row with a key, this upgrade fails rather than keep it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE unified_event ADD CONSTRAINT unified_event_provider_extras_allowlist_chk "
        "CHECK (provider_extras = '{}'::jsonb)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE unified_event DROP CONSTRAINT unified_event_provider_extras_allowlist_chk"
    )
