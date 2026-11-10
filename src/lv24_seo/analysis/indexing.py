"""Indexing triage: bucket not-indexed URLs by root cause.

Three free-form sources are folded into one stable set of reasons:

* ``google`` rows carry GSC URL Inspection ``coverage_state`` and ``verdict`` text;
* ``yandex`` rows carry the ``reason`` of a ``search-urls/events`` record;
* ``crawl`` rows carry our own crawl ``rule_id``.

Matching is case-insensitive. A URL is counted at most once per
``(reason, engine)`` pair, so a page reported by both Google and our crawl adds
two to the bucket count but appears once in ``examples``. Rows without a URL are
ignored: there is nothing to deduplicate or act on.

Buckets are returned only when non-empty and always in :data:`REASONS` order.
Examples keep first-seen order, engines are listed in google/yandex/crawl order,
and ``max_examples`` caps the example list (the default is 5).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["REASONS", "Bucket", "triage"]

REASONS: tuple[str, ...] = (
    "noindex",
    "blocked_by_robots",
    "canonical_mismatch",
    "soft_404",
    "redirect",
    "error_5xx",
    "discovered_not_indexed",
    "crawled_not_indexed",
    "duplicate",
    "other",
)

_ENGINES: tuple[str, ...] = ("google", "yandex", "crawl")

_NEXT_ACTIONS: dict[str, str] = {
    "noindex": "remove noindex from canonical pages and re-check",
    "blocked_by_robots": "fix robots.txt Disallow for this path",
    "canonical_mismatch": "align canonical with the sitemap URL",
    "soft_404": "return a real 404 or restore content",
    "redirect": "point links at the final URL, keep one hop",
    "error_5xx": "fix the server error before resubmitting",
    "discovered_not_indexed": "add internal links and refresh the sitemap",
    "crawled_not_indexed": "improve content quality or merge duplicates",
    "duplicate": "consolidate duplicates with canonical",
    "other": "inspect manually",
}

_CRAWL_REASONS: dict[str, str] = {
    "noindex-on-200": "noindex",
    "sitemap-url-noindex": "noindex",
    "robots-blocked": "blocked_by_robots",
    "canonical-mismatch": "canonical_mismatch",
    "canonical-missing": "canonical_mismatch",
    "soft-404": "soft_404",
    "redirect-chain": "redirect",
    "server-error": "error_5xx",
    "sitemap-url-not-ok": "other",
}


@dataclass(frozen=True, slots=True)
class Bucket:
    """One actionable group of URLs sharing an indexing failure reason."""

    reason: str
    count: int
    examples: tuple[str, ...]
    next_action: str
    engines: tuple[str, ...]


def triage(
    *,
    google: Sequence[Mapping[str, str]] = (),
    yandex: Sequence[Mapping[str, str]] = (),
    crawl: Sequence[Mapping[str, str]] = (),
    max_examples: int = 5,
) -> list[Bucket]:
    """Group indexing rows into non-empty buckets, ordered by :data:`REASONS`."""
    hits: dict[str, dict[str, dict[str, None]]] = {}

    for row in google:
        _record(hits, _google_reason(row), "google", _field(row, "url"))
    for row in yandex:
        _record(hits, _yandex_reason(_field(row, "reason")), "yandex", _field(row, "url"))
    for row in crawl:
        _record(hits, _crawl_reason(_field(row, "rule_id")), "crawl", _field(row, "url"))

    limit = max(0, max_examples)
    buckets: list[Bucket] = []
    for reason in REASONS:
        by_engine = hits.get(reason)
        if not by_engine:
            continue
        engines = tuple(engine for engine in _ENGINES if engine in by_engine)
        count = sum(len(by_engine[engine]) for engine in engines)
        buckets.append(
            Bucket(
                reason=reason,
                count=count,
                examples=_examples(by_engine, engines, limit),
                next_action=_NEXT_ACTIONS[reason],
                engines=engines,
            )
        )
    return buckets


def _record(
    hits: dict[str, dict[str, dict[str, None]]],
    reason: str,
    engine: str,
    url: str,
) -> None:
    if not url:
        return
    hits.setdefault(reason, {}).setdefault(engine, {}).setdefault(url, None)


def _field(row: Mapping[str, str], key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _google_reason(row: Mapping[str, str]) -> str:
    text = f"{_field(row, 'coverage_state')} {_field(row, 'verdict')}".casefold()

    if "noindex" in text:
        return "noindex"
    if "robots" in text:
        return "blocked_by_robots"
    if "soft 404" in text:
        return "soft_404"
    if "redirect" in text:
        return "redirect"
    if "5xx" in text or "server error" in text:
        return "error_5xx"
    # "Duplicate, Google chose different canonical than user" must win over the
    # generic "canonical" match below.
    if "duplicate" in text:
        return "duplicate"
    if "discovered" in text:
        return "discovered_not_indexed"
    if "crawled" in text and "not indexed" in text:
        return "crawled_not_indexed"
    if "canonical" in text or "alternate page" in text:
        return "canonical_mismatch"
    return "other"


def _yandex_reason(text: str) -> str:
    folded = text.casefold()

    if "robots" in folded:
        return "blocked_by_robots"
    if "noindex" in folded:
        return "noindex"
    if "canonical" in folded:
        return "canonical_mismatch"
    if "404" in folded:
        return "soft_404"
    if "5xx" in folded:
        return "error_5xx"
    if "duplicate" in folded:
        return "duplicate"
    return "other"


def _crawl_reason(rule_id: str) -> str:
    return _CRAWL_REASONS.get(rule_id.casefold(), "other")


def _examples(
    by_engine: Mapping[str, Mapping[str, None]],
    engines: tuple[str, ...],
    limit: int,
) -> tuple[str, ...]:
    if limit <= 0:
        return ()
    seen: set[str] = set()
    examples: list[str] = []
    for engine in engines:
        for url in by_engine[engine]:
            if url in seen:
                continue
            seen.add(url)
            examples.append(url)
            if len(examples) == limit:
                return tuple(examples)
    return tuple(examples)
