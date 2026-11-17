"""Offline tests for the Bing Webmaster + IndexNow adapter (``httpx.MockTransport``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from lv24_seo.core.errors import (
    AUTH_INVALID,
    AUTH_REQUIRED,
    INVALID_INPUT,
    QUOTA_EXCEEDED,
    SeoError,
)
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


def test_crawl_issues_decodes_the_issue_bitmask() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Url": "https://example.com/missing",
                        "HttpCode": 404,
                        "Issues": 4 | 16 | 256,
                        "InLinks": 3,
                        "Extra": "kept",
                    }
                ]
            },
        )

    result = _bing(handler).crawl_issues()

    assert result.source == "bing"
    assert result.data_age_days == 0
    assert result.rows == [
        {
            "url": "https://example.com/missing",
            "http_code": 404,
            "issues": ["4xx", "robots", "timeout"],
            "issues_bits": 276,
            "in_links": 3,
            "raw": {"Extra": "kept"},
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
    assert result.rows == [
        {
            "accepted": 2,
            "submitted": 2,
            "duplicates_removed": 0,
            "status": "accepted",
            "site_url": SITE,
        }
    ]
    assert result.limitations == ("submit_idempotency_undocumented",)


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


def test_submission_quota_reports_remaining_values_and_raw_row() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": {
                    "__type": "UrlSubmissionQuota:#Microsoft.Bing.Webmaster.Api",
                    "DailyQuota": 100,
                    "MonthlyQuota": 1400,
                }
            },
        )

    result = _bing(handler).submission_quota()

    assert result.rows == [
        {
            "daily_remaining": 100,
            "monthly_remaining": 1400,
            "raw": {
                "__type": "UrlSubmissionQuota:#Microsoft.Bing.Webmaster.Api",
                "DailyQuota": 100,
                "MonthlyQuota": 1400,
            },
        }
    ]
    assert result.total == 1
    assert result.limitations == (
        "quota_monthly_can_bind_first",
        "quota_monthly_reset_undocumented",
    )


def test_feeds_parses_documented_fields_and_states_the_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(bing_client_module, "now_utc", lambda: fetched_at)
    crawled = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
    submitted = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Compressed": False,
                        "FileSize": 1024,
                        "LastCrawled": _wcf_date(crawled),
                        "Status": "Success",
                        "Submitted": _wcf_date(submitted),
                        "Type": "Sitemap",
                        "Url": "https://example.com/sitemap.xml",
                        "UrlCount": 1023,
                        "Extra": "kept",
                    }
                ]
            },
        )

    result = _bing(handler).feeds()

    assert result.rows == [
        {
            "url": "https://example.com/sitemap.xml",
            "type": "Sitemap",
            "status": "Success",
            "submitted": _wcf_date(submitted),
            "last_crawled": _wcf_date(crawled),
            "url_count": 1023,
            "file_size": 1024,
            "compressed": False,
            "raw": {"Extra": "kept"},
        }
    ]
    assert result.data_age_days == 2
    assert result.limitations == (
        "no_per_url_sitemap_errors",
        "sitemap_status_is_not_crawl_status",
    )


def test_feeds_accepts_a_bare_list() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"Url": "https://example.com/sitemap.xml", "Status": "Success"}],
        )

    result = _bing(handler).feeds()

    assert result.total == 1
    assert result.rows[0]["url"] == "https://example.com/sitemap.xml"
    assert result.rows[0]["status"] == "Success"


def test_feeds_parses_a_wcf_date_with_a_display_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(bing_client_module, "now_utc", lambda: fetched_at)
    crawled = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
    wcf_with_offset = _wcf_date(crawled).replace(")/", "-0700)/")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [{"Url": "https://example.com/sitemap.xml", "LastCrawled": wcf_with_offset}]
            },
        )

    result = _bing(handler).feeds()

    assert result.data_age_days == 2


def test_feed_details_queries_the_child_feeds() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"d": [{"Url": "https://example.com/child.xml", "Status": "Success"}]},
        )

    result = _bing(handler).feed_details("https://example.com/sitemap.xml")

    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path.endswith("/GetFeedDetails")
    assert seen[0].url.params["feedUrl"] == "https://example.com/sitemap.xml"
    assert result.rows[0]["url"] == "https://example.com/child.xml"


def test_submit_url_batches_splits_at_500_and_dedupes() -> None:
    posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path.endswith("/GetUrlSubmissionQuota")
            return httpx.Response(
                200,
                json={"d": {"DailyQuota": 100_000, "MonthlyQuota": 100_000}},
            )
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"d": None})

    urls = [f"https://example.com/page/{index}" for index in range(501)]
    urls.insert(0, urls[0])

    result = _bing(handler).submit_url_batches(urls)

    assert [len(body["urlList"]) for body in posts] == [500, 1]  # type: ignore[arg-type]
    assert result.rows == [
        {
            "accepted": 501,
            "submitted": 501,
            "batches": 2,
            "batch_size": 500,
            "duplicates_removed": 1,
            "status": "accepted",
            "site_url": SITE,
        }
    ]
    assert result.limitations == ("submit_idempotency_undocumented",)


@pytest.mark.parametrize(
    ("daily", "monthly", "field"),
    [(2, 1400, "daily_remaining"), (100, 2, "monthly_remaining")],
)
def test_submit_url_batches_prechecks_both_quota_windows(
    daily: int, monthly: int, field: str
) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"d": {"DailyQuota": daily, "MonthlyQuota": monthly}})
        posts.append(request)
        return httpx.Response(200, json={"d": None})

    with pytest.raises(SeoError) as caught:
        _bing(handler).submit_url_batches(
            [f"https://example.com/page/{index}" for index in range(3)]
        )

    assert caught.value.code == "QUOTA_EXCEEDED"
    assert caught.value.detail[field] == (daily if field == "daily_remaining" else monthly)
    assert caught.value.detail["requested"] == 3
    assert posts == []


def test_submit_url_batches_refuses_to_submit_without_quota() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"d": {}})
        posts.append(request)
        return httpx.Response(200, json={"d": None})

    with pytest.raises(SeoError) as caught:
        _bing(handler).submit_url_batches(["https://example.com/a"])

    assert caught.value.code == "UPSTREAM_UNAVAILABLE"
    assert posts == []


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (3, "AUTH_INVALID"),
        (14, "PROPERTY_NOT_FOUND"),
        (7, "INVALID_INPUT"),
        (4, "QUOTA_EXCEEDED"),
        (5, "QUOTA_EXCEEDED"),
    ],
)
def test_bing_error_code_wins_over_the_http_status(error_code: int, expected: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ErrorCode": error_code, "Message": "provider detail that must not leak"},
        )

    with pytest.raises(SeoError) as caught:
        _bing(handler).query_stats()

    assert caught.value.code == expected
    assert caught.value.detail["error_code"] == error_code
    payload = json.dumps(caught.value.to_dict())
    assert API_KEY not in payload
    assert "provider detail that must not leak" not in payload


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
    assert result.rows == [
        {
            "submitted": 2,
            "host": "example.com",
            "status_code": 202,
            "status": "accepted_pending",
        }
    ]
    assert result.warnings == (
        "indexnow accepted pending: key validation is pending at the engine",
    )
    assert result.limitations == ("indexnow_accepted_pending",)


def test_indexnow_200_is_accepted_without_a_warning() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    result = indexnow_submit(
        host="example.com",
        key="abc12345",
        urls=["https://example.com/a"],
        http=_http(handler),
    )

    assert result.rows == [
        {
            "submitted": 1,
            "host": "example.com",
            "status_code": 200,
            "status": "accepted",
        }
    ]
    assert result.warnings == ()
    assert result.limitations == ()


@pytest.mark.parametrize(
    "key",
    ["short", "abc 12345", "abc_12345", "abc12345/extra", "a" * 129],
)
def test_indexnow_rejects_invalid_keys_before_the_network(key: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key=key,
            urls=["https://example.com/a"],
            http=_http(handler),
        )

    assert caught.value.code == INVALID_INPUT
    assert "8-128" in caught.value.message
    assert key not in str(caught.value)


def test_indexnow_403_names_the_exact_key_url_without_echoing_the_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key="abc12345",
            urls=["https://example.com/a"],
            http=_http(handler),
        )

    error = caught.value
    assert error.code == AUTH_INVALID
    assert "key file missing or mismatched" in error.message
    assert error.next_action is not None
    assert "https://example.com/abc12345.txt" in error.next_action
    assert "abc12345" not in error.message
    assert "abc12345" not in repr(error.detail)


def test_indexnow_400_is_a_format_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key="abc12345",
            urls=["https://example.com/a"],
            http=_http(handler),
        )

    assert caught.value.code == INVALID_INPUT
    assert "invalid format" in caught.value.message


def test_indexnow_429_is_spam_or_quota_and_retryable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key="abc12345",
            urls=["https://example.com/a"],
            http=_http(handler),
        )

    assert caught.value.code == QUOTA_EXCEEDED
    assert caught.value.retryable is True
    assert "spam" in caught.value.message


def test_indexnow_key_location_prefix_is_enforced() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key="abc12345",
            urls=["https://example.com/catalog/show", "https://example.com/help/x"],
            key_location="https://example.com/catalog/abc12345.txt",
            http=_http(handler),
        )

    assert caught.value.code == INVALID_INPUT
    assert "/catalog/" in caught.value.message
    assert "https://example.com/help/x" in repr(caught.value.detail)


def test_indexnow_key_location_allows_urls_under_its_prefix() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    indexnow_submit(
        host="example.com",
        key="abc12345",
        urls=["https://example.com/catalog/show", "https://example.com/catalog/42"],
        key_location="https://example.com/catalog/abc12345.txt",
        http=_http(handler),
    )

    assert json.loads(seen[0].content)["keyLocation"] == (
        "https://example.com/catalog/abc12345.txt"
    )


def test_indexnow_key_location_must_be_on_the_submitted_host() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(SeoError) as caught:
        indexnow_submit(
            host="example.com",
            key="abc12345",
            urls=["https://example.com/a"],
            key_location="https://cdn.example.net/abc12345.txt",
            http=_http(handler),
        )

    assert caught.value.code == INVALID_INPUT
    assert "submitted host" in caught.value.message


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


def test_crawl_issues_flags_unknown_bits_instead_of_guessing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"d": [{"Url": "https://example.com/a", "Issues": 512, "HttpCode": 200}]},
        )

    result = _bing(handler).crawl_issues()

    assert result.rows[0]["issues"] == []
    assert result.rows[0]["issues_bits"] == 512
    assert result.limitations == ("unknown_crawl_issue_bits",)


def test_page_stats_maps_pages_positions_and_marks_empty_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(bing_client_module, "now_utc", lambda: fetched_at)
    newest = datetime(2026, 11, 10, tzinfo=UTC)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Query": "https://example.com/pricing",
                        "Clicks": 15,
                        "Impressions": 100,
                        "AvgClickPosition": 18,
                        "AvgImpressionPosition": 17,
                        "Date": _wcf_date(newest),
                    }
                ]
            },
        )

    result = _bing(handler).page_stats()

    assert result.rows == [
        {
            "page": "https://example.com/pricing",
            "clicks": 15,
            "impressions": 100,
            "click_position": 18.0,
            "impression_position": 17.0,
            "date": newest.isoformat(),
        }
    ]
    assert result.data_age_days == 5
    assert result.limitations == ()

    empty = _bing(lambda _request: httpx.Response(200, json={"d": []})).page_stats()

    assert empty.rows == []
    assert empty.total == 0
    assert empty.data_age_days == 0
    assert empty.limitations == ("no_rows_for_window",)


def test_query_stats_empty_is_missing_data_not_zero_impressions() -> None:
    result = _bing(lambda _request: httpx.Response(200, json={"d": []})).query_stats()

    assert result.rows == []
    assert result.total == 0
    assert result.limitations == ("no_rows_for_window",)


def test_rank_and_traffic_maps_daily_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(bing_client_module, "now_utc", lambda: fetched_at)
    day = datetime(2026, 11, 14, tzinfo=UTC)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"d": [{"Date": _wcf_date(day), "Clicks": 3, "Impressions": 40}]},
        )

    result = _bing(handler).rank_and_traffic()

    assert result.rows == [{"date": day.isoformat(), "clicks": 3, "impressions": 40}]
    assert result.data_age_days == 1
    assert result.limitations == ()


def test_crawl_stats_maps_every_bucket_and_states_the_six_month_window() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "AllOtherCodes": 1,
                        "BlockedByRobotsTxt": 2,
                        "Code2xx": 9998,
                        "Code301": 3,
                        "Code302": 4,
                        "Code4xx": 5,
                        "Code5xx": 6,
                        "ContainsMalware": 0,
                        "CrawlErrors": 11,
                        "CrawledPages": 12,
                        "Date": _wcf_date(datetime(2026, 11, 14, tzinfo=UTC)),
                        "InIndex": 1000,
                        "InLinks": 2048,
                    }
                ]
            },
        )

    result = _bing(handler).crawl_stats()

    assert result.rows == [
        {
            "date": "2026-11-14T00:00:00+00:00",
            "code_2xx": 9998,
            "code_301": 3,
            "code_302": 4,
            "code_4xx": 5,
            "code_5xx": 6,
            "blocked_by_robots_txt": 2,
            "contains_malware": 0,
            "crawl_errors": 11,
            "crawled_pages": 12,
            "in_index": 1000,
            "in_links": 2048,
            "all_other_codes": 1,
        }
    ]
    assert result.limitations == ("crawl_stats_six_month_window",)


def test_url_info_maps_fields_and_turns_the_never_sentinel_into_null() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "d": {
                    "Url": "domain:example.com",
                    "AnchorCount": 50,
                    "DiscoveryDate": _wcf_date(datetime(2026, 11, 1, tzinfo=UTC)),
                    "DocumentSize": 0,
                    "HttpStatus": 0,
                    "IsPage": False,
                    "LastCrawledDate": "/Date(-62135596800000)/",
                    "TotalChildUrlCount": 8380,
                }
            },
        )

    result = _bing(handler).url_info("domain:example.com")

    assert result.rows == [
        {
            "url": "domain:example.com",
            "anchor_count": 50,
            "discovery_date": "2026-11-01T00:00:00+00:00",
            "document_size": 0,
            "http_status": 0,
            "is_page": False,
            "last_crawled_date": None,
            "total_child_url_count": 8380,
        }
    ]
    assert result.limitations == ("never_crawled",)
    assert result.data_age_days == 0
    assert seen[0].url.params["url"] == "domain:example.com"


def test_url_info_empty_d_is_provider_returned_no_rows() -> None:
    result = _bing(lambda _request: httpx.Response(200, json={"d": None})).url_info(
        "https://example.com/missing"
    )

    assert result.rows == []
    assert result.total == 0
    assert result.limitations == ("provider_returned_no_rows",)


def test_children_url_info_posts_the_documented_body_and_page() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Url": "https://example.com/a",
                        "AnchorCount": 1,
                        "DiscoveryDate": "/Date(-62135596800000)/",
                        "DocumentSize": 120,
                        "HttpStatus": 200,
                        "IsPage": True,
                        "LastCrawledDate": _wcf_date(datetime(2026, 11, 10, tzinfo=UTC)),
                        "TotalChildUrlCount": 0,
                    }
                ]
            },
        )

    result = _bing(handler).children_url_info("domain:example.com", page=2)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/GetChildrenUrlInfo")
    assert seen[0].url.params["apikey"] == API_KEY
    assert json.loads(seen[0].content) == {
        "siteUrl": SITE,
        "url": "domain:example.com",
        "page": 2,
        "filterProperties": {
            "__type": "FilterProperties:#Microsoft.Bing.Webmaster.Api",
            "CrawlDateFilter": 0,
            "DiscoveredDateFilter": 0,
            "DocFlagsFilters": 0,
            "HttpCodeFilters": 0,
        },
    }
    assert result.rows[0]["url"] == "https://example.com/a"
    assert result.rows[0]["discovery_date"] is None
    assert result.limitations == ()


@pytest.mark.parametrize("bad_page", [-1, 65_536, True])
def test_children_url_info_rejects_an_out_of_range_page_without_a_request(
    bad_page: int,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"d": []})

    with pytest.raises(SeoError) as caught:
        _bing(handler).children_url_info("https://example.com", page=bad_page)

    assert caught.value.code == INVALID_INPUT
    assert calls == []


def test_site_roles_maps_the_enum_and_hides_the_delegated_code() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Date": _wcf_date(datetime(2026, 11, 1, tzinfo=UTC)),
                        "DelegatedCode": "must-not-be-echoed",
                        "DelegatedCodeOwnerEmail": "owner@example.com",
                        "DelegatorEmail": "delegator@example.com",
                        "Email": "user@example.com",
                        "Expired": False,
                        "Role": 2,
                        "Site": "https://example.com",
                        "VerificationSite": "https://example.com",
                    }
                ]
            },
        )

    result = _bing(handler).site_roles(include_all_subdomains=True)

    assert seen[0].url.params["includeAllSubdomains"] == "true"
    assert result.rows == [
        {
            "email": "user@example.com",
            "role": "ReadWrite",
            "role_code": 2,
            "site": "https://example.com",
            "verification_site": "https://example.com",
            "delegator_email": "delegator@example.com",
            "delegated_code_owner_email": "owner@example.com",
            "expired": False,
            "date": "2026-11-01T00:00:00+00:00",
        }
    ]
    assert "must-not-be-echoed" not in json.dumps(result.to_dict())


def test_user_sites_needs_no_site_and_hides_verification_codes() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "AuthenticationCode": "must-not-be-echoed",
                        "DnsVerificationCode": "also-not-echoed",
                        "IsVerified": True,
                        "Url": "https://example.com/",
                    }
                ]
            },
        )

    result = BingClient(api_key=API_KEY, site_url="", http=_http(handler)).user_sites()

    assert result.property_id == ""
    assert result.rows == [{"url": "https://example.com/", "is_verified": True}]
    payload = json.dumps(result.to_dict())
    assert "must-not-be-echoed" not in payload
    assert "also-not-echoed" not in payload


def test_joins_attach_the_requested_dimension_and_the_varying_one() -> None:
    page = "https://example.com/pricing"
    query = "seo tools"

    def query_pages(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"d": [{"Query": page, "Clicks": 7, "Impressions": 120}]},
        )

    query_result = _bing(query_pages).query_page_stats(query)
    assert query_result.rows[0]["query"] == query
    assert query_result.rows[0]["page"] == page

    def page_queries(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"d": [{"Query": query, "Clicks": 7, "Impressions": 120}]},
        )

    page_result = _bing(page_queries).page_query_stats(page)
    assert page_result.rows[0]["query"] == query
    assert page_result.rows[0]["page"] == page

    def detail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "d": [
                    {
                        "Clicks": 7,
                        "Impressions": 120,
                        "Position": 4.5,
                        "Date": _wcf_date(datetime(2026, 11, 10, tzinfo=UTC)),
                    }
                ]
            },
        )

    detail_result = _bing(detail).query_page_detail_stats(query, page)
    assert detail_result.rows[0]["query"] == query
    assert detail_result.rows[0]["page"] == page
    assert detail_result.rows[0]["position"] == 4.5
    assert detail_result.rows[0]["click_position"] is None
    assert detail_result.rows[0]["impression_position"] is None
