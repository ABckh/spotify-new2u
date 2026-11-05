"""New singles and EPs in ``GET /v1/new-to-you`` (``singles``), and rating them.

A release Spotify calls ``single`` is a single with 1 to 3 tracks and an EP
with more. It is new by the album rule, and left out when every song the user
played from it is also on an album new in the same period. The read clock is
``read_helpers.NOW`` (2026-10-11 12:00 UTC). Every value is synthetic.
"""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from read_helpers import User, add_play, get, make_user


def play(
    engine: Engine,
    user: User,
    at: str,
    item: str,
    release: str,
    *,
    tracks: int = 1,
    single: bool = True,
    title: str | None = None,
    by: str = "Nova Kett",
    isrc: str | None = None,
) -> None:
    """A play of ``item`` on ``release``, a single (or an album) of ``tracks`` tracks."""
    extras: dict[str, object] = {"spotify_album_total_tracks": tracks}
    if single:
        extras["spotify_album_type"] = "single"
    ids = {"spotify_album_id": release} | ({"isrc": isrc} if isrc else {})
    add_play(
        engine,
        user,
        at,
        item=item,
        title=title,
        creators=(by,),
        collection=f"Release {release}",
        external_ids=ids,
        provider_extras=extras,
    )


def october(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return get(api, user, "/v1/new-to-you", period="month", anchor="2026-10-01", **params)


def singles(body: dict[str, Any]) -> list[tuple[str, str]]:
    assert body["singles"]["count"] >= len(body["singles"]["items"])
    return [(s["album_id"], s["release_type"]) for s in body["singles"]["items"]]


def test_a_single_and_an_ep_are_listed_with_their_type(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-02T10:00:00Z", "S1", "SINGLE1")
    play(engine, user, "2026-10-03T10:00:00Z", "T1", "TRIO", tracks=3)
    play(engine, user, "2026-10-03T11:00:00Z", "T2", "TRIO", tracks=3)
    # An EP of 4 tracks is new once 3 of them are played (more than half).
    for n in range(3):
        play(engine, user, f"2026-10-04T1{n}:00:00Z", f"E{n}", "EP1", tracks=4)

    body = october(read_api, user)

    assert singles(body) == [("EP1", "ep"), ("TRIO", "single"), ("SINGLE1", "single")]
    assert body["albums"]["count"] == 0
    assert body["singles"]["items"][0] | {"first_played_at": None} == {
        "album_id": "EP1",
        "name": "Release EP1",
        "artists": ["Nova Kett"],
        "artwork_url": None,
        "rating": None,
        "comment": None,
        "first_played_at": None,
        "first_played_local_date": "2026-10-04",
        "tracks_played_in_period": 3,
        "tracks_listened": 3,
        "total_tracks": 4,
        "release_type": "ep",
    }
    assert body["nothing_new"] is False


def test_a_half_played_ep_is_not_new_yet(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    for n in range(2):
        play(engine, user, f"2026-10-04T1{n}:00:00Z", f"E{n}", "EP1", tracks=4)

    assert singles(october(read_api, user)) == []


def test_a_single_whose_song_is_on_a_new_album_shows_with_the_album_only(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    # The lead single, then the album with the same recording: same ISRC, other IDs.
    play(engine, user, "2026-10-02T10:00:00Z", "LEAD", "SINGLE1", isrc="SYN000000001")
    play(
        engine, user, "2026-10-05T10:00:00Z", "ALBUMCUT", "ALB1", single=False, isrc="SYN000000001"
    )
    # Another single whose song is on the album under the same title and artist.
    play(engine, user, "2026-10-03T10:00:00Z", "B", "SINGLE2", title="Second Song")
    play(engine, user, "2026-10-05T11:00:00Z", "B2", "ALB1", single=False, title=" second song")
    # And one that has nothing to do with the album.
    play(engine, user, "2026-10-04T10:00:00Z", "OTHER", "SINGLE3")

    body = october(read_api, user)

    assert [a["album_id"] for a in body["albums"]["items"]] == ["ALB1"]
    assert singles(body) == [("SINGLE3", "single")]
    assert body["singles"]["count"] == 1


def test_a_single_stays_when_its_album_was_new_in_another_period(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-20T10:00:00Z", "ALBUMCUT", "ALB1", single=False, isrc="SYN1")
    play(engine, user, "2026-10-02T10:00:00Z", "LEAD", "SINGLE1", isrc="SYN1")

    assert singles(october(read_api, user)) == [("SINGLE1", "single")]


def test_an_ep_with_one_song_not_on_the_album_stays(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-02T10:00:00Z", "E1", "EP1", tracks=4, isrc="SYN1")
    play(engine, user, "2026-10-02T11:00:00Z", "E2", "EP1", tracks=4, isrc="SYN2")
    play(engine, user, "2026-10-02T12:00:00Z", "E3", "EP1", tracks=4, isrc="SYN3")
    play(engine, user, "2026-10-05T10:00:00Z", "A1", "ALB1", single=False, isrc="SYN1")

    assert singles(october(read_api, user)) == [("EP1", "ep")]


def test_a_single_marked_not_new_is_left_out(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-02T10:00:00Z", "S1", "SINGLE1")
    play(engine, user, "2026-10-03T10:00:00Z", "S2", "SINGLE2")

    response = read_api.put("/v1/new-to-you/not-new/album/SINGLE1", headers=user.auth)
    assert response.status_code == 204

    assert singles(october(read_api, user)) == [("SINGLE2", "single")]


def test_first_ten_by_default_and_the_full_count(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    for n in range(12):
        play(engine, user, f"2026-10-0{1 + n % 9}T1{n % 10}:00:00Z", f"S{n:02d}", f"SINGLE{n:02d}")

    first, every = october(read_api, user), october(read_api, user, all="true")

    assert (first["singles"]["count"], len(first["singles"]["items"])) == (12, 10)
    assert (every["singles"]["count"], len(every["singles"]["items"])) == (12, 12)


def test_singles_and_eps_are_rated_like_albums(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-02T10:00:00Z", "S1", "SINGLE1")
    for n in range(3):
        play(engine, user, f"2026-10-04T1{n}:00:00Z", f"E{n}", "EP1", tracks=5)
    play(engine, user, "2026-10-05T10:00:00Z", "A1", "ALB1", single=False)
    for release, rating in (("SINGLE1", 9), ("EP1", 7), ("ALB1", 8)):
        response = read_api.put(
            f"/v1/albums/{release}/rating", json={"rating": rating}, headers=user.auth
        )
        assert response.status_code == 204

    rated = get(read_api, user, "/v1/ratings/albums")["items"]
    body = october(read_api, user)

    assert [(r["album_id"], r["release_type"], r["rating"]) for r in rated] == [
        ("SINGLE1", "single", 9),
        ("ALB1", "album", 8),
        ("EP1", "ep", 7),
    ]
    assert {s["album_id"]: s["rating"] for s in body["singles"]["items"]} == {
        "SINGLE1": 9,
        "EP1": 7,
    }


# --- every single and EP listened in the period: GET /v1/singles --------------


def listened(api: TestClient, user: User, **params: str) -> list[tuple[str, str, bool]]:
    body = get(api, user, "/v1/singles", period="month", anchor="2026-10-01", **params)
    assert body["albums"]["count"] >= len(body["albums"]["items"])
    return [(s["album_id"], s["release_type"], s["is_new"]) for s in body["albums"]["items"]]


def test_every_listened_single_and_ep_new_or_not_is_listed(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    # Known since September: listened in October, not new.
    play(engine, user, "2026-09-02T10:00:00Z", "S1", "OLD1")
    play(engine, user, "2026-10-02T10:00:00Z", "S1", "OLD1")
    play(engine, user, "2026-10-02T11:00:00Z", "S1", "OLD1")
    play(engine, user, "2026-10-03T10:00:00Z", "N1", "NEW1")
    for n in range(3):
        play(engine, user, f"2026-10-04T1{n}:00:00Z", f"E{n}", "EP1", tracks=5)

    # Most played first, as the album list.
    assert listened(read_api, user) == [
        ("EP1", "ep", True),
        ("OLD1", "single", False),
        ("NEW1", "single", True),
    ]


def test_the_singles_list_has_no_album_and_the_album_list_no_single(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-02T10:00:00Z", "S1", "SINGLE1")
    play(engine, user, "2026-10-02T11:00:00Z", "A1", "ALBUM1", single=False)

    albums = get(read_api, user, "/v1/albums", period="month", anchor="2026-10-01")

    assert listened(read_api, user) == [("SINGLE1", "single", True)]
    assert [(a["album_id"], a["release_type"]) for a in albums["albums"]["items"]] == [
        ("ALBUM1", "album")
    ]


def test_an_ep_half_played_in_the_period_is_not_listed(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for n in range(2):
        play(engine, user, f"2026-10-04T1{n}:00:00Z", f"E{n}", "EP1", tracks=4)
    # The third track was in September: listened over time, not in October.
    play(engine, user, "2026-09-04T10:00:00Z", "E2", "EP1", tracks=4)

    assert listened(read_api, user) == []


def test_the_singles_list_carries_rating_and_comment_and_shows_all(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for n in range(12):
        play(engine, user, f"2026-10-0{1 + n % 9}T1{n % 10}:00:00Z", f"S{n}", f"SGL{n:02d}")
    read_api.put("/v1/albums/SGL00/rating", json={"rating": 6}, headers=user.auth)
    read_api.put("/v1/albums/SGL00/comment", json={"comment": "Short and sweet"}, headers=user.auth)

    first = get(read_api, user, "/v1/singles", period="month", anchor="2026-10-01")
    every = get(read_api, user, "/v1/singles", period="month", anchor="2026-10-01", all="true")

    assert (first["albums"]["count"], len(first["albums"]["items"])) == (12, 10)
    assert len(every["albums"]["items"]) == 12
    rated = next(s for s in every["albums"]["items"] if s["album_id"] == "SGL00")
    assert (rated["rating"], rated["comment"]) == (6, "Short and sweet")


def test_the_singles_list_needs_a_session(read_api: TestClient) -> None:
    assert read_api.get("/v1/singles", params={"period": "month"}).status_code == 401
