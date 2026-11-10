"""canonical-missing: an indexable HTML page without a canonical link."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "canonical-missing"
_SEVERITY: Severity = "medium"


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _is_html(page_content_type: str) -> bool:
    return page_content_type.lower().startswith("text/html")


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if page.status != 200 or not _is_html(page.content_type):
            continue
        if (page.canonical or "").strip():
            continue
        issues.append(_issue(page.url))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Missing canonical",
    description=(
        "A 200 HTML page without a canonical link lets tracking parameters and mirror URLs "
        "compete with it; declare a self-referencing canonical."
    ),
    check=_check,
)
