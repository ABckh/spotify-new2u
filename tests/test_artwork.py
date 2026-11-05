"""Album covers: picked, allow-listed, filled on re-seen rows, backfilled, and read (WHO-149).

All data here is synthetic.
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
from app.poller import spotify as poller_spotify
from app.poller.spotify import poll_connection
from app.spotify.artwork import pick_album_image, safe_artwork_url
from app.spotify.artwork_backfill import backfill_all, backfill_user
from app.spotify.plays import play_from_item
from fake_spotify import TOKEN, FakeSpotify
from read_helpers import User, add_play, album_extras, get, make_user
from test_poller_spotify import T0, conn_row, connect_user, plays

COVER_A = "https://i.scdn.co/image/ab67616d00001e02aaaaaaaaaaaaaaaaaaaaaaaa"
COVER_A_NEW = "https://i.scdn.co/image/ab67616d00001e02cccccccccccccccccccccccc"
COVER_B = "https://i.scdn.co/image/ab67616d00001e02bbbbbbbbbbbbbbbbbbbbbbbb"


def image(url: str, width: int | None) -> dict[str, Any]:
    return {"url": url, "width": width, "height": width}


# --- the allow-list ---------------------------------------------------------


@pytest.mark.parametrize("url", [COVER_A, "https://i.scdn.co/image/x"])
def test_a_spotify_image_url_is_kept(url: str) -> None:
    assert safe_artwork_url(url) == url


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        "http://i.scdn.co/image/x",  # not https
        "https://i.scdn.co/",  # no path
        "https://i.scdn.co/image/",  # no image
        "https://i.scdn.co/ab67616d0000b273aaaa",  # not under /image/ (WHO-148 rule 10)
        "https://i.scdn.co/images/x",
        "https://i.scdn.co/image/../x",  # several segments
        "https://i.scdn.co/image/a/b",
        "https://i.scdn.co.evil.example/image/x",
        "https://i.scdn.co@evil.example/image/x",
        "https://i.scdn.co:8443/image/x",
        "https://mosaic.scdn.co/640/x",  # another Spotify host
        "https://example.invalid/art.jpg",
        "https://i.scdn.co/image/x?track=1",
        "https://i.scdn.co/image/x#y",
        "https://i.scdn.co/image/%2e%2e",
        "https://i.scdn.co/image/x y",
        "https://i.scdn.co/image/x\n",
        "HTTPS://I.SCDN.CO/image/x",
        "https://i.scdn.co/image/" + "a" * 201,
    ],
)
def test_anything_else_is_no_cover(value: object) -> None:
    assert safe_artwork_url(value) is None


# --- picking the image ------------------------------------------------------


def test_the_smallest_image_at_least_300_wide_is_picked() -> None:
    images = [image(COVER_A, 640), image(COVER_B, 300), image(COVER_A_NEW, 64)]
    assert pick_album_image({"images": images}) == COVER_B


def test_the_pick_does_not_depend_on_the_order() -> None:
    images = [image(COVER_A_NEW, 64), image(COVER_A, 640), image(COVER_B, 320)]
    assert pick_album_image({"images": images}) == COVER_B


def test_with_none_wide_enough_the_widest_is_picked() -> None:
    images = [image(COVER_A, 64), image(COVER_B, 200)]
    assert pick_album_image({"images": images}) == COVER_B


def test_an_image_with_a_bad_url_is_never_picked() -> None:
    images = [image("https://example.invalid/300.jpg", 300), image(COVER_A, 640)]
    assert pick_album_image({"images": images}) == COVER_A


def test_without_widths_the_first_usable_image_is_picked() -> None:
    images = [image("http://i.scdn.co/image/x", None), image(COVER_A, None), image(COVER_B, None)]
    assert pick_album_image({"images": images}) == COVER_A


@pytest.mark.parametrize(
    "album",
    [{}, {"images": None}, {"images": []}, {"images": ["x", {"width": 300}]}],
)
def test_no_usable_image_is_no_cover(album: dict[str, Any]) -> None:
    assert pick_album_image(album) is None


def _item(**album: Any) -> dict[str, Any]:
    return {
        "played_at": "2026-10-11T09:01:00.000Z",
        "track": {"id": "track-1", "name": "Synthetic Title", "album": {"id": "albumA", **album}},
    }


def test_ingest_keeps_the_picked_cover() -> None:
    event = play_from_item(_item(images=[image(COVER_A, 640), image(COVER_B, 300)])).event
    assert event.artwork_url == COVER_B


def test_ingest_of_a_bad_cover_stores_the_play_without_one() -> None:
    event = play_from_item(_item(images=[image("javascript:alert(1)", 300)])).event
    assert event.artwork_url is None
    assert event.title == "Synthetic Title"


# --- the writer: fill a missing cover, never replace one ---------------------


def _event(artwork_url: str | None) -> PreciseEvent:
    return PreciseEvent(
        source="spotify",
        source_artifact="spotify_recently_played",
        ingest_method="api_poll",
        kind="listen",
        occurred_at=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
        title="Synthetic Track",
        item_key=item_key_from_provider_id("synthetic-track"),
        artwork_url=artwork_url,
        external_ids={"spotify_album_id": "albumA"},
    )


def _write(engine: Engine, user_id: UUID, artwork_url: str | None) -> WriteCounts:
    counts = WriteCounts()
    with engine.begin() as conn:
        write_precise_event(
            conn,
            user_id=user_id,
            home_timezone="UTC",
            event=_event(artwork_url),
            ingest_batch_id=None,
            counts=counts,
        )
    return counts


def _covers(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(text("SELECT artwork_url FROM unified_event ORDER BY occurred_at"))
            .scalars()
            .all()
        )


def _bare_user(engine: Engine) -> UUID:
    with engine.begin() as conn:
        user_id: UUID = conn.execute(
            text("INSERT INTO app_user (home_timezone) VALUES ('UTC') RETURNING id")
        ).scalar_one()
    return user_id


def test_a_re_seen_play_gets_the_cover_it_lacked_and_stays_one_row(engine: Engine) -> None:
    user_id = _bare_user(engine)
    _write(engine, user_id, None)

    counts = _write(engine, user_id, COVER_A)

    assert (counts.inserted, counts.upgraded, counts.skipped, counts.filled) == (0, 0, 1, 1)
    assert _covers(engine) == [COVER_A]


def test_a_stored_cover_is_never_replaced_and_a_re_write_is_a_no_op(engine: Engine) -> None:
    user_id = _bare_user(engine)
    _write(engine, user_id, COVER_A)

    for again in (COVER_A, COVER_B, None):
        counts = _write(engine, user_id, again)
        assert (counts.inserted, counts.skipped, counts.filled) == (0, 1, 0)
    assert _covers(engine) == [COVER_A]


def test_an_overlapping_poll_adds_only_the_new_play_and_fills_old_covers(
    engine: Engine, fake_spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = connect_user(engine)
    for n in range(3):
        fake_spotify.play(f"track-{n}", f"Synthetic {n}", T0 + timedelta(minutes=n + 1))
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))
    with engine.begin() as conn:  # stored as before WHO-149
        conn.execute(text("UPDATE unified_event SET artwork_url = NULL"))
    fake_spotify.play("track-3", "Synthetic 3", T0 + timedelta(minutes=6))

    # The cursor forced back, so Spotify lists all four again.
    monkeypatch.setattr(poller_spotify, "_cursor", lambda *_: T0 - timedelta(hours=1))
    result = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=10))

    assert (result.rows_seen, result.rows_inserted, result.rows_skipped) == (4, 1, 3)
    assert plays(engine) == 4
    assert _covers(engine) == ["https://i.scdn.co/image/synthetic-300"] * 4


# --- reading ----------------------------------------------------------------

ALBUM_A = {"spotify_album_id": "albumA"}
ALBUM_B = {"spotify_album_id": "albumB"}
MONTH = {"period": "month", "anchor": "2026-10-01"}


def _plays(engine: Engine, user: User) -> None:
    """albumA: an old cover, a newer one, then a newer play with none. albumB: no cover.

    Both albums are more than half played (WHO-141), so both are "new to you".
    """
    a, b = album_extras(3), album_extras(1)
    add_play(engine, user, "2026-10-03T18:00:00Z", item="T1", external_ids=ALBUM_A,
             artwork_url=COVER_A, provider_extras=a)  # fmt: skip
    add_play(engine, user, "2026-10-04T18:00:00Z", item="T2", external_ids=ALBUM_A,
             artwork_url=COVER_A_NEW, provider_extras=a)  # fmt: skip
    add_play(engine, user, "2026-10-05T18:00:00Z", item="T1", external_ids=ALBUM_A,
             provider_extras=a)  # fmt: skip
    add_play(engine, user, "2026-10-06T18:00:00Z", item="T3", external_ids=ALBUM_B,
             provider_extras=b)  # fmt: skip


def _by(items: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {i[key]: i["artwork_url"] for i in items}


def test_album_lists_show_the_newest_stored_cover(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    _plays(engine, user)

    new = get(read_api, user, "/v1/new-to-you", **MONTH)
    played = get(read_api, user, "/v1/albums", **MONTH)

    expected = {"albumA": COVER_A_NEW, "albumB": None}
    assert _by(new["albums"]["items"], "album_id") == expected
    assert _by(played["albums"]["items"], "album_id") == expected


def test_album_tracks_carry_the_albums_cover(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    _plays(engine, user)

    for path in ("/v1/albums/albumA/tracks", "/v1/new-to-you/albums/albumA/tracks"):
        body = get(read_api, user, path, **MONTH)
        assert body["album"]["artwork_url"] == COVER_A_NEW
        assert _by(body["tracks"], "item_key") == {"id:T1": COVER_A_NEW, "id:T2": COVER_A_NEW}

    no_cover = get(read_api, user, "/v1/albums/albumB/tracks", **MONTH)
    assert no_cover["album"]["artwork_url"] is None
    assert _by(no_cover["tracks"], "item_key") == {"id:T3": None}


def test_tracks_show_their_newest_stored_cover(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    _plays(engine, user)

    summary = get(read_api, user, "/v1/summary", **MONTH)
    new = get(read_api, user, "/v1/new-to-you", **MONTH)

    # T1's newest play has no cover: its newest stored one is shown.
    expected = {"id:T1": COVER_A, "id:T2": COVER_A_NEW, "id:T3": None}
    assert _by(summary["top_tracks"], "item_key") == expected
    assert _by(new["tracks"]["items"], "item_key") == expected


def test_timeline_events_carry_their_own_cover(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    _plays(engine, user)

    body = get(read_api, user, "/v1/timeline", **MONTH)

    assert [e["artwork_url"] for e in body["events"]] == [None, None, COVER_A_NEW, COVER_A]


def test_a_stored_url_of_another_form_is_sent_as_null(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-10-03T18:00:00Z", item="T1", external_ids=ALBUM_A,
             provider_extras=album_extras(1))  # fmt: skip
    with engine.begin() as conn:
        conn.execute(text("UPDATE unified_event SET artwork_url = 'https://example.invalid/a.jpg'"))

    timeline = get(read_api, user, "/v1/timeline", **MONTH)
    albums = get(read_api, user, "/v1/albums", **MONTH)
    album = get(read_api, user, "/v1/albums/albumA/tracks", **MONTH)

    assert timeline["events"][0]["artwork_url"] is None
    assert albums["albums"]["items"][0]["artwork_url"] is None
    assert album["album"]["artwork_url"] is None
    assert album["tracks"][0]["artwork_url"] is None


def test_export_sends_a_stored_url_of_another_form_as_null(
    read_api: TestClient, engine: Engine
) -> None:
    """WHO-148 rule 10: export checks the URL again, like the read API."""
    user = make_user(engine)
    add_play(engine, user, "2026-10-03T18:00:00Z", item="T1", external_ids=ALBUM_A,
             artwork_url=COVER_A)  # fmt: skip
    add_play(engine, user, "2026-10-04T18:00:00Z", item="T2", external_ids=ALBUM_A)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE unified_event SET artwork_url = 'https://example.invalid/a.jpg' "
                "WHERE artwork_url IS NULL"
            )
        )

    response = read_api.get("/v1/export", headers=user.auth)

    assert response.status_code == 200
    assert [e["artwork_url"] for e in response.json()["events"]] == [COVER_A, None]


def test_another_users_cover_never_shows(read_api: TestClient, engine: Engine) -> None:
    owner, other = make_user(engine), make_user(engine)
    add_play(engine, other, "2026-10-03T18:00:00Z", item="T1", external_ids=ALBUM_A,
             artwork_url=COVER_B)  # fmt: skip
    add_play(engine, owner, "2026-10-04T18:00:00Z", item="T1", external_ids=ALBUM_A,
             provider_extras=album_extras(1))  # fmt: skip

    albums = get(read_api, owner, "/v1/albums", **MONTH)
    summary = get(read_api, owner, "/v1/summary", **MONTH)

    assert albums["albums"]["items"][0]["artwork_url"] is None
    assert summary["top_tracks"][0]["artwork_url"] is None


# --- the one-off backfill ---------------------------------------------------


def _polled_user(engine: Engine, fake: FakeSpotify, **connect: Any) -> UUID:
    """A user with three stored plays on two albums, stored as before WHO-149."""
    user = connect_user(engine, **connect)
    fake.play("track-1", "Synthetic 1", T0 + timedelta(minutes=1), album_id="albumA")
    fake.play("track-2", "Synthetic 2", T0 + timedelta(minutes=2), album_id="albumA")
    fake.play("track-3", "Synthetic 3", T0 + timedelta(minutes=3), album_id="albumB")
    poll_connection(engine, fake.client(), user, now=T0 + timedelta(minutes=5))
    with engine.begin() as conn:
        conn.execute(text("UPDATE unified_event SET artwork_url = NULL"))
    return user


def _catalog(fake: FakeSpotify, **covers: str) -> None:
    """Albums Spotify answers for, each with one 300-wide cover and a smaller one."""
    for album_id, url in covers.items():
        fake.albums[album_id] = ["Synthetic Album Artist"]
        fake.album_images[album_id] = [
            image(url, 300),
            image("https://i.scdn.co/image/small", 64),
        ]


def _by_album(engine: Engine) -> dict[str, list[Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT external_ids ->> 'spotify_album_id' AS a, artwork_url "
                "FROM unified_event ORDER BY occurred_at"
            )
        )
        out: dict[str, list[Any]] = {}
        for row in rows:
            out.setdefault(row.a, []).append(row.artwork_url)
        return out


def test_the_backfill_fills_every_row_of_each_album_and_a_rerun_calls_nothing(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    _catalog(fake_spotify, albumA=COVER_A, albumB=COVER_B)

    result = backfill_user(engine, fake_spotify.client(), user, sleep=fake_spotify.sleeps.append)

    assert (result.status, result.albums_missing, result.albums_filled, result.rows_filled) == (
        "succeeded",
        2,
        2,
        3,
    )
    assert _by_album(engine) == {"albumA": [COVER_A, COVER_A], "albumB": [COVER_B]}
    assert plays(engine) == 3
    assert len(fake_spotify.calls("/v1/albums/albumA")) == 1
    assert fake_spotify.sleeps == [0.2]  # one pause between the two album calls
    assert all("ids" not in r.url.params for r in fake_spotify.requests)

    before = len(fake_spotify.requests)
    again = backfill_user(engine, fake_spotify.client(), user)
    assert (again.status, again.albums_missing) == ("succeeded", 0)
    assert len(fake_spotify.requests) == before  # not even a token refresh
    assert _by_album(engine) == {"albumA": [COVER_A, COVER_A], "albumB": [COVER_B]}


def test_the_backfill_never_replaces_a_stored_cover(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE unified_event SET artwork_url = :u "
                "WHERE external_ids ->> 'spotify_album_id' = 'albumA' "
                "AND occurred_at = (SELECT min(occurred_at) FROM unified_event)"
            ),
            {"u": COVER_A_NEW},
        )
    _catalog(fake_spotify, albumA=COVER_A, albumB=COVER_B)

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.albums_missing, result.rows_filled) == (2, 2)
    assert _by_album(engine) == {"albumA": [COVER_A_NEW, COVER_A], "albumB": [COVER_B]}


def test_an_album_without_a_usable_image_or_gone_keeps_no_cover(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    fake_spotify.albums["albumA"] = ["Synthetic Album Artist"]
    fake_spotify.album_images["albumA"] = [image("https://example.invalid/a.jpg", 300)]
    # albumB is not in the catalog: 404.

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.albums_filled, result.rows_filled) == ("succeeded", 0, 0)
    assert _by_album(engine) == {"albumA": [None, None], "albumB": [None]}


def test_a_429_waits_for_retry_after_then_continues(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    _catalog(fake_spotify, albumA=COVER_A, albumB=COVER_B)
    fake_spotify.fail_next("/v1/albums/albumA", httpx.Response(429, headers={"Retry-After": "7"}))

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.albums_filled) == ("succeeded", 2)
    assert 7.0 in fake_spotify.sleeps


def test_a_lasting_outage_stops_the_user_and_a_rerun_finishes(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    _catalog(fake_spotify, albumA=COVER_A, albumB=COVER_B)
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
    assert _by_album(engine)["albumB"] == [COVER_B]


def test_a_dead_grant_is_skipped_and_left_for_the_poller(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = _polled_user(engine, fake_spotify)
    _catalog(fake_spotify, albumA=COVER_A)
    fake_spotify.fail_next(TOKEN, httpx.Response(400, json={"error": "invalid_grant"}))

    result = backfill_user(engine, fake_spotify.client(), user)

    assert (result.status, result.error_kind) == ("failed", "invalid_grant")
    assert conn_row(engine, user).status == "active"  # the poller marks it, with its gap
    assert fake_spotify.calls("/v1/albums/albumA") == []


@pytest.mark.parametrize("status", [401, 403])
def test_refused_access_stops_the_user_at_once(
    engine: Engine, fake_spotify: FakeSpotify, status: int
) -> None:
    """WHO-148 rule 14: a revoked or refused grant ends the run; no further album is asked."""
    user = _polled_user(engine, fake_spotify)
    _catalog(fake_spotify, albumA=COVER_A, albumB=COVER_B)
    fake_spotify.fail_next("/v1/albums/albumA", httpx.Response(status))

    result = backfill_user(engine, fake_spotify.client(), user, sleep=lambda _s: None)

    assert (result.status, result.error_kind, result.albums_filled) == (
        "failed",
        f"album_http_{status}",
        0,
    )
    assert fake_spotify.calls("/v1/albums/albumB") == []
    assert _by_album(engine) == {"albumA": [None, None], "albumB": [None]}


def test_backfill_all_touches_each_user_only_with_their_own_albums(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    first = _polled_user(engine, fake_spotify)
    second = connect_user(engine, account_id="synthetic-account-2", refresh_token="synthetic-r2")
    _catalog(fake_spotify, albumA=COVER_A, albumB=COVER_B)

    results = backfill_all(engine, fake_spotify.client(), sleep=lambda _s: None)

    assert results[first].rows_filled == 3
    assert (results[second].status, results[second].albums_missing) == ("succeeded", 0)


def test_the_backfill_logs_counts_only(
    engine: Engine, fake_spotify: FakeSpotify, caplog: pytest.LogCaptureFixture
) -> None:
    _polled_user(engine, fake_spotify, refresh_token="synthetic-secret-refresh")
    _catalog(fake_spotify, albumA=COVER_A, albumB=COVER_B)
    caplog.set_level(logging.DEBUG, logger="app")

    backfill_all(engine, fake_spotify.client(), sleep=lambda _s: None)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "artwork backfill" in logged
    for secret in ("synthetic-secret-refresh", "synthetic-access", "albumA", "i.scdn.co"):
        assert secret not in logged
