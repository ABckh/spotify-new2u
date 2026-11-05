"""The known-music baseline (``app.spotify.library_baseline``), against the fake Spotify.

Read once per connection by the poller, stored as ``not_new_mark`` rows with
``source = 'spotify_library'``. Every value is synthetic.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.poller.spotify import poll_connection
from app.spotify.connection import connect
from app.spotify.profile import SpotifyProfile
from fake_spotify import (
    FOLLOWING,
    LIBRARY_SCOPE,
    RECENT,
    SAVED_ALBUMS,
    SAVED_TRACKS,
    TOP,
    FakeSpotify,
    library_album,
    library_track,
)
from read_helpers import User, add_play, add_session, album_extras, get

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)  # the connect
BEFORE = T0 - timedelta(days=30)
AFTER = T0 + timedelta(hours=1)


def connect_user(engine: Engine, now: datetime = T0) -> UUID:
    with engine.begin() as conn:
        return connect(
            conn,
            profile=SpotifyProfile("synthetic-account-1", "Synthetic Listener"),
            refresh_token="synthetic-refresh-0",
            home_timezone="Europe/Warsaw",
            now=now,
            import_recent_history=False,
        ).user_id


def marks(engine: Engine, user_id: UUID) -> set[tuple[str, str, str]]:
    with engine.connect() as conn:
        return {
            (r.kind, r.item_id, r.source)
            for r in conn.execute(
                text("SELECT kind, item_id, source FROM not_new_mark WHERE user_id = :u"),
                {"u": user_id},
            )
        }


def baseline_at(engine: Engine, user_id: UUID) -> datetime | None:
    with engine.connect() as conn:
        value: datetime | None = conn.execute(
            text("SELECT library_baseline_at FROM spotify_connection WHERE user_id = :u"),
            {"u": user_id},
        ).scalar_one()
        return value


def library_calls(fake: FakeSpotify) -> list[httpx.Request]:
    return [
        r
        for r in fake.requests
        if r.url.path.startswith(TOP) or r.url.path in (SAVED_TRACKS, SAVED_ALBUMS, FOLLOWING)
    ]


@pytest.fixture
def library(fake_spotify: FakeSpotify) -> FakeSpotify:
    """A granted library with one item in every source."""
    fake = fake_spotify
    fake.granted_scope = LIBRARY_SCOPE
    fake.top_artists = {"long_term": ["Old Favourite"], "short_term": ["Recent Find"]}
    fake.top_tracks = {"long_term": [library_track("TopTrack", "Top Track Artist")]}
    fake.saved_tracks = [
        (AFTER, library_track("SavedLater", "Later Artist")),
        (BEFORE, library_track("SavedTrack", "Saved Track Artist")),
    ]
    fake.saved_albums = [
        (AFTER, library_album("AlbumLater", "Later Album Artist")),
        (BEFORE, library_album("SavedAlbum", "Saved Album Artist")),
    ]
    fake.followed = ["Followed One"]
    return fake


def test_first_poll_stores_the_library_as_not_new_marks(
    engine: Engine, library: FakeSpotify
) -> None:
    user_id = connect_user(engine)

    assert poll_connection(engine, library.client(), user_id, T0).status == "succeeded"

    assert marks(engine, user_id) == {
        ("artist", "Old Favourite", "spotify_library"),
        ("artist", "Recent Find", "spotify_library"),  # read on the connect day
        ("track", "id:TopTrack", "spotify_library"),
        ("artist", "Top Track Artist", "spotify_library"),
        ("track", "id:SavedTrack", "spotify_library"),
        ("artist", "Saved Track Artist", "spotify_library"),
        ("album", "SavedAlbum", "spotify_library"),
        ("artist", "Saved Album Artist", "spotify_library"),
        ("artist", "Followed One", "spotify_library"),
    }
    assert baseline_at(engine, user_id) == T0


def test_items_saved_after_collection_started_are_not_known(
    engine: Engine, library: FakeSpotify
) -> None:
    user_id = connect_user(engine)
    poll_connection(engine, library.client(), user_id, T0)

    stored = {item_id for _, item_id, _ in marks(engine, user_id)}
    assert {"id:SavedLater", "Later Artist", "AlbumLater", "Later Album Artist"} & stored == set()


def test_a_late_baseline_reads_only_the_long_term_top_lists(
    engine: Engine, library: FakeSpotify
) -> None:
    user_id = connect_user(engine)
    later = T0 + timedelta(days=3)

    poll_connection(engine, library.client(), user_id, later)

    ranges = {r.url.params["time_range"] for r in library.requests if r.url.path.startswith(TOP)}
    assert ranges == {"long_term"}
    assert ("artist", "Recent Find", "spotify_library") not in marks(engine, user_id)
    assert ("artist", "Old Favourite", "spotify_library") in marks(engine, user_id)


def test_a_grant_without_the_library_scopes_reads_nothing(
    engine: Engine, library: FakeSpotify
) -> None:
    library.granted_scope = "user-read-recently-played"
    user_id = connect_user(engine)

    assert poll_connection(engine, library.client(), user_id, T0).status == "succeeded"

    assert library_calls(library) == []
    assert marks(engine, user_id) == set()
    assert baseline_at(engine, user_id) is None


def test_the_baseline_is_read_once(engine: Engine, library: FakeSpotify) -> None:
    user_id = connect_user(engine)
    poll_connection(engine, library.client(), user_id, T0)
    first = len(library_calls(library))

    poll_connection(engine, library.client(), user_id, T0 + timedelta(minutes=15))

    assert len(library_calls(library)) == first
    assert len(library.calls(RECENT)) == 2


def test_a_refused_source_is_skipped_and_the_rest_is_stored(
    engine: Engine, library: FakeSpotify
) -> None:
    library.fail_next(FOLLOWING, httpx.Response(403, json={"error": {"status": 403}}))
    user_id = connect_user(engine)

    poll_connection(engine, library.client(), user_id, T0)

    stored = marks(engine, user_id)
    assert ("artist", "Followed One", "spotify_library") not in stored
    assert ("album", "SavedAlbum", "spotify_library") in stored
    assert baseline_at(engine, user_id) == T0


def test_spotify_unavailable_stores_nothing_and_the_next_poll_tries_again(
    engine: Engine, library: FakeSpotify
) -> None:
    for _ in range(5):  # outlasts the client's five attempts
        library.fail_next(SAVED_ALBUMS, httpx.Response(503))
    user_id = connect_user(engine)

    result = poll_connection(engine, library.client(), user_id, T0)

    assert result.status == "succeeded"  # the plays poll is not held back
    assert marks(engine, user_id) == set()
    assert baseline_at(engine, user_id) is None

    poll_connection(engine, library.client(), user_id, T0 + timedelta(minutes=15))
    assert ("album", "SavedAlbum", "spotify_library") in marks(engine, user_id)


def test_pages_are_followed_until_the_last_one(engine: Engine, library: FakeSpotify) -> None:
    library.followed = [f"Artist {n:03d}" for n in range(120)]
    library.saved_tracks = [(BEFORE, library_track(f"T{n:03d}")) for n in range(70)]
    user_id = connect_user(engine)

    poll_connection(engine, library.client(), user_id, T0)

    stored = {(kind, item) for kind, item, _ in marks(engine, user_id)}
    assert {("artist", f"Artist {n:03d}") for n in range(120)} <= stored
    assert {("track", f"id:T{n:03d}") for n in range(70)} <= stored
    assert len(library.calls(FOLLOWING)) == 3
    assert len(library.calls(SAVED_TRACKS)) == 2


def test_artist_names_are_stored_in_the_form_the_lists_serve(
    engine: Engine, library: FakeSpotify
) -> None:
    library.followed = ["  Spaced Name  ", "Café"]  # NFD é
    user_id = connect_user(engine)

    poll_connection(engine, library.client(), user_id, T0)

    names = {item for kind, item, _ in marks(engine, user_id) if kind == "artist"}
    assert {"Spaced Name", "Café"} <= names


def test_a_mark_the_user_made_stays_theirs(engine: Engine, library: FakeSpotify) -> None:
    user_id = connect_user(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO not_new_mark (user_id, kind, item_id) "
                "VALUES (:u, 'artist', 'Followed One')"
            ),
            {"u": user_id},
        )

    poll_connection(engine, library.client(), user_id, T0)

    assert ("artist", "Followed One", "user") in marks(engine, user_id)
    assert ("artist", "Followed One", "spotify_library") not in marks(engine, user_id)


def test_known_music_is_not_new_and_undo_brings_it_back(
    engine: Engine, library: FakeSpotify, read_api: TestClient
) -> None:
    user_id = connect_user(engine)
    poll_connection(engine, library.client(), user_id, T0)
    user = User(user_id, "synthetic-session-baseline", "Europe/Warsaw")
    with engine.begin() as conn:
        add_session(conn, user_id, user.token)
    # A saved album played after connecting: the album, its song and its artist are known.
    add_play(
        engine,
        user,
        "2026-10-06T10:00:00+00:00",
        item="SongOnSaved",
        creators=("Saved Album Artist",),
        collection="Album SavedAlbum",
        external_ids={"spotify_album_id": "SavedAlbum"},
        provider_extras=album_extras(1),
    )
    add_play(engine, user, "2026-10-06T11:00:00+00:00", item="Fresh", creators=("Brand New",))

    body = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")
    assert [a["album_id"] for a in body["albums"]["items"]] == []
    assert [t["item_key"] for t in body["tracks"]["items"]] == ["id:Fresh"]

    response = read_api.delete("/v1/new-to-you/not-new/album/SavedAlbum", headers=user.auth)
    assert response.status_code == 204

    body = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")
    assert [a["album_id"] for a in body["albums"]["items"]] == ["SavedAlbum"]
