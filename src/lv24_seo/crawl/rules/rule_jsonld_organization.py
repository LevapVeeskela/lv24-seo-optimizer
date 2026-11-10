"""``jsonld-organization``: the site must expose an Organization entity somewhere.

This is a site-wide rule: it reports once at ``ctx.site_url`` only when no crawled, healthy
HTML page declares ``Organization`` (as a string or a list entry in ``@type``, including
items nested in ``@graph``). The number of pages actually inspected travels in the detail so
a report never implies broader coverage than the crawl provided.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "jsonld-organization"
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


def _has_organization(page: PageData) -> bool:
    return any(_ORGANIZATION_TYPE in _types(item) for item in _iter_jsonld(page.jsonld))


def _check(ctx: RuleContext) -> list[Issue]:
    pages_checked = 0
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        pages_checked += 1
        if _has_organization(page):
            return []
    return [_issue(ctx.site_url, {"pages_checked": pages_checked})]


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="No Organization structured data on the site",
    description=(
        "At least one page should declare schema.org Organization so search engines can "
        "associate the brand, logo and contact details with the whole site."
    ),
    check=_check,
)
