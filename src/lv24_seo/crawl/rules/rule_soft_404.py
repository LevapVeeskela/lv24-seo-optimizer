"""soft-404: a 200 page that is really a "not found" or near-empty stub."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "soft-404"
_SEVERITY: Severity = "high"

_TITLE_MARKERS = ("не найдена", "not found", "404")
_THIN_WORD_COUNT = 30


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _is_root(url: str) -> bool:
    return (urlsplit(url).path or "/") == "/"


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if page.status != 200:
            continue
        title = (page.title or "").lower()
        if any(marker in title for marker in _TITLE_MARKERS):
            issues.append(_issue(page.url, {"reason": "title"}))
            continue
        is_html = page.content_type.lower().startswith("text/html")
        if is_html and page.word_count < _THIN_WORD_COUNT and not _is_root(page.url):
            issues.append(_issue(page.url, {"reason": "thin"}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Soft 404",
    description=(
        "The page answers 200 but its title says 'not found' or it carries almost no content; "
        "search engines treat it as a soft 404, so return 404/410 or restore the content."
    ),
    check=_check,
)
