"""No cache may keep a /v1 answer, success or error (WHO-84, WHO-83 F3).

The bodies hold track titles, artists and play times. The iOS client turns
its own cache off, but only the server protects a browser on /docs, a future
client, or a proxy in between.
"""

import pytest
from fastapi.testclient import TestClient
from httpx import Headers
from sqlalchemy import Engine

from read_helpers import add_play, make_user

# Every GET under /v1 that needs a session, the parameters it needs, and what it
# answers a signed-in user. The read helpers' user has no Spotify connection row,
# so the connection status is a 404: an error answer, which must not be kept either.
SESSION_ROUTES = [
    ("/v1/timeline", {"period": "day"}, 200),
    ("/v1/summary", {"period": "day"}, 200),
    ("/v1/new-to-you", {"period": "day"}, 200),
    ("/v1/new-to-you/albums/ALB1/tracks", {"period": "day"}, 200),
    ("/v1/albums", {"period": "day"}, 200),
    ("/v1/albums/ALB1/tracks", {"period": "day"}, 200),
    ("/v1/export", {}, 200),
    ("/v1/config", {}, 200),
    ("/v1/me/connection", {}, 404),
]


def assert_no_store(headers: Headers) -> None:
    assert headers["Cache-Control"] == "no-store"
    assert headers["Pragma"] == "no-cache"


@pytest.mark.parametrize(("route", "params", "expected"), SESSION_ROUTES)
def test_a_read_answer_is_never_stored(
    read_api: TestClient, engine: Engine, route: str, params: dict[str, str], expected: int
) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-10-11T08:00:00Z", item="track-1", title="Synthetic Title")

    response = read_api.get(route, params=params, headers=user.auth)

    assert response.status_code == expected, response.text
    assert_no_store(response.headers)


@pytest.mark.parametrize(("route", "params", "expected"), SESSION_ROUTES)
def test_a_401_from_a_read_route_is_never_stored(
    read_api: TestClient, route: str, params: dict[str, str], expected: int
) -> None:
    response = read_api.get(route, params=params)

    assert response.status_code == 401
    assert_no_store(response.headers)


def test_a_422_from_a_read_route_is_never_stored(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    response = read_api.get("/v1/timeline", params={"period": "fortnight"}, headers=user.auth)

    assert response.status_code == 422
    assert_no_store(response.headers)


def test_the_public_stored_fields_list_is_not_stored_either(read_api: TestClient) -> None:
    response = read_api.get("/v1/privacy/stored-fields")

    assert response.status_code == 200
    assert_no_store(response.headers)


def test_the_health_check_is_left_alone(read_api: TestClient) -> None:
    response = read_api.get("/healthz")

    assert "Cache-Control" not in response.headers
