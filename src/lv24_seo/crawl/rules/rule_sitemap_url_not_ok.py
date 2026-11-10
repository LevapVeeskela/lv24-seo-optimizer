"""sitemap-url-not-ok: an advertised sitemap URL is missing from the crawl or is not 200."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "sitemap-url-not-ok"
_SEVERITY: Severity = "high"
_MAX_ISSUES = 100


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _norm(url: str) -> str:
    """Strip the fragment, lowercase the host and drop a trailing slash."""
    parsed = urlsplit(url.strip())
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def _index(pages: list[PageData]) -> dict[str, list[PageData]]:
    by_url: dict[str, list[PageData]] = {}
    for page in pages:
        by_url.setdefault(_norm(page.url), []).append(page)
    return by_url


def _check(ctx: RuleContext) -> list[Issue]:
    by_url = _index(ctx.pages)
    issues: list[Issue] = []
    seen: set[str] = set()
    for sitemap_url in ctx.sitemap_urls:
        key = _norm(sitemap_url)
        if key in seen:
            continue
        seen.add(key)
        candidates = by_url.get(key, [])
        if any(page.status == 200 for page in candidates):
            continue
        if candidates:
            issues.append(_issue(sitemap_url, {"reason": "status", "status": candidates[0].status}))
        else:
            issues.append(_issue(sitemap_url, {"reason": "missing", "status": None}))
    if len(issues) > _MAX_ISSUES:
        issues = issues[:_MAX_ISSUES]
        issues[-1].detail["truncated"] = True
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Sitemap URL is not a live 200",
    description=(
        "A URL listed in the sitemap was not crawled or did not answer 200; every sitemap entry "
        "must resolve to a canonical, indexable 200 page."
    ),
    check=_check,
)
