"""The API's access log: one line per request, never the query string.

Replaces uvicorn's access log, which ``app.core.log`` switches off because it
prints the full request line. The sign-in callback carries the OAuth ``code``
and ``state`` in its query string (PRD P12), and a future search or filter
parameter could carry a title, so the query string is dropped for every path.

A line holds the method, the route *template* (``/auth/spotify/login/{login_id}/claim``,
never the filled-in path, and ``-`` when no route matched, since an unmatched path
is whatever the client typed), the status, the user id once a session has been
checked (``-`` before that), and the client address. The timestamp is the log
record's own (``LOG_FORMAT``). No response body is ever read.

The client address is logged; fixed string W6 says so.
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

logger = logging.getLogger("app.access")

CallNext = Callable[[Request], Awaitable[Response]]


async def log_request(request: Request, call_next: CallNext) -> Response:
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        client = request.client.host if request.client else "-"
        # The matched route's template; set by FastAPI in the shared scope.
        route = getattr(request.scope.get("route"), "path", None) or "-"
        user_id = getattr(request.state, "user_id", None) or "-"
        logger.info(
            "request method=%s route=%s status=%s user_id=%s client=%s duration_ms=%d",
            request.method,
            route,
            status,
            user_id,
            client,
            (time.monotonic() - started) * 1000,
        )
