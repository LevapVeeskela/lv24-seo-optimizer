"""Chrome UX Report client: p75 field metrics for one origin or URL.

CrUX is the only free source of real-user Core Web Vitals. Its honesty quirk is that a
low-traffic URL answers ``404`` instead of an empty record; that is "no field data", not a
failure, so the adapter turns it into an empty :class:`ProviderResult` carrying
``LOW_TRAFFIC``. Callers can then fall back to lab data (PSI) without a special case.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, PROPERTY_NOT_FOUND, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import LOW_TRAFFIC, ProviderResult, Row, now_utc

__all__ = ["DEFAULT_BASE_URL", "CruxClient"]

DEFAULT_BASE_URL = "https://chromeuxreport.googleapis.com/v1"

_RECORD_PATH = "records:queryRecord"
_LOGIN_NEXT_ACTION = "lv24-seo auth login --provider crux"

# CrUX reports on a 28-day rolling window, so the newest data is never fresh.
_DATA_AGE_DAYS = 2
_NO_FIELD_DATA_WARNING = "no field data"

# Timing metrics are milliseconds; layout shift is a unitless 0..1 score.
_SCORE_METRICS = frozenset({"cumulative_layout_adjustment"})

# Core Web Vitals first, then the supporting timings; unknown metrics sort alphabetically.
_METRIC_ORDER = (
    "largest_contentful_paint",
    "interaction_to_next_paint",
    "cumulative_layout_adjustment",
    "first_contentful_paint",
    "experimental_time_to_first_byte",
    "first_input_delay",
)


class CruxClient:
    """Query ``records:queryRecord`` for one origin or one URL."""

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

    def url(self, url: str) -> ProviderResult[Row]:
        """Field metrics for a single page."""
        return self._query("url", url)

    def _query(self, field: str, value: str) -> ProviderResult[Row]:
        target = _require_target(value, field)
        if not self._api_key:
            raise SeoError(
                code=AUTH_REQUIRED,
                message="CrUX requires a Google API key",
                next_action=_LOGIN_NEXT_ACTION,
                detail={"provider": "crux"},
            )

        try:
            payload: object = self._http.post_json(self._endpoint(), json={field: target})
        except SeoError as error:
            if error.code == PROPERTY_NOT_FOUND:
                return _empty_result(target)
            if error.code == INVALID_INPUT:
                raise _invalid_target(error, field) from error
            raise

        rows = _rows_from(payload)
        if not rows:
            return _empty_result(target)
        return ProviderResult(
            source="crux",
            property_id=target,
            fetched_at=now_utc(),
            data_age_days=_DATA_AGE_DAYS,
            rows=rows,
        )

    def _endpoint(self) -> str:
        return f"{self._base_url}/{_RECORD_PATH}?{urlencode({'key': self._api_key})}"


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


def _invalid_target(error: SeoError, field: str) -> SeoError:
    # Keep the shared message (host + path only) and add the provider-specific hint.
    return SeoError(
        code=INVALID_INPUT,
        message=error.message,
        next_action=f"check the {field}: CrUX expects an absolute URL",
        retryable=False,
        detail={**error.detail, "provider": "crux", "field": field},
    )


def _empty_result(target: str) -> ProviderResult[Row]:
    return ProviderResult(
        source="crux",
        property_id=target,
        fetched_at=now_utc(),
        data_age_days=_DATA_AGE_DAYS,
        limitations=(LOW_TRAFFIC,),
        warnings=(_NO_FIELD_DATA_WARNING,),
    )


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
    value = percentiles.get("p75")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _proportion(metric: object, index: int) -> float:
    if not isinstance(metric, dict):
        return 0.0
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


def _mapping(value: object, key: str) -> dict[Any, Any]:
    """Return ``value[key]`` when it is a mapping, otherwise an empty one."""
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    if not isinstance(nested, dict):
        return {}
    return nested
