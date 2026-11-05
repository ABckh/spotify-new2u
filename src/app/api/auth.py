"""Sign-in, session and connection-status routes. The flow is in ``app.auth.login``.

Contract for the app (documented on WHO-6 for iOS):

- ``POST /auth/spotify/login`` ``{"home_timezone": "Europe/Helsinki"}`` ->
  ``201 {"login_id", "claim_secret", "authorize_url", "expires_at"}``.
  When the sign-in creates the account (a first connect, including the first
  one after a delete), the poller also imports the latest 50 plays Spotify
  already lists (WHO-89). This is always on (WHO-114); a renewal or a
  reconnect never imports. ``import_recent_history`` is still accepted, as a
  JSON bool, from app builds that had the switch, and is ignored.
  Open ``authorize_url`` in ``ASWebAuthenticationSession`` with the scheme of
  ``APP_CALLBACK_URL`` as its callback scheme.
  To renew Spotify access for the signed-in user, send the session token too:
  ``Authorization: Bearer <session_token>``. An invalid or expired token gets
  ``401``, never a fresh sign-in. If the user then picks a different Spotify
  account, the claim answers ``400 {"detail": {"reason": "different_account"}}``
  and nothing changes: keep the current session (S9.16, WHO-61). If the
  account was deleted while the renewal was at Spotify, the claim answers
  ``400 {"detail": {"reason": "renewal_gone"}}`` and no account is created.
  A Spotify account that is not on the server's allowlist
  (``SPOTIFY_ALLOWED_ACCOUNT_IDS``) gets ``400 {"detail": {"reason":
  "account_not_allowed"}}`` on the claim, for a first sign-in and for a renewal
  alike; nothing is stored. Show "This Spotify account can't use this server"
  rather than a retry: signing in again with the same account fails the same way.
- ``GET /auth/spotify/callback`` is Spotify's redirect target, not called by the app.
  When the sign-in finishes it ``303``-redirects to ``APP_CALLBACK_URL`` with
  ``login_id``, ``result`` (``connected`` | ``failed``) and ``callback_code``.
  A rejected callback (unknown, expired or replayed state) gets a ``400`` page.
  Locally, with no ``APP_CALLBACK_URL``, it shows a page with the callback code.
- ``POST /auth/spotify/login/{login_id}/claim`` ``{"claim_secret", "callback_code"}``
  -> ``200 {"session_token", "expires_at", "refresh_token", "refresh_expires_at"}``
  once; ``202`` while the user has not
  finished at Spotify (the claim secret alone is enough to see that);
  ``400 {"detail": {"reason"}}`` if the sign-in failed; ``404`` if unknown,
  expired, already claimed, or the secret or the callback code is wrong or
  missing. A wrong callback code does not use up the sign-in.
- Both sign-in routes answer ``429`` with ``Retry-After`` above a per-IP rate
  (10/min for login, 60/min for claim), and login answers ``503`` with
  ``Retry-After`` while too many sign-ins are in progress server-wide.
- Every ``/auth/...`` response, errors included, carries ``Cache-Control: no-store``
  and ``Pragma: no-cache`` (RFC 6749 section 5.1: it can hold a secret or token;
  ``app.api.no_store``).
- ``POST /auth/refresh`` ``{"refresh_token"}`` -> ``200`` with the same four
  fields as the claim: a new session token and a new refresh token. Keep both in
  the Keychain and drop the old ones: every refresh token works once. Refresh
  when an authenticated call answers ``401``, then retry that call once; send
  one refresh at a time, because a refresh token used twice ends the session
  (reuse detection). ``401`` means sign in again: the refresh token is unknown,
  expired (REFRESH_TOKEN_TTL_SECONDS without a refresh, 90 days by default) or
  was already used. Rate-limited per IP like the claim (``429``).
- ``POST /auth/logout`` with ``Authorization: Bearer <session_token>`` -> ``204``;
  the refresh token ends with it.
- ``GET /v1/me/connection`` (Bearer) -> the connection status below. For the
  timeline marker (WHO-89): when ``has_imported_plays`` is ``true``, draw
  "imported history ends / live collection begins" at ``collection_started_at``.
  It is ``true`` only if the import stored at least one play from before
  ``collection_started_at``; an import that found nothing earlier sets
  ``history_imported_at`` but no marker (WHO-93 F2). Plays from the extended
  history file import (WHO-104) count too.
  A reconnect after reauth moves ``collection_started_at`` to the reconnect
  (WHO-20 O7), so after one the marker sits at that reconnect instead.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, StrictBool
from sqlalchemy import Engine, text

from app.api.rate_limit import per_client_ip
from app.auth.login import (
    InvalidTimezone,
    TooManyPendingLogins,
    claim_session,
    handle_callback,
    start_login,
)
from app.auth.sessions import IssuedSession, refresh_session, revoke_session, user_for_token
from app.core.config import get_settings
from app.core.db import get_engine
from app.spotify.client import SpotifyClient, build_spotify_client
from app.spotify.connection import grant_expires_at, renew_by
from app.spotify.manual_poll import poll_state

router = APIRouter(tags=["auth"])


def get_db_engine() -> Engine:
    return get_engine()


def get_spotify_client() -> Iterator[SpotifyClient]:
    with httpx.Client(timeout=15.0) as http:
        yield build_spotify_client(http)


def utc_now() -> datetime:
    return datetime.now(UTC)


EngineDep = Annotated[Engine, Depends(get_db_engine)]
ClientDep = Annotated[SpotifyClient, Depends(get_spotify_client)]
NowDep = Annotated[datetime, Depends(utc_now)]


# Declares the session token in the OpenAPI schema, so Swagger UI (/docs) has
# an Authorize button and sends the header (WHO-58). It never decides anything:
# auto_error=False, and the header is checked by _parse_bearer below, which
# refuses what HTTPBearer would quietly drop (a "Basic" header, say).
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="The session token from POST /auth/spotify/login/{login_id}/claim.",
)
BearerSchemeDep = Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)]


def _parse_bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ")


def _bearer_token(
    _scheme: BearerSchemeDep, authorization: Annotated[str | None, Header()] = None
) -> str:
    return _parse_bearer(authorization)


TokenDep = Annotated[str, Depends(_bearer_token)]


def current_user(request: Request, token: TokenDep, engine: EngineDep, now: NowDep) -> UUID:
    with engine.connect() as conn:
        user_id = user_for_token(conn, token, now)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    request.state.user_id = user_id  # for the access log line (PRD P12)
    return user_id


UserDep = Annotated[UUID, Depends(current_user)]


# --- sign-in -------------------------------------------------------------


class LoginRequest(BaseModel):
    home_timezone: str
    # The old connect-screen switch (WHO-89). Ignored: every first connect
    # imports (WHO-114). Still a strict bool so older builds validate as before.
    import_recent_history: StrictBool | None = None


class LoginResponse(BaseModel):
    login_id: UUID
    claim_secret: str
    authorize_url: str
    expires_at: datetime


@router.post(
    "/auth/spotify/login",
    status_code=201,
    response_model=LoginResponse,
    dependencies=[
        Depends(per_client_ip("login", lambda: get_settings().login_rate_limit_per_minute))
    ],
)
def login(
    body: LoginRequest,
    engine: EngineDep,
    client: ClientDep,
    now: NowDep,
    request: Request,
    _scheme: BearerSchemeDep,
    authorization: Annotated[str | None, Header()] = None,
) -> LoginResponse:
    settings = get_settings()
    # With a session token this is a renewal for its user. A token that does
    # not check out is refused, not ignored: the app must never end up signed
    # in to a second account when it asked to renew the first.
    renewal_user_id = None
    if authorization is not None:
        renewal_user_id = current_user(request, _parse_bearer(authorization), engine, now)
    try:
        with engine.begin() as conn:
            started = start_login(
                conn,
                client,
                home_timezone=body.home_timezone,
                now=now,
                ttl=timedelta(seconds=settings.oauth_login_ttl_seconds),
                max_pending=settings.max_pending_logins,
                renewal_user_id=renewal_user_id,
                import_recent_history=True,
            )
    except InvalidTimezone:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="home_timezone must be an IANA zone name"
        ) from None
    except TooManyPendingLogins:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Retry-After": str(settings.oauth_login_ttl_seconds)},
        ) from None
    return LoginResponse(
        login_id=started.login_id,
        claim_secret=started.claim_secret,
        authorize_url=started.authorize_url,
        expires_at=started.expires_at,
    )


_PAGE = "<!doctype html><title>new2u</title><p>{message}</p>"


@router.get("/auth/spotify/callback", response_model=None)
def spotify_callback(
    engine: EngineDep,
    client: ClientDep,
    now: NowDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> Response:
    result = handle_callback(
        engine,
        client,
        code=code,
        state=state,
        error=error,
        now=now,
        allowed_account_ids=get_settings().spotify_account_allowlist,
    )
    if result.outcome == "rejected" or result.login_id is None:
        return HTMLResponse(
            _PAGE.format(message="This sign-in link has expired or was already used."),
            status_code=400,
        )
    app_url = get_settings().app_callback_url
    if app_url:
        query = urlencode(
            {
                "login_id": str(result.login_id),
                "result": result.outcome,
                "callback_code": result.callback_code,
            }
        )
        return RedirectResponse(f"{app_url}?{query}", status_code=303)
    # Only reachable with APP_ENV=local (config refuses to start otherwise):
    # show the code so a developer can claim by hand.
    if result.outcome == "connected":
        return HTMLResponse(
            _PAGE.format(message=f"Spotify is connected. Callback code: {result.callback_code}")
        )
    return HTMLResponse(
        _PAGE.format(message=f"Spotify was not connected. Callback code: {result.callback_code}"),
        status_code=400,
    )


class ClaimRequest(BaseModel):
    claim_secret: str
    # From the APP_CALLBACK_URL redirect. Needed for 200 and 400; without it a
    # finished sign-in answers 404.
    callback_code: str | None = None


class ClaimResponse(BaseModel):
    session_token: str
    expires_at: datetime
    # Trade at POST /auth/refresh for a new session token when this one expires.
    refresh_token: str
    refresh_expires_at: datetime

    @classmethod
    def of(cls, issued: IssuedSession) -> "ClaimResponse":
        return cls(
            session_token=issued.session_token,
            expires_at=issued.expires_at,
            refresh_token=issued.refresh_token,
            refresh_expires_at=issued.refresh_expires_at,
        )


class PendingResponse(BaseModel):
    status: Literal["pending"] = "pending"


@router.post(
    "/auth/spotify/login/{login_id}/claim",
    response_model=ClaimResponse | PendingResponse,
    responses={
        202: {"model": PendingResponse},
        400: {
            "description": "The sign-in failed. `detail.reason` says why, for example "
            "`access_denied`, `different_account`, `renewal_gone` or `account_not_allowed` "
            "(the Spotify account is not on SPOTIFY_ALLOWED_ACCOUNT_IDS)."
        },
    },
    dependencies=[
        Depends(per_client_ip("claim", lambda: get_settings().claim_rate_limit_per_minute))
    ],
)
def claim(
    login_id: UUID, body: ClaimRequest, engine: EngineDep, now: NowDep, response: Response
) -> ClaimResponse | PendingResponse:
    with engine.begin() as conn:
        result = claim_session(
            conn,
            login_id=login_id,
            claim_secret=body.claim_secret,
            callback_code=body.callback_code,
            now=now,
            ttl=timedelta(seconds=get_settings().session_token_ttl_seconds),
            refresh_ttl=timedelta(seconds=get_settings().refresh_token_ttl_seconds),
        )
    if result.status == "pending":
        response.status_code = status.HTTP_202_ACCEPTED
        return PendingResponse()
    if result.status == "failed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={"reason": result.reason})
    if result.status == "unknown" or result.session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return ClaimResponse.of(result.session)


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post(
    "/auth/refresh",
    response_model=ClaimResponse,
    responses={401: {"description": "Sign in again: unknown, expired or reused refresh token."}},
    dependencies=[
        Depends(per_client_ip("refresh", lambda: get_settings().claim_rate_limit_per_minute))
    ],
)
def refresh(body: RefreshRequest, engine: EngineDep, now: NowDep) -> ClaimResponse:
    settings = get_settings()
    with engine.begin() as conn:
        issued = refresh_session(
            conn,
            body.refresh_token,
            now=now,
            ttl=timedelta(seconds=settings.session_token_ttl_seconds),
            refresh_ttl=timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    if issued is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    return ClaimResponse.of(issued)


@router.post("/auth/logout", status_code=204)
def logout(token: TokenDep, engine: EngineDep) -> Response:
    with engine.begin() as conn:
        revoke_session(conn, token)
    return Response(status_code=204)


# --- connection status (liveness for the owner) --------------------------


class ConnectionStatus(BaseModel):
    status: Literal["active", "reauth_required"]
    reauth_reason: str | None
    spotify_account_id: str
    spotify_display_name: str | None
    # Where live collection began; the history-import marker on the timeline.
    collection_started_at: datetime
    # When the recent-history import stored its plays; None if the user did not
    # choose it at their first connect, or it has not succeeded yet.
    history_imported_at: datetime | None
    # Whether an import (recent plays, or the extended history file, WHO-104)
    # stored a play from before collection_started_at: the condition for the
    # timeline marker. False if it found none (WHO-93 F2).
    has_imported_plays: bool
    authorized_at: datetime
    # 180 days after authorized_at: Spotify's 6-month limit, read conservatively.
    grant_expires_at: datetime
    # The day before grant_expires_at, in the display zone: the last day to renew.
    renew_by: date
    reconnect_soon: bool
    # The last successful poll.
    last_polled_at: datetime | None
    # The newest poll attempt, scheduled or manual, failed or not (WHO-95).
    last_poll_attempt_at: datetime | None
    # When POST /v1/me/spotify/poll is next accepted; null or past means now.
    next_manual_poll_at: datetime | None
    poll_interval_seconds: int


@router.get("/v1/me/connection", response_model=ConnectionStatus)
def connection_status(user_id: UserDep, engine: EngineDep, now: NowDep) -> ConnectionStatus:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT c.status, c.reauth_reason, c.spotify_account_id, "
                "  c.spotify_display_name, c.collection_started_at, c.authorized_at, "
                "  c.last_polled_at, c.history_imported_at, u.home_timezone, "
                "  EXISTS (SELECT 1 FROM unified_event e WHERE e.user_id = :user_id "
                "    AND e.source = 'spotify' "
                "    AND e.ingest_method IN ('api_backfill', 'file_import') "
                "    AND e.occurred_at < c.collection_started_at) AS has_imported_plays "
                "FROM spotify_connection c JOIN app_user u ON u.id = c.user_id "
                "WHERE c.user_id = :user_id"
            ),
            {"user_id": user_id},
        ).one_or_none()
        state = poll_state(conn, user_id)
    if row is None or state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    settings = get_settings()
    expires_at = grant_expires_at(row.authorized_at)
    return ConnectionStatus(
        status=row.status,
        reauth_reason=row.reauth_reason,
        spotify_account_id=row.spotify_account_id,
        spotify_display_name=row.spotify_display_name,
        collection_started_at=row.collection_started_at,
        history_imported_at=row.history_imported_at,
        has_imported_plays=row.has_imported_plays,
        authorized_at=row.authorized_at,
        grant_expires_at=expires_at,
        renew_by=renew_by(row.authorized_at, row.home_timezone),
        reconnect_soon=row.status != "active"
        or now >= expires_at - timedelta(days=settings.reauth_warning_days),
        last_polled_at=row.last_polled_at,
        last_poll_attempt_at=state.last_poll_attempt_at,
        next_manual_poll_at=state.next_manual_poll_at(
            timedelta(seconds=settings.manual_poll_cooldown_seconds)
        ),
        poll_interval_seconds=settings.poller_interval_seconds,
    )
