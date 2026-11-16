"""Unit tests for the attributed change digest (Phase 2, F1)."""

from __future__ import annotations

from datetime import date

import pytest

from lv24_seo.analysis.digest import build_digest

_PERIOD_START = date(2026, 11, 4)
_PERIOD_END = date(2026, 11, 10)


def _delta(
    metric: str = "clicks",
    *,
    current: float = 80.0,
    previous: float = 100.0,
    pct_delta: float | None = -20.0,
    significant: bool = True,
    direction: str = "down",
    good: bool | None = False,
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


def _issue(
    rule_id: str = "noindex-on-200",
    *,
    url: str = "https://example.com/a",
    severity: str = "high",
    change: str | None = "new",
    state: str = "open",
    first_seen: str = "2026-11-05",
    last_seen: str = "2026-11-09",
) -> dict[str, object]:
    row: dict[str, object] = {
        "rule_id": rule_id,
        "url": url,
        "severity": severity,
        "state": state,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }
    if change is not None:
        row["change"] = change
    return row


def _digest(**kwargs: object):
    base: dict[str, object] = {
        "deltas": [_delta()],
        "period_start": _PERIOD_START,
        "period_end": _PERIOD_END,
    }
    base.update(kwargs)
    return build_digest(**base)  # type: ignore[arg-type]


def test_no_material_change_produces_no_items() -> None:
    digest = _digest(deltas=[_delta(significant=False, pct_delta=-1.0)])

    assert digest.items == []
    assert digest.total == 0
    assert digest.truncated is False
    assert digest.data_age is None
    assert digest.limitations == []


def test_metric_change_with_matching_issue_is_attributed() -> None:
    digest = _digest(issue_changes=[_issue()])

    assert digest.total == 1
    item = digest.items[0]
    assert item.metric == "clicks"
    assert item.cause == "new_crawl_issue"
    assert item.confidence == "high"
    assert item.evidence == (
        {
            "kind": "crawl_issue",
            "rule_id": "noindex-on-200",
            "url": "https://example.com/a",
            "severity": "high",
            "change": "new",
            "first_seen": "2026-11-05",
            "last_seen": "2026-11-09",
        },
    )
    assert "noindex-on-200" in item.next_action
    assert "clicks" in item.next_action
    assert digest.data_age == "2026-11-09"
    assert digest.limitations == []


def test_metric_change_without_evidence_is_unexplained() -> None:
    digest = _digest()

    item = digest.items[0]
    assert item.cause == "unexplained"
    assert item.confidence == "low"
    assert item.evidence == ()
    assert "no cause is attributed" in item.next_action
    assert digest.limitations == ["digest_no_evidence"]


def test_unmatched_evidence_is_reported_as_a_limitation() -> None:
    # A resolved issue cannot explain a worsening metric.
    digest = _digest(issue_changes=[_issue(change="resolved")])

    assert digest.items[0].cause == "unexplained"
    assert digest.limitations == ["digest_no_matching_evidence"]


def test_resolved_issue_explains_an_improvement() -> None:
    digest = _digest(
        deltas=[_delta(current=120.0, previous=100.0, pct_delta=20.0, direction="up", good=True)],
        issue_changes=[_issue(change="resolved", state="fixed")],
    )

    item = digest.items[0]
    assert item.cause == "resolved_crawl_issue"
    assert item.confidence == "medium"
    assert item.evidence[0]["change"] == "resolved"
    assert digest.limitations == []


def test_state_fixed_marks_an_issue_resolved_without_an_explicit_change() -> None:
    digest = _digest(
        deltas=[_delta(current=120.0, previous=100.0, pct_delta=20.0, direction="up", good=True)],
        issue_changes=[_issue(change=None, state="fixed")],
    )

    assert digest.items[0].cause == "resolved_crawl_issue"


def test_issue_first_seen_after_the_window_is_not_a_cause() -> None:
    digest = _digest(issue_changes=[_issue(first_seen="2026-11-20", last_seen="2026-11-20")])

    assert digest.items[0].cause == "unexplained"
    assert digest.limitations == ["digest_no_matching_evidence"]


def test_rule_without_a_metric_claim_is_never_a_cause() -> None:
    digest = _digest(issue_changes=[_issue(rule_id="image-alt-missing", severity="low")])

    assert digest.items[0].cause == "unexplained"


def test_unknown_rule_is_only_low_confidence_evidence() -> None:
    digest = _digest(issue_changes=[_issue(rule_id="future-rule", severity="critical")])

    item = digest.items[0]
    assert item.cause == "new_crawl_issue"
    assert item.confidence == "low"


def test_medium_severity_new_issue_is_medium_confidence() -> None:
    digest = _digest(issue_changes=[_issue(severity="medium")])

    assert digest.items[0].confidence == "medium"


def test_indexing_event_explains_a_worsening_metric() -> None:
    digest = _digest(
        indexing_events=[
            {"url": "https://example.com/a", "reason": "noindex", "date": "2026-11-06"}
        ]
    )

    item = digest.items[0]
    assert item.cause == "indexing_event"
    assert item.confidence == "medium"
    assert item.evidence[0]["kind"] == "indexing_event"
    assert digest.data_age == "2026-11-06"


def test_page_state_explains_a_worsening_metric() -> None:
    digest = _digest(
        pages=[
            {
                "url": "https://example.com/a",
                "status": 503,
                "index_state": "indexed",
                "canonical": "https://example.com/a",
                "last_seen": "2026-11-09",
            }
        ]
    )

    item = digest.items[0]
    assert item.cause == "page_state"
    assert item.confidence == "medium"
    assert item.evidence[0]["field"] == "status"
    assert item.evidence[0]["value"] == 503


def test_new_issue_outranks_page_state_and_indexing_event() -> None:
    digest = _digest(
        issue_changes=[_issue()],
        indexing_events=[{"url": "https://example.com/a", "reason": "noindex"}],
        pages=[{"url": "https://example.com/a", "status": 500}],
    )

    assert digest.items[0].cause == "new_crawl_issue"


def test_malformed_rows_are_counted_not_guessed() -> None:
    digest = _digest(
        issue_changes=[
            {"rule_id": "", "url": "https://example.com/a"},
            {"rule_id": "noindex-on-200", "url": "  "},
            _issue(),
        ],
        indexing_events=[{"url": "https://example.com/a"}, {"reason": "noindex"}],
        pages=[{"status": 500}],
    )

    assert digest.malformed_rows == 5
    assert digest.items[0].cause == "new_crawl_issue"
    assert "digest_evidence_malformed" in digest.limitations


def test_no_comparison_reports_the_missing_window() -> None:
    digest = _digest(deltas=[], has_comparison=False, issue_changes=[_issue()])

    assert digest.items == []
    assert digest.limitations == ["digest_no_comparison"]


def test_items_and_evidence_are_capped() -> None:
    digest = _digest(
        deltas=[
            _delta("clicks", pct_delta=-20.0),
            _delta("impressions", current=800.0, previous=1000.0, pct_delta=-20.0),
            _delta("position", current=8.0, previous=6.0, pct_delta=33.3, direction="up"),
        ],
        issue_changes=[_issue(url=f"https://example.com/{index}") for index in range(5)],
        limit=2,
        evidence_limit=2,
    )

    assert digest.total == 3
    assert len(digest.items) == 2
    assert all(len(item.evidence) == 2 for item in digest.items)
    assert digest.truncated is True


def test_to_dict_shape_is_stable() -> None:
    payload = _digest(issue_changes=[_issue()]).to_dict()

    assert set(payload) == {
        "items",
        "total",
        "truncated",
        "data_age",
        "malformed_rows",
        "limitations",
    }
    item = payload["items"][0]
    assert set(item) == {"metric", "change", "cause", "confidence", "evidence", "next_action"}
    assert set(item["change"]) == {
        "current",
        "previous",
        "abs_delta",
        "pct_delta",
        "direction",
        "good",
    }


def test_flat_change_is_skipped() -> None:
    digest = _digest(deltas=[_delta(direction="flat", pct_delta=0.0, current=100.0)])

    assert digest.items == []


@pytest.mark.parametrize("bad", [None, "clicks", 3.5, {"metric": "clicks"}])
def test_malformed_delta_rows_are_ignored(bad: object) -> None:
    digest = build_digest(
        deltas=[bad],  # type: ignore[list-item]
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
    )

    assert digest.items == []
    assert digest.total == 0
