"""Offline tests for the Bing Webmaster + IndexNow adapter (``httpx.MockTransport``)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.bing import BingClient, indexnow_submit

Handler = Callable[[httpx.Request], httpx.Response]

SITE = "https://example.com"
API_KEY = "secret-bing-key"


def _http(handler: Handler) -> HttpClient:
    """An :class:`HttpClient` that never touches the network or sleeps."""
    return HttpClient(
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )


def _bing(handler: Handler) -> BingClient:
    return BingClient(api_key=API_KEY, site_url=SITE, http=_http(handler))


def test_empty_api_key_requires_auth() -> None:
    with pytest.raises(SeoError) as caught:
        BingClient(api_key="   ", site_url=SITE)

    assert caught.value.code == AUTH_REQUIRED
    assert "LV24_SEO_BING_API_KEY" in caught.value.message
    assert caught.value.next_action == "set LV24_SEO_BING_API_KEY"


def test_query_stats_maps_bing_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Query": "seo tools",
                        "Clicks": 7,
                        "Impressions": 120,
                        "AvgImpressionPos": 4.5,
                        "Unmapped": "ignored",
                    },
                    {"query": "case-insensitive", "clicks": "3"},
                ]
            },
        )

    result = _bing(handler).query_stats()

    assert result.source == "bing"
    assert result.property_id == SITE
    assert result.data_age_days == 0
    assert result.total == 2
    assert result.rows == [
        {"query": "seo tools", "clicks": 7, "impressions": 120, "position": 4.5},
        {"query": "case-insensitive", "clicks": 3, "impressions": None, "position": None},
    ]
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path.endswith("/GetQueryStats")
    assert seen[0].url.params["siteUrl"] == SITE
    assert seen[0].url.params["apikey"] == API_KEY


def test_crawl_issues_maps_rows_and_keeps_unknown_keys_in_raw() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Url": "https://example.com/missing",
                        "IssueType": "Http404",
                        "Severity": "Error",
                        "DateDetected": "2026-11-09T10:00:00",
                        "HttpCode": 404,
                    }
                ]
            },
        )

    result = _bing(handler).crawl_issues()

    assert result.source == "bing"
    assert result.rows == [
        {
            "url": "https://example.com/missing",
            "issue_type": "Http404",
            "severity": "Error",
            "detected": "2026-11-09T10:00:00",
            "raw": {"HttpCode": 404},
        }
    ]


def test_submit_urls_posts_batch_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"d": None})

    urls = ["https://example.com/a", "https://example.com/b"]
    result = _bing(handler).submit_urls(urls)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/SubmitUrlbatch")
    assert json.loads(seen[0].content) == {"siteUrl": SITE, "urlList": urls}
    assert result.source == "bing"
    assert result.rows == [{"submitted": 2, "site_url": SITE}]


def test_submit_urls_rejects_more_than_500_without_calling_network() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"d": None})

    urls = [f"https://example.com/page/{index}" for index in range(501)]

    with pytest.raises(SeoError) as caught:
        _bing(handler).submit_urls(urls)

    assert caught.value.code == INVALID_INPUT
    assert caught.value.next_action == "split into batches of 500"
    assert caught.value.detail == {"count": 501, "limit": 500}
    assert calls == []


def test_submit_urls_rejects_empty_list() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(SeoError) as caught:
        _bing(handler).submit_urls([])

    assert caught.value.code == INVALID_INPUT
    assert "at least one URL" in caught.value.message


def test_api_key_never_leaks_into_error_messages() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"d": "bad request"})

    with pytest.raises(SeoError) as caught:
        _bing(handler).query_stats()

    assert caught.value.code == INVALID_INPUT
    payload = json.dumps(caught.value.to_dict())
    assert API_KEY not in payload
    assert API_KEY not in str(caught.value)


def test_indexnow_payload_shape_and_202_accepted() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    result = indexnow_submit(
        host="example.com",
        key="abc12345",
        urls=["https://example.com/a", "https://example.com/b"],
        key_location="https://example.com/abc12345.txt",
        http=_http(handler),
    )

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://api.indexnow.org/indexnow"
    assert json.loads(seen[0].content) == {
        "host": "example.com",
        "key": "abc12345",
        "keyLocation": "https://example.com/abc12345.txt",
        "urlList": ["https://example.com/a", "https://example.com/b"],
    }
    assert result.source == "indexnow"
    assert result.property_id == "example.com"
    assert result.data_age_days == 0
    assert result.rows == [{"submitted": 2, "host": "example.com", "status_code": 202}]


def test_indexnow_omits_key_location_when_not_given() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    indexnow_submit(
        host="example.com",
        key="abc12345",
        urls=["https://example.com/a"],
        http=_http(handler),
    )

    payload = json.loads(seen[0].content)
    assert set(payload) == {"host", "key", "urlList"}


def test_indexnow_422_is_reported_as_key_file_problem() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "key mismatch"})

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key="abc12345",
            urls=["https://example.com/a"],
            http=_http(handler),
        )

    assert caught.value.code == INVALID_INPUT
    assert caught.value.next_action is not None
    assert "https://example.com/abc12345.txt" in caught.value.next_action


def test_indexnow_rejects_more_than_10000_urls() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    urls = [f"https://example.com/page/{index}" for index in range(10_001)]

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key="abc12345",
            urls=urls,
            http=_http(handler),
        )

    assert caught.value.code == INVALID_INPUT
    assert caught.value.detail == {"count": 10_001, "limit": 10_000}


def test_indexnow_requires_a_key() -> None:
    with pytest.raises(SeoError) as caught:
        indexnow_submit(host="example.com", key="  ", urls=["https://example.com/a"])

    assert caught.value.code == AUTH_REQUIRED
