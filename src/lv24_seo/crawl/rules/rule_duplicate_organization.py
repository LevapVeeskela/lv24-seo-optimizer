"""``jsonld-organization-duplicate``: one page should declare one Organization entity.

Several ``Organization`` items on the same page (including items flattened from ``@graph``)
make entity reconciliation ambiguous for search engines. The rule reports once per affected
page with the total count; distinct pages do not influence each other.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "jsonld-organization-duplicate"
_SEVERITY: Severity = "low"
_ORGANIZATION_TYPE = "Organization"


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


def _organization_count(page: PageData) -> int:
    return sum(1 for item in _iter_jsonld(page.jsonld) if _ORGANIZATION_TYPE in _types(item))


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        count = _organization_count(page)
        if count > 1:
            issues.append(_issue(page.url, {"count": count}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Page declares Organization structured data more than once",
    description=(
        "Keep a single Organization entity per page; competing declarations make brand "
        "data ambiguous and hard for search engines to reconcile."
    ),
    check=_check,
)
