"""``schema-required``: common Schema.org types must carry their required properties.

Search engines only build a rich result when the type-specific required properties are
present. This rule covers the types that ``jsonld-required`` (articles), ``jsonld-event``
and ``jsonld-organization`` do not inspect, so no item is reported twice.

Items are flattened out of ``@graph`` containers and nested lists, exactly like
``jsonld-required``; ``@type`` may be a single string or a list. A field counts as present
only when its value is non-blank: empty strings, empty objects and lists of empty objects are
missing. Malformed JSON-LD nodes (non-objects, unusable ``@type``) are skipped and the rule
never raises, so one bad page cannot abort a crawl. Findings are capped per page.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "schema-required"
_SEVERITY: Severity = "medium"
_MAX_ISSUES_PER_PAGE = 10
_MAX_QUESTIONS = 10

# Each type maps to requirement groups. A group lists alternative property names and is
# satisfied when any of them is present; when nothing in a group is present the first name is
# reported, so the ``missing`` list always names a property that really is absent.
_REQUIREMENTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "Product": (("name",), ("image",), ("offers",)),
    "Offer": (("price",), ("priceCurrency",)),
    "BreadcrumbList": (("itemListElement",),),
    "FAQPage": (("mainEntity",),),
    "Recipe": (("name",), ("image",), ("recipeIngredient",), ("recipeInstructions",)),
    "JobPosting": (
        ("title",),
        ("description",),
        ("datePosted",),
        ("hiringOrganization",),
        ("jobLocation",),
    ),
    "VideoObject": (("name",), ("description",), ("thumbnailUrl",), ("uploadDate",)),
    "LocalBusiness": (("name",), ("address",)),
    "Course": (("name",), ("description",), ("provider",)),
    "SoftwareApplication": (("name",), ("offers", "aggregateRating")),
}

_QUESTION_FIELDS = ("name", "acceptedAnswer")


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


def _first_known_type(item: dict[str, Any]) -> str | None:
    for name in _types(item):
        if name in _REQUIREMENTS:
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


def _missing_fields(item: dict[str, Any], groups: tuple[tuple[str, ...], ...]) -> list[str]:
    missing: list[str] = []
    for group in groups:
        if not any(_present(item.get(name)) for name in group):
            missing.append(group[0])
    return missing


def _question_nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []


def _is_question(node: dict[str, Any]) -> bool:
    # CMSs frequently omit @type on FAQ entries; a node without a type inside ``mainEntity``
    # is still treated as the question it claims to be.
    types = _types(node)
    return "Question" in types or not types


def _question_gaps(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Report FAQ questions that lack a name or an accepted answer, bounded per item."""
    gaps: list[dict[str, Any]] = []
    for index, node in enumerate(_question_nodes(item.get("mainEntity"))):
        if not _is_question(node):
            continue
        missing = [name for name in _QUESTION_FIELDS if not _present(node.get(name))]
        if not missing:
            continue
        gap: dict[str, Any] = {"index": index, "missing": missing}
        name = node.get("name")
        if isinstance(name, str) and name.strip():
            gap["name"] = name.strip()
        gaps.append(gap)
        if len(gaps) >= _MAX_QUESTIONS:
            break
    return gaps


def _detail(item: dict[str, Any], matched: str) -> dict[str, Any] | None:
    detail: dict[str, Any] = {
        "type": matched,
        "missing": _missing_fields(item, _REQUIREMENTS[matched]),
    }
    if matched == "FAQPage":
        gaps = _question_gaps(item)
        if gaps:
            detail["questions_missing"] = gaps
    if detail["missing"] or "questions_missing" in detail:
        return detail
    return None


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        reported = 0
        for item in _iter_jsonld(page.jsonld):
            if reported >= _MAX_ISSUES_PER_PAGE:
                break
            matched = _first_known_type(item)
            if matched is None:
                continue
            detail = _detail(item, matched)
            if detail is None:
                continue
            issues.append(_issue(page.url, detail))
            reported += 1
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Schema.org item is missing required fields",
    description=(
        "Product, Offer, BreadcrumbList, FAQPage, Recipe, JobPosting, VideoObject, "
        "LocalBusiness, Course and SoftwareApplication items need the properties search "
        "engines require to build their rich results."
    ),
    check=_check,
)
