"""Bot crawl analysis over nginx JSON access logs (WP4.5).

Input is what nginx writes with ``log_format ... escape=json``: one JSON object per line with
keys such as ``time_iso8601``/``time``/``ts``, ``request`` (``"GET /path HTTP/1.1"``), ``status``,
``body_bytes_sent``, ``http_user_agent``, ``remote_addr`` and ``request_time`` (seconds).

Why the shapes below
--------------------
* Parsing is **tolerant**. A truncated tail, a rotated file or a field of the wrong type must not
  abort a multi-day import: unparsable lines are dropped and missing fields fall back to neutral
  values (``status = 0``, empty UA, zero bytes/latency).
* ``crawl_waste`` counts a request **once** even when it is, say, a redirecting asset with ``utm_``
  parameters. The ratios therefore stay inside ``0..1`` and answer "what share of the crawl was
  wasted", not "how many category matches were there".
* ``http_ratio`` is the share of **2xx** responses (the useful, non-redirecting, non-error part of
  the crawl); ``error_ratio`` covers 4xx and 5xx together.
* ``p95_ms`` uses the nearest-rank definition: at least 95 % of samples are at or below it. Tiny
  groups (one or two requests) then report a real observed value instead of a smoothed midpoint.
* Dates are the first 10 characters of the normalised timestamp. nginx emits ISO timestamps
  (``time_iso8601``), CLF-style local time (``$time_local``) or epoch seconds (``$msec``); all
  three are normalised to ``YYYY-MM-DDThh:mm:ss+hh:mm`` so one rollup never mixes formats. An
  entry without a usable timestamp lands in the ``"unknown"`` bucket instead of being dropped.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "LogEntry",
    "bot_daily",
    "classify_bot",
    "crawl_waste",
    "parse_nginx_json",
]

_UNKNOWN_DATE = "unknown"

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# nginx $time_local: 10/Nov/2026:12:34:56 +0300
_NGINX_LOCAL_TIME = re.compile(
    r"(?P<day>\d{1,2})/(?P<month>[A-Za-z]{3})/(?P<year>\d{4}):"
    r"(?P<clock>\d{2}:\d{2}:\d{2})(?:\s*(?P<offset>[+-]\d{4}))?"
)
_EPOCH_SECONDS = re.compile(r"-?\d+(?:\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_ASSET_PREFIXES = ("/assets/", "/static/")
# Kept deliberately narrow: sitemaps, robots.txt and JSON feeds are crawl targets, not assets.
_ASSET_EXTENSIONS = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
)
_PARAM_MARKERS = ("utm_", "fbclid", "gclid")


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One access-log request, normalised for analysis."""

    ts: str
    method: str
    path: str
    status: int
    bytes_sent: int
    ua: str
    ip: str
    request_time: float


def parse_nginx_json(lines: Iterable[str]) -> list[LogEntry]:
    """Parse nginx ``escape=json`` lines, skipping blank or malformed ones.

    Never raises on bad input: a line that is not a JSON object, or a field of the wrong type,
    degrades to defaults instead of breaking the import.
    """
    entries: list[LogEntry] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload: object = json.loads(text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        entries.append(_entry_from_payload(payload))
    return entries


def classify_bot(ua: str) -> str:
    """Map a user agent to ``googlebot`` / ``yandexbot`` / ``bingbot`` / ``other``.

    This is UA-only triage for fast log rollups. Spoofable UAs mean it is **not** proof of a
    verified crawler; the report must not treat non-search crawlers (AhrefsBot, SemrushBot) as
    search bots, hence ``other``.
    """
    haystack = ua.lower()
    if "googlebot" in haystack:
        return "googlebot"
    if "yandexbot" in haystack or "yandex.com/bots" in haystack:
        return "yandexbot"
    if "bingbot" in haystack:
        return "bingbot"
    return "other"


def bot_daily(entries: Sequence[LogEntry]) -> list[dict[str, Any]]:
    """Roll entries up into one row per ``(date, bot, status_class)``.

    Rows are sorted by date, bot and status class so repeated runs on the same log produce the
    same payload. ``p95_ms`` is the nearest-rank 95th percentile of ``request_time`` in
    milliseconds, rounded to three decimals.
    """
    groups: defaultdict[tuple[str, str, str], list[LogEntry]] = defaultdict(list)
    for entry in entries:
        key = (_entry_date(entry), classify_bot(entry.ua), _status_class(entry.status))
        groups[key].append(entry)

    rows: list[dict[str, Any]] = []
    for (date, bot, status_class), group in sorted(groups.items()):
        rows.append(
            {
                "date": date,
                "bot": bot,
                "status_class": status_class,
                "hits": len(group),
                "bytes": sum(entry.bytes_sent for entry in group),
                "p95_ms": _p95_ms([entry.request_time for entry in group]),
            }
        )
    return rows


def crawl_waste(entries: Sequence[LogEntry], *, bot: str | None = None) -> dict[str, float | int]:
    """Share of crawl traffic that does not help indexing.

    ``bot=None`` selects every request in the log (including humans and non-search crawlers);
    otherwise only requests whose UA classifies as ``bot`` are considered.

    Redirects, errors (4xx+5xx), asset requests, ``/api/`` calls and tracking-parameter requests
    are waste. A request matching several categories still counts once, so ``waste_ratio`` cannot
    exceed 1. Zero selected hits returns all-zero ratios rather than dividing by zero.
    """
    wanted = bot.strip().lower() if bot is not None else None
    selected = [entry for entry in entries if wanted is None or classify_bot(entry.ua) == wanted]
    hits = len(selected)
    if hits == 0:
        return {
            "hits": 0,
            "http_ratio": 0.0,
            "redirect_ratio": 0.0,
            "error_ratio": 0.0,
            "asset_ratio": 0.0,
            "api_ratio": 0.0,
            "param_ratio": 0.0,
            "waste_ratio": 0.0,
        }

    successes = redirects = errors = assets = api_calls = params = waste = 0
    for entry in selected:
        is_success = 200 <= entry.status <= 299
        is_redirect = 300 <= entry.status <= 399
        is_error = entry.status >= 400
        is_asset = _is_asset(entry.path)
        is_api = _base_path(entry.path).startswith("/api/")
        has_param = _has_tracking_param(entry.path)

        successes += int(is_success)
        redirects += int(is_redirect)
        errors += int(is_error)
        assets += int(is_asset)
        api_calls += int(is_api)
        params += int(has_param)
        waste += int(is_redirect or is_error or is_asset or is_api or has_param)

    return {
        "hits": hits,
        "http_ratio": _ratio(successes, hits),
        "redirect_ratio": _ratio(redirects, hits),
        "error_ratio": _ratio(errors, hits),
        "asset_ratio": _ratio(assets, hits),
        "api_ratio": _ratio(api_calls, hits),
        "param_ratio": _ratio(params, hits),
        "waste_ratio": _ratio(waste, hits),
    }


# --- parsing helpers ---------------------------------------------------------------------------


def _first(payload: dict[str, Any], *keys: str) -> object:
    """First key with a present, non-empty value (``0`` and ``False`` are valid values)."""
    for key in keys:
        value: object = payload.get(key)
        if value is None or value == "":
            continue
        return value
    return None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, OverflowError):
            return 0
    return 0


def _as_float(value: object) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return 0.0
    else:
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _split_request(value: object) -> tuple[str, str]:
    raw = _text(value).strip()
    if not raw:
        return "", ""
    parts = raw.split()
    if len(parts) == 1:
        # A bare path is tolerated; nginx logs "-" for a request it could not parse.
        return "", parts[0] if parts[0].startswith("/") else ""
    return parts[0], parts[1]


def _normalize_ts(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if _EPOCH_SECONDS.fullmatch(raw):
        return _from_epoch(float(raw))

    local = _NGINX_LOCAL_TIME.fullmatch(raw)
    if local is not None:
        month = _MONTHS.get(local["month"].lower())
        if month is not None:
            day = int(local["day"])
            offset = _normalize_offset(local["offset"])
            return f"{local['year']}-{month:02d}-{day:02d}T{local['clock']}{offset}"

    if _ISO_DATE.fullmatch(raw[:10]) and raw[10:11] == " ":
        return f"{raw[:10]}T{raw[11:]}"
    return raw


def _from_epoch(seconds: float) -> str:
    try:
        return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _normalize_offset(offset: str | None) -> str:
    if offset is None or len(offset) != 5:
        return ""
    return f"{offset[:3]}:{offset[3:]}"


def _entry_from_payload(payload: dict[str, Any]) -> LogEntry:
    method, path = _split_request(_first(payload, "request"))
    if not path:
        # Some log formats split the request line into request_method/request_uri.
        method = _text(_first(payload, "request_method")) or method
        path = _text(_first(payload, "request_uri", "uri"))

    return LogEntry(
        ts=_normalize_ts(_first(payload, "time_iso8601", "ts", "time")),
        method=method,
        path=path,
        status=_as_int(_first(payload, "status")),
        bytes_sent=_as_int(_first(payload, "body_bytes_sent", "bytes_sent")),
        ua=_text(_first(payload, "http_user_agent")),
        ip=_text(_first(payload, "remote_addr", "realip_remote_addr")),
        request_time=_as_float(_first(payload, "request_time")),
    )


# --- analysis helpers --------------------------------------------------------------------------


def _status_class(status: int) -> str:
    if 200 <= status <= 299:
        return "2xx"
    if 300 <= status <= 399:
        return "3xx"
    if 400 <= status <= 499:
        return "4xx"
    if 500 <= status <= 599:
        return "5xx"
    return "other"


def _entry_date(entry: LogEntry) -> str:
    head = entry.ts[:10]
    return head if _ISO_DATE.fullmatch(head) else _UNKNOWN_DATE


def _p95_ms(samples: Sequence[float]) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(math.ceil(0.95 * len(ordered)) - 1, 0)
    return round(ordered[rank] * 1000.0, 3)


def _base_path(path: str) -> str:
    """Lower-cased path without query string or fragment."""
    return path.split("?", 1)[0].split("#", 1)[0].lower()


def _is_asset(path: str) -> bool:
    base = _base_path(path)
    return base.startswith(_ASSET_PREFIXES) or base.endswith(_ASSET_EXTENSIONS)


def _has_tracking_param(path: str) -> bool:
    _, separator, query = path.partition("?")
    if not separator:
        return False
    lowered = query.lower()
    return any(marker in lowered for marker in _PARAM_MARKERS)


def _ratio(count: int, hits: int) -> float:
    return round(count / hits, 3) if hits else 0.0
