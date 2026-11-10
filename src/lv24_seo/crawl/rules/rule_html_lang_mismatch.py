"""``html-lang-mismatch``: the document language must be one of its hreflang variants.

Substitution note
-----------------
Wave 3 originally reserved this file for ``viewport-missing``. :class:`PageData` exposes no
viewport, raw ``<head>`` or ``page.text`` field (only parsed prose in ``text``, which the
crawler may strip), so a viewport rule could only be implemented by inventing a header or
guessing, which would violate the evidence-based contract. The agreed replacement is
``html-lang-mismatch`` and the reserved file name is kept to avoid a gap in the wave's file
manifest.

Rule behaviour: a page that sets ``html_lang`` **and** declares at least one ``hreflang``
variant must list its own language among those variants. Language tags are compared
case-insensitively after ``_`` -> ``-`` normalisation; a match on the primary subtag is
accepted (``ru`` matches ``ru-RU``), because the rule catches a genuinely wrong language,
not a regional detail. Pages without hreflang annotations are not international variants and
are skipped. ``x-default`` never satisfies the check; it names no language.
"""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "html-lang-mismatch"
_SEVERITY: Severity = "medium"
_X_DEFAULT = "x-default"


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _is_html_page(page: PageData) -> bool:
    if page.status != 200:
        return False
    mime = page.content_type.split(";", 1)[0].strip().lower()
    return mime in {"text/html", "application/xhtml+xml"}


def _normalize_lang(tag: str) -> str:
    return tag.strip().lower().replace("_", "-")


def _same_language(left: str, right: str) -> bool:
    left_norm = _normalize_lang(left)
    right_norm = _normalize_lang(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return left_norm.split("-", 1)[0] == right_norm.split("-", 1)[0]


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        html_lang = (page.html_lang or "").strip()
        if not html_lang or not page.hreflang:
            continue
        langs = [(entry.get("lang") or "").strip() for entry in page.hreflang]
        variants = [lang for lang in langs if lang and lang.lower() != _X_DEFAULT]
        if any(_same_language(html_lang, lang) for lang in variants):
            continue
        issues.append(_issue(page.url, {"html_lang": html_lang, "hreflang": variants}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="HTML lang is absent from the hreflang variants",
    description=(
        "When a page declares hreflang variants, one of them must match the document's own "
        "html lang; otherwise the language signals contradict each other."
    ),
    check=_check,
)
