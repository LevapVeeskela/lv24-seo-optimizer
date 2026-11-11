"""MCP tool implementations (Wave 7, WP7.2-WP7.3).

Every tool returns the standard :mod:`lv24_seo.core.envelope` payload as a compact JSON
text block *and* as ``structuredContent``. The registry
(:mod:`lv24_seo.mcp.registry`) stays the single source of truth for names, descriptions and
annotations: :mod:`lv24_seo.mcp.server` pairs each builder here with its ``ToolSpec`` and
refuses to start when the two drift.

Design notes
------------
* Tools are thin. They translate MCP arguments into service calls, keep payloads small and
  never synthesise provider data. Composition that needs several provider calls
  (``seo_indexing_triage``, ``seo_opportunities``) is built from the existing analysis
  helpers over rows the service actually returned.
* A missing service method is an honest ``UPSTREAM_UNAVAILABLE`` failure. A missing *write*
  method, or a write call without ``allow_write=True``, is ``WRITE_DISABLED``: the two write
  tools never touch a provider by default.
* Every ``SeoError`` becomes ``{ok: false, error: {...}}`` with ``isError`` set on the MCP
  result. Unexpected exceptions become ``INTERNAL`` without a traceback or exception text.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from mcp.types import CallToolResult, TextContent

from lv24_seo.analysis.indexing import triage
from lv24_seo.analysis.opportunities import find_opportunities
from lv24_seo.core.envelope import fail, ok
from lv24_seo.core.errors import (
    INTERNAL,
    INVALID_INPUT,
    UPSTREAM_UNAVAILABLE,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.core.timeutil import compare_period
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import render_markdown

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from lv24_seo.services import SeoService

__all__ = [
    "TOOL_BUILDERS",
    "ReportCache",
    "ToolState",
    "compact_json",
    "envelope_result",
    "respond",
]

logger = logging.getLogger(__name__)

TOOL_LIMIT_DEFAULT = 50
TOOL_LIMIT_MAX = 500
CRAWL_MAX_URLS_MAX = 5_000
CRAWL_ISSUE_LIMIT = 500
OPPORTUNITY_DAYS_DEFAULT = 28
OPPORTUNITY_LIMIT_DEFAULT = TOOL_LIMIT_DEFAULT
WRITE_NEXT_ACTION = (
    "review the plan first, then pass allow_write=True; keep the default in agent configs"
)

Envelope = dict[str, Any]
AnalysisRow = dict[str, float | str | None]
ToolBuilder = Callable[["ToolState"], Callable[..., CallToolResult]]

_ORDER_FIELDS: dict[str, str] = {
    "TOTAL_SHOWS": "shows",
    "TOTAL_CLICKS": "clicks",
    "SHOWS": "shows",
    "CLICKS": "clicks",
}
_SITEMAP_ACTIONS = frozenset({"submit", "delete"})
_CSV = ", "

# ``structuredContent`` is part of the 2025-06-18 spec; old SDK builds predate the field.
_CALL_TOOL_FIELDS = getattr(CallToolResult, "model_fields", {})
_SUPPORTS_STRUCTURED_CONTENT = "structured_content" in _CALL_TOOL_FIELDS


# -- result plumbing -------------------------------------------------------------------------


def compact_json(payload: Mapping[str, Any]) -> str:
    """Serialise an envelope as compact, UTF-8-friendly JSON."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def envelope_result(payload: Mapping[str, Any]) -> CallToolResult:
    """Render an envelope as MCP text plus ``structuredContent`` (when supported)."""
    data = dict(payload)
    try:
        text = compact_json(data)
    except (TypeError, ValueError):
        logger.warning("MCP tool produced a payload that is not valid JSON")
        data = fail(
            SeoError(
                code=INTERNAL,
                message="tool produced a payload that is not valid JSON",
                next_action="retry; if it persists report a bug",
            )
        )
        text = compact_json(data)

    is_error = not bool(data.get("ok"))
    if _SUPPORTS_STRUCTURED_CONTENT:
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=data,
            is_error=is_error,
        )
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)


def respond(
    operation: str,
    action: Callable[[], Mapping[str, Any]],
) -> CallToolResult:
    """Run one tool body and convert its envelope or failure into a tool result."""
    try:
        payload = action()
        if not isinstance(payload, Mapping):
            raise SeoError(
                code=INTERNAL,
                message=f"{operation} returned a malformed envelope",
                next_action="retry; if it persists report a bug",
            )
        return envelope_result(payload)
    except SeoError as exc:
        return envelope_result(fail(exc))
    except Exception:
        # The exception text may echo a provider request (headers, URLs); keep it in the log
        # only as a generic operation name, never in the client payload.
        logger.warning("unexpected failure in MCP tool %s", operation)
        return envelope_result(
            fail(
                SeoError(
                    code=INTERNAL,
                    message=f"unexpected internal error while running {operation}",
                    next_action="retry; if it persists run `lv24-seo doctor`",
                )
            )
        )


# -- service plumbing ------------------------------------------------------------------------


@dataclass(slots=True)
class ReportCache:
    """Last successful weekly report rendered in this server session."""

    markdown: str | None = None
    generated_at: str = ""
    property_id: str = ""

    def update(self, payload: Mapping[str, Any]) -> None:
        """Render and remember a successful weekly payload; keep the previous on failure."""
        report = _report_from_payload(payload)
        if report is None:
            return
        try:
            rendered = render_markdown(report)
        except Exception:
            logger.warning("could not render the weekly report as markdown")
            return
        self.markdown = rendered
        self.generated_at = report.generated_at
        self.property_id = report.property_id

    def read(self) -> str:
        """Return the cached report or an honest note when nothing was generated yet."""
        if self.markdown:
            return self.markdown
        return (
            "No weekly report has been generated in this MCP server session yet.\n\n"
            "Call the `seo_weekly_report` tool (or run `lv24-seo report weekly`) and read "
            "`seo://report/latest` again.\n"
        )


@dataclass(slots=True)
class ToolState:
    """Everything the registered tools share: the service and the report cache."""

    service: SeoService
    report: ReportCache = field(default_factory=ReportCache)


def invoke(
    service: SeoService,
    method: str,
    *,
    arguments: Mapping[str, Any],
    optional: Mapping[str, Any] | None = None,
) -> tuple[Envelope, list[str]]:
    """Call a service read method, forwarding optional kwargs the service accepts.

    Optional arguments (``property``, ``host_id``) are forwarded only when a caller supplied
    them *and* the method accepts that keyword. An older service keeps using its configured
    value; the caller sees a warning instead of a silently dropped input.
    """
    handler = getattr(service, method, None)
    if not callable(handler):
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"the service does not implement {method}",
            next_action="upgrade lv24-seo or use the CLI",
            retryable=False,
        )

    accepted = _accepted_keyword_names(handler)
    kwargs = dict(arguments)
    warnings: list[str] = []
    for key, value in (optional or {}).items():
        if _is_blank(value):
            continue
        if accepted is None or key in accepted:
            kwargs[key] = value
        else:
            warnings.append(f"{key}: ignored; this service uses its configured value")

    result = handler(**kwargs)
    if not isinstance(result, Mapping):
        raise SeoError(
            code=INTERNAL,
            message=f"{method} returned a malformed envelope",
            next_action="retry; if it persists report a bug",
        )
    return dict(result), warnings


def _accepted_keyword_names(handler: Callable[..., Any]) -> set[str] | None:
    """Keyword names a callable accepts, or ``None`` when it accepts ``**kwargs``."""
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables only
        return None

    names: set[str] = set()
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.add(parameter.name)
    return names


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False


def merge_warnings(payload: Mapping[str, Any], warnings: Sequence[str]) -> Envelope:
    """Return a shallow copy of ``payload`` with extra warnings appended."""
    merged = dict(payload)
    if not warnings:
        return merged
    existing = _strings(merged.get("warnings"))
    merged["warnings"] = _unique([*existing, *(str(item) for item in warnings)])
    return merged


def _strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


# -- validation helpers ----------------------------------------------------------------------


def bounded_limit(
    value: int,
    *,
    default: int = TOOL_LIMIT_DEFAULT,
    maximum: int = TOOL_LIMIT_MAX,
) -> tuple[int, str | None]:
    """Validate ``1..maximum`` and clamp an oversized request with a visible warning."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SeoError(
            code=INVALID_INPUT,
            message="limit must be an integer",
            next_action=f"pass a value between 1 and {maximum}",
        )
    if value <= 0:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"limit must be at least 1 (got {value})",
            next_action=f"pass a value between 1 and {maximum}, or omit it for {default}",
        )
    if value > maximum:
        return maximum, f"limit: clamped to {maximum}"
    return value, None


def bounded_int(
    value: int,
    *,
    name: str,
    minimum: int,
    maximum: int,
    default: int,
) -> tuple[int, str | None]:
    """Validate a bounded integer, clamping only the upper bound with a warning."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an integer",
            next_action=f"pass a value between {minimum} and {maximum}",
        )
    if value < minimum:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be at least {minimum} (got {value})",
            next_action=f"pass a value between {minimum} and {maximum}, or omit it for {default}",
        )
    if value > maximum:
        return maximum, f"{name}: clamped to {maximum}"
    return value, None


def parse_iso_date(value: str, *, name: str) -> date:
    """Parse a ``YYYY-MM-DD`` date or explain the accepted form."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an ISO date (YYYY-MM-DD)",
            next_action="pass e.g. 2026-11-01",
        ) from None


def _window(start_date: str, end_date: str, *, days: int) -> tuple[date, date]:
    """Resolve an explicit ISO window or the last ``days`` full days (lag 2)."""
    start_text = start_date.strip()
    end_text = end_date.strip()
    if not start_text and not end_text:
        return compare_period(days, lag_days=2)[0]
    if not start_text or not end_text:
        raise SeoError(
            code=INVALID_INPUT,
            message="start_date and end_date must be provided together",
            next_action="pass both ISO dates or neither",
        )
    start = parse_iso_date(start_text, name="start_date")
    end = parse_iso_date(end_text, name="end_date")
    if start > end:
        raise SeoError(
            code=INVALID_INPUT,
            message="start_date must be on or before end_date",
            next_action="swap the dates",
        )
    return start, end


def _require_http_url(value: str, *, name: str) -> None:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an absolute http(s) URL",
            next_action="pass e.g. https://example.com/page",
        )


# -- read tools ------------------------------------------------------------------------------


def build_properties(state: ToolState) -> Callable[[], CallToolResult]:
    """``seo_properties``: list configured providers and token presence."""

    def seo_properties() -> CallToolResult:
        def _run() -> Envelope:
            payload, warnings = invoke(state.service, "properties", arguments={})
            return merge_warnings(payload, warnings)

        return respond("seo_properties", _run)

    return seo_properties


def build_gsc_search_analytics(state: ToolState) -> Callable[..., CallToolResult]:
    """``gsc_search_analytics``: GSC clicks/impressions/CTR/position by dimension."""

    def gsc_search_analytics(
        property: str = "",
        start_date: str = "",
        end_date: str = "",
        dimensions: list[str] | None = None,
        limit: int = TOOL_LIMIT_DEFAULT,
    ) -> CallToolResult:
        def _run() -> Envelope:
            size, size_warning = bounded_limit(limit)
            start, end = _window(start_date, end_date, days=7)
            dims = tuple(dimensions) if dimensions else ("query",)
            warnings = [size_warning] if size_warning else []
            payload, ignored = invoke(
                state.service,
                "gsc_query",
                arguments={"start": start, "end": end, "dimensions": dims, "limit": size},
                optional={"property": property.strip()},
            )
            return merge_warnings(payload, [*warnings, *ignored])

        return respond("gsc_search_analytics", _run)

    return gsc_search_analytics


def build_yandex_query_stats(state: ToolState) -> Callable[..., CallToolResult]:
    """``yandex_query_stats``: top Yandex queries, optionally re-sorted by an indicator."""

    def yandex_query_stats(
        host_id: str = "",
        limit: int = TOOL_LIMIT_DEFAULT,
        order_by: str = "TOTAL_SHOWS",
    ) -> CallToolResult:
        def _run() -> Envelope:
            size, size_warning = bounded_limit(limit)
            field_name = _order_field(order_by)
            warnings = [size_warning] if size_warning else []
            payload, ignored = invoke(
                state.service,
                "yandex_queries",
                arguments={"limit": size},
                optional={"host_id": host_id.strip()},
            )
            warnings.extend(ignored)
            payload, sort_warning = _sort_rows(payload, field_name)
            if sort_warning:
                warnings.append(sort_warning)
            return merge_warnings(payload, warnings)

        return respond("yandex_query_stats", _run)

    return yandex_query_stats


def _order_field(value: str) -> str:
    key = value.strip().upper() or "TOTAL_SHOWS"
    field_name = _ORDER_FIELDS.get(key)
    if field_name is None:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"order_by must be one of: {_CSV.join(sorted(_ORDER_FIELDS))}",
            next_action="pass TOTAL_SHOWS or TOTAL_CLICKS",
        )
    return field_name


def _sort_rows(payload: Envelope, field_name: str) -> tuple[Envelope, str | None]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return payload, None
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return payload, None
    if not any(isinstance(row, Mapping) and field_name in row for row in rows):
        return payload, f"order_by: no {field_name} values in rows; provider order kept"

    ordered = sorted(
        (row for row in rows if isinstance(row, Mapping)),
        key=lambda row: _number(row.get(field_name)),
        reverse=True,
    )
    updated = dict(payload)
    updated["data"] = {**_mapping(data), "rows": ordered}
    meta = _mapping(payload.get("meta"))
    meta["sorted_by"] = field_name
    updated["meta"] = meta
    return updated, None


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("-inf")
    return float(value)


def build_yandex_indexing_state(state: ToolState) -> Callable[[], CallToolResult]:
    """``yandex_indexing_state``: history, problems and current counters."""

    def yandex_indexing_state() -> CallToolResult:
        def _run() -> Envelope:
            payload, warnings = invoke(state.service, "yandex_indexing", arguments={})
            return merge_warnings(payload, warnings)

        return respond("yandex_indexing_state", _run)

    return yandex_indexing_state


def build_crawl_site(state: ToolState) -> Callable[..., CallToolResult]:
    """``seo_crawl_site``: robots-respecting crawl summary plus rule violations."""

    def seo_crawl_site(
        start_url: str = "",
        max_urls: int = 0,
        include_rules: list[str] | None = None,
    ) -> CallToolResult:
        def _run() -> Envelope:
            warnings: list[str] = []
            limit: int | None
            if max_urls < 0:
                raise SeoError(
                    code=INVALID_INPUT,
                    message="max_urls must be 0 (use the configured default) or a positive integer",
                    next_action=f"pass 1..{CRAWL_MAX_URLS_MAX}",
                )
            if max_urls > CRAWL_MAX_URLS_MAX:
                limit = CRAWL_MAX_URLS_MAX
                warnings.append(f"max_urls: clamped to {CRAWL_MAX_URLS_MAX}")
            else:
                limit = max_urls or None

            url = start_url.strip() or None
            if url is not None:
                _require_http_url(url, name="start_url")

            payload, ignored = invoke(
                state.service,
                "crawl_and_store",
                arguments={"start_url": url, "max_urls": limit},
            )
            warnings.extend(ignored)
            payload = _shape_crawl_payload(payload, include_rules, warnings)
            return merge_warnings(payload, warnings)

        return respond("seo_crawl_site", _run)

    return seo_crawl_site


def _shape_crawl_payload(
    payload: Envelope,
    include_rules: Sequence[str] | None,
    warnings: list[str],
) -> Envelope:
    """Filter violations by rule, cap the returned list and label both actions."""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return payload
    issues = data.get("issues")
    if not isinstance(issues, list):
        return payload

    selected = [issue for issue in issues if isinstance(issue, Mapping)]
    wanted = {rule.strip() for rule in (include_rules or []) if rule.strip()}
    if wanted:
        selected = [issue for issue in selected if str(issue.get("rule_id") or "") in wanted]
        warnings.append(f"include_rules: {len(selected)} of {len(issues)} issues shown")

    truncated = len(selected) > CRAWL_ISSUE_LIMIT
    if truncated:
        warnings.append(f"issues: showing the first {CRAWL_ISSUE_LIMIT} of {len(selected)}")
        selected = selected[:CRAWL_ISSUE_LIMIT]

    updated = dict(payload)
    updated["data"] = {**_mapping(data), "issues": selected}
    meta = _mapping(payload.get("meta"))
    meta["issues_shown"] = len(selected)
    meta["issues_truncated"] = truncated
    if wanted:
        meta["issues_filtered_by"] = sorted(wanted)
    updated["meta"] = meta
    return updated


def build_weekly_report(state: ToolState) -> Callable[..., CallToolResult]:
    """``seo_weekly_report``: compose the digest and cache it for ``seo://report/latest``."""

    def seo_weekly_report(days: int = 7, lag_days: int = 2) -> CallToolResult:
        def _run() -> Envelope:
            span, span_warning = bounded_int(days, name="days", minimum=1, maximum=90, default=7)
            lag, lag_warning = bounded_int(
                lag_days, name="lag_days", minimum=0, maximum=14, default=2
            )
            warnings = [warning for warning in (span_warning, lag_warning) if warning]
            payload, ignored = invoke(
                state.service,
                "weekly_report",
                arguments={"days": span, "lag_days": lag},
            )
            warnings.extend(ignored)
            payload = merge_warnings(payload, warnings)
            if payload.get("ok"):
                data = payload.get("data")
                if isinstance(data, Mapping):
                    state.report.update(data)
            return payload

        return respond("seo_weekly_report", _run)

    return seo_weekly_report


_PERIOD_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$")


def build_report_diff(state: ToolState) -> Callable[..., CallToolResult]:
    """``seo_report_diff``: diff two stored weekly snapshots (no provider calls)."""

    def seo_report_diff(base: str = "", head: str = "") -> CallToolResult:
        def _run() -> Envelope:
            periods = {"base": base.strip(), "head": head.strip()}
            for name, value in periods.items():
                if value and not _PERIOD_PATTERN.match(value):
                    raise SeoError(
                        code=INVALID_INPUT,
                        message=f"{name} must be a stored period START..END",
                        next_action="pass e.g. 2026-11-02..2026-11-08, or leave it empty",
                    )
            payload, ignored = invoke(
                state.service,
                "report_diff",
                arguments={},
                optional=periods,
            )
            return merge_warnings(payload, ignored)

        return respond("seo_report_diff", _run)

    return seo_report_diff


def _report_from_payload(payload: Mapping[str, Any]) -> Report | None:
    """Rebuild a :class:`Report` from the ``Report.to_dict`` payload the service returns."""
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        return None

    sections: list[ReportSection] = []
    for item in raw_sections:
        if not isinstance(item, Mapping):
            continue
        raw_rows = item.get("rows")
        rows = (
            [dict(row) for row in raw_rows if isinstance(row, Mapping)]
            if isinstance(raw_rows, list)
            else []
        )
        sections.append(
            ReportSection(
                key=str(item.get("key") or ""),
                title=str(item.get("title") or ""),
                summary=str(item.get("summary") or ""),
                rows=rows,
                notes=_strings(item.get("notes")),
            )
        )

    period = _mapping(payload.get("period"))
    return Report(
        kind=str(payload.get("kind") or "weekly"),
        property_id=str(payload.get("property_id") or ""),
        period_start=str(period.get("start") or ""),
        period_end=str(period.get("end") or ""),
        headline=str(payload.get("headline") or ""),
        sections=sections,
        generated_at=str(payload.get("generated_at") or ""),
        limitations=_strings(payload.get("limitations")),
        warnings=_strings(payload.get("warnings")),
    )


def build_indexing_triage(state: ToolState) -> Callable[..., CallToolResult]:
    """``seo_indexing_triage``: bucket URL-level indexing events by root cause."""

    def seo_indexing_triage(max_examples: int = 5) -> CallToolResult:
        def _run() -> Envelope:
            examples, examples_warning = bounded_int(
                max_examples,
                name="max_examples",
                minimum=0,
                maximum=20,
                default=5,
            )
            warnings = [examples_warning] if examples_warning else []
            payload, ignored = invoke(state.service, "yandex_indexing", arguments={})
            warnings.extend(ignored)
            warnings.extend(_strings(payload.get("warnings")))
            if not payload.get("ok"):
                return merge_warnings(payload, warnings)

            data = _mapping(payload.get("data"))
            events = data.get("events")
            yandex_rows: list[dict[str, str]] = []
            if isinstance(events, list):
                for event in events:
                    if not isinstance(event, Mapping):
                        continue
                    yandex_rows.append(
                        {str(key): value for key, value in event.items() if isinstance(value, str)}
                    )

            buckets = triage(yandex=yandex_rows, max_examples=examples)
            provider_meta = _mapping(payload.get("meta"))
            limitations = _strings(provider_meta.get("limitations"))
            limitations.extend(
                [
                    "google: URL-level coverage is not exposed by the configured service",
                    "crawl: crawl issues are not exposed here; run seo_crawl_site separately",
                ]
            )
            if not buckets:
                warnings.append("yandex: no per-URL indexing events returned")

            meta: dict[str, Any] = {
                "sources": ["yandex_webmaster"],
                "buckets": len(buckets),
                "limitations": _unique(limitations),
                "truncated": bool(provider_meta.get("truncated", False)),
            }
            if "data_age_days" in provider_meta:
                meta["data_age_days"] = provider_meta["data_age_days"]

            return ok(
                {
                    "buckets": [
                        {
                            "reason": bucket.reason,
                            "count": bucket.count,
                            "examples": list(bucket.examples),
                            "next_action": bucket.next_action,
                            "engines": list(bucket.engines),
                        }
                        for bucket in buckets
                    ],
                    "sources": ["yandex_webmaster"],
                },
                meta=meta,
                warnings=_unique(warnings),
            )

        return respond("seo_indexing_triage", _run)

    return seo_indexing_triage


def build_opportunities(state: ToolState) -> Callable[..., CallToolResult]:
    """``seo_opportunities``: striking distance, CTR gaps and decay from GSC rows."""

    def seo_opportunities(
        days: int = OPPORTUNITY_DAYS_DEFAULT,
        limit: int = OPPORTUNITY_LIMIT_DEFAULT,
        min_impressions: int = 100,
    ) -> CallToolResult:
        def _run() -> Envelope:
            span, span_warning = bounded_int(
                days,
                name="days",
                minimum=1,
                maximum=90,
                default=OPPORTUNITY_DAYS_DEFAULT,
            )
            size, size_warning = bounded_limit(limit, default=OPPORTUNITY_LIMIT_DEFAULT)
            floor, floor_warning = bounded_int(
                min_impressions,
                name="min_impressions",
                minimum=0,
                maximum=1_000_000,
                default=100,
            )
            warnings = [
                warning for warning in (span_warning, size_warning, floor_warning) if warning
            ]
            (start, end), (previous_start, previous_end) = compare_period(span, lag_days=2)

            current, ignored = invoke(
                state.service,
                "gsc_query",
                arguments={
                    "start": start,
                    "end": end,
                    "dimensions": ("query", "page"),
                    "limit": size,
                },
            )
            warnings.extend(ignored)
            warnings.extend(_strings(current.get("warnings")))
            if not current.get("ok"):
                return merge_warnings(current, warnings)

            previous_rows: list[AnalysisRow] = []
            previous, previous_ignored = invoke(
                state.service,
                "gsc_query",
                arguments={
                    "start": previous_start,
                    "end": previous_end,
                    "dimensions": ("query", "page"),
                    "limit": size,
                },
            )
            warnings.extend(previous_ignored)
            if previous.get("ok"):
                previous_rows = _analysis_rows(_mapping(previous.get("data")).get("rows"))
                warnings.extend(_strings(previous.get("warnings")))
            else:
                error = _mapping(previous.get("error"))
                code = str(error.get("code") or "UPSTREAM_UNAVAILABLE")
                warnings.append(
                    f"previous period unavailable ({code}); decay candidates are skipped"
                )

            current_rows = _analysis_rows(_mapping(current.get("data")).get("rows"))
            found = find_opportunities(
                current_rows,
                previous=previous_rows or None,
                min_impressions=float(floor),
                limit=size,
            )
            if not current_rows:
                warnings.append("gsc: no query/page rows returned")
            if size > 0 and len(found) == size:
                warnings.append(f"opportunities: limited to {size}; raise limit to see more")

            current_meta = _mapping(current.get("meta"))
            limitations = _strings(current_meta.get("limitations"))
            limitations.append("expected_gain is a prioritisation estimate, not a traffic promise")
            meta: dict[str, Any] = {
                "sources": ["gsc"],
                "days": span,
                "limit": size,
                "min_impressions": floor,
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "previous_period": {
                    "start": previous_start.isoformat(),
                    "end": previous_end.isoformat(),
                },
                "limitations": _unique(limitations),
                "truncated": bool(current_meta.get("truncated", False))
                or (size > 0 and len(found) == size),
            }
            if "data_age_days" in current_meta:
                meta["data_age_days"] = current_meta["data_age_days"]

            return ok(
                {
                    "opportunities": [
                        {
                            "kind": item.kind,
                            "query": item.query,
                            "url": item.page,
                            "clicks": item.clicks,
                            "impressions": item.impressions,
                            "ctr": item.ctr,
                            "position": item.position,
                            "evidence": item.evidence,
                            "expected_gain": item.expected_clicks,
                        }
                        for item in found
                    ]
                },
                meta=meta,
                warnings=_unique(warnings),
            )

        return respond("seo_opportunities", _run)

    return seo_opportunities


def _analysis_rows(rows: object) -> list[AnalysisRow]:
    """Normalise service rows into the GSC-shaped mappings the analysis layer expects."""
    if not isinstance(rows, list):
        return []
    cleaned: list[AnalysisRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        values: AnalysisRow = {}
        for key, value in row.items():
            if value is None or isinstance(value, (str, int, float)):
                values[str(key)] = cast(float | str | None, value)
        cleaned.append(values)
    return cleaned


# -- write tools -----------------------------------------------------------------------------


def build_recrawl_request(state: ToolState) -> Callable[..., CallToolResult]:
    """``seo_recrawl_request``: gated Yandex recrawl queue write."""

    def seo_recrawl_request(
        host_id: str = "",
        urls: list[str] | None = None,
        allow_write: bool = False,
    ) -> CallToolResult:
        def _run() -> Envelope:
            _require_write(allow_write, tool="seo_recrawl_request")
            target_urls = _validate_urls(urls)
            handler = _write_handler(state.service, ("recrawl_request", "request_recrawl"))
            if handler is None:
                raise SeoError(
                    code=WRITE_DISABLED,
                    message="recrawl writes are not available in this build",
                    next_action=(
                        "use the CLI once the write layer lands; this server never "
                        "pretends a write happened"
                    ),
                    retryable=False,
                )
            return _call_write(
                handler,
                arguments={
                    "host_id": host_id.strip(),
                    "urls": target_urls,
                },
            )

        return respond("seo_recrawl_request", _run)

    return seo_recrawl_request


def build_sitemap_submit(state: ToolState) -> Callable[..., CallToolResult]:
    """``seo_sitemap_submit``: gated GSC/Bing sitemap submit or delete."""

    def seo_sitemap_submit(
        property: str = "",
        sitemap_url: str = "",
        action: str = "submit",
        allow_write: bool = False,
    ) -> CallToolResult:
        def _run() -> Envelope:
            _require_write(allow_write, tool="seo_sitemap_submit")
            target = property.strip()
            if not target:
                raise SeoError(
                    code=INVALID_INPUT,
                    message="property is required",
                    next_action="pass the GSC property id, e.g. sc-domain:example.com",
                )
            url = sitemap_url.strip()
            _require_http_url(url, name="sitemap_url")
            verb = action.strip().lower()
            if verb not in _SITEMAP_ACTIONS:
                raise SeoError(
                    code=INVALID_INPUT,
                    message=f"action must be one of: {_CSV.join(sorted(_SITEMAP_ACTIONS))}",
                    next_action="pass submit or delete",
                )
            handler = _write_handler(state.service, ("sitemap_submit", "submit_sitemap"))
            if handler is None:
                raise SeoError(
                    code=WRITE_DISABLED,
                    message="sitemap writes are not available in this build",
                    next_action=(
                        "use the CLI once the write layer lands; this server never "
                        "pretends a write happened"
                    ),
                    retryable=False,
                )
            return _call_write(
                handler,
                arguments={
                    "property": target,
                    "sitemap_url": url,
                    "action": verb,
                },
            )

        return respond("seo_sitemap_submit", _run)

    return seo_sitemap_submit


def _require_write(allow_write: bool, *, tool: str) -> None:
    """Refuse every write unless the caller explicitly opted in."""
    if allow_write:
        return
    raise SeoError(
        code=WRITE_DISABLED,
        message=f"{tool} is a write tool and is disabled by default",
        next_action=WRITE_NEXT_ACTION,
        retryable=False,
    )


def _validate_urls(urls: Sequence[str] | None) -> list[str]:
    if not urls:
        raise SeoError(
            code=INVALID_INPUT,
            message="urls must contain at least one absolute http(s) URL",
            next_action="pass urls=['https://example.com/page']",
        )
    cleaned: list[str] = []
    for value in urls:
        url = value.strip()
        _require_http_url(url, name="urls[]")
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def _write_handler(service: SeoService, names: Sequence[str]) -> Callable[..., Any] | None:
    for name in names:
        handler = getattr(service, name, None)
        if callable(handler):
            return cast(Callable[..., Any], handler)
    return None


def _call_write(
    handler: Callable[..., Any],
    *,
    arguments: Mapping[str, Any],
) -> Envelope:
    """Call a write method with only the keyword arguments it accepts."""
    accepted = _accepted_keyword_names(handler)
    kwargs = {key: value for key, value in arguments.items() if accepted is None or key in accepted}
    result = handler(**kwargs)
    if not isinstance(result, Mapping):
        raise SeoError(
            code=INTERNAL,
            message="the write method returned a malformed envelope",
            next_action="retry; if it persists report a bug",
        )
    return dict(result)


# -- registry pairing ------------------------------------------------------------------------

TOOL_BUILDERS: dict[str, ToolBuilder] = {
    "seo_properties": build_properties,
    "gsc_search_analytics": build_gsc_search_analytics,
    "yandex_query_stats": build_yandex_query_stats,
    "yandex_indexing_state": build_yandex_indexing_state,
    "seo_crawl_site": build_crawl_site,
    "seo_weekly_report": build_weekly_report,
    "seo_report_diff": build_report_diff,
    "seo_indexing_triage": build_indexing_triage,
    "seo_opportunities": build_opportunities,
    "seo_recrawl_request": build_recrawl_request,
    "seo_sitemap_submit": build_sitemap_submit,
}
