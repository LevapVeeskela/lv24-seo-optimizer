"""Report builder and renderer tests for the attributed change digest (F1)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from lv24_seo.report.builders import build_weekly
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import (
    render_email_text,
    render_html,
    render_markdown,
    render_text,
)

_PERIOD_START = date(2026, 11, 4)
_PERIOD_END = date(2026, 11, 10)
_GENERATED_AT = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
_CURRENT = {"clicks": 80.0, "impressions": 1000.0, "ctr": 0.05, "position": 6.0}
_PREVIOUS = {"clicks": 100.0, "impressions": 1000.0, "ctr": 0.05, "position": 6.0}


def _new_issue(url: str = "https://example.com/a") -> dict[str, object]:
    return {
        "rule_id": "noindex-on-200",
        "url": url,
        "severity": "high",
        "state": "open",
        "change": "new",
        "first_seen": "2026-11-05",
        "last_seen": "2026-11-09",
    }


def _weekly(**kwargs: object) -> Report:
    options: dict[str, object] = {
        "property_id": "sc-domain:example.com",
        "period_start": _PERIOD_START,
        "period_end": _PERIOD_END,
        "current": dict(_CURRENT),
        "previous": dict(_PREVIOUS),
        "generated_at": _GENERATED_AT,
    }
    options.update(kwargs)
    return build_weekly(**options)  # type: ignore[arg-type]


def _digest_report() -> Report:
    report = Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-04",
        period_end="2026-11-10",
        headline="clicks -20.0%",
        generated_at="2026-11-10T09:30:00+00:00",
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="",
                rows=[
                    {
                        "metric": "clicks",
                        "current": 80.0,
                        "previous": 100.0,
                        "abs_delta": -20.0,
                        "pct_delta": -20.0,
                        "significant": True,
                        "direction": "down",
                        "good": False,
                    }
                ],
            ),
            ReportSection(
                key="digest",
                title="Attributed change digest",
                summary="1 material change: 1 with matching evidence, 0 unexplained",
                rows=[
                    {
                        "metric": "clicks",
                        "change": {
                            "current": 80.0,
                            "previous": 100.0,
                            "abs_delta": -20.0,
                            "pct_delta": -20.0,
                            "direction": "down",
                            "good": False,
                        },
                        "cause": "new_crawl_issue",
                        "confidence": "high",
                        "evidence": [
                            {
                                "kind": "crawl_issue",
                                "rule_id": "noindex-on-200",
                                "url": "https://example.com/a",
                                "severity": "high",
                                "change": "new",
                            }
                        ],
                        "next_action": (
                            "fix noindex-on-200 on the affected URLs, then re-crawl "
                            "and re-check clicks"
                        ),
                    }
                ],
                total=1,
                truncated=False,
                data_age="2026-11-09",
            ),
            ReportSection(
                key="sources",
                title="Sources",
                summary="1 of 3 engines returned rows",
                rows=[{"source": "gsc", "status": "rows", "rows": 40, "limitations": []}],
            ),
        ],
        limitations=[],
        warnings=[],
    )
    return report


# -- builder ---------------------------------------------------------------------------------


def test_weekly_without_digest_evidence_keeps_the_existing_section_list() -> None:
    report = _weekly(limitations=["gsc_lag_3d"])

    assert [section.key for section in report.sections] == [
        "headline",
        "traffic",
        "opportunities",
        "issues",
        "sources",
    ]
    assert report.limitations == ["gsc_lag_3d"]


def test_weekly_digest_section_is_added_after_the_headline() -> None:
    report = _weekly(digest_evidence={"issue_changes": [_new_issue()]})

    assert [section.key for section in report.sections][:2] == ["headline", "digest"]
    section = report.section("digest")
    assert section is not None
    assert section.total == 1
    assert section.truncated is False
    assert section.data_age == "2026-11-09"
    assert section.rows[0]["cause"] == "new_crawl_issue"
    assert section.rows[0]["confidence"] == "high"
    assert section.rows[0]["change"]["pct_delta"] == -20.0
    assert report.limitations == []


def test_digest_section_serializes_bounded_metadata_only_when_set() -> None:
    report = _weekly(digest_evidence={"issue_changes": [_new_issue()]})

    digest = next(item for item in report.to_dict()["sections"] if item["key"] == "digest")
    headline = next(item for item in report.to_dict()["sections"] if item["key"] == "headline")

    assert digest["total"] == 1
    assert digest["truncated"] is False
    assert digest["data_age"] == "2026-11-09"
    assert set(headline) == {"key", "title", "summary", "rows", "notes"}


def test_digest_without_evidence_records_a_limitation() -> None:
    report = _weekly(digest_evidence={})

    section = report.section("digest")
    assert section is not None
    assert section.rows[0]["cause"] == "unexplained"
    assert "digest_no_evidence" in report.limitations


def test_digest_without_comparison_says_so() -> None:
    report = _weekly(previous={}, digest_evidence={})

    section = report.section("digest")
    assert section is not None
    assert section.rows == []
    assert section.summary == "No comparison window: metric changes cannot be attributed"
    assert report.limitations == ["digest_no_comparison"]


def test_digest_malformed_evidence_records_a_limitation() -> None:
    report = _weekly(digest_evidence={"issue_changes": [{"rule_id": "noindex-on-200"}]})

    assert "digest_evidence_malformed" in report.limitations
    assert report.section("digest") is not None


def test_digest_does_not_duplicate_a_caller_limitation() -> None:
    report = _weekly(
        limitations=["digest_no_evidence"],
        digest_evidence={},
    )

    assert report.limitations == ["digest_no_evidence"]


# -- renderers -------------------------------------------------------------------------------


def test_text_renders_the_digest_with_evidence_and_action() -> None:
    text = render_text(_digest_report())

    assert "Attributed change digest" in text
    assert "Oldest evidence: 2026-11-09" in text
    assert "1. Clicks: 100 \u2192 80 (-20.0%)" in text
    assert "Candidate cause: New crawl issue (Confidence: High)" in text
    assert "Evidence: noindex-on-200 \u00b7 https://example.com/a \u00b7 High" in text
    assert "Next action: Fix noindex-on-200, then re-crawl and re-check Clicks." in text


def test_text_snapshot_is_stable() -> None:
    assert render_text(_digest_report()) == (
        "LV24 SEO \u00b7 Weekly report\n"
        "------------------------------------------------------------------------\n"
        "\n"
        "example.com\n"
        "4\u201310 Nov 2026\n"
        "Performance is declining.\n"
        "\n"
        "Clicks: 80 \u00b7 -20.0% \u00b7 Previous 100 \u00b7 Significant\n"
        "Impressions: No data\n"
        "CTR: No data\n"
        "Avg. position: No data\n"
        "\n"
        "Attributed change digest\n"
        "Oldest evidence: 2026-11-09\n"
        "1. Clicks: 100 \u2192 80 (-20.0%)\n"
        "   Candidate cause: New crawl issue (Confidence: High)\n"
        "   Evidence: noindex-on-200 \u00b7 https://example.com/a \u00b7 High\n"
        "   Next action: Fix noindex-on-200, then re-crawl and re-check Clicks.\n"
        "\n"
        "Opportunities\n"
        "No query data to evaluate opportunities.\n"
        "\n"
        "Issues\n"
        "No issue data was provided.\n"
        "\n"
        "Data sources\n"
        "Google Search Console (Traffic data): Rows: 40\n"
        "\n"
        "Generated: 10 Nov 2026, 09:30 UTC\n"
        "Property ID: sc-domain:example.com\n"
        "Generated by lv24-seo"
    )


def test_email_text_stays_within_78_columns() -> None:
    text = render_email_text(_digest_report())

    assert max(len(line) for line in text.splitlines()) <= 78


def test_digest_copy_is_localized_in_russian_and_chinese() -> None:
    russian = render_text(_digest_report(), locale="ru")
    chinese = render_text(_digest_report(), locale="zh")

    assert "Разбор изменений" in russian
    assert "Возможная причина: Новая проблема краула (Уверенность: Высокая)" in russian
    assert "Следующее действие:" in russian
    assert "变化归因摘要" in chinese
    assert "可能原因: 新的抓取问题" in chinese
    assert "下一步" in chinese


def test_html_renders_the_digest_and_escapes_evidence() -> None:
    html_doc = render_html(_digest_report())

    assert ">Attributed change digest<" in html_doc
    assert "New crawl issue" in html_doc
    assert "noindex-on-200" in html_doc
    assert "https://example.com/a" in html_doc
    assert "{" not in html_doc
    assert "}" not in html_doc
    assert "None" not in html_doc


def test_markdown_renders_a_canonical_digest_block() -> None:
    markdown = render_markdown(_digest_report())

    assert "## Attributed change digest" in markdown
    assert "### clicks" in markdown
    assert "- Change: 100 -> 80 (-20.0%)" in markdown
    assert "- Cause: new_crawl_issue (confidence: high)" in markdown
    assert "- Evidence: noindex-on-200 \u00b7 https://example.com/a \u00b7 high" in markdown
    assert "- Next action: fix noindex-on-200" in markdown


def test_empty_digest_states_render_without_a_claim() -> None:
    report = _digest_report()
    report.sections[1].rows = []
    report.sections[1].total = 0
    report.limitations = ["digest_no_evidence"]

    text = render_text(report)
    html_doc = render_html(report)
    markdown = render_markdown(report)

    assert "No crawl issue, indexing event or page state evidence was provided" in text
    assert "No crawl issue, indexing event or page state evidence was provided" in html_doc
    assert "## Attributed change digest" in markdown
    assert "1 material change" in markdown


def test_malformed_digest_rows_never_crash_the_renderers() -> None:
    report = _digest_report()
    report.sections[1].rows = [
        {"metric": "clicks", "change": "not-a-mapping"},
        {"metric": "unknown-metric", "change": {"current": 1.0, "previous": 2.0}},
        {"metric": "clicks", "change": {"current": None}},
        {"metric": "clicks", "cause": "new_crawl_issue", "evidence": "not-a-list"},
    ]

    for document in (
        render_text(report),
        render_email_text(report),
        render_html(report),
        render_markdown(report),
    ):
        assert "Attributed change digest" in document or "变化归因" in document
        assert "None" not in document
        assert "{'metric'" not in document
