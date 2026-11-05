"""``GET /v1/albums``: every album listened in a period, new and old (WHO-133, WHO-160).

The read clock is ``read_helpers.NOW`` (2026-10-11 12:00 UTC); the default
display zone is Europe/Warsaw, so October 2026 runs from 2026-09-30T22:00Z
(UTC+2) to 2026-10-31T23:00Z (UTC+1 after 25 October).
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from read_helpers import User, add_play, album_extras, get, make_user


def play(
    engine: Engine,
    user: User,
    at: str,
    item: str,
    *,
    album: str | None = "ALB1",
    name: str | None = None,
    by: tuple[str, ...] = ("Nova Kett",),
    duration_ms: int | None = 200_000,
    total_tracks: int | None = 1,
) -> None:
    """One play of track ``item`` on Spotify album ``album`` (named ``Album <id>``)."""
    add_play(
        engine,
        user,
        at,
        item=item,
        creators=by,
        duration_ms=duration_ms,
        collection=None if album is None else (name or f"Album {album}"),
        external_ids=None if album is None else {"spotify_album_id": album},
        provider_extras=None if album is None else album_extras(total_tracks),
    )


def october(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return get(api, user, "/v1/albums", period="month", anchor="2026-10-01", **params)


def rows(body: dict[str, Any]) -> list[tuple[str, int, bool]]:
    return [(a["album_id"], a["play_count"], a["is_new"]) for a in body["albums"]["items"]]


def test_the_full_shape_with_an_old_and_a_new_album(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    # Both of its 2 tracks played in August (so not new), and again in October.
    play(engine, user, "2026-08-01T18:00:00Z", "OLD1", album="OLD", total_tracks=2)
    play(engine, user, "2026-08-01T18:05:00Z", "OLD2", album="OLD", total_tracks=2)
    play(engine, user, "2026-10-05T18:00:00Z", "OLD1", album="OLD", total_tracks=2)
    play(
        engine, user, "2026-10-06T18:00:00Z", "OLD2", album="OLD", duration_ms=None, total_tracks=2
    )
    play(engine, user, "2026-10-07T18:00:00Z", "OLD1", album="OLD", total_tracks=2)
    play(engine, user, "2026-10-08T18:00:00Z", "NEW1", album="NEW", by=("Ilo Mar",))

    body = october(read_api, user)

    assert body["period"]["start"] == "2026-09-30T22:00:00.000Z"
    assert {k: v for k, v in body.items() if k != "period"} == {
        "albums": {
            "count": 2,
            "items": [
                {
                    "album_id": "OLD",
                    "name": "Album OLD",
                    "artists": ["Nova Kett"],
                    "artwork_url": None,
                    "rating": None,
                    "comment": None,
                    "play_count": 3,  # the August play is outside the period
                    "tracks_played_in_period": 2,
                    "total_tracks": 2,
                    "total_ms": 400_000,  # a play with no duration adds 0
                    "is_new": False,
                    "release_type": "album",
                },
                {
                    "album_id": "NEW",
                    "name": "Album NEW",
                    "artists": ["Ilo Mar"],
                    "artwork_url": None,
                    "rating": None,
                    "comment": None,
                    "play_count": 1,
                    "tracks_played_in_period": 1,
                    "total_tracks": 1,
                    "total_ms": 200_000,
                    "is_new": True,
                    "release_type": "album",
                },
            ],
        },
        "is_empty": False,
        "empty_reason": None,
        "coverage_start": "2026-08-01T18:00:00.000Z",
        "collection_gap_overlap": False,
    }


def test_an_album_played_only_outside_the_period_is_absent(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-30T21:59:59Z", "T", album="BEFORE")  # 23:59:59 on 30 Sep, Warsaw
    play(engine, user, "2026-09-30T22:00:00Z", "T", album="START")  # 00:00 on 1 October
    play(engine, user, "2026-10-31T22:59:59Z", "T", album="LAST")  # 23:59:59 on 31 Oct, UTC+1
    play(engine, user, "2026-10-31T23:00:00Z", "T", album="END")  # 00:00 on 1 November
    play(engine, user, "2026-10-05T18:00:00Z", "NONE", album=None)  # no album ID: no album

    body = october(read_api, user)

    assert sorted(a["album_id"] for a in body["albums"]["items"]) == ["LAST", "START"]
    assert body["albums"]["count"] == 2


def test_is_new_is_the_new_to_you_rule(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "A", album="FRESH")
    play(engine, user, "2026-10-05T18:00:00Z", "B", album="KNOWN")
    play(engine, user, "2026-09-05T18:00:00Z", "C", album="KNOWN")  # an earlier, other track
    play(engine, user, "2026-10-05T18:00:00Z", "D", album="MARKED")
    marked = read_api.put("/v1/new-to-you/not-new/album/MARKED", headers=user.auth)
    assert marked.status_code == 204, marked.text

    body = october(read_api, user)

    assert {a["album_id"]: a["is_new"] for a in body["albums"]["items"]} == {
        "FRESH": True,
        "KNOWN": False,
        "MARKED": False,
    }
    # Exactly the albums "New to you" lists.
    new = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")
    assert [a["album_id"] for a in new["albums"]["items"]] == ["FRESH"]


def test_order_is_play_count_then_listening_time_then_name_by_code_point_then_album_id(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for minute, (album, name, plays) in enumerate(
        [
            ("A0", "zzz long", 1),  # same plays, more listening time: before every 1-play name
            ("A1", "alpha", 1),
            ("A2", "Zulu", 1),  # "Z" sorts before "a" by code point
            ("A3", " Zulu ", 1),  # the same name after trimming: the album ID decides
            ("A4", "Énfasis", 1),  # "É" (U+00C9) sorts after "a"
            ("A5", "zz", 3),  # most plays first, whatever the name
        ]
    ):
        for n in range(plays):
            play(
                engine,
                user,
                f"2026-10-05T18:{minute:02d}:{n:02d}Z",
                f"T{minute}",
                album=album,
                name=name,
                duration_ms=900_000 if album == "A0" else 200_000,
            )

    body = october(read_api, user)

    assert [a["album_id"] for a in body["albums"]["items"]] == ["A5", "A0", "A2", "A3", "A1", "A4"]


def test_ten_items_by_default_with_the_full_count_and_all_true_returns_every_one(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for n in range(12):
        play(engine, user, f"2026-10-05T18:{n:02d}:00Z", f"T{n}", album=f"ALB{n:02d}")

    cut = october(read_api, user)
    every = october(read_api, user, all="true")

    assert cut["albums"]["count"] == 12
    assert [a["album_id"] for a in cut["albums"]["items"]] == [f"ALB{n:02d}" for n in range(10)]
    assert every["albums"]["count"] == 12
    assert [a["album_id"] for a in every["albums"]["items"]] == [f"ALB{n:02d}" for n in range(12)]
    assert october(read_api, user, all="false") == cut


def test_another_users_plays_never_show_or_count(read_api: TestClient, engine: Engine) -> None:
    me, other = make_user(engine), make_user(engine)
    play(engine, me, "2026-10-05T18:00:00Z", "MINE", album="SHARED")
    play(engine, other, "2026-10-05T18:00:00Z", "THEIRS", album="SHARED", name="Their Name")
    play(engine, other, "2026-10-06T18:00:00Z", "THEIRS", album="SHARED", by=("Their Artist",))
    play(engine, other, "2026-10-05T18:00:00Z", "THEIRS", album="OTHER")
    # Their earlier play of the shared album does not make it old for me.
    play(engine, other, "2026-08-05T18:00:00Z", "THEIRS", album="SHARED")

    body = october(read_api, me, all="true")

    assert body["albums"] == {
        "count": 1,
        "items": [
            {
                "album_id": "SHARED",
                "name": "Album SHARED",
                "artists": ["Nova Kett"],
                "artwork_url": None,
                "rating": None,
                "comment": None,
                "play_count": 1,
                "tracks_played_in_period": 1,
                "total_tracks": 1,
                "total_ms": 200_000,
                "is_new": True,
                "release_type": "album",
            }
        ],
    }


def test_an_empty_period_says_why(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    body = october(read_api, user)

    assert body["albums"] == {"count": 0, "items": []}
    assert body["is_empty"] is True
    assert body["empty_reason"] is not None
    summary = get(read_api, user, "/v1/summary", period="month", anchor="2026-10-01")
    assert body["empty_reason"] == summary["empty_reason"]


def test_plays_with_no_album_are_not_an_empty_period(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "NONE", album=None)

    body = october(read_api, user)

    assert body["albums"] == {"count": 0, "items": []}
    assert body["is_empty"] is False
    assert body["empty_reason"] is None


def test_all_is_true_or_false_only(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    response = read_api.get(
        "/v1/albums", params={"period": "month", "all": "yes"}, headers=user.auth
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_parameter"


def test_the_tracks_alias_answers_like_the_new_to_you_path_for_an_old_album(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-08-01T18:00:00Z", "OLD1", album="OLD")
    play(engine, user, "2026-10-05T18:00:00Z", "OLD1", album="OLD")
    params = {"period": "month", "anchor": "2026-10-01"}

    alias = get(read_api, user, "/v1/albums/OLD/tracks", **params)
    original = get(read_api, user, "/v1/new-to-you/albums/OLD/tracks", **params)

    assert alias == original
    assert [(t["item_key"], t["is_new"]) for t in alias["tracks"]] == [("id:OLD1", False)]
    bad = read_api.get("/v1/albums/not-an-id/tracks", params=params, headers=user.auth)
    assert bad.status_code == 422


def test_album_artists_are_the_albums_own_as_in_new_to_you(
    read_api: TestClient, engine: Engine
) -> None:
    """WHO-135: an old compilation shows its label, not its last track's artists."""
    user = make_user(engine)
    for at, track, by in [
        ("2026-08-01T18:00:00Z", "H1", ("Synthetic One",)),
        ("2026-10-05T18:00:00Z", "H2", ("Synthetic Two",)),
    ]:
        add_play(
            engine,
            user,
            at,
            item=track,
            creators=by,
            collection="HELP(2)",
            external_ids={"spotify_album_id": "HELP2"},
            provider_extras={"spotify_album_artists": ["Synthetic Label"], **album_extras(1)},
        )
    # No stored album artists: the artist on every played track is shown.
    play(engine, user, "2026-10-06T18:00:00Z", "S1", album="SOLO", by=("Nova Kett", "Guest"))
    play(engine, user, "2026-10-07T18:00:00Z", "S2", album="SOLO", by=("Nova Kett",))

    items = {a["album_id"]: a for a in october(read_api, user)["albums"]["items"]}

    assert items["HELP2"]["artists"] == ["Synthetic Label"]
    assert items["HELP2"]["is_new"] is False
    assert items["SOLO"]["artists"] == ["Nova Kett"]
    [new_solo] = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")[
        "albums"
    ]["items"]
    assert new_solo["artists"] == items["SOLO"]["artists"]
    tracks = get(read_api, user, "/v1/albums/HELP2/tracks", period="month", anchor="2026-10-01")
    assert tracks["album"]["artists"] == ["Synthetic Label"]


# --- listened in the period: more than half of its tracks (WHO-160) ---------


def play_distinct(
    engine: Engine,
    user: User,
    count: int,
    *,
    day: str = "2026-10-05",
    first: int = 0,
    total_tracks: int | None = 10,
) -> None:
    """``count`` distinct tracks ``T<first>``.. of album ``ALB1``, an hour apart on ``day``."""
    for n in range(first, first + count):
        play(engine, user, f"{day}T{8 + n:02d}:00:00Z", f"T{n}", total_tracks=total_tracks)


@pytest.mark.parametrize(("played", "listed"), [(1, False), (5, False), (6, True)])
def test_a_ten_track_album_is_listed_from_six_distinct_tracks_in_the_period(
    read_api: TestClient, engine: Engine, played: int, listed: bool
) -> None:
    user = make_user(engine)
    play_distinct(engine, user, played)

    body = october(read_api, user)

    items = [
        (a["album_id"], a["tracks_played_in_period"], a["total_tracks"])
        for a in body["albums"]["items"]
    ]
    assert items == ([("ALB1", played, 10)] if listed else [])
    assert body["albums"]["count"] == (1 if listed else 0)
    assert body["is_empty"] is False  # the period has plays either way
    assert body["empty_reason"] is None


def test_one_track_played_six_times_counts_once(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    for hour in range(8, 14):
        play(engine, user, f"2026-10-05T{hour:02d}:00:00Z", "T0", total_tracks=10)

    assert october(read_api, user)["albums"] == {"count": 0, "items": []}


def test_tracks_played_before_the_period_do_not_count(read_api: TestClient, engine: Engine) -> None:
    """6 distinct of 10, but 3 in September: not listened in October."""
    user = make_user(engine)
    play_distinct(engine, user, 3, day="2026-09-10")
    play_distinct(engine, user, 3, first=3)

    assert october(read_api, user)["albums"] == {"count": 0, "items": []}
    september = get(read_api, user, "/v1/albums", period="month", anchor="2026-09-01")
    assert september["albums"] == {"count": 0, "items": []}


def test_an_album_with_an_unknown_track_count_is_not_listed(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play_distinct(engine, user, 6, total_tracks=None)

    assert october(read_api, user)["albums"] == {"count": 0, "items": []}


def test_a_one_track_album_played_once_is_listed(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "SINGLE", album="ONE", total_tracks=1)

    [album] = october(read_api, user)["albums"]["items"]

    assert album["album_id"] == "ONE"
    assert (album["tracks_played_in_period"], album["total_tracks"]) == (1, 1)


def test_count_is_of_the_listed_albums_and_limit_cuts_only_items(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for n in range(12):  # 12 listened albums: 2 of 3 tracks each
        for t in range(2):
            at = f"2026-10-05T{8 + t:02d}:{n:02d}:00Z"
            play(engine, user, at, f"T{n}-{t}", album=f"ALB{n:02d}", total_tracks=3)
    for n in range(4):  # 4 albums with 1 of 3 tracks: played, not listed
        play(engine, user, f"2026-10-06T08:{n:02d}:00Z", f"U{n}", album=f"HALF{n}", total_tracks=3)

    cut = october(read_api, user)
    every = october(read_api, user, all="true")

    assert cut["albums"]["count"] == every["albums"]["count"] == 12
    assert len(cut["albums"]["items"]) == 10
    assert sorted(a["album_id"] for a in every["albums"]["items"]) == [
        f"ALB{n:02d}" for n in range(12)
    ]
