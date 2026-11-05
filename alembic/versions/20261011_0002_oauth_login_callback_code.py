"""Bind a sign-in to the device that started it: ``oauth_login.callback_code_hash``.

WHO-36 F1. On a finished callback the server redirects the browser to
APP_CALLBACK_URL with a fresh ``callback_code``. Only its keyed hash is stored
here, and ``/claim`` needs it as well as the claim secret. NULL until the
callback finishes, so rows already pending when this runs stay valid: they
cannot be claimed, and they expire within OAUTH_LOGIN_TTL_SECONDS.

Not part of the frozen contract (``oauth_login`` is auth state, WHO-6).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE oauth_login ADD COLUMN callback_code_hash bytea")


def downgrade() -> None:
    op.execute("ALTER TABLE oauth_login DROP COLUMN callback_code_hash")
