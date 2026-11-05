"""Manual poll requests (WHO-95): the route, its cooldown, and the poller pick-up.

Against the real database: the cooldown and the check-and-set live there, so a
test with the database mocked out would prove nothing.
"""

import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.auth import utc_now
from app.api.main import app
from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.poller import main as poller_main
from app.poller import spotify as poller_spotify
from app.poller.spotify import poll_connection
from app.spotify.connection import connect
from app.spotify.manual_poll import Accepted, CoolingDown, request_poll
from app.spotify.profile import SpotifyProfile
from fake_spotify import FakeSpotify
from read_helpers import add_session, make_user

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # the connect moment
COOLDOWN = timedelta(seconds=300)  # the default MANUAL_POLL_COOLDOWN_SECONDS
POLL = "/v1/me/spotify/poll"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now


@pytest.fixture
def clock(api: TestClient) -> Clock:
    """The API's clock, settable by the test."""
    clock = Clock(T0)
    app.dependency_overrides[utc_now] = lambda: clock.now
    return clock


def signed_in_user(engine: Engine, account_id: str = "synthetic-account-1") -> dict[str, str]:
    """A connected account (at T0) with a live session; its auth header."""
    token = f"synthetic-session-{account_id}"
    with engine.begin() as conn:
        user = connect(
            conn,
            profile=SpotifyProfile(account_id, "Synthetic Listener"),
            refresh_token="synthetic-refresh-0",
            home_timezone="Europe/Helsinki",
            now=T0,
        ).user_id
        add_session(conn, user, token)
    return {"Authorization": f"Bearer {token}", "user": str(user)}


def user_of(auth: dict[str, str]) -> UUID:
    return UUID(auth["user"])


def batches(engine: Engine, user: UUID) -> int:
    with engine.connect() as conn:
        n: int = conn.execute(
            text("SELECT count(*) FROM ingest_batch WHERE user_id = :u"), {"u": user}
        ).scalar_one()
        return n


def requested_at(engine: Engine, user: UUID) -> datetime | None:
    with engine.connect() as conn:
        value: datetime | None = conn.execute(
            text("SELECT poll_requested_at FROM spotify_connection WHERE user_id = :u"),
            {"u": user},
        ).scalar_one()
        return value


def run_poller_check(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, fake: FakeSpotify, at: datetime
) -> None:
    """One between-ticks check of the poller, its clock at ``at``."""

    class _At(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "_At":
            return cls.fromtimestamp(at.timestamp(), UTC)

    monkeypatch.setattr(poller_main, "get_engine", lambda: engine)
    monkeypatch.setattr(poller_main, "datetime", _At)
    poller_main.poll_new_connections(fake.client())


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def post(api: TestClient, auth: dict[str, str]) -> Any:
    return api.post(POLL, headers={"Authorization": auth["Authorization"]})


# --- poller pick-up -------------------------------------------------------------


def test_a_request_is_polled_once_by_the_next_check(
    engine: Engine,
    fake_spotify: FakeSpotify,
    clock: Clock,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth, other_auth = signed_in_user(engine), signed_in_user(engine, "synthetic-account-2")
    user, other = user_of(auth), user_of(other_auth)
    for u in (user, other):
        poll_connection(engine, fake_spotify.client(), u, now=T0 + timedelta(minutes=1))
    clock.now = T0 + timedelta(minutes=10)

    response = post(api, auth)

    assert response.status_code == 202
    body = response.json()
    assert parse(body["requested_at"]) == clock.now
    assert parse(body["next_allowed_at"]) == clock.now + COOLDOWN
    assert poller_spotify.connections_with_poll_request(engine) == [user]

    before = batches(engine, user), batches(engine, other)
    run_poller_check(monkeypatch, engine, fake_spotify, clock.now + timedelta(seconds=15))
    assert (batches(engine, user), batches(engine, other)) == (before[0] + 1, before[1])

    # The request is used up: a second check does not poll again.
    run_poller_check(monkeypatch, engine, fake_spotify, clock.now + timedelta(seconds=30))
    assert batches(engine, user) == before[0] + 1
    assert poller_spotify.connections_with_poll_request(engine) == []


def test_a_failed_attempt_uses_up_the_request(
    engine: Engine,
    fake_spotify: FakeSpotify,
    clock: Clock,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = signed_in_user(engine)
    user = user_of(auth)
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=1))
    clock.now = T0 + timedelta(minutes=10)
    assert post(api, auth).status_code == 202
    with engine.begin() as conn:  # a ciphertext that does not authenticate
        conn.execute(
            text("UPDATE spotify_connection SET refresh_token_ciphertext = :ct WHERE user_id = :u"),
            {"ct": encrypt_secret("x", "wrong-aad"), "u": user},
        )

    run_poller_check(monkeypatch, engine, fake_spotify, clock.now + timedelta(seconds=15))
    run_poller_check(monkeypatch, engine, fake_spotify, clock.now + timedelta(seconds=30))

    with engine.connect() as conn:
        kinds = conn.execute(
            text("SELECT error_kind FROM ingest_batch WHERE user_id = :u ORDER BY started_at"),
            {"u": user},
        ).scalars()
        assert list(kinds) == [None, "token_decrypt_failed"]


def test_a_new_connection_that_also_asked_is_polled_once(
    engine: Engine,
    fake_spotify: FakeSpotify,
    clock: Clock,
    api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = signed_in_user(engine)
    clock.now = T0 + timedelta(seconds=5)
    assert post(api, auth).status_code == 202

    run_poller_check(monkeypatch, engine, fake_spotify, T0 + timedelta(seconds=15))

    assert batches(engine, user_of(auth)) == 1


# --- cooldown -------------------------------------------------------------------


def test_a_second_request_inside_the_cooldown_is_refused_until_it_ends(
    engine: Engine, clock: Clock, api: TestClient
) -> None:
    auth = signed_in_user(engine)
    clock.now = T0 + timedelta(minutes=10)
    assert post(api, auth).status_code == 202
    first = clock.now

    clock.now = first + timedelta(seconds=100, milliseconds=500)
    refused = post(api, auth)
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "200"  # 199.5 s, rounded up
    assert refused.json()["detail"]["code"] == "cooling_down"
    assert requested_at(engine, user_of(auth)) == first  # a refusal writes nothing

    clock.now = first + COOLDOWN - timedelta(milliseconds=1)
    assert post(api, auth).headers["Retry-After"] == "1"

    clock.now = first + COOLDOWN
    assert post(api, auth).status_code == 202
    assert requested_at(engine, user_of(auth)) == first + COOLDOWN


def test_a_scheduled_poll_starts_the_cooldown(
    engine: Engine, fake_spotify: FakeSpotify, clock: Clock, api: TestClient
) -> None:
    auth = signed_in_user(engine)
    tick = T0 + timedelta(minutes=15)
    poll_connection(engine, fake_spotify.client(), user_of(auth), now=tick)

    clock.now = tick + timedelta(seconds=60)
    refused = post(api, auth)
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "240"

    clock.now = tick + COOLDOWN
    assert post(api, auth).status_code == 202


def test_the_cooldown_compares_instants_not_local_times(
    engine: Engine, fake_spotify: FakeSpotify, clock: Clock, api: TestClient
) -> None:
    """A tick written with a non-UTC offset is the same instant; a timezone bug shows here."""
    auth = signed_in_user(engine)
    tick = (T0 + timedelta(minutes=15)).astimezone(timezone(timedelta(hours=3)))
    poll_connection(engine, fake_spotify.client(), user_of(auth), now=tick)

    clock.now = T0 + timedelta(minutes=16)  # UTC, 60 s after the tick
    assert post(api, auth).headers["Retry-After"] == "240"


def test_two_requests_at_the_same_time_record_one(engine: Engine) -> None:
    """The second request waits on the first one's row lock, then matches nothing."""
    auth = signed_in_user(engine)
    user = user_of(auth)
    now = T0 + timedelta(minutes=10)
    results: list[Any] = []

    with engine.connect() as first:
        first.begin()
        assert isinstance(request_poll(first, user, now, COOLDOWN), Accepted)

        def second() -> None:
            with engine.begin() as conn:
                results.append(request_poll(conn, user, now, COOLDOWN))

        thread = threading.Thread(target=second)
        thread.start()
        _wait_for_a_lock_wait(engine)
        assert results == []  # still blocked on the uncommitted first request
        first.commit()
        thread.join(timeout=10)

    assert results == [CoolingDown(now + COOLDOWN)]


def test_two_requests_at_the_same_time_through_the_api(
    engine: Engine, clock: Clock, api: TestClient
) -> None:
    auth = signed_in_user(engine)
    clock.now = T0 + timedelta(minutes=10)
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def request() -> None:
        barrier.wait()
        codes.append(post(api, auth).status_code)

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(codes) == [202, 429]


def _wait_for_a_lock_wait(engine: Engine) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            ).scalar_one()
        if waiting:
            return
        time.sleep(0.02)
    raise AssertionError("the second request never waited on the first")


# --- refusals ---------------------------------------------------------------------


def test_no_connection_is_409(engine: Engine, clock: Clock, api: TestClient) -> None:
    user = make_user(engine)

    response = api.post(POLL, headers=user.auth)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "no_active_connection"


def test_a_revoked_connection_is_409_and_nothing_is_written(
    engine: Engine, clock: Clock, api: TestClient
) -> None:
    auth = signed_in_user(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE spotify_connection SET status = 'reauth_required', "
                "  reauth_reason = 'invalid_grant', refresh_token_ciphertext = NULL "
                "WHERE user_id = :u"
            ),
            {"u": user_of(auth)},
        )

    assert post(api, auth).status_code == 409
    assert requested_at(engine, user_of(auth)) is None


def test_not_signed_in_is_401(engine: Engine, clock: Clock, api: TestClient) -> None:
    assert api.post(POLL).status_code == 401
    assert api.post(POLL, headers={"Authorization": "Bearer not-a-session"}).status_code == 401


def test_the_per_user_request_limit_also_applies(
    engine: Engine, clock: Clock, api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "read_rate_limit_per_minute", 2)
    auth = signed_in_user(engine)
    clock.now = T0 + timedelta(minutes=10)

    assert [post(api, auth).status_code for _ in range(2)] == [202, 429]
    limited = post(api, auth)  # the third this minute: the request limit, not the cooldown
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Too Many Requests"


# --- state for the app ------------------------------------------------------------


def test_connection_status_reports_the_last_attempt_and_the_next_manual_poll(
    engine: Engine, fake_spotify: FakeSpotify, clock: Clock, api: TestClient
) -> None:
    auth = signed_in_user(engine)
    headers = {"Authorization": auth["Authorization"]}
    status = api.get("/v1/me/connection", headers=headers).json()
    assert status["last_poll_attempt_at"] is None
    assert status["next_manual_poll_at"] is None  # nothing started a cooldown: allowed now

    tick = T0 + timedelta(minutes=15)
    poll_connection(engine, fake_spotify.client(), user_of(auth), now=tick)
    status = api.get("/v1/me/connection", headers=headers).json()
    assert parse(status["last_poll_attempt_at"]) == tick
    assert parse(status["next_manual_poll_at"]) == tick + COOLDOWN

    clock.now = tick + COOLDOWN + timedelta(seconds=1)
    assert post(api, auth).status_code == 202
    status = api.get("/v1/me/connection", headers=headers).json()
    assert parse(status["last_poll_attempt_at"]) == tick
    assert parse(status["next_manual_poll_at"]) == clock.now + COOLDOWN
