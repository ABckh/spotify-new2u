"""Connect -> collect -> delete -> reconnect -> collect, end to end (WHO-29, WHO-20).

S1.8a / S8.9: after a delete and a reconnect, no poll stores a play from
before the reconnect, even though Spotify still lists it. The timeline and the
export both read ``unified_event`` for the user, so the assertion is made there.

WHO-89, WHO-114: a reconnect after a delete is a first connect, so the history
import runs again (it is always on). It brings back exactly what Spotify still
lists, as ``api_backfill``, and nothing else.

P12: the same run's log, captured at DEBUG, holds no authorization code, no
state, no token or secret, and no title, artist or album.
"""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.auth import utc_now
from app.api.main import app
from app.poller.spotify import poll_connection
from app.privacy.delete import delete_all_user_data
from fake_spotify import RECENT, FakeSpotify

T_CONNECT = datetime(2026, 10, 11, 8, 0, tzinfo=UTC)
T_RECONNECT = T_CONNECT + timedelta(hours=2)

CODES = ("synthetic-auth-code-a1", "synthetic-auth-code-b2")
TITLES = ("Synthetic Title Before", "Synthetic Title Between", "Synthetic Title After")
ARTIST = "Synthetic Artist Name 77"
ALBUM = "Synthetic Album Name 88"


def at(moment: datetime) -> None:
    app.dependency_overrides[utc_now] = lambda: moment


def sign_in(api: TestClient, code: str) -> tuple[str, list[str]]:
    """Returns the session token and every secret the run handled."""
    started = api.post("/auth/spotify/login", json={"home_timezone": "Europe/Helsinki"}).json()
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    callback = api.get("/auth/spotify/callback", params={"code": code, "state": state})
    assert callback.status_code == 303
    callback_code = parse_qs(urlsplit(callback.headers["location"]).query)["callback_code"][0]
    claimed = api.post(
        f"/auth/spotify/login/{started['login_id']}/claim",
        json={"claim_secret": started["claim_secret"], "callback_code": callback_code},
    ).json()
    return claimed["session_token"], [
        code,
        state,
        started["claim_secret"],
        callback_code,
        claimed["session_token"],
    ]


def user_id(engine: Engine) -> UUID:
    with engine.connect() as conn:
        found: UUID = conn.execute(text("SELECT user_id FROM spotify_connection")).scalar_one()
        return found


def test_connect_collect_delete_reconnect_collect(
    api: TestClient,
    engine: Engine,
    fake_spotify: FakeSpotify,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    secrets: list[str] = []
    client = fake_spotify.client()

    # Connect, then collect one play.
    at(T_CONNECT)
    token, handled = sign_in(api, CODES[0])
    secrets += handled
    first_user = user_id(engine)
    fake_spotify.play(
        "track-a", TITLES[0], T_CONNECT + timedelta(minutes=10), artists=[ARTIST], album=ALBUM
    )
    poll_connection(engine, client, first_user, now=T_CONNECT + timedelta(minutes=15))

    # Delete the account. Spotify keeps listing the play.
    with engine.begin() as conn:
        assert delete_all_user_data(conn, first_user) == 1
    fake_spotify.play(
        "track-b", TITLES[1], T_CONNECT + timedelta(hours=1), artists=[ARTIST], album=ALBUM
    )

    # Reconnect. The first poll runs the import: it stores what Spotify lists.
    at(T_RECONNECT)
    token, handled = sign_in(api, CODES[1])
    secrets += handled
    second_user = user_id(engine)
    poll_connection(engine, client, second_user, now=T_RECONNECT + timedelta(minutes=1))

    # Collect. Spotify lists all three plays.
    fake_spotify.play(
        "track-c", TITLES[2], T_RECONNECT + timedelta(minutes=5), artists=[ARTIST], album=ALBUM
    )
    # The cursor alone would hide the older two; make Spotify return them anyway,
    # so the connect-moment filter is what keeps them out.
    fake_spotify.fail_next(RECENT, httpx.Response(200, json={"items": fake_spotify.history}))
    result = poll_connection(engine, client, second_user, now=T_RECONNECT + timedelta(minutes=15))
    api.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})

    assert second_user != first_user
    assert (result.rows_seen, result.rows_inserted) == (3, 1)
    with engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT user_id, title, occurred_at, ingest_method FROM unified_event "
                "ORDER BY occurred_at"
            )
        ).all()
    assert [(r.user_id, r.title, r.ingest_method) for r in stored] == [
        (second_user, TITLES[0], "api_backfill"),
        (second_user, TITLES[1], "api_backfill"),
        (second_user, TITLES[2], "api_poll"),
    ]
    assert all(r.occurred_at >= T_RECONNECT for r in stored if r.ingest_method == "api_poll")

    # The log of the whole run.
    log = caplog.text
    secrets += ["synthetic-access-", "synthetic-refresh-"]
    for forbidden in (*secrets, *TITLES, ARTIST, ALBUM, "code=", "state="):
        assert forbidden not in log, f"log contains {forbidden!r}"
    # ...and it does record the decisions.
    assert "sign-in connected" in log
    assert "account_deleted" in log
    assert "spotify poll succeeded" in log
    assert "session revoked" in log


def _connect_collect_delete(api: TestClient, engine: Engine, fake_spotify: FakeSpotify) -> UUID:
    """First connect, collect a play, delete; Spotify then lists one more play."""
    client = fake_spotify.client()
    at(T_CONNECT)
    sign_in(api, CODES[0])
    first_user = user_id(engine)
    fake_spotify.play("track-a", TITLES[0], T_CONNECT + timedelta(minutes=10))
    poll_connection(engine, client, first_user, now=T_CONNECT + timedelta(minutes=15))
    with engine.begin() as conn:
        assert delete_all_user_data(conn, first_user) == 1
    fake_spotify.play("track-b", TITLES[1], T_CONNECT + timedelta(hours=1))
    return first_user


def _stored_titles(engine: Engine) -> list[tuple[UUID, str, str]]:
    with engine.connect() as conn:
        return [
            (r.user_id, r.title, r.ingest_method)
            for r in conn.execute(
                text("SELECT user_id, title, ingest_method FROM unified_event ORDER BY occurred_at")
            )
        ]


def test_delete_then_reconnect_imports_only_what_spotify_still_lists(
    api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    first_user = _connect_collect_delete(api, engine, fake_spotify)
    # Spotify has since forgotten the first play: only the second is still listed.
    del fake_spotify.history[1:]

    at(T_RECONNECT)
    token, _ = sign_in(api, CODES[1])
    second_user = user_id(engine)
    imported_at = T_RECONNECT + timedelta(seconds=10)
    poll_connection(engine, fake_spotify.client(), second_user, now=imported_at)

    assert second_user != first_user
    assert _stored_titles(engine) == [(second_user, TITLES[1], "api_backfill")]
    status = api.get("/v1/me/connection", headers={"Authorization": f"Bearer {token}"}).json()
    assert status["history_imported_at"] == imported_at.isoformat().replace("+00:00", "Z")
    assert status["collection_started_at"] == T_RECONNECT.isoformat().replace("+00:00", "Z")
