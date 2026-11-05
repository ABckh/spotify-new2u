"""Plays stored without an album ID take one at read time (``app.read.album_sql``).

The history file import (WHO-104) has track IDs and album names but no album
IDs. Such a play takes the album of the user's latest play of the same track
that has one, or else of their latest play with the same album name and
artist, so years of imported listening count for albums. The read clock is
``read_helpers.NOW`` (2026-10-11 12:00 UTC). Every value is synthetic.
"""

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from read_helpers import User, add_play, album_extras, get, make_user


def live(engine: Engine, user: User, at: str, item: str, *, total: int) -> None:
    """A polled play: album ``ALB1`` named "Album One" by Nova Kett."""
    add_play(
        engine,
        user,
        at,
        item=item,
        creators=("Nova Kett",),
        collection="Album One",
        external_ids={"spotify_album_id": "ALB1"},
        provider_extras=album_extras(total),
    )


def imported(engine: Engine, user: User, at: str, item: str, *, album: str = "Album One") -> None:
    """A play with no album ID, as the history file import stores it."""
    add_play(engine, user, at, item=item, creators=(" Nova Kett",), collection=album)


def new_albums(api: TestClient, user: User, month: str) -> list[str]:
    body = get(api, user, "/v1/new-to-you", period="month", anchor=month)
    return [a["album_id"] for a in body["albums"]["items"]]


def test_an_imported_play_of_the_same_track_makes_the_album_old(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    imported(engine, user, "2025-05-03T10:00:00+00:00", "AAA")
    live(engine, user, "2026-10-05T10:00:00+00:00", "AAA", total=1)

    assert new_albums(read_api, user, "2026-10-01") == []
    assert new_albums(read_api, user, "2025-05-01") == ["ALB1"]


def test_imported_plays_of_other_tracks_match_by_album_name_and_artist(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    imported(engine, user, "2025-05-03T10:00:00+00:00", "BBB")
    imported(engine, user, "2025-05-04T10:00:00+00:00", "CCC")
    live(engine, user, "2026-10-05T10:00:00+00:00", "AAA", total=2)

    # Two of its two tracks were played in May 2025, from the file.
    assert new_albums(read_api, user, "2025-05-01") == ["ALB1"]
    assert new_albums(read_api, user, "2026-10-01") == []


def test_a_play_matching_no_album_still_has_none(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    imported(engine, user, "2025-05-03T10:00:00+00:00", "BBB", album="Another Album")
    live(engine, user, "2026-10-05T10:00:00+00:00", "AAA", total=2)

    assert new_albums(read_api, user, "2025-05-01") == []
    assert new_albums(read_api, user, "2026-10-01") == []


def test_another_users_plays_never_lend_an_album(read_api: TestClient, engine: Engine) -> None:
    owner = make_user(engine)
    other = make_user(engine)
    live(engine, other, "2026-10-05T10:00:00+00:00", "AAA", total=1)
    imported(engine, owner, "2025-05-03T10:00:00+00:00", "AAA")

    assert new_albums(read_api, owner, "2025-05-01") == []
