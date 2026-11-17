"""Offline tests for the shared Bing read/write service methods.

The client is a fake, so no network is involved. The tests pin the site/key resolution,
the honest meta fields and the accepted-not-indexed wording of URL submission.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from lv24_seo.core.config import (
    BingConfig,
    ProjectConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.errors import AUTH_REQUIRED, CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.base import ProviderResult
from lv24_seo.services import SeoService

SITE = "https://example.com"
FEED = "https://example.com/sitemap.xml"
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"


class FakeBingClient:
    instances: ClassVar[list[FakeBingClient]] = []

    def __init__(self, *, api_key: str, site_url: str, **_: object) -> None:
        self.api_key = api_key
        self.site_url = site_url
        self.submit_calls: list[tuple[list[str], int]] = []
        self.details: list[str] = []
        self.children_calls: list[tuple[str, int]] = []
        self.join_calls: list[tuple[str, str, str]] = []
        self.url_info_calls: list[str] = []
        self.closed = False
        FakeBingClient.instances.append(self)

    def page_stats(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=1,
            rows=[
                {
                    "page": URL_A,
                    "clicks": 3,
                    "impressions": 40,
                    "click_position": 2.0,
                    "impression_position": 3.0,
                    "date": "2026-11-14T00:00:00+00:00",
                }
            ],
            total=1,
        )

    def rank_and_traffic(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=1,
            rows=[{"date": "2026-11-14T00:00:00+00:00", "clicks": 3, "impressions": 40}],
            total=1,
        )

    def crawl_stats(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=1,
            rows=[{"date": "2026-11-14T00:00:00+00:00", "code_2xx": 10}],
            total=1,
            limitations=("crawl_stats_six_month_window",),
        )

    def url_info(self, url: str) -> ProviderResult[dict[str, object]]:
        self.url_info_calls.append(url)
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=2,
            rows=[
                {
                    "url": url,
                    "anchor_count": 1,
                    "discovery_date": "2026-11-01T00:00:00+00:00",
                    "document_size": 100,
                    "http_status": 200,
                    "is_page": True,
                    "last_crawled_date": "2026-11-13T00:00:00+00:00",
                    "total_child_url_count": 0,
                }
            ],
            total=1,
        )

    def children_url_info(self, url: str, page: int = 0) -> ProviderResult[dict[str, object]]:
        self.children_calls.append((url, page))
        pages = {
            0: [
                {"url": URL_A, "http_status": 200, "is_page": True},
                {"url": URL_B, "http_status": 200, "is_page": True},
            ],
            1: [{"url": "https://example.com/c", "http_status": 200, "is_page": True}],
        }
        rows = pages.get(page, [])
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=2 if rows else 0,
            rows=rows,
            total=len(rows),
            limitations=("provider_returned_no_rows",) if not rows else (),
        )

    def site_roles(self, **_: object) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"email": "user@example.com", "role": "ReadWrite", "role_code": 2}],
            total=1,
        )

    def user_sites(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"url": "https://example.com/", "is_verified": True}],
            total=1,
        )

    def query_page_stats(self, query: str) -> ProviderResult[dict[str, object]]:
        self.join_calls.append(("query_pages", query, ""))
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"query": query, "page": URL_A, "clicks": 1, "impressions": 2}],
            total=1,
        )

    def page_query_stats(self, page: str) -> ProviderResult[dict[str, object]]:
        self.join_calls.append(("page_queries", "", page))
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"query": "seo tools", "page": page, "clicks": 1, "impressions": 2}],
            total=1,
        )

    def query_page_detail_stats(self, query: str, page: str) -> ProviderResult[dict[str, object]]:
        self.join_calls.append(("query_page_detail", query, page))
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[
                {
                    "query": query,
                    "page": page,
                    "clicks": 1,
                    "impressions": 2,
                    "position": 3.5,
                }
            ],
            total=1,
        )

    def submission_quota(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"daily_remaining": 100, "monthly_remaining": 1400, "raw": {}}],
            total=1,
            limitations=(
                "quota_monthly_can_bind_first",
                "quota_monthly_reset_undocumented",
            ),
        )

    def feeds(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=2,
            rows=[
                {
                    "url": FEED,
                    "type": "Sitemap",
                    "status": "Success",
                    "submitted": None,
                    "last_crawled": None,
                    "url_count": 10,
                    "file_size": 100,
                    "compressed": False,
                    "raw": {},
                }
            ],
            total=1,
            limitations=(
                "no_per_url_sitemap_errors",
                "sitemap_status_is_not_crawl_status",
            ),
        )

    def feed_details(self, feed_url: str) -> ProviderResult[dict[str, object]]:
        self.details.append(feed_url)
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"url": "https://example.com/child.xml", "status": "Success"}],
            total=1,
        )

    def crawl_issues(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[
                {
                    "url": URL_A,
                    "http_code": 404,
                    "issues": ["4xx"],
                    "issues_bits": 4,
                    "in_links": 2,
                    "raw": {},
                }
            ],
            total=1,
        )

    def submit_url_batches(
        self, urls: list[str], *, batch_size: int = 500
    ) -> ProviderResult[dict[str, object]]:
        self.submit_calls.append((list(urls), batch_size))
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[
                {
                    "accepted": len(urls),
                    "submitted": len(urls),
                    "batches": 1,
                    "batch_size": batch_size,
                    "duplicates_removed": 0,
                    "status": "accepted",
                    "site_url": self.site_url,
                }
            ],
            limitations=("submit_idempotency_undocumented",),
        )

    def close(self) -> None:
        self.closed = True


def _config(tmp_path: Path, *, site: str = SITE) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(name="Example", base_url="https://example.com"),
        providers=ProvidersConfig(bing=BingConfig(site=site) if site else None),
        storage=StorageConfig(data_dir=tmp_path / "data"),
    )


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeBingClient.instances.clear()
    monkeypatch.setattr("lv24_seo.services.BingClient", FakeBingClient)


def _service(tmp_path: Path, *, site: str = SITE) -> SeoService:
    return SeoService(
        _config(tmp_path, site=site),
        secrets=SecretStore(env={"LV24_SEO_BING_API_KEY": "unit-test-key"}),
    )


def test_bing_quota_returns_remaining_values_and_limitations(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_quota()

    assert payload["ok"] is True
    assert payload["data"]["site_url"] == SITE
    assert payload["data"]["quota"]["daily_remaining"] == 100
    assert payload["data"]["quota"]["monthly_remaining"] == 1400
    assert payload["meta"]["data_age_days"] == 0
    assert payload["meta"]["truncated"] is False
    assert "quota_monthly_can_bind_first" in payload["meta"]["limitations"]
    assert FakeBingClient.instances[0].closed is True


def test_bing_sitemaps_reports_that_per_url_errors_are_unavailable(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_sitemaps()

    assert payload["ok"] is True
    assert payload["data"]["rows"][0]["url"] == FEED
    assert payload["meta"]["data_age_days"] == 2
    assert "no_per_url_sitemap_errors" in payload["meta"]["limitations"]
    assert "sitemap_status_is_not_crawl_status" in payload["meta"]["limitations"]


def test_bing_sitemaps_details_reads_one_feed_index(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_sitemaps(details=FEED)

    assert payload["ok"] is True
    assert payload["data"]["rows"][0]["url"] == "https://example.com/child.xml"
    assert FakeBingClient.instances[0].details == [FEED]


def test_bing_issues_returns_decoded_bits(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_issues()

    assert payload["ok"] is True
    assert payload["data"]["rows"][0]["issues"] == ["4xx"]
    assert payload["data"]["rows"][0]["issues_bits"] == 4


def test_bing_submit_reports_accepted_and_quota_after(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_submit_urls(urls=[URL_A, URL_B])

    assert payload["ok"] is True
    assert payload["data"]["status"] == "accepted"
    assert payload["data"]["accepted"] == 2
    assert payload["data"]["submitted"] == 2
    assert payload["data"]["batches"] == 1
    assert payload["data"]["quota_after"]["daily_remaining"] == 100
    assert payload["meta"]["total"] == 2
    assert payload["meta"]["truncated"] is False
    assert payload["meta"]["limitations"] == ["submit_idempotency_undocumented"]
    assert FakeBingClient.instances[0].submit_calls == [([URL_A, URL_B], 500)]
    assert FakeBingClient.instances[0].closed is True


def test_bing_submit_survives_a_failed_quota_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class QuotaAfterFails(FakeBingClient):
        def submission_quota(self) -> ProviderResult[dict[str, object]]:
            raise SeoError(
                code="UPSTREAM_UNAVAILABLE",
                message="quota read failed",
                retryable=True,
            )

    monkeypatch.setattr("lv24_seo.services.BingClient", QuotaAfterFails)

    payload = _service(tmp_path).bing_submit_urls(urls=[URL_A])

    assert payload["ok"] is True
    assert payload["data"]["status"] == "accepted"
    assert payload["data"]["quota_after"] is None
    assert any("quota after submit could not be read" in item for item in payload["warnings"])


def test_bing_submit_rejects_a_batch_size_above_the_provider_limit(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_submit_urls(urls=[URL_A], batch_size=501)

    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT
    assert FakeBingClient.instances == []


def test_missing_site_is_config_invalid(tmp_path: Path) -> None:
    payload = _service(tmp_path, site="").bing_quota()

    assert payload["ok"] is False
    assert payload["error"]["code"] == CONFIG_INVALID
    assert "providers.bing.site" in payload["error"]["next_action"]


def test_missing_key_requires_auth(tmp_path: Path) -> None:
    service = SeoService(_config(tmp_path), secrets=SecretStore(env={}))

    payload = service.bing_quota()

    assert payload["ok"] is False
    assert payload["error"]["code"] == AUTH_REQUIRED
    assert FakeBingClient.instances == []


def test_missing_key_blocks_the_url_write(tmp_path: Path) -> None:
    service = SeoService(_config(tmp_path), secrets=SecretStore(env={}))

    payload = service.bing_submit_urls(urls=[URL_A])

    assert payload["ok"] is False
    assert payload["error"]["code"] == AUTH_REQUIRED
    assert FakeBingClient.instances == []


def test_unknown_client_override_is_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(SeoError) as caught:
        SeoService(_config(tmp_path), clients={"nope": object()})

    assert caught.value.code == INVALID_INPUT


def test_bing_pages_traffic_and_crawl_return_rows(tmp_path: Path) -> None:
    pages = _service(tmp_path).bing_pages()
    assert pages["ok"] is True
    assert pages["data"]["rows"][0]["page"] == URL_A
    assert pages["meta"]["data_age_days"] == 1

    traffic = _service(tmp_path).bing_traffic()
    assert traffic["data"]["rows"][0]["clicks"] == 3

    crawl = _service(tmp_path).bing_crawl()
    assert crawl["data"]["rows"][0]["code_2xx"] == 10
    assert "crawl_stats_six_month_window" in crawl["meta"]["limitations"]


def test_bing_empty_stats_keep_the_no_rows_limitation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyStats(FakeBingClient):
        def page_stats(self) -> ProviderResult[dict[str, object]]:
            return ProviderResult(
                source="bing",
                property_id=self.site_url,
                fetched_at=utc_now(),
                data_age_days=0,
                rows=[],
                total=0,
                limitations=("no_rows_for_window",),
            )

    monkeypatch.setattr("lv24_seo.services.BingClient", EmptyStats)

    payload = _service(tmp_path).bing_pages()

    assert payload["ok"] is True
    assert payload["data"]["rows"] == []
    assert payload["meta"]["total"] == 0
    assert "no_rows_for_window" in payload["meta"]["limitations"]


def test_bing_url_info_returns_one_row_and_echoes_the_target(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_url_info("domain:example.com")

    assert payload["ok"] is True
    assert payload["data"]["url"] == "domain:example.com"
    assert payload["data"]["rows"][0]["total_child_url_count"] == 0
    assert FakeBingClient.instances[0].url_info_calls == ["domain:example.com"]


def test_bing_url_info_empty_is_provider_returned_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyUrlInfo(FakeBingClient):
        def url_info(self, url: str) -> ProviderResult[dict[str, object]]:
            return ProviderResult(
                source="bing",
                property_id=self.site_url,
                fetched_at=utc_now(),
                data_age_days=0,
                rows=[],
                total=0,
                limitations=("provider_returned_no_rows",),
            )

    monkeypatch.setattr("lv24_seo.services.BingClient", EmptyUrlInfo)

    payload = _service(tmp_path).bing_url_info("https://example.com/missing")

    assert payload["ok"] is True
    assert payload["data"]["rows"] == []
    assert "provider_returned_no_rows" in payload["meta"]["limitations"]


def test_bing_children_walks_pages_and_marks_the_cap(tmp_path: Path) -> None:
    service = _service(tmp_path)

    complete = service.bing_children(URL_A, page=0, pages=3)

    assert complete["ok"] is True
    assert complete["data"]["pages_read"] == 3
    assert len(complete["data"]["rows"]) == 3
    assert complete["meta"]["truncated"] is False
    assert "provider_returned_no_rows" not in complete["meta"]["limitations"]
    assert FakeBingClient.instances[0].children_calls == [
        (URL_A, 0),
        (URL_A, 1),
        (URL_A, 2),
    ]

    capped = _service(tmp_path).bing_children(URL_A, page=0, pages=1)

    assert capped["data"]["pages_read"] == 1
    assert len(capped["data"]["rows"]) == 2
    assert capped["meta"]["truncated"] is True
    assert "children_page_cap" in capped["meta"]["limitations"]


def test_bing_children_rejects_pages_above_the_cap(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_children(URL_A, pages=11)

    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT
    assert FakeBingClient.instances == []


def test_bing_users_and_sites_read_the_account_views(tmp_path: Path) -> None:
    users = _service(tmp_path).bing_users()
    assert users["data"]["rows"][0]["role"] == "ReadWrite"

    sites = _service(tmp_path).bing_sites()
    assert sites["data"]["rows"] == [{"url": "https://example.com/", "is_verified": True}]


def test_bing_sites_works_without_a_configured_site(tmp_path: Path) -> None:
    payload = _service(tmp_path, site="").bing_sites()

    assert payload["ok"] is True
    assert payload["data"]["rows"][0]["url"] == "https://example.com/"
    assert "site_url" not in payload["data"]
    assert FakeBingClient.instances[0].site_url == ""


def test_bing_joins_selects_the_mode_from_the_flags(tmp_path: Path) -> None:
    query_pages = _service(tmp_path).bing_joins(query="seo tools")
    assert query_pages["data"]["mode"] == "query_pages"
    assert query_pages["data"]["query"] == "seo tools"
    assert query_pages["data"]["page"] is None

    page_queries = _service(tmp_path).bing_joins(page=URL_A)
    assert page_queries["data"]["mode"] == "page_queries"
    assert page_queries["data"]["query"] is None
    assert page_queries["data"]["page"] == URL_A

    detail = _service(tmp_path).bing_joins(query="seo tools", page=URL_A)
    assert detail["data"]["mode"] == "query_page_detail"
    assert detail["data"]["rows"][0]["position"] == 3.5

    assert FakeBingClient.instances[-1].join_calls == [("query_page_detail", "seo tools", URL_A)]


def test_bing_joins_requires_a_query_or_page(tmp_path: Path) -> None:
    payload = _service(tmp_path).bing_joins()

    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT
    assert FakeBingClient.instances == []


def test_bing_url_info_missing_site_is_config_invalid(tmp_path: Path) -> None:
    payload = _service(tmp_path, site="").bing_url_info(URL_A)

    assert payload["ok"] is False
    assert payload["error"]["code"] == CONFIG_INVALID
    assert "providers.bing.site" in payload["error"]["next_action"]


def test_bing_url_info_missing_key_requires_auth(tmp_path: Path) -> None:
    service = SeoService(_config(tmp_path), secrets=SecretStore(env={}))

    payload = service.bing_url_info(URL_A)

    assert payload["ok"] is False
    assert payload["error"]["code"] == AUTH_REQUIRED
    assert FakeBingClient.instances == []
