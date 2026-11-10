"""robots-blocked: a 200 URL whose path is disallowed for crawlers in robots.txt."""

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


def _matchers(patterns: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(pattern.strip(), _compile(pattern.strip())) for pattern in patterns if pattern.strip()]


def _check(ctx: RuleContext) -> list[Issue]:
    matchers = _matchers(ctx.robots_disallow)
    if not matchers:
        return []
    issues: list[Issue] = []
    for page in ctx.pages:
        if page.status != 200:
            continue
        target = _target(page.url)
        for pattern, regex in matchers:
            if regex.match(target):
                issues.append(_issue(page.url, {"pattern": pattern}))
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
