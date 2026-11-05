"""The Spotify poller against the fake Spotify and the real database.

The headline property: polling an overlapping window adds zero rows. It is
proven by row counts in the database, not argued.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine, text

from app.core.crypto import encrypt_secret
from app.poller import main as poller_main
from app.poller import spotify as poller_spotify
from app.poller.spotify import poll_connection
from app.spotify.client import SpotifyClient, SpotifyUnavailable, TokenGrant
from app.spotify.connection import connect, decrypt_refresh_token
from app.spotify.profile import SpotifyProfile
from fake_spotify import RECENT, TOKEN, FakeSpotify

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # the connect moment


def connect_user(
    engine: Engine,
    *,
    account_id: str = "synthetic-account-1",
    refresh_token: str = "synthetic-refresh-0",
    home_timezone: str = "Europe/Helsinki",
    now: datetime = T0,
) -> UUID:
    with engine.begin() as conn:
        return connect(
            conn,
            profile=SpotifyProfile(account_id, "Synthetic Listener"),
            refresh_token=refresh_token,
            home_timezone=home_timezone,
            now=now,
        ).user_id


def count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        n: int = conn.execute(text(sql), params).scalar_one()
        return n


def plays(engine: Engine) -> int:
    return count(engine, "SELECT count(*) FROM unified_event")


def conn_row(engine: Engine, user_id: UUID) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM spotify_connection WHERE user_id = :u"), {"u": user_id}
        ).one()


def after_params(fake: FakeSpotify) -> list[int]:
    return [int(r.url.params["after"]) for r in fake.calls(RECENT)]


def ms(dt: datetime) -> int:
    return (dt - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


# --- the headline property ------------------------------------------------


def test_polling_an_overlapping_window_adds_zero_rows(
    engine: Engine, fake_spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = connect_user(engine)
    client = fake_spotify.client()
    for n in range(5):
        fake_spotify.play(f"track-{n}", f"Synthetic {n}", T0 + timedelta(minutes=4 * n + 1))

    first = poll_connection(engine, client, user, now=T0 + timedelta(minutes=30))
    assert (first.status, first.rows_inserted) == ("succeeded", 5)
    assert plays(engine) == 5

    # Same listed window again, through the cursor...
    second = poll_connection(engine, client, user, now=T0 + timedelta(minutes=45))
    assert plays(engine) == 5
    assert second.rows_inserted == 0

    # ...and with the cursor forced back, so Spotify returns all five again.
    monkeypatch.setattr(poller_spotify, "_cursor", lambda *_: T0 - timedelta(hours=1))
    overlap = poll_connection(engine, client, user, now=T0 + timedelta(hours=1))
    monkeypatch.undo()
    assert overlap.rows_seen == 5
    assert overlap.rows_inserted == 0
    assert overlap.rows_skipped == 5
    assert plays(engine) == 5

    # One new play in an overlapping window: exactly one more row.
    fake_spotify.play("track-new", "Synthetic New", T0 + timedelta(minutes=50))
    poll_connection(engine, client, user, now=T0 + timedelta(hours=1, minutes=15))
    assert plays(engine) == 6


def test_the_same_track_twice_is_two_plays(engine: Engine, fake_spotify: FakeSpotify) -> None:
    user = connect_user(engine)
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1))
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=5))

    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=10))

    assert plays(engine) == 2


# --- the cursor -----------------------------------------------------------


def test_cursor_advances_across_runs_including_an_empty_one(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    client = fake_spotify.client()

    # Run 1: nothing played yet. The cursor is the connect moment.
    empty = poll_connection(engine, client, user, now=T0 + timedelta(minutes=15))
    assert (empty.status, empty.rows_seen) == ("succeeded", 0)

    # Run 2: two plays.
    p1, p2 = T0 + timedelta(minutes=20, milliseconds=789), T0 + timedelta(minutes=24)
    fake_spotify.play("track-1", "Synthetic 1", p1)
    fake_spotify.play("track-2", "Synthetic 2", p2)
    poll_connection(engine, client, user, now=T0 + timedelta(minutes=30))

    # Run 3: nothing new. The cursor stays on the last stored play.
    again = poll_connection(engine, client, user, now=T0 + timedelta(minutes=45))
    assert again.rows_seen == 0

    # Run 4: one more.
    fake_spotify.play("track-3", "Synthetic 3", T0 + timedelta(minutes=50))
    poll_connection(engine, client, user, now=T0 + timedelta(hours=1))

    assert after_params(fake_spotify) == [ms(T0) - 1, ms(T0) - 1, ms(p2), ms(p2)]
    assert plays(engine) == 3
    with engine.connect() as conn:
        batches = conn.execute(
            text(
                "SELECT status, rows_seen, rows_inserted FROM ingest_batch "
                "WHERE user_id = :u ORDER BY started_at"
            ),
            {"u": user},
        ).all()
    assert [tuple(b) for b in batches] == [
        ("succeeded", 0, 0),
        ("succeeded", 2, 2),
        ("succeeded", 0, 0),
        ("succeeded", 1, 1),
    ]
    assert conn_row(engine, user).last_polled_at == T0 + timedelta(hours=1)


def test_plays_from_before_the_connect_moment_are_not_stored(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    fake_spotify.play("old", "Synthetic Old", T0 - timedelta(minutes=5))
    fake_spotify.play("new", "Synthetic New", T0 + timedelta(minutes=5))
    # Spotify would not list the old one past the cursor; force it into the page.
    fake_spotify.fail_next(RECENT, httpx.Response(200, json={"items": fake_spotify.history}))

    result = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=10))

    assert (result.rows_seen, result.rows_inserted, result.rows_skipped) == (2, 1, 1)
    assert count(engine, "SELECT count(*) FROM unified_event WHERE occurred_at < :t", t=T0) == 0


# --- retries --------------------------------------------------------------


def test_429_is_retried_after_retry_after(engine: Engine, fake_spotify: FakeSpotify) -> None:
    user = connect_user(engine)
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1))
    fake_spotify.fail_next(RECENT, httpx.Response(429, headers={"Retry-After": "7"}))
    fake_spotify.fail_next(TOKEN, httpx.Response(429, headers={"Retry-After": "3"}))

    result = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))

    assert result.status == "succeeded"
    assert fake_spotify.sleeps == [3.0, 7.0]
    assert plays(engine) == 1


def test_500_is_retried_with_exponential_backoff(engine: Engine, fake_spotify: FakeSpotify) -> None:
    user = connect_user(engine)
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1))
    for status in (500, 502, 503):
        fake_spotify.fail_next(RECENT, httpx.Response(status))

    result = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))

    assert result.status == "succeeded"
    assert fake_spotify.sleeps == [1.0, 2.0, 4.0]
    assert plays(engine) == 1


def test_a_lasting_outage_records_a_failed_run_and_the_next_tick_catches_up(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1))
    for _ in range(5):
        fake_spotify.fail_next(RECENT, httpx.Response(500))

    failed = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))
    assert (failed.status, failed.error_kind) == ("failed", "recently_played_server_error")
    assert fake_spotify.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert conn_row(engine, user).last_polled_at is None
    assert conn_row(engine, user).status == "active"

    recovered = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=20))
    assert recovered.rows_inserted == 1
    assert (
        count(
            engine,
            "SELECT count(*) FROM ingest_batch WHERE status = 'failed' AND error_kind = :k",
            k="recently_played_server_error",
        )
        == 1
    )


def test_a_retry_after_longer_than_a_tick_gives_up_until_the_next_tick(
    fake_spotify: FakeSpotify,
) -> None:
    fake_spotify.fail_next(TOKEN, httpx.Response(429, headers={"Retry-After": "3600"}))

    with pytest.raises(SpotifyUnavailable):
        fake_spotify.client().refresh("synthetic-refresh-0")
    assert fake_spotify.sleeps == []


# --- dead grants ----------------------------------------------------------


@pytest.mark.parametrize("why", ["expired after 6 months", "revoked by the user"])
def test_a_dead_refresh_token_needs_reconnect_and_the_poller_keeps_going(
    engine: Engine, fake_spotify: FakeSpotify, caplog: pytest.LogCaptureFixture, why: str
) -> None:
    caplog.set_level(logging.INFO)
    dead = connect_user(engine, account_id="synthetic-dead", refresh_token="synthetic-refresh-dead")
    alive = connect_user(engine, account_id="synthetic-alive")
    fake_spotify.dead_refresh_tokens.add("synthetic-refresh-dead")
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1))

    # The whole tick, as the process runs it: both users, one of them dead.
    _run_one_tick(engine, fake_spotify, now=T0 + timedelta(minutes=10))

    row = conn_row(engine, dead)
    assert (row.status, row.reauth_reason) == ("reauth_required", "invalid_grant")
    assert row.refresh_token_ciphertext is None  # the dead token is not kept
    assert (
        count(
            engine,
            "SELECT count(*) FROM ingest_batch WHERE user_id = :u AND error_kind = :k",
            u=dead,
            k="invalid_grant",
        )
        == 1
    )
    # The other user was polled in the same tick.
    assert count(engine, "SELECT count(*) FROM unified_event WHERE user_id = :u", u=alive) == 1
    # Not retried: one refresh attempt for the dead token, and it is not polled again.
    refreshes = [parse_qs(r.content.decode()) for r in fake_spotify.calls(TOKEN)]
    assert sum(f["refresh_token"] == ["synthetic-refresh-dead"] for f in refreshes) == 1
    _run_one_tick(engine, fake_spotify, now=T0 + timedelta(minutes=25))
    refreshes = [parse_qs(r.content.decode()) for r in fake_spotify.calls(TOKEN)]
    assert sum(f["refresh_token"] == ["synthetic-refresh-dead"] for f in refreshes) == 1

    assert f"spotify connection needs reconnect user_id={dead} reason=invalid_grant" in caplog.text
    assert "synthetic-refresh-dead" not in caplog.text


def _run_one_tick(engine: Engine, fake: FakeSpotify, now: datetime) -> None:
    for user_id in poller_spotify.active_connections(engine):
        poll_connection(engine, fake.client(), user_id, now=now)


def test_poll_once_survives_a_crash_for_one_user(
    engine: Engine, fake_spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = connect_user(engine, account_id="synthetic-broken")
    fine = connect_user(engine, account_id="synthetic-fine")
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1))
    with engine.begin() as conn:  # a ciphertext that does not authenticate
        conn.execute(
            text("UPDATE spotify_connection SET refresh_token_ciphertext = :ct WHERE user_id = :u"),
            {"ct": encrypt_secret("x", "wrong-aad"), "u": broken},
        )
    monkeypatch.setattr(poller_main, "get_engine", lambda: engine)
    monkeypatch.setattr(poller_main, "datetime", _FixedDatetime)

    poller_main.poll_once(fake_spotify.client())

    assert count(engine, "SELECT count(*) FROM unified_event WHERE user_id = :u", u=fine) == 1
    assert conn_row(engine, broken).status == "active"  # a key problem is not a dead grant
    assert (
        count(
            engine,
            "SELECT count(*) FROM ingest_batch WHERE user_id = :u AND error_kind = :k",
            u=broken,
            k="token_decrypt_failed",
        )
        == 1
    )


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> "_FixedDatetime":
        return cls(2026, 10, 11, 9, 30, tzinfo=UTC)


def test_a_rotated_refresh_token_replaces_the_stored_one(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    fake_spotify.rotate_refresh_tokens = True

    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))

    stored = bytes(conn_row(engine, user).refresh_token_ciphertext)
    assert decrypt_refresh_token(user, stored) == "synthetic-rotated-1"


def _sign_in_again_during_refresh(
    engine: Engine, client: SpotifyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user signs in again after the poller read the token, before it writes."""
    refresh = client.refresh

    def refresh_then_sign_in_again(refresh_token: str) -> TokenGrant:
        try:
            return refresh(refresh_token)
        finally:
            connect_user(engine, refresh_token="synthetic-refresh-newer-grant")

    monkeypatch.setattr(client, "refresh", refresh_then_sign_in_again)


def test_a_rotated_token_does_not_overwrite_a_newer_grant(
    engine: Engine, fake_spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WHO-36 F4: rotation is a compare-and-swap on the ciphertext that was read."""
    user = connect_user(engine)
    fake_spotify.rotate_refresh_tokens = True
    client = fake_spotify.client()
    _sign_in_again_during_refresh(engine, client, monkeypatch)

    poll_connection(engine, client, user, now=T0 + timedelta(minutes=5))

    stored = bytes(conn_row(engine, user).refresh_token_ciphertext)
    assert decrypt_refresh_token(user, stored) == "synthetic-refresh-newer-grant"


def test_a_token_that_died_does_not_mark_a_newer_grant_for_reconnect(
    engine: Engine, fake_spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = connect_user(engine)
    fake_spotify.fail_next(TOKEN, httpx.Response(400, json={"error": "invalid_grant"}))
    client = fake_spotify.client()
    _sign_in_again_during_refresh(engine, client, monkeypatch)

    poll_connection(engine, client, user, now=T0 + timedelta(minutes=5))

    row = conn_row(engine, user)
    assert row.status == "active"
    assert decrypt_refresh_token(user, bytes(row.refresh_token_ciphertext)) == (
        "synthetic-refresh-newer-grant"
    )


# --- what is stored -------------------------------------------------------


def test_a_play_is_stored_per_the_schema_mapping(engine: Engine, fake_spotify: FakeSpotify) -> None:
    user = connect_user(engine)
    fake_spotify.play(
        "track-1",
        "Synthetic Title",
        T0 + timedelta(minutes=1, seconds=2, milliseconds=345),
        artists=["Synthetic A", "Synthetic B"],
        duration_ms=123_456,
    )

    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))

    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM unified_event")).one()
    assert row.source == "spotify"
    assert row.source_artifact == "spotify_recently_played"
    assert row.ingest_method == "api_poll"
    assert row.kind == "listen"
    assert row.occurred_at_precision == "second"
    assert row.occurred_at == T0 + timedelta(minutes=1, seconds=2, milliseconds=345)
    assert row.duration_ms == 123_456
    assert row.played_ms is None  # never inferred from duration_ms
    assert row.title == "Synthetic Title"
    assert row.creators == ["Synthetic A", "Synthetic B"]
    assert row.collection == "Synthetic Album"
    assert row.external_ids == {
        "spotify_track_id": "track-1",
        "spotify_album_id": "synthetic-album",
        "isrc": "XX0000000001",
    }
    assert row.artwork_url == "https://i.scdn.co/image/synthetic-300"  # WHO-149
    # Q4: no context. The album's own artists (WHO-135), track count (WHO-141)
    # and type (WHO-162) only.
    assert row.provider_extras == {
        "spotify_album_artists": ["Synthetic Album Artist"],
        "spotify_album_total_tracks": 10,
        "spotify_album_type": "album",
    }
    assert (row.item_key, row.item_key_source) == ("id:track-1", "provider_id")
    assert row.source_event_id is None
    assert row.ingest_batch_id is not None


def test_timestamps_are_utc_whatever_the_server_and_offset(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    """The test cluster runs in America/Los_Angeles; sessions must still be UTC."""
    user = connect_user(engine)
    played = datetime(
        2026, 10, 11, 23, 30, 5, 250000, tzinfo=UTC
    )  # 16:30 in LA, Oct 12 in Helsinki
    # Spotify sends "Z"; an equivalent +03:00 rendering must be the same play.
    fake_spotify.play("track-1", "Synthetic", played)
    item = dict(fake_spotify.history[0])
    item["played_at"] = played.astimezone(timezone(timedelta(hours=3))).isoformat()
    fake_spotify.history.insert(0, item)

    poll_connection(engine, fake_spotify.client(), user, now=played + timedelta(minutes=5))

    assert plays(engine) == 1  # the two renderings of one instant are one play
    with engine.connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar_one() == "UTC"
        stored = conn.execute(text("SELECT occurred_at FROM unified_event")).scalar_one()
        text_form = conn.execute(text("SELECT occurred_at::text FROM unified_event")).scalar_one()
    assert stored == played
    assert stored.utcoffset() == timedelta(0)
    assert text_form == "2026-10-11 23:30:05.25+00"


def test_a_naive_played_at_is_rejected_not_assumed_utc(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=1))
    fake_spotify.history[0]["played_at"] = "2026-10-11T09:01:00"
    fake_spotify.fail_next(RECENT, httpx.Response(200, json={"items": fake_spotify.history}))

    result = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=5))

    assert (result.rows_seen, result.rows_inserted, result.rows_skipped) == (1, 0, 1)


def test_a_full_page_is_logged_as_a_possible_gap(
    engine: Engine, fake_spotify: FakeSpotify, caplog: pytest.LogCaptureFixture
) -> None:
    user = connect_user(engine)
    for n in range(50):
        fake_spotify.play(f"track-{n}", "Synthetic", T0 + timedelta(seconds=30 * n + 1))

    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(hours=1))

    assert plays(engine) == 50
    assert "full page" in caplog.text


# --- the 6-month grant expiry (WHO-20) ------------------------------------


def test_the_poller_warns_once_a_day_before_the_grant_expires(
    engine: Engine, fake_spotify: FakeSpotify, caplog: pytest.LogCaptureFixture
) -> None:
    user = connect_user(engine)
    client = fake_spotify.client()
    day_149 = T0 + timedelta(days=149)
    day_151 = T0 + timedelta(days=151)

    poll_connection(engine, client, user, now=day_149)
    assert "grant expires soon" not in caplog.text

    poll_connection(engine, client, user, now=day_151)
    poll_connection(engine, client, user, now=day_151 + timedelta(minutes=15))
    assert caplog.text.count(f"spotify grant expires soon user_id={user}") == 1


# --- first poll right after a connect (WHO-87) -------------------------------


def test_a_new_connection_awaits_its_first_poll_until_one_is_attempted(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    polled = connect_user(engine, account_id="synthetic-polled")
    failing = connect_user(engine, account_id="synthetic-failing")
    with engine.begin() as conn:  # a ciphertext that does not authenticate
        conn.execute(
            text("UPDATE spotify_connection SET refresh_token_ciphertext = :ct WHERE user_id = :u"),
            {"ct": encrypt_secret("x", "wrong-aad"), "u": failing},
        )
    assert set(poller_spotify.connections_awaiting_first_poll(engine)) == {polled, failing}

    client = fake_spotify.client()
    poll_connection(engine, client, polled, now=T0 + timedelta(seconds=10))
    poll_connection(engine, client, failing, now=T0 + timedelta(seconds=10))

    assert conn_row(engine, failing).last_polled_at is None
    # A failed attempt counts too: it waits for the regular tick, not the next check.
    assert poller_spotify.connections_awaiting_first_poll(engine) == []


def test_a_reconnect_after_reauth_is_polled_again_at_once(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine, refresh_token="synthetic-refresh-dead")
    fake_spotify.dead_refresh_tokens.add("synthetic-refresh-dead")
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(minutes=1))
    assert conn_row(engine, user).status == "reauth_required"
    assert poller_spotify.connections_awaiting_first_poll(engine) == []

    connect_user(engine, refresh_token="synthetic-refresh-1", now=T0 + timedelta(days=2))

    assert poller_spotify.connections_awaiting_first_poll(engine) == [user]


def test_poll_new_connections_stores_a_play_made_after_connecting(
    engine: Engine, fake_spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = connect_user(engine, account_id="synthetic-old")
    poll_connection(engine, fake_spotify.client(), old, now=T0 + timedelta(minutes=1))
    new = connect_user(engine, account_id="synthetic-new", now=T0 + timedelta(minutes=2))
    fake_spotify.play("track-1", "Synthetic", T0 + timedelta(minutes=5))
    monkeypatch.setattr(poller_main, "get_engine", lambda: engine)
    monkeypatch.setattr(poller_main, "datetime", _FixedDatetime)  # 09:30 UTC
    batches_before = count(engine, "SELECT count(*) FROM ingest_batch WHERE user_id = :u", u=old)

    poller_main.poll_new_connections(fake_spotify.client())

    assert count(engine, "SELECT count(*) FROM unified_event WHERE user_id = :u", u=new) == 1
    assert conn_row(engine, new).last_polled_at == _FixedDatetime.now(UTC)
    # The already polled connection is left to the regular tick.
    assert (
        count(engine, "SELECT count(*) FROM ingest_batch WHERE user_id = :u", u=old)
        == batches_before
    )


class _Waits(threading.Event):
    """A shutdown event whose wait times out ``n`` times, then reports shutdown."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n = n
        self.timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout)
        if len(self.timeouts) > self.n:
            self.set()
            return True
        return False


def test_between_ticks_the_poller_checks_for_new_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[None] = []
    waits = _Waits(3)
    monkeypatch.setattr(poller_main, "_shutdown", waits)
    monkeypatch.setattr(poller_main, "build_spotify_client", lambda http: None)
    monkeypatch.setattr(poller_main, "poll_new_connections", lambda client: checks.append(None))

    poller_main._wait_for_next_tick(httpx.Client(), interval_seconds=900, check_seconds=15)

    assert checks == [None, None, None]
    assert waits.timeouts == [15, 15, 15, 15]


def test_a_failed_check_does_not_end_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_check(client: object) -> None:
        raise RuntimeError("synthetic check failure")

    waits = _Waits(2)
    monkeypatch.setattr(poller_main, "_shutdown", waits)
    monkeypatch.setattr(poller_main, "build_spotify_client", lambda http: None)
    monkeypatch.setattr(poller_main, "poll_new_connections", failing_check)

    poller_main._wait_for_next_tick(httpx.Client(), interval_seconds=900, check_seconds=15)

    assert len(waits.timeouts) == 3  # kept waiting until shutdown


def test_the_wait_ends_at_the_interval_for_the_next_full_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[None] = []
    clock = iter([0.0, 10.0, 15.0, 20.0, 20.0, 99.0])
    monkeypatch.setattr("app.poller.main.time.monotonic", lambda: next(clock))
    waits = _Waits(10)
    monkeypatch.setattr(poller_main, "_shutdown", waits)
    monkeypatch.setattr(poller_main, "build_spotify_client", lambda http: None)
    monkeypatch.setattr(poller_main, "poll_new_connections", lambda client: checks.append(None))

    poller_main._wait_for_next_tick(httpx.Client(), interval_seconds=20, check_seconds=15)

    # deadline 20: wait 10 (min of 15 and 20-10), check at 15, wait 0 at 20, done.
    assert waits.timeouts == [10.0, 0.0]
    assert checks == [None]
    assert not waits.is_set()
