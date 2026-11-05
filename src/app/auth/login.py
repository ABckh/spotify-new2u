"""Sign-in with Spotify: Authorization Code with PKCE, exchanged on the server.

1. **Start.** The app calls ``POST /auth/spotify/login`` with its IANA time zone.
   We create an ``oauth_login`` row holding the keyed hash of a fresh ``state``,
   the PKCE ``code_verifier`` (AES-GCM encrypted), and the keyed hash of a
   ``claim_secret``. The app gets the Spotify authorize URL, the ``login_id``
   and the ``claim_secret``. The verifier never leaves the server.
   With a valid session token the sign-in is a **renewal** for that user
   (S9.16, WHO-61): a ``spotify_renewal`` row ties the login to the user.
2. **Callback.** Spotify redirects the browser to ``SPOTIFY_REDIRECT_URI``
   (``/auth/spotify/callback``) with ``code`` and ``state``. The state is
   accepted exactly once: one UPDATE moves the row from ``pending`` to
   ``callback_used``, and only an unexpired pending row matches. A replayed
   callback, and with it a replayed ``code``, is rejected before Spotify is
   called. We exchange the code with the verifier and the same redirect URI,
   check the granted scope, read the profile, and create or update the user and
   the connection (``app.spotify.connection.connect``). A renewal whose
   Spotify account is not the one the user's connection holds fails with
   ``different_account`` and writes nothing: no user, no token, no change to
   the connection; the new grant is dropped. A renewal whose
   ``spotify_renewal`` row is gone (delete-all ran while the user was at
   Spotify) fails with ``renewal_gone``: it never turns into a fresh sign-in
   that would recreate the deleted account. An unclaimed
   ``auth_session`` is created for the login. Whether it connected or failed,
   the browser is then redirected to ``APP_CALLBACK_URL`` with a fresh
   ``callback_code``; only its keyed hash is stored.
   Before anything is written, the Spotify account must be on
   ``SPOTIFY_ALLOWED_ACCOUNT_IDS`` (WHO-36 F3). One that is not fails with
   ``account_not_allowed``: no user, no connection, no session, and the grant
   is dropped. Renewals are checked too, so taking an account off the list
   stops it from renewing.
3. **Claim.** The app calls ``POST /auth/spotify/login/{login_id}/claim`` with
   its ``claim_secret`` and the ``callback_code`` and gets our session token,
   once. Both are needed. The claim secret proves the app started the sign-in;
   the callback code proves the browser that finished it handed control back to
   that same device (``ASWebAuthenticationSession`` captures the redirect).
   Without the code, someone who starts a sign-in and gets a victim to open the
   authorize URL would be handed a session for the victim's account (WHO-36 F1).
   Anyone who only saw the browser redirect (``code``, ``state``, ``login_id``,
   ``callback_code``) has no claim secret and cannot claim either.

Every decision is logged with the login id, the user id where there is one and
a reason; never a token, a code, a state, a secret or a profile value (P12).
"""

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import Connection, Engine, text

from app.auth.pkce import code_challenge, new_code_verifier, new_secret
from app.auth.sessions import IssuedSession, issue_session
from app.core.crypto import DecryptionError, decrypt_secret, encrypt_secret, keyed_hash
from app.spotify.client import SCOPE, InvalidGrant, SpotifyClient, SpotifyError
from app.spotify.connection import connect
from app.spotify.profile import profile_from_me

logger = logging.getLogger("app.auth")

# The `error` values Spotify documents for a denied authorize request. Any
# other value is logged as provider_error, so a crafted one is never echoed.
_KNOWN_AUTHORIZE_ERRORS = frozenset({"access_denied"})


# Read from the tzdata tree once, not on every sign-in (WHO-36 F2).
_ZONES = frozenset(available_timezones())


class InvalidTimezone(ValueError):
    pass


class TooManyPendingLogins(Exception):
    """MAX_PENDING_LOGINS sign-ins are already in progress; nothing was stored."""


class DifferentAccount(Exception):
    """A renewal signed in to a Spotify account other than the user's connection."""


class RenewalGone(Exception):
    """A renewal whose user was deleted while the sign-in was in progress."""


def validate_home_timezone(name: str) -> str:
    """An IANA zone name such as ``Europe/Helsinki``, or ``UTC`` (schema §1.1).

    Abbreviations (``PST``, ``EST``) and fixed offsets (``-08:00``, ``Etc/GMT+8``)
    are rejected: a fixed offset is wrong for half the year.
    """
    if name != "UTC" and ("/" not in name or name.startswith("Etc/")):
        raise InvalidTimezone("not an IANA region zone")
    if name not in _ZONES:
        raise InvalidTimezone("unknown zone")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise InvalidTimezone("unknown zone") from None
    return name


def _verifier_aad(login_id: UUID) -> str:
    return f"oauth_login.code_verifier:{login_id}"


@dataclass(frozen=True)
class StartedLogin:
    login_id: UUID
    claim_secret: str
    authorize_url: str
    expires_at: datetime


def start_login(
    conn: Connection,
    client: SpotifyClient,
    *,
    home_timezone: str,
    now: datetime,
    ttl: timedelta,
    max_pending: int,
    renewal_user_id: UUID | None = None,
    import_recent_history: bool = False,
) -> StartedLogin:
    """Start a sign-in; with ``renewal_user_id``, a renewal for that signed-in user.

    ``import_recent_history`` is the user's choice on the connect screen. It is
    kept on the sign-in and only acts if the callback turns out to be a first
    connect (``app.spotify.connection.connect``).
    """
    validate_home_timezone(home_timezone)
    purge_expired_logins(conn, now)
    # Every unexpired row counts, finished or not: each one is storage anyone
    # can create without an account. A soft cap: concurrent starts may pass it
    # by a few rows.
    pending = conn.execute(text("SELECT count(*) FROM oauth_login")).scalar_one()
    if pending >= max_pending:
        logger.warning("sign-in refused reason=too_many_pending pending=%s", pending)
        raise TooManyPendingLogins
    state, claim_secret, verifier = new_secret(), new_secret(), new_code_verifier()
    expires_at = now + ttl
    # The id is chosen here so the verifier can be encrypted bound to it.
    login_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO oauth_login (id, state_hash, code_verifier_ciphertext, "
            "  claim_secret_hash, home_timezone, created_at, expires_at, is_renewal, "
            "  import_recent_history) "
            "VALUES (:id, :state_hash, :verifier, :claim_hash, :tz, :now, :expires_at, "
            "  :is_renewal, :import_recent_history)"
        ),
        {
            "id": login_id,
            "state_hash": keyed_hash(state, purpose="state"),
            "verifier": encrypt_secret(verifier, _verifier_aad(login_id)),
            "claim_hash": keyed_hash(claim_secret, purpose="claim"),
            "tz": home_timezone,
            "now": now,
            "expires_at": expires_at,
            "is_renewal": renewal_user_id is not None,
            "import_recent_history": import_recent_history,
        },
    )
    if renewal_user_id is not None:
        conn.execute(
            text("INSERT INTO spotify_renewal (login_id, user_id) VALUES (:login_id, :user_id)"),
            {"login_id": login_id, "user_id": renewal_user_id},
        )
        logger.info("sign-in started login_id=%s renewal user_id=%s", login_id, renewal_user_id)
    else:
        logger.info("sign-in started login_id=%s", login_id)
    return StartedLogin(
        login_id=login_id,
        claim_secret=claim_secret,
        authorize_url=client.authorize_url(state=state, code_challenge=code_challenge(verifier)),
        expires_at=expires_at,
    )


CallbackOutcome = Literal["connected", "failed", "rejected"]


@dataclass(frozen=True)
class CallbackResult:
    outcome: CallbackOutcome
    login_id: UUID | None
    reason: str | None = None
    # Set for "connected" and "failed": the app needs it to claim the outcome.
    callback_code: str | None = None


def handle_callback(
    engine: Engine,
    client: SpotifyClient,
    *,
    code: str | None,
    state: str | None,
    error: str | None,
    now: datetime,
    allowed_account_ids: frozenset[str] | None,
) -> CallbackResult:
    """Finish a sign-in. Each step commits on its own, so a replay sees ``callback_used``.

    ``allowed_account_ids`` is ``Settings.spotify_account_allowlist``: the
    Spotify accounts that may sign in, or ``None`` when any account may.
    """
    if not state:
        logger.info("sign-in rejected reason=missing_state")
        return CallbackResult("rejected", None, "missing_state")

    with engine.begin() as conn:
        login = conn.execute(
            text(
                "UPDATE oauth_login SET outcome = 'callback_used' "
                "WHERE state_hash = :state_hash AND outcome = 'pending' AND expires_at > :now "
                "RETURNING id, code_verifier_ciphertext, home_timezone, is_renewal, "
                "  import_recent_history, "
                "  (SELECT user_id FROM spotify_renewal r WHERE r.login_id = oauth_login.id) "
                "  AS renewal_user_id"
            ),
            {"state_hash": keyed_hash(state, purpose="state"), "now": now},
        ).one_or_none()
    if login is None:
        # Unknown, expired, or already used: the code is not exchanged.
        logger.info("sign-in rejected reason=unknown_expired_or_used_state")
        return CallbackResult("rejected", None, "unknown_expired_or_used_state")
    login_id: UUID = login.id

    if login.is_renewal and login.renewal_user_id is None:
        # Delete-all removed the user while they were at Spotify. Fail before
        # the code is exchanged: no grant is obtained, nothing is written.
        return _fail(engine, login_id, "renewal_gone")

    if error is not None or not code:
        reason = error if error in _KNOWN_AUTHORIZE_ERRORS else "provider_error"
        return _fail(engine, login_id, reason)

    # From here on the state is used up, so every way out must record an outcome:
    # an exception would leave the row at callback_used and the app waiting
    # until the sign-in expires (WHO-36 F5).
    try:
        verifier = decrypt_secret(login.code_verifier_ciphertext, _verifier_aad(login_id))
    except DecryptionError:
        # E.g. TOKEN_ENCRYPTION_KEY changed while the sign-in was open.
        logger.warning("sign-in verifier did not decrypt login_id=%s", login_id)
        return _fail(engine, login_id, "internal_error")
    try:
        grant = client.exchange_code(code=code, code_verifier=verifier)
        if SCOPE not in grant.scope:
            return _fail(engine, login_id, "scope_not_granted")
        if grant.refresh_token is None:
            return _fail(engine, login_id, "no_refresh_token")
        profile = profile_from_me(client.me(grant.access_token))
    except InvalidGrant:
        return _fail(engine, login_id, "code_rejected")
    except SpotifyError as exc:
        return _fail(engine, login_id, exc.kind)
    except ValueError:
        return _fail(engine, login_id, "profile_unreadable")

    if allowed_account_ids is not None and profile.account_id not in allowed_account_ids:
        # Nothing is written and the grant is dropped with this frame. The
        # account ID is not logged: it identifies a person who is not a user.
        return _fail(engine, login_id, "account_not_allowed")

    callback_code = new_secret()
    try:
        with engine.begin() as conn:
            if login.is_renewal:
                _check_renewal(conn, login_id, profile.account_id)
            result = connect(
                conn,
                profile=profile,
                refresh_token=grant.refresh_token,
                home_timezone=login.home_timezone,
                now=now,
                import_recent_history=login.import_recent_history,
            )
            conn.execute(
                text(
                    "INSERT INTO auth_session (user_id, login_id, claim_secret_hash, expires_at) "
                    "SELECT :user_id, id, claim_secret_hash, expires_at FROM oauth_login "
                    "WHERE id = :login_id"
                ),
                {"user_id": result.user_id, "login_id": login_id},
            )
            conn.execute(
                text(
                    "UPDATE oauth_login SET outcome = 'connected', callback_code_hash = :code_hash "
                    "WHERE id = :id"
                ),
                {"code_hash": keyed_hash(callback_code, purpose="callback"), "id": login_id},
            )
    except DifferentAccount:
        # Raised before connect(): nothing was written, and the grant just
        # obtained is dropped with this frame (Spotify has no revoke endpoint).
        logger.warning(
            "sign-in renewal refused login_id=%s user_id=%s reason=different_account",
            login_id,
            login.renewal_user_id,
        )
        return _fail(engine, login_id, "different_account")
    except RenewalGone:
        # Delete-all ran between the state check and here. As above, nothing
        # was written and the grant is dropped.
        return _fail(engine, login_id, "renewal_gone")
    except Exception as exc:
        # The transaction rolled back: no user, connection or session was kept.
        # Only the exception's type is logged; its text could carry row values.
        logger.warning("sign-in connect raised login_id=%s error=%s", login_id, type(exc).__name__)
        return _fail(engine, login_id, "internal_error")
    logger.info(
        "sign-in connected login_id=%s user_id=%s kind=%s", login_id, result.user_id, result.kind
    )
    return CallbackResult("connected", login_id, callback_code=callback_code)


def warn_if_no_account_may_sign_in(allowed_account_ids: frozenset[str] | None) -> None:
    """Log once at startup when SPOTIFY_ALLOWED_ACCOUNT_IDS lets no account in."""
    if allowed_account_ids is not None and not allowed_account_ids:
        logger.warning(
            "SPOTIFY_ALLOWED_ACCOUNT_IDS is empty: every sign-in is refused "
            "with account_not_allowed"
        )


def _check_renewal(conn: Connection, login_id: UUID, account_id: str) -> None:
    """Raise unless the renewal's user still exists and ``account_id`` is theirs.

    The renewal row is read again under a lock, so a delete-all that ran since
    the state check is seen here (RenewalGone), and one that starts now waits
    for this transaction.

    The ``app_user`` row is locked first, in the same statement. Delete-all
    locks that row before its cascade reaches ``spotify_connection`` and then
    ``spotify_renewal``; locking the renewal row alone and then the connection
    row would take them in the opposite order and could deadlock (WHO-65 F1).
    """
    user_id = conn.execute(
        text(
            "SELECT r.user_id FROM spotify_renewal r JOIN app_user u ON u.id = r.user_id "
            "WHERE r.login_id = :login_id FOR KEY SHARE OF u FOR SHARE OF r"
        ),
        {"login_id": login_id},
    ).scalar_one_or_none()
    if user_id is None:
        raise RenewalGone
    _check_renews_own_account(conn, user_id, account_id)


def _check_renews_own_account(conn: Connection, user_id: UUID, account_id: str) -> None:
    """Raise DifferentAccount unless ``account_id`` is the user's connected account.

    The connection row is locked, so it cannot change before ``connect`` runs
    in the same transaction. A user with no connection has nothing to renew,
    so that is refused too.
    """
    connected = conn.execute(
        text(
            "SELECT spotify_account_id FROM spotify_connection WHERE user_id = :user_id FOR UPDATE"
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()
    if connected is None or connected != account_id:
        raise DifferentAccount


def _fail(engine: Engine, login_id: UUID, reason: str) -> CallbackResult:
    callback_code = new_secret()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE oauth_login SET outcome = 'failed', failure_kind = :reason, "
                "  callback_code_hash = :code_hash "
                "WHERE id = :id"
            ),
            {
                "reason": reason,
                "code_hash": keyed_hash(callback_code, purpose="callback"),
                "id": login_id,
            },
        )
    logger.info("sign-in failed login_id=%s reason=%s", login_id, reason)
    return CallbackResult("failed", login_id, reason, callback_code=callback_code)


ClaimStatus = Literal["unknown", "pending", "failed", "issued"]


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    session: IssuedSession | None = None
    reason: str | None = None


def claim_session(
    conn: Connection,
    *,
    login_id: UUID,
    claim_secret: str,
    callback_code: str | None,
    now: datetime,
    ttl: timedelta,
    refresh_ttl: timedelta,
) -> ClaimResult:
    """Hand the app its session token and refresh token, once.

    A wrong claim secret, a wrong or missing callback code once the callback has
    finished, and an unknown id all look the same, and none of them consumes the
    sign-in. While the callback has not finished the claim secret alone gets
    "pending", which tells its holder nothing about any account.
    """
    login = conn.execute(
        text(
            "SELECT outcome, failure_kind, callback_code_hash FROM oauth_login "
            "WHERE id = :id AND claim_secret_hash = :claim_hash AND expires_at > :now FOR UPDATE"
        ),
        {"id": login_id, "claim_hash": keyed_hash(claim_secret, purpose="claim"), "now": now},
    ).one_or_none()
    if login is None:
        logger.info("session claim rejected login_id=%s reason=unknown_or_expired", login_id)
        return ClaimResult("unknown")
    if login.outcome in ("pending", "callback_used"):
        return ClaimResult("pending")
    if (
        callback_code is None
        or login.callback_code_hash is None
        or not hmac.compare_digest(
            bytes(login.callback_code_hash), keyed_hash(callback_code, purpose="callback")
        )
    ):
        logger.info("session claim rejected login_id=%s reason=callback_code_mismatch", login_id)
        return ClaimResult("unknown")
    conn.execute(text("DELETE FROM oauth_login WHERE id = :id"), {"id": login_id})
    if login.outcome == "failed":
        return ClaimResult("failed", reason=login.failure_kind)
    issued = issue_session(conn, login_id=login_id, now=now, ttl=ttl, refresh_ttl=refresh_ttl)
    if issued is None:
        logger.info("session claim rejected login_id=%s reason=nothing_to_claim", login_id)
        return ClaimResult("unknown")
    return ClaimResult("issued", session=issued)


def purge_expired_logins(conn: Connection, now: datetime) -> int:
    return conn.execute(
        text("DELETE FROM oauth_login WHERE expires_at <= :now"), {"now": now}
    ).rowcount
