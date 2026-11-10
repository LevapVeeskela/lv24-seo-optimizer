"""``jsonld-required``: article structured data must carry the rich-result fields.

An item is inspected when its ``@type`` (a single string or a list) contains
``Article``, ``NewsArticle`` or ``BlogPosting``. Items nested in ``@graph`` containers are
flattened, which is how most CMSs emit their schema. A field counts as present only when it
has a non-blank value: empty strings, empty objects and lists of empty objects are missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "jsonld-required"
_SEVERITY: Severity = "high"
_ARTICLE_TYPES = frozenset({"Article", "NewsArticle", "BlogPosting"})
_REQUIRED_FIELDS = ("headline", "datePublished", "author", "image")


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _is_html_page(page: PageData) -> bool:
    if page.status != 200:
        return False
    mime = page.content_type.split(";", 1)[0].strip().lower()
    return mime in {"text/html", "application/xhtml+xml"}


def _iter_jsonld(value: Any) -> Iterator[dict[str, Any]]:
    """Yield JSON-LD nodes, flattening ``@graph`` and nested lists."""
    if isinstance(value, list):
        for entry in value:
            yield from _iter_jsonld(entry)
        return
    if not isinstance(value, dict):
        return
    graph = value.get("@graph")
    if isinstance(graph, list):
        for entry in graph:
            yield from _iter_jsonld(entry)
        return
    yield value


def _types(item: dict[str, Any]) -> list[str]:
    value = item.get("@type")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, str)]
    return []


def _first_type(item: dict[str, Any], wanted: frozenset[str]) -> str | None:
    for name in _types(item):
        if name in wanted:
            return name
    return None


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple, set)):
        return any(_present(entry) for entry in value)
    return True


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        for item in _iter_jsonld(page.jsonld):
            matched = _first_type(item, _ARTICLE_TYPES)
            if matched is None:
                continue
            missing = [name for name in _REQUIRED_FIELDS if not _present(item.get(name))]
            if missing:
                issues.append(_issue(page.url, {"type": matched, "missing": missing}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Article JSON-LD is missing required fields",
    description=(
        "Article, NewsArticle and BlogPosting items need headline, datePublished, author "
        "and image, otherwise search engines cannot build the expected rich result."
    ),
    check=_check,
)
