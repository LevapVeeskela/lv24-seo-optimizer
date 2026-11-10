"""Contract tests for core.timeutil: durations, ISO dates and reporting windows.

Everything here is offline and deterministic: windows are always checked against explicit dates
so no clock or timezone database is assumed beyond the module's own fallback table.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.core.timeutil import (
    UTC as TIMEUTIL_UTC,
)
from lv24_seo.core.timeutil import (
    compare_period,
    days_between,
    format_duration,
    local_today,
    parse_duration,
    period_window,
    previous_window,
    to_iso_date,
    utc_now,
)

MINSK_FALLBACK_DAYS = 3  # no tz data on Windows: Europe/Minsk resolves via the fixed table


def _error(excinfo: pytest.ExceptionInfo[SeoError], code: str) -> SeoError:
    assert excinfo.value.code == code
    return excinfo.value


# --- UTC now / local today -------------------------------------------------------------------


def test_utc_constant_is_timezone_utc() -> None:
    assert TIMEUTIL_UTC is UTC


def test_utc_now_is_aware_and_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert abs((now - datetime.now(UTC)).total_seconds()) < 60


def test_local_today_is_a_date_in_the_requested_zone() -> None:
    today = local_today()
    assert isinstance(today, date)
    assert not isinstance(today, datetime)
    assert today == local_today("Europe/Minsk")
    assert abs((today - datetime.now(UTC).date()).days) <= 1
    assert local_today("UTC") == datetime.now(UTC).date()


# --- durations: parse ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("15m", timedelta(minutes=15)),
        ("12h", timedelta(hours=12)),
        ("7d", timedelta(days=7)),
        ("4w", timedelta(days=28)),
        ("1d12h", timedelta(days=1, hours=12)),
        ("1w2d3h4m5s", timedelta(weeks=1, days=2, hours=3, minutes=4, seconds=5)),
        (" 2D ", timedelta(days=2)),
        ("90m", timedelta(minutes=90)),
        ("0s", timedelta(0)),
    ],
)
def test_parse_duration_accepts_compact_values(raw: str, expected: timedelta) -> None:
    assert parse_duration(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "7", "d", "7x", "-7d", "1.5h", "7d junk", "7d-2h", "7d12", "1,5h"],
)
def test_parse_duration_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(SeoError) as excinfo:
        parse_duration(raw)
    _error(excinfo, INVALID_INPUT)


def test_parse_duration_rejects_non_string() -> None:
    with pytest.raises(SeoError) as excinfo:
        parse_duration(None)  # type: ignore[arg-type]
    _error(excinfo, INVALID_INPUT)


def test_parse_duration_rejects_overflow() -> None:
    with pytest.raises(SeoError) as excinfo:
        parse_duration("9" * 25 + "w")
    _error(excinfo, INVALID_INPUT)


# --- durations: format -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (timedelta(0), "0s"),
        (timedelta(seconds=30), "30s"),
        (timedelta(minutes=15), "15m"),
        (timedelta(hours=12), "12h"),
        (timedelta(days=7), "7d"),
        (timedelta(hours=36), "1d12h"),
        (timedelta(minutes=90), "1h30m"),
        (timedelta(hours=1, minutes=1, seconds=1), "1h1m1s"),
        (timedelta(days=1, hours=1, minutes=1, seconds=1), "1d1h1m1s"),
        (timedelta(days=28), "28d"),
    ],
)
def test_format_duration_is_compact(value: timedelta, expected: str) -> None:
    assert format_duration(value) == expected


@pytest.mark.parametrize(
    "value",
    [timedelta(seconds=-1), timedelta(microseconds=500_000)],
)
def test_format_duration_rejects_lossy_values(value: timedelta) -> None:
    with pytest.raises(SeoError) as excinfo:
        format_duration(value)
    _error(excinfo, INVALID_INPUT)


def test_format_duration_rejects_non_timedelta() -> None:
    with pytest.raises(SeoError) as excinfo:
        format_duration("7d")  # type: ignore[arg-type]
    _error(excinfo, INVALID_INPUT)


@pytest.mark.parametrize("raw", ["30s", "15m", "12h", "7d", "4w", "1d12h", "1w2d3h4m5s", "0s"])
def test_duration_round_trips_through_format(raw: str) -> None:
    parsed = parse_duration(raw)
    formatted = format_duration(parsed)
    assert parse_duration(formatted) == parsed
    assert format_duration(parse_duration(formatted)) == formatted


# --- ISO dates -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2028, 2, 29), "2028-02-29"),
        (date(2026, 1, 1), "2026-01-01"),
        (datetime(2026, 11, 10, 9, 30, tzinfo=UTC), "2026-11-10"),
        (datetime(2026, 1, 1, 23, 30, tzinfo=UTC), "2026-01-01"),
        (datetime(2026, 1, 1, 1, 30, tzinfo=timezone(timedelta(hours=3))), "2026-01-01"),
    ],
)
def test_to_iso_date(value: date | datetime, expected: str) -> None:
    assert to_iso_date(value) == expected


def test_to_iso_date_rejects_naive_datetime() -> None:
    with pytest.raises(SeoError) as excinfo:
        to_iso_date(datetime(2026, 11, 10, 9, 30))
    _error(excinfo, INVALID_INPUT)


def test_to_iso_date_rejects_non_date() -> None:
    with pytest.raises(SeoError) as excinfo:
        to_iso_date("2026-11-10")  # type: ignore[arg-type]
    _error(excinfo, INVALID_INPUT)


# --- period windows --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, (date(2026, 11, 10), date(2026, 11, 10))),
        (7, (date(2026, 11, 4), date(2026, 11, 10))),
        (30, (date(2026, 10, 12), date(2026, 11, 10))),
    ],
)
def test_period_window_with_explicit_end(days: int, expected: tuple[date, date]) -> None:
    window = period_window(days, end=date(2026, 11, 10))
    assert window == expected
    assert all(isinstance(day, date) and not isinstance(day, datetime) for day in window)


def test_period_window_applies_lag_to_explicit_end() -> None:
    assert period_window(7, end=date(2026, 11, 10), lag_days=3) == (
        date(2026, 11, 1),
        date(2026, 11, 7),
    )


def test_period_window_defaults_to_local_today() -> None:
    start, end = period_window(7)
    today = local_today("Europe/Minsk")
    # Tolerate the (rare) midnight flip between the module call and this assertion.
    assert end in {today, today - timedelta(days=1)}
    assert start == end - timedelta(days=6)


def test_period_window_default_end_honours_lag() -> None:
    _, end = period_window(7, lag_days=2)
    today = local_today("Europe/Minsk")
    assert end in {today - timedelta(days=2), today - timedelta(days=3)}


@pytest.mark.parametrize(
    ("end", "days", "expected_start"),
    [
        (date(2028, 2, 29), 2, date(2028, 2, 28)),
        (date(2028, 3, 1), 7, date(2028, 2, 24)),
        (date(2027, 3, 1), 7, date(2027, 2, 23)),
        (date(2028, 12, 31), 366, date(2028, 1, 1)),
        (date(2027, 12, 31), 365, date(2027, 1, 1)),
        (date(2026, 1, 3), 7, date(2025, 12, 28)),
    ],
)
def test_period_window_handles_leap_years_and_year_rollover(
    end: date, days: int, expected_start: date
) -> None:
    start, resolved_end = period_window(days, end=end)
    assert (start, resolved_end) == (expected_start, end)


def test_period_window_accepts_aware_datetime_end() -> None:
    aware = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
    assert period_window(2, end=aware) == (date(2026, 11, 9), date(2026, 11, 10))


def test_period_window_rejects_naive_datetime_end() -> None:
    with pytest.raises(SeoError) as excinfo:
        period_window(7, end=datetime(2026, 11, 10, 23, 0))
    _error(excinfo, INVALID_INPUT)


@pytest.mark.parametrize("days", [0, -3, 2.5, True])
def test_period_window_rejects_bad_days(days: int) -> None:
    with pytest.raises(SeoError) as excinfo:
        period_window(days, end=date(2026, 11, 10))
    _error(excinfo, INVALID_INPUT)


def test_period_window_rejects_negative_lag() -> None:
    with pytest.raises(SeoError) as excinfo:
        period_window(7, end=date(2026, 11, 10), lag_days=-1)
    _error(excinfo, INVALID_INPUT)


# --- previous window -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            date(2026, 11, 4),
            date(2026, 11, 10),
            (date(2026, 10, 28), date(2026, 11, 3)),
        ),
        (
            date(2026, 11, 10),
            date(2026, 11, 10),
            (date(2026, 11, 9), date(2026, 11, 9)),
        ),
        (
            date(2026, 1, 1),
            date(2026, 1, 7),
            (date(2025, 12, 25), date(2025, 12, 31)),
        ),
        (
            date(2028, 3, 1),
            date(2028, 3, 7),
            (date(2028, 2, 23), date(2028, 2, 29)),
        ),
    ],
)
def test_previous_window_is_adjacent_and_same_length(
    start: date, end: date, expected: tuple[date, date]
) -> None:
    previous = previous_window(start, end)
    assert previous == expected
    assert previous[1] == start - timedelta(days=1)
    assert days_between(*previous) == days_between(start, end)


def test_previous_window_rejects_reversed_range() -> None:
    with pytest.raises(SeoError) as excinfo:
        previous_window(date(2026, 11, 10), date(2026, 11, 4))
    _error(excinfo, INVALID_INPUT)


# --- compare period --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "end", "lag", "current", "previous"),
    [
        (
            7,
            date(2026, 11, 10),
            0,
            (date(2026, 11, 4), date(2026, 11, 10)),
            (date(2026, 10, 28), date(2026, 11, 3)),
        ),
        (
            7,
            date(2026, 11, 10),
            2,
            (date(2026, 11, 2), date(2026, 11, 8)),
            (date(2026, 10, 26), date(2026, 11, 1)),
        ),
        (
            1,
            date(2026, 1, 1),
            0,
            (date(2026, 1, 1), date(2026, 1, 1)),
            (date(2025, 12, 31), date(2025, 12, 31)),
        ),
    ],
)
def test_compare_period_returns_non_overlapping_equal_windows(
    days: int,
    end: date,
    lag: int,
    current: tuple[date, date],
    previous: tuple[date, date],
) -> None:
    resolved_current, resolved_previous = compare_period(days, end=end, lag_days=lag)
    assert resolved_current == current
    assert resolved_previous == previous
    assert resolved_previous[1] < resolved_current[0]
    assert resolved_previous[1] + timedelta(days=1) == resolved_current[0]
    assert days_between(*resolved_previous) == days_between(*resolved_current)


def test_compare_period_rejects_bad_days() -> None:
    with pytest.raises(SeoError) as excinfo:
        compare_period(0, end=date(2026, 11, 10))
    _error(excinfo, INVALID_INPUT)


# --- days_between ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2026, 11, 10), date(2026, 11, 10), 0),
        (date(2026, 11, 4), date(2026, 11, 10), 6),
        (date(2026, 1, 31), date(2026, 2, 1), 1),
        (date(2025, 12, 31), date(2026, 1, 1), 1),
        (date(2028, 2, 28), date(2028, 3, 1), 2),
    ],
)
def test_days_between(start: date, end: date, expected: int) -> None:
    assert days_between(start, end) == expected


def test_days_between_rejects_reversed_range() -> None:
    with pytest.raises(SeoError) as excinfo:
        days_between(date(2026, 11, 10), date(2026, 11, 4))
    _error(excinfo, INVALID_INPUT)


def test_days_between_rejects_non_date() -> None:
    with pytest.raises(SeoError) as excinfo:
        days_between("2026-11-04", date(2026, 11, 10))  # type: ignore[arg-type]
    _error(excinfo, INVALID_INPUT)


# --- timezone mapping ------------------------------------------------------------------------


def test_minsk_default_zone_resolves_without_tzdata() -> None:
    today = local_today("Europe/Minsk")
    # UTC+3 means Minsk is never behind UTC and never more than one day ahead.
    assert (today - datetime.now(UTC).date()).days in (0, 1)


@pytest.mark.parametrize("tz", ["Mars/Olympus", "", "  "])
def test_bad_timezone_is_a_config_error(tz: str) -> None:
    with pytest.raises(SeoError) as excinfo:
        local_today(tz)
    error = _error(excinfo, CONFIG_INVALID)
    assert error.next_action == "use an IANA zone, e.g. Europe/Minsk"


@pytest.mark.parametrize("tz", ["Mars/Olympus", ""])
def test_period_window_validates_tz_even_with_explicit_end(tz: str) -> None:
    with pytest.raises(SeoError) as excinfo:
        period_window(7, end=date(2026, 11, 10), tz=tz)
    error = _error(excinfo, CONFIG_INVALID)
    assert error.next_action == "use an IANA zone, e.g. Europe/Minsk"


def test_timezone_must_be_a_string() -> None:
    with pytest.raises(SeoError) as excinfo:
        local_today(None)  # type: ignore[arg-type]
    _error(excinfo, CONFIG_INVALID)
