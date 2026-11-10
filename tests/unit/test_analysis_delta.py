"""Unit tests for the Wave 4 period delta engine (WP4.1)."""

from __future__ import annotations

import pytest

from lv24_seo.analysis.delta import (
    DeltaRow,
    DeltaThresholds,
    compute_delta,
    headline,
    summary,
)


def _by_metric(rows: list[DeltaRow]) -> dict[str, DeltaRow]:
    return {row.metric: row for row in rows}


def test_all_metrics_up_direction_and_goodness() -> None:
    rows = compute_delta(
        {"position": 12.0, "ctr": 0.06, "impressions": 1200.0, "clicks": 120.0},
        {"position": 10.0, "ctr": 0.05, "impressions": 1000.0, "clicks": 100.0},
    )
    by_metric = _by_metric(rows)

    assert [row.metric for row in rows] == ["clicks", "impressions", "ctr", "position"]
    assert by_metric["clicks"].pct_delta == pytest.approx(20.0)
    assert by_metric["impressions"].pct_delta == pytest.approx(20.0)
    assert by_metric["ctr"].pct_delta == pytest.approx(20.0)
    assert by_metric["position"].abs_delta == pytest.approx(2.0)
    assert [row.direction for row in rows] == ["up", "up", "up", "up"]
    assert [row.significant for row in rows] == [True, True, True, True]
    assert [row.good for row in rows] == [True, True, True, False]


def test_all_metrics_down_direction_and_goodness() -> None:
    rows = compute_delta(
        {"clicks": 80.0, "impressions": 800.0, "ctr": 0.04, "position": 8.0},
        {"clicks": 100.0, "impressions": 1000.0, "ctr": 0.05, "position": 10.0},
    )
    by_metric = _by_metric(rows)

    assert by_metric["clicks"].pct_delta == pytest.approx(-20.0)
    assert by_metric["impressions"].pct_delta == pytest.approx(-20.0)
    assert by_metric["ctr"].pct_delta == pytest.approx(-20.0)
    assert by_metric["position"].abs_delta == pytest.approx(-2.0)
    assert [row.direction for row in rows] == ["down", "down", "down", "down"]
    assert [row.significant for row in rows] == [True, True, True, True]
    assert [row.good for row in rows] == [False, False, False, True]


def test_exact_thresholds_are_significant() -> None:
    rows = _by_metric(
        compute_delta(
            {"clicks": 110.0, "impressions": 1150.0, "ctr": 0.11, "position": 11.5},
            {"clicks": 100.0, "impressions": 1000.0, "ctr": 0.10, "position": 10.0},
        )
    )

    assert rows["clicks"].pct_delta == pytest.approx(10.0)
    assert rows["impressions"].pct_delta == pytest.approx(15.0)
    assert rows["ctr"].pct_delta == pytest.approx(10.0)
    assert rows["position"].abs_delta == pytest.approx(1.5)
    assert all(row.significant for row in rows.values())


def test_just_below_thresholds_is_not_significant() -> None:
    rows = _by_metric(
        compute_delta(
            {"clicks": 109.9, "impressions": 1149.0, "ctr": 0.1099, "position": 11.4},
            {"clicks": 100.0, "impressions": 1000.0, "ctr": 0.10, "position": 10.0},
        )
    )

    assert not any(row.significant for row in rows.values())
    assert not any(row.good for row in rows.values())


def test_zero_previous_has_no_invented_percentage() -> None:
    rows = _by_metric(compute_delta({"clicks": 50.0}, {"clicks": 0.0}))

    clicks = rows["clicks"]
    assert clicks.pct_delta is None
    assert clicks.abs_delta == 50.0
    assert clicks.direction == "up"
    assert clicks.significant is False
    assert clicks.good is False
    assert rows["impressions"].pct_delta is None
    assert rows["impressions"].direction == "flat"


def test_missing_position_base_is_never_significant() -> None:
    position = _by_metric(compute_delta({"position": 5.0}, {}))["position"]

    assert position.pct_delta is None
    assert position.abs_delta == 5.0
    assert position.significant is False
    assert position.good is False


def test_missing_keys_are_treated_as_zero() -> None:
    rows = _by_metric(
        compute_delta(
            {"impressions": 100.0},
            {"clicks": 100.0, "impressions": 100.0},
        )
    )

    clicks = rows["clicks"]
    assert clicks.current == 0.0
    assert clicks.previous == 100.0
    assert clicks.abs_delta == -100.0
    assert clicks.pct_delta == pytest.approx(-100.0)
    assert clicks.significant is True
    assert clicks.good is False
    assert rows["impressions"].direction == "flat"
    assert rows["ctr"].pct_delta is None


def test_unknown_metrics_are_ignored() -> None:
    rows = compute_delta(
        {"clicks": 120.0, "bounce_rate": 55.0},
        {"clicks": 100.0, "bounce_rate": 40.0},
    )

    assert [row.metric for row in rows] == ["clicks", "impressions", "ctr", "position"]


def test_position_improves_when_it_decreases() -> None:
    better = _by_metric(compute_delta({"position": 8.0}, {"position": 10.0}))["position"]
    worse = _by_metric(compute_delta({"position": 12.0}, {"position": 10.0}))["position"]
    modest = _by_metric(compute_delta({"position": 9.0}, {"position": 10.0}))["position"]

    assert better.abs_delta == pytest.approx(-2.0)
    assert better.significant is True
    assert better.good is True

    assert worse.abs_delta == pytest.approx(2.0)
    assert worse.significant is True
    assert worse.good is False

    assert modest.abs_delta == pytest.approx(-1.0)
    assert modest.significant is False
    assert modest.good is False


def test_headline_formats_percentages_and_position() -> None:
    rows = compute_delta(
        {"clicks": 87.6, "impressions": 103.1, "ctr": 0.055, "position": 9.2},
        {"clicks": 100.0, "impressions": 100.0, "ctr": 0.05, "position": 10.0},
    )

    assert headline(rows) == "clicks -12.4%, impressions +3.1%, ctr +10.0%, position -0.8"


def test_headline_marks_missing_base_as_na_and_handles_empty() -> None:
    rows = compute_delta({"clicks": 50.0}, {})

    assert headline(rows) == "clicks n/a, impressions n/a, ctr n/a, position 0.0"
    assert headline([]) == ""


def test_headline_formats_zero_delta_as_zero() -> None:
    rows = compute_delta({"clicks": 100.0}, {"clicks": 100.0})

    assert headline(rows[:1]) == "clicks 0.0%"


def test_summary_counts_significant_good_and_bad() -> None:
    rows = compute_delta(
        {"clicks": 120.0, "impressions": 80.0, "ctr": 0.06, "position": 8.0},
        {"clicks": 100.0, "impressions": 100.0, "ctr": 0.05, "position": 10.0},
    )

    assert summary(rows) == {
        "changed": ["clicks", "impressions", "ctr", "position"],
        "significant": ["clicks", "impressions", "ctr", "position"],
        "good_count": 3,
        "bad_count": 1,
    }


def test_summary_excludes_flat_and_insignificant_rows() -> None:
    rows = compute_delta(
        {"clicks": 105.0, "impressions": 100.0, "ctr": 0.0505, "position": 9.5},
        {"clicks": 100.0, "impressions": 100.0, "ctr": 0.05, "position": 10.0},
    )

    assert summary(rows) == {
        "changed": ["clicks", "ctr", "position"],
        "significant": [],
        "good_count": 0,
        "bad_count": 0,
    }


def test_custom_thresholds_change_significance() -> None:
    thresholds = DeltaThresholds(
        clicks_pct=5.0,
        impressions_pct=5.0,
        ctr_pct=5.0,
        position_abs=0.5,
    )
    rows = _by_metric(
        compute_delta(
            {"clicks": 105.0, "impressions": 100.0, "ctr": 0.0505, "position": 9.5},
            {"clicks": 100.0, "impressions": 100.0, "ctr": 0.05, "position": 10.0},
            thresholds=thresholds,
        )
    )

    assert rows["clicks"].significant is True
    assert rows["clicks"].good is True
    assert rows["impressions"].significant is False
    assert rows["ctr"].significant is False
    assert rows["position"].significant is True
    assert rows["position"].good is True
