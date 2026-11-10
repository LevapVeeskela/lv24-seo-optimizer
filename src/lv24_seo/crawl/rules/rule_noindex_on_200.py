"""noindex-on-200: a live 200 page that asks search engines not to index it."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "noindex-on-200"
_SEVERITY: Severity = "critical"


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if page.status != 200:
            continue
        directives = page.robots_directives
        if "noindex" not in directives:
            continue
        issues.append(_issue(page.url, {"directives": sorted(directives)}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Noindex on a live page",
    description=(
        "A page returns 200 but carries a noindex directive, so it cannot rank even when it is "
        "linked or listed in a sitemap; drop the directive or redirect the URL."
    ),
    check=_check,
)
