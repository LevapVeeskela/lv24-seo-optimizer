"""title-length: <title> outside the 15..60 character recommendation."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "title-length"
_SEVERITY: Severity = "low"
_MIN_LENGTH = 15
_MAX_LENGTH = 60


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
    issues: list[Issue] = []
    for page in _html_pages(ctx):
        title = (page.title or "").strip()
        if not title:
            # Missing titles are reported by title-missing; do not double-report.
            continue
        length = len(title)
        if _MIN_LENGTH <= length <= _MAX_LENGTH:
            continue
        issues.append(_issue(page.url, {"length": length, "title": title}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Title length",
    description=(
        "Titles shorter than 15 or longer than 60 characters lose relevance or get "
        "truncated in search results."
    ),
    check=_check,
)
