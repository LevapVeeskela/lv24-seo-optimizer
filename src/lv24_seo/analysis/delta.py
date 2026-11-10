"""Period-over-period delta engine (Wave 4, WP4.1).

Compares two metric snapshots — ``clicks``, ``impressions``, ``ctr`` and
``position`` — and reports which movements are meaningful under caller-tunable
thresholds.

Honesty rules:

- percentages are never invented: when the previous value is zero,
  ``pct_delta`` is ``None`` and the row cannot be significant;
- metrics missing from either snapshot are treated as zero;
- unknown metric keys are ignored.

SEO semantics: clicks, impressions and CTR are better when they grow, while
position is better when it falls (rank 3 beats rank 5). A row is ``good`` only
when it is both significant and moves in the desirable direction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["DeltaRow", "DeltaThresholds", "compute_delta", "headline", "summary"]

_METRICS: tuple[str, ...] = ("clicks", "impressions", "ctr", "position")
_IMPROVEMENT_DIRECTION: dict[str, str] = {
    "clicks": "up",
    "impressions": "up",
    "ctr": "up",
    "position": "down",
}
_PCT_PRECISION = 4
_THRESHOLD_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class DeltaThresholds:
    """Significance limits; a delta exactly at a limit counts as significant."""

    clicks_pct: float = 10.0
    impressions_pct: float = 15.0
    position_abs: float = 1.5
    ctr_pct: float = 10.0


@dataclass(frozen=True, slots=True)
class DeltaRow:
    """One metric compared between the current and the previous period."""

    metric: str
    current: float
    previous: float
    abs_delta: float
    pct_delta: float | None
    significant: bool
    direction: str
    good: bool


def compute_delta(
    current: Mapping[str, float],
    previous: Mapping[str, float],
    *,
    thresholds: DeltaThresholds | None = None,
) -> list[DeltaRow]:
    """Return one :class:`DeltaRow` per known metric, in canonical order."""
    limits = thresholds or DeltaThresholds()
    rows: list[DeltaRow] = []

    for metric in _METRICS:
        current_value = float(current.get(metric, 0.0))
        previous_value = float(previous.get(metric, 0.0))
        abs_delta = current_value - previous_value
        pct_delta = _pct_delta(current_value, previous_value)
        direction = _direction(abs_delta)
        significant = _is_significant(metric, previous_value, abs_delta, pct_delta, limits)
        rows.append(
            DeltaRow(
                metric=metric,
                current=current_value,
                previous=previous_value,
                abs_delta=abs_delta,
                pct_delta=pct_delta,
                significant=significant,
                direction=direction,
                good=significant and direction == _IMPROVEMENT_DIRECTION[metric],
            )
        )

    return rows


def headline(rows: Sequence[DeltaRow]) -> str:
    """Format rows as one line, e.g. ``clicks -12.4%, position -0.8``.

    Percentage metrics without a previous base are printed as ``n/a`` instead of
    inventing a percentage. Position is shown as an absolute rank delta. An empty
    input produces an empty string.
    """
    return ", ".join(_format_row(row) for row in rows)


def summary(rows: Sequence[DeltaRow]) -> dict[str, Any]:
    """Machine-readable rollup of metric names and significant good/bad counts."""
    changed = [row.metric for row in rows if row.direction != "flat"]
    significant = [row.metric for row in rows if row.significant]
    return {
        "changed": changed,
        "significant": significant,
        "good_count": sum(1 for row in rows if row.significant and row.good),
        "bad_count": sum(1 for row in rows if row.significant and not row.good),
    }


def _pct_delta(current: float, previous: float) -> float | None:
    """Relative change in percent, or ``None`` when there is no base."""
    if previous == 0:
        return None
    rounded = round((current - previous) / previous * 100.0, _PCT_PRECISION)
    return 0.0 if rounded == 0 else rounded


def _direction(abs_delta: float) -> str:
    if abs_delta > 0:
        return "up"
    if abs_delta < 0:
        return "down"
    return "flat"


def _is_significant(
    metric: str,
    previous: float,
    abs_delta: float,
    pct_delta: float | None,
    thresholds: DeltaThresholds,
) -> bool:
    if previous == 0:
        return False
    if metric == "position":
        return _meets(abs(abs_delta), thresholds.position_abs)
    if pct_delta is None:
        return False
    return _meets(abs(pct_delta), _threshold_for(metric, thresholds))


def _threshold_for(metric: str, thresholds: DeltaThresholds) -> float:
    if metric == "clicks":
        return thresholds.clicks_pct
    if metric == "impressions":
        return thresholds.impressions_pct
    return thresholds.ctr_pct


def _meets(value: float, threshold: float) -> bool:
    """Compare with an epsilon so arithmetically exact boundaries stay exact."""
    return value >= threshold or math.isclose(
        value, threshold, rel_tol=0.0, abs_tol=_THRESHOLD_EPSILON
    )


def _format_row(row: DeltaRow) -> str:
    if row.metric == "position":
        return f"position {_signed(row.abs_delta)}"
    if row.pct_delta is None:
        return f"{row.metric} n/a"
    return f"{row.metric} {_signed(row.pct_delta)}%"


def _signed(value: float) -> str:
    """One decimal with an explicit plus for positive values; no ``-0.0``."""
    if value == 0:
        value = 0.0
    return f"{value:+.1f}" if value > 0 else f"{value:.1f}"
