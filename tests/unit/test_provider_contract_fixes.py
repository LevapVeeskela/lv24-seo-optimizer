"""Regression tests for the F04-F06/F15-F17 provider contract fixes.

Every test uses ``httpx.MockTransport`` with synthetic fixtures shaped like the
documented upstream APIs (including Cyrillic text and multi-page batches).
No live network is touched.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import httpx

from lv24_seo.core.http import HttpClient
from lv24_seo.providers.bing import BingClient
from lv24_seo.providers.crux import CruxClient
from lv24_seo.providers.gsc import GscClient
from lv24_seo.providers.yandex_webmaster import YandexWebmasterClient

Handler = Callable[[httpx.Request], httpx.Response]

USER_ID = 7
HOST = "https:example.com:443"
HOST_PATH = f"/v4/user/{USER_ID}/hosts/{HOST}"
USER_PATH = "/v4/user/"
PROPERTY = "https://example.com/"


def _http(handler: Handler) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)


def _yandex(handler: Handler) -> YandexWebmasterClient:
    return YandexWebmasterClient(token="test-token", http=_http(handler))


# --- GSC: dimensionFilterGroups (F15) -------------------------------------------------


def test_gsc_search_analytics_sends_dimension_filter_groups() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": []})

    client = GscClient(access_token="t", http=_http(handler), property_id=PROPERTY)
    client.search_analytics(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 7),
        dimensions=("query",),
        filters=[{"dimension": "country", "operator": "equals", "expression": "BLR"}],
    )

    body = json.loads(seen[-1].content)
    assert "filters" not in body
    assert body["dimensionFilterGroups"] == [
        {
            "groupType": "and",
            "filters": [{"dimension": "country", "operator": "equals", "expression": "BLR"}],
        }
    ]


# --- Bing: submit auth (F16) ----------------------------------------------------------


def test_bing_submit_urls_includes_apikey() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"d": None})

    client = BingClient(api_key="secret-bing-key", site_url=PROPERTY, http=_http(handler))
    client.submit_urls(["https://example.com/a"])

    assert seen[-1].url.params["apikey"] == "secret-bing-key"


# --- CrUX: shift name, histogram density, string p75 (F06) ----------------------------


def _crux_record() -> dict:
    return {
        "record": {
            "key": {"origin": "https://example.com"},
            "metrics": {
                "largest_contentful_paint": {
                    "histogram": [
                        {"start": 0, "end": 2500, "density": 0.78},
                        {"start": 2500, "end": 4000, "density": 0.15},
                        {"start": 4000, "density": 0.07},
                    ],
                    "percentiles": {"p75": 2410},
                },
                "interaction_to_next_paint": {
                    "histogram": [
                        {"start": 0, "end": 200, "density": 0.9},
                        {"start": 200, "end": 500, "density": 0.07},
                        {"start": 500, "density": 0.03},
                    ],
                    "percentiles": {"p75": 120},
                },
                "cumulative_layout_shift": {
                    "histogram": [
                        {"start": 0, "end": 0.1, "density": 0.82},
                        {"start": 0.1, "end": 0.25, "density": 0.12},
                        {"start": 0.25, "density": 0.06},
                    ],
                    # CLS percentiles are doubles encoded as strings.
                    "percentiles": {"p75": "0.06"},
                },
            },
        }
    }


def test_crux_reads_shift_histogram_density_and_string_p75() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_crux_record())

    result = CruxClient(api_key="k", http=_http(handler)).origin("https://example.com")
    by_metric = {row["metric"]: row for row in result.rows}

    assert by_metric["cumulative_layout_shift"]["unit"] == "score"
    assert by_metric["cumulative_layout_shift"]["p75"] == 0.06
    assert by_metric["cumulative_layout_shift"]["good"] == 0.82
    assert by_metric["largest_contentful_paint"]["good"] == 0.78
    assert by_metric["interaction_to_next_paint"]["poor"] == 0.03


def test_crux_incomplete_record_skips_metric_without_zero_fill() -> None:
    record = {
        "record": {
            "key": {"origin": "https://example.com"},
            "metrics": {
                "largest_contentful_paint": {
                    "histogram": [
                        {"start": 0, "end": 2500, "density": 0.5},
                    ],
                    "percentiles": {},
                },
            },
        }
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=record)

    result = CruxClient(api_key="k", http=_http(handler)).origin("https://example.com")
    # No p75 -> the metric is unknown, not a row of zeroes.
    assert result.rows == []


# --- Yandex: query_text, list indicators, count, pagination (F05) ---------------------


def _query_page(names: list[str], start: int) -> dict:
    return {
        "queries": [
            {
                "query_text": name,
                "indicators": [
                    {"name": "TOTAL_SHOWS", "value": 100 + start + i},
                    {"name": "TOTAL_CLICKS", "value": 7},
                    {"name": "AVG_SHOW_POSITION", "value": 4.5},
                ],
            }
            for i, name in enumerate(names)
        ],
        "count": 3,
        "total_count": 3,
    }


def test_yandex_popular_queries_maps_query_text_and_list_indicators() -> None:
    payload = _query_page(["seo", "seo минск", "продвижение сайта"], 0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(200, json=payload)

    result = _yandex(handler).popular_queries(HOST, limit=500)

    assert [row["query"] for row in result.rows] == ["seo", "seo минск", "продвижение сайта"]
    assert result.rows[0]["shows"] == 100
    assert result.rows[0]["clicks"] == 7
    assert result.rows[0]["position"] == 4.5
    assert result.total == 3
    assert result.truncated is False


def test_yandex_popular_queries_paginates_with_offset() -> None:
    seen: list[httpx.Request] = []
    pages = [
        {"queries": [{"query_text": f"q{i}", "indicators": []} for i in range(2)], "count": 3},
        {"queries": [{"query_text": "q2", "indicators": []}], "count": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        seen.append(request)
        offset = int(request.url.params.get("offset", "0"))
        return httpx.Response(200, json=pages[0] if offset == 0 else pages[1])

    result = _yandex(handler).popular_queries(HOST, limit=2)

    assert [row["query"] for row in result.rows] == ["q0", "q1", "q2"]
    assert [r.url.params.get("offset") for r in seen] == ["0", "2"]
    assert result.total == 3
    assert result.truncated is False


def test_yandex_diagnostics_expands_problems_object() -> None:
    payload = {
        "problems": {
            "NO_SITEMAPS": {
                "severity": "FATAL",
                "state": "PRESENT",
                "last_state_update": "2026-11-01T10:00:00",
            },
            "ROBOTS_TXT_MISSING": {
                "severity": "CRITICAL",
                "state": "ABSENT",
                "last_state_update": "2026-11-02T10:00:00",
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(200, json=payload)

    result = _yandex(handler).diagnostics(HOST)

    by_code = {row["code"]: row for row in result.rows}
    assert by_code["NO_SITEMAPS"]["severity"] == "FATAL"
    assert by_code["NO_SITEMAPS"]["state"] == "PRESENT"
    assert by_code["ROBOTS_TXT_MISSING"]["state"] == "ABSENT"
