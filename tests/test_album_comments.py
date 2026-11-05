"""Album comments (``app.read.ratings``): the user's own words, per user, all time.

``PUT`` / ``DELETE /v1/albums/{album_id}/comment``, the ``comment`` on every
album item, and the comments in ``GET /v1/ratings/albums``. A comment and a
rating are independent: each exists alone or with the other, and setting or
clearing one never changes the other. A comment never reaches a log line or an
error message (PRD P12). The read clock is ``read_helpers.NOW`` (2026-10-11
12:00 UTC). Every value is synthetic.
"""

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.read.ratings import MAX_COMMENT_LENGTH
from read_helpers import User, add_play, album_extras, get, make_user

SYNTHETIC_COMMENT = "Synthetic comment 5d2c: the bridge on track 3"
MONTH = {"period": "month", "anchor": "2026-10-01"}


def play(
    engine: Engine, user: User, at: str, item: str, album: str, *, single: bool = False
) -> None:
    extras = album_extras(1)
    if single:
        extras["spotify_album_type"] = "single"
    add_play(
        engine,
        user,
        at,
        item=item,
        creators=("Nova Kett",),
        collection=f"Album {album}",
        artwork_url="https://i.scdn.co/image/synthetic-300",
        external_ids={"spotify_album_id": album},
        provider_extras=extras,
    )


def comment(api: TestClient, user: User, album: str, value: Any) -> Any:
    return api.put(f"/v1/albums/{album}/comment", json={"comment": value}, headers=user.auth)


def rate(api: TestClient, user: User, album: str, rating: int) -> Any:
    return api.put(f"/v1/albums/{album}/rating", json={"rating": rating}, headers=user.auth)


def uncomment(api: TestClient, user: User, album: str) -> Any:
    return api.delete(f"/v1/albums/{album}/comment", headers=user.auth)


def unrate(api: TestClient, user: User, album: str) -> Any:
    return api.delete(f"/v1/albums/{album}/rating", headers=user.auth)


def own(api: TestClient, user: User, album: str) -> tuple[int | None, str | None]:
    """The rating and comment the album screen shows."""
    body = get(api, user, f"/v1/albums/{album}/tracks", **MONTH)["album"]
    return body["rating"], body["comment"]


def stored_comments(engine: Engine, user: User) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT album_id, comment FROM album_comment WHERE user_id = :u ORDER BY 1"),
            {"u": user.id},
        )
        return [(r.album_id, r.comment) for r in rows]


def test_a_comment_is_set_replaced_and_cleared(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    assert comment(read_api, user, "ALB1", "First take").status_code == 204
    assert own(read_api, user, "ALB1") == (None, "First take")
    assert comment(read_api, user, "ALB1", SYNTHETIC_COMMENT).status_code == 204
    assert own(read_api, user, "ALB1") == (None, SYNTHETIC_COMMENT)

    assert uncomment(read_api, user, "ALB1").status_code == 204
    assert own(read_api, user, "ALB1") == (None, None)
    assert stored_comments(engine, user) == []
    # Clearing again changes nothing.
    assert uncomment(read_api, user, "ALB1").status_code == 204


def test_rating_and_comment_are_independent(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    rate(read_api, user, "ALB1", 8)
    assert own(read_api, user, "ALB1") == (8, None)
    comment(read_api, user, "ALB1", "Both")
    assert own(read_api, user, "ALB1") == (8, "Both")
    # Changing one leaves the other as it was.
    rate(read_api, user, "ALB1", 6)
    assert own(read_api, user, "ALB1") == (6, "Both")
    comment(read_api, user, "ALB1", "Still both")
    assert own(read_api, user, "ALB1") == (6, "Still both")
    # Clearing one leaves the other.
    assert unrate(read_api, user, "ALB1").status_code == 204
    assert own(read_api, user, "ALB1") == (None, "Still both")
    rate(read_api, user, "ALB1", 9)
    assert uncomment(read_api, user, "ALB1").status_code == 204
    assert own(read_api, user, "ALB1") == (9, None)


def test_every_album_item_carries_the_comment(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    play(engine, user, "2026-10-06T10:00:00+00:00", "BBB", "ALB2")
    play(engine, user, "2026-10-07T10:00:00+00:00", "CCC", "SGL1", single=True)
    comment(read_api, user, "ALB1", "Album words")
    comment(read_api, user, "SGL1", "Single words")
    rate(read_api, user, "ALB2", 7)

    new = get(read_api, user, "/v1/new-to-you", **MONTH)
    assert {a["album_id"]: (a["rating"], a["comment"]) for a in new["albums"]["items"]} == {
        "ALB1": (None, "Album words"),
        "ALB2": (7, None),
    }
    assert [(s["album_id"], s["comment"]) for s in new["singles"]["items"]] == [
        ("SGL1", "Single words")
    ]
    played = get(read_api, user, "/v1/albums", **MONTH)["albums"]["items"]
    assert {a["album_id"]: a["comment"] for a in played} == {"ALB1": "Album words", "ALB2": None}
    tracks = get(read_api, user, "/v1/new-to-you/albums/ALB1/tracks", **MONTH)
    assert tracks["album"]["comment"] == "Album words"


def test_line_breaks_and_any_script_are_kept_exactly(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    text_ = '  Ünïcödé ☃ «quoted» "too"\nSecond line\r\n\tthird 🎵  '

    assert comment(read_api, user, "ALB1", text_).status_code == 204

    assert own(read_api, user, "ALB1") == (None, text_)


def test_the_length_is_counted_in_characters_up_to_the_bound(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    longest = "🎵" * MAX_COMMENT_LENGTH  # 4 bytes each in UTF-8, one character

    assert comment(read_api, user, "ALB1", longest).status_code == 204
    assert stored_comments(engine, user) == [("ALB1", longest)]
    assert comment(read_api, user, "ALB1", "x" * (MAX_COMMENT_LENGTH + 1)).status_code == 422
    assert stored_comments(engine, user) == [("ALB1", longest)]


@pytest.mark.parametrize(
    "value",
    ["", "   ", "\n\t ", "nul\x00inside", 5, None, ["a list"], {"an": "object"}],
)
def test_a_blank_unstorable_or_non_text_comment_is_refused(
    read_api: TestClient, engine: Engine, value: Any
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    comment(read_api, user, "ALB1", "Kept")

    response = comment(read_api, user, "ALB1", value)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_parameter"
    # A refused comment leaves the one before.
    assert stored_comments(engine, user) == [("ALB1", "Kept")]


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        # A lone surrogate has no UTF-8 form: refused, not a 500.
        (b'{"comment": "Synthetic comment 5d2c \\ud800"}', "application/json"),
        (b'{"text": "Synthetic comment 5d2c"}', "application/json"),
        (b'"Synthetic comment 5d2c"', "application/json"),
        (b'["Synthetic comment 5d2c"]', "application/json"),
        (b"Synthetic comment 5d2c", "text/plain"),
        (b"", "application/json"),
    ],
)
def test_no_refusal_repeats_the_comment(
    read_api: TestClient, engine: Engine, content: bytes, content_type: str
) -> None:  # PRD P12
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    response = read_api.put(
        "/v1/albums/ALB1/comment",
        content=content,
        headers={**user.auth, "Content-Type": content_type},
    )

    assert response.status_code == 422
    assert "5d2c" not in response.text
    assert stored_comments(engine, user) == []


def test_malformed_json_is_refused_without_repeating_it(
    read_api: TestClient, engine: Engine
) -> None:  # PRD P12
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    response = read_api.put(
        "/v1/albums/ALB1/comment",
        content=b'{"comment": "Synthetic comment 5d2c',
        headers={**user.auth, "Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "5d2c" not in response.text


def test_no_log_line_holds_the_comment(
    read_api: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:  # PRD P12
    caplog.set_level(logging.DEBUG)
    user = make_user(engine)
    other = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    assert comment(read_api, user, "ALB1", SYNTHETIC_COMMENT).status_code == 204
    get(read_api, user, "/v1/ratings/albums", commented="true")
    get(read_api, user, "/v1/albums", **MONTH)
    assert comment(read_api, other, "ALB1", SYNTHETIC_COMMENT).status_code == 404
    assert comment(read_api, user, "ALB1", SYNTHETIC_COMMENT + "\x00").status_code == 422
    assert comment(read_api, user, "ALB1", "x" * 3000 + SYNTHETIC_COMMENT).status_code == 422
    assert uncomment(read_api, user, "ALB1").status_code == 204

    access = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert any("route=/v1/albums/{album_id}/comment status=204" in line for line in access)
    assert "5d2c" not in caplog.text


def test_an_album_with_no_play_cannot_get_a_comment(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    other = make_user(engine)
    play(engine, other, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")

    assert comment(read_api, user, "ALB1", "Never heard it").status_code == 404
    assert comment(read_api, user, "not-an-id!", "Words").status_code == 422
    assert uncomment(read_api, user, "not-an-id!").status_code == 422
    assert stored_comments(engine, user) == []


def test_comments_are_per_user(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    other = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    play(engine, other, "2026-10-05T10:00:00+00:00", "AAA", "ALB1")
    comment(read_api, user, "ALB1", "Mine")

    assert own(read_api, other, "ALB1") == (None, None)
    assert get(read_api, other, "/v1/ratings/albums", commented="true")["items"] == []
    # The other user's clear does not reach this user's comment.
    uncomment(read_api, other, "ALB1")
    assert own(read_api, user, "ALB1") == (None, "Mine")


def test_comments_need_a_session(read_api: TestClient) -> None:
    assert read_api.put("/v1/albums/ALB1/comment", json={"comment": "x"}).status_code == 401
    assert read_api.delete("/v1/albums/ALB1/comment").status_code == 401


# --- the all-time list ---------------------------------------------------------


def listed(api: TestClient, user: User, **params: str) -> list[tuple[str, int | None, str | None]]:
    body = get(api, user, "/v1/ratings/albums", **params)
    assert body["count"] == len(body["items"])
    return [(a["album_id"], a["rating"], a["comment"]) for a in body["items"]]


def test_the_all_time_list_carries_each_rated_releases_comment(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-05T10:00:00+00:00", "AAA", "TOP")
    play(engine, user, "2026-10-05T11:00:00+00:00", "BBB", "MID")
    play(engine, user, "2026-10-05T12:00:00+00:00", "CCC", "WORDS")
    rate(read_api, user, "TOP", 10)
    rate(read_api, user, "MID", 5)
    comment(read_api, user, "TOP", "Best of the year")
    comment(read_api, user, "WORDS", "Not sure yet")

    # Without commented=true, only what is rated, as before: rating is never null.
    assert listed(read_api, user) == [("TOP", 10, "Best of the year"), ("MID", 5, None)]
    assert listed(read_api, user, commented="false") == listed(read_api, user)


def test_commented_adds_releases_with_only_a_comment_after_the_rated_ones(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for item, album in (("AAA", "RATED"), ("BBB", "OLDER"), ("CCC", "NEWER"), ("DDD", "SGL")):
        play(engine, user, "2026-10-05T10:00:00+00:00", item, album, single=album == "SGL")
    play(engine, user, "2026-10-05T11:00:00+00:00", "EEE", "NOTHING")  # neither: never listed
    comment(read_api, user, "OLDER", "Older words")
    rate(read_api, user, "RATED", 3)
    comment(read_api, user, "SGL", "Single words")
    comment(read_api, user, "NEWER", "Newer words")
    with engine.begin() as conn:  # a clear order, whatever the clock
        conn.execute(
            text(
                "UPDATE album_comment SET commented_at = now() - "
                "CASE album_id WHEN 'OLDER' THEN interval '2 days' "
                "WHEN 'SGL' THEN interval '1 day' ELSE interval '0' END"
            )
        )

    body = get(read_api, user, "/v1/ratings/albums", commented="true")

    # Rated first, however low; then the most recently commented.
    assert [(a["album_id"], a["rating"], a["comment"]) for a in body["items"]] == [
        ("RATED", 3, None),
        ("NEWER", None, "Newer words"),
        ("SGL", None, "Single words"),
        ("OLDER", None, "Older words"),
    ]
    rated, newer, single, _ = body["items"]
    assert rated["rated_at"].endswith("Z") and rated["commented_at"] is None
    assert newer["rated_at"] is None and newer["commented_at"].endswith("Z")
    assert newer["name"] == "Album NEWER"
    assert newer["artists"] == ["Nova Kett"]
    assert newer["artwork_url"] == "https://i.scdn.co/image/synthetic-300"
    assert newer["release_type"] == "album"
    assert single["release_type"] == "single"


def test_commented_is_true_or_false_exactly(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    for value in ("yes", "1", "True", ""):
        response = read_api.get(
            "/v1/ratings/albums", params={"commented": value}, headers=user.auth
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_parameter"
