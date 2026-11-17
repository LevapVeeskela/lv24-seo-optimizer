"""Offline service tests for the GSC read depth: inspection, sitemaps and query filters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest

from lv24_seo.core.config import (
    CrawlConfig,
    GscConfig,
    ProjectConfig,
    ProvidersConfig,
    ReportConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.errors import INVALID_INPUT
from lv24_seo.core.secrets import SecretStore
from lv24_seo.providers.base import (
    ONE_URL_PER_CALL,
    SITEMAP_INDEX_CHILDREN,
    SITEMAP_INDEXED_DEPRECATED,
    URL_INSPECTION_QUOTA,
    ProviderResult,
)
from lv24_seo.services import SeoService

PROPERTY = "sc-domain:example.com"
FETCHED_AT = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def _config() -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example",
            base_url="https://example.com",
            default_property=PROPERTY,
        ),
        providers=ProvidersConfig(gsc=GscConfig(property=PROPERTY)),
        report=ReportConfig(locale="en"),
        crawl=CrawlConfig(max_urls=50),
        storage=StorageConfig(data_dir="data"),
    )


def _result(rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> ProviderResult[dict[str, Any]]:
    return ProviderResult(
        source="gsc",
        property_id=PROPERTY,
        fetched_at=FETCHED_AT,
        data_age_days=int(kwargs.pop("data_age_days", 0)),
        rows=[dict(row) for row in rows],
        total=kwargs.pop("total", len(rows)),
        limitations=tuple(kwargs.pop("limitations", ())),
        extra=kwargs.pop("extra", {}),
    )


def _inspection_row() -> dict[str, Any]:
    return {
        "url": "https://example.com/places",
        "verdict": "NEUTRAL",
        "coverage_state": "URL is unknown to Google",
        "robots_txt_state": "ALLOWED",
        "indexing_state": "INDEXING_ALLOWED",
        "last_crawl_time": None,
        "page_fetch_state": "NOT_FETCHED",
        "google_canonical": None,
        "user_canonical": None,
        "canonical": None,
        "crawled_as": None,
        "sitemap": [],
        "referring_urls": [],
        "rich_results": {"verdict": None, "detected_items": []},
        "inspection_result_link": "https://search.google.com/search-console/inspect?x=1",
    }


class StubGscClient:
    def __init__(self) -> None:
        self.inspect_calls: list[dict[str, Any]] = []
        self.sitemap_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.index_is_index = True

    def inspect_url(
        self, url: str, *, language_code: str = "en-US"
    ) -> ProviderResult[dict[str, Any]]:
        self.inspect_calls.append({"url": url, "language_code": language_code})
        return _result(
            [_inspection_row()],
            limitations=(
                ONE_URL_PER_CALL,
                URL_INSPECTION_QUOTA,
                "inspection_reflects_last_crawl",
            ),
        )

    def list_sitemaps(self, *, site_url: str | None = None) -> ProviderResult[dict[str, Any]]:
        self.sitemap_calls.append({"method": "list", "site_url": site_url})
        return _result(
            [
                {
                    "path": "https://example.com/sitemap.xml",
                    "last_submitted": "2026-11-01T10:00:00Z",
                    "last_downloaded": "2026-11-16T08:00:00Z",
                    "is_pending": False,
                    "is_sitemaps_index": False,
                    "type": "sitemap",
                    "errors": 0,
                    "warnings": 0,
                    "contents": [
                        {
                            "type": "web",
                            "submitted": 3179,
                            "indexed": 0,
                            "indexed_deprecated": True,
                        }
                    ],
                },
                {
                    "path": "https://example.com/sitemap-seo.xml",
                    "last_submitted": "2026-11-01T10:00:00Z",
                    "last_downloaded": "2026-11-16T08:00:00Z",
                    "is_pending": False,
                    "is_sitemaps_index": True,
                    "type": "sitemap",
                    "errors": 0,
                    "warnings": 0,
                    "contents": [],
                },
            ],
            limitations=(SITEMAP_INDEXED_DEPRECATED, SITEMAP_INDEX_CHILDREN),
        )

    def get_sitemap(
        self, sitemap_url: str, *, site_url: str | None = None
    ) -> ProviderResult[dict[str, Any]]:
        self.sitemap_calls.append({"method": "get", "url": sitemap_url, "site_url": site_url})
        return _result(
            [
                {
                    "path": sitemap_url,
                    "last_submitted": "2026-11-01T10:00:00Z",
                    "last_downloaded": "2026-11-16T08:00:00Z",
                    "is_pending": False,
                    "is_sitemaps_index": self.index_is_index,
                    "type": "sitemap",
                    "errors": 0,
                    "warnings": 0,
                    "contents": [
                        {
                            "type": "sitemap",
                            "submitted": 3,
                            "indexed": 0,
                            "indexed_deprecated": True,
                        }
                    ],
                }
            ],
            limitations=(
                SITEMAP_INDEXED_DEPRECATED,
                SITEMAP_INDEX_CHILDREN,
            ),
        )

    def search_analytics(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        self.query_calls.append(kwargs)
        return _result(
            [
                {
                    "query": "coffee",
                    "device": "MOBILE",
                    "clicks": 2.0,
                    "impressions": 20.0,
                    "ctr": 0.1,
                    "position": 4.0,
                }
            ],
            extra={
                "responseAggregationType": "byPage",
                "metadata": {"first_incomplete_date": "2026-11-16"},
            },
        )


def _service(client: StubGscClient) -> SeoService:
    return SeoService(
        _config(),
        secrets=SecretStore(env={"LV24_SEO_GSC_TOKEN": "token"}),
        clients={"gsc": client},
    )


def test_gsc_inspect_returns_the_full_mapping_and_language() -> None:
    client = StubGscClient()
    response = _service(client).gsc_inspect("https://example.com/places", language="ru-RU")

    assert response["ok"] is True
    assert client.inspect_calls == [{"url": "https://example.com/places", "language_code": "ru-RU"}]
    assert response["data"]["url"] == "https://example.com/places"
    assert response["data"]["language"] == "ru-RU"
    inspection = response["data"]["inspection"]
    assert inspection["verdict"] == "NEUTRAL"
    assert inspection["coverage_state"] == "URL is unknown to Google"
    assert inspection["rich_results"] == {"verdict": None, "detected_items": []}
    assert response["meta"]["language"] == "ru-RU"
    assert ONE_URL_PER_CALL in response["meta"]["limitations"]
    assert "token" not in str(response)


def test_gsc_inspect_rejects_a_relative_url_before_calling_the_client() -> None:
    client = StubGscClient()
    response = _service(client).gsc_inspect("/places")

    assert response["ok"] is False
    assert response["error"]["code"] == INVALID_INPUT
    assert client.inspect_calls == []


def test_gsc_sitemaps_adds_the_index_detail_and_keeps_limitations() -> None:
    client = StubGscClient()
    response = _service(client).gsc_sitemaps(sitemap_index="https://example.com/sitemap-seo.xml")

    assert response["ok"] is True
    assert [call["method"] for call in client.sitemap_calls] == ["list", "get"]
    assert len(response["data"]["rows"]) == 2
    index_row = response["data"]["sitemap_index"]
    assert index_row["is_sitemaps_index"] is True
    assert index_row["contents"][0]["submitted"] == 3
    assert SITEMAP_INDEXED_DEPRECATED in response["meta"]["limitations"]
    assert SITEMAP_INDEX_CHILDREN in response["meta"]["limitations"]


def test_gsc_sitemaps_without_an_index_calls_list_only() -> None:
    client = StubGscClient()
    response = _service(client).gsc_sitemaps()

    assert response["ok"] is True
    assert [call["method"] for call in client.sitemap_calls] == ["list"]
    assert response["data"]["sitemap_index"] is None


def test_gsc_sitemap_reads_one_entry() -> None:
    client = StubGscClient()
    response = _service(client).gsc_sitemap(path="https://example.com/sitemap.xml")

    assert response["ok"] is True
    assert response["data"]["path"] == "https://example.com/sitemap.xml"
    assert response["data"]["sitemap"]["contents"][0]["indexed_deprecated"] is True
    assert client.sitemap_calls[0]["url"] == "https://example.com/sitemap.xml"


def test_gsc_query_forwards_filters_and_returns_api_metadata() -> None:
    client = StubGscClient()
    response = _service(client).gsc_query(
        start=date(2026, 11, 1),
        end=date(2026, 11, 16),
        dimensions=("query", "device"),
        limit=10,
        search_type="image",
        filters=[{"dimension": "country", "operator": "equals", "expression": "BLR"}],
        filter_lists=[{"dimension": "device", "expressions": ["MOBILE"]}],
        data_state="all",
        aggregation="bypage",
    )

    assert response["ok"] is True
    call = client.query_calls[0]
    assert call["search_type"] == "image"
    assert call["filters"] == [{"dimension": "country", "operator": "equals", "expression": "BLR"}]
    assert call["filter_lists"] == [{"dimension": "device", "expressions": ["MOBILE"]}]
    assert call["data_state"] == "all"
    assert call["aggregation_type"] == "byPage"
    assert response["meta"]["type"] == "image"
    assert response["meta"]["dataState"] == "all"
    assert response["meta"]["aggregationType"] == "byPage"
    assert response["meta"]["responseAggregationType"] == "byPage"
    assert response["meta"]["metadata"] == {"first_incomplete_date": "2026-11-16"}
    assert "store: not configured" in " ".join(response["warnings"])


def test_gsc_query_rejects_branded_with_a_query_filter() -> None:
    client = StubGscClient()
    response = _service(client).gsc_query(
        start=date(2026, 11, 1),
        end=date(2026, 11, 16),
        filters=[
            {"dimension": "brandedClassification", "operator": "equals", "expression": "BRANDED"},
            {"dimension": "query", "operator": "contains", "expression": "coffee"},
        ],
    )

    assert response["ok"] is False
    assert response["error"]["code"] == INVALID_INPUT
    assert client.query_calls == []


def test_gsc_query_rejects_an_unknown_type() -> None:
    client = StubGscClient()
    response = _service(client).gsc_query(
        start=date(2026, 11, 1), end=date(2026, 11, 16), search_type="shopping"
    )

    assert response["ok"] is False
    assert response["error"]["code"] == INVALID_INPUT
    assert client.query_calls == []


@pytest.mark.parametrize("language", ["", "en US", "en-US!"])
def test_gsc_language_validation(language: str) -> None:
    response = _service(StubGscClient()).gsc_inspect("https://example.com/", language=language)

    if language:
        assert response["ok"] is False
        assert response["error"]["code"] == INVALID_INPUT
