"""Offline tests for the Yandex Webmaster adapter (``httpx.MockTransport``, no network)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx
import pytest

from lv24_seo.core.errors import (
    AUTH_INVALID,
    AUTH_REQUIRED,
    QUOTA_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import (
    CLIENT_SIDE_DATE_FILTER,
    DEGRADED_HOST,
    EMPTY_HISTORY,
    NO_PER_URL_SITEMAP_ERRORS,
    NO_QUERY_PAGE_JOIN,
    ONE_URL_PER_CALL,
    TOP_3000_QUERIES,
    data_age_days,
)
from lv24_seo.providers.yandex_webmaster import YandexWebmasterClient

Handler = Callable[[httpx.Request], httpx.Response]

USER_ID = 7
HOST = "https:example.com:443"
HOST_PATH = f"/v4/user/{USER_ID}/hosts/{HOST}"
USER_PATH = "/v4/user/"


def _client(handler: Handler, *, token: str = "test-token") -> YandexWebmasterClient:
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    return YandexWebmasterClient(token=token, http=http)


def _with_user(payload: object, *, status: int = 200) -> Handler:
    """Serve the account endpoint, then ``payload`` for every host-scoped path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(status, json=payload)

    return handler


def test_empty_token_requires_login() -> None:
    with pytest.raises(SeoError) as excinfo:
        YandexWebmasterClient(token="   ")

    assert excinfo.value.code == AUTH_REQUIRED
    assert excinfo.value.next_action == "lv24-seo auth login --provider yandex"


def test_auth_header_and_accept_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"user_id": USER_ID})

    assert _client(handler).user().rows == [{"user_id": USER_ID}]
    assert seen[0].headers["authorization"] == "OAuth test-token"
    assert seen[0].headers["accept"] == "application/json"


def test_user_id_is_cached_and_reused_by_hosts() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(
            200,
            json={
                "hosts": [
                    {
                        "host_id": HOST,
                        "ascii_host_url": "https://example.com/",
                        "unicode_host_url": "https://пример.рф/",
                        "verified": True,
                        "main_mirror": True,
                    }
                ]
            },
        )

    client = _client(handler)
    assert client.user().rows == [{"user_id": USER_ID}]
    hosts = client.hosts()

    assert hosts.rows == [
        {
            "host_id": HOST,
            "ascii_host_url": "https://example.com/",
            "unicode_host_url": "https://пример.рф/",
            "verified": True,
        }
    ]
    assert hosts.property_id == f"user:{USER_ID}"
    assert sum(1 for request in seen if request.url.path == USER_PATH) == 1


def test_failed_user_lookup_is_not_cached() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"user_id": 9})

    client = _client(handler)
    with pytest.raises(SeoError):
        client.user()
    assert client._user_id is None

    assert client.user().rows == [{"user_id": 9}]
    assert calls == 2


def test_401_maps_to_auth_invalid_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error_code": "INVALID_TOKEN"})

    with pytest.raises(SeoError) as excinfo:
        _client(handler).user()

    error = excinfo.value
    assert error.code == AUTH_INVALID
    assert error.retryable is False
    assert "test-token" not in f"{error.message} {error.detail!r}"
    assert calls == 1


def test_403_maps_to_auth_invalid() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"error_code": "HOST_NOT_VERIFIED"})

    with pytest.raises(SeoError) as excinfo:
        _client(handler).user()

    error = excinfo.value
    assert error.code == AUTH_INVALID
    assert "access denied" in error.message
    assert error.retryable is False
    assert calls == 1


def test_summary_maps_known_counters() -> None:
    seen: list[httpx.Request] = []
    payload = {
        "sqi": 42,
        "searchable_pages_count": 1200,
        "excluded_pages_count": 30,
        "http_2xx": 1100,
        "http_3xx": 50,
        "http_4xx": 40,
        "http_5xx": 10,
        "indexing_status": "OK",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(200, json=payload)

    result = _client(handler).summary(HOST)

    assert seen[-1].url.path == f"{HOST_PATH}/summary/"
    assert result.rows == [
        {
            "sqi": 42,
            "pages_in_search": 1200,
            "pages_excluded": 30,
            "http_2xx": 1100,
            "http_3xx": 50,
            "http_4xx": 40,
            "http_5xx": 10,
        }
    ]


def test_summary_maps_documented_site_problems() -> None:
    payload = {
        "sqi": 42,
        "searchable_pages_count": 1200,
        "excluded_pages_count": 30,
        "site_problems": {
            "FATAL": 0,
            "CRITICAL": 2,
            "POSSIBLE_PROBLEM": 5,
            "RECOMMENDATION": 1,
        },
    }

    result = _client(_with_user(payload)).summary(HOST)

    assert result.rows == [
        {
            "sqi": 42,
            "pages_in_search": 1200,
            "pages_excluded": 30,
            "site_problems": {
                "CRITICAL": 2,
                "FATAL": 0,
                "POSSIBLE_PROBLEM": 5,
                "RECOMMENDATION": 1,
            },
        }
    ]


def test_summary_missing_counters_stay_absent() -> None:
    result = _client(_with_user({"sqi": 12})).summary(HOST)

    # A provider-omitted counter is absent, never zero-filled.
    assert result.rows == [{"sqi": 12}]
    assert "pages_in_search" not in result.rows[0]
    assert "pages_excluded" not in result.rows[0]
    assert "http_2xx" not in result.rows[0]
    assert "site_problems" not in result.rows[0]


def test_summary_accepts_uppercase_http_counter_keys() -> None:
    payload = {"sqi": 12, "HTTP_2XX": 10, "HTTP_5XX": 1}

    result = _client(_with_user(payload)).summary(HOST)

    assert result.rows == [{"sqi": 12, "http_2xx": 10, "http_5xx": 1}]


def test_indexing_history_passes_daily_buckets() -> None:
    payload = {"history": [{"date": "2026-11-01", "http_2xx": 100, "http_4xx": 2}]}

    result = _client(_with_user(payload)).indexing_history(HOST)

    assert result.rows == [{"date": "2026-11-01", "http_2xx": 100, "http_4xx": 2}]
    assert result.data_age_days == 0


def test_indexing_history_merges_documented_indicator_series() -> None:
    payload = {
        "indicators": {
            "HTTP_2XX": [
                {"date": "2026-11-01T00:00:00,000+0300", "value": 100},
                {"date": "2026-11-02T00:00:00,000+0300", "value": 110},
            ],
            "HTTP_4XX": [{"date": "2026-11-01T00:00:00,000+0300", "value": 2}],
            "OTHER": [{"date": "2026-11-02T00:00:00,000+0300", "value": 1}],
        }
    }

    result = _client(_with_user(payload)).indexing_history(HOST)

    # One row per date; omitted series stay absent instead of becoming zero.
    assert result.rows == [
        {"date": "2026-11-01T00:00:00,000+0300", "http_2xx": 100, "http_4xx": 2},
        {"date": "2026-11-02T00:00:00,000+0300", "http_2xx": 110, "other": 1},
    ]
    assert "http_3xx" not in result.rows[0]
    assert "http_5xx" not in result.rows[1]


def test_indexing_history_preserves_unknown_indicator_series() -> None:
    payload = {
        "indicators": {
            "HTTP_2XX": [{"date": "2026-11-01", "value": 10}],
            "HTTP_999": [{"date": "2026-11-01", "value": 3}],
        }
    }

    result = _client(_with_user(payload)).indexing_history(HOST)

    assert result.rows == [
        {
            "date": "2026-11-01",
            "http_2xx": 10,
            "extra_indicators": {"HTTP_999": 3},
        }
    ]


def test_empty_indicator_series_is_empty_history() -> None:
    result = _client(_with_user({"indicators": {}})).indexing_history(HOST)

    assert result.empty is True
    assert result.total == 0
    assert result.warnings == ()
    assert result.limitations == (EMPTY_HISTORY,)


def test_search_urls_events_passes_events() -> None:
    payload = {"events": [{"date": "2026-11-02", "event": "URL_ADDED", "count": 3}]}

    result = _client(_with_user(payload)).search_urls_events(HOST)

    assert result.rows == [{"date": "2026-11-02", "event": "URL_ADDED", "count": 3}]


def test_diagnostics_flattens_problems_and_flags_sitemap_limitation() -> None:
    payload = {
        "problems": [
            {
                "severity": "FATAL",
                "code": "SITEMAP_LOAD_ERROR",
                "state": "PRESENT",
                "last_state_update": "2026-11-01T10:00:00",
                "details": {"url": "https://example.com/sitemap.xml"},
            },
            {
                "severity": "CRITICAL",
                "code": "ROBOTS_TXT_MISSING",
                "state": "PRESENT",
                "last_state_update": "2026-11-02T10:00:00",
                "details": ["no robots.txt"],
            },
        ]
    }

    result = _client(_with_user(payload)).diagnostics(HOST)

    assert result.rows == [
        {
            "severity": "FATAL",
            "code": "SITEMAP_LOAD_ERROR",
            "state": "PRESENT",
            "last_state_update": "2026-11-01T10:00:00",
        },
        {
            "severity": "CRITICAL",
            "code": "ROBOTS_TXT_MISSING",
            "state": "PRESENT",
            "last_state_update": "2026-11-02T10:00:00",
        },
    ]
    assert result.limitations == (NO_PER_URL_SITEMAP_ERRORS,)


def test_diagnostics_without_sitemap_problem_has_no_limitation() -> None:
    payload = {
        "problems": [
            {
                "severity": "CRITICAL",
                "code": "ROBOTS_TXT_MISSING",
                "state": "PRESENT",
                "last_state_update": "2026-11-02T10:00:00",
            }
        ]
    }

    result = _client(_with_user(payload)).diagnostics(HOST)

    assert result.limitations == ()
    assert "limitations" not in result.to_meta()


def test_popular_queries_uses_repeated_params_and_reports_limits() -> None:
    seen: list[httpx.Request] = []
    payload = {
        "queries": [
            {
                "query": "seo",
                "indicators": {
                    "TOTAL_SHOWS": 100,
                    "TOTAL_CLICKS": 7,
                    "AVG_SHOW_POSITION": 4.5,
                    "AVG_CLICK_POSITION": 3.2,
                },
            },
            {
                "query": "seo минск",
                "shows": 20,
                "clicks": 1,
                "position": 12,
                "avg_click_position": 9.5,
            },
        ],
        "total": 600,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(200, json=payload)

    result = _client(handler).popular_queries(HOST)

    query_request = seen[-1]
    assert query_request.url.path == f"{HOST_PATH}/search-queries/popular/"
    assert query_request.url.params.get_list("query_indicator") == [
        "TOTAL_SHOWS",
        "TOTAL_CLICKS",
        "AVG_SHOW_POSITION",
        "AVG_CLICK_POSITION",
    ]
    assert query_request.url.params["order_by"] == "TOTAL_SHOWS"
    assert query_request.url.params["limit"] == "500"

    assert result.rows == [
        {
            "query": "seo",
            "shows": 100,
            "clicks": 7,
            "position": 4.5,
            "avg_click_position": 3.2,
        },
        {
            "query": "seo минск",
            "shows": 20,
            "clicks": 1,
            "position": 12,
            "avg_click_position": 9.5,
        },
    ]
    assert result.limitations == (TOP_3000_QUERIES, NO_QUERY_PAGE_JOIN)
    assert result.total == 600
    assert result.truncated is True
    assert result.data_age_days == 4


def test_popular_queries_honours_limit_and_payload_date() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(200, json={"queries": [], "date_to": "2026-11-01"})

    result = _client(handler).popular_queries(HOST, limit=50, order_by="TOTAL_CLICKS")

    assert seen[-1].url.params["limit"] == "50"
    assert seen[-1].url.params["order_by"] == "TOTAL_CLICKS"
    assert result.total is None
    assert result.truncated is False
    assert result.data_age_days == data_age_days(
        end=datetime(2026, 11, 1, tzinfo=UTC),
        fetched_at=result.fetched_at,
    )
    assert result.limitations == (TOP_3000_QUERIES, NO_QUERY_PAGE_JOIN)


def test_popular_queries_keeps_unknown_indicators() -> None:
    payload = {
        "queries": [
            {
                "query_id": "a08b",
                "query_text": "seo",
                "indicators": {
                    "TOTAL_SHOWS": 100,
                    "AVG_CLICK_POSITION": 2.5,
                    "SOME_FUTURE_INDICATOR": 5,
                },
            }
        ]
    }

    result = _client(_with_user(payload)).popular_queries(HOST)

    row = result.rows[0]
    assert row["query"] == "seo"
    assert row["query_id"] == "a08b"
    assert row["shows"] == 100
    assert row["avg_click_position"] == 2.5
    assert row["extra_indicators"] == {"SOME_FUTURE_INDICATOR": 5}
    assert row.get("clicks") is None
    assert row.get("position") is None


def test_popular_queries_list_indicators_keep_unknown_rows() -> None:
    payload = {
        "queries": [
            {
                "query_text": "seo",
                "indicators": [
                    {"name": "TOTAL_CLICKS", "value": 7},
                    {"name": "AVG_CLICK_POSITION", "value": 3.5},
                    {"name": "SOME_FUTURE_INDICATOR", "value": 1},
                ],
            }
        ]
    }

    result = _client(_with_user(payload)).popular_queries(HOST)

    row = result.rows[0]
    assert row["query"] == "seo"
    assert row["clicks"] == 7
    assert row["avg_click_position"] == 3.5
    assert row["extra_indicators"] == {"SOME_FUTURE_INDICATOR": 1}
    assert row.get("shows") is None


def test_popular_queries_paginates_until_reported_total() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        seen.append(request)
        offset = int(request.url.params["offset"])
        rows = [{"query_text": f"q{offset}", "indicators": {"TOTAL_SHOWS": 1}}]
        if offset == 0:
            rows.append({"query_text": "q1", "indicators": {"TOTAL_SHOWS": 2}})
        return httpx.Response(200, json={"queries": rows, "total": 3})

    result = _client(handler).popular_queries(HOST, limit=2)

    assert [request.url.params["offset"] for request in seen] == ["0", "2"]
    assert [row["query"] for row in result.rows] == ["q0", "q1", "q2"]
    assert result.total == 3
    assert result.truncated is False


def test_host_not_loaded_payload_returns_degraded_result() -> None:
    payload = {"error_code": "HOST_NOT_LOADED", "error_message": "Host is not loaded"}

    result = _client(_with_user(payload)).indexing_history(HOST)

    assert result.empty is True
    assert result.rows == []
    assert result.warnings == ("host_not_loaded",)
    assert result.limitations == (DEGRADED_HOST,)


def test_host_scoped_404_returns_degraded_result() -> None:
    result = _client(_with_user({"error_code": "HOST_NOT_FOUND"}, status=404)).summary(HOST)

    assert result.empty is True
    assert result.warnings == ("host_not_loaded",)
    assert result.limitations == (DEGRADED_HOST,)


def test_empty_history_is_empty_not_degraded() -> None:
    result = _client(_with_user({"history": []})).indexing_history(HOST)

    assert result.empty is True
    assert result.rows == []
    assert result.total == 0
    assert result.truncated is False
    assert result.warnings == ()
    assert result.limitations == (EMPTY_HISTORY,)


def test_empty_search_url_events_are_empty_not_degraded() -> None:
    result = _client(_with_user({"events": []})).search_urls_events(HOST)

    assert result.empty is True
    assert result.total == 0
    assert result.warnings == ()
    assert result.limitations == (EMPTY_HISTORY,)


def test_recrawl_quota_maps_quota_and_keeps_raw_payload() -> None:
    payload = {"daily_quota": 100, "quota_remainder": 42, "quota_used": 58}

    result = _client(_with_user(payload)).recrawl_quota(HOST)

    assert result.rows == [
        {
            "daily_quota": 100,
            "quota_remainder": 42,
            "raw": payload,
        }
    ]


def test_recrawl_quota_maps_alternative_key_names() -> None:
    payload = {"quota": 100, "remainder": 42, "quota_used": 58}

    result = _client(_with_user(payload)).recrawl_quota(HOST)

    assert result.rows == [
        {
            "daily_quota": 100,
            "quota_remainder": 42,
            "raw": payload,
        }
    ]


def test_search_urls_events_parses_indicator_series() -> None:
    payload = {
        "indicators": {
            "APPEARED_IN_SEARCH": [
                {"date": "2026-10-02", "value": 57},
                {"date": "2026-10-03", "value": 1},
            ],
            "REMOVED_FROM_SEARCH": [{"date": "2026-10-02", "value": 0}],
        }
    }

    result = _client(_with_user(payload)).search_urls_events(HOST)

    assert result.rows == [
        {"date": "2026-10-02", "appeared": 57, "removed": 0},
        {"date": "2026-10-03", "appeared": 1},
    ]
    assert "removed" not in result.rows[1]


def test_search_urls_events_keeps_unknown_indicator() -> None:
    payload = {
        "indicators": {
            "APPEARED_IN_SEARCH": [{"date": "2026-10-02", "value": 2}],
            "FUTURE_EVENT": [{"date": "2026-10-02", "value": 1}],
        }
    }

    result = _client(_with_user(payload)).search_urls_events(HOST)

    assert result.rows == [
        {
            "date": "2026-10-02",
            "appeared": 2,
            "extra_indicators": {"FUTURE_EVENT": 1},
        }
    ]


def test_indexing_and_events_history_forward_the_date_window() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        seen.append(request)
        return httpx.Response(200, json={"indicators": {}})

    client = _client(handler)
    client.indexing_history(HOST, date_from=date(2026, 10, 1), date_to=date(2026, 10, 2))
    client.search_urls_events(HOST, date_from=date(2026, 10, 1), date_to=date(2026, 10, 2))

    assert seen[0].url.params["date_from"] == "2026-10-01"
    assert seen[0].url.params["date_to"] == "2026-10-02"
    assert seen[1].url.params["date_from"] == "2026-10-01"
    assert seen[1].url.params["date_to"] == "2026-10-02"


def test_search_urls_events_falls_back_to_a_client_side_date_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        if request.url.params.get("date_from"):
            raise httpx.RemoteProtocolError("connection closed")
        return httpx.Response(
            200,
            json={
                "indicators": {
                    "APPEARED_IN_SEARCH": [
                        {"date": "2026-09-30", "value": 5},
                        {"date": "2026-10-02", "value": 57},
                        {"date": "2026-10-03", "value": 1},
                    ],
                    "REMOVED_FROM_SEARCH": [{"date": "2026-10-02", "value": 0}],
                }
            },
        )

    result = _client(handler).search_urls_events(
        HOST,
        date_from=date(2026, 10, 1),
        date_to=date(2026, 10, 2),
    )

    assert result.rows == [{"date": "2026-10-02", "appeared": 57, "removed": 0}]
    assert result.warnings == (CLIENT_SIDE_DATE_FILTER,)
    assert result.limitations == (CLIENT_SIDE_DATE_FILTER,)


def test_indexing_samples_paginates_and_reports_total() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        seen.append(request)
        offset = int(request.url.params["offset"])
        size = 100 if offset == 0 else 50
        samples = [
            {
                "status": "OK",
                "http_code": 200,
                "url": f"https://example.com/{offset + index}",
                "access_date": "2026-11-01",
            }
            for index in range(size)
        ]
        return httpx.Response(200, json={"count": 150, "samples": samples})

    result = _client(handler).indexing_samples(HOST, limit=150)

    assert [request.url.params["offset"] for request in seen] == ["0", "100"]
    assert len(result.rows) == 150
    assert result.total == 150
    assert result.truncated is False


def test_indexing_samples_limit_marks_truncated() -> None:
    payload = {
        "count": 10,
        "samples": [
            {
                "status": "OK",
                "http_code": 200,
                "url": "https://example.com/a",
                "access_date": "2026-11-01",
            },
            {
                "status": "OK",
                "http_code": 200,
                "url": "https://example.com/b",
                "access_date": "2026-11-01",
            },
        ],
    }

    result = _client(_with_user(payload)).indexing_samples(HOST, limit=2)

    assert len(result.rows) == 2
    assert result.total == 10
    assert result.truncated is True


def test_search_event_samples_returns_per_url_rows() -> None:
    payload = {
        "count": 1,
        "samples": [
            {
                "url": "https://example.com/a",
                "title": "Page A",
                "event_date": "2026-10-02",
                "last_access": "2026-10-02",
                "event": "APPEARED_IN_SEARCH",
                "excluded_url_status": None,
                "bad_http_status": None,
                "target_url": None,
            }
        ],
    }

    result = _client(_with_user(payload)).search_event_samples(HOST, limit=100)

    assert result.rows == [
        {
            "url": "https://example.com/a",
            "title": "Page A",
            "event_date": "2026-10-02",
            "last_access": "2026-10-02",
            "event": "APPEARED_IN_SEARCH",
            "excluded_url_status": None,
            "bad_http_status": None,
            "target_url": None,
        }
    ]
    assert result.total == 1


def test_sitemaps_lists_and_reads_one_sitemap() -> None:
    seen: list[httpx.Request] = []
    payload = {
        "sitemaps": [
            {
                "sitemap_id": "abc",
                "sitemap_url": "https://example.com/sitemap.xml",
                "last_access_date": "2026-11-16",
                "errors_count": 0,
                "urls_count": 6532,
                "children_count": 0,
                "sources": ["WEBMASTER"],
                "sitemap_type": "SITEMAP",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        seen.append(request)
        if request.url.path.endswith("/sitemaps/abc"):
            return httpx.Response(200, json=payload["sitemaps"][0])
        return httpx.Response(200, json=payload)

    client = _client(handler)
    listed = client.sitemaps(HOST, limit=10)
    detail = client.sitemap(HOST, "abc")

    assert listed.rows[0]["urls_count"] == 6532
    assert listed.rows[0]["sources"] == ["WEBMASTER"]
    assert listed.total == 1
    assert listed.truncated is False
    assert detail.rows == listed.rows
    assert seen[0].url.params["limit"] == "10"
    assert seen[1].url.path == f"{HOST_PATH}/sitemaps/abc"


def test_sqi_history_maps_points() -> None:
    payload = {
        "points": [
            {"date": "2026-11-01", "value": 42},
            {"date": "2026-11-02", "value": 43},
        ]
    }

    result = _client(_with_user(payload)).sqi_history(HOST)

    assert result.rows == [
        {"date": "2026-11-01", "value": 42},
        {"date": "2026-11-02", "value": 43},
    ]


def test_in_search_history_merges_indicator_series() -> None:
    payload = {
        "indicators": {
            "SEARCHABLE": [{"date": "2026-11-01", "value": 6500}],
            "EXCLUDED": [{"date": "2026-11-01", "value": 32}],
        }
    }

    result = _client(_with_user(payload)).in_search_history(HOST)

    assert result.rows == [{"date": "2026-11-01", "searchable": 6500, "excluded": 32}]


def test_all_queries_and_per_query_history() -> None:
    seen: list[httpx.Request] = []
    payload = {
        "indicators": {
            "TOTAL_SHOWS": [{"date": "2026-11-01", "value": 100}],
            "TOTAL_CLICKS": [{"date": "2026-11-01", "value": 7}],
            "AVG_SHOW_POSITION": [{"date": "2026-11-01", "value": 4.5}],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        seen.append(request)
        return httpx.Response(200, json=payload)

    client = _client(handler)
    all_rows = client.all_queries_history(HOST)
    query_rows = client.query_history(HOST, "a08b")

    assert all_rows.rows == [{"date": "2026-11-01", "shows": 100, "clicks": 7, "position": 4.5}]
    assert query_rows.rows == all_rows.rows
    expected_indicators = [
        "TOTAL_SHOWS",
        "TOTAL_CLICKS",
        "AVG_SHOW_POSITION",
        "AVG_CLICK_POSITION",
    ]
    assert seen[0].url.params.get_list("query_indicator") == expected_indicators
    assert seen[1].url.path == f"{HOST_PATH}/search-queries/a08b/history/"
    assert seen[1].url.params.get_list("query_indicator") == expected_indicators


def test_recrawl_queue_and_task() -> None:
    seen: list[httpx.Request] = []
    task = {
        "task_id": "task-1",
        "url": "https://example.com/a",
        "added_time": "2026-11-16T10:00:00",
        "state": "DONE",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        seen.append(request)
        if request.url.path.endswith("/recrawl/queue/task-1"):
            return httpx.Response(200, json=task)
        return httpx.Response(200, json={"count": 28, "tasks": [task]})

    client = _client(handler)
    queue = client.recrawl_queue(HOST, limit=10)
    detail = client.recrawl_task(HOST, "task-1")

    assert queue.rows == [task]
    assert queue.total == 28
    assert queue.truncated is True
    assert detail.rows == [task]
    assert seen[0].url.params["limit"] == "10"
    assert seen[1].url.path == f"{HOST_PATH}/recrawl/queue/task-1"


def test_recrawl_enqueue_handles_202_409_and_429() -> None:
    responses = [
        httpx.Response(202, json={"task_id": "task-1", "quota_remainder": 149}),
        httpx.Response(409, json={"error_code": "URL_ALREADY_ADDED"}),
        httpx.Response(
            429,
            json={
                "error_code": "QUOTA_EXCEEDED",
                "daily_quota": 150,
                "exceeded_until": "2026-11-18T00:00:00",
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return responses.pop(0)

    client = _client(handler)
    queued = client.recrawl_enqueue(HOST, "https://example.com/a")
    duplicate = client.recrawl_enqueue(HOST, "https://example.com/a")

    assert queued.rows == [
        {
            "url": "https://example.com/a",
            "status": "queued",
            "task_id": "task-1",
            "quota_remainder": 149,
        }
    ]
    assert queued.limitations == (ONE_URL_PER_CALL,)
    assert duplicate.rows == [
        {
            "url": "https://example.com/a",
            "status": "already_queued",
            "error_code": "URL_ALREADY_ADDED",
        }
    ]

    with pytest.raises(SeoError) as excinfo:
        client.recrawl_enqueue(HOST, "https://example.com/b")

    assert excinfo.value.code == QUOTA_EXCEEDED
    assert excinfo.value.detail["daily_quota"] == 150
    assert excinfo.value.detail["exceeded_until"] == "2026-11-18T00:00:00"


def test_recrawl_enqueue_maps_5xx_without_parsing_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == USER_PATH:
            return httpx.Response(200, json={"user_id": USER_ID})
        return httpx.Response(504)

    client = _client(handler)

    with pytest.raises(SeoError) as excinfo:
        client.recrawl_enqueue(HOST, "https://example.com/a")

    assert excinfo.value.code == UPSTREAM_UNAVAILABLE
    assert excinfo.value.detail["status_code"] == 504
    assert "504" in excinfo.value.message
    assert "invalid JSON" not in excinfo.value.message
