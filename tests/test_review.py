""" "Did you already know these?" (``app.read.review``) and bulk "Not new" marks.

``GET /v1/me/review``, ``POST /v1/me/review/done`` and ``POST
/v1/new-to-you/not-new``. The read clock is ``read_helpers.NOW`` (2026-10-11
12:00 UTC); collection starts on 2026-10-05, so the review window runs until
2026-10-19 and the review is offered until 2026-11-02. Every value is synthetic.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.auth import utc_now
from app.api.main import app
from app.spotify.connection import connect
from app.spotify.profile import SpotifyProfile
from read_helpers import NOW, User, add_play, add_session, album_extras, get, make_user

STARTED = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def user(engine: Engine) -> User:
    """A connected account with a session; collection started at ``STARTED``."""
    with engine.begin() as conn:
        user_id = connect(
            conn,
            profile=SpotifyProfile("synthetic-account-1", "Synthetic Listener"),
            refresh_token="synthetic-refresh-0",
            home_timezone="Europe/Warsaw",
            now=STARTED,
            import_recent_history=False,
        ).user_id
        add_session(conn, user_id, "synthetic-review-session")
    return User(user_id, "synthetic-review-session", "Europe/Warsaw")


def play(
    engine: Engine,
    user: User,
    at: str,
    item: str,
    by: str,
    album: str | None = None,
    single: str | None = None,
) -> None:
    """A play on ``album``, or on the 1-track ``single``, or on no release."""
    release = album or single
    extras = album_extras(1) | ({"spotify_album_type": "single"} if single else {})
    add_play(
        engine,
        user,
        at,
        item=item,
        creators=(by,),
        collection=None if release is None else f"Release {release}",
        external_ids=None if release is None else {"spotify_album_id": release},
        provider_extras=None if release is None else extras,
    )


def review(api: TestClient, user: User) -> dict[str, Any]:
    return get(api, user, "/v1/me/review")


def listed(body: dict[str, Any]) -> tuple[list[str], list[str]]:
    albums = [a["album_id"] for a in body["albums"]["items"]]
    singles = [a["album_id"] for a in body["singles"]["items"]]
    assert (body["albums"]["count"], body["singles"]["count"]) == (len(albums), len(singles))
    return albums, singles


def done(api: TestClient, user: User, through: str) -> None:
    response = api.post("/v1/me/review/done", json={"through": through}, headers=user.auth)
    assert response.status_code == 204, response.text


def batch(api: TestClient, user: User, items: list[dict[str, str]]) -> Any:
    return api.post("/v1/new-to-you/not-new", json={"items": items}, headers=user.auth)


def test_lists_albums_and_singles_first_played_in_the_first_two_weeks(
    read_api: TestClient, engine: Engine, user: User
) -> None:
    play(engine, user, "2026-10-04T10:00:00+00:00", "Imported", "Imported Artist", single="S0")
    play(engine, user, "2026-10-06T10:00:00+00:00", "Song1", "First Artist", album="AlbumOne")
    play(engine, user, "2026-10-08T10:00:00+00:00", "Song2", "Second Artist", single="S2")

    body = review(read_api, user)

    assert body["open"] is True
    assert body["window_start"] == "2026-10-04T10:00:00.000Z"  # the earliest stored play
    assert body["window_end"] == "2026-10-19T09:00:00.000Z"
    assert body["through"] == "2026-10-11T12:00:00.000Z"  # now, inside the window
    albums, singles = listed(body)
    assert albums == ["AlbumOne"]
    assert singles == ["S2", "S0"]  # newest first; the imported one is before collection
    assert body["singles"]["items"][0]["release_type"] == "single"


def test_marked_items_are_left_out(read_api: TestClient, engine: Engine, user: User) -> None:
    play(engine, user, "2026-10-06T10:00:00+00:00", "Song1", "First Artist", album="AlbumOne")
    play(engine, user, "2026-10-08T10:00:00+00:00", "Song2", "Second Artist", single="S2")
    play(engine, user, "2026-10-09T10:00:00+00:00", "Song3", "Third Artist", single="S3")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO not_new_mark (user_id, kind, item_id, source) "
                "VALUES (:u, 'album', 'S3', 'spotify_library')"
            ),
            {"u": user.id},
        )

    assert listed(review(read_api, user)) == (["AlbumOne"], ["S2"])


def test_done_keeps_the_rest_new_and_the_next_review_starts_there(
    read_api: TestClient, engine: Engine, user: User
) -> None:
    play(engine, user, "2026-10-06T10:00:00+00:00", "Song1", "First Artist", single="S1")
    through = review(read_api, user)["through"]

    done(read_api, user, through)

    assert listed(review(read_api, user)) == ([], [])
    # Still new in "New to you": the review only remembers what was shown.
    month = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")
    assert [a["album_id"] for a in month["singles"]["items"]] == ["S1"]

    play(engine, user, "2026-10-12T10:00:00+00:00", "Song2", "Later Artist", single="S2")
    app.dependency_overrides[utc_now] = lambda: datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    assert listed(review(read_api, user)) == ([], ["S2"])


def test_done_never_skips_past_now_or_moves_back(
    read_api: TestClient, engine: Engine, user: User
) -> None:
    done(read_api, user, "2030-01-01T00:00:00Z")  # forged: clamped to now
    play(engine, user, "2026-10-12T10:00:00+00:00", "Song2", "Later Artist", single="S2")
    app.dependency_overrides[utc_now] = lambda: datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    assert listed(review(read_api, user)) == ([], ["S2"])

    done(read_api, user, "2026-10-01T00:00:00Z")  # older than before: ignored
    with engine.connect() as conn:
        cleared = conn.execute(
            text("SELECT review_cleared_at FROM spotify_connection WHERE user_id = :u"),
            {"u": user.id},
        ).scalar_one()
    assert cleared == NOW


def test_done_needs_an_instant_with_an_offset(read_api: TestClient, user: User) -> None:
    response = read_api.post(
        "/v1/me/review/done", json={"through": "2026-10-11T12:00:00"}, headers=user.auth
    )
    assert response.status_code == 422


def test_the_review_closes_two_weeks_after_the_window(
    read_api: TestClient, engine: Engine, user: User
) -> None:
    play(engine, user, "2026-10-06T10:00:00+00:00", "Song1", "First Artist", single="S1")
    app.dependency_overrides[utc_now] = lambda: datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    body = review(read_api, user)

    assert body["open"] is False
    assert listed(body) == ([], [])


def test_a_user_with_no_connection_has_no_review(read_api: TestClient, engine: Engine) -> None:
    body = review(read_api, make_user(engine))

    assert body["open"] is False


def test_batch_marks_every_played_item_and_skips_the_rest(
    read_api: TestClient, engine: Engine, user: User
) -> None:
    play(engine, user, "2026-10-06T10:00:00+00:00", "Song1", "First Artist", album="AlbumOne")
    play(engine, user, "2026-10-08T10:00:00+00:00", "Song2", "Second Artist", single="S2")

    response = batch(
        read_api,
        user,
        [
            {"kind": "album", "item_id": "AlbumOne"},
            {"kind": "album", "item_id": "S2"},
            {"kind": "album", "item_id": "S2"},  # repeated: once
            {"kind": "album", "item_id": "NeverPlayed"},
        ],
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"marked": 2, "not_played": 1}
    assert listed(review(read_api, user)) == ([], [])
    with engine.connect() as conn:
        sources = set(
            conn.execute(
                text("SELECT source FROM not_new_mark WHERE user_id = :u"), {"u": user.id}
            ).scalars()
        )
    assert sources == {"user"}


GOOD = {"kind": "track", "item_id": "id:Song1"}


@pytest.mark.parametrize(
    "items",
    [
        [],
        [GOOD] + [{"kind": "album", "item_id": f"A{n}"} for n in range(200)],
        [GOOD, {"kind": "playlist", "item_id": "x"}],
        [GOOD, {"kind": "artist", "item_id": "First Artist"}],  # no artist marks any more
        [GOOD, {"kind": "album", "item_id": "not a spotify id"}],
    ],
)
def test_batch_refuses_bad_input_and_writes_nothing(
    read_api: TestClient, engine: Engine, user: User, items: list[dict[str, str]]
) -> None:
    play(engine, user, "2026-10-06T10:00:00+00:00", "Song1", "First Artist")

    response = batch(read_api, user, items)

    assert response.status_code == 422
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM not_new_mark WHERE user_id = :u"), {"u": user.id}
            ).scalar_one()
            == 0
        )


def test_review_and_batch_need_a_session(read_api: TestClient) -> None:
    assert read_api.get("/v1/me/review").status_code == 401
    assert read_api.post("/v1/me/review/done", json={"through": "x"}).status_code == 401
    assert read_api.post("/v1/new-to-you/not-new", json={"items": []}).status_code == 401
