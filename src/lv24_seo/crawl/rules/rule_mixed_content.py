"""``mixed-content``: HTTPS pages must not load subresources over plain HTTP.

Browsers block or downgrade active mixed content, so an HTTPS page that references
``http://`` assets is both a security and a rendering risk. The rule scans the parsed link
and image URLs, keeps the first five unique offenders and reports once per page. Pages that
are not served over HTTPS are skipped because nothing is mixed there.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "mixed-content"
_SEVERITY: Severity = "high"
_MAX_URLS = 5


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _is_html_page(page: PageData) -> bool:
    if page.status != 200:
        return False
    mime = page.content_type.split(";", 1)[0].strip().lower()
    return mime in {"text/html", "application/xhtml+xml"}


def _is_insecure(value: str) -> bool:
    return value.strip().lower().startswith("http://")


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        if urlsplit(page.url).scheme.lower() != "https":
            continue
        offenders = [link for link in page.links if _is_insecure(link)]
        for image in page.images:
            offenders.extend(
                value for value in image.values() if isinstance(value, str) and _is_insecure(value)
            )
        if not offenders:
            continue
        unique = list(dict.fromkeys(offenders))
        issues.append(_issue(page.url, {"urls": unique[:_MAX_URLS]}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="HTTPS page loads insecure HTTP subresources",
    description=(
        "Links and images on an HTTPS page must use HTTPS; mixed content is blocked or "
        "downgraded by browsers and weakens transport security."
    ),
    check=_check,
)
