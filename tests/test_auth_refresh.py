"""Refresh tokens: the app stays signed in past the one-hour session (rotation, reuse detection)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.auth import utc_now
from app.api.main import app
from app.auth.sessions import purge_expired_sessions
from app.core.config import get_settings
from app.core.crypto import keyed_hash
from test_auth_flow import claim, finish, start

CONNECTION = "/v1/me/connection"


def sign_in(api: TestClient) -> dict[str, Any]:
    started = start(api)
    callback_code = finish(api, started, code=f"synthetic-code-{started.login_id}")
    claimed = claim(api, started, callback_code)
    assert claimed.status_code == 200, claimed.text
    body: dict[str, Any] = claimed.json()
    return body


def refresh(api: TestClient, refresh_token: str) -> httpx.Response:
    response: httpx.Response = api.post("/auth/refresh", json={"refresh_token": refresh_token})
    return response


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def at(moment: datetime) -> None:
    app.dependency_overrides[utc_now] = lambda: moment


def parse(instant: str) -> datetime:
    return datetime.fromisoformat(instant.replace("Z", "+00:00"))


def test_the_claim_hands_out_a_refresh_token_that_outlives_the_session(api: TestClient) -> None:
    claimed = sign_in(api)

    assert claimed["refresh_token"] and claimed["refresh_token"] != claimed["session_token"]
    lifetime = parse(claimed["refresh_expires_at"]) - parse(claimed["expires_at"])
    ttl = get_settings().refresh_token_ttl_seconds - get_settings().session_token_ttl_seconds
    assert abs(lifetime.total_seconds() - ttl) < 5


def test_a_refresh_after_the_session_expired_signs_the_app_back_in(api: TestClient) -> None:
    claimed = sign_in(api)
    at(datetime.now(UTC) + timedelta(hours=2))
    assert api.get(CONNECTION, headers=bearer(claimed["session_token"])).status_code == 401

    refreshed = refresh(api, claimed["refresh_token"])

    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert set(body) == {"session_token", "expires_at", "refresh_token", "refresh_expires_at"}
    assert body["session_token"] != claimed["session_token"]
    assert body["refresh_token"] != claimed["refresh_token"]
    assert api.get(CONNECTION, headers=bearer(body["session_token"])).status_code == 200


def test_each_refresh_moves_the_sign_in_forward(api: TestClient) -> None:
    claimed = sign_in(api)
    later = datetime.now(UTC) + timedelta(days=60)
    at(later)

    body = refresh(api, claimed["refresh_token"]).json()

    assert parse(body["refresh_expires_at"]) > parse(claimed["refresh_expires_at"])
    # Another 60 days on, past the first refresh token's expiry, the new one still works.
    at(later + timedelta(days=60))
    assert refresh(api, body["refresh_token"]).status_code == 200


def test_a_refresh_token_works_once_and_its_reuse_revokes_the_session(
    api: TestClient, engine: Engine
) -> None:
    claimed = sign_in(api)
    first = refresh(api, claimed["refresh_token"]).json()

    # The old refresh token again: someone else holds it. Nobody keeps the session.
    reused = refresh(api, claimed["refresh_token"])

    assert reused.status_code == 401
    assert refresh(api, first["refresh_token"]).status_code == 401
    assert api.get(CONNECTION, headers=bearer(first["session_token"])).status_code == 401
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM auth_session")).scalar_one() == 0


def test_an_expired_refresh_token_means_signing_in_again(api: TestClient) -> None:
    claimed = sign_in(api)
    at(parse(claimed["refresh_expires_at"]) + timedelta(seconds=1))

    assert refresh(api, claimed["refresh_token"]).status_code == 401


def test_unknown_session_and_refresh_tokens_are_refused(api: TestClient) -> None:
    claimed = sign_in(api)

    assert refresh(api, "forged").status_code == 401
    # A session token is not a refresh token (the hashes have different purposes).
    assert refresh(api, claimed["session_token"]).status_code == 401
    assert api.get(CONNECTION, headers=bearer(claimed["refresh_token"])).status_code == 401


def test_logout_ends_the_refresh_token_too(api: TestClient) -> None:
    claimed = sign_in(api)

    assert api.post("/auth/logout", headers=bearer(claimed["session_token"])).status_code == 204

    assert refresh(api, claimed["refresh_token"]).status_code == 401


def test_a_deleted_account_cannot_be_refreshed(api: TestClient) -> None:
    claimed = sign_in(api)

    assert api.delete("/v1/me", headers=bearer(claimed["session_token"])).status_code == 204

    assert refresh(api, claimed["refresh_token"]).status_code == 401


def test_the_purge_keeps_a_session_while_its_refresh_token_works(
    api: TestClient, engine: Engine
) -> None:
    claimed = sign_in(api)
    refresh_expires_at = parse(claimed["refresh_expires_at"])

    with engine.begin() as conn:
        assert purge_expired_sessions(conn, datetime.now(UTC) + timedelta(hours=2)) == 0
        assert purge_expired_sessions(conn, refresh_expires_at + timedelta(seconds=1)) == 1


def test_only_keyed_hashes_of_the_refresh_tokens_are_stored(
    api: TestClient, engine: Engine
) -> None:
    claimed = sign_in(api)
    refreshed = refresh(api, claimed["refresh_token"]).json()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT refresh_token_hash, previous_refresh_hash FROM auth_session")
        ).one()
    current, previous = refreshed["refresh_token"], claimed["refresh_token"]
    assert bytes(row.refresh_token_hash) == keyed_hash(current, purpose="refresh")
    assert bytes(row.previous_refresh_hash) == keyed_hash(previous, purpose="refresh")


def test_refresh_answers_are_never_cacheable(api: TestClient) -> None:
    claimed = sign_in(api)

    for response in (refresh(api, claimed["refresh_token"]), refresh(api, "forged")):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
