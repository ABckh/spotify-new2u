"""``GET /v1/config``, the per-user read limits, and the access log of the read routes."""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import get_settings
from read_helpers import add_play, get, make_user

SYNTHETIC_TITLE = "Synthetic Logged Title 6a2f"


def test_config_states_the_poll_interval_the_poller_uses(
    read_api: TestClient, engine: Engine
) -> None:  # S1.2a
    user = make_user(engine)

    assert get(read_api, user, "/v1/config") == {
        "poll_interval_seconds": get_settings().poller_interval_seconds
    }


@pytest.mark.parametrize(
    ("route", "setting"),
    [
        ("/v1/timeline", "read_rate_limit_per_minute"),
        ("/v1/summary", "read_rate_limit_per_minute"),
        ("/v1/new-to-you", "read_rate_limit_per_minute"),
        ("/v1/new-to-you/albums/ALB1/tracks", "read_rate_limit_per_minute"),
        ("/v1/albums", "read_rate_limit_per_minute"),
        ("/v1/albums/ALB1/tracks", "read_rate_limit_per_minute"),
        ("/v1/export", "export_rate_limit_per_minute"),
    ],
)
def test_reads_are_limited_per_user(
    read_api: TestClient, engine: Engine, route: str, setting: str
) -> None:
    busy, other = make_user(engine), make_user(engine)
    limit: int = getattr(get_settings(), setting)

    for _ in range(limit):
        assert read_api.get(route, params={"period": "day"}, headers=busy.auth).status_code == 200
    over = read_api.get(route, params={"period": "day"}, headers=busy.auth)

    assert over.status_code == 429
    assert 0 < int(over.headers["Retry-After"]) <= 60
    assert read_api.get(route, params={"period": "day"}, headers=other.auth).status_code == 200


def test_the_access_log_has_route_template_status_and_user_but_no_query_or_body(
    read_api: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:  # PRD P12
    caplog.set_level(logging.DEBUG)
    user = make_user(engine)
    add_play(engine, user, "2026-10-11T08:00:00Z", title=SYNTHETIC_TITLE)

    read_api.get(
        "/v1/timeline",
        params={"period": "custom", "from": "2026-10-01", "to": "2026-10-11"},
        headers=user.auth,
    )
    read_api.get("/v1/export", headers=user.auth)
    read_api.get("/v1/summary", params={"period": "day"})  # no session

    lines = [r for r in caplog.records if r.name == "app.access"]
    assert [r.getMessage().rsplit(" duration_ms=", 1)[0] for r in lines] == [
        f"request method=GET route=/v1/timeline status=200 user_id={user.id} client=testclient",
        f"request method=GET route=/v1/export status=200 user_id={user.id} client=testclient",
        "request method=GET route=/v1/summary status=401 user_id=- client=testclient",
    ]
    assert all(r.created for r in lines)  # the timestamp is the record's own
    assert SYNTHETIC_TITLE not in caplog.text
    assert "2026-10-01" not in caplog.text
    assert "period=" not in caplog.text
    assert user.token not in caplog.text


def test_an_unmatched_path_is_not_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    client.get("/v1/Synthetic%20Track%20In%20A%20Path")

    (line,) = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert line.startswith("request method=GET route=- status=404 user_id=- ")
    assert "Synthetic" not in caplog.text
