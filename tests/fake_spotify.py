"""A scripted stand-in for the Spotify Web API, behind ``httpx.MockTransport``.

Every value is synthetic. Each endpoint answers from a queue of scripted
responses (``fail_next``) and otherwise from the fake's current state, and
every request is recorded so tests can assert what was sent.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

from app.spotify.client import SpotifyClient

TOKEN = "/api/token"
ME = "/v1/me"
RECENT = "/v1/me/player/recently-played"
ALBUMS = "/v1/albums/"
TOP = "/v1/me/top/"
SAVED_TRACKS = "/v1/me/tracks"
SAVED_ALBUMS = "/v1/me/albums"
FOLLOWING = "/v1/me/following"
LIBRARY_SCOPE = "user-read-recently-played user-top-read user-library-read user-follow-read"

CLIENT_ID = "test-client-id"
REDIRECT_URI = "http://127.0.0.1:8000/auth/spotify/callback"


# Spotify lists an album's images widest first. The 300 one is the pick (WHO-149).
SYNTHETIC_IMAGES = [
    {"url": "https://i.scdn.co/image/synthetic-640", "width": 640, "height": 640},
    {"url": "https://i.scdn.co/image/synthetic-300", "width": 300, "height": 300},
    {"url": "https://i.scdn.co/image/synthetic-64", "width": 64, "height": 64},
]


@dataclass
class FakeSpotify:
    account_id: str = "synthetic-account-1"
    display_name: str | None = "Synthetic Listener"
    # What Spotify still lists, newest first, as recently-played items.
    history: list[dict[str, Any]] = field(default_factory=list)
    granted_scope: str = "user-read-recently-played"
    code_counter: int = 0
    refresh_counter: int = 0
    rotate_refresh_tokens: bool = False
    dead_refresh_tokens: set[str] = field(default_factory=set)
    used_codes: set[str] = field(default_factory=set)
    requests: list[httpx.Request] = field(default_factory=list)
    scripted: dict[str, deque[httpx.Response | httpx.HTTPError]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    sleeps: list[float] = field(default_factory=list)
    # The catalog for GET /v1/albums/{id}: album id -> its artists' names,
    # and album id -> its track count (10 when not listed; None leaves it out).
    albums: dict[str, list[str]] = field(default_factory=dict)
    album_total_tracks: dict[str, int | None] = field(default_factory=dict)
    # And its album_type (WHO-162): "album" when not listed.
    album_types: dict[str, str] = field(default_factory=dict)
    # Its images (WHO-149); an album not listed here has none.
    album_images: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # The known-music baseline's library. Top lists per time_range; saved
    # tracks and albums as (added_at, object), newest first; followed artists.
    top_artists: dict[str, list[str]] = field(default_factory=dict)
    top_tracks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    saved_tracks: list[tuple[datetime, dict[str, Any]]] = field(default_factory=list)
    saved_albums: list[tuple[datetime, dict[str, Any]]] = field(default_factory=list)
    followed: list[str] = field(default_factory=list)

    # --- scripting ------------------------------------------------------

    def fail_next(self, path: str, response: httpx.Response | httpx.HTTPError) -> None:
        """Answer the next call to ``path`` with ``response``, or raise it if an error."""
        self.scripted[path].append(response)

    def play(self, track_id: str, title: str, played_at: datetime, **track: Any) -> None:
        """Add a play to the top of the listed history (synthetic data only)."""
        stamp = played_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.history.insert(
            0,
            {
                "played_at": stamp,
                "track": {
                    "id": track_id,
                    "name": title,
                    "duration_ms": track.get("duration_ms", 200_000),
                    "artists": [{"name": a} for a in track.get("artists", ["Synthetic Artist"])],
                    "album": {
                        "id": track.get("album_id", "synthetic-album"),
                        "name": track.get("album", "Synthetic Album"),
                        "artists": [
                            {"name": a}
                            for a in track.get("album_artists", ["Synthetic Album Artist"])
                        ],
                        "total_tracks": track.get("total_tracks", 10),
                        "album_type": track.get("album_type", "album"),
                        "images": track.get("images", SYNTHETIC_IMAGES),
                    },
                    "external_ids": {"isrc": "XX0000000001"},
                    "popularity": 50,
                    "explicit": False,
                },
                "context": {"type": "playlist", "uri": "spotify:playlist:synthetic"},
            },
        )
        del self.history[50:]

    def client(self, **kwargs: Any) -> SpotifyClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handle))
        return SpotifyClient(
            http,
            client_id=CLIENT_ID,
            client_secret=kwargs.pop("client_secret", ""),
            redirect_uri=REDIRECT_URI,
            sleep=self.sleeps.append,
            **kwargs,
        )

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    # --- the endpoints --------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.scripted[path]:
            scripted = self.scripted[path].popleft()
            if isinstance(scripted, httpx.HTTPError):
                raise scripted
            return scripted
        if path == TOKEN:
            return self._token(parse_qs(request.content.decode()))
        bearer = request.headers.get("Authorization", "")
        if not bearer.startswith("Bearer synthetic-access-"):
            return httpx.Response(401, json={"error": {"status": 401}})
        if path == ME:
            return httpx.Response(
                200,
                json={
                    "id": self.account_id,
                    "display_name": self.display_name,
                    "email": "synthetic@example.invalid",
                    "country": "FI",
                    "images": [],
                },
            )
        if path == RECENT:
            # No `after`: the latest 50, as Spotify answers the history import.
            after = int(request.url.params.get("after", -1))
            items = [i for i in self.history if _ms(i["played_at"]) > after][:50]
            return httpx.Response(200, json={"items": items, "limit": 50})
        if path.startswith(TOP):
            time_range = request.url.params["time_range"]
            if path.removeprefix(TOP) == "artists":
                found: list[dict[str, Any]] = [
                    {"name": n, "id": "x"} for n in self.top_artists.get(time_range, [])
                ]
            else:
                found = self.top_tracks.get(time_range, [])
            return httpx.Response(200, json={"items": found[:50]})
        if path in (SAVED_TRACKS, SAVED_ALBUMS):
            saved = self.saved_tracks if path == SAVED_TRACKS else self.saved_albums
            key = "track" if path == SAVED_TRACKS else "album"
            offset = int(request.url.params["offset"])
            page = saved[offset : offset + 50]
            return httpx.Response(
                200,
                json={
                    "items": [{"added_at": _stamp(at), key: obj} for at, obj in page],
                    "next": "more" if offset + 50 < len(saved) else None,
                },
            )
        if path == FOLLOWING:
            start = int(request.url.params.get("after", "0"))
            page_names = self.followed[start : start + 50]
            more = start + 50 < len(self.followed)
            return httpx.Response(
                200,
                json={
                    "artists": {
                        "items": [{"name": n, "id": "x"} for n in page_names],
                        "next": "more" if more else None,
                        "cursors": {"after": str(start + 50) if more else None},
                    }
                },
            )
        if path.startswith(ALBUMS) and path.removeprefix(ALBUMS) in self.albums:
            album_id = path.removeprefix(ALBUMS)
            return httpx.Response(
                200,
                json={
                    "id": album_id,
                    "name": "Synthetic Album",
                    "artists": [{"name": a, "id": "x"} for a in self.albums[album_id]],
                    "total_tracks": self.album_total_tracks.get(album_id, 10),
                    "album_type": self.album_types.get(album_id, "album"),
                    "images": self.album_images.get(album_id, []),
                    "tracks": {"items": []},
                },
            )
        return httpx.Response(404)

    def _token(self, form: dict[str, list[str]]) -> httpx.Response:
        grant_type = form["grant_type"][0]
        if grant_type == "authorization_code":
            code = form["code"][0]
            if code in self.used_codes or form.get("redirect_uri") != [REDIRECT_URI]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.used_codes.add(code)
            self.code_counter += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"synthetic-access-{self.code_counter}",
                    "refresh_token": f"synthetic-refresh-{self.account_id}-{self.code_counter}",
                    "scope": self.granted_scope,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        refresh_token = form["refresh_token"][0]
        if refresh_token in self.dead_refresh_tokens:
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"}
            )
        self.refresh_counter += 1
        body: dict[str, Any] = {
            "access_token": f"synthetic-access-r{self.refresh_counter}",
            "scope": self.granted_scope,
            "expires_in": 3600,
        }
        if self.rotate_refresh_tokens:
            body["refresh_token"] = f"synthetic-rotated-{self.refresh_counter}"
        return httpx.Response(200, json=body)


def library_track(track_id: str, *artists: str) -> dict[str, Any]:
    """A track object as the library endpoints list it (synthetic)."""
    return {
        "id": track_id,
        "name": f"Track {track_id}",
        "artists": [{"name": a, "id": "x"} for a in artists or ("Synthetic Artist",)],
    }


def library_album(album_id: str, *artists: str) -> dict[str, Any]:
    """An album object as ``/me/albums`` lists it (synthetic)."""
    return {
        "id": album_id,
        "name": f"Album {album_id}",
        "artists": [{"name": a, "id": "x"} for a in artists or ("Synthetic Artist",)],
    }


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(played_at: str) -> int:
    dt = datetime.fromisoformat(played_at)
    return (dt - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
