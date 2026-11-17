"""Offline tests for the shared sitemap write path (CLI + MCP).

The Bing client is replaced by a fake, so nothing touches the network. The tests pin the
resolver defaults (config site, ``<base_url>/sitemap.xml``), the missing site/key errors and
the honest ``WRITE_DISABLED`` for GSC submit and Bing delete.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from lv24_seo import services as services_module
from lv24_seo.core.config import (
    BingConfig,
    ProjectConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.errors import (
    AUTH_REQUIRED,
    CONFIG_INVALID,
    INVALID_INPUT,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.base import ProviderResult
from lv24_seo.services import SeoService

SITE = "https://example.com"
BASE_URL = "https://example.com/"
FEED = "https://example.com/sitemap.xml"


class FakeBingClient:
    """Capture ``submit_feed`` calls without constructing an HTTP client."""

    instances: ClassVar[list[FakeBingClient]] = []

    def __init__(self, *, api_key: str, site_url: str, **_: object) -> None:
        self.api_key = api_key
        self.site_url = site_url
        self.feeds: list[str] = []
        self.closed = False
        FakeBingClient.instances.append(self)

    def submit_feed(self, sitemap_url: str) -> ProviderResult[dict[str, object]]:
        self.feeds.append(sitemap_url)
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"submitted": True, "site_url": self.site_url, "sitemap_url": sitemap_url}],
        )

    def close(self) -> None:
        self.closed = True


def _config(
    tmp_path: Path,
    *,
    site: str = SITE,
    base_url: str = BASE_URL,
) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(name="Example", base_url=base_url),
        providers=ProvidersConfig(bing=BingConfig(site=site) if site else None),
        storage=StorageConfig(data_dir=tmp_path / "data"),
    )


def _secrets(**env: str) -> SecretStore:
    return SecretStore(env=env)


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeBingClient.instances.clear()


def test_bing_target_defaults_to_the_configured_site_and_base_url(tmp_path: Path) -> None:
    service = SeoService(
        _config(tmp_path),
        secrets=_secrets(LV24_SEO_BING_API_KEY="bing-key"),
    )

    target = service.sitemap_target(provider="bing")

    assert target == {"provider": "bing", "site_url": SITE, "sitemap_url": FEED}


def test_bing_target_accepts_a_property_and_url_override(tmp_path: Path) -> None:
    service = SeoService(
        _config(tmp_path),
        secrets=_secrets(LV24_SEO_BING_API_KEY="bing-key"),
    )

    target = service.sitemap_target(
        provider="bing",
        property="https://other.example",
        sitemap_url="https://other.example/feed.xml",
    )

    assert target == {
        "provider": "bing",
        "site_url": "https://other.example",
        "sitemap_url": "https://other.example/feed.xml",
    }


def test_bing_target_requires_a_configured_site(tmp_path: Path) -> None:
    service = SeoService(
        _config(tmp_path, site=""),
        secrets=_secrets(LV24_SEO_BING_API_KEY="bing-key"),
    )

    with pytest.raises(SeoError) as caught:
        service.sitemap_target(provider="bing")

    assert caught.value.code == CONFIG_INVALID
    assert "providers.bing.site" in (caught.value.next_action or "")


def test_bing_target_requires_the_api_key(tmp_path: Path) -> None:
    service = SeoService(_config(tmp_path), secrets=_secrets())

    with pytest.raises(SeoError) as caught:
        service.sitemap_target(provider="bing")

    assert caught.value.code == AUTH_REQUIRED
    assert "LV24_SEO_BING_API_KEY" in (caught.value.next_action or "")


def test_bing_target_requires_a_base_url_for_the_default_sitemap(tmp_path: Path) -> None:
    service = SeoService(
        _config(tmp_path, base_url=""),
        secrets=_secrets(LV24_SEO_BING_API_KEY="bing-key"),
    )

    with pytest.raises(SeoError) as caught:
        service.sitemap_target(provider="bing")

    assert caught.value.code == CONFIG_INVALID
    assert "project.base_url" in caught.value.message


def test_bing_target_rejects_a_relative_sitemap(tmp_path: Path) -> None:
    service = SeoService(
        _config(tmp_path),
        secrets=_secrets(LV24_SEO_BING_API_KEY="bing-key"),
    )

    with pytest.raises(SeoError) as caught:
        service.sitemap_target(provider="bing", sitemap_url="sitemap.xml")

    assert caught.value.code == INVALID_INPUT


def test_gsc_target_still_requires_property_and_url(tmp_path: Path) -> None:
    service = SeoService(_config(tmp_path))

    with pytest.raises(SeoError) as no_property:
        service.sitemap_target(provider="gsc", sitemap_url=FEED)
    with pytest.raises(SeoError) as no_url:
        service.sitemap_target(provider="gsc", property="sc-domain:example.com")

    assert no_property.value.code == INVALID_INPUT
    assert no_url.value.code == INVALID_INPUT


def test_submit_sitemap_bing_calls_submit_feed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services_module, "BingClient", FakeBingClient)
    service = SeoService(
        _config(tmp_path),
        secrets=_secrets(LV24_SEO_BING_API_KEY="bing-key"),
    )

    payload = service.submit_sitemap(provider="bing")

    assert payload["ok"] is True
    assert payload["data"] == {
        "provider": "bing",
        "status": "submitted",
        "site_url": SITE,
        "sitemap_url": FEED,
    }
    assert FakeBingClient.instances[0].api_key == "bing-key"
    assert FakeBingClient.instances[0].feeds == [FEED]
    assert FakeBingClient.instances[0].closed is True


def test_submit_sitemap_uses_an_injected_client_without_closing_it(tmp_path: Path) -> None:
    fake = FakeBingClient(api_key="unused", site_url=SITE)
    service = SeoService(_config(tmp_path), clients={"bing": fake})

    payload = service.submit_sitemap(provider="bing")

    assert payload["ok"] is True
    assert fake.feeds == [FEED]
    assert fake.closed is False


def test_submit_sitemap_gsc_is_write_disabled(tmp_path: Path) -> None:
    service = SeoService(_config(tmp_path))

    payload = service.submit_sitemap(
        provider="gsc", property="sc-domain:example.com", sitemap_url=FEED
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == WRITE_DISABLED
    assert "not wired" in payload["error"]["message"]


def test_submit_sitemap_bing_delete_is_write_disabled(tmp_path: Path) -> None:
    service = SeoService(
        _config(tmp_path), clients={"bing": FakeBingClient(api_key="k", site_url=SITE)}
    )

    payload = service.submit_sitemap(provider="bing", action="delete")

    assert payload["ok"] is False
    assert payload["error"]["code"] == WRITE_DISABLED
    assert "delete" in payload["error"]["message"]


def test_submit_sitemap_rejects_an_unknown_provider(tmp_path: Path) -> None:
    service = SeoService(_config(tmp_path))

    payload = service.submit_sitemap(provider="yandex", property=SITE, sitemap_url=FEED)

    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT
