"""Sitemap ↔ crawl coverage diff: what the site advertises vs what a crawl saw.

The sitemap is the site's *claim* about its indexable inventory; the crawl is
the evidence. This module compares the two and buckets every disagreement:

``unlinked``
    In the sitemap, fetched OK, but no crawled page links to it (orphan candidate).
``missing_from_sitemap``
    Crawled, 200, indexable HTML that the sitemap does not list.
``not_ok``
    In the sitemap but answered non-200 or redirected.
``noindex``
    In the sitemap but marked ``noindex`` (meta robots or ``X-Robots-Tag``).
``blocked``
    In the sitemap but outside crawl scope (robots.txt or another origin).
``unvisited``
    In the sitemap but never fetched (budget): status **unknown**, not broken.

Honesty rules: when the crawl or the sitemap inventory was truncated, or the
sitemap had fetch errors, ``complete`` is ``False`` and ``limitations`` says
which buckets are only *candidates*. With no sitemap at all, the diff is empty
and says so instead of flagging every page as missing. Lists are capped
(``limit``); ``counts`` always carry the full sizes. Pure function, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from lv24_seo.crawl.graph import normalize_url
from lv24_seo.crawl.models import PageData

__all__ = ["BUCKETS", "CoverageDiff", "sitemap_crawl_diff"]

BUCKETS: tuple[str, ...] = (
    "unlinked",
    "missing_from_sitemap",
    "not_ok",
    "noindex",
    "blocked",
    "unvisited",
)
_DEFAULT_LIMIT = 50
_SCOPE_ERRORS = frozenset({"robots_blocked"})


@dataclass(slots=True)
class CoverageDiff:
    """Bucketed disagreements plus the honesty fields every bounded response carries."""

    buckets: dict[str, list[str]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    sitemap_urls: int = 0
    crawled_urls: int = 0
    complete: bool = False
    truncated: bool = False
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "buckets": {name: list(self.buckets.get(name, [])) for name in BUCKETS},
            "counts": {name: self.counts.get(name, 0) for name in BUCKETS},
            "sitemap_urls": self.sitemap_urls,
            "crawled_urls": self.crawled_urls,
            "complete": self.complete,
            "truncated": self.truncated,
            "limitations": list(self.limitations),
        }


def sitemap_crawl_diff(
    pages: Sequence[PageData],
    sitemap_urls: Iterable[str],
    *,
    crawl_truncated: bool = False,
    sitemap_truncated: bool = False,
    sitemap_errors: Sequence[str] = (),
    limit: int = _DEFAULT_LIMIT,
) -> CoverageDiff:
    """Compare the advertised sitemap inventory with crawl observations."""
    advertised = _ordered_unique(normalize_url(url) for url in sitemap_urls)
    by_url = _pages_by_url(pages)
    linked = _linked_targets(pages)
    in_sitemap = set(advertised)

    found: dict[str, list[str]] = {name: [] for name in BUCKETS}
    for url in advertised:
        page = by_url.get(url)
        if page is None:
            found["unvisited"].append(url)
        elif page.error in _SCOPE_ERRORS:
            found["blocked"].append(url)
        elif page.error is not None or page.status == 0:
            found["unvisited"].append(url)  # network failure: unknown, not broken
        elif page.status != 200 or _redirected(page):
            found["not_ok"].append(url)
        elif _noindex(page):
            found["noindex"].append(url)
        elif url not in linked:
            found["unlinked"].append(url)

    if advertised:
        for url, page in sorted(by_url.items()):
            if url in in_sitemap or not _indexable_html(page):
                continue
            final = normalize_url(page.final_url or page.url)
            if final in in_sitemap:
                continue
            found["missing_from_sitemap"].append(url)

    diff = CoverageDiff(sitemap_urls=len(advertised), crawled_urls=len(by_url))
    capped = max(0, limit)
    for name in BUCKETS:
        values = sorted(found[name])
        diff.counts[name] = len(values)
        diff.buckets[name] = values[:capped]
        diff.truncated = diff.truncated or len(values) > capped

    diff.limitations = _limitations(
        advertised=bool(advertised),
        crawl_truncated=crawl_truncated,
        sitemap_truncated=sitemap_truncated,
        sitemap_errors=sitemap_errors,
        unvisited=diff.counts["unvisited"],
        list_truncated=diff.truncated,
    )
    diff.complete = (
        bool(advertised)
        and not crawl_truncated
        and not sitemap_truncated
        and not sitemap_errors
        and diff.counts["unvisited"] == 0
    )
    return diff


def _limitations(
    *,
    advertised: bool,
    crawl_truncated: bool,
    sitemap_truncated: bool,
    sitemap_errors: Sequence[str],
    unvisited: int,
    list_truncated: bool,
) -> list[str]:
    notes: list[str] = []
    if not advertised:
        notes.append("no sitemap URLs found: coverage diff is empty, not clean")
    if crawl_truncated:
        notes.append(
            "crawl truncated: 'unlinked' and 'missing_from_sitemap' are candidates, "
            "a link from an uncrawled page may exist"
        )
    if sitemap_truncated:
        notes.append("sitemap inventory truncated: 'missing_from_sitemap' may be over-reported")
    if sitemap_errors:
        notes.append(f"sitemap fetch errors ({len(sitemap_errors)}): inventory may be partial")
    if unvisited:
        notes.append(f"{unvisited} sitemap URL(s) not fetched: status unknown, not broken")
    if list_truncated:
        notes.append("bucket lists are capped; counts carry the full sizes")
    return notes


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return list(seen)


def _pages_by_url(pages: Sequence[PageData]) -> dict[str, PageData]:
    by_url: dict[str, PageData] = {}
    for page in pages:
        by_url.setdefault(normalize_url(page.url), page)
    return by_url


def _linked_targets(pages: Sequence[PageData]) -> set[str]:
    """Every URL an OK page links to (self-links excluded)."""
    targets: set[str] = set()
    for page in pages:
        if page.error is not None or page.status != 200:
            continue
        source = normalize_url(page.final_url or page.url)
        for link in page.links:
            target = normalize_url(link)
            if target != source:
                targets.add(target)
    return targets


def _redirected(page: PageData) -> bool:
    if len(page.redirect_chain) > 1:
        return True
    final = page.final_url or page.url
    return normalize_url(final) != normalize_url(page.url)


def _noindex(page: PageData) -> bool:
    directives = f"{page.meta_robots or ''},{page.x_robots_tag or ''}".lower()
    return "noindex" in directives or "none" in {part.strip() for part in directives.split(",")}


def _indexable_html(page: PageData) -> bool:
    return (
        page.error is None
        and page.status == 200
        and not _redirected(page)
        and "html" in (page.content_type or "").lower()
        and not _noindex(page)
    )
