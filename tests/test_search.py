"""Search (``GET /v1/search``, ``app.read.search``): the user's albums and songs,
all time, by name, artist or the user's own comment.

A comment finds its album and an album shows its comment. Every word of the
query must be in the item, in any field and order, in any case. The query is
never logged nor repeated in an error (PRD P12). The read clock is
``read_helpers.NOW`` (2026-10-11 12:00 UTC). Every value is synthetic.
"""

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.read.search import read_search, search_words
from read_helpers import User, add_play, album_extras, get, make_user

SYNTHETIC_COMMENT = "Synthetic comment 5d2c: the bridge on track 3"


def play(
    engine: Engine,
    user: User,
    at: str,
    item: str,
    album: str,
    *,
    name: str | None = None,
    artist: str = "Nova Kett",
    title: str | None = None,
    album_type: str | None = None,
    total: int = 1,
) -> None:
    """A polled play of track ``item`` on album ``album``, named "Album <album>"."""
    extras = album_extras(total)
    if album_type:
        extras["spotify_album_type"] = album_type
    add_play(
        engine,
        user,
        at,
        item=item,
        title=title,
        creators=(artist,),
        collection=name or f"Album {album}",
        artwork_url="https://i.scdn.co/image/synthetic-300",
        external_ids={"spotify_album_id": album},
        provider_extras=extras,
    )


def imported(
    engine: Engine, user: User, at: str, item: str, *, title: str, album: str, artist: str
) -> None:
    """A play with no album ID, as the history file import stores it."""
    add_play(engine, user, at, item=item, title=title, creators=(artist,), collection=album)


def search(api: TestClient, user: User, q: str) -> dict[str, Any]:
    body = get(api, user, "/v1/search", q=q)
    for kind in ("albums", "tracks"):
        assert body[kind]["count"] == len(body[kind]["items"])
    return body


def albums(api: TestClient, user: User, q: str) -> list[str]:
    return [a["album_id"] for a in search(api, user, q)["albums"]["items"]]


def tracks(api: TestClient, user: User, q: str) -> list[str]:
    return [t["item_key"] for t in search(api, user, q)["tracks"]["items"]]


def comment(api: TestClient, user: User, album: str, words: str) -> None:
    response = api.put(f"/v1/albums/{album}/comment", json={"comment": words}, headers=user.auth)
    assert response.status_code == 204


def test_a_comment_finds_its_album_and_the_album_shows_its_comment(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    play(engine, user, "2026-10-06T10:00:00+00:00", "BBB", "ALB2")
    comment(read_api, user, "ALB1", SYNTHETIC_COMMENT)
    read_api.put("/v1/albums/ALB1/rating", json={"rating": 8}, headers=user.auth)

    by_comment = search(read_api, user, "bridge")["albums"]["items"]
    by_name = search(read_api, user, "album alb1")["albums"]["items"]

    assert (
        by_comment
        == by_name
        == [
            {
                "album_id": "ALB1",
                "name": "Album ALB1",
                "artists": ["Nova Kett"],
                "artwork_url": "https://i.scdn.co/image/synthetic-300",
                "release_type": "album",
                "rating": 8,
                "comment": SYNTHETIC_COMMENT,
                "play_count": 1,
                "last_played_at": "2026-10-05T10:00:00.000Z",
                "last_played_local_date": "2026-10-05",
            }
        ]
    )
    # An album without a comment is found by its name, with comment null.
    assert search(read_api, user, "alb2")["albums"]["items"][0]["comment"] is None


def test_every_word_must_match_in_any_field_order_or_case(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1", name="Quiet Hours")
    comment(read_api, user, "ALB1", "Rainy mornings")

    assert albums(read_api, user, "KETT quiet") == ["ALB1"]
    assert albums(read_api, user, "morn hour nova") == ["ALB1"]
    assert albums(read_api, user, "quiet  \t hours\n") == ["ALB1"]
    assert albums(read_api, user, "quiet evenings") == []


def test_a_word_never_spans_two_fields(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1", name="Rain", artist="Bow")

    assert albums(read_api, user, "rain bow") == ["ALB1"]
    assert albums(read_api, user, "rainbow") == []
    assert tracks(read_api, user, "rainbow") == []


def test_names_and_words_are_compared_after_nfc(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1", name="Café Noir")

    assert albums(read_api, user, "café") == ["ALB1"]
    assert albums(read_api, user, "café") == ["ALB1"]
    assert albums(read_api, user, "cafe") == []


def test_like_wildcards_in_the_query_are_plain_characters(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1", name="100% Pure")
    play(engine, user, "2026-10-05T11:00:00+00:00", "BBB", "ALB2", name="Under_score")
    play(engine, user, "2026-10-05T12:00:00+00:00", "CCC", "ALB3", name="Back\\slash")

    assert albums(read_api, user, "%") == ["ALB1"]
    assert albums(read_api, user, "_") == ["ALB2"]
    assert albums(read_api, user, "\\") == ["ALB3"]
    assert albums(read_api, user, "100%") == ["ALB1"]


def test_songs_are_found_by_title_artist_or_album_name(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1", title="Low Tide")
    imported(
        engine,
        user,
        "2024-03-02T09:00:00+00:00",
        "BBB",
        title="Harbour Lights",
        album="Old Coast",
        artist="Mira Vale",
    )

    assert tracks(read_api, user, "tide") == ["id:AAA"]
    assert tracks(read_api, user, "mira") == ["id:BBB"]
    assert tracks(read_api, user, "old coast") == ["id:BBB"]
    assert search(read_api, user, "harbour")["tracks"]["items"] == [
        {
            "item_key": "id:BBB",
            "title": "Harbour Lights",
            "creators": ["Mira Vale"],
            "album": "Old Coast",
            "album_id": None,
            "artwork_url": None,
            "comment": None,
            "play_count": 1,
            "first_played_at": "2024-03-02T09:00:00.000Z",
            "last_played_at": "2024-03-02T09:00:00.000Z",
            "last_played_local_date": "2024-03-02",
        }
    ]


def test_a_song_shows_its_latest_names_and_the_album_its_plays_resolve_to(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    # Imported first, under an older title; then played live with an album ID.
    imported(
        engine,
        user,
        "2024-03-02T09:00:00+00:00",
        "AAA",
        title="Low Tide (Demo)",
        album="Album ALB1",
        artist="Nova Kett",
    )
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1", title="Low Tide")

    found = search(read_api, user, "demo")["tracks"]["items"]

    assert [(t["title"], t["album_id"], t["play_count"]) for t in found] == [
        ("Low Tide", "ALB1", 2)
    ]
    assert found[0]["first_played_at"] == "2024-03-02T09:00:00.000Z"
    assert found[0]["last_played_at"] == "2026-10-05T10:00:00.000Z"
    # The imported play counts for the album too.
    assert search(read_api, user, "alb1")["albums"]["items"][0]["play_count"] == 2


def test_most_played_first_then_most_recent(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-01T10:00:00+00:00", "AAA", "ALB1", title="Song A")
    play(engine, user, "2026-10-02T10:00:00+00:00", "BBB", "ALB2", title="Song B")
    play(engine, user, "2026-10-03T10:00:00+00:00", "BBB", "ALB2", title="Song B")
    play(engine, user, "2026-10-04T10:00:00+00:00", "CCC", "ALB3", title="Song C")

    assert albums(read_api, user, "album") == ["ALB2", "ALB3", "ALB1"]
    assert tracks(read_api, user, "song") == ["id:BBB", "id:CCC", "id:AAA"]


def test_singles_and_eps_are_found_with_their_release_type(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "SGL1", album_type="single")
    play(engine, user, "2026-10-05T11:00:00+00:00", "BBB", "EP1", album_type="single", total=5)

    found = search(read_api, user, "album")["albums"]["items"]

    assert {(a["album_id"], a["release_type"]) for a in found} == {
        ("SGL1", "single"),
        ("EP1", "ep"),
    }


def test_a_comment_is_found_even_without_plays(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO album_comment (user_id, album_id, comment) VALUES (:u, 'GONE', :c)"),
            {"u": user.id, "c": SYNTHETIC_COMMENT},
        )

    assert search(read_api, user, "5d2c")["albums"]["items"] == [
        {
            "album_id": "GONE",
            "name": None,
            "artists": [],
            "artwork_url": None,
            "release_type": "album",
            "rating": None,
            "comment": SYNTHETIC_COMMENT,
            "play_count": 0,
            "last_played_at": None,
            "last_played_local_date": None,
        }
    ]


def test_search_is_per_user(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    other = make_user(engine)
    play(engine, other, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    comment(read_api, other, "ALB1", SYNTHETIC_COMMENT)

    body = search(read_api, user, "album")
    assert body["albums"] == {"count": 0, "items": []}
    assert body["tracks"] == {"count": 0, "items": []}
    assert albums(read_api, user, "5d2c") == []


def test_count_is_every_match_and_limit_cuts_the_items(engine: Engine) -> None:
    user = make_user(engine)
    for n in range(3):
        play(engine, user, f"2026-10-0{n + 1}T10:00:00+00:00", f"T{n}", f"ALB{n}")

    with engine.connect() as conn:
        found = read_search(
            conn, user_id=user.id, words=search_words("album"), time_zone="UTC", limit=2
        )

    assert (found.album_count, len(found.albums)) == (3, 2)
    assert (found.track_count, len(found.tracks)) == (3, 2)


@pytest.mark.parametrize(
    ("q", "code"),
    [
        (None, "search_query_missing"),
        ("", "search_query_missing"),
        ("   \t", "search_query_missing"),
        ("x" * 201, "search_query_too_long"),
        (" ".join(["5d2c"] * 11), "search_query_too_long"),
        ("5d2c\x00", "invalid_parameter"),
    ],
)
def test_an_unusable_query_is_refused_without_repeating_it(
    read_api: TestClient, engine: Engine, q: str | None, code: str
) -> None:  # PRD P12
    user = make_user(engine)
    params = {} if q is None else {"q": q}

    response = read_api.get("/v1/search", params=params, headers=user.auth)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert "5d2c" not in response.text


def test_the_longest_query_is_searched(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    assert read_api.get("/v1/search", params={"q": "x" * 200}, headers=user.auth).status_code == 200
    words = " ".join(["x"] * 10)
    assert read_api.get("/v1/search", params={"q": words}, headers=user.auth).status_code == 200


def test_no_log_line_holds_the_query_or_what_it_found(
    read_api: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:  # PRD P12
    caplog.set_level(logging.DEBUG)
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1", name="Album 5d2c")
    comment(read_api, user, "ALB1", SYNTHETIC_COMMENT)

    assert albums(read_api, user, "5d2c bridge") == ["ALB1"]
    refused = read_api.get("/v1/search", params={"q": "5d2c\x00"}, headers=user.auth)
    assert refused.status_code == 422

    access = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert any("route=/v1/search status=200" in line for line in access)
    assert "5d2c" not in caplog.text


def test_search_needs_a_session(read_api: TestClient) -> None:
    assert read_api.get("/v1/search", params={"q": "album"}).status_code == 401
