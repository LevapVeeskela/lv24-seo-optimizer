"""``jsonld-event-required``: Event structured data must be usable for event rich results.

Every ``Event`` item needs ``name``, ``startDate`` and a ``location`` that names a venue or
carries an address. ``location`` may be a single Place or a list of Places; one usable place
is enough. ``@graph`` containers are flattened so CMS output is inspected too.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "jsonld-event-required"
_SEVERITY: Severity = "high"
_EVENT_TYPE = "Event"
_REQUIRED_FIELDS = ("name", "startDate", "location")


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


def _location_ok(value: Any) -> bool:
    if isinstance(value, dict):
        return _present(value.get("name")) or _present(value.get("address"))
    if isinstance(value, (list, tuple)):
        return any(_location_ok(entry) for entry in value)
    return False


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        for item in _iter_jsonld(page.jsonld):
            if _EVENT_TYPE not in _types(item):
                continue
            missing: list[str] = []
            if not _present(item.get("name")):
                missing.append("name")
            if not _present(item.get("startDate")):
                missing.append("startDate")
            if not _location_ok(item.get("location")):
                missing.append("location")
            if missing:
                issues.append(_issue(page.url, {"missing": missing}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Event JSON-LD is missing required fields",
    description=(
        "Event items need name, startDate and a location that has a name or address so "
        "search engines can surface the event with venue and date details."
    ),
    check=_check,
)
