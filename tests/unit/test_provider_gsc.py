"""Offline tests for the Google Search Console adapter (``httpx.MockTransport``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from lv24_seo.core.errors import AUTH_INVALID, AUTH_REQUIRED, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import ONE_URL_PER_CALL, ROWS_CAP_50000
from lv24_seo.providers.gsc import GscClient
from lv24_seo.providers.gsc import client as gsc_client

Handler = Callable[[httpx.Request], httpx.Response]
PROPERTY = "https://example.com/"


def _gsc(
    handler: Handler,
    *,
    access_token: str = "test-token",
    property_id: str = PROPERTY,
) -> GscClient:
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    return GscClient(access_token=access_token, http=http, property_id=property_id)


def _body(request: httpx.Request) -> dict[str, Any]:
    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    return payload


def test_auth_header_and_sites_mapping() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "siteEntry": [
                    {"siteUrl": "https://example.com/", "permissionLevel": "siteOwner"},
                    {"siteUrl": "sc-domain:example.org", "permissionLevel": "siteFullUser"},
                ]
            },
        )

    result = _gsc(handler).list_sites()

    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://www.googleapis.com/webmasters/v3/sites"
    assert seen[0].headers["Authorization"] == "Bearer test-token"
    assert result.source == "gsc"
    assert result.rows == [
        {"site_url": "https://example.com/", "permission_level": "siteOwner"},
        {"site_url": "sc-domain:example.org", "permission_level": "siteFullUser"},
    ]
    assert result.total == 2
    assert result.truncated is False
    assert result.data_age_days == 0


def test_list_sitemaps_mapping() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "sitemap": [
                    {
                        "path": "https://example.com/sitemap.xml",
                        "lastSubmitted": "2026-11-01T10:00:00.000Z",
                        "lastDownloaded": "2026-11-09T08:30:00.000Z",
                        "isPending": True,
                        "errors": "2",
                        "warnings": "1",
                    }
                ]
            },
        )

    result = _gsc(handler).list_sitemaps()

    assert str(seen[0].url) == (
        "https://www.googleapis.com/webmasters/v3/sites/https%3A%2F%2Fexample.com%2F/sitemaps"
    )
    assert result.property_id == PROPERTY
    assert result.rows == [
        {
            "path": "https://example.com/sitemap.xml",
            "last_submitted": "2026-11-01T10:00:00.000Z",
            "last_downloaded": "2026-11-09T08:30:00.000Z",
            "is_pending": True,
            "errors": 2,
            "warnings": 1,
        }
    ]


def test_search_analytics_maps_rows_and_data_age(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    fetched_at = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(gsc_client, "now_utc", lambda: fetched_at)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "keys": ["coffee", "https://example.com/coffee"],
                        "clicks": 10,
                        "impressions": 100,
                        "ctr": 0.1,
                        "position": 3.5,
                    }
                ]
            },
        )

    result = _gsc(handler).search_analytics(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 3),
        dimensions=("query", "page"),
        filters=[{"dimension": "country", "operator": "equals", "expression": "blr"}],
    )

    assert result.rows == [
        {
            "query": "coffee",
            "page": "https://example.com/coffee",
            "clicks": 10.0,
            "impressions": 100.0,
            "ctr": 0.1,
            "position": 3.5,
        }
    ]
    # 2026-11-03T23:59Z -> 2026-11-10T12:00Z is 6 whole days.
    assert result.data_age_days == 6
    assert result.total == 1
    assert result.truncated is False
    assert ROWS_CAP_50000 in result.limitations
    assert _body(seen[0]) == {
        "startDate": "2026-10-01",
        "endDate": "2026-11-03",
        "dimensions": ["query", "page"],
        "type": "web",
        "rowLimit": 25_000,
        "startRow": 0,
        "filters": [{"dimension": "country", "operator": "equals", "expression": "blr"}],
    }


def test_search_analytics_paginates_and_marks_truncated() -> None:
    bodies: list[dict[str, Any]] = []
    pages = {
        0: [
            {
                "keys": ["one"],
                "clicks": 1,
                "impressions": 2,
                "ctr": 0.5,
                "position": 1.0,
            },
            {
                "keys": ["two"],
                "clicks": 1,
                "impressions": 2,
                "ctr": 0.5,
                "position": 2.0,
            },
        ],
        2: [
            {
                "keys": ["three"],
                "clicks": 1,
                "impressions": 2,
                "ctr": 0.5,
                "position": 3.0,
            },
            {
                "keys": ["four"],
                "clicks": 1,
                "impressions": 2,
                "ctr": 0.5,
                "position": 4.0,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        bodies.append(body)
        return httpx.Response(200, json={"rows": pages.get(body["startRow"], [])})

    result = _gsc(handler).search_analytics(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 3),
        row_limit=2,
        max_rows=3,
    )

    assert [body["startRow"] for body in bodies] == [0, 2]
    assert [body["rowLimit"] for body in bodies] == [2, 2]
    assert [row["query"] for row in result.rows] == ["one", "two", "three"]
    assert result.truncated is True
    assert result.total == 3
    assert ROWS_CAP_50000 not in result.limitations
    assert result.warnings
    assert "3 rows" in result.warnings[0]


def test_search_analytics_stops_on_short_batch() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "keys": ["only"],
                        "clicks": 1,
                        "impressions": 2,
                        "ctr": 0.5,
                        "position": 1.0,
                    }
                ]
            },
        )

    result = _gsc(handler).search_analytics(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 3),
        row_limit=2,
        max_rows=10,
    )

    assert calls == 1
    assert len(result.rows) == 1
    assert result.truncated is False


def test_inspect_url_maps_inspection_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "inspectionResult": {
                    "indexStatusResult": {
                        "verdict": "PASS",
                        "coverageState": "Submitted and indexed",
                        "indexingState": "INDEXING_ALLOWED",
                        "lastCrawlTime": "2026-11-09T08:00:00Z",
                        "googleCanonical": "https://example.com/page",
                        "userCanonical": "https://example.com/page",
                        "robotsTxtState": "ALLOWED",
                        "pageFetchState": "SUCCESSFUL",
                    }
                }
            },
        )

    result = _gsc(handler).inspect_url("https://example.com/page")

    assert str(seen[0].url) == (
        "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
    )
    assert _body(seen[0]) == {
        "inspectionUrl": "https://example.com/page",
        "siteUrl": PROPERTY,
    }
    assert result.rows == [
        {
            "url": "https://example.com/page",
            "verdict": "PASS",
            "coverage_state": "Submitted and indexed",
            "indexing_state": "INDEXING_ALLOWED",
            "last_crawl_time": "2026-11-09T08:00:00Z",
            "canonical": "https://example.com/page",
            "robots_txt_state": "ALLOWED",
            "page_fetch_state": "SUCCESSFUL",
        }
    ]
    assert result.total == 1
    assert result.truncated is False
    assert result.limitations == (ONE_URL_PER_CALL,)


def test_401_is_mapped_to_auth_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})

    with pytest.raises(SeoError) as excinfo:
        _gsc(handler).list_sites()

    assert excinfo.value.code == AUTH_INVALID
    assert excinfo.value.retryable is False


def test_empty_token_raises_auth_required() -> None:
    with pytest.raises(SeoError) as excinfo:
        GscClient(access_token="   ")

    assert excinfo.value.code == AUTH_REQUIRED
    assert excinfo.value.next_action == "lv24-seo auth login --provider gsc"


def test_property_id_is_url_encoded_in_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": []})

    _gsc(handler, property_id="sc-domain:example.org").search_analytics(
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 3),
    )

    raw_path = seen[0].url.raw_path.decode("ascii")
    assert "/sites/sc-domain%3Aexample.org/searchAnalytics/query" in raw_path
