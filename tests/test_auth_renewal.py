"""Renewing Spotify access while signed in (S9.16, WHO-61).

``POST /auth/spotify/login`` with a session token is a renewal for its user. The
same Spotify account renews the connection; a different one is refused and
changes nothing.
"""

import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

import app.auth.login as login_module
from app.spotify.connection import decrypt_refresh_token
from app.spotify.profile import SpotifyProfile, profile_from_me
from fake_spotify import TOKEN, FakeSpotify
from read_helpers import NOW, make_user
from test_auth_flow import SignIn, claim, connection, finish, sign_in

OTHER_ACCOUNT = "synthetic-account-2"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def start_renewal(api: TestClient, token: str) -> SignIn:
    response = api.post(
        "/auth/spotify/login", json={"home_timezone": "Asia/Tokyo"}, headers=bearer(token)
    )
    assert response.status_code == 201, response.text
    body = response.json()
    query = parse_qs(urlsplit(body["authorize_url"]).query)
    return SignIn(body["login_id"], body["claim_secret"], query["state"][0], query)


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("app_user", "spotify_connection", "auth_session", "spotify_renewal")
        }


def snapshot(engine: Engine) -> dict[str, Any]:
    row = connection(engine)
    return {
        "user_id": row.user_id,
        "spotify_account_id": row.spotify_account_id,
        "refresh_token_ciphertext": row.refresh_token_ciphertext,
        "authorized_at": row.authorized_at,
        "collection_started_at": row.collection_started_at,
        "status": row.status,
    }


def test_a_different_account_is_refused_and_nothing_changes(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    token = sign_in(api)
    before, counts_before = snapshot(engine), counts(engine)
    fake_spotify.account_id = OTHER_ACCOUNT

    renewal = start_renewal(api, token)
    callback_code = finish(api, renewal, code="synthetic-code-renewal")
    claimed = claim(api, renewal, callback_code)

    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "different_account"}
    assert snapshot(engine) == before
    assert counts(engine) == counts_before  # no user, no connection, no session added
    # The grant for the other account was obtained, then dropped unstored.
    assert decrypt_refresh_token(
        before["user_id"], bytes(before["refresh_token_ciphertext"])
    ).startswith("synthetic-refresh-synthetic-account-1-")
    # The app keeps its current session, still on the first account.
    status = api.get("/v1/me/connection", headers=bearer(token))
    assert status.status_code == 200
    assert status.json()["spotify_account_id"] == "synthetic-account-1"
    # The account ids are never logged, only the reason.
    assert "different_account" in caplog.text
    assert OTHER_ACCOUNT not in caplog.text


def test_the_same_account_renews_the_connection(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    token = sign_in(api, "Europe/Helsinki")
    before, counts_before = snapshot(engine), counts(engine)

    renewal = start_renewal(api, token)
    callback_code = finish(api, renewal, code="synthetic-code-renewal")
    claimed = claim(api, renewal, callback_code)

    assert claimed.status_code == 200, claimed.text
    after = snapshot(engine)
    assert after["user_id"] == before["user_id"]
    assert after["authorized_at"] > before["authorized_at"]  # the 6-month clock restarts
    assert after["collection_started_at"] == before["collection_started_at"]
    token_after = decrypt_refresh_token(after["user_id"], bytes(after["refresh_token_ciphertext"]))
    assert token_after.endswith("-2")
    assert connection(engine).home_timezone == "Europe/Helsinki"  # set once, at signup
    # One more session, for the same user; the renewal row went with the claim.
    assert counts(engine) == {**counts_before, "auth_session": counts_before["auth_session"] + 1}
    new_token = claimed.json()["session_token"]
    status = api.get("/v1/me/connection", headers=bearer(new_token)).json()
    assert status["spotify_account_id"] == "synthetic-account-1"


def needs_reauth(engine: Engine) -> None:
    """As the poller leaves a connection after Spotify answered invalid_grant."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE spotify_connection SET status = 'reauth_required', "
                "  reauth_reason = 'invalid_grant', refresh_token_ciphertext = NULL"
            )
        )


def test_renewing_a_connection_that_needs_reauth_with_the_same_account_reconnects(
    api: TestClient, engine: Engine
) -> None:
    token = sign_in(api)
    needs_reauth(engine)

    renewal = start_renewal(api, token)
    claimed = claim(api, renewal, finish(api, renewal, code="synthetic-code-renewal"))

    assert claimed.status_code == 200, claimed.text
    row = connection(engine)
    assert (row.status, row.reauth_reason) == ("active", None)
    assert row.refresh_token_ciphertext is not None


def test_a_different_account_cannot_reconnect_a_connection_that_needs_reauth(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    token = sign_in(api)
    needs_reauth(engine)
    before = snapshot(engine)
    fake_spotify.account_id = OTHER_ACCOUNT

    renewal = start_renewal(api, token)
    claimed = claim(api, renewal, finish(api, renewal, code="synthetic-code-renewal"))

    assert claimed.json()["detail"] == {"reason": "different_account"}
    assert snapshot(engine) == before


@pytest.mark.parametrize(
    "authorization", ["Bearer forged", "Bearer ", "Basic c3ludGhldGlj", "forged"]
)
def test_an_invalid_session_token_is_refused_not_treated_as_a_fresh_sign_in(
    api: TestClient, engine: Engine, authorization: str
) -> None:
    sign_in(api)
    counts_before = counts(engine)

    response = api.post(
        "/auth/spotify/login",
        json={"home_timezone": "Europe/Helsinki"},
        headers={"Authorization": authorization},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"
    assert counts(engine) == counts_before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM oauth_login")).scalar_one() == 0


def test_a_logged_out_session_cannot_start_a_renewal(api: TestClient) -> None:
    token = sign_in(api)
    assert api.post("/auth/logout", headers=bearer(token)).status_code == 204

    response = api.post(
        "/auth/spotify/login", json={"home_timezone": "Europe/Helsinki"}, headers=bearer(token)
    )

    assert response.status_code == 401


def test_an_expired_session_cannot_start_a_renewal(api: TestClient, engine: Engine) -> None:
    token = sign_in(api)
    with engine.begin() as conn:
        conn.execute(text("UPDATE auth_session SET expires_at = :now"), {"now": NOW})

    response = api.post(
        "/auth/spotify/login", json={"home_timezone": "Europe/Helsinki"}, headers=bearer(token)
    )

    assert response.status_code == 401
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM oauth_login WHERE is_renewal")).scalar_one()
            == 0
        )


def test_a_user_without_a_connection_has_nothing_to_renew(api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    renewal = start_renewal(api, user.token)
    claimed = claim(api, renewal, finish(api, renewal, code="synthetic-code-renewal"))

    assert claimed.json()["detail"] == {"reason": "different_account"}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM spotify_connection")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar_one() == 1


def test_the_renewal_row_lives_only_until_the_claim(api: TestClient, engine: Engine) -> None:
    token = sign_in(api)

    renewal = start_renewal(api, token)
    assert counts(engine)["spotify_renewal"] == 1
    callback_code = finish(api, renewal, code="synthetic-code-renewal")
    assert claim(api, renewal, callback_code).status_code == 200

    assert counts(engine)["spotify_renewal"] == 0


def test_delete_all_removes_a_pending_renewal(api: TestClient, engine: Engine) -> None:
    token = sign_in(api)
    start_renewal(api, token)
    assert counts(engine)["spotify_renewal"] == 1

    assert api.delete("/v1/me", headers=bearer(token)).status_code == 204

    assert counts(engine) == {
        "app_user": 0,
        "spotify_connection": 0,
        "auth_session": 0,
        "spotify_renewal": 0,
    }


def is_renewal(engine: Engine, login_id: str) -> bool:
    with engine.connect() as conn:
        value: bool = conn.execute(
            text("SELECT is_renewal FROM oauth_login WHERE id = :id"), {"id": login_id}
        ).scalar_one()
        return value


def test_only_a_sign_in_with_a_session_token_is_marked_as_a_renewal(
    api: TestClient, engine: Engine
) -> None:
    token = sign_in(api)
    fresh = api.post("/auth/spotify/login", json={"home_timezone": "Europe/Helsinki"})

    renewal = start_renewal(api, token)

    assert is_renewal(engine, fresh.json()["login_id"]) is False
    assert is_renewal(engine, renewal.login_id) is True


def test_a_renewal_finished_after_delete_all_fails_and_creates_no_account(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine
) -> None:
    token = sign_in(api)
    renewal = start_renewal(api, token)
    assert api.delete("/v1/me", headers=bearer(token)).status_code == 204
    exchanges_before = len(fake_spotify.calls(TOKEN))

    claimed = claim(api, renewal, finish(api, renewal, code="synthetic-code-renewal"))

    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "renewal_gone"}
    # Refused before the code was exchanged: no grant was obtained at all.
    assert len(fake_spotify.calls(TOKEN)) == exchanges_before
    assert counts(engine) == {
        "app_user": 0,
        "spotify_connection": 0,
        "auth_session": 0,
        "spotify_renewal": 0,
    }


def test_a_delete_all_while_spotify_answers_still_fails_the_renewal(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete-all lands after the state check but before the user is written."""
    token = sign_in(api)
    renewal = start_renewal(api, token)

    def delete_all_then_read(body: Mapping[str, object]) -> SpotifyProfile:
        assert api.delete("/v1/me", headers=bearer(token)).status_code == 204
        return profile_from_me(body)

    monkeypatch.setattr("app.auth.login.profile_from_me", delete_all_then_read)

    claimed = claim(api, renewal, finish(api, renewal, code="synthetic-code-renewal"))

    assert claimed.json()["detail"] == {"reason": "renewal_gone"}
    assert counts(engine) == {
        "app_user": 0,
        "spotify_connection": 0,
        "auth_session": 0,
        "spotify_renewal": 0,
    }


def wait_until_waiting_for_a_lock(engine: Engine, pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            ).scalar_one_or_none()
        if waiting:
            return
        time.sleep(0.01)
    raise AssertionError("delete-all never waited for the renewal's lock")


class DeleteAll(threading.Thread):
    """``DELETE FROM app_user`` for one user, in its own connection."""

    def __init__(self, engine: Engine, user_id: Any) -> None:
        super().__init__()
        self.engine = engine
        self.user_id = user_id
        self.pid: int | None = None
        self.started_sql = threading.Event()
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            with self.engine.begin() as conn:
                self.pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
                self.started_sql.set()
                conn.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": self.user_id})
        except BaseException as exc:
            self.error = exc
        finally:
            self.started_sql.set()


def test_a_delete_all_racing_the_renewal_locks_does_not_deadlock(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHO-65 F1: delete-all starts between the renewal's two locking reads.

    Both transactions must commit: the renewal first, then the delete, which
    removes everything, including the session the renewal issued. With the
    locks taken in the opposite order to the delete's cascade, Postgres
    aborted one of them with DeadlockDetected.
    """
    token = sign_in(api)
    renewal = start_renewal(api, token)
    with engine.connect() as conn:
        user_id = conn.execute(text("SELECT id FROM app_user")).scalar_one()
    delete_all = DeleteAll(engine, user_id)
    real_check = login_module._check_renews_own_account

    def start_delete_all_then_check(conn: Connection, user: Any, account_id: str) -> None:
        delete_all.start()
        delete_all.started_sql.wait(timeout=10)
        assert delete_all.pid is not None, delete_all.error
        wait_until_waiting_for_a_lock(engine, delete_all.pid)
        real_check(conn, user, account_id)

    monkeypatch.setattr(login_module, "_check_renews_own_account", start_delete_all_then_check)

    callback_code = finish(api, renewal, code="synthetic-code-renewal")
    delete_all.join(timeout=10)

    assert not delete_all.is_alive()
    assert delete_all.error is None
    with engine.connect() as conn:
        outcome = conn.execute(
            text("SELECT outcome FROM oauth_login WHERE id = :id"), {"id": renewal.login_id}
        ).scalar_one()
    assert outcome == "connected"
    # The session the renewal issued went with the user, so there is nothing to claim.
    assert claim(api, renewal, callback_code).status_code == 404
    assert counts(engine) == {
        "app_user": 0,
        "spotify_connection": 0,
        "auth_session": 0,
        "spotify_renewal": 0,
    }
