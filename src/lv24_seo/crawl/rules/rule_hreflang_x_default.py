"""``hreflang-missing-x-default``: declared hreflang sets need an ``x-default`` entry.

Pages without any ``hreflang`` annotations are not international variants and are left
alone. Once a page declares at least one variant, the set should also say which URL serves
users whose language does not match any listed variant. ``x-default`` is compared
case-insensitively.
"""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "hreflang-missing-x-default"
_SEVERITY: Severity = "medium"
_X_DEFAULT = "x-default"


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
        if not _is_html_page(page) or not page.hreflang:
            continue
        langs = [(entry.get("lang") or "").strip() for entry in page.hreflang]
        if any(lang.lower() == _X_DEFAULT for lang in langs if lang):
            continue
        issues.append(_issue(page.url, {"langs": [lang for lang in langs if lang]}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="hreflang set has no x-default",
    description=(
        "A page that declares hreflang variants should also declare x-default so search "
        "engines know which URL to serve when no language matches the visitor."
    ),
    check=_check,
)
