"""Spotify renewals: which signed-in user a sign-in in progress renews.

WHO-61, S9.16. ``POST /auth/spotify/login`` with a valid session token is a
renewal for that user. The callback refuses it when the user picks a different
Spotify account (``different_account``), so two accounts are never merged and
no second account is created.

A table of its own rather than a column on ``oauth_login``, so ``oauth_login``
stays linked to no user (it remains in ``SYSTEM_TABLES``) and this one is
reached by the ``app_user`` delete like every other user table. A row goes
with its ``oauth_login`` row, by cascade: at claim, or when the sign-in expires
(``OAUTH_LOGIN_TTL_SECONDS``). Not part of the frozen contract.

``oauth_login.is_renewal`` records that a sign-in started as a renewal, without
saying for whom. If delete-all removes the ``spotify_renewal`` row while the
user is still at Spotify, the callback sees a renewal with no user and fails
with ``renewal_gone``; it never becomes a fresh sign-in that would recreate
the deleted account (CTO decision on WHO-61).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-14
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE spotify_renewal (
            login_id uuid PRIMARY KEY REFERENCES oauth_login (id) ON DELETE CASCADE,
            user_id  uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE
        )
        """
    )
    # For the cascade from an account delete.
    op.execute("CREATE INDEX spotify_renewal_user_idx ON spotify_renewal (user_id)")
    op.execute("ALTER TABLE oauth_login ADD COLUMN is_renewal boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE oauth_login DROP COLUMN is_renewal")
    op.execute("DROP TABLE spotify_renewal")
