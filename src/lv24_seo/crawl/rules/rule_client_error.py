"""client-error: a 4xx status that is not an expected gone/rate-limit answer."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "client-error"
_SEVERITY: Severity = "high"

# 404/410 are honest "gone" answers and 429 is a retry signal, not a page defect.
_EXPECTED_STATUSES = frozenset({404, 410, 429})


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not 400 <= page.status < 500:
            continue
        if page.status in _EXPECTED_STATUSES:
            continue
        issues.append(_issue(page.url, {"status": page.status}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Unexpected client error",
    description=(
        "A 4xx response other than 404/410/429 means the crawler was refused (auth wall, "
        "forbidden path, bad request); search engines see the same block."
    ),
    check=_check,
)
