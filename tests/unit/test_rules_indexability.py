"""Unit tests for Wave 3 rule group A: indexability and status rules."""

from __future__ import annotations

from lv24_seo.crawl.models import PageData, RuleContext
from lv24_seo.crawl.rules.rule_canonical_mismatch import RULE as CANONICAL_MISMATCH
from lv24_seo.crawl.rules.rule_canonical_missing import RULE as CANONICAL_MISSING
from lv24_seo.crawl.rules.rule_client_error import RULE as CLIENT_ERROR
from lv24_seo.crawl.rules.rule_noindex_on_200 import RULE as NOINDEX_ON_200
from lv24_seo.crawl.rules.rule_redirect_chain import RULE as REDIRECT_CHAIN
from lv24_seo.crawl.rules.rule_robots_blocked import RULE as ROBOTS_BLOCKED
from lv24_seo.crawl.rules.rule_server_error import RULE as SERVER_ERROR
from lv24_seo.crawl.rules.rule_sitemap_url_noindex import RULE as SITEMAP_URL_NOINDEX
from lv24_seo.crawl.rules.rule_sitemap_url_not_ok import RULE as SITEMAP_URL_NOT_OK
from lv24_seo.crawl.rules.rule_soft_404 import RULE as SOFT_404


def _page(
    url: str,
    status: int = 200,
    *,
    final_url: str | None = None,
    content_type: str = "text/html",
    title: str | None = None,
    meta_robots: str | None = None,
    canonical: str | None = None,
    word_count: int = 100,
    redirect_chain: list[str] | None = None,
) -> PageData:
    return PageData(
        url=url,
        status=status,
        final_url=url if final_url is None else final_url,
        content_type=content_type,
        title=title,
        meta_robots=meta_robots,
        canonical=canonical,
        word_count=word_count,
        redirect_chain=list(redirect_chain or []),
    )


def _ctx(
    *pages: PageData,
    sitemap_urls: list[str] | None = None,
    robots_disallow: list[str] | None = None,
) -> RuleContext:
    return RuleContext(
        pages=list(pages),
        site_url="https://example.com",
        sitemap_urls=list(sitemap_urls or []),
        robots_disallow=list(robots_disallow or []),
    )


def test_noindex_on_200_reports_live_noindex_page() -> None:
    page = _page("https://example.com/private", meta_robots="NoIndex, Follow")
    issues = NOINDEX_ON_200.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].rule_id == "noindex-on-200"
    assert issues[0].severity == "critical"
    assert issues[0].url == "https://example.com/private"
    assert issues[0].detail["directives"] == ["follow", "noindex"]


def test_noindex_on_200_ignores_indexable_and_error_pages() -> None:
    gone = _page("https://example.com/gone", status=410, meta_robots="noindex")

    assert NOINDEX_ON_200.run(_ctx(_page("https://example.com/a"))) == []
    assert NOINDEX_ON_200.run(_ctx(gone)) == []


def test_robots_blocked_matches_prefix_and_wildcard_patterns() -> None:
    pages = [
        _page("https://example.com/private/secret?token=1"),
        _page("https://example.com/docs/guide.pdf"),
    ]
    issues = ROBOTS_BLOCKED.run(
        _ctx(*pages, robots_disallow=["/private/", "/*.pdf$"]),
    )

    assert [issue.url for issue in issues] == [
        "https://example.com/private/secret?token=1",
        "https://example.com/docs/guide.pdf",
    ]
    assert [issue.detail["pattern"] for issue in issues] == ["/private/", "/*.pdf$"]
    assert all(issue.rule_id == "robots-blocked" for issue in issues)
    assert all(issue.severity == "critical" for issue in issues)


def test_robots_blocked_ignores_allowed_paths_and_non_200_pages() -> None:
    ctx = _ctx(
        _page("https://example.com/public/page"),
        _page("https://example.com/private/gone", status=404),
        robots_disallow=["/private/"],
    )

    assert ROBOTS_BLOCKED.run(ctx) == []


def test_canonical_mismatch_reports_foreign_canonical() -> None:
    page = _page("https://example.com/page", canonical="https://example.com/other")
    issues = CANONICAL_MISMATCH.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].rule_id == "canonical-mismatch"
    assert issues[0].severity == "high"
    assert issues[0].url == "https://example.com/page"
    assert issues[0].detail == {"canonical": "https://example.com/other", "url": page.url}


def test_canonical_mismatch_accepts_normalised_and_relative_self_canonicals() -> None:
    trailing_slash = _page("https://example.com/page", canonical="https://example.com/page/")
    upper_host = _page("https://example.com/page", canonical="https://EXAMPLE.COM/page")
    relative = _page("https://example.com/page", canonical="/page")

    assert CANONICAL_MISMATCH.run(_ctx(trailing_slash, upper_host, relative)) == []
    assert CANONICAL_MISMATCH.run(_ctx(_page("https://example.com/page"))) == []


def test_canonical_missing_reports_html_page_without_canonical() -> None:
    issues = CANONICAL_MISSING.run(_ctx(_page("https://example.com/no-canonical")))

    assert len(issues) == 1
    assert issues[0].rule_id == "canonical-missing"
    assert issues[0].severity == "medium"
    assert issues[0].url == "https://example.com/no-canonical"


def test_canonical_missing_ignores_pages_with_canonical_and_non_html() -> None:
    with_canonical = _page("https://example.com/ok", canonical="https://example.com/ok")
    pdf = _page("https://example.com/file.pdf", content_type="application/pdf")

    assert CANONICAL_MISSING.run(_ctx(with_canonical)) == []
    assert CANONICAL_MISSING.run(_ctx(pdf)) == []

    blank = _page("https://example.com/empty", canonical="  ")
    blank_issues = CANONICAL_MISSING.run(_ctx(blank))
    assert [issue.url for issue in blank_issues] == ["https://example.com/empty"]


def test_server_error_reports_5xx() -> None:
    issues = SERVER_ERROR.run(
        _ctx(
            _page("https://example.com/broken", status=503),
            _page("https://example.com/edge", status=599),
            _page("https://example.com/ok"),
        )
    )

    assert [issue.url for issue in issues] == [
        "https://example.com/broken",
        "https://example.com/edge",
    ]
    assert [issue.detail["status"] for issue in issues] == [503, 599]
    assert all(issue.rule_id == "server-error" for issue in issues)
    assert all(issue.severity == "critical" for issue in issues)


def test_server_error_ignores_4xx_and_200() -> None:
    ctx = _ctx(
        _page("https://example.com/a", status=404),
        _page("https://example.com/b", status=200),
    )

    assert SERVER_ERROR.run(ctx) == []


def test_client_error_reports_unexpected_4xx() -> None:
    issues = CLIENT_ERROR.run(
        _ctx(
            _page("https://example.com/forbidden", status=403),
            _page("https://example.com/bad-request", status=400),
        )
    )

    assert [issue.url for issue in issues] == [
        "https://example.com/forbidden",
        "https://example.com/bad-request",
    ]
    assert [issue.detail["status"] for issue in issues] == [403, 400]
    assert all(issue.rule_id == "client-error" for issue in issues)
    assert all(issue.severity == "high" for issue in issues)


def test_client_error_ignores_gone_answers_and_success() -> None:
    ctx = _ctx(
        _page("https://example.com/404", status=404),
        _page("https://example.com/410", status=410),
        _page("https://example.com/429", status=429),
        _page("https://example.com/ok"),
    )

    assert CLIENT_ERROR.run(ctx) == []


def test_soft_404_reports_title_and_thin_reasons() -> None:
    pages = [
        _page("https://example.com/missing-ru", title="Страница не найдена"),
        _page("https://example.com/missing-en", title="Page Not Found"),
        _page("https://example.com/stub", title="Promo", word_count=3),
    ]
    issues = SOFT_404.run(_ctx(*pages))

    assert [issue.url for issue in issues] == [
        "https://example.com/missing-ru",
        "https://example.com/missing-en",
        "https://example.com/stub",
    ]
    assert [issue.detail["reason"] for issue in issues] == ["title", "title", "thin"]
    assert all(issue.rule_id == "soft-404" for issue in issues)
    assert all(issue.severity == "high" for issue in issues)


def test_soft_404_ignores_root_and_content_pages() -> None:
    ctx = _ctx(
        _page("https://example.com/", title="Home", word_count=0),
        _page("https://example.com/rich", title="About us", word_count=400),
        _page("https://example.com/gone", status=404, title="Not Found"),
    )

    assert SOFT_404.run(ctx) == []


def test_redirect_chain_reports_multi_hop_and_off_target_redirects() -> None:
    pages = [
        _page(
            "https://example.com/a",
            final_url="https://example.com/d",
            redirect_chain=[
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
        ),
        _page(
            "https://example.com/e",
            final_url="https://example.com/f",
            redirect_chain=["https://example.com/e"],
        ),
    ]
    issues = REDIRECT_CHAIN.run(_ctx(*pages))

    assert [issue.url for issue in issues] == ["https://example.com/a", "https://example.com/e"]
    assert len(issues[0].detail["chain"]) == 3
    assert issues[1].detail["chain"] == ["https://example.com/e"]
    assert all(issue.rule_id == "redirect-chain" for issue in issues)
    assert all(issue.severity == "high" for issue in issues)


def test_redirect_chain_ignores_direct_200_pages() -> None:
    ctx = _ctx(
        _page("https://example.com/direct"),
        _page("https://example.com/self", redirect_chain=["https://example.com/self"]),
    )

    assert REDIRECT_CHAIN.run(ctx) == []


def test_sitemap_url_not_ok_reports_missing_and_non_200_urls() -> None:
    ctx = _ctx(
        _page("https://example.com/ok"),
        _page("https://example.com/err", status=500),
        sitemap_urls=[
            "https://example.com/ok",
            "https://example.com/missing",
            "https://example.com/err",
        ],
    )
    issues = SITEMAP_URL_NOT_OK.run(ctx)

    assert [issue.url for issue in issues] == [
        "https://example.com/missing",
        "https://example.com/err",
    ]
    assert issues[0].detail == {"reason": "missing", "status": None}
    assert issues[1].detail == {"reason": "status", "status": 500}
    assert all(issue.rule_id == "sitemap-url-not-ok" for issue in issues)
    assert all(issue.severity == "high" for issue in issues)


def test_sitemap_url_not_ok_accepts_all_live_urls_and_trailing_slash_variants() -> None:
    ctx = _ctx(
        _page("https://example.com/one"),
        _page("https://example.com/two"),
        sitemap_urls=["https://example.com/one/", "HTTPS://EXAMPLE.COM/two"],
    )

    assert SITEMAP_URL_NOT_OK.run(ctx) == []


def test_sitemap_url_not_ok_caps_issues_at_100_and_flags_truncation() -> None:
    sitemap_urls = [f"https://example.com/page-{index}" for index in range(150)]
    issues = SITEMAP_URL_NOT_OK.run(_ctx(sitemap_urls=sitemap_urls))

    assert len(issues) == 100
    assert issues[0].url == "https://example.com/page-0"
    assert "truncated" not in issues[0].detail
    assert issues[-1].detail["truncated"] is True


def test_sitemap_url_noindex_reports_noindex_and_robots_blocked_urls() -> None:
    pages = [
        _page("https://example.com/hidden", meta_robots="noindex, follow"),
        _page("https://example.com/private/page"),
    ]
    ctx = _ctx(
        *pages,
        sitemap_urls=["https://example.com/hidden", "https://example.com/private/page"],
        robots_disallow=["/private/"],
    )
    issues = SITEMAP_URL_NOINDEX.run(ctx)

    assert [issue.url for issue in issues] == [
        "https://example.com/hidden",
        "https://example.com/private/page",
    ]
    assert issues[0].detail == {"directives": ["follow", "noindex"]}
    assert issues[1].detail == {"directives": [], "pattern": "/private/"}
    assert all(issue.rule_id == "sitemap-url-noindex" for issue in issues)
    assert all(issue.severity == "critical" for issue in issues)


def test_sitemap_url_noindex_reports_one_issue_per_url() -> None:
    page = _page("https://example.com/both", meta_robots="noindex")
    ctx = _ctx(page, sitemap_urls=["https://example.com/both"], robots_disallow=["/both"])

    issues = SITEMAP_URL_NOINDEX.run(ctx)

    assert len(issues) == 1
    assert issues[0].detail["directives"] == ["noindex"]
    assert issues[0].detail["pattern"] == "/both"


def test_sitemap_url_noindex_ignores_indexable_and_uncrawled_urls() -> None:
    ctx = _ctx(
        _page("https://example.com/indexable"),
        sitemap_urls=["https://example.com/indexable", "https://example.com/not-crawled"],
        robots_disallow=["/private/"],
    )

    assert SITEMAP_URL_NOINDEX.run(ctx) == []
