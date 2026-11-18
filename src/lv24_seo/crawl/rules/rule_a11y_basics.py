"""a11y-basics: the accessibility failures that are cheap to detect from static HTML.

The rule is deliberately conservative: it only reads fields the tolerant parser can
recover (labels, accessible names, landmarks, heading order) and emits one issue per
finding. It never guesses at colour contrast or JavaScript-driven behaviour.
"""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "a11y-basics"
_SEVERITY: Severity = "medium"
_MAX_LINK_ISSUES = 20
_MAX_HEADING_ISSUES = 5
_MAX_ISSUES_PER_PAGE = 30

_ACCESSIBLE_NAME_KEYS = ("text", "aria_label", "aria_labelledby", "title")
_INPUT_NAME_KEYS = ("label", "aria_label", "aria_labelledby", "title")


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _has_name(item: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(_text(item.get(key)) for key in keys)


def _html_pages(ctx: RuleContext) -> list[PageData]:
    return [
        page
        for page in ctx.pages
        if page.status == 200 and page.content_type.startswith("text/html") and page.error is None
    ]


def _input_issues(page: PageData) -> list[Issue]:
    return [
        _issue(
            page.url,
            {
                "check": "input-label",
                "input_type": _text(item.get("type")),
                "name": _text(item.get("name")),
            },
        )
        for item in page.inputs
        if not _has_name(item, _INPUT_NAME_KEYS)
    ]


def _button_issues(page: PageData) -> list[Issue]:
    return [
        _issue(page.url, {"check": "button-name"})
        for item in page.buttons
        if not _has_name(item, _ACCESSIBLE_NAME_KEYS)
    ]


def _link_issues(page: PageData) -> list[Issue]:
    issues: list[Issue] = []
    for item in page.link_details:
        if len(issues) >= _MAX_LINK_ISSUES:
            break
        if not _has_name(item, (*_ACCESSIBLE_NAME_KEYS, "image_alt")):
            issues.append(_issue(page.url, {"check": "link-name", "href": _text(item.get("href"))}))
    return issues


def _heading_issues(page: PageData) -> list[Issue]:
    issues: list[Issue] = []
    previous: int | None = None
    for heading in page.headings:
        level = heading.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 6:
            continue
        if previous is not None and level - previous > 1 and len(issues) < _MAX_HEADING_ISSUES:
            issues.append(
                _issue(page.url, {"check": "heading-order", "from": previous, "to": level})
            )
        previous = level
    return issues


def _page_issues(page: PageData) -> list[Issue]:
    issues: list[Issue] = []
    issues.extend(_input_issues(page))
    issues.extend(_button_issues(page))
    issues.extend(_link_issues(page))
    if not page.has_main:
        issues.append(_issue(page.url, {"check": "main-landmark"}))
    if not page.has_skip_link:
        issues.append(_issue(page.url, {"check": "skip-link"}))
    issues.extend(_heading_issues(page))
    return issues[:_MAX_ISSUES_PER_PAGE]


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in _html_pages(ctx):
        issues.extend(_page_issues(page))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Accessibility basics",
    description=(
        "HTML pages should label form controls, give buttons and links an accessible "
        "name, expose a <main> landmark, offer a skip link and keep heading levels in order."
    ),
    check=_check,
    tags=("a11y",),
)
