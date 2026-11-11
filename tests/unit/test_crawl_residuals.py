"""Offline regressions for connection pinning and bounded partial crawls."""

import httpx
import pytest

from lv24_seo.crawl.fetcher import Fetcher
from lv24_seo.crawl.graph import build_graph
from lv24_seo.crawl.models import FetchResult, PageData, RuleContext
from lv24_seo.crawl.rules.rule_sitemap_url_not_ok import RULE as SITEMAP_STATUS
from lv24_seo.crawl.sitemap import SitemapReader


def test_default_transport_connects_to_approved_ip_with_original_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    resolutions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    def resolve(host: str) -> list[str]:
        resolutions.append(host)
        # A second name lookup must never happen during socket connection setup.
        return ["93.184.215.14"] if len(resolutions) == 1 else ["127.0.0.1"]

    def transport(**_kwargs: object) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    with Fetcher(resolver=resolve) as fetcher:
        result = fetcher.fetch("https://example.com:8443/path?q=x")
    assert result.status == 200
    assert result.final_url == "https://example.com:8443/path?q=x"
    assert seen[0].url == "https://93.184.215.14:8443/path?q=x"
    assert seen[0].headers["Host"] == "example.com:8443"
    assert seen[0].extensions["sni_hostname"] == "example.com"
    assert resolutions == ["example.com"]


def test_redirect_policy_refuses_disallowed_hop_before_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/private"})

    with Fetcher(
        transport=httpx.MockTransport(handler), resolver=lambda _host: ["93.184.215.14"]
    ) as fetcher:
        result = fetcher.fetch(
            "https://example.com/", allow_url=lambda url: not url.endswith("/private")
        )
    assert result.error == "robots_blocked"
    assert result.final_url == "https://example.com/private"
    assert seen == ["/"]


def test_crawl_delay_applies_between_redirect_hops_and_after_failures() -> None:
    pauses: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"Location": "/end"})
        raise httpx.ConnectError("offline", request=request)

    with Fetcher(
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: ["93.184.215.14"],
        sleep=pauses.append,
    ) as fetcher:
        fetcher.politeness_delay = 5
        assert fetcher.fetch("https://example.com/").error == "connect_error"
        assert fetcher.fetch("https://example.com/end").error == "connect_error"
    assert len(pauses) == 2
    assert all(0 < pause <= 5 for pause in pauses)


def test_sitemap_document_budget_counts_failed_requests() -> None:
    calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        calls.append(url)
        if url.endswith("root.xml"):
            children = "".join(
                f"<sitemap><loc>https://example.com/{i}.xml</loc></sitemap>" for i in range(10)
            )
            return FetchResult(
                url=url,
                status=200,
                final_url=url,
                body=f"<sitemapindex>{children}</sitemapindex>".encode(),
            )
        return FetchResult(url=url, status=404, final_url=url)

    doc = SitemapReader(fetch, max_docs=3).read("https://example.com/root.xml")
    assert len(calls) == 3
    assert doc.truncated
    assert "sitemap_docs_cap" in doc.errors


def test_shared_address_space_is_not_public() -> None:
    with Fetcher(
        transport=httpx.MockTransport(lambda _request: pytest.fail("request escaped")),
        resolver=lambda _host: ["100.64.0.1"],
    ) as fetcher:
        assert fetcher.fetch("http://example.com/").error == "blocked_private"


def test_https_downgrade_is_recorded_even_if_final_hop_returns_to_https() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"Location": "http://example.com/insecure"})
        if request.url.path == "/insecure":
            return httpx.Response(302, headers={"Location": "https://example.com/end"})
        return httpx.Response(200, text="ok")

    with Fetcher(
        transport=httpx.MockTransport(handler), resolver=lambda _host: ["93.184.215.14"]
    ) as fetcher:
        result = fetcher.fetch("https://example.com/")
    assert result.status == 200
    assert result.downgraded


def test_redirect_alias_is_one_connected_graph_node() -> None:
    pages = [
        PageData(
            url="https://example.com/",
            final_url="https://example.com/",
            status=200,
            links=["https://example.com/old"],
        ),
        PageData(
            url="https://example.com/old",
            final_url="https://example.com/new",
            status=200,
            links=["https://example.com/"],
        ),
    ]
    graph = build_graph(pages, site_host="example.com", root_url="https://example.com/")
    assert graph.nodes == 2
    assert graph.depths == {"https://example.com/": 0, "https://example.com/new": 1}
    assert graph.orphans == []
    assert graph.broken == graph.unvisited == graph.failed == []


def test_confirmed_broken_evidence_survives_a_retry_timeout() -> None:
    target = "https://example.com/gone"
    pages = [
        PageData(
            url="https://example.com/", final_url="https://example.com/", status=200, links=[target]
        ),
        PageData(url=target, final_url=target, status=404),
        PageData(url=target, final_url=target, status=0, error="timeout"),
    ]
    assert build_graph(pages, site_host="example.com").broken == [target]


def test_sitemap_unvisited_or_failed_is_not_confirmed_non_200() -> None:
    page = PageData(
        url="https://example.com/timeout",
        final_url="https://example.com/timeout",
        status=0,
        error="timeout",
    )
    ctx = RuleContext(
        pages=[page],
        site_url="https://example.com/",
        sitemap_urls=[page.url, "https://example.com/unvisited"],
    )
    assert SITEMAP_STATUS.run(ctx) == []
