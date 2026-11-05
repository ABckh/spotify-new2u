"""Collection gaps in the read API: timeline gap rows, N1 and E4 (WHO-44, D-7).

Stories S2.10d to 10g, 10j to 10l, S3.8b and S9.6; WHO-13 R-52, R-55, T-27 and
T-31. Outage gaps (S2.10h, T-29, T-30) and "collection behind" (S2.10i) are
slice N. Display zone ``Europe/Warsaw`` (UTC+2 in April, UTC+2 on 11 October)
unless a test says otherwise. Every value is synthetic.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.poller.spotify import poll_connection
from app.privacy.delete import delete_all_user_data
from app.privacy.retention import purge_collection_records
from fake_spotify import TOKEN, FakeSpotify
from read_helpers import NOW, User, add_play, add_session, get, make_user
from test_poller_spotify import connect_user

# A closed gap across midnight in Warsaw: 22:10 on the 14th to 06:35 on the 15th.
GAP_START = "2026-04-14T20:10:00Z"
GAP_END = "2026-04-15T04:35:00Z"


def add_gap(engine: Engine, user: User, start: str, end: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO collection_gap (user_id, source, started_at, ended_at) "
                "VALUES (:u, 'spotify', :start, :end)"
            ),
            {
                "u": user.id,
                "start": datetime.fromisoformat(start),
                "end": None if end is None else datetime.fromisoformat(end),
            },
        )


def timeline(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return get(api, user, "/v1/timeline", **params)


def summary(api: TestClient, user: User, **params: str) -> dict[str, Any]:
    return get(api, user, "/v1/summary", **params)


def merged(events: list[dict[str, Any]], gaps: list[dict[str, Any]]) -> list[str]:
    """Merge one page the way the contract tells the app to.

    A gap row goes after every event with ``occurred_at >= sort_at`` and before
    every event with ``occurred_at < sort_at``. Gap rows come back newest first.
    """
    out: list[str] = []
    pending = list(gaps)
    for event in events:
        while pending and pending[0]["sort_at"] > event["occurred_at"]:
            gap = pending.pop(0)
            out.append(f"gap {gap['local_date']}")
        out.append(f"play {event['occurred_at']} {event['local_date']}")
    out += [f"gap {gap['local_date']}" for gap in pending]
    return out


def all_pages(api: TestClient, user: User, **params: str) -> list[dict[str, Any]]:
    pages = [timeline(api, user, **params)]
    while pages[-1]["next_cursor"] is not None:
        pages.append(timeline(api, user, **params, cursor=pages[-1]["next_cursor"]))
    return pages


# --- S2.10d: one row per local day, same start and end ----------------------


def test_a_gap_across_midnight_shows_under_both_days_with_the_same_times(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    add_gap(engine, user, GAP_START, GAP_END)

    body = timeline(read_api, user, period="week", anchor="2026-04-15")

    assert body["gaps"] == [
        {
            "source": "spotify",
            "state": "closed",
            "started_at": "2026-04-14T20:10:00.000Z",
            "ended_at": "2026-04-15T04:35:00.000Z",
            "local_date": "2026-04-15",
            "sort_at": "2026-04-15T04:35:00.000Z",
        },
        {
            "source": "spotify",
            "state": "closed",
            "started_at": "2026-04-14T20:10:00.000Z",
            "ended_at": "2026-04-15T04:35:00.000Z",
            "local_date": "2026-04-14",
            # Local midnight starting the 15th: the row tops the 14th.
            "sort_at": "2026-04-14T22:00:00.000Z",
        },
    ]


def test_a_day_period_shows_only_its_own_row_of_the_gap(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    add_gap(engine, user, GAP_START, GAP_END)

    for day in ("2026-04-14", "2026-04-15"):
        body = timeline(read_api, user, period="day", anchor=day)
        assert [g["local_date"] for g in body["gaps"]] == [day]
        assert body["gaps"][0]["started_at"] == "2026-04-14T20:10:00.000Z"
        assert body["gaps"][0]["ended_at"] == "2026-04-15T04:35:00.000Z"
    assert timeline(read_api, user, period="day", anchor="2026-04-16")["gaps"] == []


def test_a_gap_ending_exactly_at_local_midnight_touches_only_the_day_before(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    add_gap(engine, user, "2026-04-14T10:00:00Z", "2026-04-14T22:00:00Z")

    body = timeline(read_api, user, period="week", anchor="2026-04-15")

    assert [g["local_date"] for g in body["gaps"]] == ["2026-04-14"]


def test_an_open_gap_runs_to_now_and_reads_open(read_api: TestClient, engine: Engine) -> None:
    """G2: ``ended_at`` is null; the row reaches today (NOW is 2026-10-11 14:00 Warsaw)."""
    user = make_user(engine)
    add_play(engine, user, "2026-10-09T08:00:00Z")
    add_gap(engine, user, "2026-10-09T09:00:00Z", None)

    body = timeline(read_api, user, period="week", offset="0")

    assert [(g["state"], g["ended_at"], g["local_date"]) for g in body["gaps"]] == [
        ("open", None, "2026-10-11"),
        ("open", None, "2026-10-10"),
        ("open", None, "2026-10-09"),
    ]
    assert timeline(read_api, user, period="day", anchor="2026-10-12")["gaps"] == []


def test_two_gaps_on_one_day_give_two_rows(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    add_gap(engine, user, "2026-04-14T06:00:00Z", "2026-04-14T07:00:00Z")
    add_gap(engine, user, "2026-04-14T15:00:00Z", "2026-04-14T16:00:00Z")

    body = timeline(read_api, user, period="day", anchor="2026-04-14")

    assert [(g["started_at"], g["sort_at"]) for g in body["gaps"]] == [
        ("2026-04-14T15:00:00.000Z", "2026-04-14T16:00:00.000Z"),
        ("2026-04-14T06:00:00.000Z", "2026-04-14T07:00:00.000Z"),
    ]


# --- S2.10e: where the row sits ---------------------------------------------


def test_the_row_sits_below_plays_at_or_after_its_end_and_above_plays_at_or_before_its_start(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    for at, item in (
        ("2026-04-14T20:00:00Z", "BEFORE"),
        (GAP_START, "AT_START"),
        (GAP_END, "AT_END"),
        ("2026-04-15T05:00:00Z", "AFTER"),
    ):
        add_play(engine, user, at, item=item)
    add_gap(engine, user, GAP_START, GAP_END)

    body = timeline(read_api, user, period="week", anchor="2026-04-15")

    assert merged(body["events"], body["gaps"]) == [
        "play 2026-04-15T05:00:00.000Z 2026-04-15",
        "play 2026-04-15T04:35:00.000Z 2026-04-15",
        "gap 2026-04-15",
        "gap 2026-04-14",
        "play 2026-04-14T20:10:00.000Z 2026-04-14",
        "play 2026-04-14T20:00:00.000Z 2026-04-14",
    ]


# --- S2.10f: not a play, never on two pages ---------------------------------


def test_gap_rows_are_never_counted(read_api: TestClient, engine: Engine) -> None:
    """S2.10f and S3.8b: totals count stored plays only; N1 is a flag beside them."""
    user, control = make_user(engine), make_user(engine)
    for who in (user, control):
        add_play(engine, who, "2026-04-14T08:00:00Z", item="AAA")
        add_play(engine, who, "2026-04-15T08:00:00Z", item="BBB")
    add_gap(engine, user, GAP_START, GAP_END)

    with_gap = summary(read_api, user, period="week", anchor="2026-04-15")
    without = summary(read_api, control, period="week", anchor="2026-04-15")
    page = timeline(read_api, user, period="week", anchor="2026-04-15")

    assert with_gap["totals"] == without["totals"]
    assert with_gap["totals"]["play_count"] == 2
    assert with_gap["top_tracks"] == without["top_tracks"]
    assert (with_gap["is_empty"], with_gap["empty_reason"]) == (False, None)
    assert with_gap["collection_gap_overlap"] is True
    assert without["collection_gap_overlap"] is False
    assert len(page["events"]) == 2


def test_each_gap_row_is_on_exactly_one_page_at_every_page_size(
    read_api: TestClient, engine: Engine
) -> None:
    """S2.7 and S2.10f, with ties: two plays exactly at the gap's end."""
    user = make_user(engine)
    for at, item in (
        ("2026-04-13T08:00:00Z", "A"),
        ("2026-04-14T20:00:00Z", "B"),
        (GAP_START, "C"),
        (GAP_END, "D"),
        (GAP_END, "E"),
        ("2026-04-15T05:00:00Z", "F"),
        ("2026-04-16T09:00:00Z", "G"),
    ):
        add_play(engine, user, at, item=item)
    add_gap(engine, user, GAP_START, GAP_END)
    add_gap(engine, user, "2026-04-16T10:00:00Z", "2026-04-16T11:00:00Z")
    whole = timeline(read_api, user, period="week", anchor="2026-04-15", limit="200")
    expected = merged(whole["events"], whole["gaps"])
    assert len(whole["gaps"]) == 3

    for size in range(1, 9):
        pages = all_pages(read_api, user, period="week", anchor="2026-04-15", limit=str(size))
        walked = [line for page in pages for line in merged(page["events"], page["gaps"])]
        gap_rows = [(g["started_at"], g["local_date"]) for page in pages for g in page["gaps"]]

        assert walked == expected, size
        assert len(gap_rows) == len(set(gap_rows)) == 3, size


def test_gaps_are_never_exported_as_events(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    add_gap(engine, user, GAP_START, GAP_END)

    document = read_api.get("/v1/export", headers=user.auth).json()

    assert len(document["events"]) == 1
    assert [g["started_at"] for g in document["collection_gaps"]] == ["2026-04-14T20:10:00Z"]


# --- S2.10g, T-31: four empty reasons, never interchanged -------------------


def test_the_four_empty_reasons_resolve_in_r52_order(read_api: TestClient, engine: Engine) -> None:
    """T-31. Coverage starts 2026-04-13 08:00Z; a gap covers 2026-04-20 to 22."""
    nothing = make_user(engine)
    add_gap(engine, nothing, "2026-04-10T00:00:00Z", "2026-04-12T00:00:00Z")
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    add_gap(engine, user, "2026-04-20T00:00:00Z", "2026-04-22T00:00:00Z")
    # A gap overlapping a before-coverage day still reads before_coverage (row 2 wins).
    add_gap(engine, user, "2026-04-10T00:00:00Z", "2026-04-11T00:00:00Z")

    cases = [
        (nothing, "2026-04-11", "no_data_yet"),
        (user, "2026-04-10", "before_coverage"),
        (user, "2026-04-21", "collection_gap"),
        (user, "2026-04-25", "no_plays"),
    ]
    for who, day, reason in cases:
        for path in ("/v1/timeline", "/v1/summary"):
            body = get(read_api, who, path, period="day", anchor=day)
            assert (body["is_empty"], body["empty_reason"]) == (True, reason), (path, day)
    assert summary(read_api, user, period="day", anchor="2026-04-25")["collection_gap_overlap"] is (
        False
    )


def test_an_empty_period_overlapping_a_gap_still_lists_the_gap_rows(
    read_api: TestClient, engine: Engine
) -> None:
    """S2.10g: E4 above, the gap rows of 10d below it."""
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    add_gap(engine, user, GAP_START, GAP_END)

    body = timeline(read_api, user, period="day", anchor="2026-04-15")

    assert (body["events"], body["empty_reason"]) == ([], "collection_gap")
    assert [g["local_date"] for g in body["gaps"]] == ["2026-04-15"]


def test_a_period_touching_a_gap_only_at_its_end_instant_is_not_a_gap(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    # Ends at local midnight starting the 15th: the 15th does not overlap it.
    add_gap(engine, user, "2026-04-14T10:00:00Z", "2026-04-14T22:00:00Z")

    body = summary(read_api, user, period="day", anchor="2026-04-15")

    assert body["empty_reason"] == "no_plays"
    assert body["collection_gap_overlap"] is False


# --- S2.10j: a time zone change re-renders the rows, never the times --------


def test_a_time_zone_change_moves_rows_to_the_days_they_touch_there(
    read_api: TestClient, engine: Engine
) -> None:
    user = make_user(engine)
    add_play(engine, user, "2026-04-13T08:00:00Z")
    # 20:10Z to 21:50Z: late on the 14th in Warsaw, early on the 15th in Tokyo.
    add_gap(engine, user, "2026-04-14T20:10:00Z", "2026-04-14T21:50:00Z")
    before = timeline(read_api, user, period="week", anchor="2026-04-15")["gaps"]

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE app_user SET home_timezone = 'Asia/Tokyo' WHERE id = :u"), {"u": user.id}
        )
    after = timeline(read_api, user, period="week", anchor="2026-04-15")["gaps"]

    assert [g["local_date"] for g in before] == ["2026-04-14"]
    assert [g["local_date"] for g in after] == ["2026-04-15"]
    for row in before + after:
        assert (row["started_at"], row["ended_at"]) == (
            "2026-04-14T20:10:00.000Z",
            "2026-04-14T21:50:00.000Z",
        )


# --- S9.6, T-27, S2.10k, S2.10l: through the real revoke and reconnect ------

CONNECTED = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
LAST_POLL = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
REVOKED = datetime(2026, 10, 7, 18, 0, tzinfo=UTC)


def signed_in(engine: Engine, user_id: UUID) -> User:
    token = secrets.token_urlsafe(32)
    with engine.begin() as conn:
        add_session(conn, user_id, token)
    return User(user_id, token, "Europe/Warsaw")


def connected_and_revoked(engine: Engine, fake: FakeSpotify) -> User:
    """Connected on the 5th, plays on the 5th and 6th, revoked on the 7th."""
    user_id = connect_user(engine, home_timezone="Europe/Warsaw", now=CONNECTED)
    fake.play("synthetic-a", "Synthetic A", CONNECTED + timedelta(hours=1))
    fake.play("synthetic-b", "Synthetic B", LAST_POLL - timedelta(hours=1))
    assert poll_connection(engine, fake.client(), user_id, now=LAST_POLL).status == "succeeded"
    fake.fail_next(TOKEN, httpx.Response(400, json={"error": "invalid_grant"}))
    assert poll_connection(engine, fake.client(), user_id, now=REVOKED).status == "reauth_required"
    return signed_in(engine, user_id)


def test_a_revoked_week_reads_as_a_gap_not_a_quiet_week(
    read_api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    """S9.6 and T-27: G2 from the last successful poll; past periods unchanged."""
    user = connected_and_revoked(engine, fake_spotify)

    past = summary(read_api, user, period="day", anchor="2026-10-06")
    later = summary(read_api, user, period="day", anchor="2026-10-09")
    today = timeline(read_api, user, period="day", offset="0")

    assert past["totals"]["play_count"] == 1
    assert past["collection_gap_overlap"] is True  # the gap starts at 20:00 that evening
    assert (later["is_empty"], later["empty_reason"]) == (True, "collection_gap")
    assert [(g["state"], g["started_at"], g["ended_at"]) for g in today["gaps"]] == [
        ("open", "2026-10-06T18:00:00.000Z", None)
    ]


def test_after_a_reconnect_the_open_gap_becomes_closed_at_the_reconnect(
    read_api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    """S9.6: G2 becomes G1, ending at the new collection start (S1.8a)."""
    user = connected_and_revoked(engine, fake_spotify)
    reconnected = NOW - timedelta(hours=2)
    connect_user(engine, home_timezone="Europe/Warsaw", refresh_token="r-1", now=reconnected)

    body = timeline(read_api, user, period="week", offset="0")

    assert {(g["state"], g["started_at"], g["ended_at"]) for g in body["gaps"]} == {
        ("closed", "2026-10-06T18:00:00.000Z", "2026-10-11T10:00:00.000Z")
    }
    # The 6th to the 11th in Warsaw: one row per day the gap touches.
    assert [g["local_date"] for g in body["gaps"]] == [
        "2026-10-11",
        "2026-10-10",
        "2026-10-09",
        "2026-10-08",
        "2026-10-07",
        "2026-10-06",
    ]


def test_after_a_delete_and_a_reconnect_earlier_gaps_are_gone(
    read_api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    """S2.10k: the gap was deleted with everything else; the new account has none."""
    user = connected_and_revoked(engine, fake_spotify)
    with engine.begin() as conn:
        assert delete_all_user_data(conn, user.id) == 1
    fresh = signed_in(
        engine, connect_user(engine, home_timezone="Europe/Warsaw", now=NOW - timedelta(hours=1))
    )

    body = timeline(read_api, fresh, period="month", offset="0")

    assert body["gaps"] == []
    assert summary(read_api, fresh, period="month", offset="0")["collection_gap_overlap"] is False


def test_purged_collection_records_never_read_as_a_gap_and_never_hide_one(
    read_api: TestClient, engine: Engine
) -> None:
    """S2.10l: a stored gap outlives the purge; missing records alone are no gap."""
    user = make_user(engine)
    add_play(engine, user, "2026-01-05T08:00:00Z")
    add_play(engine, user, "2026-01-20T08:00:00Z")
    add_gap(engine, user, "2026-01-10T08:00:00Z", "2026-01-12T08:00:00Z")
    with engine.begin() as conn:
        purge_collection_records(conn, now=NOW, retention=timedelta(days=90))

    gap_day = summary(read_api, user, period="day", anchor="2026-01-11")
    quiet_day = summary(read_api, user, period="day", anchor="2026-01-15")

    assert gap_day["empty_reason"] == "collection_gap"
    assert [
        g["local_date"] for g in timeline(read_api, user, period="day", anchor="2026-01-11")["gaps"]
    ] == ["2026-01-11"]
    assert quiet_day["empty_reason"] == "no_plays"
    assert timeline(read_api, user, period="day", anchor="2026-01-15")["gaps"] == []
