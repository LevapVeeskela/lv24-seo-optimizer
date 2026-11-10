"""Contract tests for the opportunity finder (Wave 4, WP4.2)."""

from __future__ import annotations

import pytest

from lv24_seo.analysis.opportunities import Opportunity, find_opportunities


def make_row(
    query: str,
    *,
    page: str | None = None,
    clicks: float = 0.0,
    impressions: float = 0.0,
    ctr: float | None = None,
    position: float = 0.0,
) -> dict[str, float | str | None]:
    """Build a GSC-shaped row; ``ctr`` is omitted when not supplied."""
    row: dict[str, float | str | None] = {
        "query": query,
        "page": page,
        "clicks": clicks,
        "impressions": impressions,
        "position": position,
    }
    if ctr is not None:
        row["ctr"] = ctr
    return row


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (8.0, 30.0),
        (20.0, 30.0),
        (7.99, None),
        (20.01, None),
    ],
)
def test_striking_distance_band_edges(position: float, expected: float | None) -> None:
    rows = [make_row("query", impressions=1000, ctr=0.5, position=position)]

    result = find_opportunities(rows)

    if expected is None:
        assert result == []
        return
    assert len(result) == 1
    [opportunity] = result
    assert opportunity.kind == "striking_distance"
    assert opportunity.expected_clicks == expected
    assert opportunity.evidence["position"] == position
    assert opportunity.evidence["impressions"] == 1000.0


def test_min_impressions_boundary_is_inclusive() -> None:
    rows = [make_row("query", impressions=100, ctr=0.5, position=10)]

    [opportunity] = find_opportunities(rows)

    assert opportunity.expected_clicks == 3.0


def test_rows_below_min_impressions_are_skipped() -> None:
    rows = [
        make_row("striking", impressions=99, ctr=0.5, position=10),
        make_row("gap", impressions=99, clicks=0, ctr=0.0, position=3),
    ]

    assert find_opportunities(rows) == []


def test_non_positive_position_is_ignored_by_band_and_gap_rules() -> None:
    rows = [make_row("unknown", impressions=1000, clicks=0, ctr=0.0, position=0)]

    assert find_opportunities(rows) == []


def test_ctr_gap_math_for_positions_three_and_fifteen() -> None:
    rows = [
        make_row("third", impressions=1000, clicks=50, ctr=0.05, position=3),
        make_row("fifteenth", impressions=500, clicks=2.5, ctr=0.005, position=15),
    ]

    result = find_opportunities(rows, position_band=(100.0, 200.0))

    assert [opportunity.query for opportunity in result] == ["third", "fifteenth"]
    third, fifteenth = result
    assert third.kind == "ctr_gap"
    assert third.expected_clicks == 60.0
    assert third.evidence["expected_ctr"] == 0.11
    assert third.evidence["ctr"] == 0.05
    assert fifteenth.expected_clicks == 7.5
    assert fifteenth.evidence["expected_ctr"] == 0.02
    assert fifteenth.evidence["position"] == 15.0


def test_decay_matches_by_query_and_page_and_by_query_alone() -> None:
    previous = [
        make_row("alpha", page="https://example.com/a", clicks=100),
        make_row("alpha", page="https://example.com/b", clicks=100),
        make_row("beta", clicks=100),
        make_row("gamma", page="https://example.com/a", clicks=100),
    ]
    current = [
        make_row("alpha", page="https://example.com/a", clicks=40),
        make_row("beta", clicks=40),
        make_row("gamma", page="https://example.com/b", clicks=40),
    ]

    result = find_opportunities(current, previous=previous)

    assert [(item.query, item.page, item.expected_clicks) for item in result] == [
        ("alpha", "https://example.com/a", 60.0),
        ("beta", None, 60.0),
    ]
    alpha, beta = result
    assert alpha.kind == "decay"
    assert alpha.evidence["previous_clicks"] == 100.0
    assert alpha.evidence["clicks"] == 40.0
    assert alpha.evidence["match"] == "query+page"
    assert beta.evidence["match"] == "query"


def test_decay_query_row_rolls_up_previous_page_rows() -> None:
    previous = [
        make_row("alpha", page="https://example.com/a", clicks=100, impressions=1000),
        make_row("alpha", page="https://example.com/b", clicks=100, impressions=500),
    ]
    current = [make_row("alpha", clicks=90, impressions=1500, ctr=0.06)]

    [opportunity] = find_opportunities(current, previous=previous)

    assert opportunity.kind == "decay"
    assert opportunity.expected_clicks == 110.0
    assert opportunity.evidence["match"] == "query_rollup"
    assert opportunity.evidence["previous_clicks"] == 200.0
    assert opportunity.evidence["previous_impressions"] == 1500.0


def test_decay_requires_previous_data_and_a_significant_drop() -> None:
    # No previous period means no decay candidates at all.
    assert find_opportunities([make_row("alpha", clicks=5)]) == []
    # Previous clicks below the 10-click floor are ignored.
    assert (
        find_opportunities([make_row("alpha", clicks=5)], previous=[make_row("alpha", clicks=5)])
        == []
    )
    # 60 clicks is more than half of 100, so it is not a decay.
    assert (
        find_opportunities([make_row("alpha", clicks=60)], previous=[make_row("alpha", clicks=100)])
        == []
    )
    [opportunity] = find_opportunities(
        [make_row("alpha", clicks=40)], previous=[make_row("alpha", clicks=100)]
    )
    assert opportunity.expected_clicks == 60.0


def test_duplicate_rows_are_deduped_per_kind() -> None:
    row = make_row("duplicate", clicks=10, impressions=1000, ctr=0.01, position=10)

    result = find_opportunities([row, dict(row), dict(row)])

    assert len(result) == 2
    assert {opportunity.kind for opportunity in result} == {"striking_distance", "ctr_gap"}
    assert all(opportunity.query == "duplicate" for opportunity in result)


def test_dedupe_keeps_distinct_pages() -> None:
    rows = [
        make_row("query", page="/a", impressions=1000, ctr=0.5, position=10),
        make_row("query", page="/b", impressions=1000, ctr=0.5, position=10),
    ]

    assert len(find_opportunities(rows)) == 2


def test_ordering_by_expected_clicks_and_limit() -> None:
    rows = [
        make_row("small", impressions=500, ctr=0.2, position=10),
        make_row("large", impressions=2000, ctr=0.4, position=10),
        make_row("medium", impressions=1000, ctr=0.2, position=10),
    ]

    result = find_opportunities(rows)

    assert [opportunity.query for opportunity in result] == ["large", "medium", "small"]
    assert [opportunity.expected_clicks for opportunity in result] == [60.0, 30.0, 15.0]
    assert find_opportunities(rows, limit=2) == result[:2]
    assert find_opportunities(rows, limit=0) == []
    assert find_opportunities(rows, limit=-3) == []


def test_equal_expected_clicks_are_ordered_by_impressions() -> None:
    rows = [
        make_row("top", impressions=1000, ctr=0.22, position=1),
        make_row("deep", impressions=2000, ctr=0.01, position=6),
    ]

    result = find_opportunities(rows)

    assert [opportunity.query for opportunity in result] == ["deep", "top"]
    assert [opportunity.expected_clicks for opportunity in result] == [60.0, 60.0]


def test_empty_input_returns_no_opportunities() -> None:
    assert find_opportunities([]) == []
    assert find_opportunities([], previous=[]) == []
    assert find_opportunities([], previous=None) == []


def test_rows_without_query_or_with_bad_numbers_are_ignored() -> None:
    rows: list[dict[str, float | str | None]] = [
        {"query": None, "impressions": 1000, "position": 10, "ctr": 0.0},
        {"query": "   ", "impressions": 1000, "position": 10, "ctr": 0.0},
        {"query": "bad", "impressions": "n/a", "position": 10, "ctr": 0.0},
        make_row("good", impressions=1000, ctr=0.5, position=10),
    ]

    result = find_opportunities(rows)

    assert [opportunity.query for opportunity in result] == ["good"]


def test_zero_impressions_and_missing_ctr_do_not_divide_by_zero() -> None:
    assert find_opportunities([make_row("zero", clicks=0, impressions=0, position=10)]) == []

    current: list[dict[str, float | str | None]] = [{"query": "alpha", "clicks": 10}]
    previous: list[dict[str, float | str | None]] = [{"query": "alpha", "clicks": 100}]

    [opportunity] = find_opportunities(current, previous=previous)

    assert isinstance(opportunity, Opportunity)
    assert opportunity.impressions == 0.0
    assert opportunity.ctr == 0.0
    assert opportunity.position == 0.0
    assert opportunity.expected_clicks == 90.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_impressions": -1},
        {"position_band": (20.0, 8.0)},
        {"ctr_lift": 1.5},
        {"decay_ratio": -0.1},
    ],
)
def test_invalid_configuration_raises(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        find_opportunities([], **kwargs)
