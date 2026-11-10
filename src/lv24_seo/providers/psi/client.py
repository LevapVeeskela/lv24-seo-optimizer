"""PageSpeed Insights client: Lighthouse lab scores for one URL.

PSI is the lab half of the Core Web Vitals story (CrUX is the field half). It works
without an API key -- a key only raises the quota -- so an empty key is not
``AUTH_REQUIRED``. A Lighthouse run describes one synthetic load and must never be
averaged across runs (see ``docs/data-sources.md``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import ProviderResult, Row, now_utc

__all__ = ["DEFAULT_BASE_URL", "PsiClient"]

DEFAULT_BASE_URL = "https://www.googleapis.com/pagespeedonline/v5"

_RUN_PATH = "runPagespeed"
_STRATEGIES = frozenset({"mobile", "desktop"})

# PSI field keys -> short Core Web Vitals labels.
_CLS_FIELD_KEY = "CUMULATIVE_LAYOUT_SHIFT_SCORE"
_FIELD_LABELS = {
    "LARGEST_CONTENTFUL_PAINT_MS": "LCP",
    "INTERACTION_TO_NEXT_PAINT": "INP",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "CLS",
    "FIRST_CONTENTFUL_PAINT_MS": "FCP",
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "TTFB",
    "FIRST_INPUT_DELAY_MS": "FID",
}
_FIELD_ORDER = ("LCP", "INP", "CLS", "FCP", "TTFB", "FID")


class PsiClient:
    """Run Lighthouse through ``runPagespeed`` for one URL."""

    def __init__(
        self,
        *,
        api_key: str = "",
        http: HttpClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._http = http if http is not None else HttpClient(base_url=self._base_url)

    def run(
        self,
        url: str,
        *,
        strategy: str = "mobile",
        categories: Sequence[str] = ("performance", "seo"),
    ) -> ProviderResult[Row]:
        """Return one row per requested Lighthouse category plus field rows if any."""
        target = _require_url(url)
        mode = _require_strategy(strategy)
        requested = _normalise_categories(categories)

        params: dict[str, Any] = {"url": target, "strategy": mode}
        if requested:
            # A list is encoded as repeated ``category`` parameters, as the API expects.
            params["category"] = list(requested)
        if self._api_key:
            params["key"] = self._api_key

        try:
            payload: object = self._http.get_json(self._endpoint(), params=params)
        except SeoError as error:
            if error.code == INVALID_INPUT:
                raise _invalid_url(error) from error
            raise

        rows, warnings = _lab_rows(payload, requested)
        field_rows, field_warnings = _field_rows(payload)
        rows.extend(field_rows)
        return ProviderResult(
            source="psi",
            property_id=target,
            fetched_at=now_utc(),
            # Lab data is produced by this very call, so there is no provider lag.
            data_age_days=0,
            rows=rows,
            warnings=tuple(warnings + field_warnings),
        )

    def _endpoint(self) -> str:
        return f"{self._base_url}/{_RUN_PATH}"


def _require_url(url: str) -> str:
    target = url.strip()
    if not target:
        raise SeoError(
            code=INVALID_INPUT,
            message="PSI url must not be empty",
            next_action="pass an absolute URL, e.g. https://example.com/",
            detail={"provider": "psi", "field": "url"},
        )
    return target


def _require_strategy(strategy: str) -> str:
    mode = strategy.strip().lower()
    if mode not in _STRATEGIES:
        raise SeoError(
            code=INVALID_INPUT,
            message="unsupported PSI strategy: use mobile or desktop",
            next_action="pass strategy=mobile or strategy=desktop",
            detail={"provider": "psi", "field": "strategy"},
        )
    return mode


def _normalise_categories(categories: Sequence[str]) -> tuple[str, ...]:
    unique: dict[str, None] = {}
    for raw in categories:
        name = raw.strip().lower()
        if not name:
            raise SeoError(
                code=INVALID_INPUT,
                message="PSI category must not be empty",
                next_action="use categories such as performance or seo",
                detail={"provider": "psi", "field": "category"},
            )
        unique.setdefault(name, None)
    return tuple(unique)


def _invalid_url(error: SeoError) -> SeoError:
    # Keep the shared message (host + path only) and add the provider-specific hint.
    return SeoError(
        code=INVALID_INPUT,
        message=error.message,
        next_action="check that the URL is public and reachable",
        retryable=False,
        detail={**error.detail, "provider": "psi"},
    )


def _lab_rows(payload: object, requested: Sequence[str]) -> tuple[list[Row], list[str]]:
    categories = _mapping(_mapping(payload, "lighthouseResult"), "categories")
    rows: list[Row] = []
    warnings: list[str] = []
    for category in requested:
        score = _score(categories.get(category))
        if score is None:
            warnings.append(f"category unavailable: {category}")
            continue
        rows.append({"category": category, "score": score})
    return rows, warnings


def _score(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


def _field_rows(payload: object) -> tuple[list[Row], list[str]]:
    metrics = _mapping(_mapping(payload, "loadingExperience"), "metrics")
    warnings: list[str] = []
    if not metrics:
        metrics = _mapping(_mapping(payload, "originLoadingExperience"), "metrics")
        if metrics:
            warnings.append("field data is origin-level, not URL-level")

    labelled: dict[str, Row] = {}
    for key, entry in metrics.items():
        if not isinstance(key, str):
            continue
        p75 = _field_p75(key, entry)
        if p75 is None:
            continue
        label = _field_label(key)
        labelled[label] = {"category": "field", "metric": label, "p75": p75}

    ordered = [label for label in _FIELD_ORDER if label in labelled]
    ordered.extend(sorted(label for label in labelled if label not in _FIELD_ORDER))
    return [labelled[label] for label in ordered], warnings


def _field_p75(key: str, entry: object) -> float | None:
    if not isinstance(entry, dict):
        return None
    percentile = entry.get("percentile")
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        return None
    value = float(percentile)
    # PSI reports CLS on a 0..100 scale; CrUX (and Lighthouse) use a 0..1 score.
    return value / 100 if key == _CLS_FIELD_KEY else value


def _field_label(key: str) -> str:
    label = _FIELD_LABELS.get(key)
    return label if label is not None else key.removesuffix("_MS")


def _mapping(value: object, key: str) -> dict[Any, Any]:
    """Return ``value[key]`` when it is a mapping, otherwise an empty one."""
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    if not isinstance(nested, dict):
        return {}
    return nested
