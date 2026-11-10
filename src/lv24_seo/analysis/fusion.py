"""Multi-engine fusion that keeps GSC, Yandex and Metrika views side by side.

The engines describe different things and must not be glued into one fake table:

* GSC can report a query together with its page, so its rows feed both ``query_totals`` and
  ``page_totals``;
* Yandex Webmaster reports queries only - it never joins a query to a page and covers just the
  top 3,000 queries - so its rows feed ``query_totals`` but never ``page_totals``;
* Metrika reports organic sessions per page (``ym:s:startURLPath``), not search clicks, so its
  visits feed ``page_totals`` under ``visits`` and never ``query_totals``.

Every gap stays visible: a metric nobody reported is ``None`` or absent instead of an invented
zero, and the report carries ``caveats`` plus per-engine ``limitations``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "CAVEAT_METRIKA_SESSIONS_NOT_CLICKS",
    "CAVEAT_QUERY_SAMPLING",
    "CAVEAT_YANDEX_NO_QUERY_PAGE_JOIN",
    "CAVEAT_YANDEX_TOP_3000_QUERIES",
    "EngineSummary",
    "FusionReport",
    "fuse_engines",
]

# Caveat strings are part of the report contract; report builders and skills match on them.
CAVEAT_QUERY_SAMPLING = "engines use different query sampling; totals are not comparable"
CAVEAT_YANDEX_NO_QUERY_PAGE_JOIN = "yandex: no query x page join"
CAVEAT_YANDEX_TOP_3000_QUERIES = "yandex: query report covers only the top 3,000 queries"
CAVEAT_METRIKA_SESSIONS_NOT_CLICKS = "metrika: sessions, not clicks"

# Stable limitation code (mirrors the provider limitation style; sessions are not clicks).
SESSIONS_NOT_CLICKS = "sessions_not_clicks"
# Codes mirrored from providers.base without importing the provider layer.
NO_QUERY_PAGE_JOIN = "no_query_page_join"
TOP_3000_QUERIES = "top_3000_queries"

# Deterministic metric order inside every totals bucket.
_METRIC_ORDER = ("clicks", "impressions", "visits")

_Row = Mapping[str, float | str | None]


@dataclass(frozen=True, slots=True)
class EngineSummary:
    """What one engine reported, with its own coverage caveats.

    ``clicks`` is non-optional by contract. Metrika has no click metric, so its summary keeps
    ``clicks=0.0`` and marks itself with ``sessions_not_clicks``; read ``page_totals`` for the
    real visit counts. Missing impressions, CTR and position stay ``None``.
    """

    engine: str
    clicks: float
    impressions: float | None
    ctr: float | None
    position: float | None
    rows: int
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FusionReport:
    """Per-engine summaries plus the only two honest cross-engine aggregations.

    ``page_totals`` maps a URL to the metrics that URL actually reported: GSC
    ``clicks``/``impressions`` and Metrika ``visits``. ``query_totals`` maps a query to summed
    GSC and Yandex clicks/impressions; Yandex contributes only its top 3,000 queries and never
    a page dimension. Both mappings are ordered by clicks descending, then key ascending.
    """

    engines: tuple[EngineSummary, ...]
    page_totals: dict[str, dict[str, float]]
    query_totals: dict[str, dict[str, float]]
    caveats: tuple[str, ...]


def fuse_engines(
    *,
    gsc: Sequence[Mapping[str, float | str | None]] = (),
    yandex: Sequence[Mapping[str, float | str | None]] = (),
    metrika: Sequence[Mapping[str, float | str | None]] = (),
) -> FusionReport:
    """Fuse already-normalised provider rows without inventing a query x page join.

    GSC rows are ``query|page|clicks|impressions|ctr|position``, Yandex rows are
    ``query|clicks|shows|position`` (``shows`` becomes ``impressions``) and Metrika rows are
    ``page|visits`` (``startURLPath`` is accepted as the page key). Numeric strings are parsed;
    blank or unparsable values count as missing.
    """
    caveats: list[str] = []
    if sum(bool(rows) for rows in (gsc, yandex, metrika)) >= 2:
        caveats.append(CAVEAT_QUERY_SAMPLING)
    if yandex:
        caveats.extend((CAVEAT_YANDEX_NO_QUERY_PAGE_JOIN, CAVEAT_YANDEX_TOP_3000_QUERIES))
    if metrika:
        caveats.append(CAVEAT_METRIKA_SESSIONS_NOT_CLICKS)

    engines: list[EngineSummary] = []
    if gsc:
        engines.append(
            _summarize(
                "gsc",
                gsc,
                click_keys=("clicks",),
                impression_keys=("impressions",),
                weight_keys=("impressions",),
                limitations=(),
            )
        )
    if yandex:
        engines.append(
            _summarize(
                "yandex",
                yandex,
                click_keys=("clicks",),
                impression_keys=("shows", "impressions"),
                weight_keys=("shows", "impressions"),
                limitations=(TOP_3000_QUERIES, NO_QUERY_PAGE_JOIN),
            )
        )
    if metrika:
        engines.append(
            _summarize(
                "metrika",
                metrika,
                click_keys=("clicks",),
                impression_keys=(),
                weight_keys=(),
                limitations=(SESSIONS_NOT_CLICKS,),
            )
        )

    return FusionReport(
        engines=tuple(engines),
        page_totals=_page_totals(gsc, metrika),
        query_totals=_query_totals(gsc, yandex),
        caveats=tuple(caveats),
    )


def _page_totals(
    gsc: Sequence[_Row],
    metrika: Sequence[_Row],
) -> dict[str, dict[str, float]]:
    """Page totals from rows that truly carry a page: GSC pages and Metrika start URLs."""
    totals = _Totals()
    for row in gsc:
        page = _text(row, "page")
        totals.add(page, "clicks", _metric(row, "clicks"))
        totals.add(page, "impressions", _metric(row, "impressions"))
    for row in metrika:
        page = _first_text(row, ("page", "startURLPath"))
        totals.add(page, "visits", _metric(row, "visits"))
    return totals.ordered()


def _query_totals(
    gsc: Sequence[_Row],
    yandex: Sequence[_Row],
) -> dict[str, dict[str, float]]:
    """Query totals from query-level rows only; Metrika has no query dimension."""
    totals = _Totals()
    for row in gsc:
        query = _text(row, "query")
        totals.add(query, "clicks", _metric(row, "clicks"))
        totals.add(query, "impressions", _metric(row, "impressions"))
    for row in yandex:
        query = _text(row, "query")
        totals.add(query, "clicks", _metric(row, "clicks"))
        totals.add(query, "impressions", _first_metric(row, ("shows", "impressions")))
    return totals.ordered()


class _Totals:
    """Sums available metrics per key; an absent metric stays absent instead of becoming 0."""

    __slots__ = ("_buckets",)

    def __init__(self) -> None:
        self._buckets: dict[str, dict[str, float]] = {}

    def add(self, key: str | None, metric: str, value: float | None) -> None:
        if key is None or value is None:
            return
        bucket = self._buckets.setdefault(key, {})
        bucket[metric] = bucket.get(metric, 0.0) + value

    def ordered(self) -> dict[str, dict[str, float]]:
        rows = [
            (key, {name: bucket[name] for name in _METRIC_ORDER if name in bucket})
            for key, bucket in self._buckets.items()
        ]
        rows.sort(key=lambda item: (-item[1].get("clicks", 0.0), item[0]))
        return dict(rows)


def _summarize(
    engine: str,
    rows: Sequence[_Row],
    *,
    click_keys: tuple[str, ...],
    impression_keys: tuple[str, ...],
    weight_keys: tuple[str, ...],
    limitations: tuple[str, ...],
) -> EngineSummary:
    clicks = _sum_metric(rows, click_keys)
    impressions = _optional_sum(rows, impression_keys)
    return EngineSummary(
        engine=engine,
        clicks=clicks,
        impressions=impressions,
        ctr=_ctr(clicks, impressions),
        position=_mean_position(rows, weight_keys),
        rows=len(rows),
        limitations=limitations,
    )


def _sum_metric(rows: Sequence[_Row], keys: tuple[str, ...]) -> float:
    total = 0.0
    for row in rows:
        value = _first_metric(row, keys)
        if value is not None:
            total += value
    return total


def _optional_sum(rows: Sequence[_Row], keys: tuple[str, ...]) -> float | None:
    if not keys:
        return None
    total = 0.0
    seen = False
    for row in rows:
        value = _first_metric(row, keys)
        if value is not None:
            total += value
            seen = True
    return total if seen else None


def _mean_position(rows: Sequence[_Row], weight_keys: tuple[str, ...]) -> float | None:
    """Impression/show-weighted average position, with a plain mean when no weight exists."""
    weighted_sum = 0.0
    weight_total = 0.0
    unweighted: list[float] = []
    for row in rows:
        position = _metric(row, "position")
        if position is None:
            continue
        unweighted.append(position)
        weight = _first_metric(row, weight_keys) if weight_keys else None
        if weight is not None and weight > 0:
            weighted_sum += position * weight
            weight_total += weight
    if weight_total > 0:
        return weighted_sum / weight_total
    if not unweighted:
        return None
    return sum(unweighted) / len(unweighted)


def _ctr(clicks: float, impressions: float | None) -> float | None:
    if impressions is None or impressions <= 0:
        return None
    return clicks / impressions


def _metric(row: _Row, key: str) -> float | None:
    return _as_float(row.get(key))


def _first_metric(row: _Row, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _as_float(row.get(key))
        if value is not None:
            return value
    return None


def _text(row: _Row, key: str) -> str | None:
    value = row.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _first_text(row: _Row, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = _text(row, key)
        if text is not None:
            return text
    return None


def _as_float(value: object) -> float | None:
    """Parse a provider metric; ``None`` marks missing, blank or unusable values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None
