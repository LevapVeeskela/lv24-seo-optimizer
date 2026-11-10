"""``hreflang-not-reciprocal``: hreflang annotations must point at each other.

When page A declares ``hreflang`` links to page B, B must list A back with some ``hreflang``
annotation. Targets that were not crawled are ignored: the crawler cannot prove a missing
backlink without seeing the other page. URLs are resolved against the page they appear on
and compared after defragmentation, host/scheme case folding and trailing-slash removal.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "hreflang-not-reciprocal"
_SEVERITY: Severity = "high"


def _issue(url: str, detail: dict[str, str]) -> Issue:
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


def _entry_url(entry: dict[str, str]) -> str:
    return (entry.get("url") or "").strip()


def _listed_urls(page: PageData) -> set[str]:
    urls: set[str] = set()
    for entry in page.hreflang:
        key = _normalize(_entry_url(entry), page.url)
        if key:
            urls.add(key)
    return urls


def _source_keys(page: PageData) -> set[str]:
    keys = {
        _normalize(page.url, page.url),
        _normalize(page.final_url, page.url),
    }
    keys.discard("")
    return keys


def _check(ctx: RuleContext) -> list[Issue]:
    index = _page_index(ctx.pages)
    issues: list[Issue] = []
    for page in ctx.pages:
        if not _is_html_page(page):
            continue
        source_keys = _source_keys(page)
        seen: set[tuple[str, str]] = set()
        for entry in page.hreflang:
            target_raw = _entry_url(entry)
            if not target_raw:
                continue
            target_key = _normalize(target_raw, page.url)
            if not target_key or target_key in source_keys:
                continue
            target_page = index.get(target_key)
            if target_page is None:
                continue
            if _listed_urls(target_page) & source_keys:
                continue
            lang = (entry.get("lang") or "").strip()
            marker = (lang, target_key)
            if marker in seen:
                continue
            seen.add(marker)
            issues.append(_issue(page.url, {"lang": lang, "target": target_raw}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="hreflang links are not reciprocal",
    description=(
        "When one page points at another with hreflang, the target must return the link; "
        "one-way annotations are ignored by search engines and signal an inconsistent "
        "international setup."
    ),
    check=_check,
)
