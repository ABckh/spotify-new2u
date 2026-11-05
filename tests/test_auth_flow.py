"""Sign-in with Spotify end to end, against the fake Spotify and the real database."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.auth import utc_now
from app.api.main import app
from app.auth.pkce import code_challenge
from app.core.crypto import encrypt_secret, keyed_hash
from app.spotify.connection import decrypt_refresh_token, renew_by
from fake_spotify import CLIENT_ID, ME, REDIRECT_URI, TOKEN, FakeSpotify

CALLBACK = "/auth/spotify/callback"


@dataclass
class SignIn:
    login_id: str
    claim_secret: str
    state: str
    authorize_query: dict[str, list[str]]


def start(api: TestClient, home_timezone: str = "Europe/Helsinki") -> SignIn:
    response = api.post("/auth/spotify/login", json={"home_timezone": home_timezone})
    assert response.status_code == 201, response.text
    body = response.json()
    url = urlsplit(body["authorize_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.spotify.com/authorize"
    query = parse_qs(url.query)
    return SignIn(body["login_id"], body["claim_secret"], query["state"][0], query)


def finish(api: TestClient, started: SignIn, **params: str) -> str:
    """Run the callback in "the browser"; return the callback code sent to the app."""
    response = api.get(CALLBACK, params={"state": started.state, **params})
    assert response.status_code == 303, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == "new2u-test://signed-in"
    query = parse_qs(location.query)
    assert query["login_id"] == [started.login_id]
    return query["callback_code"][0]


def claim(
    api: TestClient,
    sign_in: SignIn,
    callback_code: str | None = None,
    secret: str | None = None,
) -> httpx.Response:
    body = {"claim_secret": secret or sign_in.claim_secret}
    if callback_code is not None:
        body["callback_code"] = callback_code
    response: httpx.Response = api.post(f"/auth/spotify/login/{sign_in.login_id}/claim", json=body)
    return response


def sign_in(api: TestClient, home_timezone: str = "Europe/Helsinki") -> str:
    started = start(api, home_timezone)
    callback_code = finish(api, started, code=f"synthetic-code-{started.login_id}")
    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 200, claimed.text
    token: str = claimed.json()["session_token"]
    return token


def connection(engine: Engine) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT c.*, u.home_timezone FROM spotify_connection c "
                "JOIN app_user u ON u.id = c.user_id"
            )
        ).one()


def test_full_flow_connects_stores_an_encrypted_token_and_issues_a_session(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    started = start(api, "America/Los_Angeles")

    # The authorize request: recently played plus the three read-only library
    # scopes of the known-music baseline, PKCE S256, our redirect URI verbatim.
    q = started.authorize_query
    assert q["client_id"] == [CLIENT_ID]
    assert q["response_type"] == ["code"]
    assert q["scope"] == [
        "user-read-recently-played user-follow-read user-library-read user-top-read"
    ]
    assert q["redirect_uri"] == [REDIRECT_URI]
    assert q["code_challenge_method"] == ["S256"]

    assert claim(api, started).status_code == 202  # not finished at Spotify yet

    callback_code = finish(api, started, code="synthetic-code")

    # The token exchange: same redirect URI, a verifier matching the challenge.
    form = parse_qs(fake_spotify.calls(TOKEN)[0].content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["redirect_uri"] == [REDIRECT_URI]
    assert code_challenge(form["code_verifier"][0]) == q["code_challenge"][0]
    assert "Authorization" not in fake_spotify.calls(TOKEN)[0].headers  # public client

    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 200
    token = claimed.json()["session_token"]
    assert "synthetic-refresh" not in claimed.text and "synthetic-access" not in claimed.text

    row = connection(engine)
    assert row.spotify_account_id == "synthetic-account-1"
    assert row.spotify_display_name == "Synthetic Listener"
    assert row.home_timezone == "America/Los_Angeles"
    assert row.status == "active"
    assert b"synthetic-refresh" not in bytes(row.refresh_token_ciphertext)
    assert (
        decrypt_refresh_token(row.user_id, bytes(row.refresh_token_ciphertext))
        == "synthetic-refresh-synthetic-account-1-1"
    )

    status = api.get("/v1/me/connection", headers={"Authorization": f"Bearer {token}"})
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "active"
    assert body["poll_interval_seconds"] == 900
    assert body["reconnect_soon"] is False
    assert "refresh" not in status.text

    # The claim works once.
    assert claim(api, started, callback_code).status_code == 404


def test_the_callback_and_its_code_are_accepted_exactly_once(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    started = start(api)
    params = {"code": "synthetic-code", "state": started.state}

    assert api.get(CALLBACK, params=params).status_code == 303
    replay = api.get(CALLBACK, params=params)

    assert replay.status_code == 400
    assert len(fake_spotify.calls(TOKEN)) == 1  # the replayed code never reached Spotify
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar_one() == 1


def test_an_unknown_state_is_rejected_without_calling_spotify(
    api: TestClient, fake_spotify: FakeSpotify
) -> None:
    start(api)

    response = api.get(CALLBACK, params={"code": "synthetic-code", "state": "forged-state"})

    assert response.status_code == 400
    assert fake_spotify.requests == []


def test_an_expired_sign_in_is_rejected(api: TestClient, fake_spotify: FakeSpotify) -> None:
    started = start(api)
    app.dependency_overrides[utc_now] = lambda: datetime.now(UTC) + timedelta(minutes=11)

    response = api.get(CALLBACK, params={"code": "synthetic-code", "state": started.state})

    assert response.status_code == 400
    assert fake_spotify.requests == []


def test_a_wrong_claim_secret_gets_nothing(api: TestClient) -> None:
    started = start(api)
    callback_code = finish(api, started, code="synthetic-code")

    assert claim(api, started, callback_code, secret="not-the-secret").status_code == 404
    # The right secret still works afterwards.
    assert claim(api, started, callback_code).status_code == 200


def test_the_claim_secret_alone_cannot_claim_a_finished_sign_in(api: TestClient) -> None:
    """WHO-36 F1: whoever started the sign-in cannot claim a session that another
    browser finished. They never see the callback code, which only goes to the
    APP_CALLBACK_URL of the device whose browser ran the callback."""
    started = start(api)
    callback_code = finish(api, started, code="synthetic-code")

    assert claim(api, started).status_code == 404
    assert claim(api, started, callback_code).status_code == 200


def test_a_wrong_callback_code_gets_nothing_and_does_not_use_up_the_sign_in(
    api: TestClient, engine: Engine
) -> None:
    started = start(api)
    callback_code = finish(api, started, code="synthetic-code")

    assert claim(api, started, "not-the-callback-code").status_code == 404
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM oauth_login")).scalar_one() == 1
        unclaimed = "SELECT count(*) FROM auth_session WHERE token_hash IS NULL"
        assert conn.execute(text(unclaimed)).scalar_one() == 1
    assert claim(api, started, callback_code).status_code == 200


def test_the_authorize_url_always_shows_the_consent_dialog(api: TestClient) -> None:
    """WHO-36 F1, defence in depth: a forwarded link is never one silent redirect."""
    assert start(api).authorize_query["show_dialog"] == ["true"]


def test_the_callback_code_is_stored_only_as_a_keyed_hash(api: TestClient, engine: Engine) -> None:
    started = start(api)
    callback_code = finish(api, started, code="synthetic-code")

    with engine.connect() as conn:
        stored = conn.execute(text("SELECT callback_code_hash FROM oauth_login")).scalar_one()
    assert bytes(stored) == keyed_hash(callback_code, purpose="callback")


def test_the_user_denying_access_fails_the_sign_in(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    started = start(api)

    callback_code = finish(api, started, error="access_denied")

    assert claim(api, started).status_code == 404  # the failure is the device's to read
    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "access_denied"}
    assert fake_spotify.requests == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar_one() == 0


def test_a_grant_without_the_scope_is_refused(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    fake_spotify.granted_scope = ""
    started = start(api)

    callback_code = finish(api, started, code="synthetic-code")

    assert claim(api, started, callback_code).json()["detail"] == {"reason": "scope_not_granted"}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM spotify_connection")).scalar_one() == 0


def test_a_spotify_outage_during_sign_in_fails_cleanly(
    api: TestClient, fake_spotify: FakeSpotify
) -> None:
    for _ in range(5):
        fake_spotify.fail_next(ME, httpx.Response(503))
    started = start(api)

    callback_code = finish(api, started, code="synthetic-code")

    assert claim(api, started, callback_code).json()["detail"] == {"reason": "me_server_error"}


def test_an_undecodable_token_answer_fails_the_sign_in_instead_of_crashing(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    """WHO-42 S3: a non-network httpx error must not leave the row at callback_used."""
    fake_spotify.fail_next(TOKEN, httpx.DecodingError("synthetic: body does not decode"))
    started = start(api)

    callback_code = finish(api, started, code="synthetic-code")

    with engine.connect() as conn:
        outcome = conn.execute(
            text("SELECT outcome FROM oauth_login WHERE id = :id"), {"id": started.login_id}
        ).scalar_one()
    assert outcome == "failed"
    assert len(fake_spotify.calls(TOKEN)) == 1  # not retried
    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "token_http_error"}


def test_a_verifier_that_does_not_decrypt_fails_the_sign_in(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    """WHO-36 F5: e.g. TOKEN_ENCRYPTION_KEY changed while the sign-in was open."""
    started = start(api)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE oauth_login SET code_verifier_ciphertext = :v"),
            {"v": encrypt_secret("synthetic-verifier", "sealed-for-something-else")},
        )

    callback_code = finish(api, started, code="synthetic-code")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT outcome FROM oauth_login")).scalar_one() == "failed"
    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "internal_error"}
    assert fake_spotify.calls(TOKEN) == []


def test_an_error_while_connecting_fails_the_sign_in_and_keeps_nothing(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHO-36 F5: an exception from connect() must not leave the sign-in stuck."""

    def broken_connect(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("app.auth.login.connect", broken_connect)
    started = start(api)

    callback_code = finish(api, started, code="synthetic-code")

    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "internal_error"}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM auth_session")).scalar_one() == 0


@pytest.mark.parametrize("zone", ["PST", "EST", "-08:00", "Etc/GMT+8", "Mars/Olympus", ""])
def test_home_timezone_must_be_an_iana_region(api: TestClient, zone: str) -> None:
    response = api.post("/auth/spotify/login", json={"home_timezone": zone})

    assert response.status_code == 422


def test_logout_revokes_the_session(api: TestClient) -> None:
    token = sign_in(api)
    headers = {"Authorization": f"Bearer {token}"}
    assert api.get("/v1/me/connection", headers=headers).status_code == 200

    assert api.post("/auth/logout", headers=headers).status_code == 204

    assert api.get("/v1/me/connection", headers=headers).status_code == 401


def test_an_expired_session_is_refused(api: TestClient) -> None:
    token = sign_in(api)
    app.dependency_overrides[utc_now] = lambda: datetime.now(UTC) + timedelta(hours=2)

    response = api.get("/v1/me/connection", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_no_token_and_a_forged_token_are_refused(api: TestClient) -> None:
    sign_in(api)

    assert api.get("/v1/me/connection").status_code == 401
    forged = {"Authorization": "Bearer forged"}
    assert api.get("/v1/me/connection", headers=forged).status_code == 401


def test_signing_in_again_keeps_collection_start_and_timezone(
    api: TestClient, engine: Engine
) -> None:
    sign_in(api, "Europe/Helsinki")
    first = connection(engine)

    sign_in(api, "Asia/Tokyo")
    second = connection(engine)

    assert second.user_id == first.user_id
    assert second.collection_started_at == first.collection_started_at
    assert second.authorized_at > first.authorized_at
    assert second.home_timezone == "Europe/Helsinki"  # set once, at signup
    assert decrypt_refresh_token(second.user_id, bytes(second.refresh_token_ciphertext)).endswith(
        "-2"
    )


def test_a_confidential_client_also_sends_basic_auth(fake_spotify: FakeSpotify) -> None:
    client = fake_spotify.client(client_secret="test-client-secret")

    client.exchange_code(code="synthetic-code", code_verifier="v" * 43)

    request = fake_spotify.calls(TOKEN)[0]
    assert request.headers["Authorization"].startswith("Basic ")
    assert parse_qs(request.content.decode())["code_verifier"] == ["v" * 43]


def test_connection_status_says_reconnect_soon_30_days_before_expiry(
    api: TestClient, engine: Engine
) -> None:
    headers = {"Authorization": f"Bearer {sign_in(api)}"}
    status = api.get("/v1/me/connection", headers=headers).json()
    authorized = datetime.fromisoformat(status["authorized_at"])
    assert datetime.fromisoformat(status["grant_expires_at"]) - authorized == timedelta(days=180)
    assert status["reconnect_soon"] is False

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE spotify_connection SET authorized_at = :t"),
            {"t": datetime.now(UTC) - timedelta(days=151)},
        )

    assert api.get("/v1/me/connection", headers=headers).json()["reconnect_soon"] is True


def test_renew_by_is_the_day_before_the_grant_ends_in_the_display_zone() -> None:
    """S9.12. Ends 2027-03-30T23:30Z: already 31 March in Helsinki, still 30th in UTC."""
    authorized = datetime(2026, 10, 1, 23, 30, tzinfo=UTC)

    assert renew_by(authorized, "Europe/Helsinki") == date(2027, 3, 30)
    assert renew_by(authorized, "UTC") == date(2027, 3, 29)
    assert renew_by(authorized, "America/Los_Angeles") == date(2027, 3, 29)


def test_connection_status_reports_renew_by_in_the_stored_zone(
    api: TestClient, engine: Engine
) -> None:
    headers = {"Authorization": f"Bearer {sign_in(api, 'Asia/Tokyo')}"}
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE spotify_connection SET authorized_at = :t"),
            {"t": datetime(2026, 10, 1, 16, 0, tzinfo=UTC)},
        )

    status = api.get("/v1/me/connection", headers=headers).json()

    # Ends 2027-03-30T16:00Z, which is 31 March 01:00 in Tokyo.
    assert status["grant_expires_at"].startswith("2027-03-30T16:00:00")
    assert status["renew_by"] == "2027-03-30"


def assert_no_store(response: httpx.Response) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_sign_in_answers_are_never_cacheable(api: TestClient) -> None:
    """RFC 6749 5.1: the claim secret and the session token must not be cached."""
    login = api.post("/auth/spotify/login", json={"home_timezone": "Europe/Helsinki"})
    assert login.status_code == 201
    assert_no_store(login)
    body = login.json()
    started = SignIn(body["login_id"], body["claim_secret"], "", {})
    state = parse_qs(urlsplit(body["authorize_url"]).query)["state"][0]

    pending = claim(api, started)
    assert pending.status_code == 202
    assert_no_store(pending)

    redirect = api.get(CALLBACK, params={"code": "synthetic-code", "state": state})
    assert redirect.status_code == 303
    assert_no_store(redirect)
    callback_code = parse_qs(urlsplit(redirect.headers["location"]).query)["callback_code"][0]

    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 200
    assert_no_store(claimed)

    # Error answers too: a replayed callback page, a used-up claim, a bad body.
    replay = api.get(CALLBACK, params={"code": "synthetic-code", "state": state})
    assert replay.status_code == 400
    assert_no_store(replay)
    reused = claim(api, started, callback_code)
    assert reused.status_code == 404
    assert_no_store(reused)
    invalid = api.post("/auth/spotify/login", json={})
    assert invalid.status_code == 422
    assert_no_store(invalid)


def test_other_routes_are_left_alone(api: TestClient) -> None:
    assert "Cache-Control" not in api.get("/healthz").headers
