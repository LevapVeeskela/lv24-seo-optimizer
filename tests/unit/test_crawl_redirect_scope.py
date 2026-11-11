"""Service redirect scope: forbidden targets are never requested.

``Fetcher.fetch`` accepts ``allow_url`` and checks it before the seed request
and before every redirect hop (returning ``robots_blocked`` without issuing the
forbidden request). ``SeoService._walk_site`` wires that callback so that:

* robots rules from the seed origin never apply to another origin;
* a redirect to another origin is conservatively blocked before the request;
* the scope limit is reported in coverage notes;
* ``robots_blocked`` stays a scope fact, never a network error or a confirmed
  broken link.

These tests assert the absence of the forbidden request, not just the final
status.
"""

from __future__ import annotations

import httpx

from lv24_seo.core.config import (
    CrawlConfig,
    ProjectConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.crawl.fetcher import Fetcher
from lv24_seo.services import SeoService

BASE = "https://example.com/"


def _config() -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example", base_url=BASE, default_property="sc-domain:example.com"
        ),
        providers=ProvidersConfig(),
        crawl=CrawlConfig(max_urls=10),
        storage=StorageConfig(data_dir="data"),
    )


def _service() -> SeoService:
    return SeoService(_config(), secrets=SecretStore(env={}))


def test_off_origin_redirect_is_blocked_before_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "example.com" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if request.url.host == "example.com" and request.url.path == "/sitemap.xml":
            return httpx.Response(404, text="no sitemap")
        if request.url.host == "example.com" and request.url.path == "/":
            return httpx.Response(302, headers={"location": "https://evil.com/steal"})
        raise AssertionError(f"forbidden request escaped: {request.url}")

    def resolver(host: str) -> list[str]:
        return {
            "example.com": ["93.184.215.14"],
            "evil.com": ["93.184.215.99"],
        }[host]

    service = _service()
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=resolver,
    )
    try:
        outcome = service._walk_site(fetcher, BASE, 10)
    finally:
        fetcher.close()

    assert "https://example.com/" in requested
    assert all("evil.com" not in url for url in requested)
    assert BASE in outcome.blocked or any(page.error == "robots_blocked" for page in outcome.pages)
    assert outcome.errors == [] or all("robots_blocked" not in item for item in outcome.errors)
    assert any("off-origin" in note for note in outcome.coverage_notes)


def test_same_origin_disallowed_redirect_is_blocked_before_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/sitemap.xml":
            return httpx.Response(404, text="no sitemap")
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "https://example.com/private"})
        raise AssertionError(f"disallowed hop was requested: {request.url}")

    def fake_robots(base_url: str):  # type: ignore[no-untyped-def]
        from lv24_seo.crawl.fetcher import RobotsPolicy

        assert base_url == "https://example.com"
        return RobotsPolicy(disallow=["/private"], allow=[], sitemaps=[])

    service = _service()
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: ["93.184.215.14"],
    )
    fetcher.fetch_robots = fake_robots  # type: ignore[method-assign]
    try:
        outcome = service._walk_site(fetcher, BASE, 10)
    finally:
        fetcher.close()

    assert "/" in requested
    assert "/private" not in requested
    assert any(page.error == "robots_blocked" for page in outcome.pages)
    assert all("robots_blocked" not in item for item in outcome.errors)
