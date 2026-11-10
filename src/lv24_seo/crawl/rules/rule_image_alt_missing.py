"""image-alt-missing: images without an ``alt`` attribute at all."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "image-alt-missing"
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
    issues: list[Issue] = []
    for page in _html_pages(ctx):
        if not page.images:
            continue
        # An explicit empty alt is decorative and valid; a missing key or None is not.
        missing = sum(1 for image in page.images if image.get("alt") is None)
        if missing:
            issues.append(_issue(page.url, {"missing": missing, "total": len(page.images)}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Images without alt text",
    description=(
        "Images need descriptive alt text (or an explicit empty alt when decorative) for "
        "accessibility and image search."
    ),
    check=_check,
)
