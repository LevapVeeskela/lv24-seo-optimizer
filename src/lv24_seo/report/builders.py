"""Report builders for weekly, indexing and opportunities reports (Wave 5).

WP5.1 composes the weekly review; WP5.5 adds the indexing and opportunities
reports. Builders are pure: they receive already-fetched rows and never touch
the network, the database or the clock beyond the ``generated_at`` default.

Honesty rules:

* every number comes from the input rows or from the analysis layer;
* an engine without rows is named as missing in ``sources`` instead of being
  rendered as a zero;
* ``limitations`` are copied verbatim, never guessed;
* ``expected_clicks`` is labelled as a prioritisation estimate, not a promise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from lv24_seo.analysis.delta import DeltaRow, DeltaThresholds, compute_delta, headline, summary
from lv24_seo.analysis.fusion import EngineSummary, FusionReport, fuse_engines
from lv24_seo.analysis.indexing import Bucket, triage
from lv24_seo.analysis.opportunities import Opportunity, find_opportunities
from lv24_seo.report.models import Report, ReportSection

__all__ = ["build_indexing", "build_opportunities", "build_weekly"]

_ENGINES: tuple[str, ...] = ("gsc", "yandex", "metrika")
_SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
_UNKNOWN_SEVERITY = "unknown"

_WEEKLY_OPPORTUNITIES = 5
_INDEXING_EXAMPLES = 5

_BAD_DELTA_PREFIX = "⚠ "
_ESTIMATE_NOTE = "expected_clicks is a prioritisation estimate, not a traffic promise"
_VERIFICATION_NOTE = (
    "Re-run indexing triage for the same period and engines, then compare bucket counts: "
    "a bucket should shrink only after the fix is deployed and the URLs are recrawled."
)


def build_weekly(
    *,
    property_id: str,
    period_start: date,
    period_end: date,
    current: Mapping[str, float],
    previous: Mapping[str, float],
    gsc_rows: Sequence[Mapping[str, Any]] = (),
    gsc_previous_rows: Sequence[Mapping[str, Any]] = (),
    gsc_previous_complete: bool = False,
    yandex_rows: Sequence[Mapping[str, Any]] = (),
    metrika_rows: Sequence[Mapping[str, Any]] = (),
    issues: Sequence[Mapping[str, Any]] = (),
    extra_engines: Sequence[Mapping[str, Any]] = (),
    limitations: Sequence[str] = (),
    thresholds: DeltaThresholds | None = None,
    generated_at: datetime | None = None,
) -> Report:
    """Build the weekly review: deltas, traffic, opportunities, issues and sources.

    ``current``/``previous`` carry one engine's comparable window totals (GSC):
    the headline never merges engines with different sampling or windows.
    ``gsc_rows`` are the current-window ``query``/``page`` rows for fusion and
    opportunities; ``gsc_previous_rows`` enables decay matching, and
    ``gsc_previous_complete`` must be True only when both windows share the same
    cap without truncation. ``extra_engines`` are ``{"engine", "kind", "rows",
    "limitations"}`` mappings (Bing traffic, CrUX field, PSI lab) shown
    per-engine, never merged. ``issues`` are stored crawl findings, newest last
    write wins; an empty list renders an explicit "no issue rows" note, never a
    clean bill of health.
    """
    delta_rows = (
        compute_delta(current, previous, thresholds=thresholds) if current and previous else []
    )
    fusion = fuse_engines(gsc=gsc_rows, yandex=yandex_rows, metrika=metrika_rows)
    opportunities = find_opportunities(
        gsc_rows,
        previous=gsc_previous_rows or None,
        previous_complete=gsc_previous_complete,
        limit=_WEEKLY_OPPORTUNITIES,
    )
    has_source_data = bool(gsc_rows or yandex_rows or metrika_rows or extra_engines)
    headline_text = (
        _weekly_headline(delta_rows)
        if delta_rows
        else "GSC comparison unavailable"
        if has_source_data
        else "No provider data available"
    )

    return Report(
        kind="weekly",
        property_id=property_id,
        period_start=_iso_date(period_start),
        period_end=_iso_date(period_end),
        headline=headline_text,
        sections=[
            _headline_section(delta_rows),
            _traffic_section(fusion),
            _opportunities_section(opportunities, has_rows=bool(gsc_rows)),
            _issues_section(issues),
            _sources_section(
                fusion,
                row_counts={
                    "gsc": len(gsc_rows),
                    "yandex": len(yandex_rows),
                    "metrika": len(metrika_rows),
                },
                extra_engines=extra_engines,
                limitations=limitations,
            ),
        ],
        generated_at=_timestamp(generated_at),
        limitations=list(limitations),
        warnings=_weekly_warnings(current, previous, gsc_rows, yandex_rows, metrika_rows),
    )


def build_indexing(
    *,
    property_id: str,
    period_start: date,
    period_end: date,
    google: Sequence[Mapping[str, str]] = (),
    yandex: Sequence[Mapping[str, str]] = (),
    crawl: Sequence[Mapping[str, str]] = (),
    generated_at: datetime | None = None,
) -> Report:
    """Build the indexing report: reason buckets, example URLs and how to verify."""
    buckets = triage(
        google=google,
        yandex=yandex,
        crawl=crawl,
        max_examples=_INDEXING_EXAMPLES,
    )
    total = sum(bucket.count for bucket in buckets)

    return Report(
        kind="indexing",
        property_id=property_id,
        period_start=_iso_date(period_start),
        period_end=_iso_date(period_end),
        headline=f"{total} issues in {len(buckets)} buckets",
        sections=[
            _buckets_section(buckets, total),
            _examples_section(buckets),
            _verification_section(),
        ],
        generated_at=_timestamp(generated_at),
        limitations=[],
        warnings=_indexing_warnings(buckets, google, yandex, crawl),
    )


def build_opportunities(
    *,
    property_id: str,
    period_start: date,
    period_end: date,
    rows: Sequence[Mapping[str, Any]],
    previous: Sequence[Mapping[str, Any]] = (),
    limit: int = 10,
    generated_at: datetime | None = None,
) -> Report:
    """Build the ranked opportunities report with a modelled weekly upside."""
    opportunities = find_opportunities(rows, previous=previous or None, limit=limit)
    expected = round(sum(item.expected_clicks for item in opportunities), 1)

    return Report(
        kind="opportunities",
        property_id=property_id,
        period_start=_iso_date(period_start),
        period_end=_iso_date(period_end),
        headline=(f"Top {len(opportunities)} opportunities, expected +{expected:.1f} clicks/week"),
        sections=[
            _opportunities_section(opportunities, has_rows=bool(rows), include_estimate=True),
        ],
        generated_at=_timestamp(generated_at),
        limitations=[],
        warnings=_opportunities_warnings(opportunities, rows),
    )


def _headline_section(rows: Sequence[DeltaRow]) -> ReportSection:
    return ReportSection(
        key="headline",
        title="Headline",
        summary=_delta_summary_text(rows),
        rows=[
            {
                "metric": row.metric,
                "current": row.current,
                "previous": row.previous,
                "abs_delta": row.abs_delta,
                "pct_delta": row.pct_delta,
                "significant": row.significant,
                "direction": row.direction,
                "good": row.good,
            }
            for row in rows
        ],
    )


def _traffic_section(fusion: FusionReport) -> ReportSection:
    return ReportSection(
        key="traffic",
        title="Traffic by engine",
        summary=_traffic_summary(fusion.engines),
        rows=[
            {
                "engine": engine.engine,
                "rows": engine.rows,
                "clicks": engine.clicks,
                "impressions": engine.impressions,
                "ctr": engine.ctr,
                "position": engine.position,
                "limitations": list(engine.limitations),
            }
            for engine in fusion.engines
        ],
    )


def _opportunities_section(
    opportunities: Sequence[Opportunity],
    *,
    has_rows: bool,
    include_estimate: bool = False,
) -> ReportSection:
    notes: list[str] = []
    if include_estimate or opportunities:
        notes.append(_ESTIMATE_NOTE)
    if not has_rows:
        notes.append("no GSC query rows were provided; opportunities are empty")
    return ReportSection(
        key="opportunities",
        title="Opportunities",
        summary=(
            f"Top {len(opportunities)} by modelled upside"
            if opportunities
            else "No opportunities found"
        ),
        rows=[_opportunity_row(opportunity) for opportunity in opportunities],
        notes=notes,
    )


def _issues_section(issues: Sequence[Mapping[str, Any]]) -> ReportSection:
    groups = _group_issues(issues)
    total = sum(group["count"] for group in groups)
    return ReportSection(
        key="issues",
        title="Issues by severity",
        summary=_issues_summary(total, groups),
        rows=groups,
        notes=[] if issues else ["no issue rows were provided"],
    )


def _sources_section(
    fusion: FusionReport,
    *,
    row_counts: Mapping[str, int],
    extra_engines: Sequence[Mapping[str, Any]] = (),
    limitations: Sequence[str],
) -> ReportSection:
    engine_limits = {engine.engine: engine.limitations for engine in fusion.engines}
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    present: list[str] = []

    for engine in _ENGINES:
        count = row_counts.get(engine, 0)
        rows.append(
            {
                "source": engine,
                "status": "rows" if count > 0 else "missing",
                "rows": count,
                "limitations": list(engine_limits.get(engine, ())),
            }
        )
        if count > 0:
            present.append(engine)
            notes.append(f"{engine}: {count} {_plural(count, 'row')}")
        else:
            notes.append(f"{engine}: missing (no rows)")

    for extra in extra_engines:
        name = str(extra.get("engine", "unknown"))
        kind = str(extra.get("kind", "traffic"))
        extra_rows = extra.get("rows", [])
        count = len(extra_rows) if isinstance(extra_rows, list) else 0
        extra_limits = extra.get("limitations", [])
        rows.append(
            {
                "source": name,
                "status": "rows" if count > 0 else "missing",
                "rows": count,
                "kind": kind,
                "limitations": list(extra_limits) if isinstance(extra_limits, list) else [],
            }
        )
        if count > 0:
            present.append(f"{name} ({kind})")
            notes.append(f"{name} ({kind}): {count} {_plural(count, 'row')}")
        else:
            notes.append(f"{name} ({kind}): missing (no rows)")

    notes.extend(f"limitation: {text}" for text in limitations)
    notes.extend(f"caveat: {text}" for text in fusion.caveats)

    total_engines = len(_ENGINES) + len(extra_engines)
    return ReportSection(
        key="sources",
        title="Sources",
        summary=(
            f"{len(present)} of {total_engines} engines returned rows"
            if present
            else "No engine returned rows"
        ),
        rows=rows,
        notes=notes,
    )


def _buckets_section(buckets: Sequence[Bucket], total: int) -> ReportSection:
    return ReportSection(
        key="buckets",
        title="Indexing buckets",
        summary=f"{total} issues in {len(buckets)} buckets",
        rows=[
            {
                "reason": bucket.reason,
                "count": bucket.count,
                "engines": list(bucket.engines),
                "next_action": bucket.next_action,
            }
            for bucket in buckets
        ],
    )


def _examples_section(buckets: Sequence[Bucket]) -> ReportSection:
    rows = [
        {"reason": bucket.reason, "url": url}
        for bucket in buckets
        for url in bucket.examples[:_INDEXING_EXAMPLES]
    ]
    notes: list[str] = []
    if any(bucket.count != len(bucket.examples) for bucket in buckets):
        notes.append(
            "a URL is counted once per engine, so a bucket count can exceed its example URLs"
        )
    if not rows:
        notes.append("no example URLs were provided")
    return ReportSection(
        key="examples",
        title="Example URLs",
        summary=(
            f"{len(rows)} example {_plural(len(rows), 'URL')} across "
            f"{len(buckets)} {_plural(len(buckets), 'bucket')}"
            if rows
            else "No example URLs"
        ),
        rows=rows,
        notes=notes,
    )


def _verification_section() -> ReportSection:
    return ReportSection(
        key="verification",
        title="Verification",
        summary="How to re-run",
        notes=[_VERIFICATION_NOTE],
    )


def _weekly_headline(rows: Sequence[DeltaRow]) -> str:
    text = headline(rows)
    if any(row.significant and not row.good for row in rows):
        return f"{_BAD_DELTA_PREFIX}{text}"
    return text


def _delta_summary_text(rows: Sequence[DeltaRow]) -> str:
    if not rows:
        return "Comparison unavailable: both complete baseline windows are required"
    rollup = summary(rows)
    significant = len(rollup["significant"])
    if significant == 0:
        return "No significant change versus the previous period"
    return (
        f"{significant} significant {_plural(significant, 'change')}: "
        f"{rollup['good_count']} improving, {rollup['bad_count']} worsening"
    )


def _traffic_summary(engines: Sequence[EngineSummary]) -> str:
    if not engines:
        return "No engine reported traffic rows"
    names = ", ".join(engine.engine for engine in engines)
    return f"{len(engines)} {_plural(len(engines), 'engine')} reported rows: {names}"


def _group_issues(issues: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for issue in issues:
        grouped.setdefault(_severity(issue.get("severity")), []).append(issue)

    groups: list[dict[str, Any]] = []
    for severity in sorted(grouped, key=_severity_sort_key):
        rule_ids: set[str] = set()
        for issue in grouped[severity]:
            rule_id = _text(issue.get("rule_id"))
            if rule_id:
                rule_ids.add(rule_id)
        groups.append(
            {
                "severity": severity,
                "count": len(grouped[severity]),
                "rule_ids": sorted(rule_ids),
            }
        )
    return groups


def _issues_summary(total: int, groups: Sequence[Mapping[str, Any]]) -> str:
    if total == 0:
        return "No issues reported"
    breakdown = ", ".join(f"{group['count']} {group['severity']}" for group in groups)
    return f"{total} {_plural(total, 'issue')}: {breakdown}"


def _weekly_warnings(
    current: Mapping[str, float],
    previous: Mapping[str, float],
    gsc_rows: Sequence[Mapping[str, Any]],
    yandex_rows: Sequence[Mapping[str, Any]],
    metrika_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    if not current and not previous:
        warnings.append("no metrics for either period; delta rows are all zero")
    elif not previous:
        warnings.append("previous period has no metrics; deltas cannot be significant")
    if not gsc_rows and not yandex_rows and not metrika_rows:
        warnings.append("no provider rows; traffic and opportunities are empty")
    return warnings


def _indexing_warnings(
    buckets: Sequence[Bucket],
    google: Sequence[Mapping[str, str]],
    yandex: Sequence[Mapping[str, str]],
    crawl: Sequence[Mapping[str, str]],
) -> list[str]:
    if not google and not yandex and not crawl:
        return ["no indexing rows were provided; buckets are empty"]
    if not buckets:
        return ["indexing rows contained no URLs; buckets are empty"]
    return []


def _opportunities_warnings(
    opportunities: Sequence[Opportunity],
    rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    if not rows:
        return ["no query rows were provided; opportunities are empty"]
    if not opportunities:
        return ["query rows did not meet the opportunity thresholds"]
    return []


def _opportunity_row(opportunity: Opportunity) -> dict[str, Any]:
    return {
        "kind": opportunity.kind,
        "query": opportunity.query,
        "page": opportunity.page,
        "clicks": opportunity.clicks,
        "impressions": opportunity.impressions,
        "ctr": opportunity.ctr,
        "position": opportunity.position,
        "expected_clicks": opportunity.expected_clicks,
        "evidence": dict(opportunity.evidence),
    }


def _severity(value: object) -> str:
    text = _text(value)
    return text.casefold() if text else _UNKNOWN_SEVERITY


def _severity_sort_key(severity: str) -> tuple[int, str]:
    if severity in _SEVERITIES:
        return _SEVERITIES.index(severity), ""
    return len(_SEVERITIES), severity


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _iso_date(value: date) -> str:
    return value.isoformat()


def _timestamp(value: datetime | None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural if plural is not None else f"{singular}s"
