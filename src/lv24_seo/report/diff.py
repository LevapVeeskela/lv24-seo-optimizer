"""Week-over-week diff of two stored report snapshots (for PR comments and CI summaries).

A weekly report already compares two *provider* windows. This module compares
two *reports* — what changed in our own conclusions between runs: headline
metric values, issue counts per severity, opportunities that appeared or
resolved, and limitations that were added or lifted.

Honesty rules:

* A headline metric is compared only when **both** snapshots carry a numeric
  value for it; otherwise it is listed under ``unknown`` instead of being read
  as zero.
* ``comparable`` is ``False`` when either snapshot had no headline comparison
  (no comparable GSC windows) or the two snapshots cover different period
  lengths; ``limitations`` says why.
* Opportunity identity is ``(kind, query, page)``; rank movement within the
  list is not reported as a change.

Output is deterministic (sorted keys and lists) so the Markdown can be posted
to a PR or ``$GITHUB_STEP_SUMMARY`` and diffed between runs. Pure, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

__all__ = ["diff_snapshots", "render_diff_markdown"]

_HEADLINE_METRICS: tuple[str, ...] = ("clicks", "impressions", "ctr", "position")
_MAX_LISTED = 20


def diff_snapshots(base: Mapping[str, Any], head: Mapping[str, Any]) -> dict[str, Any]:
    """Return a structured diff from ``base`` (older) to ``head`` (newer)."""
    base_headline = _headline_values(base)
    head_headline = _headline_values(head)
    metrics: list[dict[str, Any]] = []
    unknown: list[str] = []
    for metric in _HEADLINE_METRICS:
        before = base_headline.get(metric)
        after = head_headline.get(metric)
        if before is None or after is None:
            if before is not None or after is not None:
                unknown.append(metric)
            continue
        change = after - before
        metrics.append(
            {
                "metric": metric,
                "base": before,
                "head": after,
                "abs_delta": round(change, 6),
                "pct_delta": round(change / before * 100, 2) if before else None,
            }
        )

    base_opps = _opportunity_keys(base)
    head_opps = _opportunity_keys(head)
    base_limits = set(_strings(base.get("limitations")))
    head_limits = set(_strings(head.get("limitations")))

    limitations: list[str] = []
    if not base_headline or not head_headline:
        limitations.append(
            "one snapshot has no headline comparison (no comparable GSC windows): "
            "metric changes are unknown"
        )
    base_days = _period_days(base)
    head_days = _period_days(head)
    if base_days is not None and head_days is not None and base_days != head_days:
        limitations.append(
            f"period lengths differ ({base_days}d vs {head_days}d): metric deltas are not "
            "like-for-like"
        )
    if unknown:
        limitations.append(
            "metrics present in only one snapshot are unknown, not zero: " + ", ".join(unknown)
        )

    return {
        "base": _identity(base),
        "head": _identity(head),
        "comparable": not limitations,
        "headline": metrics,
        "unknown_metrics": unknown,
        "issues": _issue_changes(base, head),
        "opportunities": {
            "added": [_opp_dict(key) for key in sorted(head_opps - base_opps)],
            "resolved": [_opp_dict(key) for key in sorted(base_opps - head_opps)],
            "unchanged": len(base_opps & head_opps),
        },
        "limitations_changed": {
            "added": sorted(head_limits - base_limits),
            "lifted": sorted(base_limits - head_limits),
        },
        "limitations": limitations,
    }


def render_diff_markdown(diff: Mapping[str, Any]) -> str:
    """Render :func:`diff_snapshots` output as GitHub-flavoured Markdown."""
    base = _mapping(diff.get("base"))
    head = _mapping(diff.get("head"))
    lines = [
        f"### SEO report diff: {base.get('period', '?')} → {head.get('period', '?')}",
        "",
        f"Property: `{head.get('property_id') or base.get('property_id') or '?'}` · "
        f"comparable: **{'yes' if diff.get('comparable') else 'no'}**",
        "",
    ]
    headline = [row for row in diff.get("headline") or [] if isinstance(row, Mapping)]
    if headline:
        lines += ["| Metric | Base | Head | Δ | Δ% |", "|---|---:|---:|---:|---:|"]
        for row in headline:
            pct = row.get("pct_delta")
            lines.append(
                f"| {row['metric']} | {_num(row['base'])} | {_num(row['head'])} | "
                f"{_signed(row['abs_delta'])} | {'n/a' if pct is None else _signed(pct) + '%'} |"
            )
        lines.append("")
    else:
        lines += ["_No headline metrics comparable in both snapshots._", ""]

    issues = [row for row in diff.get("issues") or [] if isinstance(row, Mapping)]
    changed = [row for row in issues if row.get("delta")]
    if changed:
        lines.append(
            "**Issues by severity:** "
            + ", ".join(
                f"{row['severity']} {row['base']}→{row['head']} ({_signed(row['delta'])})"
                for row in changed
            )
        )
        lines.append("")

    opportunities = _mapping(diff.get("opportunities"))
    for label, key in (("New opportunities", "added"), ("Resolved opportunities", "resolved")):
        rows = [row for row in opportunities.get(key) or [] if isinstance(row, Mapping)]
        if not rows:
            continue
        lines.append(f"**{label} ({len(rows)}):**")
        for row in rows[:_MAX_LISTED]:
            page = f" — {row['page']}" if row.get("page") else ""
            lines.append(f"- `{row['kind']}` {row['query'] or '(no query)'}{page}")
        if len(rows) > _MAX_LISTED:
            lines.append(f"- … and {len(rows) - _MAX_LISTED} more")
        lines.append("")

    limits = _mapping(diff.get("limitations_changed"))
    for label, key in (("Limitations added", "added"), ("Limitations lifted", "lifted")):
        values = _strings(limits.get(key))
        if values:
            lines.append(f"**{label}:**")
            lines += [f"- {value}" for value in values]
            lines.append("")

    caveats = _strings(diff.get("limitations"))
    if caveats:
        lines.append("> **Caveats:** " + "; ".join(caveats))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# -- helpers ---------------------------------------------------------------------------------


def _sections(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for section in snapshot.get("sections") or []:
        if isinstance(section, Mapping) and isinstance(section.get("key"), str):
            found.setdefault(section["key"], section)
    return found


def _rows(snapshot: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    section = _sections(snapshot).get(key) or {}
    return [row for row in section.get("rows") or [] if isinstance(row, Mapping)]


def _headline_values(snapshot: Mapping[str, Any]) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in _rows(snapshot, "headline"):
        metric = row.get("metric")
        value = _number(row.get("current"))
        if isinstance(metric, str) and value is not None:
            values[metric] = value
    return values


def _issue_changes(base: Mapping[str, Any], head: Mapping[str, Any]) -> list[dict[str, Any]]:
    before = _issue_counts(base)
    after = _issue_counts(head)
    return [
        {
            "severity": severity,
            "base": before.get(severity, 0),
            "head": after.get(severity, 0),
            "delta": after.get(severity, 0) - before.get(severity, 0),
        }
        for severity in sorted(set(before) | set(after))
    ]


def _issue_counts(snapshot: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _rows(snapshot, "issues"):
        severity = row.get("severity")
        count = row.get("count")
        if isinstance(severity, str) and isinstance(count, int) and not isinstance(count, bool):
            counts[severity] = counts.get(severity, 0) + count
    return counts


def _opportunity_keys(snapshot: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for row in _rows(snapshot, "opportunities"):
        keys.add(
            (str(row.get("kind") or ""), str(row.get("query") or ""), str(row.get("page") or ""))
        )
    return keys


def _opp_dict(key: tuple[str, str, str]) -> dict[str, str]:
    return {"kind": key[0], "query": key[1], "page": key[2]}


def _identity(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    period = _mapping(snapshot.get("period"))
    return {
        "property_id": snapshot.get("property_id"),
        "period": f"{period.get('start', '?')}..{period.get('end', '?')}",
        "headline": snapshot.get("headline"),
    }


def _period_days(snapshot: Mapping[str, Any]) -> int | None:
    period = _mapping(snapshot.get("period"))
    try:
        start = date.fromisoformat(str(period.get("start")))
        end = date.fromisoformat(str(period.get("end")))
    except ValueError:
        return None
    return (end - start).days + 1


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return []
    return [item for item in value if isinstance(item, str)]


def _num(value: object) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    return f"{number:.0f}" if number.is_integer() else f"{number:.4g}"


def _signed(value: object) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    text = f"{number:.0f}" if number.is_integer() else f"{number:.4g}"
    return text if number < 0 else f"+{text}"
