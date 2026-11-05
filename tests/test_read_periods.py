"""The period resolver against the WHO-13 ``period-model`` worked examples.

Rule and example ids (R-n, W-n, T-n) are the document's. The UTC bounds are the
document's literal values, verified there against the IANA database.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.read.periods import (
    Period,
    PeriodError,
    PeriodQuery,
    local_midnight,
    resolve_period,
)

WARSAW = "Europe/Warsaw"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve(zone: str = WARSAW, now: datetime = NOW, **params: str) -> Period:
    return resolve_period(PeriodQuery(**params), zone, now)


def resolve_params(params: dict[str, str]) -> Period:
    """Resolve query parameters as sent on the wire, ``from`` included."""
    renamed = {("from_" if key == "from" else key): value for key, value in params.items()}
    return resolve_period(PeriodQuery(**renamed), WARSAW, NOW)


def custom_range(first: str, last: str) -> Period:
    return resolve(period="custom", from_=first, to=last)


def test_w1_a_plain_day_in_winter() -> None:  # T-02
    period = resolve(period="day", anchor="2026-03-10")

    assert (period.start, period.end) == (utc("2026-03-09T23:00:00Z"), utc("2026-03-10T23:00:00Z"))
    assert period.local_start_date == period.local_end_date == date(2026, 3, 10)
    assert period.is_partial is False


def test_w3_the_dst_week_is_167_hours_and_its_sunday_23() -> None:  # T-06
    week = resolve(period="week", anchor="2026-03-25")
    sunday = resolve(period="day", anchor="2026-03-29")

    assert (week.start, week.end) == (utc("2026-03-22T23:00:00Z"), utc("2026-03-29T22:00:00Z"))
    assert (week.local_start_date, week.local_end_date) == (date(2026, 3, 23), date(2026, 3, 29))
    assert week.end - week.start == timedelta(hours=167)
    assert (sunday.start, sunday.end) == (utc("2026-03-28T23:00:00Z"), utc("2026-03-29T22:00:00Z"))
    assert sunday.end - sunday.start == timedelta(hours=23)
    # 2026-03-29T22:30Z is Monday 00:30 local: next week, not this one.
    assert not week.start <= utc("2026-03-29T22:30:00Z") < week.end


def test_the_autumn_dst_day_is_25_hours() -> None:
    day = resolve(period="day", anchor="2026-10-25")

    assert day.end - day.start == timedelta(hours=25)


def test_w3_a_normal_week_is_168_hours() -> None:  # T-07
    week = resolve(period="week", anchor="2026-04-15")

    assert (week.start, week.end) == (utc("2026-04-12T22:00:00Z"), utc("2026-04-19T22:00:00Z"))
    assert week.end - week.start == timedelta(hours=168)


@pytest.mark.parametrize("anchor", ["2026-04-13", "2026-04-15", "2026-04-19"])
def test_a_week_starts_on_monday_whatever_day_anchors_it(anchor: str) -> None:  # T-08
    week = resolve(period="week", anchor=anchor)

    assert week.local_start_date == date(2026, 4, 13)
    assert week.local_start_date.weekday() == 0


def test_a_month_runs_from_the_1st_to_its_last_day() -> None:
    february = resolve(period="month", anchor="2028-02-10")

    assert (february.local_start_date, february.local_end_date) == (
        date(2028, 2, 1),
        date(2028, 2, 29),
    )
    assert february.start == utc("2028-01-31T23:00:00Z")
    assert february.end == utc("2028-02-29T23:00:00Z")


def test_w4_the_stored_zone_decides_the_day() -> None:  # T-09, T-10
    e5 = utc("2026-06-01T23:30:00Z")
    warsaw_june_2 = resolve(WARSAW, period="day", anchor="2026-06-02")
    new_york_june_2 = resolve("America/New_York", period="day", anchor="2026-06-02")
    new_york_june_1 = resolve("America/New_York", period="day", anchor="2026-06-01")

    assert warsaw_june_2.start <= e5 < warsaw_june_2.end
    assert new_york_june_2.start == utc("2026-06-02T04:00:00Z")
    assert not new_york_june_2.start <= e5 < new_york_june_2.end
    assert new_york_june_1.start <= e5 < new_york_june_1.end


def test_today_is_the_server_clock_read_in_the_stored_zone() -> None:
    # 23:30 UTC on Oct 11 is already Oct 12 in Warsaw.
    late = datetime(2026, 10, 11, 23, 30, tzinfo=UTC)

    assert resolve(now=late, period="day").local_start_date == date(2026, 10, 12)
    assert resolve("America/New_York", now=late, period="day").local_start_date == date(
        2026, 10, 11
    )


@pytest.mark.parametrize("period_type", ["day", "week", "month"])
def test_offsets_walk_consecutive_periods_with_no_gap_or_overlap(period_type: str) -> None:  # T-24
    periods = [resolve(period=period_type, offset=str(n)) for n in range(-14, 2)]

    for earlier, later in zip(periods, periods[1:], strict=False):
        assert earlier.end == later.start
        assert earlier.local_end_date + timedelta(days=1) == later.local_start_date


def test_no_offset_and_no_anchor_means_the_current_period() -> None:
    assert resolve(period="week") == resolve(period="week", offset="0")
    assert resolve(period="week").offset == 0
    assert resolve(period="week", offset="-1").local_start_date == date(2026, 9, 28)


def test_month_offsets_cross_year_ends() -> None:
    assert resolve(period="month", offset="-10").local_start_date == date(2025, 12, 1)
    assert resolve(period="month", offset="3").local_start_date == date(2027, 1, 1)


def test_the_current_period_is_partial_and_a_past_one_is_not() -> None:  # R-18
    assert resolve(period="month").is_partial is True
    assert resolve(period="month", offset="-1").is_partial is False


def test_w7_a_single_day_custom_range_equals_that_day() -> None:  # R-20
    custom = custom_range("2026-01-01", "2026-01-01")
    day = resolve(period="day", anchor="2026-01-01")

    assert (custom.start, custom.end) == (day.start, day.end)
    assert custom.local_start_date == custom.local_end_date == date(2026, 1, 1)


@pytest.mark.parametrize(
    ("first", "last"),
    [("2026-01-01", "2026-12-31"), ("2025-01-01", "2026-01-01")],  # 365 and 366 days
)
def test_w7_ranges_up_to_366_days_are_valid(first: str, last: str) -> None:
    custom = custom_range(first, last)

    assert custom.local_end_date == date.fromisoformat(last)
    assert custom.end == local_midnight(
        date.fromisoformat(last) + timedelta(days=1), ZoneInfo(WARSAW)
    )


def test_a_future_end_is_partial_not_an_error() -> None:  # T-23, R-24
    custom = custom_range("2026-10-01", "2026-12-31")

    assert custom.is_partial is True


def test_w7_last_row_is_still_too_long() -> None:
    """W-7 lists 2026-04-13..2099-01-01 as 200, but that is 26 197 days: R-21 refuses it.

    The rule wins over the example; the conflict is reported on WHO-7 for Product.
    """
    with pytest.raises(PeriodError) as raised:
        custom_range("2026-04-13", "2099-01-01")

    assert raised.value.code == "period_range_too_long"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"period": "custom", "from": "2026-04-19", "to": "2026-04-13"}, "period_range_reversed"),
        ({"period": "custom", "from": "2025-01-01", "to": "2026-01-02"}, "period_range_too_long"),
        ({"period": "custom", "from": "2026-02-30", "to": "2026-03-01"}, "period_invalid_date"),
        ({"period": "custom", "from": "2026-04-13"}, "period_range_incomplete"),
        ({"period": "custom", "to": "2026-04-13"}, "period_range_incomplete"),
        ({"period": "day", "anchor": "2026-04-13", "offset": "-1"}, "period_anchor_conflict"),
        # Ambiguous or malformed: refused, never guessed (R-22).
        ({"period": "day", "anchor": "20260413"}, "period_invalid_date"),
        ({"period": "week", "anchor": "2026-W15-1"}, "period_invalid_date"),
        ({"period": "day", "anchor": "2026-4-13"}, "period_invalid_date"),
        ({"period": "day", "anchor": ""}, "period_invalid_date"),
        ({}, "period_invalid_parameter"),
        ({"period": "year"}, "period_invalid_parameter"),
        ({"period": "Day"}, "period_invalid_parameter"),
        ({"period": "day", "offset": "1.5"}, "period_invalid_parameter"),
        ({"period": "day", "offset": ""}, "period_invalid_parameter"),
        ({"period": "day", "offset": "99999"}, "period_invalid_parameter"),
        # Non-ASCII digits (Arabic-Indic one, fullwidth 2026) are not digits here.
        ({"period": "day", "offset": "-\u0661"}, "period_invalid_parameter"),
        ({"period": "day", "anchor": "\uff12\uff10\uff12\uff16-04-13"}, "period_invalid_date"),
        ({"period": "day", "from": "2026-04-13", "to": "2026-04-13"}, "period_invalid_parameter"),
        (
            {"period": "custom", "from": "2026-04-13", "to": "2026-04-14", "offset": "0"},
            "period_invalid_parameter",
        ),
        ({"period": "month", "anchor": "9999-12-15"}, "period_invalid_parameter"),
        (
            {"period": "custom", "from": "9999-12-31", "to": "9999-12-31"},
            "period_invalid_parameter",
        ),
    ],
)
def test_w7_invalid_requests_are_refused_with_a_stable_code(  # T-21
    params: dict[str, str], code: str
) -> None:
    with pytest.raises(PeriodError) as raised:
        resolve_params(params)

    assert raised.value.code == code


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", "../../etc/passwd", "Europe"])
def test_an_unknown_stored_zone_is_refused(zone: str) -> None:  # T-21
    with pytest.raises(PeriodError) as raised:
        resolve(zone, period="day")

    assert raised.value.code == "period_invalid_time_zone"


def test_r17_a_doubled_midnight_uses_the_first() -> None:
    # America/Goose_Bay, 2000-10-29: local 00:00 happened at 03:00Z and again at 04:00Z.
    start = local_midnight(date(2000, 10, 29), ZoneInfo("America/Goose_Bay"))

    assert start == utc("2000-10-29T03:00:00Z")


def test_r17_a_missing_midnight_uses_the_first_instant_after_the_gap() -> None:
    # Africa/Cairo, 2000-04-28: clocks went from 23:59:59 straight to 01:00.
    zone = ZoneInfo("Africa/Cairo")
    start = local_midnight(date(2000, 4, 28), zone)

    assert start == utc("2000-04-27T22:00:00Z")
    assert start.astimezone(zone).replace(tzinfo=None) == datetime(2000, 4, 28, 1, 0)
    assert (start - timedelta(seconds=1)).astimezone(zone).date() == date(2000, 4, 27)
