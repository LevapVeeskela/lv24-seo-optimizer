"""soft-404: a 200 page that is really a "not found" stub.

Two independent signals are required. A title that merely contains the word "404" (for
example a guide called "How to fix 404 errors") is not evidence of a soft 404; the rule only
fires when a not-found *phrase* is present **and** the page is empty or explicitly presents a
404/410 status. A near-empty page without any phrase is reported separately as ``thin``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "soft-404"
_SEVERITY: Severity = "high"

# Whole phrases, not bare numbers: "404" alone appears in legitimate article titles.
_NOT_FOUND_PHRASES = (
    "not found",
    "page not found",
    "страница не найдена",
    "не найдена",
    "не найден",
    "не существует",
)
_STATUS_MARKERS = re.compile(r"(?<![\w-])(404|410)(?![\w-])")
_THIN_WORD_COUNT = 30


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _is_root(url: str) -> bool:
    return (urlsplit(url).path or "/") == "/"


def _not_found_phrase(title: str) -> str | None:
    for phrase in _NOT_FOUND_PHRASES:
        if phrase in title:
            return phrase
    return None


def _is_empty(page_word_count: int) -> bool:
    return page_word_count < _THIN_WORD_COUNT


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if page.status != 200:
            continue
        title = (page.title or "").lower()
        phrase = _not_found_phrase(title)
        if phrase is not None and (_is_empty(page.word_count) or _STATUS_MARKERS.search(title)):
            detail: dict[str, Any] = {"reason": "not_found_phrase", "phrase": phrase}
            if _STATUS_MARKERS.search(title):
                detail["status_marker"] = True
            if _is_empty(page.word_count):
                detail["empty_body"] = True
            issues.append(_issue(page.url, detail))
            continue
        is_html = page.content_type.lower().startswith("text/html")
        if is_html and _is_empty(page.word_count) and not _is_root(page.url):
            issues.append(_issue(page.url, {"reason": "thin"}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Soft 404",
    description=(
        "The page answers 200 but its title says 'not found' while the body is empty or it "
        "presents a 404/410 status; search engines treat it as a soft 404, so return 404/410 "
        "or restore the content."
    ),
    check=_check,
)
