"""An album is new only once more than half of its tracks were played (WHO-141, WHO-130).

The rule: ``distinct tracks played * 2 > total tracks``, over every stored play,
new in the period holding the play that took it past half. "New to you" and
``GET /v1/albums`` ``is_new`` must agree for every album both list; ``/v1/albums``
lists only albums with more than half of their tracks played in the period itself
(WHO-160), so an album new by tracks split across periods is not in it. All data
here is synthetic.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.poller.spotify import poll_connection
from app.spotify.album_artists_backfill import backfill_user
from app.spotify.plays import play_from_item
from fake_spotify import FakeSpotify
from read_helpers import User, add_play, album_extras, get, make_user
from test_poller_spotify import T0, connect_user, plays

TOTAL = "spotify_album_total_tracks"


def play(
    engine: Engine,
    user: User,
    at: str,
    track: str,
    *,
    album: str = "ALB",
    total_tracks: int | None = 10,
    by: tuple[str, ...] = ("Nova Kett",),
) -> None:
    """One play of ``track`` on ``album``, which has ``total_tracks`` tracks (None: unknown)."""
    add_play(
        engine,
        user,
        at,
        item=track,
        creators=by,
        collection=f"Album {album}",
        external_ids={"spotify_album_id": album},
        provider_extras=album_extras(total_tracks),
    )


def play_tracks(
    engine: Engine, user: User, count: int, *, total_tracks: int | None, day: str = "2026-10-05"
) -> None:
    """``count`` distinct tracks of album ``ALB``, an hour apart from 08:00 UTC on ``day``."""
    for n in range(count):
        play(engine, user, f"{day}T{8 + n:02d}:00:00Z", f"T{n}", total_tracks=total_tracks)


def new_albums(api: TestClient, user: User, anchor: str = "2026-10-01") -> list[dict[str, Any]]:
    body = get(api, user, "/v1/new-to-you", period="month", anchor=anchor)
    assert body["albums"]["count"] == len(body["albums"]["items"])
    return list(body["albums"]["items"])


def is_new(api: TestClient, user: User, anchor: str = "2026-10-01") -> dict[str, bool]:
    body = get(api, user, "/v1/albums", period="month", anchor=anchor)
    return {a["album_id"]: a["is_new"] for a in body["albums"]["items"]}


def assert_agree(api: TestClient, user: User, anchor: str, expected_new: set[str]) -> None:
    """Both album lists say the same: New to you holds exactly the ``is_new`` albums."""
    assert {a["album_id"] for a in new_albums(api, user, anchor)} == expected_new
    assert {a for a, new in is_new(api, user, anchor).items() if new} == expected_new


# --- the threshold ----------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "played", "new"),
    [
        (1, 1, True),
        (2, 1, False),
        (2, 2, True),
        (7, 3, False),
        (7, 4, True),
        (10, 5, False),  # exactly half is not more than half
        (10, 6, True),
    ],
)
def test_more_than_half_of_the_tracks_makes_the_album_new(
    read_api: TestClient, engine: Engine, total: int, played: int, new: bool
) -> None:
    user = make_user(engine)
    play_tracks(engine, user, played, total_tracks=total)

    assert_agree(read_api, user, "2026-10-01", {"ALB"} if new else set())
    # Every track in one period: in the albums list exactly when new (WHO-160).
    assert is_new(read_api, user) == ({"ALB": True} if new else {})


def test_the_same_track_played_again_counts_once(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play_tracks(engine, user, 5, total_tracks=10)
    for hour in (14, 15, 16):
        play(engine, user, f"2026-10-05T{hour}:00:00Z", "T0")

    assert_agree(read_api, user, "2026-10-01", set())


# --- the counts on a new album row (WHO-155) --------------------------------


def day_album(api: TestClient, user: User, day: str) -> dict[str, Any]:
    body = get(api, user, "/v1/new-to-you", period="day", anchor=day)
    (album,) = body["albums"]["items"]
    return dict(album)


def play_russia34(engine: Engine, user: User) -> None:
    """16 tracks: T0..T5 on 4 Oct, then T3..T10 on 5 Oct (3 overlap). The 9th
    distinct track (T8) is played on 5 Oct, so the album is new that day."""
    for n in range(6):
        play(engine, user, f"2026-10-04T{8 + n:02d}:00:00Z", f"T{n}", total_tracks=16)
    for n in range(3, 11):
        play(engine, user, f"2026-10-05T{5 + n:02d}:00:00Z", f"T{n}", total_tracks=16)


def test_a_new_album_row_carries_the_rules_count_and_the_track_count(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play_russia34(engine, user)

    album = day_album(read_api, user, "2026-10-05")

    assert album["tracks_played_in_period"] == 8
    assert (album["tracks_listened"], album["total_tracks"]) == (11, 16)


def test_a_play_after_the_period_does_not_raise_its_count(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play_russia34(engine, user)
    for n in range(11, 14):
        play(engine, user, f"2026-10-06T{8 + n:02d}:00:00Z", f"T{n}", total_tracks=16)

    album = day_album(read_api, user, "2026-10-05")

    assert (album["tracks_listened"], album["total_tracks"]) == (11, 16)


def test_a_track_played_twice_counts_once_in_the_listened_count(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play_russia34(engine, user)
    play(engine, user, "2026-10-05T20:00:00Z", "T9", total_tracks=16)
    play(engine, user, "2026-10-05T21:00:00Z", "T0", total_tracks=16)

    album = day_album(read_api, user, "2026-10-05")

    assert album["tracks_played_in_period"] == 9  # T0 is new to the day, T9 is not
    assert (album["tracks_listened"], album["total_tracks"]) == (11, 16)


# --- which period ------------------------------------------------------------


def test_plays_split_over_two_months_make_it_new_in_the_second(
    read_api: TestClient, engine: Engine
) -> None:
    """3 tracks in March, 3 in April, of 10: listened, and new, in April only."""
    user = make_user(engine)
    for n in range(3):
        play(engine, user, f"2026-03-1{n}T12:00:00Z", f"T{n}")
    for n in range(3, 6):
        play(engine, user, f"2026-04-1{n}T12:00:00Z", f"T{n}")
    play(engine, user, "2026-04-20T12:00:00Z", "T7")  # after the 6th: changes nothing

    assert_agree(read_api, user, "2026-03-01", set())
    [album] = new_albums(read_api, user, "2026-04-01")
    # 4 of 10 tracks in April itself: new, but not in April's albums list (WHO-160).
    assert is_new(read_api, user, "2026-04-01") == {}
    # The play of the 6th distinct track, not the album's first play.
    assert album["first_played_at"] == "2026-04-15T12:00:00.000Z"
    assert album["first_played_local_date"] == "2026-04-15"
    assert album["tracks_played_in_period"] == 4  # the period only, as before
    assert_agree(read_api, user, "2026-05-01", set())


def test_plays_after_the_period_do_not_count(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play_tracks(engine, user, 5, total_tracks=10, day="2026-10-05")
    play(engine, user, "2026-11-02T12:00:00Z", "T9")

    assert_agree(read_api, user, "2026-10-01", set())
    assert [a["album_id"] for a in new_albums(read_api, user, "2026-11-01")] == ["ALB"]
    assert is_new(read_api, user, "2026-11-01") == {}  # 1 of 10 tracks in November


def test_the_new_date_is_in_the_display_zone(read_api: TestClient, engine: Engine) -> None:
    """The 2nd of 2 tracks at 22:30 UTC on 30 Sep is 1 October in Warsaw: new in October."""
    user = make_user(engine)  # Europe/Warsaw
    play(engine, user, "2026-09-10T12:00:00Z", "T0", total_tracks=2)
    play(engine, user, "2026-09-30T22:30:00Z", "T1", total_tracks=2)

    assert_agree(read_api, user, "2026-09-01", set())
    [album] = new_albums(read_api, user, "2026-10-01")
    assert album["first_played_local_date"] == "2026-10-01"


def test_albums_are_ordered_by_the_play_that_made_them_new(
    read_api: TestClient, engine: Engine
) -> None:
    """R-60 with the new date: EARLY was first played first but crossed half last."""
    user = make_user(engine)
    play(engine, user, "2026-10-02T12:00:00Z", "E1", album="EARLY", total_tracks=2)
    play(engine, user, "2026-10-03T12:00:00Z", "L1", album="LATE", total_tracks=1)
    play(engine, user, "2026-10-09T12:00:00Z", "E2", album="EARLY", total_tracks=2)

    assert [a["album_id"] for a in new_albums(read_api, user)] == ["EARLY", "LATE"]


# --- the track count ----------------------------------------------------------


def test_an_unknown_track_count_is_never_new_nor_listed(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    play_tracks(engine, user, 4, total_tracks=None)

    assert_agree(read_api, user, "2026-10-01", set())
    assert is_new(read_api, user) == {}  # not in the albums list either (WHO-160)
    tracks = get(read_api, user, "/v1/albums/ALB/tracks", period="month", anchor="2026-10-01")
    assert len(tracks["tracks"]) == 4  # the album tracks screen lists every played track


def test_the_most_recent_known_track_count_is_used(read_api: TestClient, engine: Engine) -> None:
    """Older plays said 2 tracks, the latest says 10: 2 played tracks are not enough."""
    user = make_user(engine)
    play(engine, user, "2026-10-02T12:00:00Z", "T0", total_tracks=2)
    play(engine, user, "2026-10-03T12:00:00Z", "T1", total_tracks=2)
    play(engine, user, "2026-10-04T12:00:00Z", "T1", total_tracks=10)
    play(engine, user, "2026-10-05T12:00:00Z", "T0", total_tracks=None)

    assert_agree(read_api, user, "2026-10-01", set())


def test_a_track_count_on_one_play_is_enough(read_api: TestClient, engine: Engine) -> None:
    """Rows stored before WHO-141 lack the count; one row that has it decides."""
    user = make_user(engine)
    play(engine, user, "2026-10-02T12:00:00Z", "T0", total_tracks=None)
    play(engine, user, "2026-10-03T12:00:00Z", "T1", total_tracks=3)

    assert_agree(read_api, user, "2026-10-01", {"ALB"})


def test_another_users_plays_and_track_count_never_count(
    read_api: TestClient, engine: Engine
) -> None:
    owner, other = make_user(engine), make_user(engine)
    play(engine, owner, "2026-10-02T12:00:00Z", "T0", total_tracks=None)
    play(engine, owner, "2026-10-03T12:00:00Z", "T1", total_tracks=None)
    play(engine, other, "2026-10-04T12:00:00Z", "T2", total_tracks=3)

    assert_agree(read_api, owner, "2026-10-01", set())  # the count 3 is not theirs
    assert_agree(read_api, other, "2026-10-01", set())  # 1 of 3: T0 and T1 are not theirs


# --- what does not change -------------------------------------------------------


def test_tracks_of_a_half_played_album_stay_new(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play(engine, user, "2026-10-02T12:00:00Z", "T0", by=("Pine Arc",))

    body = get(read_api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")

    assert body["albums"]["count"] == 0
    assert [t["item_key"] for t in body["tracks"]["items"]] == ["id:T0"]
    assert body["nothing_new"] is False


def test_a_not_new_mark_still_hides_a_listened_album(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    play_tracks(engine, user, 6, total_tracks=10)
    assert_agree(read_api, user, "2026-10-01", {"ALB"})

    response = read_api.put("/v1/new-to-you/not-new/album/ALB", headers=user.auth)
    assert response.status_code == 204

    assert_agree(read_api, user, "2026-10-01", set())


# --- collecting the count ---------------------------------------------------------


def _item(**album: Any) -> dict[str, Any]:
    return {
        "played_at": "2026-10-11T09:01:00.000Z",
        "track": {
            "id": "track-1",
            "name": "Synthetic Title",
            "artists": [{"name": "Synthetic Artist"}],
            "album": {"id": "album-1", "name": "Synthetic Album", **album},
        },
    }


@pytest.mark.parametrize("total", [1, 12, 100_000])
def test_the_track_count_is_kept(total: int) -> None:
    assert play_from_item(_item(total_tracks=total)).event.provider_extras == {TOTAL: total}


@pytest.mark.parametrize("total", [None, 0, -3, 100_001, 1.5, "12", True, [12]])
def test_a_missing_or_unusable_track_count_is_left_out(total: object) -> None:
    album = {} if total is None else {"total_tracks": total}

    assert play_from_item(_item(**album)).event.provider_extras == {}


# --- the backfill -----------------------------------------------------------------


def _counts_by_album(engine: Engine) -> dict[str, list[Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT external_ids ->> 'spotify_album_id' AS a, provider_extras AS v "
                "FROM unified_event ORDER BY occurred_at"
            )
        )
        out: dict[str, list[Any]] = {}
        for row in rows:
            out.setdefault(row.a, []).append(row.v.get(TOTAL))
        return out


def _polled_user_without_counts(engine: Engine, fake: FakeSpotify) -> UUID:
    """Three plays on two albums, stored with album artists but without a track count."""
    user = connect_user(engine)
    fake.play("track-1", "Synthetic 1", T0 + timedelta(minutes=1), album_id="albumA")
    fake.play("track-2", "Synthetic 2", T0 + timedelta(minutes=2), album_id="albumA")
    fake.play("track-3", "Synthetic 3", T0 + timedelta(minutes=3), album_id="albumB")
    poll_connection(engine, fake.client(), user, now=T0 + timedelta(minutes=5))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE unified_event SET provider_extras = provider_extras - :k"), {"k": TOTAL}
        )
    return user


def test_the_backfill_fills_the_track_count_and_keeps_the_stored_artists(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user_without_counts(engine, fake_spotify)
    # Spotify now names other artists: the stored ones are kept, only the count is added.
    fake_spotify.albums = {"albumA": ["Renamed Artist"], "albumB": ["Renamed Artist"]}
    fake_spotify.album_total_tracks = {"albumA": 12, "albumB": 3}

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.albums_missing, result.albums_filled, result.rows_filled) == (
        "succeeded",
        2,
        2,
        3,
    )
    assert _counts_by_album(engine) == {"albumA": [12, 12], "albumB": [3]}
    with engine.connect() as conn:
        artists = conn.execute(
            text("SELECT DISTINCT provider_extras -> 'spotify_album_artists' FROM unified_event")
        ).scalars()
        assert list(artists) == [["Synthetic Album Artist"]]
    assert plays(engine) == 3

    before = len(fake_spotify.requests)
    again = backfill_user(engine, fake_spotify.client(), user)
    assert (again.status, again.albums_missing) == ("succeeded", 0)
    assert len(fake_spotify.requests) == before


def test_one_album_call_fills_every_key(engine: Engine, fake_spotify: FakeSpotify) -> None:
    """Rows from before WHO-135 lack every key: one GET /v1/albums/{id} per album fills them."""
    user = _polled_user_without_counts(engine, fake_spotify)
    with engine.begin() as conn:
        conn.execute(text("UPDATE unified_event SET provider_extras = '{}'"))
    fake_spotify.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo"]}
    fake_spotify.album_total_tracks = {"albumA": 12, "albumB": 3}

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.rows_filled) == ("succeeded", 3)
    assert len(fake_spotify.calls("/v1/albums/albumA")) == 1
    with engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT provider_extras FROM unified_event "
                "WHERE external_ids ->> 'spotify_album_id' = 'albumB'"
            )
        ).scalar_one()
    assert stored == {
        "spotify_album_artists": ["Synthetic Duo"],
        TOTAL: 3,
        "spotify_album_type": "album",  # WHO-162
    }


def test_an_unusable_track_count_from_spotify_is_not_stored(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user_without_counts(engine, fake_spotify)
    fake_spotify.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo"]}
    fake_spotify.album_total_tracks = {"albumA": None, "albumB": 0}

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    # The artists are already stored, so there is nothing to write; the count stays unknown.
    assert (result.status, result.albums_filled, result.rows_filled) == ("succeeded", 0, 0)
    assert _counts_by_album(engine) == {"albumA": [None, None], "albumB": [None]}
