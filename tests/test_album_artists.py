"""Album artists: collected, filled on re-seen rows, backfilled, and read (WHO-135, WHO-128).

The owner's case: on a compilation such as *HELP(2)* "New to you" showed the
artists of the tracks; Spotify's album artist is the label. All data here is
synthetic.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.events.keys import item_key_from_provider_id
from app.events.store import PreciseEvent, WriteCounts, write_precise_event
from app.poller.spotify import import_recent_history, poll_connection
from app.spotify.album_artists_backfill import backfill_all, backfill_user
from app.spotify.connection import decrypt_refresh_token
from app.spotify.plays import play_from_item
from fake_spotify import TOKEN, FakeSpotify
from read_helpers import User, add_play, album_extras, get, make_user
from test_poller_spotify import T0, conn_row, connect_user, plays

KEY = "spotify_album_artists"
TOTAL = "spotify_album_total_tracks"
# The reading tests store a 1-track count, so the album is "new to you" (WHO-130).
ONE_TRACK = album_extras(1)


def item(**album: Any) -> dict[str, Any]:
    return {
        "played_at": "2026-10-11T09:01:00.000Z",
        "track": {
            "id": "track-1",
            "name": "Synthetic Title",
            "artists": [{"name": "Track Artist A"}, {"name": "Track Artist B"}],
            "album": {"id": "album-1", "name": "Synthetic Compilation", **album},
        },
    }


# --- collecting -------------------------------------------------------------


def test_the_album_artists_are_kept_apart_from_the_track_artists() -> None:
    event = play_from_item(item(artists=[{"name": "Synthetic Label", "id": "x"}])).event

    assert event.creators == ("Track Artist A", "Track Artist B")
    assert event.provider_extras == {KEY: ["Synthetic Label"]}


@pytest.mark.parametrize(
    "album",
    [
        {},
        {"artists": None},
        {"artists": []},
        {"artists": [{"name": ""}, {"id": "no-name"}, "not-an-object"]},
    ],
)
def test_no_album_artists_means_no_key(album: dict[str, Any]) -> None:
    assert play_from_item(item(**album)).event.provider_extras == {}


def test_album_artists_are_kept_within_the_database_bounds() -> None:
    names = [{"name": f"Synthetic {n}"} for n in range(25)]
    too_long = {"name": "x" * 501}

    extras = play_from_item(item(artists=[too_long, *names])).event.provider_extras

    # The overlong name is left out, never cut; the list stops at 20.
    assert extras == {KEY: [f"Synthetic {n}" for n in range(20)]}


# --- filling rows stored without the key ------------------------------------


def _event(extras: dict[str, object]) -> PreciseEvent:
    return PreciseEvent(
        source="spotify",
        source_artifact="spotify_recently_played",
        ingest_method="api_poll",
        kind="listen",
        occurred_at=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
        title="Synthetic Track",
        item_key=item_key_from_provider_id("synthetic-track"),
        external_ids={"spotify_album_id": "album1"},
        provider_extras=extras,
    )


def _write(engine: Engine, user_id: UUID, extras: dict[str, object]) -> WriteCounts:
    counts = WriteCounts()
    with engine.begin() as conn:
        write_precise_event(
            conn,
            user_id=user_id,
            home_timezone="UTC",
            event=_event(extras),
            ingest_batch_id=None,
            counts=counts,
        )
    return counts


def _extras(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT provider_extras FROM unified_event")).scalars())


def test_a_re_seen_play_gets_the_key_it_lacked_and_stays_one_row(engine: Engine) -> None:
    with engine.begin() as conn:
        user_id = conn.execute(
            text("INSERT INTO app_user (home_timezone) VALUES ('UTC') RETURNING id")
        ).scalar_one()
    _write(engine, user_id, {})

    counts = _write(engine, user_id, {KEY: ["Synthetic Label"]})

    assert (counts.inserted, counts.upgraded, counts.skipped, counts.filled) == (0, 0, 1, 1)
    assert _extras(engine) == [{KEY: ["Synthetic Label"]}]


def test_a_stored_value_is_never_replaced_and_a_re_write_is_a_no_op(engine: Engine) -> None:
    with engine.begin() as conn:
        user_id = conn.execute(
            text("INSERT INTO app_user (home_timezone) VALUES ('UTC') RETURNING id")
        ).scalar_one()
    _write(engine, user_id, {KEY: ["Synthetic Label"]})

    same = _write(engine, user_id, {KEY: ["Synthetic Label"]})
    other = _write(engine, user_id, {KEY: ["Another Label"]})
    none = _write(engine, user_id, {})

    for counts in (same, other, none):
        assert (counts.inserted, counts.skipped, counts.filled) == (0, 1, 0)
    assert _extras(engine) == [{KEY: ["Synthetic Label"]}]


def _strip_key(engine: Engine) -> None:
    """Make the stored rows look like rows from before WHO-135."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE unified_event SET provider_extras = provider_extras - :k"), {"k": KEY}
        )


def test_the_history_import_repairs_old_rows_without_adding_any(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    for n in range(3):
        fake_spotify.play(f"track-{n}", f"Synthetic {n}", T0 + timedelta(minutes=n + 1))
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))
    _strip_key(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE spotify_connection SET history_import_requested = true WHERE user_id = :u"
            ),
            {"u": user},
        )

    result = import_recent_history(
        engine, fake_spotify.client(), "synthetic-access-1", user, "UTC", T0 + timedelta(minutes=6)
    )

    assert (result.status, result.rows_inserted, result.rows_skipped) == ("succeeded", 0, 3)
    assert plays(engine) == 3
    assert (
        _extras(engine)
        == [{KEY: ["Synthetic Album Artist"], TOTAL: 10, "spotify_album_type": "album"}] * 3
    )


# --- reading ----------------------------------------------------------------

HELP2 = {"spotify_album_id": "HELP2"}


def _new_albums(api: TestClient, user: User) -> list[dict[str, Any]]:
    body = get(api, user, "/v1/new-to-you", period="month", anchor="2026-10-01")
    return list(body["albums"]["items"])


def _album(api: TestClient, user: User, album_id: str) -> dict[str, Any]:
    body = get(
        api, user, f"/v1/new-to-you/albums/{album_id}/tracks", period="month", anchor="2026-10-01"
    )
    return dict(body["album"])


def test_help2_shows_its_label_not_its_track_artists(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    label: dict[str, object] = {KEY: ["War Child Records"], **ONE_TRACK}
    add_play(
        engine,
        user,
        "2026-10-05T18:00:00Z",
        item="T1",
        creators=("Synthetic One",),
        collection="HELP(2)",
        external_ids=HELP2,
        provider_extras=label,
    )
    add_play(
        engine,
        user,
        "2026-10-06T18:00:00Z",
        item="T2",
        creators=("Synthetic Two",),
        collection="HELP(2)",
        external_ids=HELP2,
        provider_extras=label,
    )

    [album] = _new_albums(read_api, user)

    assert album["artists"] == ["War Child Records"]
    assert _album(read_api, user, "HELP2")["artists"] == ["War Child Records"]


def test_the_latest_play_with_the_key_wins_over_newer_plays_without_it(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(
        engine,
        user,
        "2026-10-05T18:00:00Z",
        item="T1",
        creators=("Synthetic One",),
        external_ids=HELP2,
        provider_extras={KEY: ["Old Label"], **ONE_TRACK},
    )
    add_play(
        engine,
        user,
        "2026-10-06T18:00:00Z",
        item="T1",
        creators=("Synthetic One",),
        external_ids=HELP2,
        provider_extras={KEY: ["New Label"], **ONE_TRACK},
    )
    add_play(
        engine,
        user,
        "2026-10-07T18:00:00Z",
        item="T2",
        creators=("Synthetic Two",),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )

    assert _new_albums(read_api, user)[0]["artists"] == ["New Label"]
    assert _album(read_api, user, "HELP2")["artists"] == ["New Label"]


def test_without_the_key_the_artist_on_every_track_is_shown(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(
        engine,
        user,
        "2026-10-05T18:00:00Z",
        item="T1",
        creators=("Synthetic Guest", "Synthetic Band"),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )
    add_play(
        engine,
        user,
        "2026-10-06T18:00:00Z",
        item="T2",
        creators=("Synthetic Band",),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )

    assert _new_albums(read_api, user)[0]["artists"] == ["Synthetic Band"]
    assert _album(read_api, user, "HELP2")["artists"] == ["Synthetic Band"]


def test_shared_artists_keep_their_order_on_the_tracks(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(
        engine,
        user,
        "2026-10-05T18:00:00Z",
        item="T1",
        creators=("Synthetic Zed", "Synthetic Amy"),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )
    add_play(
        engine,
        user,
        "2026-10-06T18:00:00Z",
        item="T2",
        creators=("Synthetic Zed", "Synthetic Amy", "Synthetic Guest"),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )

    assert _new_albums(read_api, user)[0]["artists"] == ["Synthetic Zed", "Synthetic Amy"]


def test_without_the_key_and_no_shared_artist_the_artists_are_empty(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(
        engine,
        user,
        "2026-10-05T18:00:00Z",
        item="T1",
        creators=("Synthetic One",),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )
    add_play(
        engine,
        user,
        "2026-10-06T18:00:00Z",
        item="T2",
        creators=("Synthetic Two",),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )

    assert _new_albums(read_api, user)[0]["artists"] == []
    assert _album(read_api, user, "HELP2")["artists"] == []


def test_another_users_album_artists_never_show(read_api: TestClient, engine: Engine) -> None:
    owner, other = make_user(engine), make_user(engine)
    add_play(
        engine,
        other,
        "2026-10-05T18:00:00Z",
        item="T1",
        external_ids=HELP2,
        provider_extras={KEY: ["Other Users Label"], **ONE_TRACK},
    )
    add_play(
        engine,
        owner,
        "2026-10-06T18:00:00Z",
        item="T1",
        creators=("Synthetic One",),
        external_ids=HELP2,
        provider_extras=ONE_TRACK,
    )

    assert _new_albums(read_api, owner)[0]["artists"] == ["Synthetic One"]


# --- the one-off backfill ---------------------------------------------------


def _polled_user(engine: Engine, fake: FakeSpotify, **connect: Any) -> UUID:
    """A user with three stored plays on two albums, stored as before WHO-135."""
    user = connect_user(engine, **connect)
    fake.play("track-1", "Synthetic 1", T0 + timedelta(minutes=1), album_id="albumA")
    fake.play("track-2", "Synthetic 2", T0 + timedelta(minutes=2), album_id="albumA")
    fake.play("track-3", "Synthetic 3", T0 + timedelta(minutes=3), album_id="albumB")
    poll_connection(engine, fake.client(), user, now=T0 + timedelta(minutes=5))
    _strip_key(engine)
    return user


def _by_album(engine: Engine) -> dict[str, list[Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT external_ids ->> 'spotify_album_id' AS a, provider_extras -> :k AS v "
                "FROM unified_event ORDER BY occurred_at"
            ),
            {"k": KEY},
        )
        out: dict[str, list[Any]] = {}
        for row in rows:
            out.setdefault(row.a, []).append(row.v)
        return out


def test_the_backfill_fills_every_row_of_each_album_and_a_rerun_calls_nothing(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo", "Other"]}

    result = backfill_user(engine, fake_spotify.client(), user, sleep=fake_spotify.sleeps.append)

    assert (result.status, result.albums_missing, result.albums_filled, result.rows_filled) == (
        "succeeded",
        2,
        2,
        3,
    )
    assert _by_album(engine) == {
        "albumA": [["Synthetic Label"], ["Synthetic Label"]],
        "albumB": [["Synthetic Duo", "Other"]],
    }
    assert plays(engine) == 3
    assert len(fake_spotify.calls("/v1/albums/albumA")) == 1
    assert fake_spotify.sleeps == [0.2]  # one pause between the two album calls

    before = len(fake_spotify.requests)
    again = backfill_user(engine, fake_spotify.client(), user)
    assert (again.status, again.albums_missing) == ("succeeded", 0)
    assert len(fake_spotify.requests) == before  # not even a token refresh


def test_an_album_spotify_no_longer_has_keeps_the_fallback(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.albums = {"albumB": ["Synthetic Duo"]}  # albumA answers 404

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.albums_filled) == ("succeeded", 1)
    assert _by_album(engine) == {"albumA": [None, None], "albumB": [["Synthetic Duo"]]}


def test_a_429_waits_for_retry_after_then_continues(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo"]}
    fake_spotify.fail_next("/v1/albums/albumA", httpx.Response(429, headers={"Retry-After": "7"}))

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.albums_filled) == ("succeeded", 2)
    assert 7.0 in fake_spotify.sleeps


def test_a_lasting_outage_stops_the_user_and_a_rerun_finishes(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo"]}
    for _ in range(5):
        fake_spotify.fail_next("/v1/albums/albumB", httpx.Response(503))

    failed = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (failed.status, failed.error_kind, failed.albums_filled) == (
        "failed",
        "album_server_error",
        1,
    )
    assert _by_album(engine)["albumB"] == [None]

    done = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)
    assert (done.status, done.albums_missing, done.albums_filled) == ("succeeded", 1, 1)


def test_a_dead_grant_is_skipped_and_left_for_the_poller(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.albums = {"albumA": ["Synthetic Label"]}
    fake_spotify.fail_next(TOKEN, httpx.Response(400, json={"error": "invalid_grant"}))

    result = backfill_user(engine, fake_spotify.client(), user)

    assert (result.status, result.error_kind) == ("failed", "invalid_grant")
    assert conn_row(engine, user).status == "active"  # the poller marks it, with its gap
    assert fake_spotify.calls("/v1/albums/albumA") == []


def test_a_rotated_refresh_token_is_stored(engine: Engine, fake_spotify: FakeSpotify) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.albums = {"albumA": ["Synthetic Label"]}
    fake_spotify.rotate_refresh_tokens = True

    backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    stored = decrypt_refresh_token(user, bytes(conn_row(engine, user).refresh_token_ciphertext))
    assert stored.startswith("synthetic-rotated-")


def test_backfill_all_touches_each_user_only_with_their_own_albums(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    first = _polled_user(engine, fake_spotify)
    second = connect_user(engine, account_id="synthetic-account-2", refresh_token="synthetic-r2")
    fake_spotify.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo"]}

    results = backfill_all(engine, fake_spotify.client(), sleep=lambda _s: None)

    assert results[first].rows_filled == 3
    assert (results[second].status, results[second].albums_missing) == ("succeeded", 0)


def test_an_album_id_that_is_not_base62_is_skipped_and_never_sent(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    fake_spotify.play("track-1", "Synthetic 1", T0 + timedelta(minutes=1), album_id="bad/id")
    fake_spotify.play("track-2", "Synthetic 2", T0 + timedelta(minutes=2), album_id="albumA")
    fake_spotify.play("track-3", "Synthetic 3", T0 + timedelta(minutes=3), album_id="../me")
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))
    _strip_key(engine)
    fake_spotify.albums = {"albumA": ["Synthetic Label"]}

    results = backfill_all(engine, fake_spotify.client(), sleep=lambda _s: None)

    assert (results[user].status, results[user].albums_missing) == ("succeeded", 3)
    assert (results[user].albums_filled, results[user].rows_filled) == (1, 1)
    assert _by_album(engine) == {"bad/id": [None], "albumA": [["Synthetic Label"]], "../me": [None]}
    album_paths = [str(r.url.path) for r in fake_spotify.requests if "/albums" in r.url.path]
    assert album_paths == ["/v1/albums/albumA"]


def test_the_backfill_logs_counts_only(
    engine: Engine, fake_spotify: FakeSpotify, caplog: pytest.LogCaptureFixture
) -> None:
    _polled_user(engine, fake_spotify, refresh_token="synthetic-secret-refresh")
    fake_spotify.albums = {"albumA": ["Synthetic Label"], "albumB": ["Synthetic Duo"]}
    caplog.set_level(logging.DEBUG, logger="app")

    backfill_all(engine, fake_spotify.client(), sleep=lambda _s: None)

    ours = [r for r in caplog.records if r.name.startswith("app")]
    assert any("rows_filled=3" in r.getMessage() for r in ours)
    for record in ours:
        message = record.getMessage()
        for secret in ("albumA", "albumB", "Synthetic Label", "Synthetic Duo", "synthetic-secret"):
            assert secret not in message
