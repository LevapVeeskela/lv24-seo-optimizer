"""Time and period helpers shared by every report and CLI command.

Why one module
--------------
Search providers report by **calendar day in a local zone**, not by rolling 24-hour slices, and
GSC / Yandex Webmaster data lags two to three days. Reports, scheduled runs and ad-hoc commands
must all compute the same window, so the math lives here and nowhere else.

Rules
-----
* Windows are **inclusive** calendar days; ``period_window(7)`` covers 7 days, not 8.
* ``end`` defaults to *today in ``tz``*; ``lag_days`` then shifts that end date back.
* ``previous_window`` is the same length and never overlaps the window it precedes.
* No naive datetimes: a ``datetime`` without a timezone is rejected at the boundary.
* A wrong zone is a config error (:data:`CONFIG_INVALID`), never a silent UTC fallback.

Timezone data
-------------
CPython ships no tz database; on Windows (and in slim containers) :mod:`zoneinfo` only works
when the ``tzdata`` package is installed. To keep the default zone usable without adding a
runtime dependency, :data:`_FIXED_OFFSET_ZONES` resolves a few zones that have no DST today.
``Europe/Minsk`` is permanent UTC+3 since 2011, so the fallback is exact rather than an
approximation. Any other zone needs real tz data; installing ``tzdata`` is enough.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, SeoError

__all__ = [
    "UTC",
    "compare_period",
    "days_between",
    "format_duration",
    "local_today",
    "parse_duration",
    "period_window",
    "previous_window",
    "to_iso_date",
    "utc_now",
]

_DURATION_PART = re.compile(r"(?P<count>\d+)(?P<unit>[smhdw])")
_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
# Formatting never emits weeks: "7d" is unambiguous, "1w" is not (a working week is 5 days).
_FORMAT_UNITS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))

_FIXED_OFFSET_ZONES: dict[str, timedelta] = {
    "Europe/Minsk": timedelta(hours=3),  # permanent UTC+3, no DST since 2011
    "UTC": timedelta(0),
    "Etc/UTC": timedelta(0),
    "GMT": timedelta(0),
}

_ZONE_NEXT_ACTION = "use an IANA zone, e.g. Europe/Minsk"


def _invalid(message: str, next_action: str | None = None) -> SeoError:
    return SeoError(code=INVALID_INPUT, message=message, next_action=next_action)


def _config_invalid(message: str) -> SeoError:
    return SeoError(code=CONFIG_INVALID, message=message, next_action=_ZONE_NEXT_ACTION)


def _require_int(value: int, *, name: str, minimum: int) -> int:
    candidate: object = value
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise _invalid(f"{name} must be an integer, got {type(candidate).__name__}")
    if candidate < minimum:
        raise _invalid(f"{name} must be >= {minimum}, got {candidate!r}")
    return candidate


def _as_date(value: date | datetime, *, what: str) -> date:
    candidate: object = value
    if isinstance(candidate, datetime):
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise _invalid(
                f"{what} must be timezone-aware, got a naive datetime",
                "pass an aware datetime, e.g. utc_now()",
            )
        return candidate.date()
    if isinstance(candidate, date):
        return candidate
    raise _invalid(f"{what} must be a date or datetime, got {type(candidate).__name__}")


def _zone(tz: str) -> tzinfo:
    """Resolve an IANA zone name, or fail fast with :data:`CONFIG_INVALID`."""
    candidate: object = tz
    if not isinstance(candidate, str) or not candidate.strip():
        raise _config_invalid("timezone must be a non-empty IANA name")
    key = candidate.strip()
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        fallback = _FIXED_OFFSET_ZONES.get(key)
        if fallback is not None:
            return timezone(fallback)
        raise _config_invalid(
            f"timezone {key!r} is unknown or this host has no tz database"
        ) from exc


def utc_now() -> datetime:
    """Current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def parse_duration(text: str) -> timedelta:
    """Parse a compact duration such as ``"30s"``, ``"15m"``, ``"12h"``, ``"7d"`` or ``"4w"``.

    Parts may be combined (``"1d12h"``) and are summed; case and surrounding whitespace are
    ignored. Weeks are 7 days. Raises :class:`SeoError` with ``INVALID_INPUT`` on anything else.
    """
    candidate: object = text
    if not isinstance(candidate, str):
        raise _invalid(f"duration must be a string like '7d', got {type(candidate).__name__}")
    raw = "".join(candidate.split()).lower()
    if not raw:
        raise _invalid("duration is empty", "use a value like '7d', '12h' or '30s'")
    total = 0
    position = 0
    for match in _DURATION_PART.finditer(raw):
        if match.start() != position:
            raise _invalid(f"invalid duration {text!r}", "use a value like '7d', '12h' or '30s'")
        total += int(match["count"]) * _SECONDS_PER_UNIT[match["unit"]]
        position = match.end()
    if position != len(raw):
        raise _invalid(f"invalid duration {text!r}", "use a value like '7d', '12h' or '30s'")
    try:
        return timedelta(seconds=total)
    except OverflowError as exc:
        raise _invalid(f"duration is too large: {text!r}") from exc


def format_duration(td: timedelta) -> str:
    """Format a non-negative whole-second duration compactly: ``"7d"``, ``"1d12h"``, ``"0s"``.

    Weeks are never emitted (7 days prints as ``"7d"``), so output round-trips through
    :func:`parse_duration`. Sub-second precision and negative spans are rejected instead of
    being silently truncated.
    """
    candidate: object = td
    if not isinstance(candidate, timedelta):
        raise _invalid(f"duration must be a timedelta, got {type(candidate).__name__}")
    if candidate.microseconds:
        raise _invalid("duration must be a whole number of seconds")
    total = int(candidate.total_seconds())
    if total < 0:
        raise _invalid("duration must not be negative")
    if total == 0:
        return "0s"
    parts: list[str] = []
    for suffix, size in _FORMAT_UNITS:
        count, total = divmod(total, size)
        if count:
            parts.append(f"{count}{suffix}")
    return "".join(parts)


def to_iso_date(value: date | datetime) -> str:
    """Render ``YYYY-MM-DD``.

    A datetime keeps the calendar date of **its own** timezone; nothing is converted. Use
    :func:`local_today` / :func:`period_window` when a local reporting day is what you need.
    """
    return _as_date(value, what="value").isoformat()


def days_between(start: date, end: date) -> int:
    """Days from ``start`` to ``end`` (``0`` when equal).

    An inclusive window therefore holds ``days_between(start, end) + 1`` days. A reversed range
    is an input error, not a negative count.
    """
    first = _as_date(start, what="start")
    last = _as_date(end, what="end")
    if last < first:
        raise _invalid(f"end {last.isoformat()} is before start {first.isoformat()}")
    return (last - first).days


def local_today(tz: str = "Europe/Minsk") -> date:
    """Today's calendar date in ``tz``."""
    return datetime.now(_zone(tz)).date()


def period_window(
    days: int,
    *,
    end: date | None = None,
    lag_days: int = 0,
    tz: str = "Europe/Minsk",
) -> tuple[date, date]:
    """Inclusive ``(start, end)`` of ``days`` whole local days.

    ``end`` defaults to today in ``tz``; both the explicit and the default end are shifted back
    by ``lag_days`` (GSC reports 2-3 days behind). ``tz`` is validated even when ``end`` is
    explicit so a bad config never hides until the first default window.
    """
    span = _require_int(days, name="days", minimum=1)
    lag = _require_int(lag_days, name="lag_days", minimum=0)
    zone = _zone(tz)
    last = _as_date(end, what="end") if end is not None else datetime.now(zone).date()
    last -= timedelta(days=lag)
    return last - timedelta(days=span - 1), last


def previous_window(start: date, end: date) -> tuple[date, date]:
    """The equally long window that ends the day before ``start``.

    ``previous_window`` of ``2026-11-04..2026-11-10`` is ``2026-10-28..2026-11-03``: same
    length, no overlap, no gap.
    """
    first = _as_date(start, what="start")
    last = _as_date(end, what="end")
    length = days_between(first, last) + 1
    previous_end = first - timedelta(days=1)
    return previous_end - timedelta(days=length - 1), previous_end


def compare_period(
    days: int,
    *,
    end: date | None = None,
    lag_days: int = 0,
) -> tuple[tuple[date, date], tuple[date, date]]:
    """Current and previous windows for a like-for-like report comparison.

    Uses the default zone; pass an explicit ``end`` when the caller has already resolved a
    local day in another timezone.
    """
    current = period_window(days, end=end, lag_days=lag_days)
    return current, previous_window(*current)
