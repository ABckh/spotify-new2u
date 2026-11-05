"""The dedupe key recipe (schema revision 3 §4.1), pinned byte for byte."""

import hashlib
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.events import keys

USER = UUID("00000000-0000-4000-8000-000000000001")
PLAYED = datetime(2026, 10, 11, 23, 30, 5, 789000, tzinfo=UTC)


def spotify_key(occurred_at: datetime, home_timezone: str = "UTC") -> bytes:
    return keys.dedupe_key(
        user_id=USER,
        source="spotify",
        precision="second",
        occurred_at=occurred_at,
        item_key="id:track-1",
        home_timezone=home_timezone,
    )


def test_dedupe_key_is_the_documented_recipe() -> None:
    expected = hashlib.sha256(
        b"1|00000000-0000-4000-8000-000000000001|spotify|second|2026-10-11T23:30:05Z|id:track-1"
    ).digest()

    assert spotify_key(PLAYED) == expected


def test_day_key_uses_the_local_date_in_home_timezone() -> None:
    # 23:30 UTC on Oct 11 is Oct 12 in Helsinki and Oct 11 in Los Angeles.
    def day_key(zone: str) -> bytes:
        return keys.day_key(
            user_id=USER,
            source="spotify",
            occurred_at=PLAYED,
            item_key="id:track-1",
            home_timezone=zone,
        )

    prefix = b"1|00000000-0000-4000-8000-000000000001|spotify|"
    assert day_key("Europe/Helsinki") == hashlib.sha256(prefix + b"2026-10-12|id:track-1").digest()
    assert (
        day_key("America/Los_Angeles") == hashlib.sha256(prefix + b"2026-10-11|id:track-1").digest()
    )


def test_one_instant_in_any_offset_is_one_key() -> None:
    helsinki = PLAYED.astimezone(timezone(timedelta(hours=3)))
    los_angeles = PLAYED.astimezone(timezone(timedelta(hours=-7)))

    assert spotify_key(helsinki) == spotify_key(PLAYED) == spotify_key(los_angeles)


def test_a_second_precision_key_ignores_home_timezone() -> None:
    assert spotify_key(PLAYED, "Asia/Tokyo") == spotify_key(PLAYED, "UTC")


def test_the_key_truncates_to_the_second_and_does_not_round() -> None:
    assert spotify_key(PLAYED) == spotify_key(PLAYED.replace(microsecond=0))
    assert spotify_key(PLAYED) != spotify_key(PLAYED.replace(microsecond=0) + timedelta(seconds=1))


def test_a_timezone_regression_changes_the_key() -> None:
    """Reading a UTC wall time as local time must not silently match (§9 guard)."""
    misread = PLAYED.replace(tzinfo=timezone(timedelta(hours=3)))

    assert spotify_key(misread) != spotify_key(PLAYED)


def test_a_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError):
        spotify_key(PLAYED.replace(tzinfo=None))
    with pytest.raises(ValueError):
        keys.local_date(PLAYED.replace(tzinfo=None), "UTC")


def test_normalize_absorbs_case_whitespace_and_unicode_form_only() -> None:
    assert keys.normalize_title("  Café  del   MAR ") == "café del mar"
    assert keys.normalize_title("Café") == keys.normalize_title("Café")
    assert keys.item_key_from_title("Synthetic Movie") != keys.item_key_from_title(
        "Synthetic Movie (2019)"
    )


def test_item_keys_are_namespaced() -> None:
    assert keys.item_key_from_provider_id("abc") == keys.ItemKey("id:abc", "provider_id")
    title_key = keys.item_key_from_title("Synthetic")
    assert title_key.source == "normalized_title"
    assert title_key.value == "nt:" + hashlib.sha256(b"synthetic").hexdigest()[:32]


def test_local_date() -> None:
    assert keys.local_date(PLAYED, "America/Los_Angeles") == date(2026, 10, 11)
    assert keys.local_date(PLAYED, "Europe/Helsinki") == date(2026, 10, 12)
