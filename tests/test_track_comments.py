"""Track comments: the user's own words about a track, per user, all time.

``PUT`` / ``DELETE /v1/tracks/{item_key}/comment``, and the ``comment`` on every
play of the timeline, every track of an album screen and every song of
search, which also finds a song by its comment. The rules are the album
comment's: 1 to 2000 characters, not blank, no NUL, only on a track the user
played, never in a log line or an error message (PRD P12). The read clock is
``read_helpers.NOW`` (2026-10-11 12:00 UTC). Every value is synthetic.
"""

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from read_helpers import User, add_play, album_extras, get, make_user

SYNTHETIC_COMMENT = "Synthetic track comment 7e1f: the bridge at 2:10"
MONTH = {"period": "month", "anchor": "2026-10-01"}


def play(engine: Engine, user: User, at: str, item: str, album: str = "ALB1") -> None:
    add_play(
        engine,
        user,
        at,
        item=item,
        collection=f"Album {album}",
        external_ids={"spotify_album_id": album},
        provider_extras=album_extras(2),
    )


def comment(api: TestClient, user: User, item_key: str, value: Any) -> Any:
    return api.put(f"/v1/tracks/{item_key}/comment", json={"comment": value}, headers=user.auth)


def uncomment(api: TestClient, user: User, item_key: str) -> Any:
    return api.delete(f"/v1/tracks/{item_key}/comment", headers=user.auth)


def on_album(api: TestClient, user: User) -> dict[str, str | None]:
    tracks = get(api, user, "/v1/albums/ALB1/tracks", **MONTH)["tracks"]
    return {t["item_key"]: t["comment"] for t in tracks}


def stored(engine: Engine, user: User) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT item_key, comment FROM track_comment WHERE user_id = :u ORDER BY 1"),
            {"u": user.id},
        )
        return [(r.item_key, r.comment) for r in rows]


def test_a_comment_is_set_replaced_and_cleared(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA")
    play(engine, user, "2026-10-05T11:00:00+00:00", "BBB")

    assert comment(read_api, user, "id:AAA", "First take").status_code == 204
    assert on_album(read_api, user) == {"id:AAA": "First take", "id:BBB": None}
    assert comment(read_api, user, "id:AAA", SYNTHETIC_COMMENT).status_code == 204
    assert stored(engine, user) == [("id:AAA", SYNTHETIC_COMMENT)]

    assert uncomment(read_api, user, "id:AAA").status_code == 204
    assert on_album(read_api, user) == {"id:AAA": None, "id:BBB": None}
    assert uncomment(read_api, user, "id:AAA").status_code == 204


def test_every_play_of_the_track_carries_its_comment(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA")
    play(engine, user, "2026-10-06T10:00:00+00:00", "AAA")
    play(engine, user, "2026-10-06T11:00:00+00:00", "BBB")
    comment(read_api, user, "id:AAA", SYNTHETIC_COMMENT)

    events = get(read_api, user, "/v1/timeline", **MONTH)["events"]

    assert [(e["item_key"], e["comment"]) for e in events] == [
        ("id:BBB", None),
        ("id:AAA", SYNTHETIC_COMMENT),
        ("id:AAA", SYNTHETIC_COMMENT),
    ]


def test_search_finds_a_song_by_its_comment_and_shows_it(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA")
    play(engine, user, "2026-10-05T11:00:00+00:00", "BBB")
    comment(read_api, user, "id:AAA", SYNTHETIC_COMMENT)

    by_comment = get(read_api, user, "/v1/search", q="bridge 2:10")["tracks"]["items"]
    by_title = get(read_api, user, "/v1/search", q="track")["tracks"]["items"]

    assert [(t["item_key"], t["comment"]) for t in by_comment] == [("id:AAA", SYNTHETIC_COMMENT)]
    assert {t["item_key"]: t["comment"] for t in by_title} == {
        "id:AAA": SYNTHETIC_COMMENT,
        "id:BBB": None,
    }


def test_a_track_with_no_play_cannot_get_a_comment(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    other = make_user(engine)
    play(engine, other, "2026-10-05T10:00:00+00:00", "AAA")

    assert comment(read_api, user, "id:AAA", "Never heard it").status_code == 404
    assert stored(engine, user) == []


@pytest.mark.parametrize("item_key", ["AAA", "id:", "id:A-B", "nt:xyz", "id:a/b", "id:" + "A" * 65])
def test_a_key_that_is_not_a_track_key_is_refused(
    read_api: TestClient, engine: Engine, item_key: str
) -> None:
    user = make_user(engine)

    assert comment(read_api, user, item_key, "Words").status_code == 422
    assert uncomment(read_api, user, item_key).status_code == 422


@pytest.mark.parametrize("value", ["", "   ", "x" * 2001, "7e1f\x00", 5, None])
def test_an_unusable_comment_is_refused_without_repeating_it(
    read_api: TestClient, engine: Engine, value: Any
) -> None:  # PRD P12
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA")

    response = comment(read_api, user, "id:AAA", value)

    assert response.status_code == 422
    assert "7e1f" not in response.text
    assert stored(engine, user) == []


def test_comments_are_per_user(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    other = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA")
    play(engine, other, "2026-10-05T10:00:00+00:00", "AAA")
    comment(read_api, user, "id:AAA", "Mine")

    assert on_album(read_api, other) == {"id:AAA": None}
    uncomment(read_api, other, "id:AAA")
    assert on_album(read_api, user) == {"id:AAA": "Mine"}


def test_no_log_line_holds_the_comment(
    read_api: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:  # PRD P12
    caplog.set_level(logging.DEBUG)
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA")

    assert comment(read_api, user, "id:AAA", SYNTHETIC_COMMENT).status_code == 204
    get(read_api, user, "/v1/timeline", **MONTH)
    get(read_api, user, "/v1/albums/ALB1/tracks", **MONTH)
    assert comment(read_api, user, "id:AAA", SYNTHETIC_COMMENT + "\x00").status_code == 422
    assert uncomment(read_api, user, "id:AAA").status_code == 204

    access = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert any("route=/v1/tracks/{item_key:path}/comment status=204" in line for line in access)
    assert "7e1f" not in caplog.text


def test_track_comments_need_a_session(read_api: TestClient) -> None:
    assert read_api.put("/v1/tracks/id:AAA/comment", json={"comment": "x"}).status_code == 401
    assert read_api.delete("/v1/tracks/id:AAA/comment").status_code == 401
