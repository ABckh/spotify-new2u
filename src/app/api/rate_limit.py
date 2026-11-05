"""Request limits: per client IP on the sign-in routes (WHO-36 F2), per user on reads.

A fixed one-minute window per (route, client IP), counted in memory. That is
enough for this deployment: one API process (docker-compose.yml). With more
than one process each would count on its own, and the limit would multiply.

The client IP is ``request.client.host``. Behind a reverse proxy that is the
proxy's address unless uvicorn trusts its ``X-Forwarded-For``: uvicorn runs with
``--proxy-headers``, and FORWARDED_ALLOW_IPS must name the proxy, and only the
proxy (see docs/configuration.md). Otherwise every client shares one bucket, or a client
picks its own IP.

An IPv6 client is counted by its /64 (WHO-40 N3): one subscriber usually holds
a whole /64, so per-address buckets would give it 2^64 of them. The table has a
hard cap. When it is full of windows still running, a new client gets 429
rather than a new entry: memory stays bounded, at the price of new clients
waiting up to a minute while someone floods the routes.
"""

import ipaddress
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID

from fastapi import HTTPException, Request, status

_WINDOW_SECONDS = 60.0
# Entries across all routes. A few hundred bytes each, so about a megabyte.
_MAX_KEYS = 10_000


class RateLimiter:
    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_keys: int = _MAX_KEYS
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()  # sync routes run in a thread pool
        self._windows: dict[str, tuple[float, int]] = {}

    def check(self, key: str, limit: int) -> float | None:
        """Count one request for ``key``. None if allowed, else seconds until it would be."""
        now = self._clock()
        with self._lock:
            if key not in self._windows and len(self._windows) >= self._max_keys:
                self._forget_finished_windows(now)
                if len(self._windows) >= self._max_keys:
                    oldest = min(started for started, _ in self._windows.values())
                    return _WINDOW_SECONDS - (now - oldest)
            started, count = self._windows.get(key, (now, 0))
            if now - started >= _WINDOW_SECONDS:
                started, count = now, 0
            if count >= limit:
                return _WINDOW_SECONDS - (now - started)
            self._windows[key] = (started, count + 1)
            return None

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()

    def _forget_finished_windows(self, now: float) -> None:
        self._windows = {
            key: window
            for key, window in self._windows.items()
            if now - window[0] < _WINDOW_SECONDS
        }


limiter = RateLimiter()


class Busy(Exception):
    """The key is already held."""


class OneAtATime:
    """Keys of which at most one may be held at a time, e.g. one export per user."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: set[str] = set()

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        """Hold ``key`` for the block, or raise ``Busy`` if it is already held."""
        with self._lock:
            if key in self._held:
                raise Busy(key)
            self._held.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._held.discard(key)

    def clear(self) -> None:
        with self._lock:
            self._held.clear()


# Exports streaming now, by user id (WHO-48 F1). Per process, like ``limiter``.
export_streams = OneAtATime()


def client_key(host: str) -> str:
    """The bucket for a client address: the address itself, or its /64 for IPv6."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host  # "unknown", or a test client's name
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def per_client_ip(route: str, limit: Callable[[], int]) -> Callable[[Request], None]:
    """A route dependency that answers 429 once a client IP is over ``limit()`` a minute."""

    def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        _check(f"{route}:{client_key(client_ip)}", limit())

    return dependency


def check_per_user(route: str, user_id: UUID, limit: int) -> None:
    """Answer 429 once ``user_id`` is over ``limit`` requests a minute on ``route``.

    For the authenticated reads (WHO-7): the session has been checked first, so
    the bucket is the account, not an address someone else may share.
    """
    _check(f"{route}:user:{user_id}", limit)


def _check(key: str, limit: int) -> None:
    retry_after = limiter.check(key, limit)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(math.ceil(retry_after))},
        )
