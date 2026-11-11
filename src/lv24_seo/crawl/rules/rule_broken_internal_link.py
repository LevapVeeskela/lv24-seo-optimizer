"""broken-internal-link: internal links with a confirmed non-200 target.

Only targets that were actually fetched and answered 4xx/5xx are reported.
A link whose target was never visited — outside the crawl budget, for example —
is *unknown*, not broken, and stays out of the findings; the graph's
``unvisited`` list names those candidates. Inconclusive fetches (timeouts,
blocks) land in the graph's ``failed`` list, likewise not reported here.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

from lv24_seo.crawl.graph import normalize_url
from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "broken-internal-link"
_SEVERITY: Severity = "high"
_MAX_ISSUES_PER_PAGE = 50


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


def _resolve(base: str, link: str) -> str | None:
    """Absolute, normalized http(s) target for a raw href, or None for non-web links."""
    absolute = urljoin(base, link.strip())
    parsed = urlparse(absolute)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    return normalize_url(absolute)


def _check(ctx: RuleContext) -> list[Issue]:
    site_host = (urlparse(ctx.site_url).hostname or "").lower()

    healthy: set[str] = set()
    confirmed: set[str] = set()
    for page in ctx.pages:
        for candidate in (page.url, page.final_url):
            if not candidate:
                continue
            normalized = normalize_url(candidate)
            if page.status == 200 and page.error is None:
                healthy.add(normalized)
            elif page.status >= 400:
                confirmed.add(normalized)

    issues: list[Issue] = []
    for page in _html_pages(ctx):
        base = page.final_url or page.url
        seen: set[str] = set()
        for link in page.links:
            if len(seen) >= _MAX_ISSUES_PER_PAGE:
                break
            target = _resolve(base, link)
            if target is None or target in seen:
                continue
            if (urlparse(target).hostname or "").lower() != site_host:
                continue
            if target in healthy:
                continue
            if target not in confirmed:
                # Never visited (budget cap, external frontier) or inconclusive
                # fetch: unknown, not broken.
                continue
            # ``seen`` also deduplicates broken targets, so it is the per-page issue counter.
            seen.add(target)
            issues.append(_issue(page.url, {"target": target, "reason": "status"}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Broken internal link",
    description=(
        "Internal links pointing to confirmed 4xx/5xx URLs waste crawl budget and "
        "strand users; fix or remove them."
    ),
    check=_check,
)
