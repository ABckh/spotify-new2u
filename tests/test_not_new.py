""" "Not new" marks for "New to you" (WHO-123, WHO-124).

``PUT`` / ``DELETE /v1/new-to-you/not-new/{kind}/{item_id}``, and how a mark
changes ``GET /v1/new-to-you`` and the album tracks list. The read clock is
``read_helpers.NOW`` (2026-10-11 12:00 UTC); the display zone is Europe/Warsaw.
"""

from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from read_helpers import User, add_play, album_extras, get, make_user


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


def mark_path(kind: str, item_id: str) -> str:
    return f"/v1/new-to-you/not-new/{kind}/{quote(item_id, safe=':')}"


def mark(api: TestClient, user: User, kind: str, item_id: str) -> None:
    response = api.put(mark_path(kind, item_id), headers=user.auth)
    assert response.status_code == 204, response.text
    assert response.content == b""


def unmark(api: TestClient, user: User, kind: str, item_id: str) -> None:
    response = api.delete(mark_path(kind, item_id), headers=user.auth)
    assert response.status_code == 204, response.text
    assert response.content == b""


def october(api: TestClient, user: User) -> dict[str, Any]:
    return get(api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")


def new_items(body: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(album ids, single and EP ids, track item keys), and each ``count`` matches."""
    albums = [a["album_id"] for a in body["albums"]["items"]]
    singles = [a["album_id"] for a in body["singles"]["items"]]
    tracks = [t["item_key"] for t in body["tracks"]["items"]]
    assert [body[k]["count"] for k in ("albums", "singles", "tracks")] == [
        len(albums),
        len(singles),
        len(tracks),
    ]
    return albums, singles, tracks


def tracks_is_new(api: TestClient, user: User, album: str) -> dict[str, bool]:
    body = get(
        api, user, f"/v1/new-to-you/albums/{album}/tracks", period="month", anchor="2026-10-01"
    )
    return {t["item_key"]: t["is_new"] for t in body["tracks"]}


def marks_of(engine: Engine, user: User) -> list[tuple[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT kind, item_id FROM not_new_mark WHERE user_id = :u ORDER BY 1, 2"),
            {"u": user.id},
        )
        return [(r.kind, r.item_id) for r in rows]


def two_albums(engine: Engine, user: User) -> None:
    """ALB1 by Nova Kett feat. Pine Arc; ALB2 by Rho Vale. All new in October."""
    play(engine, user, "2026-10-02T18:00:00Z", "AAA", by=("Nova Kett", "Pine Arc"), album="ALB1")
    play(engine, user, "2026-10-03T18:00:00Z", "BBB", album="ALB1")
    play(engine, user, "2026-10-04T18:00:00Z", "CCC", by=("Rho Vale",), album="ALB2")


# --- each kind ------------------------------------------------------------------


def test_a_marked_track_is_not_new_and_nothing_else_changes(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    two_albums(engine, user)

    mark(read_api, user, "track", "id:AAA")

    assert new_items(october(read_api, user)) == (
        ["ALB2", "ALB1"],
        [],
        ["id:CCC", "id:BBB"],
    )
    assert tracks_is_new(read_api, user, "ALB1") == {"id:AAA": False, "id:BBB": True}


def test_a_marked_album_hides_the_album_and_its_tracks(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    two_albums(engine, user)
    play(engine, user, "2026-10-05T18:00:00Z", "DDD", by=("Nova Kett",), album="ALB3")

    mark(read_api, user, "album", "ALB1")

    assert new_items(october(read_api, user)) == (
        ["ALB3", "ALB2"],
        [],
        ["id:DDD", "id:CCC"],
    )
    assert tracks_is_new(read_api, user, "ALB1") == {"id:AAA": False, "id:BBB": False}
    assert tracks_is_new(read_api, user, "ALB3") == {"id:DDD": True}


def test_a_track_of_a_marked_album_first_played_after_the_mark_is_not_new(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-02T18:00:00Z", "AAA", album="ALB1")
    mark(read_api, user, "album", "ALB1")

    play(engine, user, "2026-10-10T18:00:00Z", "EEE", by=("Kai Moor",), album="ALB1")

    body = october(read_api, user)
    assert new_items(body) == ([], [], [])
    assert body["nothing_new"] is True
    assert tracks_is_new(read_api, user, "ALB1") == {"id:AAA": False, "id:EEE": False}


def test_a_mark_holds_in_every_period(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-09-10T18:00:00Z", "AAA", album="ALB1")
    play(engine, user, "2026-10-02T18:00:00Z", "BBB", by=("Rho Vale",), album="ALB2")

    mark(read_api, user, "album", "ALB1")

    september = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-09-01")
    assert new_items(september) == ([], [], [])
    assert (september["nothing_new"], september["is_empty"]) == (True, False)
    assert new_items(october(read_api, user)) == (["ALB2"], [], ["id:BBB"])


def test_counts_stay_full_when_show_all_is_off(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    for n in range(12):
        play(engine, user, f"2026-10-0{1 + n % 9}T1{n % 10}:00:00Z", f"T{n:02d}", by=(f"A{n}",))
    mark(read_api, user, "track", "id:T00")

    body = october(read_api, user)

    assert (body["tracks"]["count"], len(body["tracks"]["items"])) == (11, 10)


# --- undo and idempotency -------------------------------------------------------


def test_undo_of_an_album_mark_brings_back_its_tracks(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    two_albums(engine, user)
    before = new_items(october(read_api, user))
    mark(read_api, user, "album", "ALB1")

    unmark(read_api, user, "album", "ALB1")

    assert new_items(october(read_api, user)) == before
    assert tracks_is_new(read_api, user, "ALB1") == {"id:AAA": True, "id:BBB": True}
    assert marks_of(engine, user) == []


@pytest.mark.parametrize(("kind", "item_id"), [("album", "ALB1"), ("track", "id:AAA")])
def test_put_and_delete_are_idempotent(
    read_api: TestClient, engine: Engine, kind: str, item_id: str
) -> None:
    user = make_user(engine)
    two_albums(engine, user)

    mark(read_api, user, kind, item_id)
    mark(read_api, user, kind, item_id)
    assert marks_of(engine, user) == [(kind, item_id)]

    unmark(read_api, user, kind, item_id)
    unmark(read_api, user, kind, item_id)
    assert marks_of(engine, user) == []


# --- validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/new-to-you/not-new/playlist/ALB1",
        "/v1/new-to-you/not-new/Album/ALB1",
        "/v1/new-to-you/not-new/album/ALB-1",
        "/v1/new-to-you/not-new/album/" + "A" * 65,
        "/v1/new-to-you/not-new/track/AAA",
        "/v1/new-to-you/not-new/track/id:AAA%27--",
        "/v1/new-to-you/not-new/track/nt:XYZ",
        # There is no artist list, so no artist marks (``not_new.MARKABLE``).
        "/v1/new-to-you/not-new/artist/Nova%20Kett",
        "/v1/new-to-you/not-new/artist/AC%2FDC",
    ],
)
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_a_bad_kind_or_item_id_is_422_and_writes_nothing(
    read_api: TestClient, engine: Engine, method: str, path: str
) -> None:
    user = make_user(engine)
    two_albums(engine, user)

    response = read_api.request(method, path, headers=user.auth)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_parameter"
    assert marks_of(engine, user) == []


@pytest.mark.parametrize(
    ("kind", "item_id"),
    [
        ("album", "NOPE"),
        ("track", "id:NOPE"),
    ],
)
def test_marking_an_item_the_user_never_played_is_404(
    read_api: TestClient, engine: Engine, kind: str, item_id: str
) -> None:
    user = make_user(engine)
    two_albums(engine, user)

    response = read_api.put(mark_path(kind, item_id), headers=user.auth)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    assert marks_of(engine, user) == []


# --- user isolation and personal data ------------------------------------------


def test_a_mark_is_per_user(read_api: TestClient, engine: Engine) -> None:
    marker, other = make_user(engine), make_user(engine)
    two_albums(engine, marker)
    two_albums(engine, other)
    before = new_items(october(read_api, other))

    mark(read_api, marker, "album", "ALB1")
    mark(read_api, marker, "track", "id:CCC")

    assert new_items(october(read_api, other)) == before
    assert marks_of(engine, other) == []
    # Undo by the other user removes nothing of the marker's.
    unmark(read_api, other, "album", "ALB1")
    assert len(marks_of(engine, marker)) == 2


def test_a_user_cannot_mark_an_item_only_another_user_played(
    read_api: TestClient, engine: Engine
) -> None:
    attacker, victim = make_user(engine), make_user(engine)
    two_albums(engine, victim)

    response = read_api.put(mark_path("album", "ALB1"), headers=attacker.auth)

    assert response.status_code == 404
    assert marks_of(engine, attacker) == []


def test_marks_are_exported_and_deleted_with_the_account(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    two_albums(engine, user)
    mark(read_api, user, "album", "ALB1")
    mark(read_api, user, "track", "id:CCC")

    response = read_api.get("/v1/export", headers=user.auth)
    assert response.status_code == 200
    exported = response.json()["not_new_marks"]
    assert sorted((m["kind"], m["item_id"]) for m in exported) == [
        ("album", "ALB1"),
        ("track", "id:CCC"),
    ]
    assert all(m["user_id"] == str(user.id) and m["created_at"].endswith("Z") for m in exported)

    assert read_api.delete("/v1/me", headers=user.auth).status_code == 204
    assert marks_of(engine, user) == []
