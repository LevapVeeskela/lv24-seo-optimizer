"""thin-content: slug-like article pages with fewer than 200 words."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "thin-content"
_SEVERITY: Severity = "medium"
_MAX_WORDS = 200
_MAX_INTERNAL_LINKS = 20


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _html_pages(ctx: RuleContext) -> list[PageData]:
    return [
        page
        for page in ctx.pages
        if page.status == 200 and page.content_type.startswith("text/html")
    ]


def _has_slug_tail(page: PageData) -> bool:
    """True for article-like URLs (``/blog/post-name``, ``/news/2026``)."""
    path = urlparse(page.final_url or page.url).path
    if path in ("", "/") or path.endswith("/"):
        return False
    tail = path.rsplit("/", 1)[-1]
    return "-" in tail or any(char.isdigit() for char in tail)


def _internal_link_count(page: PageData, site_host: str) -> int:
    base = page.final_url or page.url
    internal: set[str] = set()
    for link in page.links:
        parsed = urlparse(urljoin(base, link.strip()))
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() in ("http", "https") and host == site_host:
            internal.add(parsed._replace(fragment="").geturl())
    return len(internal)


def _check(ctx: RuleContext) -> list[Issue]:
    site_host = (urlparse(ctx.site_url).hostname or "").lower()
    issues: list[Issue] = []
    for page in _html_pages(ctx):
        if page.word_count >= _MAX_WORDS:
            continue
        if not _has_slug_tail(page):
            continue
        # Index/listing pages legitimately carry little prose; their many links give them away.
        if _internal_link_count(page, site_host) > _MAX_INTERNAL_LINKS:
            continue
        issues.append(_issue(page.url, {"word_count": page.word_count}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Thin content",
    description=(
        "Article-like pages under 200 words rarely satisfy search intent; expand the text "
        "or consolidate the page."
    ),
    check=_check,
)
