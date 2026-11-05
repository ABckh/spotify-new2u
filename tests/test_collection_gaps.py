"""Stored collection gaps: opened on revoke, closed on reconnect (WHO-44, WHO-47).

Security conditions on WHO-47: only id, user, source, start and end are stored
(1); a gap never starts before the current collection start (5); the
collection-record purge never removes a gap (5); gap logs carry ids, never a
start or end time (6). Export and delete are covered in ``test_read_export.py``
and ``test_privacy_delete.py``; what the read API shows is in ``test_read_gaps.py``.
"""

import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine, text

from app.events.gaps import close_gap, gap_start, open_gap
from app.poller.spotify import poll_connection
from app.privacy.retention import purge_collection_records
from fake_spotify import TOKEN, FakeSpotify
from test_poller_spotify import T0, conn_row, connect_user

LAST_POLL = T0 + timedelta(minutes=30)
REVOKED = T0 + timedelta(hours=2)
RECONNECTED = T0 + timedelta(days=3, hours=5)


def gaps(engine: Engine, user_id: UUID) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT source, started_at, ended_at FROM collection_gap "
                    "WHERE user_id = :u ORDER BY started_at"
                ),
                {"u": user_id},
            )
        ]


def poll_ok(engine: Engine, fake: FakeSpotify, user_id: UUID, now: datetime) -> None:
    assert poll_connection(engine, fake.client(), user_id, now=now).status == "succeeded"


def revoke(engine: Engine, fake: FakeSpotify, user_id: UUID, now: datetime) -> None:
    fake.fail_next(TOKEN, httpx.Response(400, json={"error": "invalid_grant"}))
    assert poll_connection(engine, fake.client(), user_id, now=now).status == "reauth_required"


def reconnect(engine: Engine, now: datetime) -> UUID:
    return connect_user(engine, refresh_token="synthetic-refresh-1", now=now)


# --- open and close (G2, G1, S9.6) ------------------------------------------


def test_a_revoke_opens_a_gap_at_the_last_successful_poll(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    poll_ok(engine, fake_spotify, user, LAST_POLL)

    revoke(engine, fake_spotify, user, REVOKED)

    assert gaps(engine, user) == [("spotify", LAST_POLL, None)]


def test_a_revoke_before_any_poll_opens_the_gap_at_the_collection_start(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)

    revoke(engine, fake_spotify, user, REVOKED)

    assert gaps(engine, user) == [("spotify", T0, None)]


def test_a_failed_poll_that_is_not_a_revoke_opens_no_gap(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    """A 5xx or 429 is an outage, not a revoke: outage gaps are slice N (S2.10h)."""
    user = connect_user(engine)
    fake_spotify.fail_next(TOKEN, httpx.Response(503))
    fake_spotify.fail_next(TOKEN, httpx.Response(429, headers={"Retry-After": "1"}))

    poll_connection(engine, fake_spotify.client(), user, now=LAST_POLL)
    poll_connection(engine, fake_spotify.client(), user, now=REVOKED)

    assert gaps(engine, user) == []


def test_a_reconnect_closes_the_gap_at_the_new_collection_start(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    user = connect_user(engine)
    poll_ok(engine, fake_spotify, user, LAST_POLL)
    revoke(engine, fake_spotify, user, REVOKED)

    assert reconnect(engine, RECONNECTED) == user

    with engine.connect() as conn:
        collection_started_at = conn.execute(
            text("SELECT collection_started_at FROM spotify_connection WHERE user_id = :u"),
            {"u": user},
        ).scalar_one()
    assert collection_started_at == RECONNECTED
    assert gaps(engine, user) == [("spotify", LAST_POLL, RECONNECTED)]


def test_a_second_revoke_before_any_new_poll_starts_at_the_reconnect(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    """WHO-47 (5): ``last_polled_at`` is still the old poll, so it must be clamped."""
    user = connect_user(engine)
    poll_ok(engine, fake_spotify, user, LAST_POLL)
    revoke(engine, fake_spotify, user, REVOKED)
    reconnect(engine, RECONNECTED)

    revoke(engine, fake_spotify, user, RECONNECTED + timedelta(hours=1))

    assert gaps(engine, user) == [
        ("spotify", LAST_POLL, RECONNECTED),
        ("spotify", RECONNECTED, None),
    ]


def test_a_reconnect_closes_only_the_callers_gap(engine: Engine, fake_spotify: FakeSpotify) -> None:
    """WHO-49 F1: one user's reconnect must never close another user's open gap."""
    user = connect_user(engine)
    other = connect_user(engine, account_id="synthetic-account-2")
    revoke(engine, fake_spotify, user, REVOKED)
    revoke(engine, fake_spotify, other, REVOKED)

    assert reconnect(engine, RECONNECTED) == user

    assert gaps(engine, user) == [("spotify", T0, RECONNECTED)]
    assert gaps(engine, other) == [("spotify", T0, None)]
    with engine.connect() as conn:
        closed = conn.execute(
            text("SELECT count(*) FROM collection_gap WHERE ended_at IS NOT NULL")
        ).scalar_one()
    assert closed == 1


def test_signing_in_again_while_active_does_not_touch_gaps(engine: Engine) -> None:
    user = connect_user(engine)

    reconnect(engine, RECONNECTED)

    assert gaps(engine, user) == []


def test_s9_13_a_renewal_between_polls_loses_no_play_and_opens_no_gap(
    engine: Engine, fake_spotify: FakeSpotify
) -> None:
    """WHO-60: last poll 10:00, play 10:01, renewal 10:05, next run stores 10:01."""
    ten = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    user = connect_user(engine)
    poll_ok(engine, fake_spotify, user, ten)
    started = conn_row(engine, user).collection_started_at

    fake_spotify.play("track-1", "Synthetic 1", ten + timedelta(minutes=1))
    assert reconnect(engine, ten + timedelta(minutes=5)) == user
    poll_ok(engine, fake_spotify, user, ten + timedelta(minutes=15))

    renewed = conn_row(engine, user)
    assert renewed.status == "active"
    assert renewed.collection_started_at == started
    assert renewed.authorized_at == ten + timedelta(minutes=5)
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT occurred_at FROM unified_event WHERE user_id = :u"), {"u": user}
        ).scalars()
        assert list(stored) == [ten + timedelta(minutes=1)]
    assert gaps(engine, user) == []


def test_only_one_gap_is_open_per_source(engine: Engine) -> None:
    user = connect_user(engine)
    with engine.begin() as conn:
        first = open_gap(conn, user, "spotify", T0)
        second = open_gap(conn, user, "spotify", REVOKED)

    assert (first, second) == (True, False)
    assert gaps(engine, user) == [("spotify", T0, None)]


def test_closing_with_no_open_gap_is_a_no_op(engine: Engine) -> None:
    user = connect_user(engine)
    with engine.begin() as conn:
        assert close_gap(conn, user, "spotify", RECONNECTED) is False

    assert gaps(engine, user) == []


def test_an_end_before_the_start_is_clamped_not_refused(engine: Engine) -> None:
    user = connect_user(engine)
    with engine.begin() as conn:
        open_gap(conn, user, "spotify", REVOKED)
        assert close_gap(conn, user, "spotify", T0) is True

    assert gaps(engine, user) == [("spotify", REVOKED, REVOKED)]


def test_naive_times_are_refused(engine: Engine) -> None:
    user = connect_user(engine)
    with engine.begin() as conn, pytest.raises(ValueError):
        open_gap(conn, user, "spotify", datetime(2026, 10, 11, 9, 0))


# --- minimal columns (WHO-47 (1)) --------------------------------------------


def test_the_table_holds_only_id_user_source_start_and_end(engine: Engine) -> None:
    """No reason, error text, counts, payload or IP column (W2 "Gaps in collection")."""
    with engine.connect() as conn:
        columns = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'collection_gap'"
                )
            ).scalars()
        )

    assert columns == {"id", "user_id", "source", "started_at", "ended_at"}


# --- bounds and purge (WHO-47 (5), S2.10k, S2.10l) ---------------------------


def test_a_gap_never_starts_before_collection_started() -> None:
    assert gap_start(None, T0) == T0
    assert gap_start(T0 - timedelta(days=1), T0) == T0
    assert gap_start(T0 + timedelta(minutes=5), T0) == T0 + timedelta(minutes=5)


def test_gap_start_compares_instants_not_wall_times() -> None:
    # 11:30 at +03:00 is 08:30 UTC: before T0 (09:00 UTC), though its wall time is later.
    earlier_instant = datetime(2026, 10, 11, 11, 30, tzinfo=timezone(timedelta(hours=3)))

    assert gap_start(earlier_instant, T0) == T0


def test_the_purge_never_removes_a_gap(engine: Engine, fake_spotify: FakeSpotify) -> None:
    """Records older than the window go; the gap they overlap stays."""
    user = connect_user(engine)
    revoke(engine, fake_spotify, user, REVOKED)
    reconnect(engine, RECONNECTED)

    with engine.begin() as conn:
        purged = purge_collection_records(
            conn, now=RECONNECTED + timedelta(days=400), retention=timedelta(days=90)
        )

    assert purged >= 1
    with engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM ingest_batch WHERE user_id = :u"), {"u": user}
        ).scalar_one()
    assert left == 0
    assert gaps(engine, user) == [("spotify", T0, RECONNECTED)]


# --- logs (WHO-47 (6), W6) ---------------------------------------------------


def test_gap_logs_hold_ids_but_never_a_start_or_end_time(
    engine: Engine, fake_spotify: FakeSpotify, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.events.gaps")
    user = connect_user(engine)
    poll_ok(engine, fake_spotify, user, LAST_POLL)
    revoke(engine, fake_spotify, user, REVOKED)
    reconnect(engine, RECONNECTED)

    lines = [r.getMessage() for r in caplog.records if r.name == "app.events.gaps"]

    assert [line.split(" gap_id=")[0] for line in lines] == [
        "collection gap opened",
        "collection gap closed",
    ]
    for line in lines:
        assert f"user_id={user}" in line
        for instant in (T0, LAST_POLL, REVOKED, RECONNECTED):
            for zone in (UTC, timezone(timedelta(hours=3))):
                local = instant.astimezone(zone)
                for rendering in (local.isoformat(), str(local), local.strftime("%Y-%m-%d")):
                    assert rendering not in line
