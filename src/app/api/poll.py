"""``POST /v1/me/spotify/poll``: ask for a Spotify poll now (WHO-95).

``202 {"requested_at", "next_allowed_at"}`` once recorded; the poller starts the
poll within ``POLLER_NEW_CONNECTION_CHECK_SECONDS``. ``409`` with no active
connection, nothing written. ``429`` with ``Retry-After`` (whole seconds) while
the cooldown runs (``app.spotify.manual_poll``) or the per-user request limit is
hit. Errors are ``{"detail": {"code", "message"}}``
(``no_active_connection``, ``cooling_down``), except the request-limit ``429``.
``401`` without a valid session, before any query.
"""

import math
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.auth import EngineDep, NowDep, UserDep
from app.api.rate_limit import check_per_user
from app.core.config import get_settings
from app.spotify.manual_poll import Accepted, NotConnected, request_poll

router = APIRouter(prefix="/v1/me/spotify", tags=["collection"])


class PollRequested(BaseModel):
    requested_at: datetime
    next_allowed_at: datetime


@router.post(
    "/poll",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PollRequested,
    responses={409: {"description": "No active Spotify connection"}, 429: {}},
)
def request_spotify_poll(user_id: UserDep, engine: EngineDep, now: NowDep) -> PollRequested:
    settings = get_settings()
    check_per_user("manual_poll", user_id, settings.read_rate_limit_per_minute)
    cooldown = timedelta(seconds=settings.manual_poll_cooldown_seconds)
    with engine.begin() as conn:
        result = request_poll(conn, user_id, now, cooldown)
    if isinstance(result, Accepted):
        return PollRequested(
            requested_at=result.requested_at, next_allowed_at=result.next_allowed_at
        )
    if isinstance(result, NotConnected):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "no_active_connection", "message": "Connect Spotify first."},
        )
    retry_after = max(1, math.ceil((result.next_allowed_at - now).total_seconds()))
    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"code": "cooling_down", "message": "Spotify was checked a moment ago."},
        headers={"Retry-After": str(retry_after)},
    )
