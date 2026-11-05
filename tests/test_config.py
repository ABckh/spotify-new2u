"""Settings refuse values that would weaken sign-in (WHO-36 F1, F6; WHO-40 N1, N2)."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings

REQUIRED = {
    "database_url": "postgresql+psycopg://test:test@localhost:5432/test",
    "token_encryption_key": "unused",
    "session_token_secret": "unused",
}


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **values})  # type: ignore[call-arg]


@pytest.mark.parametrize("ttl", [0, -1, 86_401, 30 * 86_400])
def test_a_session_ttl_outside_one_second_to_one_day_is_refused(ttl: int) -> None:
    with pytest.raises(ValidationError):
        settings(session_token_ttl_seconds=ttl)


def test_a_session_ttl_of_one_day_is_allowed() -> None:
    assert settings(session_token_ttl_seconds=86_400).session_token_ttl_seconds == 86_400


@pytest.mark.parametrize("env", ["staging", "production"])
def test_app_callback_url_is_required_outside_local(env: str) -> None:
    with pytest.raises(ValidationError, match="APP_CALLBACK_URL"):
        settings(app_env=env, app_callback_url="")

    assert settings(app_env=env, app_callback_url="new2u://signed-in").app_env == env


LOCAL_REDIRECT = "http://127.0.0.1:8000/auth/spotify/callback"


@pytest.mark.parametrize(
    "redirect_uri",
    [
        LOCAL_REDIRECT,
        "http://localhost:8000/auth/spotify/callback",
        "http://[::1]:8000/auth/spotify/callback",
        "",
    ],
)
def test_app_callback_url_may_be_empty_locally_on_a_loopback_redirect(redirect_uri: str) -> None:
    configured = settings(app_env="local", app_callback_url="", spotify_redirect_uri=redirect_uri)

    assert configured.app_callback_url == ""


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://api.example/auth/spotify/callback",
        "http://192.0.2.10:8000/auth/spotify/callback",
        "http://127.0.0.1.example/auth/spotify/callback",
        "http://localhost.example/auth/spotify/callback",
    ],
)
def test_an_empty_app_callback_url_on_a_public_redirect_is_refused(redirect_uri: str) -> None:
    """WHO-40 N1: a server whose .env kept APP_ENV=local must not show callback codes."""
    with pytest.raises(ValidationError, match="SPOTIFY_REDIRECT_URI"):
        settings(app_env="local", app_callback_url="", spotify_redirect_uri=redirect_uri)


def test_app_env_left_out_on_a_public_redirect_is_refused() -> None:
    with pytest.raises(ValidationError, match="SPOTIFY_REDIRECT_URI"):
        settings(app_callback_url="", spotify_redirect_uri="https://api.example/cb")


@pytest.mark.parametrize("url", ["new2u://signed-in?next=x", "new2u://signed-in#x"])
def test_app_callback_url_must_not_carry_a_query_or_fragment(url: str) -> None:
    with pytest.raises(ValidationError):
        settings(app_callback_url=url)


@pytest.mark.parametrize("url", ["new2u://signed-in", "com.example.new2u://auth", "x-y+z.1://cb"])
def test_app_callback_url_may_be_a_custom_scheme(url: str) -> None:
    assert settings(app_callback_url=url).app_callback_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://attacker.example/collect",
        "https://attacker.example/collect",
        "https://www.example.com/signed-in",
        "javascript://signed-in",
        "data://signed-in",
        "file:///tmp/signed-in",
        "wss://attacker.example/collect",
        "new2u:signed-in",
        "new2u://",
        "NEW2U://signed-in",
        "//attacker.example/collect",
        "signed-in",
        "new2u://signed in",
        " new2u://signed-in",
    ],
)
def test_app_callback_url_must_be_an_app_scheme(url: str) -> None:
    """WHO-40 N2: the callback code must go to the app, never to a web page."""
    with pytest.raises(ValidationError, match="APP_CALLBACK_URL"):
        settings(app_callback_url=url)


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        ("abc", frozenset({"abc"})),
        (" abc , def ", frozenset({"abc", "def"})),
        ("abc,,def,", frozenset({"abc", "def"})),
    ],
)
def test_spotify_allowed_account_ids_are_trimmed_and_empty_items_dropped(
    ids: str, expected: frozenset[str]
) -> None:
    assert settings(spotify_allowed_account_ids=ids).spotify_account_allowlist == expected


@pytest.mark.parametrize("ids", ["", " ", " , ,"])
def test_an_empty_allowlist_allows_any_account_only_locally(ids: str) -> None:
    """WHO-74: empty means everyone in local, and no one in staging or production."""
    assert settings(spotify_allowed_account_ids=ids).spotify_account_allowlist is None
    for env in ("staging", "production"):
        allowlist = settings(
            app_env=env, app_callback_url="new2u://signed-in", spotify_allowed_account_ids=ids
        ).spotify_account_allowlist
        assert allowlist == frozenset()


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://203.0.113.11/auth/spotify/callback",
        "https://[2001:db8::10]/auth/spotify/callback",
    ],
)
def test_a_redirect_on_the_servers_ip_address_works_like_a_name(redirect_uri: str) -> None:
    """WHO-180: API_HOSTNAME may be the server's IP, so the redirect URI is on it too."""
    configured = settings(
        app_env="production",
        app_callback_url="new2u://signed-in",
        spotify_redirect_uri=redirect_uri,
    )
    assert configured.spotify_redirect_uri == redirect_uri

    # A public IP is not loopback: the callback code never goes to a web page.
    with pytest.raises(ValidationError, match="SPOTIFY_REDIRECT_URI"):
        settings(app_env="local", app_callback_url="", spotify_redirect_uri=redirect_uri)
