"""``GET /v1/timeline``: range edges, paging, the empty and single-event cases."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.read.timeline import Cursor
from read_helpers import User, add_play, get, make_user


def timeline(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return get(api, user, "/v1/timeline", **params)


def all_pages(api: TestClient, user: User, **params: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(100):
        page = timeline(api, user, **params, **({"cursor": cursor} if cursor else {}))
        events += page["events"]
        cursor = page["next_cursor"]
        if cursor is None:
            return events
    raise AssertionError("paging did not end")


def test_w1_edges_newest_first(read_api: TestClient, engine: Engine) -> None:  # T-01
    user = make_user(engine)
    add_play(engine, user, "2026-03-09T22:59:30Z", item="E1")
    add_play(engine, user, "2026-03-09T23:00:00Z", item="E2")
    add_play(engine, user, "2026-03-10T22:59:59Z", item="E3")
    add_play(engine, user, "2026-03-10T23:00:00Z", item="E4")

    body = timeline(read_api, user, period="day", anchor="2026-03-10")

    assert [e["item_key"] for e in body["events"]] == ["id:E3", "id:E2"]
    assert body["next_cursor"] is None
    assert (body["is_empty"], body["empty_reason"]) == (False, None)
    assert body["period"]["start"] == "2026-03-09T23:00:00.000Z"


def test_an_event_carries_its_fields_and_its_local_date(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(
        engine,
        user,
        "2026-03-10T22:59:59.123456Z",
        item="E3",
        title="Synthetic Title",
        creators=("Nova Kett", "Pine Arc"),
        duration_ms=240_000,
        collection="Synthetic Album",
    )

    (event,) = timeline(read_api, user, period="day", anchor="2026-03-10")["events"]

    assert set(event) == {
        "id",
        "source",
        "kind",
        "occurred_at",
        "occurred_at_precision",
        "local_date",
        "duration_ms",
        "played_ms",
        "title",
        "creators",
        "collection",
        "artwork_url",
        "item_key",
        "comment",
    }
    assert event["comment"] is None
    assert event["occurred_at"] == "2026-03-10T22:59:59.123Z"
    assert event["local_date"] == "2026-03-10"  # 23:59:59 in Warsaw
    assert event["occurred_at_precision"] == "second"
    assert (event["source"], event["kind"]) == ("spotify", "listen")
    assert (event["duration_ms"], event["played_ms"]) == (240_000, None)
    assert event["creators"] == ["Nova Kett", "Pine Arc"]
    assert event["collection"] == "Synthetic Album"


def test_a_day_header_holds_exactly_what_that_days_summary_counts(
    read_api: TestClient, engine: Engine
) -> None:  # R-48, T-04
    user = make_user(engine)
    for at in ("2026-03-09T22:59:30Z", "2026-03-09T23:00:00Z", "2026-03-10T22:59:59Z"):
        add_play(engine, user, at, item=at)
    add_play(engine, user, "2026-03-10T23:00:00Z", item="E4")

    week = all_pages(read_api, user, period="week", anchor="2026-03-10")
    for day in ("2026-03-09", "2026-03-10", "2026-03-11"):
        in_header = [e for e in week if e["local_date"] == day]
        counted = get(read_api, user, "/v1/summary", period="day", anchor=day)
        assert len(in_header) == counted["totals"]["play_count"], day


def test_paging_across_a_page_boundary_inside_a_tie(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    # Five plays at one instant (only the item tells them apart), and two around them.
    # "D" sorts before "a" by code point but after it in a language collation, so
    # ORDER BY and the cursor predicate must both use code point order.
    add_play(engine, user, "2026-10-11T09:00:00Z", item="newest")
    for item in ("e", "c", "a", "D", "b"):
        add_play(engine, user, "2026-10-11T08:00:00Z", item=item)
    add_play(engine, user, "2026-10-11T07:00:00Z", item="oldest")

    for limit in ("1", "2", "3", "7", "50"):
        keys = [e["item_key"] for e in all_pages(read_api, user, period="day", limit=limit)]
        assert keys == ["id:newest", "id:D", "id:a", "id:b", "id:c", "id:e", "id:oldest"], limit


def test_the_last_full_page_has_no_next_cursor(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    for minute in range(4):
        add_play(engine, user, f"2026-10-11T08:0{minute}:00Z", item=str(minute))

    first = timeline(read_api, user, period="day", limit="2")
    second = timeline(read_api, user, period="day", limit="2", cursor=first["next_cursor"])

    assert first["next_cursor"] is not None
    assert len(second["events"]) == 2
    assert second["next_cursor"] is None
    assert (second["is_empty"], second["empty_reason"]) == (False, None)


def test_a_play_stored_while_paging_shows_up_once_and_nothing_repeats(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for minute in range(4):
        add_play(engine, user, f"2026-10-11T08:0{minute}:00Z", item=str(minute))

    first = timeline(read_api, user, period="day", limit="2")
    add_play(engine, user, "2026-10-11T09:00:00Z", item="new")  # the poller, between pages
    rest = timeline(read_api, user, period="day", limit="50", cursor=first["next_cursor"])

    keys = [e["item_key"] for e in first["events"] + rest["events"]]
    assert keys == ["id:3", "id:2", "id:1", "id:0"]


def test_an_empty_range(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-03-09T21:00:00Z")

    body = timeline(read_api, user, period="day", anchor="2026-04-14")

    assert body["events"] == []
    assert body["next_cursor"] is None
    assert (body["is_empty"], body["empty_reason"]) == (True, "no_plays")
    assert body["coverage_start"] == "2026-03-09T21:00:00.000Z"


def test_a_single_event_range(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-14T10:00:00Z", item="only")
    add_play(engine, user, "2026-04-15T10:00:00Z", item="next-day")

    body = timeline(read_api, user, period="custom", **{"from": "2026-04-14", "to": "2026-04-14"})

    assert [e["item_key"] for e in body["events"]] == ["id:only"]
    assert body["next_cursor"] is None


def test_no_data_yet(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    body = timeline(read_api, user, period="week")

    assert (body["is_empty"], body["empty_reason"], body["coverage_start"]) == (
        True,
        "no_data_yet",
        None,
    )


def test_a_cursor_from_another_period_is_refused(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    for minute in range(3):
        add_play(engine, user, f"2026-10-11T08:0{minute}:00Z", item=str(minute))
    cursor = timeline(read_api, user, period="day", limit="1")["next_cursor"]

    response = read_api.get(
        "/v1/timeline", params={"period": "week", "cursor": cursor}, headers=user.auth
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_cursor"


@pytest.mark.parametrize("cursor", ["", "not-a-cursor", "eyJ9", "e30", "!!!!"])
def test_an_unreadable_cursor_is_refused(read_api: TestClient, engine: Engine, cursor: str) -> None:
    user = make_user(engine)

    response = read_api.get(
        "/v1/timeline", params={"period": "day", "cursor": cursor}, headers=user.auth
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_cursor"


@pytest.mark.parametrize(
    "limit", ["0", "201", "-5", "ten", "", "\u0661", "1" * 5000, "0" * 4400 + "5"]
)
def test_a_limit_outside_1_to_200_is_refused(
    read_api: TestClient, engine: Engine, limit: str
) -> None:
    user = make_user(engine)

    response = read_api.get(
        "/v1/timeline", params={"period": "day", "limit": limit}, headers=user.auth
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_parameter"


def test_w4_changing_the_stored_zone_moves_buckets_and_travel_does_not(
    read_api: TestClient, engine: Engine
) -> None:  # T-09, T-10
    user = make_user(engine)
    add_play(engine, user, "2026-06-01T23:30:00Z", item="E5", duration_ms=180_000)

    def day_keys(day: str) -> list[str]:
        events = timeline(read_api, user, period="day", anchor=day)["events"]
        return [e["item_key"] for e in events]

    assert (day_keys("2026-06-02"), day_keys("2026-06-01")) == (["id:E5"], [])
    # The device zone is never an input, so "travel" is a request that says so.
    response = read_api.get(
        "/v1/timeline",
        params={"period": "day", "anchor": "2026-06-01", "time_zone": "America/New_York"},
        headers={**user.auth, "X-Time-Zone": "America/New_York"},
    )
    assert response.json()["events"] == []

    with engine.begin() as conn:  # the one way a bucket moves: the stored zone changes
        conn.execute(
            text("UPDATE app_user SET home_timezone = 'America/New_York' WHERE id = :u"),
            {"u": user.id},
        )

    assert (day_keys("2026-06-02"), day_keys("2026-06-01")) == ([], ["id:E5"])
    body = timeline(read_api, user, period="day", anchor="2026-06-02")
    assert body["period"]["start"] == "2026-06-02T04:00:00.000Z"


def test_the_cursor_is_opaque_but_names_no_user(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-10-11T08:00:00Z", item="a")
    add_play(engine, user, "2026-10-11T07:00:00Z", item="b")

    cursor = Cursor.decode(timeline(read_api, user, period="day", limit="1")["next_cursor"])

    assert str(user.id) not in repr(cursor)
    assert cursor.item_key == "id:a"
