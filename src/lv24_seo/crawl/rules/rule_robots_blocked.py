"""robots-blocked: a 200 URL whose path is disallowed for crawlers in robots.txt.

Uses the same longest-match semantics as the fetcher (RFC 9309 §2.2.2): the longest
matching pattern wins and ``Allow`` wins ties, so an ``Allow: /private/public``
entry exempts a path that ``Disallow: /private/`` would otherwise block. Both the
requested and the final (post-redirect) URL are checked: a redirect that lands on
a disallowed path is a finding, not a clean page.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "robots-blocked"
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


def _longest_match(patterns: list[str], target: str) -> int:
    """Longest pattern length that matches ``target`` (RFC 9309 longest-match), or -1."""
    best = -1
    for pattern in patterns:
        candidate = pattern.strip()
        if not candidate:
            continue
        try:
            regex = _compile(candidate)
        except re.error:
            continue
        if regex.match(target) and len(candidate) > best:
            best = len(candidate)
    return best


def _check(ctx: RuleContext) -> list[Issue]:
    allow = list(getattr(ctx, "robots_allow", []) or [])
    if not ctx.robots_disallow and not allow:
        return []
    issues: list[Issue] = []
    for page in ctx.pages:
        if page.status != 200:
            continue
        for candidate in (page.url, page.final_url):
            if not candidate:
                continue
            target = _target(candidate)
            disallow_hit = _longest_match(ctx.robots_disallow, target)
            if disallow_hit < 0:
                continue
            # ``Allow`` wins ties and longer allows exempt the path.
            if _longest_match(allow, target) >= disallow_hit:
                continue
            issues.append(_issue(page.url, {"pattern": target, "final_url": page.final_url}))
            break
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Blocked by robots.txt",
    description=(
        "robots.txt disallows the path, so search engines cannot fetch the page and any "
        "ranking signal is lost; remove the Disallow rule or noindex the page instead."
    ),
    check=_check,
)
