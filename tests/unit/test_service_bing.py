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
        self.closed = False
        FakeBingClient.instances.append(self)

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
