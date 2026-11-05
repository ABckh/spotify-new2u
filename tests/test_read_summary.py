"""``GET /v1/summary`` against the WHO-13 worked examples W-1, W-2, W-5, W-6, W-7."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from read_helpers import User, add_play, get, make_user

W5_PLAYS: list[dict[str, Any]] = [
    {"at": "2026-04-13T08:00:00Z", "item": "AAA", "title": "Blue Line", "by": ("Nova Kett",)},
    {"at": "2026-04-13T08:05:00Z", "item": "AAA", "title": "Blue Line", "by": ("Nova Kett",)},
    {
        "at": "2026-04-14T19:00:00Z",
        "item": "BBB",
        "title": "Amber Hours",
        "by": ("Nova Kett", "Pine Arc"),
        "ms": 300_000,
    },
    {"at": "2026-04-15T07:30:00Z", "item": "CCC", "title": "Cold Field", "by": ("Pine Arc",)},
    {"at": "2026-04-15T07:40:00Z", "item": "DDD", "title": "Drift", "by": ("Pine Arc",)},
    {
        "at": "2026-04-16T06:00:00Z",
        "item": "EEE",
        "title": "Evening Note",
        "by": ("Solen",),
        "ms": None,
    },
]


def add_w5(engine: Engine, user: User) -> None:
    # Inserted in an order that is neither the expected one nor its reverse.
    for play in (W5_PLAYS[4], W5_PLAYS[2], W5_PLAYS[5], W5_PLAYS[0], W5_PLAYS[3], W5_PLAYS[1]):
        add_play(
            engine,
            user,
            play["at"],
            item=play["item"],
            title=play["title"],
            creators=play["by"],
            duration_ms=play.get("ms", 200_000),
        )


def summary(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return get(api, user, "/v1/summary", **params)


def test_w5_totals_and_both_top_lists_with_their_tie_breaks(
    read_api: TestClient, engine: Engine
) -> None:  # T-11 to T-17
    user = make_user(engine)
    add_w5(engine, user)

    body = summary(read_api, user, period="week", anchor="2026-04-15")

    assert body["period"] == {
        "type": "week",
        "offset": None,
        "anchor": "2026-04-15",
        "time_zone": "Europe/Warsaw",
        "week_start": "monday",
        "start": "2026-04-12T22:00:00.000Z",
        "end": "2026-04-19T22:00:00.000Z",
        "local_start_date": "2026-04-13",
        "local_end_date": "2026-04-19",
        "is_partial": False,
    }
    assert body["totals"] == {
        "play_count": 6,
        "distinct_tracks": 5,
        "distinct_artists": 3,
        "total_ms": 1_100_000,
        "events_without_duration": 1,
    }
    assert [(t["item_key"], t["play_count"], t["total_ms"]) for t in body["top_tracks"]] == [
        ("id:AAA", 2, 400_000),
        ("id:BBB", 1, 300_000),
        ("id:CCC", 1, 200_000),  # ties DDD on plays and ms: "Cold Field" < "Drift"
        ("id:DDD", 1, 200_000),
        ("id:EEE", 1, 0),
    ]
    assert body["top_tracks"][1] == {
        "item_key": "id:BBB",
        "title": "Amber Hours",
        "creators": ["Nova Kett", "Pine Arc"],
        "artwork_url": None,
        "play_count": 1,
        "total_ms": 300_000,
    }
    assert body["top_artists"] == [
        {"name": "Nova Kett", "play_count": 3, "total_ms": 700_000},  # ties Pine Arc: name
        {"name": "Pine Arc", "play_count": 3, "total_ms": 700_000},
        {"name": "Solen", "play_count": 1, "total_ms": 0},
    ]
    assert (body["is_empty"], body["empty_reason"]) == (False, None)
    assert body["coverage_start"] == "2026-04-13T08:00:00.000Z"


def test_w5_the_same_data_gives_the_same_lists_every_time(
    read_api: TestClient, engine: Engine
) -> None:  # T-16
    user = make_user(engine)
    add_w5(engine, user)

    bodies = [summary(read_api, user, period="week", anchor="2026-04-15") for _ in range(5)]

    assert all(body == bodies[0] for body in bodies)


def test_top_limits_both_lists_and_never_pads(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_w5(engine, user)

    two = summary(read_api, user, period="week", anchor="2026-04-15", top="2")
    fifty = summary(read_api, user, period="week", anchor="2026-04-15", top="50")

    assert [t["item_key"] for t in two["top_tracks"]] == ["id:AAA", "id:BBB"]
    assert [a["name"] for a in two["top_artists"]] == ["Nova Kett", "Pine Arc"]
    assert len(fifty["top_tracks"]) == 5
    assert len(fifty["top_artists"]) == 3


@pytest.mark.parametrize(
    "top", ["0", "51", "-1", "five", "1.0", "", "\u0665", "1" * 5000, "0" * 4400 + "5"]
)
def test_a_top_outside_1_to_50_is_refused(read_api: TestClient, engine: Engine, top: str) -> None:
    user = make_user(engine)

    response = read_api.get("/v1/summary", params={"period": "day", "top": top}, headers=user.auth)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_parameter"


def test_w1_start_is_inclusive_and_end_exclusive(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)  # T-01
    add_play(engine, user, "2026-03-09T22:59:30Z", item="E1", duration_ms=210_000)
    add_play(engine, user, "2026-03-09T23:00:00Z", item="E2", duration_ms=180_000)
    add_play(engine, user, "2026-03-10T22:59:59Z", item="E3", duration_ms=240_000)
    add_play(engine, user, "2026-03-10T23:00:00Z", item="E4", duration_ms=195_000)

    body = summary(read_api, user, period="day", anchor="2026-03-10")

    assert body["totals"]["play_count"] == 2
    assert body["totals"]["total_ms"] == 420_000
    assert body["totals"]["distinct_tracks"] == 2
    assert body["totals"]["events_without_duration"] == 0


def test_w2_a_play_crossing_midnight_counts_once_in_full_on_its_day(
    read_api: TestClient, engine: Engine
) -> None:  # T-03, T-05
    user = make_user(engine)
    add_play(engine, user, "2026-03-09T23:00:00Z", item="E2", duration_ms=180_000)
    add_play(engine, user, "2026-03-10T22:59:59Z", item="E3", duration_ms=240_000)
    add_play(engine, user, "2026-03-10T23:00:00Z", item="E4", duration_ms=195_000)

    march_10 = summary(read_api, user, period="day", anchor="2026-03-10")["totals"]
    march_11 = summary(read_api, user, period="day", anchor="2026-03-11")["totals"]
    both = summary(read_api, user, period="custom", **{"from": "2026-03-10", "to": "2026-03-11"})

    assert (march_10["play_count"], march_10["total_ms"]) == (2, 420_000)
    assert (march_11["play_count"], march_11["total_ms"]) == (1, 195_000)
    assert both["totals"]["play_count"] == march_10["play_count"] + march_11["play_count"]
    assert both["totals"]["total_ms"] == march_10["total_ms"] + march_11["total_ms"]


def test_played_ms_wins_over_duration_when_present(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)  # R-25: coalesce(played_ms, duration_ms, 0)
    add_play(engine, user, "2026-10-11T08:00:00Z", item="A", duration_ms=200_000, played_ms=5_000)
    add_play(engine, user, "2026-10-11T09:00:00Z", item="B", duration_ms=None, played_ms=7_000)
    add_play(engine, user, "2026-10-11T10:00:00Z", item="C", duration_ms=100_000)

    totals = summary(read_api, user, period="day")["totals"]

    assert totals["total_ms"] == 5_000 + 7_000 + 100_000
    assert totals["events_without_duration"] == 0


def test_artists_group_after_nfc_and_trimming_only(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)  # R-31
    composed, decomposed = "Björk", "Björk"
    add_play(engine, user, "2026-10-11T08:00:00Z", item="A", creators=(composed,))
    add_play(engine, user, "2026-10-11T08:10:00Z", item="B", creators=(f" {decomposed}\t",))
    add_play(engine, user, "2026-10-11T08:20:00Z", item="C", creators=("björk",))
    # The same artist twice on one play still adds one play to that artist.
    add_play(engine, user, "2026-10-11T08:30:00Z", item="D", creators=(composed, decomposed))

    body = summary(read_api, user, period="day")

    assert body["totals"]["distinct_artists"] == 2  # case is not folded
    assert [(a["name"], a["play_count"]) for a in body["top_artists"]] == [
        (composed, 3),
        ("björk", 1),
    ]


def test_w6_empty_before_coverage(read_api: TestClient, engine: Engine) -> None:  # T-18 to T-20
    user = make_user(engine)
    add_play(engine, user, "2026-03-09T21:00:00Z")

    body = summary(read_api, user, period="day", anchor="2026-01-05")

    assert body["totals"] == {
        "play_count": 0,
        "distinct_tracks": 0,
        "distinct_artists": 0,
        "total_ms": 0,
        "events_without_duration": 0,
    }
    assert (body["top_tracks"], body["top_artists"]) == ([], [])
    assert (body["is_empty"], body["empty_reason"]) == (True, "before_coverage")
    assert body["coverage_start"] == "2026-03-09T21:00:00.000Z"
    assert body["period"]["local_start_date"] == "2026-01-05"


def test_w6_empty_after_coverage_is_no_plays(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-03-09T21:00:00Z")

    body = summary(read_api, user, period="day", anchor="2026-04-14")

    assert (body["is_empty"], body["empty_reason"]) == (True, "no_plays")


def test_the_day_coverage_starts_on_is_not_before_coverage(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)  # period end <= coverage_start, not start
    add_play(engine, user, "2026-03-10T12:00:00Z")

    assert summary(read_api, user, period="day", anchor="2026-03-10")["is_empty"] is False
    body = summary(read_api, user, period="day", anchor="2026-03-09")
    assert body["empty_reason"] == "before_coverage"


def test_w6_a_user_with_no_plays_has_no_data_yet(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    body = summary(read_api, user, period="day", anchor="2026-04-14")

    assert body["coverage_start"] is None
    assert (body["is_empty"], body["empty_reason"]) == (True, "no_data_yet")


def test_w7_a_bad_range_is_422_with_its_code(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    response = read_api.get(
        "/v1/summary",
        params={"period": "custom", "from": "2026-04-19", "to": "2026-04-13"},
        headers=user.auth,
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"code": "period_range_reversed", "message": "to is before from."}
    }


def test_w7_a_bad_stored_zone_is_422(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine, time_zone="Mars/Olympus")

    response = read_api.get("/v1/summary", params={"period": "day"}, headers=user.auth)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "period_invalid_time_zone"


def test_client_clock_and_zone_parameters_are_ignored(
    read_api: TestClient, engine: Engine
) -> None:  # R-8, T-26
    user = make_user(engine)

    plain = summary(read_api, user, period="day")
    with_client_values = summary(
        read_api, user, period="day", time_zone="Pacific/Kiritimati", now="2030-01-01T00:00:00Z"
    )

    assert with_client_values == plain
    assert plain["period"]["local_start_date"] == "2026-10-11"


def test_a_repoll_leaves_every_total_unchanged(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)  # R-30, T-25
    add_w5(engine, user)
    before = summary(read_api, user, period="week", anchor="2026-04-15")

    add_w5(engine, user)

    assert summary(read_api, user, period="week", anchor="2026-04-15") == before
