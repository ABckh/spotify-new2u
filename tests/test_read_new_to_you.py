"""``GET /v1/new-to-you``: albums, singles and EPs, and tracks first played in a period (S14, F24).

The read clock is ``read_helpers.NOW`` (2026-10-11 12:00 UTC); the default
display zone is Europe/Warsaw (UTC+2 in September and October 2026).
"""

import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from read_helpers import User, add_play, album_extras, get, make_user


def new_to_you(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return get(api, user, "/v1/new-to-you", **params)


def october(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return new_to_you(api, user, period="month", anchor="2026-10-01", **params)


def play(
    engine: Engine,
    user: User,
    at: str,
    item: str,
    *,
    by: tuple[str, ...] = ("Nova Kett",),
    album: str | None = None,
    total_tracks: int | None = 1,
) -> None:
    """One play of track ``item``; ``album`` is its Spotify album ID and name."""
    add_play(
        engine,
        user,
        at,
        item=item,
        creators=by,
        collection=None if album is None else f"Album {album}",
        external_ids=None if album is None else {"spotify_album_id": album},
        provider_extras=None if album is None else album_extras(total_tracks),
    )


def keys(body: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(album ids, single and EP ids, track item keys) in the order served."""
    return (
        [a["album_id"] for a in body["albums"]["items"]],
        [a["album_id"] for a in body["singles"]["items"]],
        [t["item_key"] for t in body["tracks"]["items"]],
    )


def test_s14_2_a_first_play_inside_the_period_is_new(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "AAA", album="ALB1")
    play(engine, user, "2026-10-06T18:00:00Z", "BBB", album="ALB1")
    play(engine, user, "2026-10-07T18:00:00Z", "AAA", album="ALB1")

    body = october(read_api, user)

    assert keys(body) == (["ALB1"], [], ["id:BBB", "id:AAA"])
    assert body["albums"] == {
        "count": 1,
        "items": [
            {
                "album_id": "ALB1",
                "name": "Album ALB1",
                "artists": ["Nova Kett"],
                "artwork_url": None,
                "rating": None,
                "comment": None,
                "first_played_at": "2026-10-05T18:00:00.000Z",
                "first_played_local_date": "2026-10-05",
                "tracks_played_in_period": 2,
                # The fixture's 1-track album: both tracks count (WHO-155).
                "tracks_listened": 2,
                "total_tracks": 1,
            }
        ],
    }
    assert body["singles"] == {"count": 0, "items": []}
    assert body["tracks"]["items"][1] == {
        "item_key": "id:AAA",
        "title": "Track AAA",
        "creators": ["Nova Kett"],
        "artwork_url": None,
        "first_played_at": "2026-10-05T18:00:00.000Z",
        "first_played_local_date": "2026-10-05",
    }
    assert (body["nothing_new"], body["is_empty"], body["empty_reason"]) == (False, False, None)


def test_s14_2_a_first_play_before_the_period_with_a_repeat_inside_is_not_new(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-10T18:00:00Z", "AAA", album="ALB1")
    play(engine, user, "2026-10-05T18:00:00Z", "AAA", album="ALB1")

    body = october(read_api, user)

    assert keys(body) == ([], [], [])
    assert [body[k]["count"] for k in ("albums", "singles", "tracks")] == [0, 0, 0]
    # S14.14: plays in the period, nothing new -> V3, not an empty period.
    assert (body["nothing_new"], body["is_empty"]) == (True, False)
    september = new_to_you(read_api, user, period="month", anchor="2026-09-01")
    assert keys(september) == (["ALB1"], [], ["id:AAA"])


def test_s14_2_a_new_track_on_a_known_album_is_new_but_the_album_is_not(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-10T18:00:00Z", "AAA", album="ALB1")
    play(engine, user, "2026-10-05T18:00:00Z", "BBB", album="ALB1")

    assert keys(october(read_api, user)) == ([], [], ["id:BBB"])


def test_s14_10_a_play_before_a_disconnect_still_counts_after_reconnect(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-10T18:00:00Z", "AAA", album="ALB1")
    with engine.begin() as conn:  # revoked on 20 September, reconnected on 2 October
        conn.execute(
            text(
                "INSERT INTO collection_gap (user_id, source, started_at, ended_at) "
                "VALUES (:u, 'spotify', :start, :end)"
            ),
            {
                "u": user.id,
                "start": datetime(2026, 9, 20, tzinfo=UTC),
                "end": datetime(2026, 10, 2, tzinfo=UTC),
            },
        )
    play(engine, user, "2026-10-05T18:00:00Z", "AAA", album="ALB1")
    play(engine, user, "2026-10-05T19:00:00Z", "CCC", by=("Pine Arc",), album="ALB2")

    body = october(read_api, user)

    assert keys(body) == (["ALB2"], [], ["id:CCC"])
    assert body["collection_gap_overlap"] is True  # N1 applies, as in S3.8b


def test_s14_11_after_delete_everything_items_are_new_again(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-10T18:00:00Z", "AAA", album="ALB1")
    play(engine, user, "2026-10-05T18:00:00Z", "AAA", album="ALB1")
    assert keys(october(read_api, user)) == ([], [], [])

    assert read_api.delete("/v1/me", headers=user.auth).status_code == 204
    # The next connect starts a new account and a new stored history (D-2).
    again = make_user(engine)
    play(engine, again, "2026-10-08T18:00:00Z", "AAA", album="ALB1")

    body = october(read_api, again)
    assert keys(body) == (["ALB1"], [], ["id:AAA"])
    assert body["coverage_start"] == "2026-10-08T18:00:00.000Z"  # V1 shows the new date
    assert body["coverage_start_local_date"] == "2026-10-08"


def test_s14_4_two_album_ids_are_two_albums_and_a_play_without_one_has_no_album(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T18:00:00Z", "AAA", album="ALB1")
    add_play(engine, user, "2026-10-05T19:00:00Z", item="AAB", collection="Album ALB1")
    play(engine, user, "2026-10-05T20:00:00Z", "AAC", album="ALB1-DELUXE")

    # Same local date, so the name decides (R-60).
    assert keys(october(read_api, user))[0] == ["ALB1", "ALB1-DELUXE"]


def test_t37_two_album_ids_with_the_same_name_are_two_albums(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for item, album in (("T1", "AL1"), ("T2", "AL9")):
        add_play(
            engine,
            user,
            "2026-10-05T18:00:00Z",
            item=item,
            collection="Morning",
            external_ids={"spotify_album_id": album},
            provider_extras=album_extras(1),
        )

    albums = october(read_api, user)["albums"]["items"]

    assert [(a["album_id"], a["name"]) for a in albums] == [("AL1", "Morning"), ("AL9", "Morning")]


def test_r59_names_shown_are_those_of_the_most_recent_stored_play(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for at, title, album in (
        ("2026-10-05T18:00:00Z", "Old Title", "Old Album Name"),
        ("2026-10-20T18:00:00Z", "New Title", "New Album Name"),
    ):
        add_play(
            engine,
            user,
            at,
            item="AAA",
            title=title,
            collection=album,
            external_ids={"spotify_album_id": "ALB1"},
            provider_extras=album_extras(1),
        )

    body = october(read_api, user)

    assert body["tracks"]["items"][0]["title"] == "New Title"
    assert body["albums"]["items"][0]["name"] == "New Album Name"
    assert body["tracks"]["items"][0]["first_played_local_date"] == "2026-10-05"


def test_s14_6_newest_first_then_name_a_to_z(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    for item, title in (("T1", "beta"), ("T2", "Alpha"), ("T3", "alpha"), ("T4", "Zulu")):
        add_play(engine, user, "2026-10-05T18:00:00Z", item=item, title=title)
    add_play(engine, user, "2026-10-04T18:00:00Z", item="T5", title="Aardvark")
    add_play(engine, user, "2026-10-06T18:00:00Z", item="T6", title="Yak")

    titles = [t["title"] for t in october(read_api, user)["tracks"]["items"]]

    # Ties compare by code point, so the order never depends on the server locale.
    assert titles == ["Yak", "Alpha", "Zulu", "alpha", "beta", "Aardvark"]


def test_s14_7_first_ten_by_default_all_on_request_and_the_full_count(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for n in range(12):
        play(engine, user, f"2026-10-05T{n:02d}:00:00Z", f"T{n:02d}", by=(f"Artist {n:02d}",))

    first = october(read_api, user)
    every = october(read_api, user, all="true")

    assert (first["tracks"]["count"], len(first["tracks"]["items"])) == (12, 10)
    assert (every["tracks"]["count"], len(every["tracks"]["items"])) == (12, 12)
    assert every["tracks"]["items"][:10] == first["tracks"]["items"]
    assert october(read_api, user, all="false") == first


@pytest.mark.parametrize("value", ["1", "True", "yes", ""])
def test_all_other_than_true_or_false_is_refused(
    read_api: TestClient, engine: Engine, value: str
) -> None:
    user = make_user(engine)

    response = read_api.get(
        "/v1/new-to-you", params={"period": "day", "all": value}, headers=user.auth
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_parameter"


def test_a_bad_period_is_422_with_the_resolver_code(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    response = read_api.get(
        "/v1/new-to-you",
        params={"period": "custom", "from": "2026-10-05", "to": "2026-10-01"},
        headers=user.auth,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "period_range_reversed"


def test_s14_8_9_collection_start_and_the_period_that_contains_it(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-10T18:00:00Z", "AAA")
    play(engine, user, "2026-10-05T18:00:00Z", "BBB")

    september = new_to_you(read_api, user, period="month", anchor="2026-09-01")
    later = october(read_api, user)

    for body in (september, later):
        assert body["coverage_start"] == "2026-09-10T18:00:00.000Z"
        assert body["coverage_start_local_date"] == "2026-09-10"
    assert september["period_contains_coverage_start"] is True  # V2
    assert later["period_contains_coverage_start"] is False


@pytest.mark.parametrize(
    ("anchor", "plays", "reason"),
    [
        ("2026-10-01", [], "no_data_yet"),
        ("2026-08-01", ["2026-09-10T18:00:00Z"], "before_coverage"),
        ("2026-10-01", ["2026-09-10T18:00:00Z"], "no_plays"),
    ],
)
def test_s14_15_an_empty_period_has_the_summarys_empty_state(
    read_api: TestClient, engine: Engine, anchor: str, plays: list[str], reason: str
) -> None:
    user = make_user(engine)
    for at in plays:
        play(engine, user, at, "AAA")

    body = new_to_you(read_api, user, period="month", anchor=anchor)
    summary = get(read_api, user, "/v1/summary", period="month", anchor=anchor)

    assert (body["is_empty"], body["empty_reason"]) == (True, reason)
    assert (summary["is_empty"], summary["empty_reason"]) == (True, reason)
    assert body["nothing_new"] is False
    assert keys(body) == ([], [], [])


def test_t09_travel_gives_the_same_lists_and_the_stored_zone_decides_the_boundary(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)  # Europe/Warsaw
    # 1 October 00:30 in Warsaw, but still 30 September in New York.
    play(engine, user, "2026-09-30T22:30:00Z", "AAA", album="ALB1")
    plain = october(read_api, user)

    # The device zone is never an input, so "travel" is a request that says so.
    response = read_api.get(
        "/v1/new-to-you",
        params={"period": "month", "anchor": "2026-10-01", "time_zone": "America/New_York"},
        headers={**user.auth, "X-Time-Zone": "America/New_York"},
    )
    assert response.json() == plain
    assert keys(plain) == (["ALB1"], [], ["id:AAA"])
    assert plain["tracks"]["items"][0]["first_played_local_date"] == "2026-10-01"

    with engine.begin() as conn:  # the one way the boundary moves: the stored zone changes
        conn.execute(
            text("UPDATE app_user SET home_timezone = 'America/New_York' WHERE id = :u"),
            {"u": user.id},
        )
    assert keys(october(read_api, user)) == ([], [], [])
    september = new_to_you(read_api, user, period="month", anchor="2026-09-01")
    assert september["tracks"]["items"][0]["first_played_local_date"] == "2026-09-30"


def test_a_repoll_of_the_same_window_changes_nothing(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    plays = [("2026-09-10T18:00:00Z", "AAA"), ("2026-10-05T18:00:00Z", "BBB")]
    for at, item in plays:
        play(engine, user, at, item, album=f"ALB-{item}")
    before = october(read_api, user)

    for at, item in plays:
        play(engine, user, at, item, album=f"ALB-{item}")

    assert october(read_api, user) == before
    assert before["albums"]["items"][0]["tracks_played_in_period"] == 1


# --- W-8: new to you, at the boundaries (period-model §7a) ----------------

# (occurred_at, item, title, album id, album name, artists). Europe/Warsaw, UTC+1.
W8_PLAYS = [
    ("2026-03-02T07:00:00Z", "T0", "Old Song", "AL0", "Old Album", ("Kade Moss",)),  # E0
    ("2026-03-09T22:59:59Z", "T2", "Edge", "AL2", "Borders", ("Tamsin Reyes",)),  # E1
    ("2026-03-09T23:00:00Z", "T1", "First Light", "AL1", "Morning", ("Ilsa Vane",)),  # E2
    ("2026-03-10T12:00:00Z", "T2", "Edge", "AL2", "Borders", ("Tamsin Reyes",)),  # E3
    ("2026-03-10T15:00:00Z", "T4", "Second Side", "AL0", "Old Album", ("Kade Moss", "Lio Brandt")),
    ("2026-03-10T23:00:00Z", "T5", "Late Bell", "AL5", "Night", ("Oren Vale",)),  # E5
    ("2026-03-12T10:00:00Z", "T1", "First Light", "AL1", "Morning", ("Ilsa Vane",)),  # E6
]


def add_w8(engine: Engine, user: User) -> None:
    for at, item, title, album_id, album, by in W8_PLAYS:
        add_play(
            engine,
            user,
            at,
            item=item,
            title=title,
            creators=by,
            duration_ms=240_000,
            collection=album,
            external_ids={"spotify_album_id": album_id},
            provider_extras=album_extras(1),
        )


def lists(body: dict[str, Any]) -> dict[str, list[tuple[Any, ...]]]:
    """Each list as (name, first-play local date, in-period number) rows."""
    return {
        "tracks": [(t["title"], t["first_played_local_date"]) for t in body["tracks"]["items"]],
        "albums": [
            (a["name"], a["first_played_local_date"], a["tracks_played_in_period"])
            for a in body["albums"]["items"]
        ],
    }


def test_w8_day_2026_03_10_start_is_inside_end_is_outside(
    read_api: TestClient, engine: Engine
) -> None:  # T-32, T-33, T-34, T-36
    user = make_user(engine)
    add_w8(engine, user)

    body = new_to_you(read_api, user, period="day", anchor="2026-03-10")

    assert lists(body) == {
        "tracks": [("First Light", "2026-03-10"), ("Second Side", "2026-03-10")],
        "albums": [("Morning", "2026-03-10", 1)],
    }
    assert body["period_contains_coverage_start"] is False


def test_w8_the_neighbouring_days(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)  # T-33, T-34, T-40
    add_w8(engine, user)

    ninth = new_to_you(read_api, user, period="day", anchor="2026-03-09")
    eleventh = new_to_you(read_api, user, period="day", anchor="2026-03-11")
    twelfth = new_to_you(read_api, user, period="day", anchor="2026-03-12")

    assert keys(ninth) == (["AL2"], [], ["id:T2"])
    assert keys(eleventh) == (["AL5"], [], ["id:T5"])
    assert keys(twelfth) == ([], [], [])
    assert (twelfth["nothing_new"], twelfth["is_empty"]) == (True, False)  # V3


def test_w8_the_week_of_2026_03_09(read_api: TestClient, engine: Engine) -> None:  # T-38
    user = make_user(engine)
    add_w8(engine, user)

    body = new_to_you(read_api, user, period="week", anchor="2026-03-09")

    assert lists(body) == {
        "tracks": [
            ("Late Bell", "2026-03-11"),
            ("First Light", "2026-03-10"),
            ("Second Side", "2026-03-10"),
            ("Edge", "2026-03-09"),
        ],
        "albums": [
            ("Night", "2026-03-11", 1),
            ("Morning", "2026-03-10", 1),
            ("Borders", "2026-03-09", 1),
        ],
    }
    assert body["period_contains_coverage_start"] is False


def test_w8_the_week_containing_coverage_start_shows_v2(
    read_api: TestClient, engine: Engine
) -> None:  # T-39
    user = make_user(engine)
    add_w8(engine, user)

    body = new_to_you(read_api, user, period="week", anchor="2026-03-02")

    assert keys(body) == (["AL0"], [], ["id:T0"])
    assert body["period_contains_coverage_start"] is True
    assert body["coverage_start_local_date"] == "2026-03-02"


def test_t45_plays_after_a_period_leave_its_lists_unchanged(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_w8(engine, user)
    before = new_to_you(read_api, user, period="day", anchor="2026-03-10")

    add_play(engine, user, "2026-03-20T10:00:00Z", item="T9", title="Later", creators=("Ada",))

    assert new_to_you(read_api, user, period="day", anchor="2026-03-10") == before


# --- S14.13: speed --------------------------------------------------------

# One year of synthetic plays: 60 a day, 5,000 tracks on 500 albums of 10 tracks, 800
# artists, two per track. Written in one statement; every value is synthetic.
_ONE_YEAR = text(
    """
    INSERT INTO unified_event (
        user_id, source, source_artifact, ingest_method, kind,
        occurred_at, occurred_at_precision, duration_ms, title, creators,
        collection, external_ids, provider_extras, item_key, item_key_source, dedupe_key,
        day_key
    )
    SELECT :u, 'spotify', 'spotify_recently_played', 'api_poll', 'listen',
           :first + n * interval '24 minutes', 'second', 200000,
           'Track ' || t, ARRAY['Artist ' || (t % 800), 'Artist ' || ((t * 7 + 3) % 800)],
           'Album ' || (t / 10), jsonb_build_object('spotify_album_id', 'ALB' || (t / 10)),
           jsonb_build_object('spotify_album_total_tracks', 10),
           'id:T' || t, 'provider_id',
           sha256(convert_to('dedupe:' || n, 'UTF8')), sha256(convert_to('day:' || n, 'UTF8'))
    FROM generate_series(0, 365 * 60 - 1) AS n
    CROSS JOIN LATERAL (SELECT (n * 7919) % 5000 AS t) AS track
    """
)


def test_s14_13_a_month_answers_within_two_seconds_with_a_year_of_history(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    with engine.begin() as conn:
        conn.execute(_ONE_YEAR, {"u": user.id, "first": datetime(2025, 10, 11, tzinfo=UTC)})
        conn.execute(text("ANALYZE unified_event"))

    started = time.perf_counter()
    body = new_to_you(read_api, user, period="month", offset="-1", all="true")
    elapsed = time.perf_counter() - started

    print(f"\nS14.13: month of 365 x 60 plays answered in {elapsed:.3f} s")
    assert body["is_empty"] is False
    assert elapsed < 2.0
