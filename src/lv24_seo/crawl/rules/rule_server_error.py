"""server-error: the page answered with a 5xx status."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "server-error"
_SEVERITY: Severity = "critical"


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _check(ctx: RuleContext) -> list[Issue]:
    return [
        _issue(page.url, {"status": page.status}) for page in ctx.pages if 500 <= page.status < 600
    ]


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Server error",
    description=(
        "A 5xx response tells search engines the URL is temporarily broken; repeated failures "
        "drop the page from the index, so fix the backend or serve a deliberate status."
    ),
    check=_check,
)
