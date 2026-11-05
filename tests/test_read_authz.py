"""No route returns or deletes another user's data, through any parameter.

The user id only comes from the session. These tests give the attacker every
parameter the routes read, and a few they do not, filled with the victim's
values, and check that nothing of the victim's comes back.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.read.timeline import Cursor
from read_helpers import NOW, User, add_play, get, make_user

VICTIM_TITLE = "Victim Synthetic Title 5b7c"
VICTIM_ARTIST = "Victim Synthetic Artist 2d9e"

READ_ROUTES = [
    "/v1/timeline",
    "/v1/summary",
    "/v1/new-to-you",
    "/v1/new-to-you/albums/ALB1/tracks",
    "/v1/albums",
    "/v1/albums/ALB1/tracks",
    "/v1/export",
    "/v1/config",
]
MARK_ROUTE = "/v1/new-to-you/not-new/album/ALB1"
ALL_ROUTES = [("GET", route) for route in READ_ROUTES] + [
    ("DELETE", "/v1/me"),
    ("PUT", MARK_ROUTE),
    ("DELETE", MARK_ROUTE),
]


def two_users(engine: Engine) -> tuple[User, User]:
    attacker, victim = make_user(engine), make_user(engine)
    add_play(engine, attacker, "2026-10-11T08:00:00Z", item="mine", title="Attacker Title")
    for minute in range(3):
        add_play(
            engine,
            victim,
            f"2026-10-11T08:0{minute}:30Z",
            item=f"victim-{minute}",
            title=VICTIM_TITLE,
            creators=(VICTIM_ARTIST,),
            collection=VICTIM_TITLE,
            external_ids={"spotify_album_id": f"victim-album-{minute}"},
        )
    return attacker, victim


def victim_cursor(api: TestClient, victim: User) -> str:
    cursor: str = get(api, victim, "/v1/timeline", period="day", limit="1")["next_cursor"]
    return cursor


def assert_nothing_of(victim: User, body: str) -> None:
    assert VICTIM_TITLE not in body
    assert VICTIM_ARTIST not in body
    assert "victim-" not in body  # item keys and album ids
    assert str(victim.id) not in body


def test_timeline_and_summary_never_return_another_users_plays(
    read_api: TestClient, engine: Engine
) -> None:
    attacker, victim = two_users(engine)
    stolen_cursor = victim_cursor(read_api, victim)
    # Every parameter the routes read, plus user-ish ones they must ignore.
    hostile = {
        "user_id": str(victim.id),
        "user": str(victim.id),
        "owner": str(victim.id),
        "uid": str(victim.id),
    }
    requests = [
        ("/v1/timeline", {"period": "day"}),
        ("/v1/timeline", {"period": "day", "cursor": stolen_cursor}),
        ("/v1/timeline", {"period": "custom", "from": "2026-01-01", "to": "2026-12-31"}),
        ("/v1/summary", {"period": "day", "top": "50"}),
        ("/v1/summary", {"period": "month", "offset": "0"}),
        ("/v1/new-to-you", {"period": "day"}),
        ("/v1/new-to-you", {"period": "month", "all": "true"}),
        ("/v1/albums", {"period": "day"}),
        ("/v1/albums", {"period": "month", "all": "true"}),
    ]

    for route, params in requests:
        response = read_api.get(
            route,
            params={**params, **hostile},
            headers={**attacker.auth, "X-User-Id": str(victim.id)},
        )
        assert response.status_code == 200, (route, response.text)
        assert_nothing_of(victim, response.text)

    mine = get(read_api, attacker, "/v1/timeline", period="day")
    assert [e["item_key"] for e in mine["events"]] == ["id:mine"]
    totals = get(read_api, attacker, "/v1/summary", period="day")["totals"]
    assert totals["play_count"] == 1
    new = get(read_api, attacker, "/v1/new-to-you", period="day", all="true")
    assert [t["item_key"] for t in new["tracks"]["items"]] == ["id:mine"]
    assert new["albums"]["count"] == 0
    assert new["singles"] == {"count": 0, "items": []}
    assert get(read_api, attacker, "/v1/albums", period="day", all="true")["albums"] == {
        "count": 0,
        "items": [],
    }


def test_a_forged_cursor_with_the_victims_keys_reads_only_the_callers_rows(
    read_api: TestClient, engine: Engine
) -> None:
    attacker, victim = two_users(engine)
    stolen = Cursor.decode(victim_cursor(read_api, victim))
    # Rewind it to "after the end of time" so it would match every victim row.
    forged = Cursor(
        stolen.period_start,
        stolen.period_end,
        datetime(2100, 1, 1, tzinfo=UTC),
        stolen.item_key,
        stolen.id,
    ).encode()

    response = read_api.get(
        "/v1/timeline", params={"period": "day", "cursor": forged}, headers=attacker.auth
    )

    assert response.status_code == 200
    assert [e["item_key"] for e in response.json()["events"]] == ["id:mine"]
    assert_nothing_of(victim, response.text)


def test_export_holds_only_the_callers_rows(read_api: TestClient, engine: Engine) -> None:
    attacker, victim = two_users(engine)

    response = read_api.get("/v1/export", params={"user_id": str(victim.id)}, headers=attacker.auth)

    assert response.status_code == 200
    document = json.loads(response.text)
    assert document["user_id"] == str(attacker.id)
    assert document["account"]["id"] == str(attacker.id)
    assert {e["user_id"] for e in document["events"]} == {str(attacker.id)}
    assert {s["user_id"] for s in document["sessions"]} == {str(attacker.id)}
    assert_nothing_of(victim, response.text)


def test_delete_me_deletes_only_the_caller(read_api: TestClient, engine: Engine) -> None:
    attacker, victim = two_users(engine)

    response = read_api.request(
        "DELETE", "/v1/me", params={"user_id": str(victim.id)}, headers=attacker.auth
    )

    assert response.status_code == 204
    after = get(read_api, victim, "/v1/summary", period="day")
    assert after["totals"]["play_count"] == 3
    with engine.connect() as conn:
        users = conn.execute(text("SELECT id FROM app_user")).scalars().all()
    assert users == [victim.id]


@pytest.mark.parametrize(("method", "route"), ALL_ROUTES)
@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer ", "Bearer forged-token", "Basic dXNlcjpwYXNz", "bearer lowercase"],
)
def test_every_route_denies_without_a_valid_session(
    read_api: TestClient, engine: Engine, method: str, route: str, authorization: str | None
) -> None:
    two_users(engine)
    headers = {} if authorization is None else {"Authorization": authorization}

    response = read_api.request(method, route, params={"period": "day"}, headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar_one() == 2


@pytest.mark.parametrize(("method", "route"), ALL_ROUTES)
def test_an_expired_session_is_denied(
    read_api: TestClient, engine: Engine, method: str, route: str
) -> None:
    user = make_user(engine)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE auth_session SET expires_at = :t WHERE user_id = :u"),
            {"t": NOW - timedelta(seconds=1), "u": user.id},
        )

    response = read_api.request(method, route, params={"period": "day"}, headers=user.auth)

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "route"), ALL_ROUTES)
def test_after_delete_the_old_session_is_denied_and_no_account_comes_back(
    read_api: TestClient, engine: Engine, method: str, route: str
) -> None:  # WHO-25 R4
    user = make_user(engine)
    add_play(engine, user, "2026-10-11T08:00:00Z")
    assert read_api.delete("/v1/me", headers=user.auth).status_code == 204

    response = read_api.request(method, route, params={"period": "day"}, headers=user.auth)

    assert response.status_code == 401
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar_one() == 0


@pytest.mark.parametrize(("method", "route"), ALL_ROUTES)
def test_a_session_whose_account_row_is_gone_is_denied(
    read_api: TestClient, engine: Engine, method: str, route: str
) -> None:
    """Even if a session row outlived its account, it would authenticate nobody."""
    user = make_user(engine)
    with engine.begin() as conn:
        # Replica mode skips the foreign key triggers, so the cascade does not run.
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM app_user WHERE id = :u"), {"u": user.id})
    with engine.connect() as conn:
        orphans = "SELECT count(*) FROM auth_session WHERE user_id = :u"
        assert conn.execute(text(orphans), {"u": user.id}).scalar_one() == 1
    try:
        response = read_api.request(method, route, params={"period": "day"}, headers=user.auth)
        assert response.status_code == 401
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM auth_session WHERE user_id = :u"), {"u": user.id})
