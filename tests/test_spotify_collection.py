"""PRD P15 / S1.8a / S8.9: only plays made while connected are stored.

The connect flow and the poll land with WHO-6; there the same scenarios run end
to end against the database. These tests pin the rule they will call.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.spotify.collection import plays_made_while_connected

VILNIUS = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Play:
    track_id: str
    played_at: datetime


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 10, hour, minute, second, tzinfo=UTC)


def test_s1_8a_play_before_the_connect_is_dropped_and_after_it_kept() -> None:
    """The story's example: a 09:55 play and a 10:00 connect; a 10:01 play appears."""
    recent = [Play("t-0955", at(9, 55)), Play("t-1001", at(10, 1))]

    kept = plays_made_while_connected(recent, collection_started_at=at(10, 0))

    assert [p.track_id for p in kept] == ["t-1001"]


def test_s1_8a_a_play_at_the_connect_instant_is_kept() -> None:
    recent = [Play("t-before", at(9, 59, 59)), Play("t-at", at(10, 0))]

    kept = plays_made_while_connected(recent, collection_started_at=at(10, 0))

    assert [p.track_id for p in kept] == ["t-at"]


def test_s1_8a_reconnect_after_disconnect_drops_the_plays_from_the_gap() -> None:
    """Connected 08:00, disconnected 09:00, reconnected 10:00: 09:30 is never stored."""
    recent = [Play("t-0830", at(8, 30)), Play("t-0930", at(9, 30)), Play("t-1030", at(10, 30))]

    kept = plays_made_while_connected(recent, collection_started_at=at(10, 0))

    assert [p.track_id for p in kept] == ["t-1030"]


def test_s8_9_reconnect_after_delete_never_brings_deleted_plays_back() -> None:
    """Collected 08:00-09:00, deleted at 09:30, reconnected at 10:00.

    Spotify still lists the deleted plays among its last 50; none is stored again.
    """
    deleted = [Play(f"t-08{m:02d}", at(8, m)) for m in range(0, 60, 5)]
    recent = [*deleted, Play("t-0945", at(9, 45)), Play("t-1005", at(10, 5))]

    kept = plays_made_while_connected(recent, collection_started_at=at(10, 0))

    assert [p.track_id for p in kept] == ["t-1005"]


def test_connect_moment_and_plays_compare_as_instants_across_timezones() -> None:
    """10:00 UTC is 13:00 in Vilnius; a wall-clock comparison would get this wrong."""
    recent = [Play("t-1259-local", at(9, 59).astimezone(VILNIUS)), Play("t-1001", at(10, 1))]

    kept = plays_made_while_connected(recent, collection_started_at=at(10, 0).astimezone(VILNIUS))

    assert [p.track_id for p in kept] == ["t-1001"]


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="collection_started_at"):
        plays_made_while_connected([], collection_started_at=at(10, 0).replace(tzinfo=None))
    with pytest.raises(ValueError, match="played_at"):
        plays_made_while_connected(
            [Play("t", at(10, 1).replace(tzinfo=None))], collection_started_at=at(10, 0)
        )
