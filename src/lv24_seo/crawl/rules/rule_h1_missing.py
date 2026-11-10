"""h1-missing: HTML pages without an <h1> heading."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "h1-missing"
_SEVERITY: Severity = "medium"


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
    return [_issue(page.url) for page in _html_pages(ctx) if len(page.h1) == 0]


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Missing H1",
    description=(
        "A page without an <h1> gives readers and search engines no single main topic; "
        "add exactly one primary heading."
    ),
    check=_check,
)
