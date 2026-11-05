"""Session tokens and refresh tokens we issue to the app.

A session token is 256 random bits, handed to the app at claim and at every
refresh. Only its keyed hash is stored (``auth_session.token_hash``). It is
valid until ``expires_at`` (SESSION_TOKEN_TTL_SECONDS, one hour by default).

With it the app gets a refresh token, also 256 random bits and stored only as a
keyed hash (``refresh_token_hash``), valid until ``refresh_expires_at``
(REFRESH_TOKEN_TTL_SECONDS, 90 days by default). ``POST /auth/refresh`` trades
it for a new session token and a new refresh token, and moves
``refresh_expires_at`` forward again: the app stays signed in for as long as it
is opened at least that often. Every refresh token works once. The hash of the
one just used is kept in ``previous_refresh_hash``; if that token comes back,
two parties hold it (it leaked), so the whole session is revoked and both must
sign in again (OAuth 2.0 Security BCP, refresh token rotation).

Revocation is deleting the row, which ends both tokens: ``POST /auth/logout``
does that for the session token presented, a reused refresh token does it, and
deleting the account removes every session by cascade.

The Spotify tokens never leave the server; nothing here can return one.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, text

from app.auth.pkce import new_secret
from app.core.crypto import keyed_hash

logger = logging.getLogger("app.auth")


@dataclass(frozen=True)
class IssuedSession:
    """What the app gets at claim and at refresh. Never logged."""

    session_token: str
    expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


def _new_tokens(now: datetime, ttl: timedelta, refresh_ttl: timedelta) -> IssuedSession:
    return IssuedSession(
        session_token=new_secret(),
        expires_at=now + ttl,
        refresh_token=new_secret(),
        refresh_expires_at=now + refresh_ttl,
    )


def issue_session(
    conn: Connection, *, login_id: UUID, now: datetime, ttl: timedelta, refresh_ttl: timedelta
) -> IssuedSession | None:
    """Turn the unclaimed session of ``login_id`` into a live one.

    Returns the session and refresh tokens, or None if there is nothing to
    claim. The UPDATE matches only an unclaimed row, so tokens are handed out
    at most once.
    """
    issued = _new_tokens(now, ttl, refresh_ttl)
    row = conn.execute(
        text(
            "UPDATE auth_session SET token_hash = :token_hash, claim_secret_hash = NULL, "
            "  expires_at = :expires_at, refresh_token_hash = :refresh_hash, "
            "  refresh_expires_at = :refresh_expires_at "
            "WHERE login_id = :login_id AND token_hash IS NULL AND expires_at > :now "
            "RETURNING user_id"
        ),
        {
            "token_hash": keyed_hash(issued.session_token, purpose="session"),
            "expires_at": issued.expires_at,
            "refresh_hash": keyed_hash(issued.refresh_token, purpose="refresh"),
            "refresh_expires_at": issued.refresh_expires_at,
            "login_id": login_id,
            "now": now,
        },
    ).one_or_none()
    if row is None:
        return None
    logger.info(
        "session issued user_id=%s expires_at=%s refresh_expires_at=%s",
        row.user_id,
        issued.expires_at.isoformat(),
        issued.refresh_expires_at.isoformat(),
    )
    return issued


def refresh_session(
    conn: Connection, refresh_token: str, *, now: datetime, ttl: timedelta, refresh_ttl: timedelta
) -> IssuedSession | None:
    """Trade a refresh token for a new session token and a new refresh token.

    None means "sign in again": the token is unknown, expired, or was already
    used. A used token revokes its session (the row is deleted), so whoever
    holds the newer refresh token is signed out too. Only a session whose
    account still exists can be refreshed (WHO-25 R4).
    """
    presented = keyed_hash(refresh_token, purpose="refresh")
    row = conn.execute(
        text(
            "SELECT s.id, s.user_id, s.refresh_token_hash = :h AS current, "
            "  s.refresh_expires_at > :now AS live "
            "FROM auth_session s JOIN app_user u ON u.id = s.user_id "
            "WHERE s.refresh_token_hash = :h OR s.previous_refresh_hash = :h "
            "FOR UPDATE OF s"
        ),
        {"h": presented, "now": now},
    ).one_or_none()
    if row is None:
        return None
    if not row.current:
        conn.execute(text("DELETE FROM auth_session WHERE id = :id"), {"id": row.id})
        logger.warning("refresh token reused, session revoked user_id=%s", row.user_id)
        return None
    if not row.live:
        return None
    issued = _new_tokens(now, ttl, refresh_ttl)
    conn.execute(
        text(
            "UPDATE auth_session SET token_hash = :token_hash, expires_at = :expires_at, "
            "  previous_refresh_hash = :presented, refresh_token_hash = :refresh_hash, "
            "  refresh_expires_at = :refresh_expires_at "
            "WHERE id = :id"
        ),
        {
            "token_hash": keyed_hash(issued.session_token, purpose="session"),
            "expires_at": issued.expires_at,
            "presented": presented,
            "refresh_hash": keyed_hash(issued.refresh_token, purpose="refresh"),
            "refresh_expires_at": issued.refresh_expires_at,
            "id": row.id,
        },
    )
    logger.info(
        "session refreshed user_id=%s refresh_expires_at=%s",
        row.user_id,
        issued.refresh_expires_at.isoformat(),
    )
    return issued


def user_for_token(conn: Connection, token: str, now: datetime) -> UUID | None:
    """The user a live session token belongs to, or None.

    Joins ``app_user`` so a session never outlives its account (WHO-25 R4), even
    if a row were ever left behind by something other than the cascade.
    """
    user_id: UUID | None = conn.execute(
        text(
            "SELECT s.user_id FROM auth_session s JOIN app_user u ON u.id = s.user_id "
            "WHERE s.token_hash = :h AND s.expires_at > :now"
        ),
        {"h": keyed_hash(token, purpose="session"), "now": now},
    ).scalar_one_or_none()
    return user_id


def revoke_session(conn: Connection, token: str) -> bool:
    """End the session of ``token``, its refresh token included."""
    user_id = conn.execute(
        text("DELETE FROM auth_session WHERE token_hash = :h RETURNING user_id"),
        {"h": keyed_hash(token, purpose="session")},
    ).scalar_one_or_none()
    if user_id is None:
        return False
    logger.info("session revoked user_id=%s", user_id)
    return True


def purge_expired_sessions(conn: Connection, now: datetime) -> int:
    """Delete sessions that can no longer be used, claimed or not.

    A session whose refresh token still works is kept, even when its session
    token has expired: the app refreshes it the next time it is opened.
    """
    return conn.execute(
        text(
            "DELETE FROM auth_session WHERE expires_at <= :now "
            "  AND (refresh_expires_at IS NULL OR refresh_expires_at <= :now)"
        ),
        {"now": now},
    ).rowcount
