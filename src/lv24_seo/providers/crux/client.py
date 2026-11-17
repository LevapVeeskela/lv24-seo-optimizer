"""Chrome UX Report client: p75 field metrics and weekly history for one origin or URL.

CrUX is the only free source of real-user Core Web Vitals. Its honesty quirks:

* a low-traffic URL answers ``404`` instead of an empty record; that is "no field
  data", not a failure, so the adapter turns it into an empty
  :class:`ProviderResult` carrying ``LOW_TRAFFIC``;
* the newest data is always behind, but the exact lag is in
  ``collectionPeriod.lastDate``. ``data_age_days`` is computed from that date, and
  the old two-day heuristic is only a fallback when the provider omits the period;
* a URL lookup may be silently normalized; ``urlNormalizationDetails`` becomes a
  warning plus ``crux_url_normalized`` instead of pretending the requested URL was
  queried verbatim;
* ``records:queryHistoryRecord`` returns one 28-day rolling window per week, newest
  last. ``history()`` caps the response to the requested number of weeks (1..40)
  and reports gaps (``null`` p75 entries) instead of inventing values.

URL queries accept an optional ``formFactor`` (``PHONE``/``DESKTOP``/``TABLET``). The
device is a property of the request, not of the metric, so ``origin()`` and ``url()``
without a device keep their historical body and row shape. When a device is requested,
each row additionally carries ``url`` and ``device`` so a writer can persist the row
without re-deriving the request.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, PROPERTY_NOT_FOUND, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import LOW_TRAFFIC, ProviderResult, Row, now_utc

__all__ = [
    "CRUX_HISTORY_GAPS",
    "CRUX_HISTORY_METRICS_FILTERED",
    "CRUX_HISTORY_PERIODS_MISSING",
    "CRUX_HISTORY_SHORT",
    "CRUX_PERIOD_MISSING",
    "CRUX_URL_NORMALIZED",
    "DEFAULT_BASE_URL",
    "HISTORY_MAX_PERIODS",
    "CruxClient",
]

DEFAULT_BASE_URL = "https://chromeuxreport.googleapis.com/v1"

_RECORD_PATH = "records:queryRecord"
_HISTORY_PATH = "records:queryHistoryRecord"
_LOGIN_NEXT_ACTION = "lv24-seo auth login --provider crux"

# CrUX reports on a 28-day rolling window, so the newest data is never fresh. This is
# only used when the provider omits ``collectionPeriod`` (or for a 404 with no body).
_DATA_AGE_DAYS = 2
_NO_FIELD_DATA_WARNING = "no field data"

# Stable limitation codes; consumers match on these strings, not on message text.
CRUX_PERIOD_MISSING = "crux_period_missing"
CRUX_URL_NORMALIZED = "crux_url_normalized"
CRUX_HISTORY_SHORT = "crux_history_short"
CRUX_HISTORY_GAPS = "crux_history_gaps"
CRUX_HISTORY_METRICS_FILTERED = "crux_history_metrics_filtered"
CRUX_HISTORY_PERIODS_MISSING = "crux_history_periods_missing"

HISTORY_MAX_PERIODS = 40
_HISTORY_DEFAULT_PERIODS = 25

# Timing metrics are milliseconds; layout shift is a unitless 0..1 score.
_SCORE_METRICS = frozenset({"cumulative_layout_shift"})

# Canonical device aliases -> CrUX ``formFactor`` enum. ``mobile`` is the CLI/PSI word
# for the same population CrUX calls ``PHONE``.
_DEVICE_ALIASES: dict[str, str] = {
    "phone": "PHONE",
    "mobile": "PHONE",
    "smartphone": "PHONE",
    "desktop": "DESKTOP",
    "tablet": "TABLET",
    "tablets": "TABLET",
}

# Core Web Vitals first, then the supporting timings; unknown metrics sort alphabetically.
_METRIC_ORDER = (
    "largest_contentful_paint",
    "interaction_to_next_paint",
    "cumulative_layout_shift",
    "first_contentful_paint",
    "experimental_time_to_first_byte",
    "first_input_delay",
)

# History rows use the short CWV labels already used by the report layer.
_SHORT_METRICS: dict[str, str] = {
    "largest_contentful_paint": "lcp",
    "interaction_to_next_paint": "inp",
    "cumulative_layout_shift": "cls",
    "first_contentful_paint": "fcp",
    "experimental_time_to_first_byte": "ttfb",
    "first_input_delay": "fid",
}


class CruxClient:
    """Query ``records:queryRecord`` and ``records:queryHistoryRecord``."""

    def __init__(
        self,
        *,
        api_key: str,
        http: HttpClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._http = http if http is not None else HttpClient(base_url=self._base_url)

    def origin(self, origin: str) -> ProviderResult[Row]:
        """Field metrics for every URL under ``origin``."""
        return self._query("origin", origin)

    def url(self, url: str, form_factor: str | None = None) -> ProviderResult[Row]:
        """Field metrics for a single page, optionally for one device.

        ``form_factor`` accepts the CrUX enum names and the CLI aliases (``phone``,
        ``mobile``, ``desktop``, ``tablet``). Omitting it keeps the historical
        device-agnostic request and row shape.
        """
        return self._query("url", url, form_factor=form_factor)

    def history(
        self,
        *,
        origin: str = "",
        url: str = "",
        form_factor: str | None = None,
        weeks: int = _HISTORY_DEFAULT_PERIODS,
    ) -> ProviderResult[Row]:
        """Weekly p75 history for one origin or one URL (one row per collection period).

        ``weeks`` is sent as ``collectionPeriodCount`` (the API accepts 1..40 and
        defaults to 25) and is also enforced locally, so a provider that ignores the
        field cannot make the response unbounded.
        """
        field, target = _history_target(origin, url)
        span = _history_weeks(weeks)
        device = _form_factor(form_factor)
        self._require_key()

        body: dict[str, Any] = {field: target, "collectionPeriodCount": span}
        if device is not None:
            body["formFactor"] = device

        fetched = now_utc()
        try:
            payload: object = self._http.post_json(
                self._history_endpoint(), json=body, retry=True, rate_limit_key="crux"
            )
        except SeoError as error:
            if error.code == PROPERTY_NOT_FOUND:
                return _empty_result(target, fetched=fetched)
            if error.code == INVALID_INPUT:
                raise _invalid_target(error, field) from error
            raise

        warnings: list[str] = []
        limitations: list[str] = []
        normalization = _url_normalization(payload)
        if normalization is not None:
            warnings.append(_normalization_warning(normalization))
            limitations.append(CRUX_URL_NORMALIZED)

        periods = _collection_periods(payload)
        if not periods:
            limitations.append(CRUX_HISTORY_PERIODS_MISSING)
            return _empty_result(
                target,
                fetched=fetched,
                warnings=tuple(warnings),
                limitations=tuple(limitations),
            )

        rows, gaps, filtered = _history_rows(payload, periods, span, device)
        data_age, period_limitations = _period_age(periods[-1], fetched)
        limitations.extend(period_limitations)
        if len(periods) < span:
            limitations.append(CRUX_HISTORY_SHORT)
        if gaps:
            limitations.append(CRUX_HISTORY_GAPS)
        if filtered:
            limitations.append(CRUX_HISTORY_METRICS_FILTERED)

        return ProviderResult(
            source="crux",
            property_id=target,
            fetched_at=fetched,
            data_age_days=data_age,
            rows=rows,
            total=len(periods),
            truncated=len(periods) > span,
            limitations=tuple(_unique(limitations)),
            warnings=tuple(warnings),
            extra=_period_extra(periods[-1]),
        )

    def _query(
        self,
        field: str,
        value: str,
        *,
        form_factor: str | None = None,
    ) -> ProviderResult[Row]:
        target = _require_target(value, field)
        device = _form_factor(form_factor)
        self._require_key()

        body: dict[str, str] = {field: target}
        if device is not None:
            body["formFactor"] = device

        fetched = now_utc()
        try:
            payload: object = self._http.post_json(
                self._endpoint(), json=body, retry=True, rate_limit_key="crux"
            )
        except SeoError as error:
            if error.code == PROPERTY_NOT_FOUND:
                return _empty_result(target, fetched=fetched)
            if error.code == INVALID_INPUT:
                raise _invalid_target(error, field) from error
            raise

        warnings: list[str] = []
        limitations: list[str] = []
        normalization = _url_normalization(payload)
        if normalization is not None:
            warnings.append(_normalization_warning(normalization))
            limitations.append(CRUX_URL_NORMALIZED)

        rows = _rows_from(payload)
        period = _collection_period(payload)
        data_age, period_limitations = _period_age(period, fetched)
        limitations.extend(period_limitations)
        if not rows:
            return _empty_result(
                target,
                fetched=fetched,
                period=period,
                warnings=tuple(warnings),
                limitations=tuple(limitations),
            )
        if period is not None:
            rows = [_with_period(row, period) for row in rows]
        if device is not None:
            rows = _with_device(rows, target, device)
        return ProviderResult(
            source="crux",
            property_id=target,
            fetched_at=fetched,
            data_age_days=data_age,
            rows=rows,
            limitations=tuple(_unique(limitations)),
            warnings=tuple(warnings),
            extra=_period_extra(period),
        )

    def _require_key(self) -> None:
        if not self._api_key:
            raise SeoError(
                code=AUTH_REQUIRED,
                message="CrUX requires a Google API key",
                next_action=_LOGIN_NEXT_ACTION,
                detail={"provider": "crux"},
            )

    def _endpoint(self) -> str:
        return f"{self._base_url}/{_RECORD_PATH}?{urlencode({'key': self._api_key})}"

    def _history_endpoint(self) -> str:
        return f"{self._base_url}/{_HISTORY_PATH}?{urlencode({'key': self._api_key})}"


def _require_target(value: str, field: str) -> str:
    target = value.strip()
    if not target:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"CrUX {field} must not be empty",
            next_action=f"pass a non-empty {field}, e.g. https://example.com/",
            detail={"provider": "crux", "field": field},
        )
    return target


def _history_target(origin: str, url: str) -> tuple[str, str]:
    origin_value = origin.strip()
    url_value = url.strip()
    if origin_value and url_value:
        raise SeoError(
            code=INVALID_INPUT,
            message="CrUX history takes either an origin or a url, not both",
            next_action="pass --origin https://example.com or --url https://example.com/page",
            detail={"provider": "crux", "field": "target"},
        )
    if url_value:
        return "url", url_value
    if origin_value:
        return "origin", origin_value
    raise SeoError(
        code=INVALID_INPUT,
        message="CrUX history needs an origin or a url",
        next_action="pass --origin https://example.com or --url https://example.com/page",
        detail={"provider": "crux", "field": "target"},
    )


def _history_weeks(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= HISTORY_MAX_PERIODS
    ):
        raise SeoError(
            code=INVALID_INPUT,
            message=f"CrUX history weeks must be between 1 and {HISTORY_MAX_PERIODS}",
            next_action=f"pass --weeks between 1 and {HISTORY_MAX_PERIODS}",
            detail={"provider": "crux", "field": "weeks"},
        )
    return value


def _form_factor(value: str | None) -> str | None:
    """Normalise a device alias to the CrUX enum, or ``None`` when no device was asked."""
    if value is None:
        return None
    key = value.strip().casefold()
    if not key:
        return None
    device = _DEVICE_ALIASES.get(key)
    if device is None:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unsupported CrUX form factor: {value!r}",
            next_action="use phone, desktop or tablet",
            detail={"provider": "crux", "field": "form_factor"},
        )
    return device


def _with_device(rows: list[Row], url: str, device: str) -> list[Row]:
    """Attach the request's URL and canonical device to every metric row."""
    canonical = device.lower()
    return [{**row, "url": url, "device": canonical} for row in rows]


def _with_period(row: Row, period: tuple[date, date]) -> Row:
    return {
        **row,
        "first_date": period[0].isoformat(),
        "last_date": period[1].isoformat(),
    }


def _period_extra(period: tuple[date, date] | None) -> dict[str, Any]:
    if period is None:
        return {}
    return {
        "collection_period": {
            "first_date": period[0].isoformat(),
            "last_date": period[1].isoformat(),
        }
    }


def _invalid_target(error: SeoError, field: str) -> SeoError:
    # Keep the shared message (host + path only) and add the provider-specific hint.
    return SeoError(
        code=INVALID_INPUT,
        message=error.message,
        next_action=f"check the {field}: CrUX expects an absolute URL",
        retryable=False,
        detail={**error.detail, "provider": "crux", "field": field},
    )


def _period_age(
    period: tuple[date, date] | None,
    fetched: datetime,
) -> tuple[int, list[str]]:
    """Age from the collection period's last date; the 2-day guess is a fallback."""
    if period is None:
        return _DATA_AGE_DAYS, [CRUX_PERIOD_MISSING]
    return max(0, (fetched.date() - period[1]).days), []


def _empty_result(
    target: str,
    *,
    fetched: datetime | None = None,
    period: tuple[date, date] | None = None,
    warnings: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> ProviderResult[Row]:
    fetched_at = fetched or now_utc()
    data_age, period_limitations = _period_age(period, fetched_at)
    return ProviderResult(
        source="crux",
        property_id=target,
        fetched_at=fetched_at,
        data_age_days=data_age,
        limitations=tuple(_unique([LOW_TRAFFIC, *period_limitations, *limitations])),
        warnings=(_NO_FIELD_DATA_WARNING, *warnings),
        extra=_period_extra(period),
    )


def _collection_period(payload: object) -> tuple[date, date] | None:
    record = _mapping(payload, "record")
    raw = record.get("collectionPeriod")
    if not isinstance(raw, Mapping):
        return None
    return _period_pair(raw)


def _collection_periods(payload: object) -> list[tuple[date, date]]:
    record = _mapping(payload, "record")
    raw = record.get("collectionPeriods")
    if not isinstance(raw, list):
        return []
    periods: list[tuple[date, date]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            return []
        pair = _period_pair(entry)
        if pair is None:
            return []
        periods.append(pair)
    return periods


def _period_pair(value: Mapping[Any, Any]) -> tuple[date, date] | None:
    first = _date_value(value.get("firstDate"))
    last = _date_value(value.get("lastDate"))
    if first is None or last is None or last < first:
        return None
    return first, last


def _date_value(value: object) -> date | None:
    if not isinstance(value, Mapping):
        return None
    year = _int_part(value.get("year"))
    month = _int_part(value.get("month"))
    day = _int_part(value.get("day"))
    if year is None or month is None or day is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _int_part(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _url_normalization(payload: object) -> tuple[str, str] | None:
    details = _mapping(payload, "urlNormalizationDetails")
    original = _text(details.get("originalUrl"))
    normalized = _text(details.get("normalizedUrl"))
    if not original and not normalized:
        return None
    return original or "?", normalized or "?"


def _normalization_warning(details: tuple[str, str]) -> str:
    return f"crux normalized url: {_bounded(details[0], 300)} -> {_bounded(details[1], 300)}"


def _rows_from(payload: object) -> list[Row]:
    metrics = _metrics(payload)
    rows: list[Row] = []
    for name in _ordered_names(metrics):
        metric = metrics[name]
        p75 = _p75(metric)
        if p75 is None:
            continue
        rows.append(
            {
                "metric": name,
                "p75": p75,
                "good": _proportion(metric, 0),
                "needs_improvement": _proportion(metric, 1),
                "poor": _proportion(metric, 2),
                "unit": "score" if name in _SCORE_METRICS else "ms",
            }
        )
    return rows


def _history_rows(
    payload: object,
    periods: list[tuple[date, date]],
    span: int,
    device: str | None,
) -> tuple[list[Row], int, bool]:
    """One row per collection period; missing p75 cells are counted, never filled."""
    metrics = _metrics(payload)
    known = [name for name in _METRIC_ORDER if name in metrics]
    filtered = any(name not in _METRIC_ORDER for name in metrics)
    series = {name: _p75_series(metrics[name]) for name in known}

    selected = periods[-span:] if len(periods) > span else periods
    offset = len(periods) - len(selected)
    rows: list[Row] = []
    gaps = 0
    for index, period in enumerate(selected):
        source_index = offset + index
        row: Row = {
            "first_date": period[0].isoformat(),
            "last_date": period[1].isoformat(),
        }
        if device is not None:
            row["device"] = device.lower()
        for name in known:
            values = series[name]
            value = values[source_index] if source_index < len(values) else None
            if value is None:
                gaps += 1
                continue
            row[_SHORT_METRICS[name]] = value
        rows.append(row)
    return rows, gaps, filtered


def _p75_series(metric: object) -> list[float | None]:
    timeseries = _mapping(metric, "percentilesTimeseries")
    raw = timeseries.get("p75s")
    if not isinstance(raw, list):
        return []
    return [_number(value) for value in raw]


def _metrics(payload: object) -> dict[str, Any]:
    raw = _mapping(payload, "record").get("metrics")
    if not isinstance(raw, dict):
        return {}
    return {
        name: value
        for name, value in raw.items()
        if isinstance(name, str) and isinstance(value, dict)
    }


def _ordered_names(metrics: dict[str, Any]) -> list[str]:
    canonical = [name for name in _METRIC_ORDER if name in metrics]
    extras = sorted(name for name in metrics if name not in _METRIC_ORDER)
    return canonical + extras


def _p75(metric: object) -> float | None:
    percentiles = _mapping(metric, "percentiles")
    return _number(percentiles.get("p75"))


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        # CLS percentiles are doubles encoded as strings ("0.06").
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _proportion(metric: object, index: int) -> float:
    if not isinstance(metric, dict):
        return 0.0
    # Real CrUX API shape: ``histogram: [{start, end, density}]``.
    # ``distributions[].proportion`` is kept as a legacy fallback.
    histogram = metric.get("histogram")
    if isinstance(histogram, list) and index < len(histogram):
        bucket = histogram[index]
        if isinstance(bucket, dict):
            density = bucket.get("density")
            if isinstance(density, bool):
                return 0.0
            if isinstance(density, (int, float)):
                return float(density)
    distributions = metric.get("distributions")
    if not isinstance(distributions, list) or index >= len(distributions):
        return 0.0
    bucket = distributions[index]
    if not isinstance(bucket, dict):
        return 0.0
    proportion = bucket.get("proportion")
    if isinstance(proportion, bool) or not isinstance(proportion, (int, float)):
        return 0.0
    return float(proportion)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _mapping(value: object, key: str) -> dict[Any, Any]:
    """Return ``value[key]`` when it is a mapping, otherwise an empty one."""
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    if not isinstance(nested, dict):
        return {}
    return nested
