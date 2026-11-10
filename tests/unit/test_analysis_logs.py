"""Offline tests for nginx bot-log analysis (WP4.5): parsing, rollups and waste shares.

Everything here is deterministic: fixtures are literal JSON lines and hand-written entries, with
no network, clock or filesystem access.
"""

from __future__ import annotations

import json

import pytest

from lv24_seo.analysis.logs import (
    LogEntry,
    bot_daily,
    classify_bot,
    crawl_waste,
    parse_nginx_json,
)

GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
YANDEXBOT = "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)"
BINGBOT = "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
AHREFSBOT = "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)"
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

TS = "2026-11-10T09:00:00+00:00"


def _line(**fields: object) -> str:
    return json.dumps(fields)


def _entry(**overrides: object) -> LogEntry:
    payload: dict[str, object] = {
        "ts": TS,
        "method": "GET",
        "path": "/",
        "status": 200,
        "bytes_sent": 1000,
        "ua": GOOGLEBOT,
        "ip": "66.249.66.1",
        "request_time": 0.1,
    }
    payload.update(overrides)
    return LogEntry(**payload)  # type: ignore[arg-type]


# --- parsing -----------------------------------------------------------------------------------


def test_parse_valid_line_maps_every_nginx_field() -> None:
    lines = [
        _line(
            time_iso8601="2026-11-10T09:15:00+03:00",
            request="GET /catalog?utm_source=news HTTP/1.1",
            status=200,
            body_bytes_sent=5120,
            http_user_agent=GOOGLEBOT,
            remote_addr="66.249.66.1",
            request_time=0.123,
        )
    ]

    assert parse_nginx_json(lines) == [
        LogEntry(
            ts="2026-11-10T09:15:00+03:00",
            method="GET",
            path="/catalog?utm_source=news",
            status=200,
            bytes_sent=5120,
            ua=GOOGLEBOT,
            ip="66.249.66.1",
            request_time=0.123,
        )
    ]


def test_parse_accepts_alternate_time_keys_and_coerces_numeric_strings() -> None:
    entries = parse_nginx_json(
        [
            _line(time="10/Nov/2026:12:34:56 +0300", request="GET / HTTP/1.1"),
            _line(ts=1700000000, request="GET /news HTTP/1.1"),
            _line(time="2026-11-10 12:00:00", request="GET /about HTTP/1.1"),
            _line(
                time_iso8601="2026-11-10",
                request="GET /plain HTTP/1.1",
                status="301",
                bytes_sent="2048",
                request_time="0.25",
            ),
        ]
    )

    assert [entry.ts for entry in entries] == [
        "2026-11-10T12:34:56+03:00",
        "2023-11-14T22:13:20+00:00",
        "2026-11-10T12:00:00",
        "2026-11-10",
    ]
    assert (entries[3].status, entries[3].bytes_sent, entries[3].request_time) == (301, 2048, 0.25)


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["", "   ", "\t"],
        ["not json", "{broken", "[1, 2, 3]", "null", '"text"', "42"],
    ],
)
def test_parse_skips_blank_and_invalid_lines(lines: list[str]) -> None:
    assert parse_nginx_json(lines) == []


def test_parse_never_raises_on_missing_or_mistyped_fields() -> None:
    entries = parse_nginx_json(
        [
            _line(),
            _line(
                time_iso8601=None,
                request=42,
                status="not-a-status",
                body_bytes_sent=None,
                http_user_agent=None,
                remote_addr=["127.0.0.1"],
                request_time="n/a",
            ),
            _line(request="GET /ok HTTP/1.1", status=None, body_bytes_sent=False),
        ]
    )

    assert entries[0] == LogEntry(
        ts="", method="", path="", status=0, bytes_sent=0, ua="", ip="", request_time=0.0
    )
    assert entries[1] == LogEntry(
        ts="", method="", path="", status=0, bytes_sent=0, ua="", ip="", request_time=0.0
    )
    assert entries[2] == LogEntry(
        ts="", method="GET", path="/ok", status=0, bytes_sent=0, ua="", ip="", request_time=0.0
    )


@pytest.mark.parametrize(
    ("request_line", "method", "path"),
    [
        ("GET /a HTTP/1.1", "GET", "/a"),
        ("POST /api/search?q=x HTTP/2.0", "POST", "/api/search?q=x"),
        ("/bare-path", "", "/bare-path"),
        ("-", "", ""),
        ("", "", ""),
        (None, "", ""),
    ],
)
def test_parse_request_line_tolerance(request_line: object, method: str, path: str) -> None:
    [entry] = parse_nginx_json([_line(request=request_line)])
    assert (entry.method, entry.path) == (method, path)


def test_parse_accepts_split_request_keys() -> None:
    [entry] = parse_nginx_json([_line(request_method="GET", request_uri="/assets/app.js?v=1")])
    assert (entry.method, entry.path) == ("GET", "/assets/app.js?v=1")


# --- bot classification ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ua", "expected"),
    [
        (GOOGLEBOT, "googlebot"),
        ("Googlebot-Image/1.0", "googlebot"),
        (YANDEXBOT, "yandexbot"),
        # Real Yandex UAs outside the crawler often only carry the yandex.com/bots marker.
        ("Mozilla/5.0 (compatible; YandexImages/3.0; +http://yandex.com/bots)", "yandexbot"),
        (BINGBOT, "bingbot"),
        (AHREFSBOT, "other"),
        (BROWSER, "other"),
        ("", "other"),
    ],
)
def test_classify_bot(ua: str, expected: str) -> None:
    assert classify_bot(ua) == expected


def test_classify_bot_is_case_insensitive() -> None:
    assert classify_bot("GOOGLEBOT/2.1") == "googlebot"
    assert classify_bot("YandexBot/3.0") == "yandexbot"


# --- daily rollup ------------------------------------------------------------------------------


def test_bot_daily_groups_by_date_bot_and_status_class() -> None:
    entries = [
        _entry(ts="2026-11-10T10:00:00+00:00", status=200, bytes_sent=100),
        _entry(ts="2026-11-10T11:00:00+00:00", status=200, bytes_sent=200),
        _entry(ts="2026-11-10T12:00:00+00:00", status=404, bytes_sent=50),
        _entry(ts="2026-11-11T09:00:00+00:00", status=200, ua=YANDEXBOT, bytes_sent=10),
        _entry(ts="2026-11-10T09:00:00+00:00", status=301, ua=AHREFSBOT, bytes_sent=5),
    ]

    assert bot_daily(entries) == [
        {
            "date": "2026-11-10",
            "bot": "googlebot",
            "status_class": "2xx",
            "hits": 2,
            "bytes": 300,
            "p95_ms": 100.0,
        },
        {
            "date": "2026-11-10",
            "bot": "googlebot",
            "status_class": "4xx",
            "hits": 1,
            "bytes": 50,
            "p95_ms": 100.0,
        },
        {
            "date": "2026-11-10",
            "bot": "other",
            "status_class": "3xx",
            "hits": 1,
            "bytes": 5,
            "p95_ms": 100.0,
        },
        {
            "date": "2026-11-11",
            "bot": "yandexbot",
            "status_class": "2xx",
            "hits": 1,
            "bytes": 10,
            "p95_ms": 100.0,
        },
    ]


def test_bot_daily_p95_uses_nearest_rank() -> None:
    # request_time 0.001..0.020 s -> 1..20 ms; nearest-rank p95 of 20 samples is the 19th value.
    entries = [_entry(request_time=millis / 1000.0) for millis in range(1, 21)]

    [row] = bot_daily(entries)
    assert row["p95_ms"] == 19.0


def test_bot_daily_keeps_entries_without_timestamp_in_unknown_bucket() -> None:
    [row] = bot_daily([_entry(ts="")])
    assert row["date"] == "unknown"
    assert row["hits"] == 1


# --- crawl waste -------------------------------------------------------------------------------


def _mixed_entries() -> list[LogEntry]:
    return [
        _entry(path="/", status=200),
        _entry(path="/about", status=200),
        _entry(path="/old-page", status=301),
        _entry(path="/missing", status=404),
        _entry(path="/oops", status=500),
        _entry(path="/assets/app.js", status=200),
        _entry(path="/static/logo.png", status=200),
        _entry(path="/api/search", status=200),
        _entry(path="/catalog?utm_source=news", status=200),
        _entry(path="/catalog?gclid=abc", status=200),
    ]


def test_crawl_waste_ratios_for_selected_bot() -> None:
    entries = [*_mixed_entries(), _entry(path="/yandex-only", status=200, ua=YANDEXBOT)]

    assert crawl_waste(entries, bot="googlebot") == {
        "hits": 10,
        "http_ratio": 0.7,
        "redirect_ratio": 0.1,
        "error_ratio": 0.2,
        "asset_ratio": 0.2,
        "api_ratio": 0.1,
        "param_ratio": 0.2,
        "waste_ratio": 0.8,
    }


def test_crawl_waste_without_bot_filter_uses_every_request() -> None:
    entries = [*_mixed_entries(), _entry(path="/yandex-only", status=200, ua=YANDEXBOT)]

    result = crawl_waste(entries)
    assert result["hits"] == 11
    assert result["http_ratio"] == 0.727
    assert result["waste_ratio"] == 0.727


def test_crawl_waste_zero_hits_returns_zero_ratios() -> None:
    expected = {
        "hits": 0,
        "http_ratio": 0.0,
        "redirect_ratio": 0.0,
        "error_ratio": 0.0,
        "asset_ratio": 0.0,
        "api_ratio": 0.0,
        "param_ratio": 0.0,
        "waste_ratio": 0.0,
    }

    assert crawl_waste([]) == expected
    assert crawl_waste([_entry(ua=YANDEXBOT)], bot="bingbot") == expected


def test_crawl_waste_counts_each_wasted_request_once() -> None:
    entries = [
        _entry(path="/assets/app.js", status=301),  # redirect + asset
        _entry(path="/catalog?utm_source=x", status=404),  # error + param
    ]

    result = crawl_waste(entries)
    assert result["redirect_ratio"] == 0.5
    assert result["asset_ratio"] == 0.5
    assert result["error_ratio"] == 0.5
    assert result["param_ratio"] == 0.5
    assert result["waste_ratio"] == 1.0


@pytest.mark.parametrize(
    "path",
    [
        "/catalog?utm_source=news",
        "/catalog?utm_campaign=spring",
        "/catalog?fbclid=abc",
        "/catalog?a=1&gclid=zzz",
    ],
)
def test_crawl_waste_detects_tracking_params(path: str) -> None:
    result = crawl_waste([_entry(path=path)])
    assert result["param_ratio"] == 1.0
    assert result["waste_ratio"] == 1.0


@pytest.mark.parametrize("path", ["/catalog", "/catalog?page=2", "/news/utm_source"])
def test_crawl_waste_ignores_plain_queries_and_path_text(path: str) -> None:
    result = crawl_waste([_entry(path=path)])
    assert result["param_ratio"] == 0.0
    assert result["waste_ratio"] == 0.0


@pytest.mark.parametrize(
    "path",
    [
        "/assets/app.js",
        "/static/logo.png",
        "/img/photo.JPG",
        "/styles/app.css?v=1",
        "/fonts/inter.woff2",
    ],
)
def test_crawl_waste_counts_assets(path: str) -> None:
    result = crawl_waste([_entry(path=path, status=200)])
    assert result["asset_ratio"] == 1.0
    assert result["waste_ratio"] == 1.0


@pytest.mark.parametrize("path", ["/", "/catalog/item", "/api/places", "/assets"])
def test_crawl_waste_does_not_treat_pages_or_api_as_assets(path: str) -> None:
    result = crawl_waste([_entry(path=path, status=200)])
    assert result["asset_ratio"] == 0.0
