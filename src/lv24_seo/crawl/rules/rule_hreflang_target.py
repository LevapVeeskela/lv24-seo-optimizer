"""``hreflang-target-not-ok``: hreflang annotations must target reachable pages.

A source page that lists a crawled target with a non-200 status is reported once per
``(lang, target)`` pair. Targets outside the crawl are ignored rather than guessed. URLs are
resolved against the source page before matching, so relative annotations work.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "hreflang-target-not-ok"
_SEVERITY: Severity = "high"


def _issue(url: str, detail: dict[str, Any]) -> Issue:
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail)


def _is_html_page(page: PageData) -> bool:
    if page.status != 200:
        return False
    mime = page.content_type.split(";", 1)[0].strip().lower()
    return mime in {"text/html", "application/xhtml+xml"}


def _normalize(url: str, base: str) -> str:
    """Return a comparable absolute URL, or an empty string when there is none."""
    raw = url.strip()
    if not raw:
        return ""
    absolute = urljoin(base, raw)
    parts = urlsplit(absolute)
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _page_index(pages: list[PageData]) -> dict[str, PageData]:
    index: dict[str, PageData] = {}
    for page in pages:
        for candidate in (page.url, page.final_url):
            key = _normalize(candidate, page.url)
            if key:
                index.setdefault(key, page)
    return index


def _check(ctx: RuleContext) -> list[Issue]:
    index = _page_index(ctx.pages)
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        seen: set[tuple[str, str, int]] = set()
        for entry in page.hreflang:
            target_raw = (entry.get("url") or "").strip()
            if not target_raw:
                continue
            target_key = _normalize(target_raw, page.url)
            if not target_key:
                continue
            target_page = index.get(target_key)
            if target_page is None or target_page.status == 200:
                continue
            lang = (entry.get("lang") or "").strip()
            marker = (lang, target_key, target_page.status)
            if marker in seen:
                continue
            seen.add(marker)
            issues.append(
                _issue(
                    page.url,
                    {"lang": lang, "target": target_raw, "status": target_page.status},
                )
            )
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="hreflang target is not a working page",
    description=(
        "Every hreflang annotation should point at a page that returns 200; a broken "
        "target wastes crawl budget and drops the language variant from search results."
    ),
    check=_check,
)
