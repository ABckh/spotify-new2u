"""``GET /v1/new-to-you/albums/{album_id}/tracks``: one album's tracks played in a period (WHO-116).

The read clock is ``read_helpers.NOW`` (2026-10-11 12:00 UTC); the default
display zone is Europe/Warsaw, so October 2026 runs from 2026-09-30T22:00Z
(UTC+2) to 2026-10-31T23:00Z (UTC+1 after 25 October).
"""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from read_helpers import User, add_play, get, make_user


def album_tracks(api: TestClient, user: User, album: str, **params: str) -> dict[str, Any]:
    return get(api, user, f"/v1/new-to-you/albums/{album}/tracks", **params)


def october(api: TestClient, user: User, album: str) -> dict[str, Any]:
    return album_tracks(api, user, album, period="month", anchor="2026-10-01")


def play(
    engine: Engine,
    user: User,
    at: str,
    item: str,
    *,
    album: str | None = "ALB1",
    title: str | None = None,
    by: tuple[str, ...] = ("Nova Kett",),
) -> None:
    """One play of track ``item`` on Spotify album ``album`` (named ``Album <id>``)."""
    add_play(
        engine,
        user,
        at,
        item=item,
        title=title,
        creators=by,
        collection=None if album is None else f"Album {album}",
        external_ids=None if album is None else {"spotify_album_id": album},
    )


def rows(body: dict[str, Any]) -> list[tuple[str, int, bool]]:
    return [(t["item_key"], t["play_count"], t["is_new"]) for t in body["tracks"]]


def test_the_full_shape(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "AAA")
    play(engine, user, "2026-10-06T18:00:00Z", "AAA")

    body = october(read_api, user, "ALB1")

    assert body["period"]["type"] == "month"
    assert body["period"]["start"] == "2026-09-30T22:00:00.000Z"
    assert {k: v for k, v in body.items() if k != "period"} == {
        "album": {
            "album_id": "ALB1",
            "name": "Album ALB1",
            "artists": ["Nova Kett"],
            "artwork_url": None,
            "rating": None,
            "comment": None,
        },
        "tracks": [
            {
                "item_key": "id:AAA",
                "title": "Track AAA",
                "creators": ["Nova Kett"],
                "artwork_url": None,
                "play_count": 2,
                "first_played_at": "2026-10-05T18:00:00.000Z",
                "first_played_local_date": "2026-10-05",
                "is_new": True,
                "comment": None,
            }
        ],
    }


def test_only_this_albums_tracks_and_only_plays_in_the_period_count(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "AAA")
    play(engine, user, "2026-09-05T18:00:00Z", "AAA")  # before October: not counted
    play(engine, user, "2026-11-05T18:00:00Z", "AAA")  # after October: not counted
    play(engine, user, "2026-09-05T18:00:00Z", "OLD")  # this album, only before October
    play(engine, user, "2026-10-05T18:00:00Z", "OTH", album="ALB2")
    play(engine, user, "2026-10-05T18:00:00Z", "NONE", album=None)

    body = october(read_api, user, "ALB1")

    assert rows(body) == [("id:AAA", 1, False)]


def test_period_edges_start_is_inside_end_is_outside(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-30T21:59:59Z", "BEFORE")  # 23:59:59 on 30 September in Warsaw
    play(engine, user, "2026-09-30T22:00:00Z", "START")  # 00:00 on 1 October
    play(engine, user, "2026-10-31T22:59:59Z", "LAST")  # 23:59:59 on 31 October, UTC+1
    play(engine, user, "2026-10-31T23:00:00Z", "END")  # 00:00 on 1 November

    body = october(read_api, user, "ALB1")

    assert sorted(t["item_key"] for t in body["tracks"]) == ["id:LAST", "id:START"]
    dates = {t["item_key"]: t["first_played_local_date"] for t in body["tracks"]}
    # A UTC date here would be 2026-09-30: the local date is the display zone's.
    assert dates == {"id:START": "2026-10-01", "id:LAST": "2026-10-31"}


def test_is_new_uses_the_first_play_over_the_whole_history_and_every_album(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "NEW")
    play(engine, user, "2026-10-05T18:00:00Z", "KNOWN")
    play(engine, user, "2026-08-01T18:00:00Z", "KNOWN")
    # The same track first stored without an album ID (or on another release).
    play(engine, user, "2026-10-06T18:00:00Z", "ELSEWHERE")
    play(engine, user, "2026-07-01T18:00:00Z", "ELSEWHERE", album=None)

    body = october(read_api, user, "ALB1")
    by_key = {t["item_key"]: t for t in body["tracks"]}

    assert {k: t["is_new"] for k, t in by_key.items()} == {
        "id:NEW": True,
        "id:KNOWN": False,
        "id:ELSEWHERE": False,
    }
    assert by_key["id:KNOWN"]["first_played_local_date"] == "2026-08-01"
    assert by_key["id:ELSEWHERE"]["first_played_at"] == "2026-07-01T18:00:00.000Z"
    # The same rule as the "New to you" track list.
    new = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")
    assert [t["item_key"] for t in new["tracks"]["items"]] == ["id:NEW"]


def test_order_is_play_count_then_title_by_code_point_then_item_key(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for minute, (item, title, plays) in enumerate(
        [
            ("T1", "alpha", 1),
            ("T2", "Zulu", 1),  # "Z" sorts before "a" by code point
            ("T3", "  Zulu", 1),  # same title after trimming: item_key decides
            ("T4", "beta", 3),
            ("T5", "Mid", 2),
        ]
    ):
        for n in range(plays):
            play(engine, user, f"2026-10-0{n + 1}T18:{minute:02d}:00Z", item, title=title)

    body = october(read_api, user, "ALB1")

    assert [t["item_key"] for t in body["tracks"]] == [
        "id:T4",
        "id:T5",
        "id:T2",
        "id:T3",
        "id:T1",
    ]


def test_r59_names_are_those_of_the_most_recent_stored_play(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(
        engine,
        user,
        "2026-10-05T18:00:00Z",
        item="AAA",
        title="Old Title",
        creators=("Old Artist",),
        collection="Old Album",
        external_ids={"spotify_album_id": "ALB1"},
    )
    add_play(
        engine,
        user,
        "2026-10-20T18:00:00Z",
        item="AAA",
        title="New Title",
        creators=("New Artist",),
        collection="New Album",
        external_ids={"spotify_album_id": "ALB1"},
    )

    body = october(read_api, user, "ALB1")

    assert body["album"] == {
        "album_id": "ALB1",
        "name": "New Album",
        "artists": ["New Artist"],
        "artwork_url": None,
        "rating": None,
        "comment": None,
    }
    assert [(t["title"], t["creators"]) for t in body["tracks"]] == [("New Title", ["New Artist"])]


def test_an_unknown_album_is_an_empty_list(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "AAA")

    body = october(read_api, user, "NOPE")

    assert body["album"] == {
        "album_id": "NOPE",
        "name": None,
        "artists": [],
        "artwork_url": None,
        "rating": None,
        "comment": None,
    }
    assert body["tracks"] == []


def test_an_album_played_only_outside_the_period_is_an_empty_list(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-05T18:00:00Z", "AAA")

    body = october(read_api, user, "ALB1")

    assert body["album"]["name"] == "Album ALB1"
    assert body["tracks"] == []


def test_another_users_album_reads_as_unknown(read_api: TestClient, engine: Engine) -> None:
    me, other = make_user(engine), make_user(engine)
    play(engine, me, "2026-10-05T18:00:00Z", "MINE", album="SHARED")
    play(engine, other, "2026-10-05T18:00:00Z", "THEIRS", album="SHARED")
    play(engine, other, "2026-10-06T18:00:00Z", "THEIRS", album="ONLYTHEIRS")

    shared = october(read_api, me, "SHARED")
    theirs = october(read_api, me, "ONLYTHEIRS")

    assert rows(shared) == [("id:MINE", 1, True)]
    assert theirs["album"] == {
        "album_id": "ONLYTHEIRS",
        "name": None,
        "artists": [],
        "artwork_url": None,
        "rating": None,
        "comment": None,
    }
    assert theirs["tracks"] == []


def test_a_repoll_of_the_same_window_changes_nothing(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "AAA")
    before = october(read_api, user, "ALB1")

    play(engine, user, "2026-10-05T18:00:00Z", "AAA")  # the same play again: deduplicated

    assert october(read_api, user, "ALB1") == before
    assert rows(before) == [("id:AAA", 1, True)]


def test_an_album_id_that_is_not_a_spotify_id_is_refused(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)

    for bad in ("a-b", "a%20b", "x" * 65, "%00"):
        response = read_api.get(
            f"/v1/new-to-you/albums/{bad}/tracks", params={"period": "day"}, headers=user.auth
        )
        assert response.status_code == 422, bad
        assert response.json()["detail"]["code"] == "invalid_parameter"


def test_a_bad_period_is_422_with_the_resolver_code(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    response = read_api.get(
        "/v1/new-to-you/albums/ALB1/tracks",
        params={"period": "custom", "from": "2026-10-05", "to": "2026-10-01"},
        headers=user.auth,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "period_range_reversed"
