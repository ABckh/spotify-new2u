"""Only Spotify accounts on SPOTIFY_ALLOWED_ACCOUNT_IDS may sign in (WHO-36 F3, WHO-74)."""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.auth import login
from app.auth.login import warn_if_no_account_may_sign_in
from app.core.config import get_settings
from fake_spotify import FakeSpotify
from test_auth_flow import CALLBACK, claim, connection, sign_in, start
from test_auth_renewal import counts, snapshot, start_renewal

ACCOUNT = "synthetic-account-1"  # the fake Spotify's account
UNLISTED = "synthetic-account-unlisted"


def allow(monkeypatch: pytest.MonkeyPatch, ids: str, app_env: str = "local") -> None:
    monkeypatch.setattr(get_settings(), "spotify_allowed_account_ids", ids)
    monkeypatch.setattr(get_settings(), "app_env", app_env)


def assert_refused(api: TestClient, engine: Engine) -> None:
    """A first sign-in is refused with account_not_allowed and stores nothing."""
    started = start(api)
    response = api.get(CALLBACK, params={"state": started.state, "code": "synthetic-code"})
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["result"] == ["failed"]

    assert counts(engine) == {
        "app_user": 0,
        "spotify_connection": 0,
        "auth_session": 0,
        "spotify_renewal": 0,
    }
    claimed = claim(api, started, query["callback_code"][0])
    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "account_not_allowed"}


def test_an_unlisted_account_creates_nothing_and_the_claim_says_account_not_allowed(
    api: TestClient,
    fake_spotify: FakeSpotify,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="app")
    allow(monkeypatch, ACCOUNT, app_env="production")
    fake_spotify.account_id = UNLISTED

    assert_refused(api, engine)

    # The refusal is logged by reason; the refused account's ID never is.
    assert "account_not_allowed" in caplog.text
    assert UNLISTED not in caplog.text


@pytest.mark.parametrize("app_env", ["local", "production"])
def test_a_listed_account_signs_in(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    # Spaces are trimmed and empty items dropped.
    allow(monkeypatch, f" {UNLISTED} ,, {ACCOUNT} ,", app_env=app_env)

    sign_in(api)

    assert connection(engine).spotify_account_id == ACCOUNT


@pytest.mark.parametrize("ids", ["", " , ,"])
def test_an_empty_list_in_production_refuses_every_account(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch, ids: str
) -> None:
    allow(monkeypatch, ids, app_env="production")

    assert_refused(api, engine)


def test_an_empty_list_in_local_allows_any_account(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow(monkeypatch, "", app_env="local")

    sign_in(api)

    assert connection(engine).spotify_account_id == ACCOUNT


def test_an_account_taken_off_the_list_cannot_renew_and_nothing_changes(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow(monkeypatch, ACCOUNT, app_env="production")
    token = sign_in(api)
    before, counts_before = snapshot(engine), counts(engine)
    allow(monkeypatch, UNLISTED, app_env="production")

    renewal = start_renewal(api, token)
    response = api.get(CALLBACK, params={"state": renewal.state, "code": "synthetic-code-2"})
    callback_code = parse_qs(urlsplit(response.headers["location"]).query)["callback_code"][0]
    claimed = claim(api, renewal, callback_code)

    assert claimed.status_code == 400
    assert claimed.json()["detail"] == {"reason": "account_not_allowed"}
    assert snapshot(engine) == before
    # The renewal row went with the claim; nothing else changed.
    assert counts(engine) == counts_before


def test_the_refusal_happens_before_connect_is_called(
    api: TestClient, fake_spotify: FakeSpotify, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow(monkeypatch, ACCOUNT, app_env="production")
    fake_spotify.account_id = UNLISTED

    def connect_must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("connect() ran for an account not on the list")

    monkeypatch.setattr(login, "connect", connect_must_not_run)

    # connect() raising would end as internal_error, not account_not_allowed.
    assert_refused(api, engine)


@pytest.mark.parametrize(
    ("allowlist", "warns"),
    [(frozenset(), True), (None, False), (frozenset({ACCOUNT}), False)],
)
def test_startup_warns_only_when_no_account_may_sign_in(
    caplog: pytest.LogCaptureFixture, allowlist: frozenset[str] | None, warns: bool
) -> None:
    with caplog.at_level("WARNING", logger="app.auth"):
        warn_if_no_account_may_sign_in(allowlist)
    assert ("SPOTIFY_ALLOWED_ACCOUNT_IDS is empty" in caplog.text) is warns
