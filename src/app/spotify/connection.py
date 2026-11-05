"""``spotify_connection``: one per user. The refresh token is stored here, encrypted.

The refresh token is encrypted with AES-256-GCM bound to the owning user
(``app.core.crypto``), never logged, and never returned by any API. When
Spotify answers ``invalid_grant`` the connection moves to ``reauth_required``
and the dead token is deleted, so nothing keeps a credential that cannot work.
The same transaction opens a collection gap, and the reconnect closes it
(``app.events.gaps``, D-7).

Spotify refresh tokens expire 6 months after the user authorized, and refreshing
does not extend that (Spotify, 18 Jun 2026; WHO-20). ``authorized_at`` is the
start of that clock; ``grant_expires_at`` estimates the end conservatively.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.core.crypto import decrypt_secret, encrypt_secret
from app.events.gaps import close_gap, gap_start, open_gap
from app.spotify.plays import SOURCE
from app.spotify.profile import SpotifyProfile, profile_columns

logger = logging.getLogger("app.spotify.connection")

# Spotify says 6 months. 180 days is the shortest reading of that, so warnings
# and the reported end date err early, never late.
GRANT_LIFETIME = timedelta(days=180)

Status = Literal["active", "reauth_required"]
ConnectKind = Literal["first_connect", "refresh_grant", "reconnect_after_reauth"]


def _aad(user_id: UUID) -> str:
    return f"spotify_connection.refresh_token:{user_id}"


def encrypt_refresh_token(user_id: UUID, refresh_token: str) -> bytes:
    return encrypt_secret(refresh_token, _aad(user_id))


def decrypt_refresh_token(user_id: UUID, stored: bytes) -> str:
    return decrypt_secret(stored, _aad(user_id))


def grant_expires_at(authorized_at: datetime) -> datetime:
    return authorized_at + GRANT_LIFETIME


def renew_by(authorized_at: datetime, display_zone: str) -> date:
    """The last day to renew: the day before the grant ends, in the display zone (S9.12).

    Signing in again starts a fresh 6 months at Spotify (WHO-60), so renewing
    on this day or earlier keeps collection unbroken.
    """
    return grant_expires_at(authorized_at).astimezone(ZoneInfo(display_zone)).date() - timedelta(
        days=1
    )


def collection_start_on_reconnect_after_reauth(
    previous_collection_started_at: datetime, now: datetime
) -> datetime:
    """Where collection restarts when a revoked or expired grant is reconnected.

    Open question O7 on WHO-20: should the reconnect recover gap plays that
    Spotify still lists? Until it is answered this returns ``now``, the same as
    every other connect (WHO-29), so no play made while we held no valid grant
    is stored. Answering O7 "yes" means returning ``previous_collection_started_at``
    here, and ending the collection gap at the oldest recovered play instead of
    at the new collection start (``connect``).
    """
    return now


@dataclass(frozen=True)
class ConnectResult:
    user_id: UUID
    kind: ConnectKind


def connect(
    conn: Connection,
    *,
    profile: SpotifyProfile,
    refresh_token: str,
    home_timezone: str,
    now: datetime,
    import_recent_history: bool = False,
) -> ConnectResult:
    """Create or update the connection for this Spotify account. Caller owns the transaction.

    - No connection for the account: a new ``app_user`` with ``home_timezone``
      (set once, here) and a new connection. Collection starts now. With
      ``import_recent_history`` the poller also imports the plays Spotify
      already lists, once (``history_import_requested``, WHO-89).
    - An active connection: the user signed in again. Tokens and ``authorized_at``
      are replaced; ``collection_started_at`` is kept, because moving it forward
      would drop plays made since the last poll.
    - A connection that needs reauth: active again; see
      ``collection_start_on_reconnect_after_reauth``. Its open collection gap
      ends at the new collection start: G2 becomes G1 (S9.6).

    Only the first connect reads ``import_recent_history``. A refresh grant leaves
    ``history_import_requested`` as it is. A reconnect after reauth clears it
    unless the import already succeeded: a pending import lapses, because the
    plays Spotify lists by then include plays from the gap, made while we held
    no valid grant (WHO-93 F1). Neither ever starts an import.
    """
    profile_values = profile_columns(profile)
    existing = conn.execute(
        text(
            "SELECT user_id, status, collection_started_at FROM spotify_connection "
            "WHERE spotify_account_id = :account_id FOR UPDATE"
        ),
        {"account_id": profile.account_id},
    ).one_or_none()

    if existing is None:
        user_id: UUID = conn.execute(
            text("INSERT INTO app_user (home_timezone) VALUES (:tz) RETURNING id"),
            {"tz": home_timezone},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO spotify_connection (user_id, spotify_account_id, "
                "  spotify_display_name, collection_started_at, authorized_at, "
                "  refresh_token_ciphertext, status, history_import_requested) "
                "VALUES (:user_id, :spotify_account_id, :spotify_display_name, :now, :now, "
                "  :token, 'active', :import_requested)"
            ),
            {
                **profile_values,
                "user_id": user_id,
                "now": now,
                "token": encrypt_refresh_token(user_id, refresh_token),
                "import_requested": import_recent_history,
            },
        )
        return ConnectResult(user_id, "first_connect")

    user_id = existing.user_id
    if existing.status == "active":
        kind: ConnectKind = "refresh_grant"
        collection_started_at = existing.collection_started_at
    else:
        kind = "reconnect_after_reauth"
        collection_started_at = collection_start_on_reconnect_after_reauth(
            existing.collection_started_at, now
        )
    conn.execute(
        text(
            "UPDATE spotify_connection SET spotify_display_name = :spotify_display_name, "
            "  collection_started_at = :collection_started_at, authorized_at = :now, "
            "  refresh_token_ciphertext = :token, status = 'active', reauth_reason = NULL, "
            "  history_import_requested = CASE WHEN :lapse_pending_import "
            "    THEN history_import_requested AND history_imported_at IS NOT NULL "
            "    ELSE history_import_requested END "
            "WHERE user_id = :user_id"
        ),
        {
            "lapse_pending_import": kind == "reconnect_after_reauth",
            "spotify_display_name": profile_values["spotify_display_name"],
            "collection_started_at": collection_started_at,
            "now": now,
            "token": encrypt_refresh_token(user_id, refresh_token),
            "user_id": user_id,
        },
    )
    if kind == "reconnect_after_reauth":
        close_gap(conn, user_id, SOURCE, collection_started_at)
    return ConnectResult(user_id, kind)


def store_rotated_refresh_token(
    conn: Connection, user_id: UUID, refresh_token: str, *, read_ciphertext: bytes
) -> bool:
    """Replace the token the poller used with the one Spotify rotated it to.

    Compare-and-swap on ``read_ciphertext``, the value the poller decrypted: if
    the user signed in again in the meantime, the stored token is from a newer
    grant and is kept (WHO-36 F4). Returns whether the row was updated.
    """
    updated = conn.execute(
        text(
            "UPDATE spotify_connection SET refresh_token_ciphertext = :token "
            "WHERE user_id = :user_id AND status = 'active' "
            "  AND refresh_token_ciphertext = :read_ciphertext"
        ),
        {
            "token": encrypt_refresh_token(user_id, refresh_token),
            "user_id": user_id,
            "read_ciphertext": read_ciphertext,
        },
    ).rowcount
    if updated == 0:
        logger.info("spotify token rotation_skipped_newer_grant user_id=%s", user_id)
    return bool(updated)


def mark_reauth_required(
    conn: Connection, user_id: UUID, reason: str, *, read_ciphertext: bytes
) -> bool:
    """Terminal until the user reconnects. The dead token is deleted, not kept.

    Compare-and-swap like :func:`store_rotated_refresh_token`: a token that
    died after the user signed in again says nothing about the newer grant.
    Returns whether the connection was marked.

    Opens the collection gap (G2) in the same transaction, from the last
    successful poll (``app.events.gaps.gap_start``).
    """
    marked = conn.execute(
        text(
            "UPDATE spotify_connection SET status = 'reauth_required', reauth_reason = :reason, "
            "  refresh_token_ciphertext = NULL "
            "WHERE user_id = :user_id AND refresh_token_ciphertext = :read_ciphertext "
            "RETURNING last_polled_at, collection_started_at"
        ),
        {"reason": reason, "user_id": user_id, "read_ciphertext": read_ciphertext},
    ).one_or_none()
    if marked is None:
        logger.info("spotify reauth_skipped_newer_grant user_id=%s reason=%s", user_id, reason)
        return False
    open_gap(conn, user_id, SOURCE, gap_start(marked.last_polled_at, marked.collection_started_at))
    logger.warning("spotify connection needs reconnect user_id=%s reason=%s", user_id, reason)
    return True
