"""Offline tests for the crawler fetcher and robots.txt policy (no network)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.crawl.fetcher import (
    DEFAULT_USER_AGENT,
    Fetcher,
    RobotsPolicy,
    is_allowed,
    parse_robots,
)

Handler = Callable[[httpx.Request], httpx.Response]

ROBOTS = """\
# global rules
User-agent: *
Disallow: /global
Allow: /global/public
Crawl-delay: 10

User-agent: LV24-SEO
Disallow: /specific
Sitemap: https://example.com/sitemap.xml # inline comment
"""


def _fetcher(
    handler: Handler,
    *,
    max_redirects: int = 5,
    max_bytes: int = 5_000_000,
    resolver: Callable[[str], list[str]] | None = None,
) -> Fetcher:
    # A fixed public IP keeps MockTransport tests hermetic (no real DNS);
    # per-test resolvers cover the private/rebinding cases.
    return Fetcher(
        transport=httpx.MockTransport(handler),
        max_redirects=max_redirects,
        max_bytes=max_bytes,
        resolver=resolver or (lambda _host: ["93.184.215.14"]),
    )


def _policy(*, disallow: list[str] | None = None, allow: list[str] | None = None) -> RobotsPolicy:
    return RobotsPolicy(disallow=disallow or [], allow=allow or [], sitemaps=[])


def test_fetch_returns_body_headers_and_elapsed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "X-Robots-Tag": "noindex",
            },
            content=b"<html>ok</html>",
        )

    with _fetcher(handler) as fetcher:
        result = fetcher.fetch("https://example.com/page")

    assert result.status == 200
    assert result.url == "https://example.com/page"
    assert result.final_url == "https://example.com/page"
    assert result.body == b"<html>ok</html>"
    assert result.headers["content-type"] == "text/html; charset=utf-8"
    assert result.headers["x-robots-tag"] == "noindex"
    assert result.redirect_chain == ["https://example.com/page"]
    assert result.error is None
    assert result.elapsed_ms >= 0.0
    assert seen[0].method == "GET"
    assert seen[0].headers["user-agent"] == DEFAULT_USER_AGENT


def test_single_redirect_is_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(301, headers={"Location": "/final"})
        return httpx.Response(200, content=b"ok")

    with _fetcher(handler) as fetcher:
        result = fetcher.fetch("https://example.com/start")

    assert result.status == 200
    assert result.final_url == "https://example.com/final"
    assert result.redirect_chain == ["https://example.com/start", "https://example.com/final"]
    assert result.error is None


def test_relative_location_is_resolved_against_the_current_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dir/start":
            return httpx.Response(302, headers={"Location": "next"})
        return httpx.Response(200, content=b"ok")

    with _fetcher(handler) as fetcher:
        result = fetcher.fetch("https://example.com/dir/start")

    assert result.final_url == "https://example.com/dir/next"
    assert result.redirect_chain == [
        "https://example.com/dir/start",
        "https://example.com/dir/next",
    ]


def test_five_redirects_then_loop_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path.removeprefix("/r"))
        return httpx.Response(302, headers={"Location": f"/r{(index + 1) % 6}"})

    with _fetcher(handler, max_redirects=5) as fetcher:
        result = fetcher.fetch("https://example.com/r0")

    assert result.status == 0
    assert result.error == "redirect_loop"
    assert result.redirect_chain == [f"https://example.com/r{i}" for i in range(6)]


def test_redirect_budget_is_enforced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path.removeprefix("/p"))
        return httpx.Response(302, headers={"Location": f"/p{index + 1}"})

    with _fetcher(handler, max_redirects=2) as fetcher:
        result = fetcher.fetch("https://example.com/p0")

    assert result.status == 0
    assert result.error == "too_many_redirects"
    assert result.redirect_chain == [
        "https://example.com/p0",
        "https://example.com/p1",
        "https://example.com/p2",
    ]


def test_chain_at_the_redirect_budget_succeeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path.removeprefix("/r"))
        if index == 5:
            return httpx.Response(200, content=b"done")
        return httpx.Response(302, headers={"Location": f"/r{index + 1}"})

    with _fetcher(handler, max_redirects=5) as fetcher:
        result = fetcher.fetch("https://example.com/r0")

    assert result.status == 200
    assert result.body == b"done"
    assert len(result.redirect_chain) == 6


def test_body_over_max_bytes_returns_too_large() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 11)

    with _fetcher(handler, max_bytes=10) as fetcher:
        result = fetcher.fetch("https://example.com/big")

    assert result.status == 0
    assert result.error == "too_large"
    assert result.body == b""
    assert result.final_url == "https://example.com/big"


def test_timeout_is_returned_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow response")

    with _fetcher(handler) as fetcher:
        result = fetcher.fetch("https://example.com/slow")

    assert result.status == 0
    assert result.error == "timeout"
    assert result.final_url == "https://example.com/slow"
    assert result.redirect_chain == ["https://example.com/slow"]


def test_connect_error_is_returned_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with _fetcher(handler) as fetcher:
        result = fetcher.fetch("https://example.com/down")

    assert result.status == 0
    assert result.error == "connect_error"


def test_invalid_url_is_rejected_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called for an invalid URL")

    with _fetcher(handler) as fetcher:
        scheme = fetcher.fetch("ftp://example.com/file")
        relative = fetcher.fetch("not a url")

    assert scheme.error == "invalid_url"
    assert scheme.status == 0
    assert relative.error == "invalid_url"


def test_parse_robots_prefers_the_specific_agent_group() -> None:
    policy = parse_robots(ROBOTS)

    assert policy.disallow == ["/specific"]
    assert policy.allow == []
    assert policy.sitemaps == ["https://example.com/sitemap.xml"]
    assert policy.crawl_delay is None


def test_parse_robots_falls_back_to_the_wildcard_group() -> None:
    policy = parse_robots(ROBOTS, user_agent="other-bot/1.0")

    assert policy.disallow == ["/global"]
    assert policy.allow == ["/global/public"]
    assert policy.crawl_delay == 10.0
    assert policy.sitemaps == ["https://example.com/sitemap.xml"]
    assert is_allowed(policy, "/global/public/page") is True
    assert is_allowed(policy, "/global/private") is False


def test_parse_robots_uses_the_longest_matching_agent_token() -> None:
    text = """\
User-agent: lv24
Disallow: /short

User-agent: lv24-seo
Disallow: /long
"""

    policy = parse_robots(text)

    assert policy.disallow == ["/long"]


def test_parse_robots_shares_rules_between_consecutive_agent_lines() -> None:
    text = """\
User-agent: other-bot
User-agent: lv24-seo
Disallow: /shared
"""

    assert parse_robots(text).disallow == ["/shared"]


def test_parse_robots_ignores_empty_disallow() -> None:
    policy = parse_robots("User-agent: *\nDisallow:\n")

    assert policy.disallow == []
    assert is_allowed(policy, "/anything") is True


def test_disallow_slash_blocks_the_whole_site() -> None:
    policy = parse_robots("User-agent: *\nDisallow: /\n")

    assert is_allowed(policy, "/") is False
    assert is_allowed(policy, "/any/page") is False


def test_longest_matching_rule_wins() -> None:
    policy = _policy(disallow=["/private"], allow=["/private/public"])

    assert is_allowed(policy, "/private") is False
    assert is_allowed(policy, "/private/report") is False
    assert is_allowed(policy, "/private/public/report") is True
    assert is_allowed(policy, "/open") is True


def test_allow_wins_a_tie() -> None:
    policy = _policy(disallow=["/same"], allow=["/same"])

    assert is_allowed(policy, "/same") is True


def test_wildcard_and_end_anchor_rules() -> None:
    policy = _policy(disallow=["/*.pdf$"], allow=["/public/*.pdf$"])

    assert is_allowed(policy, "/files/report.pdf") is False
    assert is_allowed(policy, "https://example.com/files/report.pdf") is False
    assert is_allowed(policy, "/files/report.pdf?download=1") is True
    assert is_allowed(policy, "/files/report.pdf.html") is True
    assert is_allowed(policy, "/public/report.pdf") is True


def test_prefix_wildcard_blocks_the_whole_subtree() -> None:
    policy = _policy(disallow=["/search*"])

    assert is_allowed(policy, "/search") is False
    assert is_allowed(policy, "/search?q=1") is False
    assert is_allowed(policy, "/about") is True


def test_fetch_robots_404_allows_everything() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    with _fetcher(handler) as fetcher:
        policy = fetcher.fetch_robots("https://example.com/")

    assert str(seen[0].url) == "https://example.com/robots.txt"
    assert policy == RobotsPolicy(disallow=[], allow=[], sitemaps=[])
    assert is_allowed(policy, "/any/path") is True


def test_fetch_robots_parses_a_200_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text=ROBOTS)

    with _fetcher(handler) as fetcher:
        policy = fetcher.fetch_robots("https://example.com")

    assert policy.disallow == ["/specific"]
    assert policy.sitemaps == ["https://example.com/sitemap.xml"]


def test_fetch_robots_network_error_defers_crawl() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with _fetcher(handler) as fetcher:
        policy = fetcher.fetch_robots("https://example.com")

    assert policy.deferred is True
    assert is_allowed(policy, "/any/path") is False


def test_fetch_robots_server_error_defers_crawl() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _fetcher(handler) as fetcher:
        policy = fetcher.fetch_robots("https://example.com")

    assert policy.deferred is True
    assert is_allowed(policy, "/any/path") is False


@pytest.mark.parametrize(
    "kwargs",
    [{"timeout": 0.0}, {"max_redirects": -1}, {"max_bytes": 0}],
)
def test_invalid_limits_raise_invalid_input(kwargs: dict[str, float]) -> None:
    with pytest.raises(SeoError) as excinfo:
        Fetcher(**kwargs)

    assert excinfo.value.code == INVALID_INPUT


def test_default_transport_pins_ipv4() -> None:
    fetcher = Fetcher()
    try:
        transport = fetcher._client._transport
        assert isinstance(transport, httpx.HTTPTransport)
        assert transport._pool._local_address == "0.0.0.0"
    finally:
        fetcher.close()


def test_injected_transport_is_used_and_close_closes_the_client() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))

    fetcher = Fetcher(transport=transport)
    try:
        assert fetcher._client._transport is transport
        assert fetcher.fetch("https://example.com/").status == 200
    finally:
        fetcher.close()

    assert fetcher._client.is_closed is True
