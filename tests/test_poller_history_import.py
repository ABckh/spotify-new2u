"""The history import on first connect (WHO-89, WHO-91), against the fake Spotify and the DB.

If the user chose it at their first connect, the first poll stores the latest
50 plays Spotify lists as one ``api_backfill`` batch, once. Every other poll
keeps the WHO-29 filter. Duplicates are disproven by row counts, not argued.
"""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine, text

from app.poller import spotify as poller_spotify
from app.poller.spotify import import_recent_history, poll_connection
from app.spotify.connection import connect, mark_reauth_required
from app.spotify.profile import SpotifyProfile
from fake_spotify import RECENT, FakeSpotify

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # the first connect


def connect_user(
    engine: Engine,
    *,
    import_recent_history: bool,
    refresh_token: str = "synthetic-refresh-0",
    now: datetime = T0,
) -> UUID:
    with engine.begin() as conn:
        return connect(
            conn,
            profile=SpotifyProfile("synthetic-account-1", "Synthetic Listener"),
            refresh_token=refresh_token,
            home_timezone="Europe/Helsinki",
            now=now,
            import_recent_history=import_recent_history,
        ).user_id


def count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        n: int = conn.execute(text(sql), params).scalar_one()
        return n


def plays(engine: Engine) -> int:
    return count(engine, "SELECT count(*) FROM unified_event")


def distinct_plays(engine: Engine) -> int:
    return count(engine, "SELECT count(DISTINCT (user_id, dedupe_key)) FROM unified_event")


def batches(engine: Engine, method: str) -> list[Any]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT * FROM ingest_batch WHERE ingest_method = :m ORDER BY started_at"),
                {"m": method},
            )
        )


def connection(engine: Engine, user_id: UUID) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM spotify_connection WHERE user_id = :u"), {"u": user_id}
        ).one()


def listened_before_connect(fake: FakeSpotify, n: int) -> list[datetime]:
    """``n`` plays in the hours before T0, oldest first."""
    times = [T0 - timedelta(hours=n - i, seconds=0.25) for i in range(n)]
    for i, played_at in enumerate(times):
        fake.play(f"track-old-{i}", f"Synthetic Old {i}", played_at)
    return times


# --- first connect with the import chosen --------------------------------------


def test_first_poll_imports_the_listed_plays_as_one_backfill_batch(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    times = listened_before_connect(fake_spotify, 12)
    user = connect_user(engine, import_recent_history=True)
    assert poller_spotify.connections_awaiting_first_poll(engine) == [user]

    # A clock in another zone: everything must still land in UTC.
    now = (T0 + timedelta(seconds=10)).astimezone(timezone(timedelta(hours=3)))
    result = poll_connection(engine, fake_spotify.client(), user, now=now)

    assert result.status == "succeeded"
    assert plays(engine) == 12
    (backfill,) = batches(engine, "api_backfill")
    assert backfill.status == "succeeded"
    assert (backfill.rows_seen, backfill.rows_inserted, backfill.rows_skipped) == (12, 12, 0)
    assert (backfill.window_start, backfill.window_end) == (times[0], times[-1])
    assert backfill.window_start.utcoffset() == timedelta(0)
    assert count(engine, "SELECT count(*) FROM unified_event WHERE ingest_method = 'api_backfill'")
    assert (
        count(
            engine, "SELECT count(*) FROM unified_event WHERE ingest_batch_id = :b", b=backfill.id
        )
        == 12
    )
    # The normal poll ran after it, and found nothing newer.
    (poll,) = batches(engine, "api_poll")
    assert (poll.status, poll.rows_seen) == ("succeeded", 0)
    row = connection(engine, user)
    assert row.history_imported_at == now
    assert row.history_imported_at.utcoffset() == timedelta(0)
    # One recently-played call without `after`, then one with it.
    assert ["after" in r.url.params for r in fake_spotify.calls(RECENT)] == [False, True]
    # WHO-87: picked up once per connect, not again.
    assert poller_spotify.connections_awaiting_first_poll(engine) == []


def test_the_import_runs_once_and_later_polls_keep_the_who_29_filter(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    listened_before_connect(fake_spotify, 3)
    user = connect_user(engine, import_recent_history=True)
    client = fake_spotify.client()
    poll_connection(engine, client, user, now=T0 + timedelta(seconds=10))
    fake_spotify.play("track-live", "Synthetic Live", T0 + timedelta(minutes=5))

    later = poll_connection(engine, client, user, now=T0 + timedelta(minutes=15))

    assert later.rows_inserted == 1
    assert plays(engine) == 4
    assert len(batches(engine, "api_backfill")) == 1
    assert ["after" in r.url.params for r in fake_spotify.calls(RECENT)] == [False, True, True]
    assert count(engine, "SELECT count(*) FROM unified_event WHERE ingest_method = 'api_poll'") == 1


def test_an_empty_history_is_imported_once_and_not_retried(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine, import_recent_history=True)
    client = fake_spotify.client()

    poll_connection(engine, client, user, now=T0 + timedelta(seconds=10))
    poll_connection(engine, client, user, now=T0 + timedelta(minutes=15))

    (backfill,) = batches(engine, "api_backfill")
    assert (backfill.status, backfill.rows_seen, backfill.window_start) == ("succeeded", 0, None)
    assert connection(engine, user).history_imported_at == T0 + timedelta(seconds=10)
    assert plays(engine) == 0


# --- no duplicates -------------------------------------------------------------


def test_importing_twice_adds_zero_rows(engine: Engine, fake_spotify: FakeSpotify) -> None:
    listened_before_connect(fake_spotify, 50)
    user = connect_user(engine, import_recent_history=True)
    client = fake_spotify.client()
    poll_connection(engine, client, user, now=T0 + timedelta(seconds=10))
    assert plays(engine) == 50

    # Forget that it ran, so the next poll imports the same 50 again.
    with engine.begin() as conn:
        conn.execute(text("UPDATE spotify_connection SET history_imported_at = NULL"))
    poll_connection(engine, client, user, now=T0 + timedelta(minutes=15))

    first, second = batches(engine, "api_backfill")
    assert (second.rows_seen, second.rows_inserted, second.rows_skipped) == (50, 0, 50)
    assert plays(engine) == 50
    assert distinct_plays(engine) == 50


def test_a_second_import_call_after_success_writes_nothing(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    listened_before_connect(fake_spotify, 5)
    user = connect_user(engine, import_recent_history=True)
    client = fake_spotify.client()
    poll_connection(engine, client, user, now=T0 + timedelta(seconds=10))

    # As if two pollers raced: the loser sees the import done under the lock.
    again = import_recent_history(
        engine, client, "synthetic-access-x", user, "Europe/Helsinki", T0 + timedelta(seconds=11)
    )

    assert (again.status, again.error_kind) == ("failed", "import_not_due")
    assert len(batches(engine, "api_backfill")) == 1
    assert plays(engine) == 5


def test_import_and_an_overlapping_first_poll_add_zero_duplicates(
    engine: Engine, fake_spotify: FakeSpotify, monkeypatch: pytest.MonkeyPatch
) -> None:
    listened_before_connect(fake_spotify, 4)
    user = connect_user(engine, import_recent_history=True)
    # Played after connecting but before the first poll: both paths see these.
    fake_spotify.play("track-live-1", "Synthetic Live 1", T0 + timedelta(seconds=2))
    fake_spotify.play("track-live-2", "Synthetic Live 2", T0 + timedelta(seconds=5))
    # Force the poll's cursor back so it asks for everything the import stored.
    monkeypatch.setattr(poller_spotify, "_cursor", lambda *_: T0 - timedelta(days=1))

    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(seconds=10))

    (backfill,) = batches(engine, "api_backfill")
    (poll,) = batches(engine, "api_poll")
    assert backfill.rows_inserted == 6
    # The poll's WHO-29 filter keeps the two live plays; both are already stored.
    assert (poll.rows_seen, poll.rows_inserted, poll.rows_skipped) == (6, 0, 6)
    assert plays(engine) == 6
    assert distinct_plays(engine) == 6


# --- not chosen, renewals and reconnects ----------------------------------------


def test_without_the_choice_nothing_from_before_connect_is_stored(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    listened_before_connect(fake_spotify, 5)
    user = connect_user(engine, import_recent_history=False)
    fake_spotify.play("track-live", "Synthetic Live", T0 + timedelta(seconds=3))
    # Spotify answering with everything it has: the WHO-29 filter must drop the old five.
    fake_spotify.fail_next(RECENT, httpx.Response(200, json={"items": fake_spotify.history}))

    result = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(seconds=10))

    assert (result.rows_seen, result.rows_inserted) == (6, 1)
    assert plays(engine) == 1
    assert batches(engine, "api_backfill") == []
    assert all("after" in r.url.params for r in fake_spotify.calls(RECENT))
    row = connection(engine, user)
    assert (row.history_import_requested, row.history_imported_at) == (False, None)


def test_a_renewal_asking_for_the_import_never_imports(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    listened_before_connect(fake_spotify, 5)
    user = connect_user(engine, import_recent_history=False)
    renewed = connect_user(
        engine,
        import_recent_history=True,
        refresh_token="synthetic-refresh-1",
        now=T0 + timedelta(days=1),
    )
    assert renewed == user
    assert connection(engine, user).history_import_requested is False

    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(days=1, minutes=1))

    assert plays(engine) == 0
    assert batches(engine, "api_backfill") == []


def test_a_reconnect_after_reauth_asking_for_the_import_never_imports(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine, import_recent_history=False)
    with engine.begin() as conn:
        ciphertext = conn.execute(
            text("SELECT refresh_token_ciphertext FROM spotify_connection")
        ).scalar_one()
        assert mark_reauth_required(conn, user, "invalid_grant", read_ciphertext=bytes(ciphertext))
    listened_before_connect(fake_spotify, 5)

    connect_user(
        engine,
        import_recent_history=True,
        refresh_token="synthetic-refresh-1",
        now=T0 + timedelta(days=2),
    )
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(days=2, minutes=1))

    assert connection(engine, user).history_import_requested is False
    assert plays(engine) == 0
    assert batches(engine, "api_backfill") == []


def test_a_renewal_after_the_import_does_not_import_again(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    listened_before_connect(fake_spotify, 5)
    user = connect_user(engine, import_recent_history=True)
    client = fake_spotify.client()
    poll_connection(engine, client, user, now=T0 + timedelta(seconds=10))

    connect_user(
        engine,
        import_recent_history=True,
        refresh_token="synthetic-refresh-1",
        now=T0 + timedelta(days=1),
    )
    poll_connection(engine, client, user, now=T0 + timedelta(days=1, seconds=10))

    assert len(batches(engine, "api_backfill")) == 1
    assert connection(engine, user).history_imported_at == T0 + timedelta(seconds=10)


# --- failures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "attempts", "error_kind"),
    [
        # A Retry-After past the tick gives up at once; a 5xx is tried 5 times.
        (httpx.Response(429, headers={"Retry-After": "3600"}), 1, "recently_played_rate_limited"),
        (httpx.Response(500), 5, "recently_played_server_error"),
    ],
)
def test_a_failed_import_is_recorded_does_not_stop_the_poll_and_is_retried(
    engine: Engine,
    fake_spotify: FakeSpotify,
    response: httpx.Response,
    attempts: int,
    error_kind: str,
) -> None:
    listened_before_connect(fake_spotify, 3)
    user = connect_user(engine, import_recent_history=True)
    fake_spotify.play("track-live", "Synthetic Live", T0 + timedelta(seconds=3))
    for _ in range(attempts):  # every attempt the client makes
        fake_spotify.fail_next(RECENT, response)
    client = fake_spotify.client()

    first = poll_connection(engine, client, user, now=T0 + timedelta(seconds=10))

    (failed,) = batches(engine, "api_backfill")
    assert (failed.status, failed.error_kind) == ("failed", error_kind)
    assert connection(engine, user).history_imported_at is None
    # The live poll still ran and stored the play made after connecting.
    assert (first.status, first.rows_inserted) == ("succeeded", 1)
    assert plays(engine) == 1
    # WHO-87: the attempt counts, so the fast path does not retry it every few seconds.
    assert poller_spotify.connections_awaiting_first_poll(engine) == []

    poll_connection(engine, client, user, now=T0 + timedelta(minutes=15))

    _, retried = batches(engine, "api_backfill")
    assert (retried.status, retried.rows_seen, retried.rows_inserted) == ("succeeded", 4, 3)
    assert connection(engine, user).history_imported_at == T0 + timedelta(minutes=15)
    assert plays(engine) == 4
    assert distinct_plays(engine) == 4


@pytest.mark.parametrize("why", ["expired after 6 months", "revoked by the user"])
def test_a_dead_grant_imports_nothing_and_keeps_the_import_pending(
    engine: Engine, fake_spotify: FakeSpotify, why: str
) -> None:
    listened_before_connect(fake_spotify, 3)
    user = connect_user(engine, import_recent_history=True, refresh_token="synthetic-refresh-dead")
    fake_spotify.dead_refresh_tokens.add("synthetic-refresh-dead")

    result = poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(seconds=10))

    assert result.status == "reauth_required"
    assert fake_spotify.calls(RECENT) == []
    assert batches(engine, "api_backfill") == []
    assert plays(engine) == 0
    row = connection(engine, user)
    assert (row.history_import_requested, row.history_imported_at) == (True, None)


def test_a_pending_import_lapses_on_reconnect_after_reauth(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    # WHO-93 F1: the grant dies before the import runs, the user listens while we
    # hold no grant, then reconnects. Those gap plays must never be stored.
    user = connect_user(engine, import_recent_history=True, refresh_token="synthetic-refresh-dead")
    fake_spotify.dead_refresh_tokens.add("synthetic-refresh-dead")
    client = fake_spotify.client()
    dead = poll_connection(engine, client, user, now=T0 + timedelta(seconds=10))
    assert dead.status == "reauth_required"
    fake_spotify.play("track-gap", "Synthetic Gap", T0 + timedelta(days=1))
    reconnect_at = T0 + timedelta(days=2)

    connect_user(
        engine,
        import_recent_history=False,
        refresh_token="synthetic-refresh-1",
        now=reconnect_at,
    )
    assert connection(engine, user).history_import_requested is False
    result = poll_connection(engine, client, user, now=reconnect_at + timedelta(minutes=1))

    assert result.status == "succeeded"
    assert (
        count(
            engine,
            "SELECT count(*) FROM unified_event WHERE occurred_at >= :a AND occurred_at < :b",
            a=T0,
            b=reconnect_at,
        )
        == 0
    )
    assert plays(engine) == 0
    assert [b for b in batches(engine, "api_backfill") if b.status == "succeeded"] == []
    row = connection(engine, user)
    assert (row.history_import_requested, row.history_imported_at) == (False, None)


def test_a_reconnect_after_reauth_keeps_a_finished_import_recorded(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    listened_before_connect(fake_spotify, 2)
    user = connect_user(engine, import_recent_history=True)
    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(seconds=10))
    with engine.begin() as conn:
        ciphertext = conn.execute(
            text("SELECT refresh_token_ciphertext FROM spotify_connection")
        ).scalar_one()
        assert mark_reauth_required(conn, user, "invalid_grant", read_ciphertext=bytes(ciphertext))

    connect_user(
        engine,
        import_recent_history=False,
        refresh_token="synthetic-refresh-1",
        now=T0 + timedelta(days=2),
    )

    row = connection(engine, user)
    assert (row.history_import_requested, row.history_imported_at) == (
        True,
        T0 + timedelta(seconds=10),
    )


def test_an_unreadable_item_is_skipped_and_counted(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    listened_before_connect(fake_spotify, 2)
    unreadable = {"played_at": "not-a-time", "track": {"name": "Synthetic"}}
    fake_spotify.fail_next(
        RECENT, httpx.Response(200, json={"items": [unreadable, *fake_spotify.history]})
    )
    user = connect_user(engine, import_recent_history=True)

    poll_connection(engine, fake_spotify.client(), user, now=T0 + timedelta(seconds=10))

    (backfill,) = batches(engine, "api_backfill")
    assert (backfill.rows_seen, backfill.rows_inserted, backfill.rows_skipped) == (3, 2, 1)
    assert plays(engine) == 2
