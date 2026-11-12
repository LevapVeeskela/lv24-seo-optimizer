# ruff: noqa: RUF001
"""Product renderer contract: weekly email HTML, plain text and subject (en/ru/zh)."""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

import pytest

from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import (
    render_email_text,
    render_html,
    render_markdown,
    render_subject,
    render_text,
)

NBSP = "\u00a0"
_CYRILLIC = re.compile("[\u0400-\u04ff]")
_CJK = re.compile("[\u4e00-\u9fff]")
_FORBIDDEN_TOKENS = (
    "metric:",
    "current:",
    "abs_delta",
    "pct_delta",
    "significant:",
    "direction:",
    "evidence:",
    "rule_ids:",
)
_HTML_FORBIDDEN_TAGS = ("script", "style", "link", "img", "svg", "iframe", "form")


class _TagCollector(HTMLParser):
    """Collect start tags so tests can assert the document structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[str] = []
        self.attributes: list[dict[str, str | None]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:  # pragma: no cover - trivial collector
        self.tags.append(tag)
        self.attributes.append(dict(attrs))


def _display_width(text: str) -> int:
    """Terminal width: East Asian wide/fullwidth characters count as two."""
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _delta_row(
    metric: str,
    *,
    current: float,
    previous: float,
    pct_delta: float,
    significant: bool,
    good: bool,
    direction: str,
) -> dict[str, object]:
    return {
        "metric": metric,
        "current": current,
        "previous": previous,
        "abs_delta": round(current - previous, 4),
        "pct_delta": pct_delta,
        "significant": significant,
        "direction": direction,
        "good": good,
    }


def _weekly_full() -> Report:
    """A realistic built weekly report, including English builder prose to ignore."""
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="BUILDER-HEADLINE-MUST-NOT-RENDER",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="BUILDER-SUMMARY-MUST-NOT-RENDER",
                rows=[
                    _delta_row(
                        "clicks",
                        current=1234,
                        previous=1042,
                        pct_delta=18.4,
                        significant=True,
                        good=True,
                        direction="up",
                    ),
                    _delta_row(
                        "impressions",
                        current=45600,
                        previous=47000,
                        pct_delta=-3.0,
                        significant=False,
                        good=False,
                        direction="down",
                    ),
                    _delta_row(
                        "ctr",
                        current=0.0271,
                        previous=0.0222,
                        pct_delta=22.1,
                        significant=True,
                        good=True,
                        direction="up",
                    ),
                    _delta_row(
                        "position",
                        current=8.4,
                        previous=9.0,
                        pct_delta=-6.7,
                        significant=False,
                        good=False,
                        direction="down",
                    ),
                ],
                notes=["BUILDER-NOTE-MUST-NOT-RENDER"],
            ),
            ReportSection(
                key="traffic",
                title="Traffic by engine",
                summary="BUILDER-TRAFFIC-SUMMARY",
                rows=[
                    {
                        "engine": "gsc",
                        "rows": 120,
                        "clicks": 1234.0,
                        "impressions": 45600.0,
                        "ctr": 0.0271,
                        "position": 8.4,
                        "limitations": [],
                    },
                    {
                        "engine": "yandex",
                        "rows": 80,
                        "clicks": 210.0,
                        "impressions": 9000.0,
                        "ctr": 0.0233,
                        "position": 6.2,
                        "limitations": ["top_3000_queries"],
                    },
                    {
                        "engine": "metrika",
                        "rows": 40,
                        "clicks": 0.0,
                        "impressions": None,
                        "ctr": None,
                        "position": None,
                        "limitations": ["sessions_not_clicks"],
                    },
                ],
            ),
            ReportSection(
                key="opportunities",
                title="Opportunities",
                summary="BUILDER-OPPORTUNITIES-SUMMARY",
                rows=[
                    {
                        "kind": "striking_distance",
                        "query": "seo tools",
                        "page": "https://example.com/blog/seo",
                        "clicks": 3.0,
                        "impressions": 1200.0,
                        "ctr": 0.0025,
                        "position": 12.4,
                        "expected_clicks": 36.0,
                        "evidence": {"position": 12.4, "impressions": 1200.0},
                    },
                    {
                        "kind": "ctr_gap",
                        "query": "rank tracker",
                        "page": None,
                        "clicks": 5.0,
                        "impressions": 2400.0,
                        "ctr": 0.002,
                        "position": 9.1,
                        "expected_clicks": 41.0,
                        "evidence": {"ctr": 0.002, "expected_ctr": 0.04},
                    },
                    {
                        "kind": "decay",
                        "query": "old post",
                        "page": "https://example.com/old",
                        "clicks": 3.0,
                        "impressions": 100.0,
                        "ctr": 0.03,
                        "position": 15.0,
                        "expected_clicks": 14.0,
                        "evidence": {"previous_clicks": 17.0, "match": "query+page"},
                    },
                    {
                        "kind": "striking_distance",
                        "query": "fourth query must not render",
                        "page": "https://example.com/4",
                        "clicks": 1.0,
                        "impressions": 100.0,
                        "ctr": 0.01,
                        "position": 11.0,
                        "expected_clicks": 3.0,
                        "evidence": {},
                    },
                ],
                notes=["expected_clicks is a prioritisation estimate, not a traffic promise"],
            ),
            ReportSection(
                key="issues",
                title="Issues by severity",
                summary="BUILDER-ISSUES-SUMMARY",
                rows=[
                    {"severity": "critical", "count": 1, "rule_ids": ["broken_canonical"]},
                    {
                        "severity": "high",
                        "count": 1,
                        "rule_ids": ["broken_canonical", "thin_content"],
                    },
                    {"severity": "low", "count": 1, "rule_ids": []},
                ],
            ),
            ReportSection(
                key="sources",
                title="Sources",
                summary="BUILDER-SOURCES-SUMMARY",
                rows=[
                    {"source": "gsc", "status": "rows", "rows": 120, "limitations": []},
                    {"source": "yandex", "status": "rows", "rows": 80, "limitations": []},
                    {"source": "metrika", "status": "rows", "rows": 40, "limitations": []},
                    {"source": "bing", "status": "missing", "rows": 0, "limitations": []},
                    {
                        "source": "crux",
                        "status": "rows",
                        "rows": 10,
                        "kind": "field",
                        "limitations": [],
                    },
                    {
                        "source": "psi",
                        "status": "missing",
                        "rows": 0,
                        "kind": "lab",
                        "limitations": [],
                    },
                ],
                notes=["gsc: 120 rows", "bing: missing (no rows)"],
            ),
        ],
        limitations=["GSC data lags by two days."],
        warnings=[
            "yandex: quota almost exhausted",
            "GSC data lags by two days.",
        ],
    )


def _weekly_no_comparison() -> Report:
    """Fallback shape: a ``gsc`` section with current values only."""
    return Report(
        kind="weekly",
        property_id="https://example.com/",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="GSC comparison unavailable",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="gsc",
                title="Google Search Console",
                summary="clicks 500.0",
                rows=[
                    {"metric": "clicks", "current": 500.0},
                    {"metric": "impressions", "current": 9000.0},
                    {"metric": "ctr", "current": 0.0555},
                    {"metric": "position", "current": 7.5},
                ],
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


def _weekly_no_data() -> Report:
    return Report(
        kind="weekly",
        property_id="sc-domain:empty.example",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="No provider data available",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="sources",
                title="Sources",
                summary="No engine returned rows",
                rows=[
                    {"source": "gsc", "status": "missing", "rows": 0, "limitations": []},
                    {"source": "yandex", "status": "missing", "rows": 0, "limitations": []},
                    {"source": "metrika", "status": "missing", "rows": 0, "limitations": []},
                ],
                notes=["configure a provider token to collect data"],
            )
        ],
        limitations=[],
        warnings=["no provider rows; traffic and opportunities are empty"],
    )


def _weekly_clicks_only() -> Report:
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="",
                rows=[
                    _delta_row(
                        "clicks",
                        current=500,
                        previous=400,
                        pct_delta=25.0,
                        significant=True,
                        good=True,
                        direction="up",
                    )
                ],
            )
        ],
    )


def _weekly_ctr_only() -> Report:
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="headline",
                title="Headline",
                summary="",
                rows=[
                    _delta_row(
                        "ctr",
                        current=0.0271,
                        previous=0.0222,
                        pct_delta=22.1,
                        significant=True,
                        good=True,
                        direction="up",
                    )
                ],
            )
        ],
    )


def _weekly_with_sessions() -> Report:
    """A fallback report that carries Yandex Metrica sessions."""
    report = _weekly_full()
    report.sections.append(
        ReportSection(
            key="yandex_metrika",
            title="Yandex Metrica",
            summary="",
            rows=[{"metric": "visits", "current": 2690.0}],
        )
    )
    return report


def _weekly_gsc_fallback_with_others() -> Report:
    """Only section data: GSC plus Yandex engines, no ``sources``/``traffic`` rows."""
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="gsc",
                title="Google Search Console",
                summary="",
                rows=[
                    {"metric": "clicks", "current": 500.0},
                    {"query": "seo", "page": "https://example.com/a", "clicks": 3.0},
                ],
            ),
            ReportSection(
                key="yandex_webmaster",
                title="Yandex Webmaster",
                summary="",
                rows=[{"metric": "clicks", "current": 10.0}],
            ),
            ReportSection(
                key="yandex_metrika",
                title="Yandex Metrica",
                summary="",
                rows=[{"metric": "visits", "current": 20.0}],
            ),
        ],
    )


def _weekly_unknown_metric() -> Report:
    """Only unknown metric rows: enough data to avoid a no-data verdict."""
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="gsc",
                title="Google Search Console",
                summary="",
                rows=[
                    {
                        "metric": "scroll_depth",
                        "current": 0.4,
                        "previous": 0.2,
                        "significant": True,
                        "good": True,
                    },
                    {
                        "metric": "scroll_depth",
                        "current": 0.3,
                        "note": "half of sessions scroll past the fold",
                    },
                ],
            )
        ],
    )


def _weekly_metric_only() -> Report:
    """Metric rows but no query/page rows: opportunities have nothing to rank."""
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="gsc",
                title="Google Search Console",
                summary="",
                rows=[{"metric": "clicks", "current": 500.0}],
            )
        ],
    )


def _weekly_query_rows_no_opportunities() -> Report:
    report = _weekly_metric_only()
    report.section("gsc").rows.append(
        {"query": "seo", "page": "https://example.com/a", "clicks": 3.0}
    )
    report.sections.append(
        ReportSection(key="opportunities", title="Opportunities", summary="", rows=[])
    )
    return report


def _weekly_zh_wide() -> Report:
    """Chinese content long enough to exercise East Asian line wrapping."""
    report = _weekly_full()
    report.sections[2].rows = [
        {
            "kind": "striking_distance",
            "query": "搜索引擎优化工具推荐与对比分析",
            "page": "https://example.com/博客/搜索引擎优化工具推荐与对比分析指南",
            "clicks": 3.0,
            "impressions": 1200.0,
            "ctr": 0.0025,
            "position": 12.4,
            "expected_clicks": 36.0,
            "evidence": {},
        },
        {
            "kind": "ctr_gap",
            "query": "排名追踪工具哪个好用",
            "page": None,
            "clicks": 5.0,
            "impressions": 2400.0,
            "ctr": 0.002,
            "position": 9.1,
            "expected_clicks": 41.0,
            "evidence": {},
        },
        {
            "kind": "decay",
            "query": "旧文章流量下滑的原因分析",
            "page": "https://example.com/旧文章",
            "clicks": 3.0,
            "impressions": 100.0,
            "ctr": 0.03,
            "position": 15.0,
            "expected_clicks": 14.0,
            "evidence": {},
        },
    ]
    report.limitations = [
        "搜索控制台数据存在约两天的延迟，因此在解读最近几天的变化时需要格外谨慎，并结合其他来源交叉验证。"
    ]
    report.warnings = [
        "Yandex 配额即将用尽，请检查令牌和每日请求上限，以免影响下一周期的数据采集。"
    ]
    return report


def _unknown_section_report() -> Report:
    report = _weekly_no_comparison()
    report.sections.append(
        ReportSection(
            key="custom_block",
            title="Custom block",
            summary="Extra English prose kept as-is.",
            rows=[{"note": "kept value", "nested": {"a": 1}, "tags": ["alpha", "beta"]}],
            notes=["Custom note"],
        )
    )
    return report


def _xss_report() -> Report:
    payload = "<script>alert(1)</script>"
    image = '<img src=x onerror="alert(1)">'
    return Report(
        kind="weekly",
        property_id=f"sc-domain:{payload}",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline=payload,
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="headline",
                title=payload,
                summary=payload,
                rows=[
                    {
                        "metric": "clicks",
                        "current": 10.0,
                        "previous": 8.0,
                        "pct_delta": 25.0,
                        "significant": True,
                        "good": True,
                        "direction": "up",
                    }
                ],
                notes=[payload],
            ),
            ReportSection(
                key="opportunities",
                title="Opportunities",
                summary="",
                rows=[
                    {
                        "kind": "ctr_gap",
                        "query": payload,
                        "page": image,
                        "clicks": 1.0,
                        "impressions": 300.0,
                        "ctr": 0.0033,
                        "position": 10.0,
                        "expected_clicks": 5.0,
                        "evidence": {},
                    }
                ],
            ),
        ],
        limitations=[payload],
        warnings=[image],
    )


def _assert_no_schema_leakage(*documents: str) -> None:
    for document in documents:
        for token in _FORBIDDEN_TOKENS:
            assert token not in document
        assert not re.search(r"\bNone\b", document)
        assert not re.search(r"\bnan\b", document)
        assert "{" not in document and "}" not in document


def _collect(html_doc: str) -> _TagCollector:
    parser = _TagCollector()
    parser.feed(html_doc)
    parser.close()
    return parser


# -- weekly product text ---------------------------------------------------------------------


def test_weekly_text_uses_localized_rows_and_ignores_builder_prose() -> None:
    report = _weekly_full()
    text = render_text(report)

    assert report.headline not in text
    assert "BUILDER-SUMMARY-MUST-NOT-RENDER" not in text
    assert "BUILDER-NOTE-MUST-NOT-RENDER" not in text
    assert "Traffic by engine" not in text
    assert "Clicks" in text
    assert "1,234" in text
    assert "+18.4%" in text
    assert "1,042" in text
    assert "2.71%" in text  # CTR ratio rendered as a percent
    assert "8.4" in text
    assert "-0.6" in text
    assert "better" in text.lower()
    assert "significant" in text.lower()
    assert "significant:" not in text.lower()
    assert "fourth query must not render" not in text
    assert "previous_clicks" not in text
    assert "broken_canonical" not in text
    assert "thin_content" not in text
    assert "3 issues" in text
    assert "2 rules" in text  # unique rules, not individual findings
    assert "Top 3 by expected clicks" in text
    assert "top query rows" in text
    assert "sums of the fetched top rows" in text
    assert "Yandex Metrica" in text
    assert "0 clicks" not in text.lower()
    assert "clicks 0" not in text.lower()
    assert text.count("GSC data lags by two days.") == 1
    assert "Generated by lv24-seo" in text
    assert "sc-domain:example.com" in text
    _assert_no_schema_leakage(text)


def test_email_text_is_78_columns_and_terminal_text_is_100() -> None:
    report = _weekly_full()

    email_lines = render_email_text(report).splitlines()
    terminal_lines = render_text(report).splitlines()

    assert max(_display_width(line) for line in email_lines) <= 78
    assert max(_display_width(line) for line in terminal_lines) <= 100
    assert "\x1b[" not in "\n".join(terminal_lines)


def test_chinese_lines_fit_display_width_not_character_count() -> None:
    report = _weekly_zh_wide()

    email_lines = render_email_text(report, locale="zh").splitlines()
    terminal_lines = render_text(report, locale="zh").splitlines()

    assert max(_display_width(line) for line in email_lines) <= 78
    assert max(_display_width(line) for line in terminal_lines) <= 100
    # Han characters are two columns wide, so a 78-column line holds fewer
    # characters than a naive ``len`` check would allow.
    assert any(len(line) < _display_width(line) for line in email_lines)


def test_kpi_labels_and_subject_are_localized() -> None:
    report = _weekly_full()
    en = render_email_text(report, locale="en")
    ru = render_email_text(report, locale="ru")
    zh = render_email_text(report, locale="zh")

    assert "Clicks" in en and "Impressions" in en and "Avg. position" in en
    assert "Clicks" not in ru and "Impressions" not in ru and "Avg. position" not in ru
    assert "Clicks" not in zh and "Impressions" not in zh and "Avg. position" not in zh
    assert _CYRILLIC.search(ru)
    assert _CJK.search(zh)

    en_subject = render_subject(report, locale="en")
    ru_subject = render_subject(report, locale="ru")
    zh_subject = render_subject(report, locale="zh")

    assert en_subject != ru_subject != zh_subject
    assert _CYRILLIC.search(ru_subject)
    assert _CJK.search(zh_subject)
    for subject in (en_subject, ru_subject, zh_subject):
        assert subject
        assert "\n" not in subject
        assert len(subject) <= 78
    assert "critical" in en_subject.lower()


def test_unknown_kpi_is_never_zero_and_no_data_report_is_explicit() -> None:
    text = render_email_text(_weekly_no_data())

    assert "Clicks: 0" not in text
    assert "Clicks:" in text
    assert "No issue data" in text
    assert "No issues found" not in text
    assert "query data" in text.lower()
    assert "No data" in text
    _assert_no_schema_leakage(text)


def test_current_only_kpis_render_no_comparison_not_no_data() -> None:
    report = _weekly_no_comparison()
    text = render_email_text(report)

    assert "Clicks: 500 · No comparison" in text
    assert "Impressions: 9,000 · No comparison" in text
    assert "CTR: 5.55% · No comparison" in text
    assert "Avg. position: 7.5 · No comparison" in text
    assert "Clicks: No data" not in text
    assert "Previous 0" not in text
    assert "Worse" not in text
    assert "Better" not in text


def test_no_comparison_report_states_it_in_text_and_subject() -> None:
    report = _weekly_no_comparison()
    text = render_email_text(report)
    subject = render_subject(report)

    assert "500" in text
    assert "no comparison" in text.lower()
    assert "comparison" in subject.lower()
    assert "no data" not in subject.lower()


def test_opportunities_top_three_and_empty_states() -> None:
    text = render_email_text(_weekly_full())

    assert "Striking distance" in text
    assert "CTR gap" in text
    assert "Declining page" in text
    assert "Page not set" in text
    assert "Expected clicks: 36" in text
    assert "Expected clicks are a prioritisation estimate, not a traffic promise." in text

    metric_only = render_email_text(_weekly_metric_only())
    assert "No query data to evaluate opportunities." in metric_only
    assert "No opportunities found" not in metric_only

    queried = render_email_text(_weekly_query_rows_no_opportunities())
    assert "No opportunities found for this period." in queried


def test_opportunities_dedupe_by_query_and_page_keeping_highest_estimate() -> None:
    report = _weekly_full()
    rows = report.section("opportunities").rows
    rows.append({**rows[0], "expected_clicks": 5.0})

    text = render_email_text(report)

    assert text.count("seo tools") == 1
    assert "Expected clicks: 36" in text
    assert "Expected clicks: 5" not in text


def test_issues_never_print_rule_ids_and_zero_is_reported() -> None:
    zero = Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="No issues",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="issues",
                title="Issues by severity",
                summary="No issues reported",
                rows=[{"severity": "high", "count": 0, "rule_ids": ["hidden_rule"]}],
            )
        ],
    )

    text = render_email_text(zero)
    assert "No issues found" in text
    assert "hidden_rule" not in text


def test_russian_severity_labels_are_feminine_plural() -> None:
    text = render_email_text(_weekly_full(), locale="ru")

    assert "Критические: 1" in text
    assert "Высокие: 1" in text
    assert "Низкие: 1" in text
    assert "Критично" not in text
    assert "2 правила" in text


def test_sources_merge_traffic_and_source_rows_in_order() -> None:
    text = render_email_text(_weekly_full())

    positions = [
        text.index("Google Search Console"),
        text.index("Yandex Webmaster"),
        text.index("Yandex Metrica"),
        text.index("Bing"),
        text.index("Chrome UX Report"),
        text.index("PageSpeed Insights"),
    ]
    assert positions == sorted(positions)
    assert text.count("Data sources") == 1
    assert "Field data" in text
    assert "Lab data" in text
    assert "No data" in text
    assert "Yandex Metrica (Traffic data): Rows: 40" in text
    assert "Google Search Console (Traffic data · top query rows)" in text
    assert "Yandex Webmaster (Traffic data · top query rows)" in text


def test_gsc_is_listed_with_other_fallback_sources() -> None:
    text = render_email_text(_weekly_gsc_fallback_with_others())
    sources = text[text.index("Data sources") :]

    assert "Google Search Console" in sources
    assert "Yandex Webmaster" in sources
    assert "Yandex Metrica" in sources


def test_metrica_sessions_are_not_clicks() -> None:
    en = render_email_text(_weekly_with_sessions())
    zh = render_email_text(_weekly_with_sessions(), locale="zh")

    assert "Yandex Metrica (Traffic data): sessions 2,690" in en
    assert "0 clicks" not in en.lower()
    assert "会话数 2,690" in zh


def test_chinese_source_units_precede_values() -> None:
    text = render_email_text(_weekly_full(), locale="zh")

    assert "点击次数 1,234" in text
    assert "展示次数" in text
    assert "次点击 1,234" not in text
    assert "次展示" not in text


def test_russian_numbers_keep_nbsp_in_renderers() -> None:
    text = render_email_text(_weekly_full(), locale="ru")
    subject = render_subject(_weekly_clicks_only(), locale="ru")

    assert f"1{NBSP}234" in text
    assert f"45{NBSP}600" in text
    assert f"+18,4{NBSP}%" in text
    assert "+0,49 п.п." in text
    assert "1 234" not in text
    assert f"+25,0{NBSP}%" in subject


def test_limitation_codes_are_localized_and_unknown_codes_are_wrapped() -> None:
    report = _weekly_full()
    report.limitations = ["top_3000_queries", "rows_cap_50000", "brand_new_code"]

    en = render_email_text(report)
    assert "top_3000_queries" not in en
    assert "rows_cap_50000" not in en
    assert "Yandex covers only the top 3,000 queries." in en
    assert "The provider caps responses at 50,000 rows." in en
    assert "Other limitation (brand_new_code)" in en

    ru = render_email_text(report, locale="ru")
    assert "top_3000_queries" not in ru
    assert "rows_cap_50000" not in ru
    assert "Яндекс покрывает только топ-3 000 запросов." in ru
    assert "Провайдер ограничивает ответ 50 000 строками." in ru
    assert "Другое ограничение (brand_new_code)" in ru

    zh = render_email_text(report, locale="zh")
    assert "top_3000_queries" not in zh
    assert "Yandex 仅覆盖前 3,000 个查询。" in zh
    assert "其他限制（brand_new_code）" in zh


def test_known_limitation_code_and_its_prose_are_not_duplicated() -> None:
    report = _weekly_full()
    report.limitations = ["top_3000_queries", "Yandex covers only the top 3,000 queries."]

    assert render_email_text(report).count("top 3,000 queries") == 1


def test_builder_warnings_are_localized_and_provider_warnings_stay_raw() -> None:
    report = _weekly_full()
    report.warnings = [
        "no metrics for either period; delta rows are all zero",
        "previous period has no metrics; deltas cannot be significant",
        "no provider rows; traffic and opportunities are empty",
        "yandex: quota almost exhausted",
    ]

    en = render_email_text(report)
    assert "No metrics for either period; delta rows are all zero." in en
    assert "The previous period has no metrics" in en
    assert "No provider returned rows; traffic and opportunities are empty." in en
    assert "yandex: quota almost exhausted" in en

    ru = render_email_text(report, locale="ru")
    assert "За оба периода нет метрик" in ru
    assert "За предыдущий период нет метрик" in ru
    assert "Ни один провайдер не вернул строки" in ru

    zh = render_email_text(report, locale="zh")
    assert "两个周期均无指标" in zh
    assert "上一周期没有指标" in zh
    assert "所有提供商均未返回数据" in zh


def test_unknown_metric_rows_are_skipped_without_losing_has_data() -> None:
    report = _weekly_unknown_metric()
    text = render_email_text(report)

    assert "scroll_depth" not in text
    assert "metric:" not in text
    assert "current:" not in text
    assert "previous:" not in text
    assert "significant:" not in text
    assert "good:" not in text
    # A safe humanized label and free text may still be shown.
    assert "scroll depth: half of sessions scroll past the fold" in text
    assert "No search data" not in text
    assert "No previous period to compare." in text


def test_unknown_sections_are_kept_in_a_generic_fallback() -> None:
    report = _unknown_section_report()
    text = render_email_text(report)
    html_doc = render_html(report)
    markdown = render_markdown(report)

    assert "Custom block" in text
    assert "kept value" in text
    assert "alpha, beta" in text
    assert "Custom note" in text
    assert "Custom block" in html_doc
    assert "kept value" in html_doc
    assert "Custom block" in markdown
    # A nested mapping is omitted instead of being dumped as a Python dict.
    assert "{'a': 1}" not in text
    assert "{'a': 1}" not in html_doc


def test_non_weekly_reports_use_the_generic_renderer() -> None:
    report = Report(
        kind="audit",
        property_id="https://example.com/",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="Audit found two blocking issues",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="findings",
                title="Findings",
                summary="Two rules fired.",
                rows=[{"rule": "broken_canonical", "url": "https://example.com/a"}],
            )
        ],
    )

    text = render_email_text(report)
    html_doc = render_html(report)

    assert "Audit found two blocking issues" in text
    assert "Findings" in text
    assert "broken_canonical" in text
    assert "Findings" in html_doc
    assert (
        render_html(report, title="Audit <report>").count("<title>Audit &lt;report&gt;</title>")
        == 1
    )


@pytest.mark.parametrize("kind", ["audit", "indexing"])
def test_non_weekly_reports_never_claim_a_missing_comparison(kind: str) -> None:
    report = Report(
        kind=kind,
        property_id="https://example.com/",
        period_start="2026-11-03",
        period_end="2026-11-09",
        headline="A generic report headline",
        generated_at="2026-11-10T06:00:00Z",
        sections=[
            ReportSection(
                key="findings",
                title="Findings",
                summary="Two rules fired.",
                rows=[{"rule": "broken_canonical", "url": "https://example.com/a"}],
            )
        ],
    )

    for output in (render_text(report), render_email_text(report), render_html(report)):
        assert "No previous period" not in output
        assert "comparison" not in output.lower()
    assert "comparison" not in render_subject(report).lower()


# -- subject priorities ----------------------------------------------------------------------


def test_subject_priorities_are_localized_and_bounded() -> None:
    no_data = render_subject(_weekly_no_data())
    no_comparison = render_subject(_weekly_no_comparison())
    full = render_subject(_weekly_full())

    assert "search data" in no_data.lower()
    assert "comparison" in no_comparison.lower()
    assert "critical" in full.lower()

    without_issues = _weekly_full()
    without_issues.sections = [
        section for section in without_issues.sections if section.key != "issues"
    ]
    kpi_subject = render_subject(without_issues)
    assert "+18.4%" in kpi_subject
    assert len(kpi_subject) <= 78

    stable = _weekly_full()
    for row in stable.section("headline").rows:
        row["significant"] = False
        row["good"] = False
    stable.sections = [section for section in stable.sections if section.key != "issues"]
    assert "weekly" in render_subject(stable).lower()


def test_subject_prefers_critical_issues_over_missing_comparison() -> None:
    report = _weekly_no_comparison()
    report.sections.append(
        ReportSection(
            key="issues",
            title="Issues by severity",
            summary="",
            rows=[{"severity": "critical", "count": 2, "rule_ids": ["broken_canonical"]}],
        )
    )

    subject = render_subject(report)

    assert "critical" in subject.lower()
    assert "comparison" not in subject.lower()
    assert len(subject) <= 78


def test_subject_ctr_delta_uses_percentage_points() -> None:
    report = _weekly_ctr_only()

    assert render_subject(report, locale="en") == "example.com · CTR: +0.49 pp · 3–9 Nov"
    ru = render_subject(report, locale="ru")
    assert f"+0,49{NBSP}п.п." in ru or "+0,49 п.п." in ru
    assert "22.1" not in render_subject(report, locale="en")
    zh = render_subject(report, locale="zh")
    assert "个百分点" in zh


def test_subject_uses_ascii_minus_and_truncates_long_property() -> None:
    report = _weekly_full()
    report.property_id = "sc-domain:" + "very-long-property-name-" * 4 + ".example.com"
    for row in report.section("headline").rows:
        if row["metric"] == "clicks":
            row["current"] = 500.0
            row["previous"] = 1000.0
            row["pct_delta"] = -50.0
            row["significant"] = True
            row["good"] = False
    report.sections = [section for section in report.sections if section.key != "issues"]

    subject = render_subject(report)

    assert len(subject) <= 78
    assert "\u2212" not in subject
    assert "-50.0" in subject
    assert report.property_id not in subject


# -- HTML contract ---------------------------------------------------------------------------


def test_html_is_email_safe_standalone_document() -> None:
    html_doc = render_html(_weekly_full(), title="Weekly <report>")
    parser = _collect(html_doc)

    assert html_doc.startswith("<!DOCTYPE html>")
    assert '<html lang="en" dir="ltr">' in html_doc
    assert "<title>Weekly &lt;report&gt;</title>" in html_doc
    assert html_doc.count("<h1") == 1
    assert 'width="600"' not in html_doc
    assert 'style="width:100%;max-width:600px;border-collapse:separate;' in html_doc
    assert "display:none" in html_doc
    assert html_doc.index("display:none") < html_doc.index("LV24")
    assert "background-color:#080D12" in html_doc
    assert "#5FD4C4" in html_doc
    assert "font-family:" in html_doc
    assert "PingFang SC" in html_doc
    for tag in _HTML_FORBIDDEN_TAGS:
        assert tag not in parser.tags
        assert f"<{tag}" not in html_doc
    assert "@import" not in html_doc
    assert "url(" not in html_doc
    assert "src=" not in html_doc
    assert "href=" not in html_doc
    assert html_doc.count('style="') > 10
    assert "Weekly report" in html_doc
    assert "sc-domain:example.com" in html_doc
    _assert_no_schema_leakage(html_doc)


def test_html_uses_mobile_friendly_card_and_light_color_scheme() -> None:
    html_doc = render_html(_weekly_full())

    assert '<meta name="color-scheme" content="light only">' in html_doc
    assert '<meta name="supported-color-schemes" content="light">' in html_doc
    assert 'width="100%"' in html_doc
    assert html_doc.count('width="100%"') >= 2


def test_html_footer_uses_a_human_utc_timestamp() -> None:
    html_doc = render_html(_weekly_full())

    assert "Generated: 10 Nov 2026, 06:00 UTC" in html_doc
    assert "2026-11-10T06:00:00Z" not in html_doc


def test_html_lang_follows_locale() -> None:
    report = _weekly_full()

    assert '<html lang="en" dir="ltr">' in render_html(report, locale="en")
    assert '<html lang="ru" dir="ltr">' in render_html(report, locale="ru")
    assert '<html lang="zh-Hans" dir="ltr">' in render_html(report, locale="zh")


def test_html_escapes_all_user_controlled_fields_once() -> None:
    report = _xss_report()
    html_doc = render_html(report)
    text = render_text(report)
    markdown = render_markdown(report)
    escaped = "&lt;script&gt;alert(1)&lt;/script&gt;"

    assert "<script>" not in html_doc
    assert "<img" not in html_doc
    assert "<svg" not in html_doc
    assert escaped in html_doc
    assert "&amp;lt;" not in html_doc  # escaped exactly once
    assert "<script>" not in markdown
    assert escaped in markdown
    assert "\x1b" not in text
    assert "<script>alert(1)</script>" in text  # inert plain text


def test_html_structure_has_single_sources_section_and_no_data_issue_message() -> None:
    html_doc = render_html(_weekly_no_data())

    assert html_doc.count(">Data sources<") == 1
    assert "No issue data" in html_doc
    assert "No issues found" not in html_doc
    assert "Clicks" in html_doc


# -- markdown compatibility and determinism --------------------------------------------------


def test_render_markdown_structure_remains_canonical_english() -> None:
    markdown = render_markdown(_weekly_full())
    lines = markdown.splitlines()

    assert lines[0] == "# BUILDER-HEADLINE-MUST-NOT-RENDER"
    assert "## Headline" in markdown
    assert "| metric | current | previous |" in markdown
    assert "BUILDER-SUMMARY-MUST-NOT-RENDER" in markdown
    assert "## Data limitations" in markdown


def test_render_markdown_tables_escape_pipes_and_use_union_of_keys() -> None:
    report = _weekly_no_comparison()
    report.sections = [
        ReportSection(
            key="one",
            title="One",
            summary="",
            rows=[{"b": 1, "a": 2}, {"c": 3}],
        )
    ]

    markdown = render_markdown(report)

    assert "| b | a | c |" in markdown
    assert "| 1 | 2 |  |" in markdown
    assert "|  |  | 3 |" in markdown


def test_renderers_are_deterministic_and_use_generated_at_only() -> None:
    report = _weekly_full()

    outputs = {
        "text": render_text(report),
        "email": render_email_text(report),
        "html": render_html(report),
        "subject": render_subject(report),
        "markdown": render_markdown(report),
    }
    assert outputs["text"] == render_text(report)
    assert outputs["email"] == render_email_text(report)
    assert outputs["html"] == render_html(report)
    assert outputs["subject"] == render_subject(report)
    assert outputs["markdown"] == render_markdown(report)
    assert "2026" in outputs["text"]
    assert "2026" in outputs["html"]
    assert "Generated by lv24-seo" in outputs["html"]
    # No clock beyond report.generated_at: a second report with an empty value has no timestamp.
    no_time = _weekly_full()
    no_time.generated_at = ""
    assert "2026-11-10" not in render_text(no_time)
    assert "2026-11-10" not in render_html(no_time)


def test_cyrillic_and_cjk_survive_all_renderers() -> None:
    report = _weekly_full()

    for locale, pattern in (("ru", _CYRILLIC), ("zh", _CJK)):
        assert pattern.search(render_text(report, locale=locale))
        assert pattern.search(render_email_text(report, locale=locale))
        assert pattern.search(render_html(report, locale=locale))
        assert pattern.search(render_subject(report, locale=locale))


def test_rows_are_mappings_and_renderers_never_crash() -> None:
    report = _weekly_full()
    report.sections[0].rows.append({"metric": "clicks", "current": "not-a-number"})
    report.sections[1].rows.append({"engine": None, "rows": "42"})

    assert render_email_text(report)
    assert render_html(report)
    assert render_subject(report)


def test_text_and_html_have_no_mapping_repr() -> None:
    report = _weekly_full()
    report.warnings.append({"code": "x"})  # type: ignore[arg-type]

    text = render_email_text(report)
    html_doc = render_html(report)

    assert "{'code': 'x'}" not in text
    assert "{'code': 'x'}" not in html_doc
