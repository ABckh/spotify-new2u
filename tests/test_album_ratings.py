"""Album ratings (``app.read.ratings``): 1 to 10, per user, all time.

``PUT`` / ``DELETE /v1/albums/{album_id}/rating``, the ``rating`` on every
album item, and ``GET /v1/ratings/albums``. The read clock is
``read_helpers.NOW`` (2026-10-11 12:00 UTC). Every value is synthetic.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from read_helpers import User, add_play, album_extras, get, make_user


def play(engine: Engine, user: User, at: str, item: str, album: str) -> None:
    add_play(
        engine,
        user,
        at,
        item=item,
        creators=("Nova Kett",),
        collection=f"Album {album}",
        artwork_url="https://i.scdn.co/image/synthetic-300",
        external_ids={"spotify_album_id": album},
        provider_extras=album_extras(1),
    )


def rate(api: TestClient, user: User, album: str, rating: Any) -> Any:
    return api.put(f"/v1/albums/{album}/rating", json={"rating": rating}, headers=user.auth)


def all_ratings(api: TestClient, user: User) -> list[tuple[str, int]]:
    body = get(api, user, "/v1/ratings/albums")
    assert body["count"] == len(body["items"])
    return [(a["album_id"], a["rating"]) for a in body["items"]]


def test_a_rating_is_set_replaced_and_cleared(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    assert rate(read_api, user, "ALB1", 7).status_code == 204
    assert all_ratings(read_api, user) == [("ALB1", 7)]
    assert rate(read_api, user, "ALB1", 9).status_code == 204
    assert all_ratings(read_api, user) == [("ALB1", 9)]

    response = read_api.delete("/v1/albums/ALB1/rating", headers=user.auth)
    assert response.status_code == 204
    assert all_ratings(read_api, user) == []
    # Clearing again changes nothing.
    assert read_api.delete("/v1/albums/ALB1/rating", headers=user.auth).status_code == 204


def test_every_album_item_carries_the_rating(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    play(engine, user, "2026-10-06T10:00:00+00:00", "BBB", "ALB2")
    rate(read_api, user, "ALB1", 8)
    month = {"period": "month", "anchor": "2026-10-01"}

    new = get(read_api, user, "/v1/new-to-you", **month)["albums"]["items"]
    assert {a["album_id"]: a["rating"] for a in new} == {"ALB1": 8, "ALB2": None}
    played = get(read_api, user, "/v1/albums", **month)["albums"]["items"]
    assert {a["album_id"]: a["rating"] for a in played} == {"ALB1": 8, "ALB2": None}
    tracks = get(read_api, user, "/v1/albums/ALB1/tracks", **month)
    assert tracks["album"]["rating"] == 8
    tracks = get(read_api, user, "/v1/new-to-you/albums/ALB2/tracks", **month)
    assert tracks["album"]["rating"] is None


def test_the_all_time_list_is_best_first_with_names_and_covers(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2025-01-05T10:00:00+00:00", "AAA", "OLD")  # long ago: still listed
    play(engine, user, "2026-10-05T10:00:00+00:00", "BBB", "MID")
    play(engine, user, "2026-10-06T10:00:00+00:00", "CCC", "TOP")
    rate(read_api, user, "MID", 6)
    rate(read_api, user, "OLD", 6)
    rate(read_api, user, "TOP", 10)

    body = get(read_api, user, "/v1/ratings/albums")

    # Equal ratings: the one rated last first.
    assert [(a["album_id"], a["rating"]) for a in body["items"]] == [
        ("TOP", 10),
        ("OLD", 6),
        ("MID", 6),
    ]
    top = body["items"][0]
    assert top["name"] == "Album TOP"
    assert top["artists"] == ["Nova Kett"]
    assert top["artwork_url"] == "https://i.scdn.co/image/synthetic-300"
    assert top["rated_at"].endswith("Z")


@pytest.mark.parametrize("rating", [0, 11, -1, 7.5, "8", None])
def test_only_whole_numbers_from_1_to_10(read_api: TestClient, engine: Engine, rating: Any) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    assert rate(read_api, user, "ALB1", rating).status_code == 422
    assert all_ratings(read_api, user) == []


def test_an_album_with_no_play_cannot_be_rated(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    other = make_user(engine)
    play(engine, other, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    assert rate(read_api, user, "ALB1", 8).status_code == 404
    assert rate(read_api, user, "not-an-id!", 8).status_code == 422


def test_ratings_are_per_user(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    other = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    play(engine, other, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    rate(read_api, user, "ALB1", 3)

    assert all_ratings(read_api, other) == []
    tracks = get(read_api, other, "/v1/albums/ALB1/tracks", period="month", anchor="2026-10-01")
    assert tracks["album"]["rating"] is None


def test_ratings_need_a_session(read_api: TestClient) -> None:
    assert read_api.get("/v1/ratings/albums").status_code == 401
    assert read_api.put("/v1/albums/ALB1/rating", json={"rating": 5}).status_code == 401
    assert read_api.delete("/v1/albums/ALB1/rating").status_code == 401
