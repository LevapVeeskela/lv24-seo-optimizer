"""sitemap-url-not-ok: a sitemap URL was fetched and returned a non-200 status."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.graph import normalize_url
from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "sitemap-url-not-ok"
_SEVERITY: Severity = "high"
_MAX_ISSUES = 100


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _index(pages: list[PageData]) -> dict[str, list[PageData]]:
    by_url: dict[str, list[PageData]] = {}
    for page in pages:
        for candidate in (page.url, page.final_url):
            if candidate:
                by_url.setdefault(normalize_url(candidate), []).append(page)
    return by_url


def _check(ctx: RuleContext) -> list[Issue]:
    by_url = _index(ctx.pages)
    issues: list[Issue] = []
    seen: set[str] = set()
    for sitemap_url in ctx.sitemap_urls:
        key = normalize_url(sitemap_url)
        if key in seen:
            continue
        seen.add(key)
        candidates = by_url.get(key, [])
        if any(page.status == 200 for page in candidates):
            continue
        confirmed = [page for page in candidates if page.status > 0 and page.error is None]
        if confirmed:
            issues.append(_issue(sitemap_url, {"reason": "status", "status": confirmed[0].status}))
    if len(issues) > _MAX_ISSUES:
        issues = issues[:_MAX_ISSUES]
        issues[-1].detail["truncated"] = True
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Sitemap URL is not a live 200",
    description=(
        "A URL listed in the sitemap was fetched and did not answer 200; every sitemap entry "
        "must resolve to a canonical, indexable 200 page."
    ),
    check=_check,
)
