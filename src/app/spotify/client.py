"""The Spotify Web API calls we make, and nothing else.

Official endpoints:

- ``GET  https://accounts.spotify.com/authorize`` (a URL we hand to the app)
- ``POST https://accounts.spotify.com/api/token`` (code exchange, refresh)
- ``GET  https://api.spotify.com/v1/me`` (account id and display name only)
- ``GET  https://api.spotify.com/v1/me/player/recently-played``
- ``GET  https://api.spotify.com/v1/albums/{id}`` (the one-off album artists
  and track count backfill only, WHO-135, WHO-141). One album per call: the several-albums endpoint
  ``GET /v1/albums?ids=`` was removed for development-mode apps in February 2026.
- ``GET  https://api.spotify.com/v1/me/top/{artists|tracks}``,
  ``GET  https://api.spotify.com/v1/me/tracks``, ``GET  https://api.spotify.com/v1/me/albums``
  and ``GET  https://api.spotify.com/v1/me/following?type=artist``: the known-music
  baseline read once per connection (``app.spotify.library_baseline``).

Retries: ``429`` waits for ``Retry-After``; ``5xx`` and transport errors back
off exponentially (1 s, 2 s, 4 s, ... capped). After ``max_attempts`` the call
raises ``SpotifyUnavailable`` and the caller tries again on the next tick.

Errors carry a classified ``kind`` only. A response body, a URL with a query
string, or a token never goes into an exception message or a log line (P12).
"""

import base64
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings

logger = logging.getLogger("app.spotify.client")

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
# The scope sign-in requires; a grant without it is refused.
SCOPE = "user-read-recently-played"
# Asked for too, for the known-music baseline only. A grant without them still
# works; it just gets no baseline (``app.spotify.library_baseline``).
LIBRARY_SCOPES: frozenset[str] = frozenset(
    {"user-top-read", "user-library-read", "user-follow-read"}
)
# What the authorize URL asks for, in a fixed order.
AUTHORIZE_SCOPE = " ".join([SCOPE, *sorted(LIBRARY_SCOPES)])
# Spotify's largest page for every library endpoint.
PAGE_LIMIT = 50
RECENTLY_PLAYED_LIMIT = 50
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SPOTIFY_ID = re.compile("[0-9A-Za-z]{1,64}")


class SpotifyError(Exception):
    """A Spotify call failed. ``kind`` is safe to log and to store in error_kind."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


class InvalidGrant(SpotifyError):
    """The token endpoint answered ``invalid_grant``: the code or refresh token is dead.

    For a refresh token this is terminal (revoked, or past Spotify's 6-month
    expiry): the connection needs the user to reconnect. Never retried.
    """

    def __init__(self) -> None:
        super().__init__("invalid_grant")


class SpotifyUnavailable(SpotifyError):
    """429 / 5xx / network failure that outlasted the retries."""


class SpotifyRejected(SpotifyError):
    """Any other 4xx, or a response we cannot read."""


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    # Spotify may rotate the refresh token on refresh; None means "keep the old one".
    refresh_token: str | None
    scope: frozenset[str]

    def __repr__(self) -> str:
        return f"TokenGrant(scope={sorted(self.scope)})"


@dataclass(frozen=True)
class RecentlyPlayed:
    items: list[Mapping[str, Any]]


class SpotifyClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        max_retry_after: float = 300.0,
    ) -> None:
        if not client_id or not redirect_uri:
            raise RuntimeError("SPOTIFY_CLIENT_ID and SPOTIFY_REDIRECT_URI must be set")
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._max_retry_after = max_retry_after

    # --- auth -------------------------------------------------------------

    def authorize_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                # Verbatim from config; the token request sends the same string.
                "redirect_uri": self._redirect_uri,
                "scope": AUTHORIZE_SCOPE,
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": code_challenge,
                # Always show the consent screen, even to a user who already
                # approved us, so a forwarded sign-in link is never one silent
                # redirect (WHO-36 F1, defence in depth).
                "show_dialog": "true",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def exchange_code(self, *, code: str, code_verifier: str) -> TokenGrant:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "client_id": self._client_id,
                "code_verifier": code_verifier,
            }
        )

    def refresh(self, refresh_token: str) -> TokenGrant:
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
            }
        )

    def _token_request(self, form: dict[str, str]) -> TokenGrant:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self._client_secret:
            pair = f"{self._client_id}:{self._client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
        response = self._send(
            "token", lambda: self._http.post(TOKEN_URL, data=form, headers=headers)
        )
        if response.status_code == 400 and _oauth_error(response) == "invalid_grant":
            raise InvalidGrant()
        body = self._json_or_reject("token", response)
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        scope = body.get("scope", "")
        if not isinstance(access_token, str) or not access_token:
            raise SpotifyRejected("token_response_unreadable")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise SpotifyRejected("token_response_unreadable")
        return TokenGrant(
            access_token=access_token,
            refresh_token=refresh_token or None,
            scope=frozenset(scope.split()) if isinstance(scope, str) else frozenset(),
        )

    # --- API --------------------------------------------------------------

    def me(self, access_token: str) -> Mapping[str, Any]:
        response = self._send(
            "me", lambda: self._http.get(f"{API_BASE}/me", headers=_bearer(access_token))
        )
        return self._json_or_reject("me", response)

    def recently_played(self, access_token: str, *, after: datetime | None) -> RecentlyPlayed:
        """Plays strictly after ``after``, at most 50 (all Spotify keeps).

        ``after=None`` asks for the latest 50, whenever they were played: the
        history import (``api_backfill``) only.
        """
        params = {"limit": RECENTLY_PLAYED_LIMIT}
        if after is not None:
            if after.tzinfo is None:
                raise ValueError("after must be timezone-aware")
            # Integer arithmetic: a float timestamp can round 0.789 s down to 788 ms.
            params["after"] = (after - _EPOCH) // timedelta(milliseconds=1)
        response = self._send(
            "recently_played",
            lambda: self._http.get(
                f"{API_BASE}/me/player/recently-played",
                params=params,
                headers=_bearer(access_token),
            ),
        )
        body = self._json_or_reject("recently_played", response)
        items = body.get("items")
        if not isinstance(items, list):
            raise SpotifyRejected("recently_played_unreadable")
        return RecentlyPlayed(items=items)

    def album(self, access_token: str, album_id: str) -> Mapping[str, Any]:
        """One catalog album. ``album_id`` must be a Spotify ID (base62)."""
        if not _SPOTIFY_ID.fullmatch(album_id):
            raise ValueError("album_id is not a Spotify ID")
        response = self._send(
            "album",
            lambda: self._http.get(f"{API_BASE}/albums/{album_id}", headers=_bearer(access_token)),
        )
        return self._json_or_reject("album", response)

    def top(self, access_token: str, kind: str, time_range: str) -> list[Mapping[str, Any]]:
        """The user's top ``artists`` or ``tracks`` over ``short_term`` (about 4 weeks),
        ``medium_term`` (about 6 months) or ``long_term`` (about a year): one page of 50."""
        if kind not in ("artists", "tracks"):
            raise ValueError("kind is artists or tracks")
        if time_range not in ("short_term", "medium_term", "long_term"):
            raise ValueError("time_range is short_term, medium_term or long_term")
        body = self._get_json(
            f"top_{kind}",
            f"/me/top/{kind}",
            access_token,
            {"limit": PAGE_LIMIT, "time_range": time_range},
        )
        return _items(f"top_{kind}", body)

    def saved_page(
        self, access_token: str, kind: str, offset: int
    ) -> tuple[list[Mapping[str, Any]], bool]:
        """One page of the user's saved ``tracks`` or ``albums``, newest first:
        the items (each with ``added_at``) and whether a next page exists."""
        if kind not in ("tracks", "albums"):
            raise ValueError("kind is tracks or albums")
        body = self._get_json(
            f"saved_{kind}",
            f"/me/{kind}",
            access_token,
            {"limit": PAGE_LIMIT, "offset": offset},
        )
        return _items(f"saved_{kind}", body), body.get("next") is not None

    def followed_artists_page(
        self, access_token: str, after: str | None
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """One page of the artists the user follows, and the cursor of the next
        page (None after the last)."""
        params: dict[str, str | int] = {"type": "artist", "limit": PAGE_LIMIT}
        if after is not None:
            params["after"] = after
        body = self._get_json("followed_artists", "/me/following", access_token, params)
        artists = body.get("artists")
        if not isinstance(artists, dict):
            raise SpotifyRejected("followed_artists_unreadable")
        items = _items("followed_artists", artists)
        cursors = artists.get("cursors")
        cursor = cursors.get("after") if isinstance(cursors, dict) else None
        has_next = artists.get("next") is not None
        return items, cursor if has_next and isinstance(cursor, str) and cursor else None

    def _get_json(
        self, call: str, path: str, access_token: str, params: Mapping[str, str | int]
    ) -> Mapping[str, Any]:
        response = self._send(
            call,
            lambda: self._http.get(
                f"{API_BASE}{path}", params=dict(params), headers=_bearer(access_token)
            ),
        )
        return self._json_or_reject(call, response)

    # --- transport --------------------------------------------------------

    def _send(self, call: str, request: Callable[[], httpx.Response]) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            wait: float | None
            try:
                response = request()
            except httpx.TransportError as exc:
                kind = f"{call}_network_error"
                wait = self._backoff(attempt)
                # The exception class only; its message can contain the URL.
                logger.warning(
                    "spotify call=%s failed error=%s attempt=%d", call, type(exc).__name__, attempt
                )
            except httpx.HTTPError as exc:
                # Not a network fault (a body that does not decode, a redirect
                # loop): retrying gets the same answer. Fail as unavailable so
                # callers handle it like an outage instead of crashing.
                logger.warning("spotify call=%s failed error=%s", call, type(exc).__name__)
                raise SpotifyUnavailable(f"{call}_http_error") from None
            else:
                if response.status_code == 429:
                    kind = f"{call}_rate_limited"
                    wait = self._retry_after(response, attempt)
                elif response.status_code >= 500:
                    kind = f"{call}_server_error"
                    wait = self._backoff(attempt)
                else:
                    return response
                logger.warning(
                    "spotify call=%s status=%d attempt=%d", call, response.status_code, attempt
                )
            if attempt == self._max_attempts or wait is None:
                raise SpotifyUnavailable(kind)
            self._sleep(wait)
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int) -> float:
        return float(min(self._base_delay * 2 ** (attempt - 1), self._max_delay))

    def _retry_after(self, response: httpx.Response, attempt: int) -> float | None:
        """Seconds Spotify asked us to wait; None if longer than we wait in one tick."""
        header = response.headers.get("Retry-After", "")
        try:
            wait = float(int(header))
        except ValueError:
            return self._backoff(attempt)
        if wait > self._max_retry_after:
            return None
        return max(wait, 0.0)

    @staticmethod
    def _json_or_reject(call: str, response: httpx.Response) -> Mapping[str, Any]:
        if response.status_code != 200:
            raise SpotifyRejected(f"{call}_http_{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise SpotifyRejected(f"{call}_unreadable") from None
        if not isinstance(body, dict):
            raise SpotifyRejected(f"{call}_unreadable")
        return body


def build_spotify_client(http: httpx.Client) -> SpotifyClient:
    """The client configured from SPOTIFY_* settings."""
    settings = get_settings()
    return SpotifyClient(
        http,
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
    )


def _bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _items(call: str, body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = body.get("items")
    if not isinstance(items, list):
        raise SpotifyRejected(f"{call}_unreadable")
    return [item for item in items if isinstance(item, dict)]


def _oauth_error(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) else None
