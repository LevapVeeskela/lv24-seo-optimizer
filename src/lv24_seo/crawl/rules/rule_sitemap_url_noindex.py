"""sitemap-url-noindex: an advertised sitemap URL that search engines cannot index."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from lv24_seo.crawl.graph import normalize_url
from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "sitemap-url-noindex"
_SEVERITY: Severity = "critical"


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _target(url: str) -> str:
    """Robots patterns match the path plus query string, never scheme or host."""
    parsed = urlsplit(url)
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _compile(pattern: str) -> re.Pattern[str]:
    """Translate one robots.txt path pattern into a regex (``*`` wildcard, ``$`` end anchor)."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    if not body.startswith("/"):
        body = f"/{body}"
    body = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(body + ("$" if anchored else ""))


def _matchers(patterns: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(pattern.strip(), _compile(pattern.strip())) for pattern in patterns if pattern.strip()]


def _robots_pattern(page: PageData, matchers: list[tuple[str, re.Pattern[str]]]) -> str | None:
    if page.status != 200:
        return None
    target = _target(page.url)
    for pattern, regex in matchers:
        if regex.match(target):
            return pattern
    return None


def _index(pages: list[PageData]) -> dict[str, list[PageData]]:
    by_url: dict[str, list[PageData]] = {}
    for page in pages:
        for candidate in (page.url, page.final_url):
            if candidate:
                by_url.setdefault(normalize_url(candidate), []).append(page)
    return by_url


def _check(ctx: RuleContext) -> list[Issue]:
    by_url = _index(ctx.pages)
    matchers = _matchers(ctx.robots_disallow)
    issues: list[Issue] = []
    seen: set[str] = set()
    for sitemap_url in ctx.sitemap_urls:
        key = normalize_url(sitemap_url)
        if key in seen:
            continue
        seen.add(key)
        for page in by_url.get(key, []):
            directives = page.robots_directives
            blocked_by = _robots_pattern(page, matchers)
            if "noindex" not in directives and blocked_by is None:
                continue
            detail: dict[str, Any] = {"directives": sorted(directives)}
            if blocked_by is not None:
                detail["pattern"] = blocked_by
            issues.append(_issue(sitemap_url, detail))
            break
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Sitemap URL is not indexable",
    description=(
        "A URL submitted in the sitemap is marked noindex or disallowed in robots.txt, so the "
        "sitemap advertises a page that search engines will drop; fix the signal or remove the URL."
    ),
    check=_check,
)
