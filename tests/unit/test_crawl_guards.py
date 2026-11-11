"""Regression tests for the crawl guard fixes (F10-F12, F18, F20 + base href).

All network is faked: ``httpx.MockTransport`` for the real ``Fetcher`` and
in-memory fakes for service-level walks. No real DNS is touched — tests pass
explicit resolvers.
"""

from __future__ import annotations

import httpx

from lv24_seo.crawl.fetcher import Fetcher, RobotsPolicy, is_allowed, parse_robots
from lv24_seo.crawl.graph import build_graph
from lv24_seo.crawl.html import parse_html
from lv24_seo.crawl.models import PageData, RuleContext


def _html_fetcher(
    handler: object,
    *,
    resolver: object | None = None,
    allow_private: bool = False,
) -> Fetcher:
    return Fetcher(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        resolver=resolver or (lambda _host: ["93.184.215.14"]),  # type: ignore[arg-type]
        allow_private=allow_private,
    )


def test_base_href_resolves_relative_links() -> None:
    body = (
        '<html><head><base href="/sub/"></head>'
        '<body><a href="x">X</a><a href="/abs">A</a></body></html>'
    )
    page = parse_html("https://example.com/dir/p", body)
    assert "https://example.com/sub/x" in page.links
    assert "https://example.com/abs" in page.links


def test_base_href_resolves_canonical() -> None:
    body = (
        '<html><head><base href="https://cdn.example.com/s/">'
        '<link rel="canonical" href="page"></head><body></body></html>'
    )
    page = parse_html("https://example.com/p", body)
    assert page.canonical == "https://cdn.example.com/s/page"


def test_outbound_policy_blocks_private_seed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request must leave for a private seed")

    fetcher = _html_fetcher(handler, resolver=lambda _h: ["127.0.0.1"])
    try:
        result = fetcher.fetch("https://example.com/private")
    finally:
        fetcher.close()
    assert result.error == "blocked_private"
    assert result.status == 0


def test_outbound_policy_blocks_private_and_metadata_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(301, headers={"location": "http://169.254.169.254/x"})
        raise AssertionError(f"must not request {request.url}")

    def resolver(host: str) -> list[str]:
        return {"example.com": ["93.184.215.14"], "169.254.169.254": ["169.254.169.254"]}[host]

    fetcher = _html_fetcher(handler, resolver=resolver)
    try:
        result = fetcher.fetch("https://example.com/")
    finally:
        fetcher.close()
    assert result.error == "blocked_private"


def test_outbound_policy_blocks_dns_rebinding_mid_chain() -> None:
    seen_hosts = {"example.com": ["93.184.215.14"], "cdn.example.com": ["10.0.0.5"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/x"})
        raise AssertionError(f"must not request {request.url}")

    fetcher = _html_fetcher(handler, resolver=lambda h: seen_hosts[h])
    try:
        result = fetcher.fetch("https://example.com/")
    finally:
        fetcher.close()
    assert result.error == "blocked_private"


def test_outbound_policy_allows_private_with_explicit_opt_in() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    fetcher = _html_fetcher(handler, resolver=lambda _h: ["127.0.0.1"], allow_private=True)
    try:
        result = fetcher.fetch("http://localhost:8000/")
    finally:
        fetcher.close()
    assert result.status == 200


def test_outbound_policy_allows_legitimate_public_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(301, headers={"location": "https://www.example.com/"})
        if request.url.host == "www.example.com":
            return httpx.Response(200, text="home")
        raise AssertionError(f"unexpected {request.url}")

    def resolver(host: str) -> list[str]:
        return {"example.com": ["93.184.215.14"], "www.example.com": ["93.184.215.15"]}[host]

    fetcher = _html_fetcher(handler, resolver=resolver)
    try:
        result = fetcher.fetch("https://example.com/")
    finally:
        fetcher.close()
    assert result.status == 200
    assert result.final_url == "https://www.example.com/"


def test_https_downgrade_is_flagged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(301, headers={"location": "http://example.com/"})
        return httpx.Response(200, text="home")

    fetcher = _html_fetcher(handler)
    try:
        result = fetcher.fetch("https://example.com/")
    finally:
        fetcher.close()
    assert result.status == 200
    assert result.downgraded is True


def test_robots_unreachable_defers_instead_of_fail_open() -> None:
    policy = RobotsPolicy(disallow=["/"], allow=[], sitemaps=[], deferred=True)
    assert policy.deferred is True
    assert is_allowed(policy, "/") is False
    assert is_allowed(policy, "/public") is False


def test_robots_longest_match_allow_wins_tie() -> None:
    policy = parse_robots("User-agent: *\nDisallow: /private/\nAllow: /private/public\n")
    assert is_allowed(policy, "/private/secret") is False
    assert is_allowed(policy, "/private/public/page") is True


def test_canonical_self_reference_after_redirect_is_clean() -> None:
    from lv24_seo.crawl.rules.rule_canonical_mismatch import RULE

    page = PageData(
        url="https://example.com/old",
        status=200,
        final_url="https://example.com/new",
        canonical="https://example.com/new",
    )
    ctx = RuleContext(pages=[page], site_url="https://example.com")
    assert RULE.run(ctx) == []


def test_robots_check_covers_final_url_after_redirect() -> None:
    from lv24_seo.crawl.rules.rule_robots_blocked import RULE

    page = PageData(
        url="https://example.com/a",
        status=200,
        final_url="https://example.com/admin/panel",
    )
    ctx = RuleContext(
        pages=[page],
        site_url="https://example.com",
        robots_disallow=["/admin/"],
    )
    issues = RULE.run(ctx)
    assert [issue.url for issue in issues] == ["https://example.com/a"]


def test_graph_reports_confirmed_broken_unvisited_and_failed() -> None:
    def page(url: str, **kwargs: object) -> PageData:
        params: dict[str, object] = {"url": url, "status": 200, "final_url": url}
        params.update(kwargs)
        return PageData(**params)  # type: ignore[arg-type]

    pages = [
        page("https://example.com/", links=["https://example.com/gone", "https://example.com/new"]),
        page("https://example.com/gone", status=404),
    ]
    stats = build_graph(pages, site_host="example.com", root_url="https://example.com/")
    assert stats.broken == ["https://example.com/gone"]
    assert stats.unvisited == ["https://example.com/new"]
    assert stats.failed == []
