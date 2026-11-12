"""Render a report as plain text, Markdown or email-safe HTML.

The renderers are pure functions over :class:`~lv24_seo.report.models.Report`:

- :func:`render_text` fits a 100-column terminal and never emits ANSI;
- :func:`render_email_text` fits the 78-column width used by email and Telegram;
- :func:`render_html` is standalone and email-safe: inline CSS only, no scripts,
  no external assets and every value HTML-escaped;
- :func:`render_subject` builds a short, localized mail subject;
- :func:`render_markdown` stays the canonical English document used by MCP and
  report diffs.

Localization is presentation-only. The weekly presenter reads the structured
rows (``metric``, ``kind``, ``severity``, ``source``...) and builds its own copy
instead of reusing English builder prose. Missing numbers stay missing: an
unknown KPI is rendered as a localized "no data", never as ``0``.

Honesty rules: limitations always come after the data, an empty issue section
never claims the site is clean, and no renderer creates a timestamp of its own —
the only time shown is ``report.generated_at``.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from lv24_seo.report.i18n import translator
from lv24_seo.report.models import Report, ReportSection

__all__ = [
    "render_email_text",
    "render_html",
    "render_markdown",
    "render_subject",
    "render_text",
]

_TEXT_WIDTH = 100
_EMAIL_WIDTH = 78
_SUBJECT_LIMIT = 78
_PROPERTY_LIMIT = 24
_LIMITATIONS_TITLE = "Data limitations"

# Email palette: neon is only ever used on the dark header/footer.
_DARK = "#080D12"
_NEON = "#5FD4C4"
_ON_DARK = "#E4E7EC"
_ON_DARK_MUTED = "#98A2B3"
_PAGE_BG = "#EEF2F5"
_CARD_BG = "#FFFFFF"
_CARD_ALT = "#F7F9FB"
_BORDER = "#E4E7EC"
_TEXT = "#101828"
_MUTED = "#667085"
_SOFT = "#475467"
_GREEN = "#067647"
_RED = "#B42318"

_FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
    "'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif"
)
_BODY_STYLE = (
    f"margin:0;padding:0;background-color:{_PAGE_BG};font-family:{_FONT_STACK};"
    "-webkit-text-size-adjust:100%;"
)
_PREHEADER_STYLE = (
    "display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;"
    "opacity:0;overflow:hidden;mso-hide:all;"
)
_H1_STYLE = (
    f"margin:0;font-size:26px;line-height:1.25;font-weight:700;color:{_TEXT};word-break:break-word;"
)
_HERO_META_STYLE = f"margin:8px 0 0 0;font-size:14px;line-height:1.5;color:{_MUTED};"
_VERDICT_STYLE = f"margin:10px 0 0 0;font-size:15px;line-height:1.5;color:{_SOFT};"
_H2_STYLE = f"margin:28px 0 10px 0;font-size:16px;line-height:1.35;font-weight:700;color:{_TEXT};"
_P_STYLE = f"margin:0 0 10px 0;font-size:14px;line-height:1.55;color:{_SOFT};"
_SMALL_STYLE = f"margin:0;font-size:12px;line-height:1.5;color:{_MUTED};"
_UL_STYLE = f"margin:0 0 4px 0;padding-left:18px;font-size:13px;line-height:1.55;color:{_SOFT};"
_CARD_TABLE_STYLE = (
    f"width:100%;border-collapse:separate;background-color:{_CARD_ALT};"
    f"border:1px solid {_BORDER};border-radius:10px;"
)
_CARD_CELL_STYLE = "padding:14px 14px 12px 14px;"
_KPI_LABEL_STYLE = (
    f"font-size:11px;line-height:1.3;font-weight:700;letter-spacing:0.06em;"
    f"text-transform:uppercase;color:{_MUTED};"
)
_KPI_VALUE_STYLE = f"margin-top:6px;font-size:24px;line-height:1.2;font-weight:700;color:{_TEXT};"
_KPI_DELTA_STYLE = "margin-top:4px;font-size:12px;line-height:1.4;"
_KPI_PREV_STYLE = f"margin-top:2px;font-size:12px;line-height:1.4;color:{_MUTED};"
_BADGE_STYLE = (
    "display:inline-block;margin-top:8px;padding:2px 7px;border-radius:999px;"
    f"background-color:{_TEXT};color:#FFFFFF;font-size:10px;line-height:1.5;"
    "font-weight:600;"
)
_DATA_TABLE_STYLE = f"width:100%;border-collapse:collapse;font-size:13px;color:{_SOFT};"
_TH_STYLE = (
    f"padding:6px 0;border-bottom:1px solid {_BORDER};text-align:left;"
    f"font-size:11px;line-height:1.4;font-weight:700;color:{_MUTED};"
)
_TD_STYLE = f"padding:7px 0;border-bottom:1px solid {_BORDER};vertical-align:top;"
_FOOTER_ITEM_STYLE = "margin:0;font-size:12px;line-height:1.6;"

# C0/C1 control bytes and bidi overrides have no place in report parameters:
# they can break terminals, mail headers and left-to-right rendering.
_UNSAFE_CHARACTERS: dict[int, None] = dict.fromkeys(
    (
        *(code for code in range(32) if code not in (9, 10)),
        127,
        *range(128, 160),
        1564,
        8206,
        8207,
        *range(8234, 8239),
        *range(8294, 8298),
    )
)

# ``_stringify`` keeps the historical behaviour used by Markdown. Product
# renderers use ``_clean``/``_inline`` so they can also drop bidi overrides.
_CONTROL_CHARACTERS: dict[int, None] = {
    code: None for code in range(0x20) if code not in (0x09, 0x0A)
}
_CONTROL_CHARACTERS[0x7F] = None

_KPI_KEYS: tuple[str, ...] = ("clicks", "impressions", "ctr", "position")
_SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "unknown")
_SEVERITY_KEYS: dict[str, str] = {
    "critical": "issue.critical",
    "high": "issue.high",
    "medium": "issue.medium",
    "low": "issue.low",
    "unknown": "issue.unknown",
}
_SOURCE_ORDER: tuple[str, ...] = (
    "gsc",
    "yandex_webmaster",
    "yandex_metrika",
    "bing",
    "crux",
    "psi",
)
_ENGINE_ALIASES: dict[str, str] = {
    "gsc": "gsc",
    "google": "gsc",
    "google_search_console": "gsc",
    "yandex": "yandex_webmaster",
    "yandex_webmaster": "yandex_webmaster",
    "webmaster": "yandex_webmaster",
    "metrika": "yandex_metrika",
    "yandex_metrika": "yandex_metrika",
    "bing": "bing",
    "crux": "crux",
    "chrome_ux_report": "crux",
    "psi": "psi",
    "pagespeed": "psi",
    "pagespeed_insights": "psi",
    "page_speed_insights": "psi",
}
_METRIC_LABEL_KEYS: dict[str, str] = {
    "clicks": "unit.clicks",
    "impressions": "unit.impressions",
    "ctr": "kpi.ctr",
    "position": "kpi.position",
    "visits": "unit.sessions",
    "sessions": "unit.sessions",
}
_LIMITATION_KEYS: dict[str, str] = {
    "top_3000_queries": "notes.limitation.top_3000_queries",
    "no_query_page_join": "notes.limitation.no_query_page_join",
    "sessions_not_clicks": "notes.limitation.sessions_not_clicks",
    "rows_cap_50000": "notes.limitation.rows_cap_50000",
    "sampled": "notes.limitation.sampled",
    "low_traffic": "notes.limitation.low_traffic",
    "degraded_host": "notes.limitation.degraded_host",
    "one_url_per_call": "notes.limitation.one_url_per_call",
    "no_per_url_sitemap_errors": "notes.limitation.no_per_url_sitemap_errors",
    "token_expiring": "notes.limitation.token_expiring",
    "paid_provider": "notes.limitation.paid_provider",
    "partial_response": "notes.limitation.partial_response",
}
# Builder warnings that are system statements, not provider prose. They are
# localized; anything else stays verbatim.
_WARNING_KEYS: dict[str, str] = {
    "no metrics for either period; delta rows are all zero": "warning.no_metrics",
    "previous period has no metrics; deltas cannot be significant": ("warning.previous_no_metrics"),
    "no provider rows; traffic and opportunities are empty": "warning.no_provider_rows",
}
# An unknown snake_case limitation code is still a code, not prose. Requiring an
# underscore keeps ordinary single-word text from being mislabelled.
_SNAKE_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_ASCII_SPACE_RE = re.compile(r"[ \t\r\n]+")
_HIDDEN_ROW_KEYS: frozenset[str] = frozenset({"evidence", "rule_ids", "limitations"})
# Raw delta-row schema fields. An unknown metric row may expose a humanized
# label and safe free text, never these keys or their boolean values.
_METRIC_SCHEMA_KEYS: frozenset[str] = frozenset(
    {
        "metric",
        "current",
        "previous",
        "abs_delta",
        "pct_delta",
        "significant",
        "direction",
        "good",
    }
)
_WEEKLY_KEYS: frozenset[str] = frozenset(
    {
        "headline",
        "gsc",
        "traffic",
        "opportunities",
        "issues",
        "sources",
        "yandex_webmaster",
        "yandex_metrika",
    }
)

_ENGLISH: dict[str, str] = {
    "brand.name": "lv24-seo",
    "brand.suffix": "SEO report",
    "report.type.weekly": "Weekly report",
    "report.type.indexing": "Indexing report",
    "report.type.opportunities": "Opportunities report",
    "report.type.audit": "Site audit",
    "label.generated": "Generated",
    "label.property": "Property",
    "label.period": "Period",
    "kpi.clicks": "Clicks",
    "kpi.impressions": "Impressions",
    "kpi.ctr": "CTR",
    "kpi.position": "Avg. position",
    "kpi.no_data": "No data",
    "kpi.no_comparison": "No comparison",
    "kpi.previous": "Previous",
    "kpi.significant": "Significant",
    "kpi.better": "Better",
    "kpi.worse": "Worse",
    "kpi.flat": "Flat",
    "headline.no_data": "No search data for this period.",
    "headline.no_comparison": "No previous period to compare.",
    "headline.improving": "Performance is improving.",
    "headline.worsening": "Performance is declining.",
    "headline.no_change": "Performance is stable.",
    "section.opportunities": "Opportunities",
    "section.issues": "Issues",
    "section.sources": "Data sources",
    "section.notes": "Data notes",
    "section.warnings": "Warnings",
    "opportunity.kind.striking_distance": "Striking distance",
    "opportunity.kind.ctr_gap": "CTR gap",
    "opportunity.kind.decay": "Declining page",
    "opportunity.kind.unknown": "Opportunity",
    "opportunity.query": "Query",
    "opportunity.page": "Page",
    "opportunity.page_missing": "Page not set",
    "opportunity.position": "Position",
    "opportunity.impressions": "Impressions",
    "opportunity.expected_clicks": "Expected clicks",
    "opportunity.estimate": "Estimate",
    "opportunity.none": "No opportunities found for this period.",
    "opportunity.no_data": "No query data to evaluate opportunities.",
    "issue.critical": "Critical",
    "issue.high": "High",
    "issue.medium": "Medium",
    "issue.low": "Low",
    "issue.unknown": "Unknown",
    "issue.no_data": "No issue data was provided.",
    "issue.zero": "No issues found.",
    "source.rows": "Rows",
    "source.missing": "No data",
    "source.kind.traffic": "Traffic data",
    "source.kind.field": "Field data",
    "source.kind.lab": "Lab data",
    "engine.gsc": "Google Search Console",
    "engine.yandex_webmaster": "Yandex Webmaster",
    "engine.yandex_metrika": "Yandex Metrica",
    "engine.bing": "Bing Webmaster Tools",
    "engine.crux": "Chrome UX Report",
    "engine.psi": "PageSpeed Insights",
    "unit.clicks": "clicks",
    "unit.impressions": "impressions",
    "unit.rows": "rows",
    "unit.sessions": "sessions",
    "unit.pp": "pp",
    "notes.original": "Provider text is shown in its original language.",
    "notes.estimates": "Expected clicks are a prioritisation estimate, not a traffic promise.",
    "notes.other_limitation": "Other limitation ({code})",
    "notes.source_totals": (
        "Source metrics are sums of the fetched top rows, not full-window totals."
    ),
    "notes.limitation.top_3000_queries": "Yandex covers only the top 3,000 queries.",
    "notes.limitation.no_query_page_join": "Yandex does not join queries to pages.",
    "notes.limitation.sessions_not_clicks": "Yandex Metrica counts sessions, not clicks.",
    "notes.limitation.rows_cap_50000": "The provider caps responses at 50,000 rows.",
    "notes.limitation.sampled": "The provider returned sampled data.",
    "notes.limitation.low_traffic": "Traffic is too low for reliable field data.",
    "notes.limitation.degraded_host": "The provider reported a degraded host.",
    "notes.limitation.one_url_per_call": "The provider accepts one URL per call.",
    "notes.limitation.no_per_url_sitemap_errors": (
        "The provider does not report sitemap errors per URL."
    ),
    "notes.limitation.token_expiring": "The provider token expires soon.",
    "notes.limitation.paid_provider": "This provider requires a paid plan.",
    "notes.limitation.partial_response": "The provider returned a partial response.",
    "warning.no_metrics": "No metrics for either period; delta rows are all zero.",
    "warning.previous_no_metrics": (
        "The previous period has no metrics, so deltas cannot be significant."
    ),
    "warning.no_provider_rows": ("No provider returned rows; traffic and opportunities are empty."),
    "source.top_rows": "top query rows",
    "opportunity.top3": "Top 3 by expected clicks",
    "footer.generated_by": "Generated by lv24-seo",
    "footer.property_id": "Property ID: {property_id}",
    "generic.no_sections": "No sections in this report.",
    "generic.other_metrics": "Other metrics",
    "generic.field": "Field",
    "generic.value": "Value",
    "subject.weekly": "Weekly SEO report: {period}",
    "subject.no_data": "No search data for {period}",
    "subject.no_comparison": "SEO report without comparison: {period}",
    "subject.critical": "Critical SEO issues: {count}",
    "subject.clicks": "Clicks: {change}",
    "subject.impressions": "Impressions: {change}",
    "subject.ctr": "CTR: {change}",
    "subject.position": "Avg. position: {change}",
}
_ENGLISH_COUNTS: dict[str, tuple[str, str]] = {
    "headline.significant": (
        "{count} metric changed significantly",
        "{count} metrics changed significantly",
    ),
    "issue.count": ("{count} issue", "{count} issues"),
    "issue.checks": ("{count} rule", "{count} rules"),
}

_REPORT_TYPES: dict[str, str] = {
    "weekly": "report.type.weekly",
    "indexing": "report.type.indexing",
    "opportunities": "report.type.opportunities",
    "audit": "report.type.audit",
}
_HTML_LANG: dict[str, str] = {"en": "en", "ru": "ru", "zh": "zh-Hans"}
_FALLBACK_MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


# -- sanitizing and formatting ---------------------------------------------------------------


def _stringify(value: Any) -> str:
    """Return a stable display string, dropping terminal control bytes."""
    if value is None:
        return ""
    return str(value).translate(_CONTROL_CHARACTERS)


def _clean(value: Any) -> str:
    """Return text without control or bidi-override characters."""
    if value is None:
        return ""
    return str(value).translate(_UNSAFE_CHARACTERS)


def _inline(value: Any) -> str:
    """Return a single-line version of *value*."""
    return " ".join(_clean(value).split())


def _numeric(value: Any) -> str:
    """Strip control/bidi characters without collapsing whitespace.

    Russian numbers use a no-break space as the thousands separator, so the
    numeric path must never run through :func:`_inline` (which would turn
    ``1\u00a0234`` into ``1 234``).
    """
    return _clean(value)


def _prose(value: object) -> str:
    """Return a scalar as one line; mappings and other objects are not prose."""
    if isinstance(value, str):
        return _inline(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _inline(value)
    return ""


def _mapping_rows(rows: Sequence[object]) -> list[Mapping[str, Any]]:
    """Keep only mapping rows; report data may arrive from untyped JSON."""
    return [row for row in rows if isinstance(row, Mapping)]


def _number(value: Any) -> float | None:
    """Return a finite float, or ``None`` when the value is missing or unusable."""
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


def _count_value(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0 or number != int(number):
        return None
    return int(number)


def _format_number(value: float, digits: int = 0) -> str:
    if not math.isfinite(value):
        return ""
    return f"{value:,.{digits}f}"


def _format_percent(value: float, *, signed: bool = False, digits: int = 1) -> str:
    if not math.isfinite(value):
        return ""
    text = f"{value:,.{digits}f}%"
    if value > 0 and signed:
        return f"+{text}"
    return text


def _format_position(value: float) -> str:
    if not math.isfinite(value):
        return ""
    return f"{value:.1f}"


def _format_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    return f"{parsed.day} {_FALLBACK_MONTHS[parsed.month - 1]} {parsed.year}"


def _english(key: str, params: Mapping[str, object]) -> str:
    template = _ENGLISH.get(key)
    if template is None:
        return key.replace(".", " ").replace("_", " ").strip()
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


def _english_count(key: str, n: int, params: Mapping[str, object]) -> str:
    forms = _ENGLISH_COUNTS.get(key)
    if forms is None:
        return key.replace(".", " ").strip()
    template = forms[0] if n == 1 else forms[1]
    values = {"count": _format_number(float(n)), **params}
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


class _Messages:
    """Small, defensive facade over the presentation translator.

    A missing key or an unexpected failure degrades to the built-in English
    copy instead of leaking a key name into a customer email.
    """

    def __init__(self, locale: str | None = None) -> None:
        self._tr: Any = translator(locale)
        self.locale = _inline(getattr(self._tr, "locale", "")) or _inline(locale) or "en"
        self.html_lang = _inline(getattr(self._tr, "html_lang", "")) or _HTML_LANG.get(
            self.locale, self.locale
        )

    def t(self, key: str, **params: object) -> str:
        try:
            value = self._tr.t(key, **params)
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            value = ""
        text = _ASCII_SPACE_RE.sub(" ", _clean(value)).strip()
        if not text or text == key:
            return _english(key, params)
        return text

    def count(self, key: str, n: int, **params: object) -> str:
        try:
            value = self._tr.count(key, n, **params)
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            value = ""
        text = _ASCII_SPACE_RE.sub(" ", _clean(value)).strip()
        if not text or text == key:
            return _english_count(key, n, params)
        return text

    def number(self, value: float, digits: int = 0) -> str:
        try:
            text = _numeric(self._tr.number(value, digits=digits))
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            text = ""
        return text or _format_number(value, digits)

    def percent(
        self,
        value: float,
        *,
        signed: bool = False,
        digits: int = 1,
        scale: str = "percent",
    ) -> str:
        try:
            text = _numeric(self._tr.percent(value, signed=signed, digits=digits, scale=scale))
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            text = ""
        if text:
            return text
        scaled = value * 100.0 if scale == "ratio" else value
        return _format_percent(scaled, signed=signed, digits=digits)

    def position(self, value: float) -> str:
        try:
            text = _numeric(self._tr.position(value))
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            text = ""
        return text or _format_position(value)

    def pp(self, value: float, *, signed: bool = False, digits: int = 2) -> str:
        """Format percentage points with the localized ``unit.pp`` suffix."""
        text = self.number(value, digits=digits)
        if not text:
            return ""
        if signed and value > 0:
            text = f"+{text}"
        return f"{text} {self.t('unit.pp')}"

    def datetime(self, value: object) -> str:
        try:
            text = _inline(self._tr.datetime(value))
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            text = ""
        return text or _inline(value)

    def period(self, start: object, end: object) -> str:
        try:
            text = _inline(self._tr.period(start, end))
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            text = ""
        return text or _period_pair(start, end, short=False)

    def short_period(self, start: object, end: object) -> str:
        try:
            text = _inline(self._tr.short_period(start, end))
        except Exception:  # pragma: no cover - translator contract degrades, never crashes
            text = ""
        return text or _period_pair(start, end, short=True)


def _period_pair(start: object, end: object, *, short: bool) -> str:
    first = _inline(start)
    last = _inline(end)
    if short:
        return f"{first}..{last}" if first and last else first or last
    if not first or not last:
        return first or last
    return f"{_format_date(first)} \u2013 {_format_date(last)}"


# -- view model ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Kpi:
    key: str
    label: str
    value: float | None
    previous: float | None
    abs_delta: float | None
    pct_delta: float | None
    significant: bool
    good: bool | None


@dataclass(frozen=True, slots=True)
class _Opportunity:
    kind_label: str
    query: str
    page: str
    page_missing: bool
    evidence: str
    estimate: str


@dataclass(frozen=True, slots=True)
class _IssueGroup:
    severity: str
    label: str
    count: int


@dataclass(slots=True)
class _Issues:
    total: int = 0
    unique_rules: int = 0
    groups: list[_IssueGroup] = field(default_factory=list)
    message: str = ""


@dataclass(frozen=True, slots=True)
class _Source:
    name: str
    kind: str
    metrics: tuple[str, ...]
    rows_text: str
    missing: bool
    scope: str = ""


@dataclass(slots=True)
class _SourceData:
    raw_name: str
    canonical: str
    kind: str = "traffic"
    rows: int | None = None
    missing: bool = False
    metrics: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _GenericSection:
    title: str
    summary: str
    rows: tuple[tuple[tuple[str, str], ...], ...]
    notes: tuple[str, ...]


@dataclass(slots=True)
class _View:
    kind: str
    report_type: str
    property_id: str
    property_label: str
    period: str
    period_short: str
    generated: str
    verdict: str
    kpis: list[_Kpi]
    opportunities: list[_Opportunity]
    opportunity_note: str
    opportunity_footnote: str
    opportunity_scope: str
    issues: _Issues
    sources: list[_Source]
    sources_empty: bool
    notes: list[str]
    warnings: list[str]
    extras: list[_GenericSection]
    intro: str
    critical_count: int
    has_data: bool
    has_comparison: bool
    biggest: _Kpi | None


def _display_property(property_id: str) -> str:
    """Return a readable property label: no ``sc-domain:`` prefix and no scheme."""
    text = _inline(property_id)
    if not text:
        return ""
    lowered = text.casefold()
    if lowered.startswith("sc-domain:"):
        text = text[len("sc-domain:") :].strip()
    elif "://" in text:
        text = text.split("://", 1)[1]
    if text.startswith("//"):
        text = text[2:]
    if "/" in text:
        host, _, rest = text.partition("/")
        text = host if not rest.strip("/") else f"{host}/{rest.strip('/')}"
    return text.rstrip("/") or _inline(property_id)


def _report_type(msgs: _Messages, kind: str) -> str:
    key = _REPORT_TYPES.get(kind)
    return msgs.t(key) if key is not None else msgs.t("brand.suffix")


def _metric_key(row: Mapping[str, Any]) -> str | None:
    value = row.get("metric")
    if not isinstance(value, str):
        return None
    key = value.strip().casefold()
    return key if key in _KPI_KEYS else None


def _flag(value: object) -> bool:
    return value is True


def _optional_flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _kpi_section_rows(report: Report) -> list[Mapping[str, Any]]:
    for key in ("headline", "gsc"):
        section = report.section(key)
        if section is None:
            continue
        rows = _mapping_rows(section.rows)
        if any(_metric_key(row) for row in rows):
            return rows
    return []


def _has_metric_values(report: Report) -> bool:
    """True when a metric row carries a current value, known metric or not.

    Unknown metric rows are not rendered, but they still prove the report has
    data; ``has_data`` must not turn them into a "no search data" verdict.
    """
    for key in ("headline", "gsc"):
        section = report.section(key)
        if section is None:
            continue
        for row in _mapping_rows(section.rows):
            if _number(row.get("current")) is not None:
                return True
    return False


def _build_kpis(report: Report, msgs: _Messages) -> list[_Kpi]:
    rows = _kpi_section_rows(report)
    by_metric: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        metric = _metric_key(row)
        if metric is not None:
            by_metric.setdefault(metric, row)

    kpis: list[_Kpi] = []
    for key in _KPI_KEYS:
        row = by_metric.get(key, {})
        value = _number(row.get("current"))
        previous = _number(row.get("previous"))
        abs_delta = _number(row.get("abs_delta"))
        pct_delta = _number(row.get("pct_delta"))
        if abs_delta is None and value is not None and previous is not None:
            abs_delta = value - previous
        if pct_delta is None and value is not None and previous is not None and previous != 0:
            pct_delta = round((value - previous) / previous * 100.0, 4)
        if abs_delta is not None and key == "position":
            abs_delta = round(abs_delta, 1)
        kpis.append(
            _Kpi(
                key=key,
                label=msgs.t(f"kpi.{key}"),
                value=value,
                previous=previous,
                abs_delta=abs_delta,
                pct_delta=pct_delta,
                significant=_flag(row.get("significant")),
                good=_optional_flag(row.get("good")),
            )
        )
    return kpis


def _kpi_value(kpi: _Kpi, msgs: _Messages) -> str:
    if kpi.value is None:
        return msgs.t("kpi.no_data")
    return _format_metric(kpi.key, kpi.value, msgs)


def _format_metric(key: str, value: float, msgs: _Messages) -> str:
    if key == "position":
        return msgs.position(value)
    if key == "ctr":
        return msgs.percent(value, digits=2, scale="ratio")
    return msgs.number(value)


def _kpi_delta(kpi: _Kpi, msgs: _Messages) -> str:
    if kpi.value is None:
        return ""
    if kpi.previous is None:
        return msgs.t("kpi.no_comparison")
    if kpi.key == "ctr":
        # CTR lives on a 0..1 ratio scale; its delta is percentage points, not
        # a relative change, so ``pct_delta`` is never used here.
        return msgs.pp((kpi.value - kpi.previous) * 100.0, signed=True, digits=2)
    if kpi.key == "position":
        delta = kpi.abs_delta
        if delta is None:
            delta = kpi.value - kpi.previous
        if abs(delta) < 0.05:
            return f"{msgs.position(0.0)} ({msgs.t('kpi.flat')})"
        label = msgs.t("kpi.better") if delta < 0 else msgs.t("kpi.worse")
        return f"{_signed_position(delta, msgs)} ({label})"
    if kpi.pct_delta is None:
        if kpi.abs_delta is not None:
            return _signed_number(kpi.abs_delta, msgs)
        return msgs.t("kpi.no_comparison")
    return msgs.percent(kpi.pct_delta, signed=True, digits=1)


def _signed_number(value: float, msgs: _Messages) -> str:
    digits = 0 if float(value).is_integer() else 1
    text = msgs.number(value, digits=digits)
    if value > 0:
        return f"+{text}"
    return text


def _signed_position(value: float, msgs: _Messages) -> str:
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}{msgs.position(abs(value))}"


def _kpi_previous(kpi: _Kpi, msgs: _Messages) -> str:
    if kpi.previous is None:
        return ""
    return f"{msgs.t('kpi.previous')} {_format_metric(kpi.key, kpi.previous, msgs)}"


def _kpi_magnitude(kpi: _Kpi) -> float:
    if kpi.key == "position":
        delta = kpi.abs_delta
        if delta is None and kpi.value is not None and kpi.previous is not None:
            delta = kpi.value - kpi.previous
        return abs(delta or 0.0) * 10.0
    if kpi.key == "ctr":
        if kpi.value is None or kpi.previous is None:
            return 0.0
        return abs((kpi.value - kpi.previous) * 100.0)
    return abs(kpi.pct_delta or 0.0)


def _biggest_kpi(kpis: Sequence[_Kpi]) -> _Kpi | None:
    # A subject change is only honest when a previous value exists.
    significant = [
        kpi
        for kpi in kpis
        if kpi.significant and kpi.value is not None and kpi.previous is not None
    ]
    if not significant:
        return None
    return max(significant, key=_kpi_magnitude)


def _verdict(kpis: Sequence[_Kpi], *, has_data: bool, has_comparison: bool, msgs: _Messages) -> str:
    if not has_data:
        return msgs.t("headline.no_data")
    if not has_comparison:
        return msgs.t("headline.no_comparison")
    good = sum(1 for kpi in kpis if kpi.significant and kpi.good is True)
    bad = sum(1 for kpi in kpis if kpi.significant and kpi.good is False)
    if good and bad:
        return msgs.count("headline.significant", good + bad)
    if good:
        return msgs.t("headline.improving")
    if bad:
        return msgs.t("headline.worsening")
    return msgs.t("headline.no_change")


def _opportunity_kind(kind: str, msgs: _Messages) -> str:
    if kind in {"striking_distance", "ctr_gap", "decay"}:
        return msgs.t(f"opportunity.kind.{kind}")
    return msgs.t("opportunity.kind.unknown")


def _compact_number(value: float, msgs: _Messages) -> str:
    digits = 0 if float(value).is_integer() else 1
    return msgs.number(value, digits=digits)


def _opportunity_evidence(row: Mapping[str, Any], kind: str, msgs: _Messages) -> str:
    clicks = _number(row.get("clicks"))
    impressions = _number(row.get("impressions"))
    ctr = _number(row.get("ctr"))
    position = _number(row.get("position"))
    expected = _number(row.get("expected_clicks"))

    if kind == "decay" and clicks is not None and expected is not None:
        previous = clicks + expected
        return (
            f"{msgs.t('kpi.clicks')} {_compact_number(previous, msgs)}"
            f" \u2192 {_compact_number(clicks, msgs)}"
        )
    if position is not None and position > 0 and impressions is not None:
        position_text = (
            f"{msgs.t('opportunity.position')} {msgs.position(position)}"
            f" · {msgs.t('opportunity.impressions')} {msgs.number(impressions)}"
        )
        if kind == "ctr_gap" and ctr is not None and 0 <= ctr <= 1:
            ctr_text = msgs.percent(ctr, digits=2, scale="ratio")
            return f"{msgs.t('kpi.ctr')} {ctr_text} · {position_text}"
        return position_text
    if impressions is not None:
        return f"{msgs.t('opportunity.impressions')} {msgs.number(impressions)}"
    if clicks is not None:
        return f"{msgs.t('kpi.clicks')} {msgs.number(clicks)}"
    return ""


def _opportunity_rank(row: Mapping[str, Any]) -> float:
    expected = _number(row.get("expected_clicks"))
    return expected if expected is not None else -1.0


def _build_opportunities(
    report: Report, msgs: _Messages
) -> tuple[list[_Opportunity], str, str, str]:
    section = report.section("opportunities")
    rows = _mapping_rows(section.rows if section else [])

    # Email shows one line per (query, page); keep the row with the highest
    # expected clicks and ignore the evidence mapping entirely.
    best: dict[tuple[str, str], Mapping[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        query = _prose(row.get("query"))
        if not query:
            continue
        page = _prose(row.get("page"))
        key = (query.casefold(), page.casefold())
        if key not in best:
            best[key] = row
            order.append(key)
        elif _opportunity_rank(row) > _opportunity_rank(best[key]):
            best[key] = row

    ranked = sorted(order, key=lambda key: _opportunity_rank(best[key]), reverse=True)
    items: list[_Opportunity] = []
    for key in ranked[:3]:
        row = best[key]
        kind = _prose(row.get("kind")).casefold()
        query = _prose(row.get("query"))
        page = _prose(row.get("page"))
        estimate = ""
        expected = _number(row.get("expected_clicks"))
        if expected is not None:
            estimate = f"{msgs.t('opportunity.expected_clicks')}: {_compact_number(expected, msgs)}"
        items.append(
            _Opportunity(
                kind_label=_opportunity_kind(kind, msgs),
                query=query,
                page=page or msgs.t("opportunity.page_missing"),
                page_missing=not page,
                evidence=_opportunity_evidence(row, kind, msgs),
                estimate=estimate,
            )
        )

    note = ""
    footnote = msgs.t("notes.estimates") if items else ""
    scope = msgs.t("opportunity.top3") if items else ""
    if not items:
        note = _opportunity_empty_note(report, msgs)
    return items, note, footnote, scope


def _opportunity_empty_note(report: Report, msgs: _Messages) -> str:
    notes: list[str] = []
    for key in ("opportunities", "sources"):
        section = report.section(key)
        if section is not None:
            notes.extend(section.notes)
    hint = " ".join(_prose(note) for note in notes).casefold()
    if "no gsc query rows" in hint or "no query rows" in hint:
        return msgs.t("opportunity.no_data")
    if not _has_query_rows(report):
        return msgs.t("opportunity.no_data")
    return msgs.t("opportunity.none")


def _has_query_rows(report: Report) -> bool:
    """True when some fetched row actually carries a query or a page.

    Metric-only rows (``metric``/``current``) prove that a provider answered,
    not that query rows exist, so they must not turn "no query data" into
    "no opportunities found".
    """
    for key in ("gsc", "traffic", "sources"):
        section = report.section(key)
        if section is None:
            continue
        for row in _mapping_rows(section.rows):
            if _prose(row.get("query")) or _prose(row.get("page")):
                return True
    return False


def _build_issues(report: Report, msgs: _Messages) -> _Issues:
    section = report.section("issues")
    rows = _mapping_rows(section.rows if section else [])
    groups: dict[str, int] = {}
    rules: set[str] = set()
    saw_row = False
    for row in rows:
        count = _count_value(row.get("count"))
        if count is None:
            continue
        saw_row = True
        severity = _prose(row.get("severity")).casefold() or "unknown"
        groups[severity] = groups.get(severity, 0) + count
        rule_ids = row.get("rule_ids")
        if isinstance(rule_ids, (list, tuple)):
            for rule in rule_ids:
                text = _inline(rule)
                if text:
                    rules.add(text)

    total = sum(groups.values())
    if not saw_row:
        return _Issues(message=msgs.t("issue.no_data"))
    if total == 0:
        return _Issues(total=0, unique_rules=len(rules), message=msgs.t("issue.zero"))

    ordered = sorted(
        groups,
        key=lambda severity: (
            _SEVERITY_ORDER.index(severity)
            if severity in _SEVERITY_ORDER
            else len(_SEVERITY_ORDER),
            severity,
        ),
    )
    issue_groups = [
        _IssueGroup(
            severity=severity,
            label=(msgs.t(_SEVERITY_KEYS[severity]) if severity in _SEVERITY_KEYS else severity),
            count=groups[severity],
        )
        for severity in ordered
        if groups[severity] > 0
    ]
    return _Issues(total=total, unique_rules=len(rules), groups=issue_groups)


def _canonical_source(name: str) -> str:
    key = _inline(name).casefold().replace("-", "_").replace(" ", "_")
    return _ENGINE_ALIASES.get(key, key)


def _engine_name(canonical: str, raw: str, msgs: _Messages) -> str:
    if canonical in _SOURCE_ORDER:
        return msgs.t(f"engine.{canonical}")
    return raw


def _source_metrics(row: Mapping[str, Any], canonical: str, msgs: _Messages) -> list[str]:
    # Metrica reports sessions, never search clicks: the fusion row carries a
    # structural ``clicks=0.0`` that must not be shown.
    if canonical == "yandex_metrika":
        return []
    metrics: list[str] = []
    clicks = _number(row.get("clicks"))
    impressions = _number(row.get("impressions"))
    ctr = _number(row.get("ctr"))
    position = _number(row.get("position"))
    if clicks is not None:
        metrics.append(f"{msgs.t('unit.clicks')} {msgs.number(clicks)}")
    if impressions is not None:
        metrics.append(f"{msgs.t('unit.impressions')} {msgs.number(impressions)}")
    if ctr is not None and 0 <= ctr <= 1:
        metrics.append(f"{msgs.t('kpi.ctr')} {msgs.percent(ctr, digits=2, scale='ratio')}")
    if position is not None and position > 0:
        metrics.append(f"{msgs.t('kpi.position')} {msgs.position(position)}")
    return metrics


def _fallback_metric_metrics(
    rows: Sequence[Mapping[str, Any]], msgs: _Messages, *, sessions_only: bool = False
) -> list[str]:
    totals: dict[str, float] = {}
    for row in rows:
        metric = _prose(row.get("metric")).casefold()
        value = _number(row.get("current"))
        if not metric or value is None:
            continue
        if sessions_only and metric not in {"visits", "users", "sessions"}:
            continue
        totals[metric] = totals.get(metric, 0.0) + value
    metrics: list[str] = []
    for metric, value in totals.items():
        label = msgs.t(_METRIC_LABEL_KEYS[metric]) if metric in _METRIC_LABEL_KEYS else metric
        if metric == "ctr" and 0 <= value <= 1:
            formatted = msgs.percent(value, digits=2, scale="ratio")
        elif metric == "position":
            formatted = msgs.position(value)
        else:
            formatted = msgs.number(value)
        metrics.append(f"{label} {formatted}")
    return metrics


def _collect_sources(report: Report, msgs: _Messages) -> list[_Source]:
    collected: dict[str, _SourceData] = {}

    def entry(raw: str, canonical: str) -> _SourceData:
        if canonical not in collected:
            collected[canonical] = _SourceData(raw_name=raw, canonical=canonical)
        return collected[canonical]

    sources_section = report.section("sources")
    for row in _mapping_rows(sources_section.rows if sources_section else []):
        raw = _prose(row.get("source"))
        if not raw:
            continue
        canonical = _canonical_source(raw)
        data = entry(raw, canonical)
        kind = _prose(row.get("kind"))
        if kind:
            data.kind = kind
        count = _count_value(row.get("rows"))
        if count is not None:
            data.rows = count
        status = _prose(row.get("status")).casefold()
        if status == "missing" or count == 0:
            data.missing = True
        limitations = row.get("limitations")
        if isinstance(limitations, (list, tuple)):
            for item in limitations:
                text = _prose(item)
                if text and text not in data.limitations:
                    data.limitations.append(text)

    traffic_section = report.section("traffic")
    for row in _mapping_rows(traffic_section.rows if traffic_section else []):
        raw = _prose(row.get("engine"))
        if not raw:
            continue
        canonical = _canonical_source(raw)
        data = entry(raw, canonical)
        data.kind = "traffic"
        count = _count_value(row.get("rows"))
        if count is not None:
            data.rows = count
            data.missing = data.missing or count == 0
        if not data.metrics:
            data.metrics = _source_metrics(row, canonical, msgs)
        limitations = row.get("limitations")
        if isinstance(limitations, (list, tuple)):
            for item in limitations:
                text = _prose(item)
                if text and text not in data.limitations:
                    data.limitations.append(text)

    for key, canonical, kind in (
        ("yandex_webmaster", "yandex_webmaster", "traffic"),
        ("yandex_metrika", "yandex_metrika", "traffic"),
    ):
        section = report.section(key)
        if section is None or not section.rows:
            continue
        data = entry(key, canonical)
        data.kind = kind
        data.rows = len(section.rows)
        data.missing = False
        if not data.metrics:
            data.metrics = _fallback_metric_metrics(
                section.rows, msgs, sessions_only=canonical == "yandex_metrika"
            )

    # A fallback weekly report can carry only a ``gsc`` comparison section and
    # no per-engine source rows. Name the engine whenever GSC rows exist, even
    # when other providers were collected, but never invent a row count.
    gsc_section = report.section("gsc")
    if gsc_section is not None and gsc_section.rows:
        entry("gsc", "gsc")

    extras = sorted(
        (data for key, data in collected.items() if key not in _SOURCE_ORDER),
        key=lambda data: (data.raw_name.casefold(), data.canonical),
    )
    ordered = [collected[key] for key in _SOURCE_ORDER if key in collected] + extras

    sources: list[_Source] = []
    for data in ordered:
        kind = data.kind or "traffic"
        kind_label = msgs.t(f"source.kind.{kind}") if kind in {"traffic", "field", "lab"} else kind
        if data.rows is not None and data.rows > 0:
            rows_text = f"{msgs.t('source.rows')}: {msgs.number(float(data.rows))}"
        elif data.missing or data.rows == 0:
            rows_text = msgs.t("source.missing")
        else:
            rows_text = ""
        # GSC and Yandex Webmaster report per-query rows; their numbers are
        # sums over the fetched top rows, not full-window totals.
        scope = (
            msgs.t("source.top_rows")
            if data.metrics and data.canonical in {"gsc", "yandex_webmaster"}
            else ""
        )
        sources.append(
            _Source(
                name=_engine_name(data.canonical, data.raw_name, msgs),
                kind=kind_label,
                metrics=tuple(data.metrics),
                rows_text=rows_text,
                missing=data.missing or data.rows == 0,
                scope=scope,
            )
        )
    return sources


def _source_limitations(report: Report) -> list[str]:
    limitations: list[str] = []
    for key in ("sources", "traffic"):
        section = report.section(key)
        if section is None:
            continue
        for row in _mapping_rows(section.rows):
            items = row.get("limitations")
            if not isinstance(items, (list, tuple)):
                continue
            for item in items:
                text = _prose(item)
                if text and text not in limitations:
                    limitations.append(text)
    return limitations


def _append_unique(target: list[str], seen: set[str], text: object) -> None:
    cleaned = _prose(text)
    if not cleaned:
        return
    key = cleaned.casefold()
    if key in seen:
        return
    seen.add(key)
    target.append(cleaned)


def _normalize_warning(text: str) -> str:
    return " ".join(text.split()).casefold().rstrip(".")


def _limitation_text(text: str, msgs: _Messages) -> tuple[str, bool]:
    """Return ``(display text, is_raw_prose)`` for one limitation string."""
    key = _LIMITATION_KEYS.get(text.casefold())
    if key is not None:
        return msgs.t(key), False
    if _SNAKE_CODE_RE.match(text):
        return msgs.t("notes.other_limitation", code=text), False
    return text, True


def _build_notes(report: Report, msgs: _Messages) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    notes: list[str] = []
    raw_note = False

    candidates: list[str] = []
    for limitation in report.limitations:
        text = _prose(limitation)
        if text:
            candidates.append(text)
    candidates.extend(_source_limitations(report))

    # The same fact can arrive as a stable code and as English builder prose.
    # Once a code is rendered as a localized sentence, its prose twin is dropped.
    covered = {
        _ENGLISH.get(_LIMITATION_KEYS[text.casefold()], "").casefold()
        for text in candidates
        if text.casefold() in _LIMITATION_KEYS
    }
    for text in candidates:
        display, is_prose = _limitation_text(text, msgs)
        if is_prose and text.casefold() in covered:
            continue
        if is_prose:
            raw_note = True
        _append_unique(notes, seen, display)

    warnings: list[str] = []
    raw_warning = False
    for warning in report.warnings:
        text = _prose(warning)
        if not text:
            continue
        key = _WARNING_KEYS.get(_normalize_warning(text))
        if key is not None:
            _append_unique(warnings, seen, msgs.t(key))
            continue
        raw_warning = True
        _append_unique(warnings, seen, text)
    if msgs.locale != "en":
        # Provider prose stays untranslated; say so once instead of repeating a
        # suffix on every line. Localized system warnings never trigger this.
        if raw_note and notes:
            _append_unique(notes, seen, msgs.t("notes.original"))
        elif raw_warning and warnings:
            _append_unique(warnings, seen, msgs.t("notes.original"))
    return notes, warnings


def _humanize(key: object) -> str:
    text = _inline(key).replace("_", " ").replace("-", " ")
    return " ".join(text.split()) or _inline(key)


def _flat_value(value: object) -> str:
    if isinstance(value, Mapping):
        return ""
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            text = _flat_value(item)
            if text:
                parts.append(text)
        return ", ".join(parts)
    return _inline(value)


def _generic_section(section: ReportSection, msgs: _Messages) -> _GenericSection:
    rows: list[tuple[tuple[str, str], ...]] = []
    for row in _mapping_rows(section.rows):
        pairs: list[tuple[str, str]] = []
        for key, value in row.items():
            if str(key) in _HIDDEN_ROW_KEYS:
                continue
            text = _flat_value(value)
            if not text:
                continue
            pairs.append((_humanize(key), text))
        if pairs:
            rows.append(tuple(pairs))
    notes = tuple(text for text in (_prose(note) for note in section.notes) if text)
    title = _prose(section.title) or _humanize(section.key) or msgs.t("generic.other_metrics")
    return _GenericSection(
        title=title,
        summary=_prose(section.summary),
        rows=tuple(rows),
        notes=notes,
    )


def _weekly_extras(report: Report, msgs: _Messages) -> list[_GenericSection]:
    extras: list[_GenericSection] = []
    other_rows: list[tuple[tuple[str, str], ...]] = []
    for key in ("headline", "gsc"):
        section = report.section(key)
        if section is None:
            continue
        for row in _mapping_rows(section.rows):
            metric = _prose(row.get("metric"))
            if metric:
                if _metric_key(row) is not None:
                    continue  # a known KPI row is already rendered as a card
                # Unknown metric: a humanized label plus safe free text only.
                value = " · ".join(
                    text
                    for item_key, item_value in row.items()
                    if str(item_key) not in _HIDDEN_ROW_KEYS
                    and str(item_key) not in _METRIC_SCHEMA_KEYS
                    and (text := _flat_value(item_value))
                )
                if value:
                    other_rows.append(((_humanize(metric), value),))
                continue
            pairs: list[tuple[str, str]] = []
            for item_key, value in row.items():
                if str(item_key) in _HIDDEN_ROW_KEYS:
                    continue
                text = _flat_value(value)
                if not text:
                    continue
                pairs.append((_humanize(item_key), text))
            if pairs:
                other_rows.append(tuple(pairs))
    if other_rows:
        extras.append(
            _GenericSection(
                title=msgs.t("generic.other_metrics"),
                summary="",
                rows=tuple(other_rows),
                notes=(),
            )
        )
    for section in report.sections:
        if section.key in _WEEKLY_KEYS:
            continue
        extras.append(_generic_section(section, msgs))
    return extras


def _build_view(report: Report, msgs: _Messages) -> _View:
    is_weekly = report.kind == "weekly"
    report_type = _report_type(msgs, report.kind)
    property_id = _inline(report.property_id)
    property_label = _display_property(property_id) or property_id
    period = msgs.period(report.period_start, report.period_end)
    period_short = msgs.short_period(report.period_start, report.period_end)
    generated = msgs.datetime(report.generated_at) if _inline(report.generated_at) else ""

    if is_weekly:
        kpis = _build_kpis(report, msgs)
        opportunities, opportunity_note, opportunity_footnote, opportunity_scope = (
            _build_opportunities(report, msgs)
        )
        issues = _build_issues(report, msgs)
        sources = _collect_sources(report, msgs)
        notes, warnings = _build_notes(report, msgs)
        if any(source.scope for source in sources):
            _append_unique(
                notes,
                {note.casefold() for note in notes},
                msgs.t("notes.source_totals"),
            )
        extras = _weekly_extras(report, msgs)
        intro = ""
        source_rows = any(source.rows_text and not source.missing for source in sources)
        has_data = (
            any(kpi.value is not None for kpi in kpis)
            or bool(opportunities)
            or issues.total > 0
            or source_rows
            or _has_metric_values(report)
        )
        has_comparison = any(kpi.value is not None and kpi.previous is not None for kpi in kpis)
        critical_count = sum(group.count for group in issues.groups if group.severity == "critical")
        verdict = _verdict(kpis, has_data=has_data, has_comparison=has_comparison, msgs=msgs)
    else:
        kpis = []
        opportunities = []
        opportunity_note = ""
        opportunity_footnote = ""
        opportunity_scope = ""
        issues = _Issues()
        sources = []
        notes, warnings = _build_notes(report, msgs)
        extras = [_generic_section(section, msgs) for section in report.sections]
        intro = _inline(report.headline)
        has_data = bool(extras) or bool(notes) or bool(warnings)
        has_comparison = False
        critical_count = 0
        # Only the weekly presenter makes a comparison claim; an audit or
        # indexing report must not say "No previous period to compare".
        verdict = ""

    return _View(
        kind=report.kind,
        report_type=report_type,
        property_id=property_id,
        property_label=property_label,
        period=period,
        period_short=period_short,
        generated=generated,
        verdict=verdict,
        kpis=kpis,
        opportunities=opportunities,
        opportunity_note=opportunity_note,
        opportunity_footnote=opportunity_footnote,
        opportunity_scope=opportunity_scope,
        issues=issues,
        sources=sources,
        sources_empty=not sources,
        notes=notes,
        warnings=warnings,
        extras=extras,
        intro=intro,
        critical_count=critical_count,
        has_data=has_data,
        has_comparison=has_comparison,
        biggest=_biggest_kpi(kpis),
    )


# -- plain text ------------------------------------------------------------------------------


def _char_width(char: str) -> int:
    """Return the monospace column width of one character."""
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    """Return display width: East Asian wide and fullwidth characters count as two."""
    return sum(_char_width(char) for char in text)


def _is_space(char: str) -> bool:
    """Whitespace that may break a line; no-break spaces are ordinary characters."""
    return char.isspace() and char not in {"\u00a0", "\u202f", "\u2007"}


def _break_units(text: str) -> list[str]:
    """Split text into breakable units: spaces, wide characters, or word runs."""
    units: list[str] = []
    word: list[str] = []
    for char in text:
        if _is_space(char):
            if word:
                units.append("".join(word))
                word = []
            if units and units[-1].isspace():
                units[-1] = " "
            else:
                units.append(" ")
        elif _char_width(char) == 2:
            if word:
                units.append("".join(word))
                word = []
            units.append(char)
        else:
            word.append(char)
    if word:
        units.append("".join(word))
    return units


def _split_by_width(unit: str, width: int) -> list[str]:
    """Hard-split a single unit that does not fit on one line."""
    pieces: list[str] = []
    current: list[str] = []
    current_width = 0
    for char in unit:
        char_width = _char_width(char)
        if current and current_width + char_width > width:
            pieces.append("".join(current))
            current = []
            current_width = 0
        current.append(char)
        current_width += char_width
    if current:
        pieces.append("".join(current))
    return pieces


def _wrap_lines(text: str, width: int, *, indent: str = "") -> list[str]:
    """Wrap *text* to *width* display columns, counting CJK as two columns.

    ``textwrap`` measures ``len``, which overflows a 78-column mail body once a
    Chinese label or sentence is present; this greedy wrapper measures the
    East Asian width instead and can break between Han characters.
    """
    if not text or not text.strip():
        return []
    available = max(width - _display_width(indent), 1)
    lines: list[str] = []
    current = ""
    current_width = 0

    def flush() -> None:
        nonlocal current, current_width
        line = current.rstrip()
        if line:
            lines.append(f"{indent}{line}")
        current = ""
        current_width = 0

    for unit in _break_units(text):
        if unit.isspace():
            if current and current_width < available:
                current += " "
                current_width += 1
            continue
        unit_width = _display_width(unit)
        if current and current_width + unit_width > available:
            flush()
        if unit_width > available:
            pieces = _split_by_width(unit, available)
            for piece in pieces[:-1]:
                if current:
                    flush()
                lines.append(f"{indent}{piece}")
            current = pieces[-1]
            current_width = _display_width(current)
            continue
        current += unit
        current_width += unit_width
    flush()
    return lines


def _append_block(blocks: list[str], lines: Sequence[str]) -> None:
    body = "\n".join(lines).strip("\n")
    if body:
        blocks.append(body)


def _text_header(lines: list[str], view: _View, width: int) -> None:
    lines.extend(_wrap_lines(f"LV24 SEO · {view.report_type}", width))
    lines.append("-" * min(width, 72))
    lines.append("")
    if view.intro:
        lines.extend(_wrap_lines(view.intro, width))
        lines.append("")
    if view.property_label:
        lines.extend(_wrap_lines(view.property_label, width))
    if view.period:
        lines.extend(_wrap_lines(view.period, width))
    if view.verdict:
        lines.extend(_wrap_lines(view.verdict, width))


def _text_kpis(lines: list[str], view: _View, msgs: _Messages, width: int) -> None:
    lines.append("")
    for kpi in view.kpis:
        parts = [f"{kpi.label}: {_kpi_value(kpi, msgs)}"]
        delta = _kpi_delta(kpi, msgs)
        if delta:
            parts.append(delta)
        previous = _kpi_previous(kpi, msgs)
        if previous:
            parts.append(previous)
        if kpi.significant:
            parts.append(msgs.t("kpi.significant"))
        lines.extend(_wrap_lines(" · ".join(parts), width, indent=""))


def _text_opportunities(lines: list[str], view: _View, msgs: _Messages, width: int) -> None:
    lines.append("")
    lines.extend(_wrap_lines(msgs.t("section.opportunities"), width))
    if not view.opportunities:
        lines.extend(_wrap_lines(view.opportunity_note, width))
        return
    if view.opportunity_scope:
        lines.extend(_wrap_lines(view.opportunity_scope, width))
    for index, item in enumerate(view.opportunities, start=1):
        lines.extend(_wrap_lines(f"{index}. {item.kind_label}", width))
        lines.extend(_wrap_lines(item.query, width, indent="   "))
        page_line = item.page if item.page_missing else f"{msgs.t('opportunity.page')}: {item.page}"
        lines.extend(_wrap_lines(page_line, width, indent="   "))
        if item.evidence:
            lines.extend(_wrap_lines(item.evidence, width, indent="   "))
        if item.estimate:
            lines.extend(_wrap_lines(item.estimate, width, indent="   "))
    if view.opportunity_footnote:
        lines.append("")
        lines.extend(_wrap_lines(view.opportunity_footnote, width))


def _text_issues(lines: list[str], view: _View, msgs: _Messages, width: int) -> None:
    lines.append("")
    lines.extend(_wrap_lines(msgs.t("section.issues"), width))
    issues = view.issues
    if issues.message:
        lines.extend(_wrap_lines(issues.message, width))
        return
    total = msgs.count("issue.count", issues.total)
    rules = msgs.count("issue.checks", issues.unique_rules)
    lines.extend(_wrap_lines(f"{total} · {rules}", width))
    for group in issues.groups:
        lines.extend(_wrap_lines(f"{group.label}: {msgs.number(float(group.count))}", width))


def _text_sources(lines: list[str], view: _View, msgs: _Messages, width: int) -> None:
    lines.append("")
    lines.extend(_wrap_lines(msgs.t("section.sources"), width))
    if view.sources_empty:
        lines.extend(_wrap_lines(msgs.t("source.missing"), width))
        return
    for source in view.sources:
        tail = " · ".join(
            part
            for part in ((" · ".join(source.metrics)) if source.metrics else "", source.rows_text)
            if part
        )
        label = f"{source.kind} · {source.scope}" if source.scope else source.kind
        line = f"{source.name} ({label})"
        if tail:
            line = f"{line}: {tail}"
        lines.extend(_wrap_lines(line, width))


def _text_notes(lines: list[str], view: _View, msgs: _Messages, width: int) -> None:
    if view.notes:
        lines.append("")
        lines.extend(_wrap_lines(msgs.t("section.notes"), width))
        for note in view.notes:
            lines.extend(_wrap_lines(f"- {note}", width, indent="  "))
    if view.warnings:
        lines.append("")
        lines.extend(_wrap_lines(msgs.t("section.warnings"), width))
        for warning in view.warnings:
            lines.extend(_wrap_lines(f"- {warning}", width, indent="  "))


def _text_extras(lines: list[str], view: _View, msgs: _Messages, width: int) -> None:
    for section in view.extras:
        lines.append("")
        lines.extend(_wrap_lines(section.title, width))
        if section.summary:
            lines.extend(_wrap_lines(section.summary, width))
        for row in section.rows:
            for label, value in row:
                lines.extend(_wrap_lines(f"{label}: {value}", width, indent="  "))
        for note in section.notes:
            lines.extend(_wrap_lines(f"- {note}", width, indent="  "))


def _text_footer(lines: list[str], view: _View, msgs: _Messages, width: int) -> None:
    footer: list[str] = []
    if view.generated:
        footer.extend(_wrap_lines(f"{msgs.t('label.generated')}: {view.generated}", width))
    if view.property_id:
        footer.extend(
            _wrap_lines(msgs.t("footer.property_id", property_id=view.property_id), width)
        )
    footer.extend(_wrap_lines(msgs.t("footer.generated_by"), width))
    if footer:
        lines.append("")
        lines.extend(footer)


def _render_text(report: Report, *, locale: str, width: int) -> str:
    msgs = _Messages(locale)
    view = _build_view(report, msgs)
    lines: list[str] = []
    _text_header(lines, view, width)
    if report.kind == "weekly":
        _text_kpis(lines, view, msgs, width)
        if view.critical_count:
            _text_issues(lines, view, msgs, width)
            _text_opportunities(lines, view, msgs, width)
        else:
            _text_opportunities(lines, view, msgs, width)
            _text_issues(lines, view, msgs, width)
        _text_sources(lines, view, msgs, width)
        _text_notes(lines, view, msgs, width)
        _text_extras(lines, view, msgs, width)
    else:
        if not view.extras and not view.notes and not view.warnings:
            lines.append("")
            lines.extend(_wrap_lines(msgs.t("generic.no_sections"), width))
        _text_extras(lines, view, msgs, width)
        _text_notes(lines, view, msgs, width)
    _text_footer(lines, view, msgs, width)
    return "\n".join(lines).strip("\n")


def render_text(report: Report, *, locale: str = "en") -> str:
    """Render *report* as plain text no wider than 100 columns."""
    return _render_text(report, locale=locale, width=_TEXT_WIDTH)


def render_email_text(report: Report, *, locale: str = "en") -> str:
    """Render *report* as plain text suitable for email and Telegram (78 columns)."""
    return _render_text(report, locale=locale, width=_EMAIL_WIDTH)


# -- subject ---------------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _subject_delta(kpi: _Kpi, msgs: _Messages) -> str:
    if kpi.key == "position":
        delta = kpi.abs_delta
        if delta is None and kpi.value is not None and kpi.previous is not None:
            delta = kpi.value - kpi.previous
        return _signed_position(delta or 0.0, msgs).replace("\u2212", "-")
    if kpi.key == "ctr":
        # Percentage points, never the relative ``pct_delta``.
        delta_pp = 0.0
        if kpi.value is not None and kpi.previous is not None:
            delta_pp = (kpi.value - kpi.previous) * 100.0
        text = msgs.pp(delta_pp, signed=True, digits=2)
        return text.replace("\u2212", "-")
    pct = kpi.pct_delta or 0.0
    text = msgs.percent(pct, signed=True, digits=1)
    return text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")


def _subject_parts(view: _View, msgs: _Messages, property_label: str) -> str:
    period = view.period_short
    if not view.has_data:
        core = msgs.t("subject.no_data", period=period)
    elif view.critical_count:
        # Critical issues outrank the missing-comparison branch: a current-only
        # report can still carry critical findings.
        core = msgs.t("subject.critical", count=msgs.number(float(view.critical_count)))
        if period:
            core = f"{core} · {period}"
    elif not view.has_comparison and view.kpis:
        core = msgs.t("subject.no_comparison", period=period)
    elif view.biggest is not None:
        core = msgs.t(f"subject.{view.biggest.key}", change=_subject_delta(view.biggest, msgs))
        if period:
            core = f"{core} · {period}"
    elif view.kind == "weekly":
        core = msgs.t("subject.weekly", period=period)
    elif period:
        core = f"{view.report_type}: {period}"
    else:
        core = view.report_type
    return f"{property_label} · {core}" if property_label else core


def _collapse_subject(text: str) -> str:
    # Only ASCII whitespace is collapsed: Russian subjects keep the no-break
    # space that ``Translator.percent`` puts before ``%``.
    parts = [_ASCII_SPACE_RE.sub(" ", part).strip() for part in _clean(text).split("·")]
    return " · ".join(part for part in parts if part)


def render_subject(report: Report, *, locale: str = "en") -> str:
    """Render a one-line localized mail subject of at most 78 characters."""
    msgs = _Messages(locale)
    view = _build_view(report, msgs)
    subject = _collapse_subject(
        _subject_parts(view, msgs, _truncate(view.property_label, _PROPERTY_LIMIT))
    )
    if len(subject) <= _SUBJECT_LIMIT:
        return subject
    # Shorten the property first, then fall back to a hard bound.
    for limit in range(_PROPERTY_LIMIT - 1, 7, -1):
        shortened = _collapse_subject(
            _subject_parts(view, msgs, _truncate(view.property_label, limit))
        )
        if len(shortened) <= _SUBJECT_LIMIT:
            return shortened
    return _truncate(subject, _SUBJECT_LIMIT)


# -- generic markdown (unchanged contract) ---------------------------------------------------


def _period(report: Report) -> str:
    start = _stringify(report.period_start)
    end = _stringify(report.period_end)
    if start and end:
        return f"{start} - {end}"
    return start or end


def _markdown_text(value: Any) -> str:
    """Escape angle brackets so report data cannot become raw HTML."""
    return _stringify(value).replace(">", "&gt;").replace("<", "&lt;")


def _markdown_cell(value: Any) -> str:
    text = _stringify(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return _markdown_text(text).replace("|", "\\|")


def _columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the union of row keys in first-seen order."""
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _markdown_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    columns = _columns(rows)
    if not columns:
        return []
    header = "| " + " | ".join(_markdown_cell(column) for column in columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, separator]
    for row in rows:
        cells = " | ".join(_markdown_cell(row.get(column, "")) for column in columns)
        lines.append(f"| {cells} |")
    return lines


def _markdown_section(section: ReportSection) -> str:
    lines = [f"## {_markdown_text(section.title)}"]
    summary = _stringify(section.summary)
    if summary.strip():
        lines.extend(["", _markdown_text(summary)])
    table = _markdown_table(section.rows)
    if table:
        lines.extend(["", *table])
    if section.notes:
        lines.extend(["", *(f"- {_markdown_text(note)}" for note in section.notes)])
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """Render *report* as a Markdown document."""
    blocks: list[str] = []
    header: list[str] = []
    headline = _markdown_text(report.headline)
    if headline:
        header.append(f"# {headline}")

    period = _period(report)
    if period:
        header.append(f"_{_markdown_text(period)}_")
    generated_at = _stringify(report.generated_at)
    if generated_at:
        header.append(f"_Generated: {_markdown_text(generated_at)}_")
    if header:
        blocks.append("\n".join(header))

    for section in report.sections:
        blocks.append(_markdown_section(section))

    if report.warnings:
        blocks.append(
            "\n".join(["## Warnings", *(f"- {_markdown_text(item)}" for item in report.warnings)])
        )

    if report.limitations:
        lines = [f"## {_LIMITATIONS_TITLE}"]
        lines.extend(f"- {_markdown_text(item)}" for item in report.limitations)
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


# -- HTML ------------------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(_clean(value), quote=True)


def _html_paragraph(text: str, style: str) -> str:
    return f'<p style="{style}">{_e(text)}</p>'


def _html_list(items: Sequence[str], *, style: str = _UL_STYLE) -> str:
    if not items:
        return ""
    body = "".join(f"<li>{_e(item)}</li>" for item in items)
    return f'<ul style="{style}">{body}</ul>'


def _delta_color(kpi: _Kpi) -> str:
    if kpi.significant and kpi.good is True:
        return _GREEN
    if kpi.significant and kpi.good is False:
        return _RED
    if kpi.key == "position" and kpi.abs_delta is not None and abs(kpi.abs_delta) >= 0.05:
        return _GREEN if kpi.abs_delta < 0 else _RED
    return _SOFT


def _html_kpi_card(kpi: _Kpi, msgs: _Messages) -> str:
    value = _kpi_value(kpi, msgs)
    delta = _kpi_delta(kpi, msgs)
    previous = _kpi_previous(kpi, msgs)
    delta_html = (
        f'<div style="{_KPI_DELTA_STYLE}color:{_delta_color(kpi)};">{_e(delta)}</div>'
        if delta
        else ""
    )
    previous_html = f'<div style="{_KPI_PREV_STYLE}">{_e(previous)}</div>' if previous else ""
    badge_html = (
        f'<div><span style="{_BADGE_STYLE}">{_e(msgs.t("kpi.significant"))}</span></div>'
        if kpi.significant
        else ""
    )
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="{_CARD_TABLE_STYLE}">'
        "<tr>"
        f'<td style="{_CARD_CELL_STYLE}">'
        f'<div style="{_KPI_LABEL_STYLE}">{_e(kpi.label)}</div>'
        f'<div style="{_KPI_VALUE_STYLE}">{_e(value)}</div>'
        f"{delta_html}{previous_html}{badge_html}"
        "</td>"
        "</tr>"
        "</table>"
    )


def _html_kpis(kpis: Sequence[_Kpi], msgs: _Messages) -> str:
    if not kpis:
        return ""
    rows: list[str] = []
    for start in range(0, len(kpis), 2):
        pair = kpis[start : start + 2]
        cells: list[str] = []
        for index, kpi in enumerate(pair):
            padding = "padding:0 6px 0 0;" if index == 0 else "padding:0 0 0 6px;"
            cells.append(f'<td width="50%" valign="top" style="{padding}">')
            cells.append(_html_kpi_card(kpi, msgs))
            cells.append("</td>")
        if len(pair) == 1:
            cells.append('<td width="50%" valign="top" style="padding:0 0 0 6px;"></td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;border-collapse:collapse;">{"".join(rows)}</table>'
    )


def _html_opportunities(view: _View, msgs: _Messages) -> str:
    title = f'<h2 style="{_H2_STYLE}">{_e(msgs.t("section.opportunities"))}</h2>'
    if not view.opportunities:
        return title + _html_paragraph(view.opportunity_note, _P_STYLE)
    parts = [title]
    if view.opportunity_scope:
        parts.append(_html_paragraph(view.opportunity_scope, _SMALL_STYLE))
    for item in view.opportunities:
        parts.append(
            f'<div style="padding:10px 0;border-bottom:1px solid {_BORDER};">'
            f'<div style="font-size:11px;line-height:1.4;font-weight:700;color:{_MUTED};">'
            f"{_e(item.kind_label)}</div>"
            f'<div style="margin-top:2px;font-size:15px;line-height:1.4;font-weight:600;'
            f'color:{_TEXT};word-break:break-word;">{_e(item.query)}</div>'
            f'<div style="margin-top:2px;font-size:12px;line-height:1.5;color:{_MUTED};'
            f'word-break:break-word;">{_e(item.page)}</div>'
        )
        if item.evidence:
            parts.append(
                f'<div style="margin-top:4px;font-size:12px;line-height:1.5;color:{_SOFT};">'
                f"{_e(item.evidence)}</div>"
            )
        if item.estimate:
            parts.append(
                f'<div style="margin-top:2px;font-size:12px;line-height:1.5;font-weight:600;'
                f'color:{_TEXT};">{_e(item.estimate)}</div>'
            )
        parts.append("</div>")
    if view.opportunity_footnote:
        parts.append(_html_paragraph(view.opportunity_footnote, _SMALL_STYLE))
    return "".join(parts)


def _html_issues(view: _View, msgs: _Messages) -> str:
    parts = [f'<h2 style="{_H2_STYLE}">{_e(msgs.t("section.issues"))}</h2>']
    issues = view.issues
    if issues.message:
        parts.append(_html_paragraph(issues.message, _P_STYLE))
        return "".join(parts)
    parts.append(
        f'<p style="{_P_STYLE}font-weight:600;">'
        f"{_e(msgs.count('issue.count', issues.total))} · "
        f"{_e(msgs.count('issue.checks', issues.unique_rules))}</p>"
    )
    rows = "".join(
        "<tr>"
        f'<td style="{_TD_STYLE}">{_e(group.label)}</td>'
        f'<td style="{_TD_STYLE}text-align:right;">{_e(msgs.number(float(group.count)))}</td>'
        "</tr>"
        for group in issues.groups
    )
    parts.append(
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="{_DATA_TABLE_STYLE}"><tbody>{rows}</tbody></table>'
    )
    return "".join(parts)


def _html_sources(view: _View, msgs: _Messages) -> str:
    parts = [f'<h2 style="{_H2_STYLE}">{_e(msgs.t("section.sources"))}</h2>']
    if view.sources_empty:
        parts.append(_html_paragraph(msgs.t("source.missing"), _P_STYLE))
        return "".join(parts)
    rows: list[str] = []
    for source in view.sources:
        data = " · ".join(source.metrics) if source.metrics else ""
        kind = f"{source.kind} · {source.scope}" if source.scope else source.kind
        rows.append(
            "<tr>"
            f'<td style="{_TD_STYLE}word-break:break-word;">'
            f'<span style="font-weight:600;color:{_TEXT};">{_e(source.name)}</span>'
            f' <span style="font-size:11px;color:{_MUTED};">({_e(kind)})</span>'
            "</td>"
            f'<td style="{_TD_STYLE}">{_e(data)}</td>'
            f'<td style="{_TD_STYLE}text-align:right;white-space:nowrap;">'
            f"{_e(source.rows_text)}</td>"
            "</tr>"
        )
    parts.append(
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="{_DATA_TABLE_STYLE}"><tbody>{"".join(rows)}</tbody></table>'
    )
    return "".join(parts)


def _html_extras(view: _View, msgs: _Messages) -> str:
    parts: list[str] = []
    for section in view.extras:
        parts.append(f'<h2 style="{_H2_STYLE}">{_e(section.title)}</h2>')
        if section.summary:
            parts.append(_html_paragraph(section.summary, _P_STYLE))
        if section.rows:
            header = (
                "<tr>"
                f'<th style="{_TH_STYLE}">{_e(msgs.t("generic.field"))}</th>'
                f'<th style="{_TH_STYLE}">{_e(msgs.t("generic.value"))}</th>'
                "</tr>"
            )
            body = "".join(
                "<tr>"
                f'<td style="{_TD_STYLE}">{_e(label)}</td>'
                f'<td style="{_TD_STYLE}word-break:break-word;">{_e(value)}</td>'
                "</tr>"
                for row in section.rows
                for label, value in row
            )
            parts.append(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'border="0" style="{_DATA_TABLE_STYLE}"><tbody>{header}{body}</tbody></table>'
            )
        parts.append(_html_list(section.notes))
    return "".join(parts)


def _html_notes(view: _View, msgs: _Messages) -> str:
    parts: list[str] = []
    if view.notes:
        parts.append(f'<h2 style="{_H2_STYLE}">{_e(msgs.t("section.notes"))}</h2>')
        parts.append(_html_list(view.notes))
    if view.warnings:
        parts.append(f'<h2 style="{_H2_STYLE}">{_e(msgs.t("section.warnings"))}</h2>')
        parts.append(_html_list(view.warnings))
    return "".join(parts)


def _html_footer(view: _View, msgs: _Messages) -> str:
    items: list[str] = []
    if view.generated:
        items.append(
            f'<p style="{_FOOTER_ITEM_STYLE}color:{_ON_DARK_MUTED};">'
            f"{_e(msgs.t('label.generated'))}: {_e(view.generated)}</p>"
        )
    if view.property_id:
        items.append(
            f'<p style="{_FOOTER_ITEM_STYLE}color:{_ON_DARK};">'
            f"{_e(msgs.t('footer.property_id', property_id=view.property_id))}</p>"
        )
    items.append(
        f'<p style="{_FOOTER_ITEM_STYLE}color:{_NEON};">{_e(msgs.t("footer.generated_by"))}</p>'
    )
    return (
        f'<tr><td style="background-color:{_DARK};padding:18px 20px;'
        f'border-top:2px solid {_NEON};">{"".join(items)}</td></tr>'
    )


def _html_header(view: _View) -> str:
    return (
        f'<tr><td style="background-color:{_DARK};padding:20px 20px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        "<tr>"
        '<td align="left" style="font-size:18px;line-height:1.3;font-weight:800;'
        f'letter-spacing:0.08em;color:{_ON_DARK};">'
        f'<span style="color:{_NEON};">LV24</span> SEO</td>'
        f'<td align="right" style="font-size:12px;line-height:1.3;color:{_ON_DARK_MUTED};">'
        f"{_e(view.report_type)}</td>"
        "</tr>"
        "</table>"
        "</td></tr>"
    )


def render_html(report: Report, *, title: str | None = None, locale: str = "en") -> str:
    """Render *report* as a standalone, email-safe HTML document."""
    msgs = _Messages(locale)
    view = _build_view(report, msgs)
    document_title = _inline(title) or (
        f"{view.report_type} · {view.property_label}" if view.property_label else view.report_type
    )
    preheader = " · ".join(
        part for part in (view.property_label, view.period, view.verdict) if part
    )

    lines: list[str] = [
        "<!DOCTYPE html>",
        f'<html lang="{_e(msgs.html_lang)}" dir="ltr">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light only">',
        '<meta name="supported-color-schemes" content="light">',
        f"<title>{_e(document_title)}</title>",
        "</head>",
        f'<body style="{_BODY_STYLE}">',
        f'<div style="{_PREHEADER_STYLE}">{_e(preheader)}</div>',
        (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="width:100%;border-collapse:collapse;background-color:{_PAGE_BG};">'
        ),
        '<tr><td align="center" style="padding:24px 12px;">',
        (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="width:100%;max-width:600px;border-collapse:separate;'
            f'background-color:{_CARD_BG};border-radius:14px;overflow:hidden;">'
        ),
        _html_header(view),
    ]

    hero: list[str] = [
        '<tr><td style="padding:26px 20px 0 20px;">',
        f'<h1 style="{_H1_STYLE}">{_e(view.property_label)}</h1>',
    ]
    if view.period:
        hero.append(f'<p style="{_HERO_META_STYLE}">{_e(view.period)}</p>')
    if view.verdict:
        hero.append(f'<p style="{_VERDICT_STYLE}">{_e(view.verdict)}</p>')
    hero.append("</td></tr>")
    lines.extend(hero)

    if view.intro:
        lines.append('<tr><td style="padding:12px 20px 0 20px;">')
        lines.append(_html_paragraph(view.intro, _P_STYLE))
        lines.append("</td></tr>")

    if view.kpis:
        lines.append('<tr><td style="padding:18px 20px 0 20px;">')
        lines.append(_html_kpis(view.kpis, msgs))
        lines.append("</td></tr>")

    body_parts: list[str] = []
    if report.kind == "weekly":
        if view.critical_count:
            body_parts.extend(
                [
                    _html_issues(view, msgs),
                    _html_opportunities(view, msgs),
                ]
            )
        else:
            body_parts.extend(
                [
                    _html_opportunities(view, msgs),
                    _html_issues(view, msgs),
                ]
            )
        body_parts.append(_html_sources(view, msgs))
        body_parts.append(_html_notes(view, msgs))
        body_parts.append(_html_extras(view, msgs))
    else:
        if not view.extras and not view.notes and not view.warnings:
            body_parts.append(_html_paragraph(msgs.t("generic.no_sections"), _P_STYLE))
        body_parts.append(_html_extras(view, msgs))
        body_parts.append(_html_notes(view, msgs))

    if body_parts:
        lines.append('<tr><td style="padding:0 20px;">')
        lines.extend(body_parts)
        lines.append("</td></tr>")

    lines.append(_html_footer(view, msgs))
    lines.extend(
        [
            "</table>",
            "</td></tr>",
            "</table>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(lines)
