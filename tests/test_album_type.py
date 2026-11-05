"""Spotify's album type: collected, backfilled, and singles left out of album lists (WHO-162).

Owner decision on WHO-159: a single is what Spotify calls one (``album_type``
``single``, EPs included), and it is in no album list or album count. A release
whose type is not stored yet stays an album. All data here is synthetic.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.poller.spotify import poll_connection
from app.spotify.album_artists_backfill import backfill_user
from app.spotify.plays import album_extras
from fake_spotify import FakeSpotify
from read_helpers import User, add_play, get, make_user
from test_poller_spotify import T0, connect_user, plays

TYPE = "spotify_album_type"


# --- collecting -------------------------------------------------------------


@pytest.mark.parametrize("album_type", ["album", "single", "compilation"])
def test_each_spotify_type_is_kept(album_type: str) -> None:
    assert album_extras({"album_type": album_type})[TYPE] == album_type


@pytest.mark.parametrize("album_type", [None, "", "appears_on", "Single", "ep", 1, ["single"]])
def test_any_other_value_is_not_stored(album_type: object) -> None:
    assert TYPE not in album_extras({"album_type": album_type})


def test_no_type_means_no_key() -> None:
    assert TYPE not in album_extras({})


def test_the_poller_stores_the_type(engine: Engine, fake_spotify: FakeSpotify) -> None:
    user = connect_user(engine)
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1), album_type="single")
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=2))

    with engine.connect() as conn:
        extras = conn.execute(text("SELECT provider_extras FROM unified_event")).scalar_one()
    assert extras[TYPE] == "single"


# --- the one-off backfill ---------------------------------------------------


def _polled_user(engine: Engine, fake: FakeSpotify) -> UUID:
    """Three plays on two albums, stored as before WHO-162: no type."""
    user = connect_user(engine)
    fake.play("track-1", "Synthetic 1", T0 + timedelta(minutes=1), album_id="albumA")
    fake.play("track-2", "Synthetic 2", T0 + timedelta(minutes=2), album_id="albumA")
    fake.play("track-3", "Synthetic 3", T0 + timedelta(minutes=3), album_id="albumB")
    poll_connection(engine, fake.client(), user, now=T0 + timedelta(minutes=5))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE unified_event SET provider_extras = provider_extras - :k"), {"k": TYPE}
        )
    fake.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo"]}
    fake.album_types = {"albumA": "single", "albumB": "compilation"}
    return user


def _types(engine: Engine) -> list[tuple[str, Any]]:
    with engine.connect() as conn:
        return [
            (r.a, r.album_type)
            for r in conn.execute(
                text(
                    "SELECT external_ids ->> 'spotify_album_id' AS a, "
                    "provider_extras ->> :k AS album_type "
                    "FROM unified_event ORDER BY occurred_at"
                ),
                {"k": TYPE},
            )
        ]


def test_the_backfill_fills_the_type_and_a_rerun_calls_nothing(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.albums_missing, result.albums_filled, result.rows_filled) == (
        "succeeded",
        2,
        2,
        3,
    )
    assert _types(engine) == [("albumA", "single"), ("albumA", "single"), ("albumB", "compilation")]
    assert len(fake_spotify.calls("/v1/albums/albumA")) == 1
    assert plays(engine) == 3

    before = len(fake_spotify.requests)
    again = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)
    assert (again.status, again.albums_missing) == ("succeeded", 0)
    assert len(fake_spotify.requests) == before


@pytest.mark.parametrize("status", [401, 403])
def test_401_or_403_on_an_album_stops_the_user_at_once(
    engine: Engine, fake_spotify: FakeSpotify, status: int
) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.fail_next("/v1/albums/albumA", httpx.Response(status))

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.error_kind, result.albums_filled) == (
        "failed",
        f"album_http_{status}",
        0,
    )
    assert fake_spotify.calls("/v1/albums/albumB") == []  # not tried after the refusal
    assert _types(engine) == [("albumA", None), ("albumA", None), ("albumB", None)]


# --- reading ----------------------------------------------------------------

# album id -> its stored type; None is a release stored before WHO-162.
RELEASES: dict[str, str | None] = {
    "SINGLE": "single",
    "EP": "single",  # Spotify calls an EP a single
    "ALBUM": "album",
    "COMP": "compilation",
    "UNKNOWN": None,
}
LISTED = ["ALBUM", "COMP", "UNKNOWN"]


def _play_every_release(engine: Engine, user: User) -> None:
    """One play of a 1-track release each, in October: each is listened and new."""
    for n, (album_id, album_type) in enumerate(RELEASES.items()):
        extras: dict[str, object] = {"spotify_album_total_tracks": 1}
        if album_type is not None:
            extras[TYPE] = album_type
        add_play(
            engine,
            user,
            f"2026-10-0{n + 1}T18:00:00Z",
            item=f"T{album_id}",
            creators=(f"Artist {album_id}",),
            collection=f"Album {album_id}",
            external_ids={"spotify_album_id": album_id},
            provider_extras=extras,
        )


def _october(api: TestClient, user: User, path: str) -> dict[str, Any]:
    return get(api, user, path, period="month", anchor="2026-10-01")


def test_singles_are_left_out_of_new_to_you_albums_and_their_count(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    _play_every_release(engine, user)

    body = _october(read_api, user, "/v1/new-to-you")

    assert body["albums"]["count"] == 3
    assert sorted(a["album_id"] for a in body["albums"]["items"]) == sorted(LISTED)
    # The singles are their own list, and their tracks still count.
    assert body["singles"]["count"] == 2
    assert sorted(s["album_id"] for s in body["singles"]["items"]) == ["EP", "SINGLE"]
    assert body["tracks"]["count"] == 5


def test_singles_are_left_out_of_all_albums_and_their_count(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    _play_every_release(engine, user)

    body = _october(read_api, user, "/v1/albums")

    assert body["albums"]["count"] == 3
    assert sorted(a["album_id"] for a in body["albums"]["items"]) == sorted(LISTED)


def test_songs_and_artists_counts_do_not_change(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    _play_every_release(engine, user)

    summary = _october(read_api, user, "/v1/summary")

    assert (summary["totals"]["distinct_tracks"], summary["totals"]["distinct_artists"]) == (5, 5)


def test_the_type_of_the_latest_play_that_has_one_wins(
    read_api: TestClient, engine: Engine
) -> None:
    """Spotify relabelled a release: the newest stored type is the one used."""
    user = make_user(engine)
    for at, item, album_type in [
        ("2026-10-01T18:00:00Z", "T1", "single"),
        ("2026-10-02T18:00:00Z", "T2", "album"),
        ("2026-10-03T18:00:00Z", "T3", None),  # a newer play with no type changes nothing
    ]:
        extras: dict[str, object] = {"spotify_album_total_tracks": 3}
        if album_type is not None:
            extras[TYPE] = album_type
        add_play(
            engine,
            user,
            at,
            item=item,
            collection="Album RELABEL",
            external_ids={"spotify_album_id": "RELABEL"},
            provider_extras=extras,
        )

    body = _october(read_api, user, "/v1/albums")

    assert [a["album_id"] for a in body["albums"]["items"]] == ["RELABEL"]
