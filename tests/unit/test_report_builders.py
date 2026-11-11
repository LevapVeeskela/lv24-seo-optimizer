"""Unit tests for the Wave 5 report builders (WP5.1 + WP5.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from lv24_seo.report.builders import build_indexing, build_opportunities, build_weekly

_PERIOD_START = date(2026, 11, 4)
_PERIOD_END = date(2026, 11, 10)
_GENERATED_AT = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)


def _current() -> dict[str, float]:
    return {"clicks": 120.0, "impressions": 2400.0, "ctr": 0.05, "position": 6.0}


def _previous() -> dict[str, float]:
    return {"clicks": 100.0, "impressions": 2000.0, "ctr": 0.05, "position": 7.0}


def _gsc_rows() -> list[dict[str, object]]:
    return [
        {
            "query": "seo",
            "page": "/a",
            "clicks": 10,
            "impressions": 200,
            "ctr": 0.05,
            "position": 9.0,
        },
        {
            "query": "seo tools",
            "page": "/b",
            "clicks": 5,
            "impressions": 100,
            "ctr": 0.05,
            "position": 3.0,
        },
    ]


def _yandex_rows() -> list[dict[str, object]]:
    return [{"query": "seo", "clicks": 7, "shows": 300, "position": 4.0}]


def _issues() -> list[dict[str, str]]:
    return [
        {"rule_id": "h1_missing", "severity": "high", "url": "https://example.com/a"},
        {"rule_id": "title_missing", "severity": "critical", "url": "https://example.com/b"},
        {"rule_id": "h1_missing", "severity": "high", "url": "https://example.com/c"},
    ]


def test_weekly_report_has_sections_in_order_with_two_engines() -> None:
    report = build_weekly(
        property_id="sc-domain:example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        current=_current(),
        previous=_previous(),
        gsc_rows=_gsc_rows(),
        yandex_rows=_yandex_rows(),
        issues=_issues(),
        limitations=["gsc_lag_3d"],
        generated_at=_GENERATED_AT,
    )

    assert report.kind == "weekly"
    assert report.property_id == "sc-domain:example.com"
    assert report.period_start == "2026-11-04"
    assert report.period_end == "2026-11-10"
    assert report.generated_at == "2026-11-10T09:30:00+00:00"
    assert report.limitations == ["gsc_lag_3d"]
    assert report.headline == "clicks +20.0%, impressions +20.0%, ctr 0.0%, position -1.0"
    assert [section.key for section in report.sections] == [
        "headline",
        "traffic",
        "opportunities",
        "issues",
        "sources",
    ]

    traffic = report.section("traffic")
    assert traffic is not None
    assert [row["engine"] for row in traffic.rows] == ["gsc", "yandex"]
    assert traffic.rows[0]["clicks"] == 15.0
    assert traffic.rows[0]["impressions"] == 300.0
    assert traffic.rows[1]["impressions"] == 300.0
    assert traffic.rows[1]["limitations"] == ["top_3000_queries", "no_query_page_join"]

    opportunities = report.section("opportunities")
    assert opportunities is not None
    assert [row["kind"] for row in opportunities.rows] == ["striking_distance", "ctr_gap"]
    assert all(row["expected_clicks"] == 6.0 for row in opportunities.rows)

    issues = report.section("issues")
    assert issues is not None
    assert [(row["severity"], row["count"], row["rule_ids"]) for row in issues.rows] == [
        ("critical", 1, ["title_missing"]),
        ("high", 2, ["h1_missing"]),
    ]
    assert issues.summary == "3 issues: 1 critical, 2 high"

    sources = report.section("sources")
    assert sources is not None
    assert [row["source"] for row in sources.rows] == ["gsc", "yandex", "metrika"]
    assert [row["status"] for row in sources.rows] == ["rows", "rows", "missing"]
    assert "metrika: missing (no rows)" in sources.notes
    assert "limitation: gsc_lag_3d" in sources.notes
    assert "caveat: engines use different query sampling; totals are not comparable" in (
        sources.notes
    )


def test_weekly_headline_warns_on_a_significant_bad_delta() -> None:
    report = build_weekly(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        current={"clicks": 50.0, "impressions": 1000.0, "ctr": 0.05, "position": 6.0},
        previous={"clicks": 100.0, "impressions": 1000.0, "ctr": 0.05, "position": 6.0},
        generated_at=_GENERATED_AT,
    )

    assert report.headline.startswith("⚠ ")
    assert "clicks -50.0%" in report.headline


def test_weekly_headline_has_no_warning_when_every_significant_delta_is_good() -> None:
    report = build_weekly(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        current={"clicks": 150.0, "impressions": 1000.0, "ctr": 0.05, "position": 5.0},
        previous={"clicks": 100.0, "impressions": 1000.0, "ctr": 0.05, "position": 5.0},
        generated_at=_GENERATED_AT,
    )

    assert not report.headline.startswith("⚠ ")
    assert report.headline.startswith("clicks +50.0%")


def test_missing_engine_is_named_in_sources_notes() -> None:
    report = build_weekly(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        current=_current(),
        previous=_previous(),
        gsc_rows=_gsc_rows()[:1],
        generated_at=_GENERATED_AT,
    )

    sources = report.section("sources")
    assert sources is not None
    assert "gsc: 1 row" in sources.notes
    assert "yandex: missing (no rows)" in sources.notes
    assert "metrika: missing (no rows)" in sources.notes
    assert sources.summary == "1 of 3 engines returned rows"


def test_indexing_report_buckets_examples_and_verification() -> None:
    report = build_indexing(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        google=[
            {"url": "https://example.com/a", "coverage_state": "Excluded by 'noindex' tag"},
            {"url": "https://example.com/b", "coverage_state": "Excluded by 'noindex' tag"},
        ],
        crawl=[{"url": "https://example.com/c", "rule_id": "robots-blocked"}],
        generated_at=_GENERATED_AT,
    )

    assert report.kind == "indexing"
    assert report.headline == "3 issues in 2 buckets"
    assert report.warnings == []
    assert [section.key for section in report.sections] == [
        "buckets",
        "examples",
        "verification",
    ]

    buckets = report.section("buckets")
    assert buckets is not None
    assert [row["reason"] for row in buckets.rows] == ["noindex", "blocked_by_robots"]
    assert [row["count"] for row in buckets.rows] == [2, 1]
    assert buckets.rows[0]["engines"] == ["google"]
    assert buckets.rows[0]["next_action"] == "remove noindex from canonical pages and re-check"

    examples = report.section("examples")
    assert examples is not None
    assert examples.rows == [
        {"reason": "noindex", "url": "https://example.com/a"},
        {"reason": "noindex", "url": "https://example.com/b"},
        {"reason": "blocked_by_robots", "url": "https://example.com/c"},
    ]

    verification = report.section("verification")
    assert verification is not None
    assert verification.rows == []
    assert len(verification.notes) == 1
    assert "re-run" in verification.notes[0].lower()
    assert "bucket counts" in verification.notes[0]


def test_opportunities_report_headline_sums_expected_clicks() -> None:
    report = build_opportunities(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        rows=[
            {
                "query": "seo",
                "page": "/a",
                "clicks": 5,
                "impressions": 1000,
                "ctr": 0.005,
                "position": 12.0,
            }
        ],
        limit=1,
        generated_at=_GENERATED_AT,
    )

    assert report.kind == "opportunities"
    assert report.headline == "Top 1 opportunities, expected +30.0 clicks/week"

    section = report.section("opportunities")
    assert section is not None
    assert len(section.rows) == 1
    row = section.rows[0]
    assert row["kind"] == "striking_distance"
    assert row["query"] == "seo"
    assert row["page"] == "/a"
    assert row["expected_clicks"] == 30.0
    assert row["evidence"]["position_band"] == "8-20"
    assert row["evidence"]["ctr_lift"] == 0.03


def test_empty_weekly_input_produces_a_valid_report_with_warnings() -> None:
    report = build_weekly(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        current={},
        previous={},
        generated_at=_GENERATED_AT,
    )

    assert report.headline == "No provider data available"
    assert report.warnings == [
        "no metrics for either period; delta rows are all zero",
        "no provider rows; traffic and opportunities are empty",
    ]
    assert [section.key for section in report.sections] == [
        "headline",
        "traffic",
        "opportunities",
        "issues",
        "sources",
    ]
    issues = report.section("issues")
    assert issues is not None
    assert issues.rows == []
    traffic = report.section("traffic")
    assert traffic is not None
    assert traffic.rows == []


def test_empty_indexing_and_opportunities_reports_are_valid() -> None:
    indexing = build_indexing(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        generated_at=_GENERATED_AT,
    )

    assert indexing.headline == "0 issues in 0 buckets"
    assert indexing.warnings == ["no indexing rows were provided; buckets are empty"]
    assert indexing.section("verification") is not None

    opportunities = build_opportunities(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        rows=[],
        generated_at=_GENERATED_AT,
    )

    assert opportunities.headline == "Top 0 opportunities, expected +0.0 clicks/week"
    assert opportunities.warnings == ["no query rows were provided; opportunities are empty"]
    section = opportunities.section("opportunities")
    assert section is not None
    assert section.rows == []


def test_generated_at_defaults_to_an_aware_utc_timestamp() -> None:
    report = build_weekly(
        property_id="example.com",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        current=_current(),
        previous=_previous(),
    )

    assert datetime.fromisoformat(report.generated_at).utcoffset() == timedelta(0)


def test_same_input_produces_the_same_report() -> None:
    def build() -> dict[str, object]:
        return build_weekly(
            property_id="example.com",
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
            current=_current(),
            previous=_previous(),
            gsc_rows=_gsc_rows(),
            yandex_rows=_yandex_rows(),
            issues=_issues(),
            limitations=["gsc_lag_3d"],
            generated_at=_GENERATED_AT,
        ).to_dict()

    assert build() == build()
