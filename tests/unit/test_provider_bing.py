"""Offline tests for the Bing Webmaster + IndexNow adapter (``httpx.MockTransport``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.bing import BingClient, indexnow_submit
from lv24_seo.providers.bing import client as bing_client_module

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


def test_submit_feed_posts_the_current_json_endpoint() -> None:
    seen: list[httpx.Request] = []
    feed = "https://example.com/sitemap.xml"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"d": None})

    result = _bing(handler).submit_feed(feed)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/SubmitFeed")
    assert seen[0].url.params["apikey"] == API_KEY
    assert json.loads(seen[0].content) == {"siteUrl": SITE, "feedUrl": feed}
    assert result.source == "bing"
    assert result.property_id == SITE
    assert result.data_age_days == 0
    assert result.rows == [{"submitted": True, "site_url": SITE, "sitemap_url": feed}]


@pytest.mark.parametrize("bad", ["", "   ", "not-a-url", "/sitemap.xml", "ftp://example.com/s.xml"])
def test_submit_feed_rejects_invalid_urls_without_a_request(bad: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"d": None})

    with pytest.raises(SeoError) as caught:
        _bing(handler).submit_feed(bad)

    assert caught.value.code == INVALID_INPUT
    assert calls == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, INVALID_INPUT),
        (401, "AUTH_INVALID"),
        (403, "AUTH_INVALID"),
        (404, "PROPERTY_NOT_FOUND"),
        (429, "QUOTA_EXCEEDED"),
        (500, "UPSTREAM_UNAVAILABLE"),
    ],
)
def test_submit_feed_maps_provider_errors(status: int, code: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"d": "rejected"})

    with pytest.raises(SeoError) as caught:
        _bing(handler).submit_feed("https://example.com/sitemap.xml")

    assert caught.value.code == code
    payload = json.dumps(caught.value.to_dict())
    assert API_KEY not in payload


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


def _wcf_date(value: datetime) -> str:
    """Bing/WCF JSON date: milliseconds since the Unix epoch."""
    return f"/Date({int(value.timestamp() * 1000)})/"


def test_query_stats_data_age_comes_from_the_newest_row_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(bing_client_module, "now_utc", lambda: fetched_at)
    newest = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Query": "old",
                        "Clicks": 1,
                        "Date": _wcf_date(datetime(2026, 10, 1, tzinfo=UTC)),
                    },
                    {"Query": "new", "Clicks": 2, "Date": _wcf_date(newest)},
                ]
            },
        )

    result = _bing(handler).query_stats()

    # 2026-11-10T00:00Z -> 2026-11-15T12:00Z is five whole days; a hardcoded 0 would
    # present week-old query data as fresh.
    assert result.data_age_days == 5
    assert result.total == 2


def test_query_stats_without_dates_is_zero_not_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(bing_client_module, "now_utc", lambda: fetched_at)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"d": [{"Query": "seo", "Clicks": 1}]})

    assert _bing(handler).query_stats().data_age_days == 0


def test_crawl_issues_data_age_comes_from_the_newest_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(bing_client_module, "now_utc", lambda: fetched_at)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {"Url": "https://example.com/a", "DateDetected": "2026-10-01T10:00:00"},
                    {"Url": "https://example.com/b", "DateDetected": "2026-11-13T10:00:00"},
                ]
            },
        )

    result = _bing(handler).crawl_issues()

    assert result.data_age_days == 2
