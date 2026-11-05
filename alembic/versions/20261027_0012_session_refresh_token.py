"""Refresh tokens for sessions, rotated on every use.

The app stays signed in without a fresh Spotify sign-in every hour: next to its
short-lived session token, a claimed session now holds a refresh token, which
``POST /auth/refresh`` trades for a new session token and a new refresh token
(``app.auth.sessions``). Like the session token, only its keyed hash is stored.

- ``refresh_token_hash``: the keyed hash of the current refresh token.
- ``previous_refresh_hash``: the hash of the one it replaced. A refresh token
  that comes back after it was used means two parties hold it, so the session
  is revoked (refresh token reuse detection, OAuth 2.0 Security BCP 4.14).
- ``refresh_expires_at``: when the refresh token stops working; every refresh
  moves it to now + REFRESH_TOKEN_TTL_SECONDS.

Only a claimed session has a refresh token, and a hash always comes with its
expiry. Existing sessions get none: they end at their ``expires_at`` as before.
The expiry purge keeps a row while its refresh token is still valid.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth_session
            ADD COLUMN refresh_token_hash    bytea UNIQUE,
            ADD COLUMN previous_refresh_hash bytea UNIQUE,
            ADD COLUMN refresh_expires_at    timestamptz,
            ADD CONSTRAINT auth_session_refresh_expiry_chk
                CHECK ((refresh_token_hash IS NULL) = (refresh_expires_at IS NULL)),
            ADD CONSTRAINT auth_session_refresh_claimed_chk
                CHECK (refresh_token_hash IS NULL OR token_hash IS NOT NULL)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE auth_session
            DROP CONSTRAINT auth_session_refresh_claimed_chk,
            DROP CONSTRAINT auth_session_refresh_expiry_chk,
            DROP COLUMN refresh_expires_at,
            DROP COLUMN previous_refresh_hash,
            DROP COLUMN refresh_token_hash
        """
    )
