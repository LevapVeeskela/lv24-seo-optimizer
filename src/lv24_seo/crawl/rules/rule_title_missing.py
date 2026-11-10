"""title-missing: HTML pages that do not declare a non-empty <title>."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "title-missing"
_SEVERITY: Severity = "high"


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


def _check(ctx: RuleContext) -> list[Issue]:
    return [_issue(page.url) for page in _html_pages(ctx) if not (page.title or "").strip()]


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Missing title",
    description=(
        "Every indexable HTML page needs a non-empty <title>: it is the primary search "
        "snippet and a ranking signal."
    ),
    check=_check,
)
