"""Attributed change digest: what moved, and which stored evidence could explain it.

The weekly report already knows *what* changed (the delta engine) and *which
crawl issues exist* (the issues section). This module joins the two with the
indexing events and page state kept in the store, so a reader gets a candidate
cause per material metric change instead of a blank Monday.

Honesty rules:

* only significant delta rows are explained; a flat or missing comparison is
  reported as "nothing to attribute", never as a cause;
* an item always names its evidence (rule id and URL, indexing event, or page
  field) and a confidence level; a *low* confidence is still a statement about
  evidence, not a claim of causality;
* with no matching evidence the item is ``unexplained`` and a limitation says
  so — the digest never invents a cause to fill the gap;
* a rule known not to move search metrics (alt text, HTML lang) is never a
  candidate; an unknown rule is only weak (low confidence) evidence;
* rows without the identifiers needed to check them are dropped and counted in
  ``malformed_rows`` instead of being guessed at;
* items and evidence are capped, and ``total``/``truncated`` carry the full
  sizes. ``data_age`` is the oldest observation among the evidence actually
  shown, so a stale crawl cannot look fresh.

Pure function, no I/O, no clock: dates come from the caller's rows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

__all__ = ["CAUSES", "CONFIDENCES", "Digest", "DigestItem", "build_digest"]

METRICS: tuple[str, ...] = ("clicks", "impressions", "ctr", "position")
CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
CAUSES: tuple[str, ...] = (
    "new_crawl_issue",
    "resolved_crawl_issue",
    "indexing_event",
    "page_state",
    "unexplained",
)

_CONFIDENCE_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_SEVERITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_UNKNOWN_SEVERITY = len(_SEVERITY_RANK)
_WORSENING_PREFERENCE: tuple[str, ...] = ("new_crawl_issue", "page_state", "indexing_event")
_IMPROVING_PREFERENCE: tuple[str, ...] = ("resolved_crawl_issue", "indexing_event")
_DEFAULT_LIMIT = 8
_DEFAULT_EVIDENCE_LIMIT = 3

_VISIBILITY: tuple[str, ...] = ("clicks", "impressions", "position")
_SNIPPET: tuple[str, ...] = ("ctr", "clicks", "position")
_CONTENT: tuple[str, ...] = ("clicks", "ctr", "position")
_RICH_RESULT: tuple[str, ...] = ("ctr",)

# Rule id -> metrics a fix could plausibly move. An empty tuple is a known rule
# with no search-metric claim: it must never be offered as a cause.
_RULE_METRICS: dict[str, tuple[str, ...]] = {
    "noindex-on-200": _VISIBILITY,
    "sitemap-url-noindex": _VISIBILITY,
    "robots-blocked": _VISIBILITY,
    "canonical-mismatch": _VISIBILITY,
    "canonical-missing": _VISIBILITY,
    "server-error": _VISIBILITY,
    "client-error": _VISIBILITY,
    "sitemap-url-not-ok": _VISIBILITY,
    "redirect-chain": _VISIBILITY,
    "soft-404": _VISIBILITY,
    "broken-internal-link": _VISIBILITY,
    "mixed-content": _VISIBILITY,
    "hreflang-target-not-ok": _VISIBILITY,
    "hreflang-not-reciprocal": _VISIBILITY,
    "hreflang-missing-x-default": _VISIBILITY,
    "title-missing": _SNIPPET,
    "title-length": _SNIPPET,
    "title-duplicate": _SNIPPET,
    "meta-description-missing": _SNIPPET,
    "meta-description-duplicate": _SNIPPET,
    "h1-missing": _CONTENT,
    "h1-multiple": _CONTENT,
    "thin-content": _CONTENT,
    "jsonld-required": _RICH_RESULT,
    "jsonld-organization": _RICH_RESULT,
    "jsonld-organization-duplicate": _RICH_RESULT,
    "jsonld-event-required": _RICH_RESULT,
    "image-alt-missing": (),
    "html-lang-missing": (),
    "html-lang-mismatch": (),
}

_NEGATIVE_EVENT_WORDS: tuple[str, ...] = (
    "noindex",
    "robots",
    "canonical",
    "error",
    "5xx",
    "4xx",
    "404",
    "redirect",
    "duplicate",
    "excluded",
    "not indexed",
    "removed",
    "declined",
)
_POSITIVE_EVENT_WORDS: tuple[str, ...] = (
    "indexed",
    "valid",
    "restored",
    "added",
    "submitted",
    "ok",
)
_INDEX_STATE_ANOMALIES: frozenset[str] = frozenset(
    {"noindex", "blocked", "robots_blocked", "error", "excluded", "redirect"}
)


@dataclass(frozen=True, slots=True)
class DigestItem:
    """One material metric change with its candidate cause and evidence."""

    metric: str
    current: float
    previous: float
    abs_delta: float
    pct_delta: float | None
    direction: str
    good: bool | None
    cause: str
    confidence: str
    evidence: tuple[dict[str, Any], ...]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "change": {
                "current": self.current,
                "previous": self.previous,
                "abs_delta": self.abs_delta,
                "pct_delta": self.pct_delta,
                "direction": self.direction,
                "good": self.good,
            },
            "cause": self.cause,
            "confidence": self.confidence,
            "evidence": [dict(item) for item in self.evidence],
            "next_action": self.next_action,
        }


@dataclass(slots=True)
class Digest:
    """Bounded digest: explained and unexplained changes plus its own caveats."""

    items: list[DigestItem] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    data_age: str | None = None
    malformed_rows: int = 0
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "truncated": self.truncated,
            "data_age": self.data_age,
            "malformed_rows": self.malformed_rows,
            "limitations": list(self.limitations),
        }


def build_digest(
    *,
    deltas: Sequence[Mapping[str, Any]],
    issue_changes: Sequence[Mapping[str, Any]] = (),
    indexing_events: Sequence[Mapping[str, Any]] = (),
    pages: Sequence[Mapping[str, Any]] = (),
    has_comparison: bool = True,
    period_start: date | None = None,
    period_end: date | None = None,
    limit: int = _DEFAULT_LIMIT,
    evidence_limit: int = _DEFAULT_EVIDENCE_LIMIT,
) -> Digest:
    """Correlate significant metric changes with stored crawl/indexing evidence.

    ``deltas`` are headline delta rows (``metric``/``current``/``previous``/
    ``abs_delta``/``pct_delta``/``significant``/``direction``/``good``).
    ``issue_changes`` are crawl issues with ``rule_id``/``url`` and either an
    explicit ``change`` (``new``/``resolved``), a ``state`` of ``fixed`` or a
    ``first_seen`` inside the current period. ``indexing_events`` carry
    ``url`` plus ``reason``/``event``. ``pages`` carry ``url`` and the crawl
    fields ``status``/``index_state``/``canonical``.
    """
    issues, malformed_issues = _normalize_issues(issue_changes, period_start, period_end)
    events, malformed_events = _normalize_events(indexing_events)
    page_rows, malformed_pages = _normalize_pages(pages)
    malformed = malformed_issues + malformed_events + malformed_pages

    digest = Digest(total=0, malformed_rows=malformed)
    if not has_comparison:
        digest.limitations.append("digest_no_comparison")
        return digest

    significant = [row for row in (_normalize_delta(row) for row in deltas) if row is not None]
    digest.total = len(significant)
    if not significant:
        return digest

    max_items = max(0, int(limit))
    max_evidence = max(0, int(evidence_limit))
    selected = significant[:max_items]
    digest.truncated = len(significant) > len(selected)

    unexplained = 0
    dates: list[date] = []
    for row in selected:
        item, evidence_dates, evidence_capped = _explain(
            row,
            issues=issues,
            events=events,
            pages=page_rows,
            evidence_limit=max_evidence,
        )
        if item.cause == "unexplained":
            unexplained += 1
        digest.items.append(item)
        dates.extend(evidence_dates)
        digest.truncated = digest.truncated or evidence_capped

    if dates:
        digest.data_age = min(dates).isoformat()
    if not issues and not events and not page_rows:
        digest.limitations.append("digest_no_evidence")
    elif unexplained:
        digest.limitations.append("digest_no_matching_evidence")
    if malformed:
        digest.limitations.append("digest_evidence_malformed")
    return digest


# -- normalization ---------------------------------------------------------------------------


def _normalize_delta(row: object) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    metric = _text(row.get("metric")).casefold()
    if metric not in METRICS:
        return None
    if row.get("significant") is not True:
        return None
    current = _number(row.get("current"))
    previous = _number(row.get("previous"))
    if current is None or previous is None:
        return None
    abs_delta = _number(row.get("abs_delta"))
    if abs_delta is None:
        abs_delta = current - previous
    direction = _text(row.get("direction")).casefold()
    if direction not in {"up", "down", "flat"}:
        direction = "up" if abs_delta > 0 else "down" if abs_delta < 0 else "flat"
    if direction == "flat" or abs_delta == 0:
        # A significant change cannot be flat; a contradictory row is not a change.
        return None
    good = row.get("good") if isinstance(row.get("good"), bool) else None
    if good is None:
        return None
    return {
        "metric": metric,
        "current": current,
        "previous": previous,
        "abs_delta": abs_delta,
        "pct_delta": _number(row.get("pct_delta")),
        "direction": direction,
        "good": good,
    }


def _normalize_issues(
    rows: Sequence[Mapping[str, Any]],
    period_start: date | None,
    period_end: date | None,
) -> tuple[list[dict[str, Any]], int]:
    issues: list[dict[str, Any]] = []
    malformed = 0
    for row in rows:
        rule_id = _text(row.get("rule_id"))
        url = _text(row.get("url"))
        if not rule_id or not url:
            malformed += 1
            continue
        change = _text(row.get("change")).casefold()
        state = _text(row.get("state")).casefold()
        first_seen = _day(row.get("first_seen"))
        if change not in {"new", "resolved"}:
            if state in {"fixed", "resolved"}:
                change = "resolved"
            elif first_seen is not None and period_start is not None:
                change = "new" if first_seen >= period_start else "existing"
            else:
                change = "existing"
        if (
            change == "new"
            and period_end is not None
            and first_seen is not None
            and first_seen > period_end
        ):
            # An issue first seen after the reported window cannot explain it.
            change = "future"
        issues.append(
            {
                "rule_id": rule_id,
                "url": url,
                "severity": _text(row.get("severity")).casefold(),
                "change": change,
                "first_seen": first_seen,
                "last_seen": _day(row.get("last_seen")),
            }
        )
    return issues, malformed


def _normalize_events(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    malformed = 0
    for row in rows:
        url = _text(row.get("url"))
        reason = _text(row.get("reason")) or _text(row.get("event"))
        if not url or not reason:
            malformed += 1
            continue
        events.append(
            {
                "url": url,
                "reason": reason,
                "date": _day(row.get("date")) or _day(row.get("seen_at")),
            }
        )
    return events, malformed


def _normalize_pages(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    pages: list[dict[str, Any]] = []
    malformed = 0
    for row in rows:
        url = _text(row.get("url"))
        if not url:
            malformed += 1
            continue
        pages.append(
            {
                "url": url,
                "status": _int(row.get("status")),
                "index_state": _text(row.get("index_state")).casefold(),
                "canonical": _text(row.get("canonical")),
                "last_seen": _day(row.get("last_seen")),
            }
        )
    return pages, malformed


# -- correlation -----------------------------------------------------------------------------


def _explain(
    row: Mapping[str, Any],
    *,
    issues: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    evidence_limit: int,
) -> tuple[DigestItem, list[date], bool]:
    metric = str(row["metric"])
    worsening = row["good"] is False
    candidates = _candidates(metric, worsening=worsening, issues=issues, events=events, pages=pages)
    preference = _WORSENING_PREFERENCE if worsening else _IMPROVING_PREFERENCE

    cause = "unexplained"
    chosen: list[dict[str, Any]] = []
    for name in preference:
        if candidates[name]:
            cause = name
            chosen = candidates[name]
            break

    if not chosen:
        return (
            DigestItem(
                metric=metric,
                current=row["current"],
                previous=row["previous"],
                abs_delta=row["abs_delta"],
                pct_delta=row["pct_delta"],
                direction=row["direction"],
                good=row["good"],
                cause="unexplained",
                confidence="low",
                evidence=(),
                next_action=_unexplained_action(metric),
            ),
            [],
            False,
        )

    ordered = sorted(
        chosen,
        key=lambda item: (
            _CONFIDENCE_RANK.get(str(item["confidence"]), len(_CONFIDENCE_RANK)),
            _SEVERITY_RANK.get(str(item.get("severity", "")), _UNKNOWN_SEVERITY),
            str(item["evidence"].get("rule_id", "")),
            str(item["evidence"].get("url", "")),
            str(item["evidence"].get("reason", "")),
        ),
    )
    confidence = min(
        (str(item["confidence"]) for item in ordered),
        key=lambda value: _CONFIDENCE_RANK.get(value, len(_CONFIDENCE_RANK)),
    )
    shown = ordered[: max(0, evidence_limit)]
    dates = [day for item in shown for day in (item.get("date"),) if isinstance(day, date)]
    return (
        DigestItem(
            metric=metric,
            current=row["current"],
            previous=row["previous"],
            abs_delta=row["abs_delta"],
            pct_delta=row["pct_delta"],
            direction=row["direction"],
            good=row["good"],
            cause=cause,
            confidence=confidence,
            evidence=tuple(dict(item["evidence"]) for item in shown),
            next_action=_next_action(cause, metric, ordered[0]["evidence"]),
        ),
        dates,
        len(shown) < len(ordered),
    )


def _candidates(
    metric: str,
    *,
    worsening: bool,
    issues: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {
        "new_crawl_issue": [],
        "resolved_crawl_issue": [],
        "indexing_event": [],
        "page_state": [],
    }

    for issue in issues:
        rule_metrics = _rule_metrics(issue["rule_id"])
        if rule_metrics == () or (rule_metrics is not None and metric not in rule_metrics):
            continue
        severity = str(issue.get("severity", ""))
        evidence = {
            "kind": "crawl_issue",
            "rule_id": issue["rule_id"],
            "url": issue["url"],
            "severity": severity,
            "change": issue["change"],
        }
        if issue.get("first_seen") is not None:
            evidence["first_seen"] = issue["first_seen"].isoformat()
        if issue.get("last_seen") is not None:
            evidence["last_seen"] = issue["last_seen"].isoformat()
        observed = issue.get("last_seen") or issue.get("first_seen")
        if issue["change"] == "new" and worsening:
            confidence = "low" if rule_metrics is None else _new_issue_confidence(severity)
            found["new_crawl_issue"].append(
                {
                    "confidence": confidence,
                    "severity": severity,
                    "evidence": evidence,
                    "date": observed,
                }
            )
        elif issue["change"] == "resolved" and not worsening:
            confidence = "low" if rule_metrics is None else "medium"
            found["resolved_crawl_issue"].append(
                {
                    "confidence": confidence,
                    "severity": severity,
                    "evidence": evidence,
                    "date": observed,
                }
            )

    for event in events:
        polarity = _event_polarity(event["reason"])
        if polarity == "unknown":
            confidence = "low"
        elif (polarity == "negative" and worsening) or (polarity == "positive" and not worsening):
            confidence = "medium"
        else:
            continue
        evidence = {"kind": "indexing_event", "url": event["url"], "reason": event["reason"]}
        if event.get("date") is not None:
            evidence["date"] = event["date"].isoformat()
        found["indexing_event"].append(
            {
                "confidence": confidence,
                "evidence": evidence,
                "date": event.get("date"),
            }
        )

    if worsening:
        for page in pages:
            anomaly = _page_anomaly(page)
            if anomaly is None:
                continue
            field_name, value = anomaly
            evidence = {"kind": "page_state", "url": page["url"], "field": field_name}
            evidence["value"] = value
            found["page_state"].append(
                {
                    "confidence": "medium" if field_name in {"status", "index_state"} else "low",
                    "evidence": evidence,
                    "date": page.get("last_seen"),
                }
            )
    return found


def _rule_metrics(rule_id: str) -> tuple[str, ...] | None:
    return _RULE_METRICS.get(rule_id.casefold())


def _new_issue_confidence(severity: str) -> str:
    return "high" if _SEVERITY_RANK.get(severity, _UNKNOWN_SEVERITY) <= 1 else "medium"


def _event_polarity(reason: str) -> str:
    folded = reason.casefold()
    if any(word in folded for word in _NEGATIVE_EVENT_WORDS):
        return "negative"
    if any(word in folded for word in _POSITIVE_EVENT_WORDS):
        return "positive"
    return "unknown"


def _page_anomaly(page: Mapping[str, Any]) -> tuple[str, Any] | None:
    status = page.get("status")
    if isinstance(status, int) and status >= 400:
        return "status", status
    index_state = str(page.get("index_state") or "")
    if index_state in _INDEX_STATE_ANOMALIES:
        return "index_state", index_state
    canonical = str(page.get("canonical") or "")
    url = str(page.get("url") or "")
    if canonical and url and _bare_url(canonical) != _bare_url(url):
        return "canonical", canonical
    return None


def _bare_url(value: str) -> str:
    return value.strip().rstrip("/").casefold()


# -- next actions ----------------------------------------------------------------------------


def _next_action(cause: str, metric: str, evidence: Mapping[str, Any]) -> str:
    if cause == "new_crawl_issue":
        return (
            f"fix {evidence.get('rule_id', 'the new issue')} on the affected URLs, "
            f"then re-crawl and re-check {metric}"
        )
    if cause == "resolved_crawl_issue":
        return (
            f"confirm the {evidence.get('rule_id', 'resolved')} fix is live, "
            f"then re-check {metric} after the next crawl"
        )
    if cause == "indexing_event":
        return (
            f"review the {evidence.get('reason', 'indexing')} event for "
            f"{evidence.get('url', 'the affected URL')}, then request a recrawl"
        )
    if cause == "page_state":
        return (
            f"restore {evidence.get('field', 'the page state')} for "
            f"{evidence.get('url', 'the affected URL')}, then re-check {metric} "
            "after the next crawl"
        )
    return _unexplained_action(metric)


def _unexplained_action(metric: str) -> str:
    return (
        f"collect crawl issues, indexing events and page state for the URLs behind {metric}; "
        "no cause is attributed"
    )


# -- scalars ---------------------------------------------------------------------------------


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object) -> float | None:
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


def _int(value: object) -> int | None:
    number = _number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
