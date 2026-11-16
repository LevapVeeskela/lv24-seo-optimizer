"""Unit tests for Core Web Vitals by URL template (Phase 2, F8/F13)."""

from __future__ import annotations

from typing import Any

import pytest

from lv24_seo.analysis.cwv import build_cwv, normalize_template

_DATE = "2026-11-01"


def _row(
    path: str = "/blog/1",
    *,
    device: str = "phone",
    metric: str = "lcp",
    p75: float = 3000.0,
    previous: float | None = None,
    date: str = _DATE,
    scope: str = "url",
    kind: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "url": f"https://example.com{path}",
        "device": device,
        "metric": metric,
        "p75": p75,
        "date": date,
        "scope": scope,
        "source": "crux",
    }
    if previous is not None:
        row["previous_p75"] = previous
    if kind is not None:
        row["kind"] = kind
    return row


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/blog/42/", "/blog/{id}"),
        ("https://example.com/blog/post-123", "/blog/post-{id}"),
        ("https://example.com/a/550e8400-e29b-41d4-a716-446655440000", "/a/{id}"),
        ("https://example.com/a/deadbeef12", "/a/{id}"),
        ("https://example.com/", "/"),
        ("https://example.com/a?x=1#frag", "/a"),
        ("/blog/7", "/blog/{id}"),
        ("", ""),
    ],
)
def test_normalize_template_replaces_ids(url: str, expected: str) -> None:
    assert normalize_template(url) == expected


def test_rows_group_by_template_and_keep_the_worst_page_p75() -> None:
    report = build_cwv(
        [
            _row("/blog/1", p75=3000.0),
            _row("/blog/2", p75=2000.0),
            _row("/blog/3", p75=2500.0),
            _row("/blog/3", metric="cls", p75=0.2),
        ]
    )

    assert report.total == 1
    assert report.truncated is False
    template = report.templates[0]
    assert template.template == "/blog/{id}"
    assert template.device == "phone"
    assert template.urls == 3
    assert template.sample == 4
    assert template.coverage == pytest.approx(2 / 3, abs=0.0001)
    metrics = {metric.metric: metric for metric in template.metrics}
    assert metrics["lcp"].p75 == 3000.0
    assert metrics["lcp"].sample == 3
    assert metrics["cls"].p75 == 0.2
    assert metrics["cls"].unit == "score"
    assert metrics["cls"].status == "needs_improvement"
    # No baseline: comparison fields stay unknown, never zero.
    assert metrics["lcp"].previous_p75 is None
    assert metrics["lcp"].delta is None
    assert metrics["lcp"].delta_pct is None
    assert metrics["lcp"].regressed is False
    assert "cwv_no_comparison" in report.limitations
    assert "cwv_p75_worst_page" in report.limitations


def test_devices_are_split_not_merged() -> None:
    report = build_cwv(
        [
            _row("/p/1", device="phone", p75=5000.0),
            _row("/p/1", device="desktop", p75=2000.0),
            _row("/p/2", device="mobile", p75=4500.0),
        ]
    )

    assert report.total == 2
    by_device = {template.device: template for template in report.templates}
    assert set(by_device) == {"phone", "desktop"}
    assert by_device["phone"].urls == 2
    assert by_device["phone"].metrics[0].p75 == 5000.0
    assert by_device["desktop"].metrics[0].p75 == 2000.0


def test_empty_rows_are_a_limitation_not_a_zero() -> None:
    report = build_cwv([])

    assert report.templates == []
    assert report.total == 0
    assert report.truncated is False
    assert report.data_age is None
    assert report.limitations == ["cwv_no_rows"]


def test_missing_template_and_device_are_limitations() -> None:
    report = build_cwv(
        [
            {"metric": "lcp", "p75": 3000.0, "device": "phone"},
            {"url": "https://example.com/blog/1", "metric": "lcp", "p75": 3000.0},
            {
                "url": "https://example.com/",
                "scope": "origin",
                "device": "phone",
                "metric": "lcp",
                "p75": 3000.0,
            },
        ]
    )

    assert report.templates == []
    assert "cwv_template_missing" in report.limitations
    assert "cwv_device_missing" in report.limitations


def test_regressed_template_is_ranked_first_and_small_changes_are_not_regressions() -> None:
    report = build_cwv(
        [
            _row("/blog/1", p75=3000.0, previous=2000.0),
            _row("/news/1", p75=3100.0, previous=3000.0),
            _row("/docs/1", p75=2000.0, previous=2500.0),
        ]
    )

    assert next(template.template for template in report.templates) == "/blog/{id}"
    assert report.regressed_template == "/blog/{id}"
    first = report.templates[0]
    assert first.regressed is True
    assert first.regression_pct == 50.0
    assert first.regression_metric == "lcp"
    metrics = {metric.metric: metric for metric in first.metrics}
    assert metrics["lcp"].delta == 1000.0
    assert metrics["lcp"].delta_pct == 50.0
    assert metrics["lcp"].regressed is True
    assert all(template.regressed is False for template in report.templates[1:])
    assert "cwv_no_comparison" not in report.limitations


def test_absolute_regression_from_a_zero_baseline_is_kept() -> None:
    report = build_cwv([_row("/p/1", metric="cls", p75=0.05, previous=0.0)])

    metric = report.templates[0].metrics[0]
    assert metric.delta == 0.05
    assert metric.delta_pct is None
    assert metric.regressed is True
    assert report.regressed_template == "/p/{id}"


def test_limit_caps_templates_and_reports_the_full_total() -> None:
    rows = [_row(f"/t{index}/1", p75=float(index * 1000)) for index in range(1, 8)]
    report = build_cwv(rows, limit=5)

    assert report.total == 7
    assert report.truncated is True
    assert len(report.templates) == 5


def test_data_age_is_the_oldest_date_among_shown_templates() -> None:
    report = build_cwv(
        [
            _row("/a/1", date="2026-10-05"),
            _row("/b/1", date="2026-10-01"),
        ]
    )

    assert report.data_age == "2026-10-01"


def test_invalid_values_are_ignored_not_invented() -> None:
    report = build_cwv(
        [
            {"url": "https://example.com/a/1", "device": "phone", "metric": "lcp"},
            {
                "url": "https://example.com/a/2",
                "device": "phone",
                "metric": "lcp",
                "p75": "nope",
            },
            {
                "url": "https://example.com/a/3",
                "device": "phone",
                "metric": "lcp",
                "p75": -1.0,
            },
            {
                "url": "https://example.com/a/4",
                "device": "phone",
                "metric": "cls",
                "p75": 60.0,
            },
            {
                "url": "https://example.com/a/5",
                "device": "phone",
                "metric": "scroll_depth",
                "p75": 0.5,
            },
        ]
    )

    assert report.templates == []
    assert report.ignored_rows == 5
    assert "cwv_values_ignored" in report.limitations


def test_lab_rows_are_never_merged_with_field_data() -> None:
    report = build_cwv(
        [
            _row("/a/1", p75=3000.0, kind="lab"),
            _row("/a/1", p75=2000.0, kind="field"),
        ]
    )

    assert report.total == 1
    assert report.templates[0].metrics[0].p75 == 2000.0
    assert "cwv_lab_ignored" in report.limitations


def test_partial_baseline_is_reported_as_partial_comparison() -> None:
    report = build_cwv(
        [
            _row("/p/1", p75=3000.0, previous=2500.0),
            _row("/p/2", p75=2800.0),
        ]
    )

    assert report.templates[0].compared == 1
    assert report.templates[0].sample == 2
    assert "cwv_partial_comparison" in report.limitations
    assert "cwv_no_comparison" not in report.limitations


def test_full_core_coverage_is_one() -> None:
    report = build_cwv(
        [
            _row("/a/1", metric="lcp", p75=2000.0),
            _row("/a/1", metric="inp", p75=150.0),
            _row("/a/1", metric="cls", p75=0.05),
        ]
    )

    assert report.templates[0].coverage == 1.0
    assert {metric.status for metric in report.templates[0].metrics} == {"good"}
