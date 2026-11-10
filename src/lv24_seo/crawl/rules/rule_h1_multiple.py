"""h1-multiple: HTML pages with more than one <h1> heading."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "h1-multiple"
_SEVERITY: Severity = "low"


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
    return [
        _issue(page.url, {"count": len(page.h1)}) for page in _html_pages(ctx) if len(page.h1) > 1
    ]


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Multiple H1 headings",
    description=(
        "Competing <h1> headings blur the page topic; keep one H1 and demote the rest to H2/H3."
    ),
    check=_check,
)
