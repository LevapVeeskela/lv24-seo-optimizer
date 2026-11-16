"""Core Web Vitals by URL template: stored CrUX/PSI p75 rows, grouped honestly.

The weekly report already shows CrUX/PSI rows per engine, but an origin-level
CrUX record cannot say *which page pattern* regressed. This module groups
already-stored field metric rows by URL template and device, so a reader gets
"the blog article template on phone got slower" instead of one origin number.

Honesty rules:

* only stored rows are used — no provider call, no clock, no invented value;
* a row without a URL template (including an origin-scope CrUX record) or
  without a device is skipped and reported as a limitation, never assigned to a
  guessed template/device;
* a template p75 is the *worst* page p75 in that template, because percentiles
  cannot be averaged across URLs; the limitation says so;
* a regression needs a baseline. Without one the current p75 is shown and the
  comparison fields stay ``None``; nothing is treated as a zero baseline;
* lab (Lighthouse) rows are ignored: field and lab data describe different
  populations and are never merged;
* output is capped and ``total``/``truncated`` carry the full sizes;
  ``data_age`` is the oldest observation among the templates actually shown.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import SplitResult, urlsplit

__all__ = [
    "CORE_METRICS",
    "CwvMetric",
    "CwvReport",
    "CwvTemplate",
    "build_cwv",
    "normalize_template",
]

CORE_METRICS: tuple[str, ...] = ("lcp", "inp", "cls")
METRIC_ORDER: tuple[str, ...] = ("lcp", "inp", "cls", "fcp", "ttfb", "fid")
STATUSES: tuple[str, ...] = ("good", "needs_improvement", "poor", "unknown")
DEVICES: tuple[str, ...] = ("phone", "desktop", "tablet")

_DEFAULT_LIMIT = 5
_REGRESSION_MIN_PCT = 5.0
_REGRESSION_MIN_ABS: dict[str, float] = {"cls": 0.01}

# Canonical metric -> (unit, good threshold, poor threshold). Thresholds are the
# published Core Web Vitals bands; they classify a p75, they do not create one.
_THRESHOLDS: dict[str, tuple[str, float, float]] = {
    "lcp": ("ms", 2500.0, 4000.0),
    "inp": ("ms", 200.0, 500.0),
    "cls": ("score", 0.1, 0.25),
    "fcp": ("ms", 1800.0, 3000.0),
    "ttfb": ("ms", 800.0, 1800.0),
    "fid": ("ms", 100.0, 300.0),
}

_METRIC_ALIASES: dict[str, str] = {
    "lcp": "lcp",
    "largest_contentful_paint": "lcp",
    "inp": "inp",
    "interaction_to_next_paint": "inp",
    "cls": "cls",
    "cumulative_layout_shift": "cls",
    "cumulative_layout_shift_score": "cls",
    "fcp": "fcp",
    "first_contentful_paint": "fcp",
    "ttfb": "ttfb",
    "experimental_time_to_first_byte": "ttfb",
    "fid": "fid",
    "first_input_delay": "fid",
}

_DEVICE_ALIASES: dict[str, str] = {
    "phone": "phone",
    "mobile": "phone",
    "smartphone": "phone",
    "desktop": "desktop",
    "tablet": "tablet",
    "tablets": "tablet",
}

_LAB_KINDS = frozenset(
    {"lab", "lighthouse", "performance", "accessibility", "best-practices", "seo"}
)
_STATUS_RANK: dict[str, int] = {"poor": 2, "needs_improvement": 1, "good": 0, "unknown": -1}

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")
_NUMERIC_SUFFIX_RE = re.compile(r"(?<=[-_])\d+")


@dataclass(frozen=True, slots=True)
class CwvMetric:
    """One canonical metric for one template+device group."""

    metric: str
    p75: float
    unit: str
    status: str
    previous_p75: float | None
    delta: float | None
    delta_pct: float | None
    regressed: bool
    sample: int
    compared: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "p75": self.p75,
            "unit": self.unit,
            "status": self.status,
            "previous_p75": self.previous_p75,
            "delta": self.delta,
            "delta_pct": self.delta_pct,
            "regressed": self.regressed,
            "sample": self.sample,
            "compared": self.compared,
        }


@dataclass(frozen=True, slots=True)
class CwvTemplate:
    """One URL template on one device, with its bounded metric list."""

    template: str
    device: str
    urls: int
    sample: int
    compared: int
    coverage: float
    metrics: tuple[CwvMetric, ...]
    regressed: bool
    regression_pct: float | None
    regression_metric: str | None
    poor_metrics: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "device": self.device,
            "urls": self.urls,
            "sample": self.sample,
            "compared": self.compared,
            "coverage": self.coverage,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "regressed": self.regressed,
            "regression_pct": self.regression_pct,
            "regression_metric": self.regression_metric,
            "poor_metrics": self.poor_metrics,
        }


@dataclass(slots=True)
class CwvReport:
    """Bounded template list plus the caveats that qualify it."""

    templates: list[CwvTemplate] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    data_age: str | None = None
    ignored_rows: int = 0
    limitations: list[str] = field(default_factory=list)

    @property
    def regressed_template(self) -> str | None:
        """The worst regressed template, or ``None`` when nothing regressed."""
        if self.templates and self.templates[0].regressed:
            return self.templates[0].template
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "templates": [template.to_dict() for template in self.templates],
            "total": self.total,
            "truncated": self.truncated,
            "data_age": self.data_age,
            "ignored_rows": self.ignored_rows,
            "regressed_template": self.regressed_template,
            "limitations": list(self.limitations),
        }


@dataclass(slots=True)
class _MetricRows:
    sample: int = 0
    compared: int = 0
    current: float | None = None
    previous: float | None = None

    def add(self, current: float, previous: float | None) -> None:
        self.sample += 1
        self.current = current if self.current is None else max(self.current, current)
        if previous is not None:
            self.compared += 1
            self.previous = previous if self.previous is None else max(self.previous, previous)


@dataclass(slots=True)
class _Group:
    template: str
    device: str
    urls: set[str] = field(default_factory=set)
    sample: int = 0
    metrics: dict[str, _MetricRows] = field(default_factory=dict)
    dates: list[date] = field(default_factory=list)


def build_cwv(
    rows: Sequence[object],
    *,
    limit: int = _DEFAULT_LIMIT,
) -> CwvReport:
    """Group stored CWV metric rows by URL template and device.

    Accepted row keys (first present wins):

    * URL: ``url``, ``key``, ``page``, ``target``;
    * device: ``device``, ``form_factor``, ``formFactor``, ``strategy``;
    * metric: ``metric``, ``name`` (``LCP``/``largest_contentful_paint``/...);
    * p75: ``p75``, ``value``, ``percentile``;
    * previous p75 (optional): ``previous_p75``, ``previous_value``,
      ``p75_previous``, ``previous``;
    * observation date (optional): ``date``, ``end``, ``observed_at``,
      ``fetched_at``.

    ``scope="origin"`` rows are skipped: an origin aggregate has no template.
    ``kind``/``category`` of ``lab``/``lighthouse`` rows are skipped: field and
    lab data are never merged. A metric without a p75 (for example a PSI lab
    score row) is not a CWV row and is counted as ignored.
    """
    report = CwvReport()
    if not rows:
        report.limitations.append("cwv_no_rows")
        return report

    groups: dict[tuple[str, str], _Group] = {}
    missing_template = 0
    missing_device = 0
    lab_rows = 0
    ignored = 0

    for row in rows:
        if not isinstance(row, Mapping):
            ignored += 1
            continue
        metric = _metric(_first_text(row, ("metric", "name")))
        p75 = _first_number(row, ("p75", "value", "percentile"))
        if metric is None or p75 is None:
            ignored += 1
            continue
        if _first_text(row, ("kind", "category")).casefold() in _LAB_KINDS:
            lab_rows += 1
            continue
        if p75 < 0 or (_unit(metric) == "score" and p75 > 1.0):
            ignored += 1
            continue
        if _text(row.get("scope")).casefold() == "origin":
            missing_template += 1
            continue
        url = _first_text(row, ("url", "key", "page", "target"))
        template = normalize_template(url)
        if not template:
            missing_template += 1
            continue
        device = _device(_first_text(row, ("device", "form_factor", "formFactor", "strategy")))
        if device is None:
            missing_device += 1
            continue
        previous = _first_number(
            row, ("previous_p75", "previous_value", "p75_previous", "previous")
        )
        if previous is not None and (previous < 0 or (_unit(metric) == "score" and previous > 1.0)):
            previous = None
        observed = _day(_first_value(row, ("date", "end", "observed_at", "fetched_at")))
        group = groups.get((template, device))
        if group is None:
            group = _Group(template=template, device=device)
            groups[(template, device)] = group
        group.urls.add(url)
        group.sample += 1
        group.metrics.setdefault(metric, _MetricRows()).add(p75, previous)
        if observed is not None:
            group.dates.append(observed)

    report.ignored_rows = ignored
    built: list[tuple[CwvTemplate, list[date]]] = []
    for group in groups.values():
        built_template = _build_template(group)
        if built_template is not None:
            built.append((built_template, group.dates))
    built.sort(key=lambda item: _rank(item[0]))
    report.total = len(built)
    selected = built[: max(0, int(limit))]
    report.templates = [item[0] for item in selected]
    report.truncated = len(selected) < len(built)
    dates = [day for _, group_dates in selected for day in group_dates]
    if dates:
        report.data_age = min(dates).isoformat()

    if missing_template:
        report.limitations.append("cwv_template_missing")
    if missing_device:
        report.limitations.append("cwv_device_missing")
    if lab_rows:
        report.limitations.append("cwv_lab_ignored")
    if ignored:
        report.limitations.append("cwv_values_ignored")
    if report.total:
        report.limitations.append("cwv_p75_worst_page")
    has_comparison = any(
        metric.previous is not None
        for group in groups.values()
        for metric in group.metrics.values()
    )
    if report.total and not has_comparison:
        report.limitations.append("cwv_no_comparison")
    elif any(
        0 < metric.compared < metric.sample
        for group in groups.values()
        for metric in group.metrics.values()
    ):
        report.limitations.append("cwv_partial_comparison")
    return report


def normalize_template(url: str) -> str:
    """Return a stable path template: IDs and numeric suffixes become ``{id}``.

    ``https://example.com/blog/42/`` -> ``/blog/{id}``. Query strings and
    fragments are dropped. An empty or unusable value returns ``""``.
    """
    text = _text(url)
    if not text:
        return ""
    split = _split_url(text)
    path = split.path
    if not path:
        path = "/"
    segments = [_normalize_segment(segment) for segment in path.split("/") if segment]
    return "/" + "/".join(segments)


def _split_url(text: str) -> SplitResult:
    if "://" in text or text.startswith("//"):
        return urlsplit(text)
    if text.startswith("/"):
        return urlsplit(text)
    first = text.split("/", 1)[0]
    if "." in first:
        return urlsplit("//" + text)
    return urlsplit("/" + text)


def _normalize_segment(segment: str) -> str:
    if _UUID_RE.fullmatch(segment) or segment.isdigit():
        return "{id}"
    if _HEX_RE.fullmatch(segment) and any(char.isdigit() for char in segment):
        return "{id}"
    return _NUMERIC_SUFFIX_RE.sub("{id}", segment)


def _build_template(group: _Group) -> CwvTemplate | None:
    metrics: list[CwvMetric] = []
    for metric in METRIC_ORDER:
        aggregate = group.metrics.get(metric)
        if aggregate is None or aggregate.current is None:
            continue
        previous = aggregate.previous
        delta = None if previous is None else aggregate.current - previous
        delta_pct = None
        if delta is not None and previous is not None and previous > 0:
            delta_pct = round(delta / previous * 100.0, 2)
        metrics.append(
            CwvMetric(
                metric=metric,
                p75=aggregate.current,
                unit=_unit(metric),
                status=_status(metric, aggregate.current),
                previous_p75=previous,
                delta=delta,
                delta_pct=delta_pct,
                regressed=_is_regression(metric, delta, delta_pct),
                sample=aggregate.sample,
                compared=aggregate.compared,
            )
        )
    if not metrics:
        return None

    present = {metric.metric for metric in metrics}
    coverage = round(len(present & set(CORE_METRICS)) / len(CORE_METRICS), 4)
    regressed = [metric for metric in metrics if metric.regressed]
    worst = (
        max(
            regressed,
            key=lambda metric: (
                metric.delta_pct is not None,
                metric.delta_pct or 0.0,
                metric.delta or 0.0,
            ),
        )
        if regressed
        else None
    )
    return CwvTemplate(
        template=group.template,
        device=group.device,
        urls=len(group.urls),
        sample=group.sample,
        compared=sum(metric.compared for metric in metrics),
        coverage=coverage,
        metrics=tuple(metrics),
        regressed=worst is not None,
        regression_pct=worst.delta_pct if worst is not None else None,
        regression_metric=worst.metric if worst is not None else None,
        poor_metrics=sum(1 for metric in metrics if metric.status == "poor"),
    )


def _rank(template: CwvTemplate) -> tuple[int, float, int, int, str, str]:
    """Regressed first, then the largest relative regression, then severity."""
    worst_status = max(
        (_STATUS_RANK.get(metric.status, -1) for metric in template.metrics),
        default=-1,
    )
    return (
        0 if template.regressed else 1,
        -(template.regression_pct or 0.0),
        -template.poor_metrics,
        -worst_status,
        template.template,
        template.device,
    )


def _status(metric: str, p75: float) -> str:
    thresholds = _THRESHOLDS.get(metric)
    if thresholds is None:
        return "unknown"
    _, good, poor = thresholds
    if p75 <= good:
        return "good"
    if p75 > poor:
        return "poor"
    return "needs_improvement"


def _is_regression(metric: str, delta: float | None, delta_pct: float | None) -> bool:
    if delta is None or delta <= 0:
        return False
    if delta <= _REGRESSION_MIN_ABS.get(metric, 0.0):
        return False
    if delta_pct is None:
        return True
    return delta_pct >= _REGRESSION_MIN_PCT


def _unit(metric: str) -> str:
    thresholds = _THRESHOLDS.get(metric)
    return thresholds[0] if thresholds is not None else "ms"


def _metric(value: str) -> str | None:
    return _METRIC_ALIASES.get(value.strip().casefold())


def _device(value: str) -> str | None:
    return _DEVICE_ALIASES.get(value.strip().casefold())


def _first_value(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _first_text(row: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        text = _text(row.get(key))
        if text:
            return text
    return ""


def _first_number(row: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        number = _number(row.get(key))
        if number is not None:
            return number
    return None


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
