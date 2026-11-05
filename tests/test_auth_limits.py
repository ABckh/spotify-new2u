"""WHO-36 F2: nobody can fill the database or burn CPU through the sign-in routes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.main import app
from app.api.rate_limit import RateLimiter, client_key
from app.auth import login as login_module
from app.core.config import get_settings

LOGIN = "/auth/spotify/login"
BODY = {"home_timezone": "Europe/Helsinki"}


def logins(engine: Engine) -> int:
    with engine.connect() as conn:
        n: int = conn.execute(text("SELECT count(*) FROM oauth_login")).scalar_one()
        return n


def behind_proxy(trusted_hosts: str) -> TestClient:
    """The app as uvicorn serves it with --proxy-headers and FORWARDED_ALLOW_IPS."""
    # uvicorn types its ASGI app more narrowly than Starlette does; same protocol.
    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts))  # type: ignore[arg-type]


def test_a_login_over_the_pending_cap_gets_503_and_stores_nothing(
    api: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_pending_logins", 3)
    for _ in range(3):
        assert api.post(LOGIN, json=BODY).status_code == 201

    over = api.post(LOGIN, json=BODY)

    assert over.status_code == 503
    assert over.headers["retry-after"] == "600"
    assert over.headers["cache-control"] == "no-store"
    assert logins(engine) == 3


def test_the_11th_login_in_a_minute_from_one_ip_gets_429(api: TestClient, engine: Engine) -> None:
    for _ in range(10):
        assert api.post(LOGIN, json=BODY).status_code == 201

    over = api.post(LOGIN, json=BODY)

    assert over.status_code == 429
    assert 0 < int(over.headers["retry-after"]) <= 60
    assert over.headers["cache-control"] == "no-store"
    assert logins(engine) == 10


def test_the_61st_claim_in_a_minute_from_one_ip_gets_429(api: TestClient) -> None:
    login_id = api.post(LOGIN, json=BODY).json()["login_id"]
    url = f"/auth/spotify/login/{login_id}/claim"
    for _ in range(60):
        assert api.post(url, json={"claim_secret": "guess"}).status_code == 404

    assert api.post(url, json={"claim_secret": "guess"}).status_code == 429


def test_behind_a_trusted_proxy_each_client_ip_has_its_own_limit(api: TestClient) -> None:
    proxied = behind_proxy("testclient")
    for _ in range(10):
        response = proxied.post(LOGIN, json=BODY, headers={"X-Forwarded-For": "203.0.113.1"})
        assert response.status_code == 201
    first = {"X-Forwarded-For": "203.0.113.1"}
    second = {"X-Forwarded-For": "203.0.113.2"}

    assert proxied.post(LOGIN, json=BODY, headers=first).status_code == 429
    assert proxied.post(LOGIN, json=BODY, headers=second).status_code == 201


def test_a_forged_forwarded_for_from_an_untrusted_peer_is_ignored(api: TestClient) -> None:
    direct = behind_proxy("127.0.0.1")
    for n in range(10):
        response = direct.post(LOGIN, json=BODY, headers={"X-Forwarded-For": f"203.0.113.{n}"})
        assert response.status_code == 201

    forged = {"X-Forwarded-For": "203.0.113.99"}
    assert direct.post(LOGIN, json=BODY, headers=forged).status_code == 429


def test_the_limit_resets_after_a_minute() -> None:
    now = [1000.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert [limiter.check("k", 2) for _ in range(2)] == [None, None]
    assert limiter.check("k", 2) == 60.0

    now[0] += 59.0
    assert limiter.check("k", 2) == 1.0
    now[0] += 1.0
    assert limiter.check("k", 2) is None


def test_two_addresses_in_one_ipv6_64_share_a_limit(api: TestClient) -> None:
    """WHO-40 N3: rotating addresses inside one /64 does not buy fresh buckets."""
    proxied = behind_proxy("testclient")
    for n in range(10):
        forwarded = {"X-Forwarded-For": f"2001:db8:1:2::{n + 1:x}"}
        assert proxied.post(LOGIN, json=BODY, headers=forwarded).status_code == 201

    same_64 = {"X-Forwarded-For": "2001:db8:1:2:ffff:ffff:ffff:ffff"}
    next_64 = {"X-Forwarded-For": "2001:db8:1:3::1"}
    assert proxied.post(LOGIN, json=BODY, headers=same_64).status_code == 429
    assert proxied.post(LOGIN, json=BODY, headers=next_64).status_code == 201


@pytest.mark.parametrize(
    ("host", "key"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:DB8:1:2:abcd::9", "2001:db8:1:2::/64"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("unknown", "unknown"),
    ],
)
def test_client_key(host: str, key: str) -> None:
    assert client_key(host) == key


def test_a_full_table_refuses_new_keys_and_keeps_counting_known_ones() -> None:
    """WHO-40 N3: the table never grows past its cap; the overflow gets 429."""
    now = [1000.0]
    limiter = RateLimiter(clock=lambda: now[0], max_keys=3)
    assert [limiter.check(k, 5) for k in ("a", "b", "c")] == [None, None, None]
    now[0] += 10.0

    assert limiter.check("d", 5) == 50.0
    assert limiter.check("a", 5) is None
    assert len(limiter._windows) == 3

    now[0] += 50.0  # the first three windows have ended
    assert limiter.check("d", 5) is None
    assert len(limiter._windows) == 1


def test_the_time_zone_list_is_not_read_per_login(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def walks_the_tzdata_tree() -> set[str]:
        raise AssertionError("available_timezones() called on the request path")

    monkeypatch.setattr(login_module, "available_timezones", walks_the_tzdata_tree)

    assert api.post(LOGIN, json=BODY).status_code == 201
