"""Unit tests for Wave 4 indexing triage (WP4.3)."""

from __future__ import annotations

import pytest

from lv24_seo.analysis.indexing import REASONS, triage


def _google(url: str, coverage_state: str = "", *, verdict: str = "") -> dict[str, str]:
    return {"url": url, "coverage_state": coverage_state, "verdict": verdict}


def _yandex(url: str, reason: str) -> dict[str, str]:
    return {"url": url, "reason": reason}


def _crawl(url: str, rule_id: str) -> dict[str, str]:
    return {"url": url, "rule_id": rule_id}


@pytest.mark.parametrize(
    ("coverage_state", "verdict", "expected"),
    [
        ("Excluded by 'noindex' tag", "FAIL", "noindex"),
        ("Blocked by robots.txt", "FAIL", "blocked_by_robots"),
        ("Alternate page with proper canonical tag", "PASS", "canonical_mismatch"),
        ("Alternate page", "", "canonical_mismatch"),
        ("Duplicate, Google chose different canonical than user", "FAIL", "duplicate"),
        ("Duplicate without user-selected canonical", "FAIL", "duplicate"),
        ("Soft 404", "FAIL", "soft_404"),
        ("Page with redirect", "NEUTRAL", "redirect"),
        ("Server error (5xx)", "FAIL", "error_5xx"),
        ("Discovered - currently not indexed", "FAIL", "discovered_not_indexed"),
        ("Crawled - currently not indexed", "FAIL", "crawled_not_indexed"),
        ("Excluded", "NEUTRAL", "other"),
        ("URL is unknown to Google", "NEUTRAL", "other"),
        ("", "", "other"),
        ("Excluded by 'NOINDEX' Tag", "", "noindex"),
        ("BLOCKED BY ROBOTS.TXT", "", "blocked_by_robots"),
        ("", "noindex", "noindex"),
        ("", "SOFT 404", "soft_404"),
    ],
)
def test_google_phrases_map_to_reasons(
    coverage_state: str,
    verdict: str,
    expected: str,
) -> None:
    buckets = triage(google=[_google("https://example.com/a", coverage_state, verdict=verdict)])

    assert [bucket.reason for bucket in buckets] == [expected]


@pytest.mark.parametrize(
    ("reason_text", "expected"),
    [
        ("ROBOTS_BLOCKED", "blocked_by_robots"),
        ("robots_blocked", "blocked_by_robots"),
        ("NOINDEX", "noindex"),
        ("CANONICAL_MISMATCH", "canonical_mismatch"),
        ("NOT_FOUND_404", "soft_404"),
        ("HTTP_5XX", "error_5xx"),
        ("DUPLICATE", "duplicate"),
        ("MOVED_PERMANENTLY", "other"),
        ("", "other"),
    ],
)
def test_yandex_reasons_map_to_reasons(reason_text: str, expected: str) -> None:
    buckets = triage(yandex=[_yandex("https://example.com/a", reason_text)])

    assert [bucket.reason for bucket in buckets] == [expected]


@pytest.mark.parametrize(
    ("rule_id", "expected"),
    [
        ("noindex-on-200", "noindex"),
        ("sitemap-url-noindex", "noindex"),
        ("robots-blocked", "blocked_by_robots"),
        ("canonical-mismatch", "canonical_mismatch"),
        ("canonical-missing", "canonical_mismatch"),
        ("soft-404", "soft_404"),
        ("redirect-chain", "redirect"),
        ("server-error", "error_5xx"),
        ("sitemap-url-not-ok", "other"),
        ("title-missing", "other"),
        ("NOINDEX-ON-200", "noindex"),
    ],
)
def test_crawl_rules_map_to_reasons(rule_id: str, expected: str) -> None:
    buckets = triage(crawl=[_crawl("https://example.com/a", rule_id)])

    assert [bucket.reason for bucket in buckets] == [expected]


def test_counts_aggregate_once_per_reason_and_engine() -> None:
    google = [
        _google("https://example.com/a", "Excluded by 'noindex' tag"),
        _google("https://example.com/a", "Excluded by 'noindex' tag"),
        _google("https://example.com/b", "Excluded by 'noindex' tag"),
        _google("https://example.com/c", "Blocked by robots.txt"),
    ]
    yandex = [_yandex("https://example.com/a", "NOINDEX")]
    crawl = [
        _crawl("https://example.com/a", "noindex-on-200"),
        _crawl("https://example.com/b", "sitemap-url-noindex"),
        _crawl("https://example.com/d", "sitemap-url-noindex"),
    ]

    buckets = triage(google=google, yandex=yandex, crawl=crawl)
    by_reason = {bucket.reason: bucket for bucket in buckets}

    assert list(by_reason) == ["noindex", "blocked_by_robots"]
    assert by_reason["noindex"].count == 6  # 2 google + 1 yandex + 3 crawl
    assert by_reason["noindex"].engines == ("google", "yandex", "crawl")
    assert by_reason["noindex"].examples == (
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/d",
    )
    assert by_reason["blocked_by_robots"].count == 1
    assert by_reason["blocked_by_robots"].engines == ("google",)


def test_examples_are_capped_at_five_by_default() -> None:
    google = [
        _google(f"https://example.com/{index}", "Crawled - currently not indexed")
        for index in range(7)
    ]

    bucket = triage(google=google)[0]

    assert bucket.reason == "crawled_not_indexed"
    assert bucket.count == 7
    assert bucket.examples == tuple(f"https://example.com/{index}" for index in range(5))


def test_examples_respect_explicit_max_examples() -> None:
    google = [
        _google(f"https://example.com/{index}", "Discovered - currently not indexed")
        for index in range(4)
    ]

    assert triage(google=google, max_examples=2)[0].examples == (
        "https://example.com/0",
        "https://example.com/1",
    )
    assert triage(google=google, max_examples=0)[0].examples == ()
    assert triage(google=google, max_examples=0)[0].count == 4


def test_unknown_strings_from_every_engine_fall_into_other() -> None:
    buckets = triage(
        google=[_google("https://example.com/g", "Something brand new")],
        yandex=[_yandex("https://example.com/y", "SOMETHING_NEW")],
        crawl=[_crawl("https://example.com/c", "title-missing")],
    )

    assert [bucket.reason for bucket in buckets] == ["other"]
    assert buckets[0].count == 3
    assert buckets[0].engines == ("google", "yandex", "crawl")
    assert buckets[0].examples == (
        "https://example.com/g",
        "https://example.com/y",
        "https://example.com/c",
    )


def test_empty_input_returns_no_buckets() -> None:
    assert triage() == []
    assert triage(google=[], yandex=[], crawl=[]) == []


def test_bucket_order_follows_canonical_reason_order() -> None:
    buckets = triage(
        google=[
            _google(
                "https://example.com/x",
                "Duplicate, Google chose different canonical than user",
            )
        ],
        yandex=[_yandex("https://example.com/y", "HTTP_5XX")],
        crawl=[_crawl("https://example.com/z", "soft-404")],
    )

    assert [bucket.reason for bucket in buckets] == ["soft_404", "error_5xx", "duplicate"]
    assert REASONS == (
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


@pytest.mark.parametrize(
    ("expected", "rows"),
    [
        ("noindex", {"google": [_google("https://example.com/a", "noindex")]}),
        ("blocked_by_robots", {"google": [_google("https://example.com/a", "robots")]}),
        ("canonical_mismatch", {"google": [_google("https://example.com/a", "canonical")]}),
        ("soft_404", {"google": [_google("https://example.com/a", "soft 404")]}),
        ("redirect", {"google": [_google("https://example.com/a", "redirect")]}),
        ("error_5xx", {"google": [_google("https://example.com/a", "5xx")]}),
        (
            "discovered_not_indexed",
            {"google": [_google("https://example.com/a", "discovered")]},
        ),
        (
            "crawled_not_indexed",
            {"google": [_google("https://example.com/a", "crawled not indexed")]},
        ),
        ("duplicate", {"google": [_google("https://example.com/a", "duplicate")]}),
        ("other", {"google": [_google("https://example.com/a", "excluded")]}),
    ],
)
def test_each_reason_carries_its_next_action(
    expected: str,
    rows: dict[str, list[dict[str, str]]],
) -> None:
    actions = {
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

    bucket = triage(**rows)[0]

    assert bucket.reason == expected
    assert bucket.next_action == actions[expected]


def test_rows_without_a_url_are_ignored() -> None:
    buckets = triage(
        google=[
            {"coverage_state": "noindex"},
            _google("", "noindex"),
            _google("  ", "noindex"),
        ]
    )

    assert buckets == []
