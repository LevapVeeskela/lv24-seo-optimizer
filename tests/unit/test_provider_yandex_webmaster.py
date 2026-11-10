"""Offline tests for the Yandex Webmaster adapter (``httpx.MockTransport``, no network)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from lv24_seo.core.errors import AUTH_INVALID, AUTH_REQUIRED, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import (
    DEGRADED_HOST,
    NO_PER_URL_SITEMAP_ERRORS,
    NO_QUERY_PAGE_JOIN,
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


def test_indexing_history_passes_daily_buckets() -> None:
    payload = {"history": [{"date": "2026-11-01", "http_2xx": 100, "http_4xx": 2}]}

    result = _client(_with_user(payload)).indexing_history(HOST)

    assert result.rows == [{"date": "2026-11-01", "http_2xx": 100, "http_4xx": 2}]
    assert result.data_age_days == 0


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
                },
            },
            {"query": "seo минск", "shows": 20, "clicks": 1, "position": 12},
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
    ]
    assert query_request.url.params["order_by"] == "TOTAL_SHOWS"
    assert query_request.url.params["limit"] == "500"

    assert result.rows == [
        {"query": "seo", "shows": 100, "clicks": 7, "position": 4.5},
        {"query": "seo минск", "shows": 20, "clicks": 1, "position": 12},
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


def test_empty_history_returns_degraded_result() -> None:
    result = _client(_with_user({"history": []})).indexing_history(HOST)

    assert result.empty is True
    assert result.warnings == ("host_not_loaded",)
    assert result.limitations == (DEGRADED_HOST,)


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
