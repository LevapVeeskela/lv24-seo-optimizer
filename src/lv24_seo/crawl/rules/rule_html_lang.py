"""``html-lang-missing``: every HTML document needs a non-blank ``lang`` attribute.

The rule reads ``PageData.html_lang`` (populated by the parser) and flags ``None``, an empty
string and whitespace-only values. A missing document language weakens language targeting
and is a WCAG 3.1.1 failure.
"""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "html-lang-missing"
_SEVERITY: Severity = "medium"


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _is_html_page(page: PageData) -> bool:
    if page.status != 200:
        return False
    mime = page.content_type.split(";", 1)[0].strip().lower()
    return mime in {"text/html", "application/xhtml+xml"}


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        if (page.html_lang or "").strip():
            continue
        issues.append(_issue(page.url, {"html_lang": page.html_lang}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="HTML lang attribute is missing",
    description=(
        'The root html element must declare a language (for example lang="ru") so search '
        "engines and assistive technology know how to interpret and read the document."
    ),
    check=_check,
)
