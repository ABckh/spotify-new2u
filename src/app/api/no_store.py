"""No cache may keep an answer from a private route (WHO-25 R5, WHO-84).

Every ``/auth/...`` answer can hold a claim secret or a session token
(RFC 6749 section 5.1), and every ``/v1/...`` answer holds the caller's
listening history or account. Each gets ``Cache-Control: no-store`` and
``Pragma: no-cache``, errors included.

A middleware rather than per-route headers or a router dependency, so 401,
422, 429 and every other error answer gets the headers too, and a new route
under these prefixes gets them without anyone remembering.
"""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response

PRIVATE_PREFIXES = ("/auth/", "/v1/")


async def no_store_on_private_routes(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    if request.url.path.startswith(PRIVATE_PREFIXES):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response
