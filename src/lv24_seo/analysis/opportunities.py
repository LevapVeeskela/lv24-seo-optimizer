"""Opportunity finder for Search Console query/page rows (Wave 4, WP4.2).

The finder is a pure function over GSC-shaped rows: it performs no network,
clock or filesystem access and is deterministic for a given input. It surfaces
three kinds of candidates:

``striking_distance``
    A query already ranks inside ``position_band`` and has enough impressions
    to deserve a title/snippet/content push. The modelled upside is
    ``impressions * ctr_lift``, i.e. what the extra clicks would be if the
    page earned ``ctr_lift`` of its impressions.
``ctr_gap``
    The observed CTR is below the CTR that a page in that position typically
    earns. The modelled upside is ``impressions * (expected_ctr - ctr)``.
``decay``
    The query (and page, when both reports carry one) lost a large share of
    its clicks versus ``previous``. The modelled upside is the lost traffic,
    ``previous_clicks - current_clicks``.

Expected CTR table
------------------
The lookup is a coarse industry-typical curve, documented here so every
number in the output can be reproduced:

===========  ==============
Position     Expected CTR
===========  ==============
1            0.28
2            0.15
3            0.11
4            0.08
5            0.06
6-10         0.04
11-20        0.02
else         0.01
===========  ==============

GSC positions are fractional averages. They are bucketed with ``ceil`` (e.g.
``3.2`` uses the position-4 row), which keeps the estimate conservative: a
higher rank number always carries an equal or lower expected CTR.

Matching, dedupe and honesty
----------------------------
* Rows without a query, or with malformed/negative metrics, are ignored.
* Impression-based rules require ``impressions >= min_impressions``; a row
  with ``position <= 0`` (unknown rank) is skipped by both of them.
* Decay needs at least 10 previous clicks. A current row with a page matches
  the previous row for that page, falling back to a query-level previous row
  (``page is None``); a current query-level row matches a query-level previous
  row, otherwise the previous per-page rows are summed into a rollup.
* At most one opportunity per ``(kind, query, page)`` is returned. Different
  kinds may describe the same query/page because they answer different
  questions.
* Results are sorted by ``expected_clicks`` (desc), then impressions (desc),
  with deterministic tie-breakers on kind/query/page, and capped by ``limit``.
* ``expected_clicks`` is a prioritisation estimate, never a promise of traffic.
  Fields that do not apply to a kind (for example CTR on a decay candidate
  without impression data) are reported as ``0.0``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_MIN_DECAY_CLICKS = 10.0
_CTR_BY_POSITION: dict[int, float] = {1: 0.28, 2: 0.15, 3: 0.11, 4: 0.08, 5: 0.06}
_CTR_TOP_10 = 0.04
_CTR_TOP_20 = 0.02
_CTR_FALLBACK = 0.01


@dataclass(frozen=True, slots=True)
class Opportunity:
    """One actionable candidate together with the evidence behind it.

    ``expected_clicks`` is a modelled upside used to rank candidates; it is an
    estimate, not a guarantee. ``page`` is ``None`` for query-level rows.
    """

    kind: str
    query: str
    page: str | None
    clicks: float
    impressions: float
    ctr: float
    position: float
    expected_clicks: float
    evidence: dict[str, float | str]


@dataclass(frozen=True, slots=True)
class _Metrics:
    """Parsed, validated metric row."""

    query: str
    page: str | None
    clicks: float | None
    impressions: float | None
    ctr: float | None
    position: float | None


@dataclass(frozen=True, slots=True)
class _Previous:
    """Previous-period row that is usable for decay matching."""

    clicks: float
    impressions: float | None


def find_opportunities(
    rows: Sequence[Mapping[str, float | str | None]],
    *,
    previous: Sequence[Mapping[str, float | str | None]] | None = None,
    min_impressions: float = 100,
    position_band: tuple[float, float] = (8.0, 20.0),
    ctr_lift: float = 0.03,
    decay_ratio: float = 0.5,
    limit: int = 20,
) -> list[Opportunity]:
    """Return ranked opportunity candidates for ``rows``.

    ``rows`` and ``previous`` are GSC-shaped mapping rows with ``query``,
    ``page``, ``clicks``, ``impressions``, ``ctr`` and ``position`` keys.
    ``previous`` is required for ``decay`` and ignored otherwise. A raise
    happens only for invalid configuration; bad rows are skipped silently so
    one malformed provider row cannot hide the rest of the report.
    """
    _validate(min_impressions, position_band, ctr_lift, decay_ratio)
    current = _parse_rows(rows)
    found: list[Opportunity] = []
    found.extend(_striking_distance(current, min_impressions, position_band, ctr_lift))
    found.extend(_ctr_gap(current, min_impressions))
    if previous is not None:
        found.extend(_decay(current, _parse_rows(previous), decay_ratio))
    unique = _dedupe(found)
    unique.sort(key=_sort_key)
    return unique[:limit] if limit > 0 else []


def _validate(
    min_impressions: float,
    position_band: tuple[float, float],
    ctr_lift: float,
    decay_ratio: float,
) -> None:
    """Reject configuration values that would make every estimate dishonest."""
    low, high = position_band
    if not math.isfinite(min_impressions) or min_impressions < 0:
        raise ValueError("min_impressions must be a finite number >= 0")
    if not (math.isfinite(low) and math.isfinite(high)) or low > high:
        raise ValueError("position_band must be a finite (low, high) pair with low <= high")
    if not math.isfinite(ctr_lift) or not 0 <= ctr_lift <= 1:
        raise ValueError("ctr_lift must be in [0, 1]")
    if not math.isfinite(decay_ratio) or not 0 <= decay_ratio <= 1:
        raise ValueError("decay_ratio must be in [0, 1]")


def _parse_rows(rows: Sequence[object]) -> list[_Metrics]:
    """Parse usable rows, dropping anything that cannot be trusted."""
    parsed: list[_Metrics] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        row = _parse_row(raw)
        if row is not None:
            parsed.append(row)
    return parsed


def _parse_row(raw: Mapping[str, float | str | None]) -> _Metrics | None:
    """Return a typed row, or ``None`` when the query is missing."""
    query_value = raw.get("query")
    if not isinstance(query_value, str):
        return None
    query = query_value.strip()
    if not query:
        return None
    clicks = _number(raw.get("clicks"))
    impressions = _number(raw.get("impressions"))
    ctr = _number(raw.get("ctr"))
    if ctr is not None and ctr > 1:
        ctr = None
    if ctr is None:
        ctr = _ratio(clicks, impressions)
    return _Metrics(
        query=query,
        page=_page(raw.get("page")),
        clicks=clicks,
        impressions=impressions,
        ctr=ctr,
        position=_number(raw.get("position")),
    )


def _number(value: object) -> float | None:
    """Coerce a provider value to a finite non-negative float, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _page(value: object) -> str | None:
    """Return a trimmed page URL, or ``None`` for a query-level row."""
    if not isinstance(value, str):
        return None
    page = value.strip()
    return page or None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Return ``numerator / denominator`` when it is a plausible CTR."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    ratio = numerator / denominator
    return ratio if ratio <= 1 else None


def _expected_ctr(position: float) -> float:
    """Return the documented expected CTR for a positive fractional position."""
    rank = math.ceil(position)
    exact = _CTR_BY_POSITION.get(rank)
    if exact is not None:
        return exact
    if 6 <= rank <= 10:
        return _CTR_TOP_10
    if 11 <= rank <= 20:
        return _CTR_TOP_20
    return _CTR_FALLBACK


def _striking_distance(
    rows: Sequence[_Metrics],
    min_impressions: float,
    position_band: tuple[float, float],
    ctr_lift: float,
) -> list[Opportunity]:
    """Find queries ranking just outside the top results with real demand."""
    low, high = position_band
    found: list[Opportunity] = []
    for row in rows:
        if row.position is None or row.impressions is None:
            continue
        if row.position <= 0 or not low <= row.position <= high:
            continue
        if row.impressions < min_impressions:
            continue
        found.append(
            _make_opportunity(
                "striking_distance",
                row,
                round(row.impressions * ctr_lift, 1),
                {
                    "position": row.position,
                    "impressions": row.impressions,
                    "position_band": f"{low:g}-{high:g}",
                    "ctr_lift": ctr_lift,
                },
            )
        )
    return found


def _ctr_gap(rows: Sequence[_Metrics], min_impressions: float) -> list[Opportunity]:
    """Find pages whose CTR sits below the position-based expectation."""
    found: list[Opportunity] = []
    for row in rows:
        if row.position is None or row.position <= 0:
            continue
        if row.impressions is None or row.impressions < min_impressions:
            continue
        if row.ctr is None:
            continue
        expected_ctr = _expected_ctr(row.position)
        if row.ctr >= expected_ctr:
            continue
        found.append(
            _make_opportunity(
                "ctr_gap",
                row,
                round(row.impressions * (expected_ctr - row.ctr), 1),
                {
                    "position": row.position,
                    "impressions": row.impressions,
                    "ctr": row.ctr,
                    "expected_ctr": expected_ctr,
                },
            )
        )
    return found


def _decay(
    rows: Sequence[_Metrics],
    previous_rows: Sequence[_Metrics],
    decay_ratio: float,
) -> list[Opportunity]:
    """Find queries that lost clicks versus the previous period."""
    exact: dict[tuple[str, str], _Previous] = {}
    query_level: dict[str, _Previous] = {}
    by_query: dict[str, list[_Previous]] = {}
    for row in previous_rows:
        if row.clicks is None:
            continue
        previous = _Previous(clicks=row.clicks, impressions=row.impressions)
        by_query.setdefault(row.query, []).append(previous)
        if row.page is None:
            query_level.setdefault(row.query, previous)
        else:
            exact.setdefault((row.query, row.page), previous)

    found: list[Opportunity] = []
    for row in rows:
        if row.clicks is None:
            continue
        match = _match_previous(row, exact, query_level, by_query)
        if match is None:
            continue
        previous_clicks, previous_impressions, matched_by = match
        if previous_clicks < _MIN_DECAY_CLICKS:
            continue
        if row.clicks > previous_clicks * decay_ratio:
            continue
        expected = round(previous_clicks - row.clicks, 1)
        if expected <= 0:
            continue
        evidence: dict[str, float | str] = {
            "previous_clicks": previous_clicks,
            "clicks": row.clicks,
            "decay_ratio": decay_ratio,
            "match": matched_by,
        }
        if previous_impressions is not None:
            evidence["previous_impressions"] = previous_impressions
        found.append(_make_opportunity("decay", row, expected, evidence))
    return found


def _match_previous(
    row: _Metrics,
    exact: Mapping[tuple[str, str], _Previous],
    query_level: Mapping[str, _Previous],
    by_query: Mapping[str, list[_Previous]],
) -> tuple[float, float | None, str] | None:
    """Match a current row to previous clicks, preferring the narrowest key."""
    if row.page is not None:
        page_match = exact.get((row.query, row.page))
        if page_match is not None:
            return page_match.clicks, page_match.impressions, "query+page"
        query_match = query_level.get(row.query)
        if query_match is not None:
            return query_match.clicks, query_match.impressions, "query"
        return None

    query_match = query_level.get(row.query)
    if query_match is not None:
        return query_match.clicks, query_match.impressions, "query"
    candidates = by_query.get(row.query)
    if not candidates:
        return None
    clicks = sum(candidate.clicks for candidate in candidates)
    impressions = _sum_impressions(candidates)
    return clicks, impressions, "query_rollup"


def _sum_impressions(rows: Sequence[_Previous]) -> float | None:
    """Sum impressions, or ``None`` when any contributor is unknown."""
    total = 0.0
    for row in rows:
        if row.impressions is None:
            return None
        total += row.impressions
    return total


def _make_opportunity(
    kind: str,
    row: _Metrics,
    expected_clicks: float,
    evidence: dict[str, float | str],
) -> Opportunity:
    """Build the public record from a parsed row and a modelled upside."""
    return Opportunity(
        kind=kind,
        query=row.query,
        page=row.page,
        clicks=row.clicks if row.clicks is not None else 0.0,
        impressions=row.impressions if row.impressions is not None else 0.0,
        ctr=row.ctr if row.ctr is not None else 0.0,
        position=row.position if row.position is not None else 0.0,
        expected_clicks=expected_clicks,
        evidence=evidence,
    )


def _dedupe(opportunities: Iterable[Opportunity]) -> list[Opportunity]:
    """Keep the first record per ``(kind, query, page)``."""
    seen: set[tuple[str, str, str | None]] = set()
    unique: list[Opportunity] = []
    for opportunity in opportunities:
        key = (opportunity.kind, opportunity.query, opportunity.page)
        if key in seen:
            continue
        seen.add(key)
        unique.append(opportunity)
    return unique


def _sort_key(opportunity: Opportunity) -> tuple[float, float, str, str, str]:
    """Sort by upside, then demand, then stable identity fields."""
    return (
        -opportunity.expected_clicks,
        -opportunity.impressions,
        opportunity.kind,
        opportunity.query,
        opportunity.page or "",
    )
