"""meta-description-duplicate: several HTML pages share one meta description."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "meta-description-duplicate"
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
    groups: dict[str, list[PageData]] = {}
    for page in _html_pages(ctx):
        description = (page.meta_description or "").strip()
        if not description:
            # Blank descriptions are meta-description-missing, not duplicates.
            continue
        groups.setdefault(description.lower(), []).append(page)

    issues: list[Issue] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        for page in group:
            issues.append(
                _issue(
                    page.url,
                    {"description": (page.meta_description or "").strip(), "count": len(group)},
                )
            )
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Duplicate meta description",
    description=(
        "Reused meta descriptions give every page the same snippet and dilute relevance; "
        "each page should describe itself."
    ),
    check=_check,
)
