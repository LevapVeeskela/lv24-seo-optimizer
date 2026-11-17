"""PageSpeed Insights client: Lighthouse lab scores and audits for one URL.

PSI is the lab half of the Core Web Vitals story (CrUX is the field half). It works
without an API key -- a key only raises the quota -- so an empty key is not
``AUTH_REQUIRED``. A Lighthouse run describes one synthetic load and must never be
averaged across runs (see ``docs/data-sources.md``).

A run can succeed at the HTTP level while Lighthouse reports ``runtimeError`` or a
CAPTCHA. Those are surfaced as warnings plus stable limitation codes, and the
``finalUrl`` is compared with the requested URL so a redirect is never silently
reported as a clean run of the requested page. The per-audit diagnostics (savings,
details type, item count) are bounded: descriptions are plain text, and at most
``PSI_AUDITS_CAP`` audits are returned.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import ProviderResult, Row, now_utc

__all__ = [
    "DEFAULT_BASE_URL",
    "PSI_AUDITS_CAP",
    "PSI_AUDITS_TRUNCATED",
    "PSI_CAPTCHA",
    "PSI_FINAL_URL_DIFFERS",
    "PSI_RUNTIME_ERROR",
    "PsiAudits",
    "PsiClient",
]

DEFAULT_BASE_URL = "https://www.googleapis.com/pagespeedonline/v5"

_RUN_PATH = "runPagespeed"
_STRATEGIES = frozenset({"mobile", "desktop"})

# Stable limitation codes: consumers match on these strings, not on message text.
PSI_RUNTIME_ERROR = "psi_runtime_error"
PSI_FINAL_URL_DIFFERS = "psi_final_url_differs"
PSI_CAPTCHA = "psi_captcha"
PSI_AUDITS_TRUNCATED = "psi_audits_cap"

# Bounds keep one provider payload from becoming an unbounded response.
PSI_AUDITS_CAP = 100
_TEXT_CAP = 160
_DESCRIPTION_CAP = 280

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

_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True, slots=True)
class PsiAudits:
    """One bounded Lighthouse audit list plus the caveats around the run."""

    source: str
    property_id: str
    fetched_at: datetime
    data_age_days: int
    strategy: str
    rows: list[Row]
    categories: list[Row]
    total: int
    truncated: bool
    analysis_utc_timestamp: str
    final_url: str
    limitations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.rows

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "source": self.source,
            "property_id": self.property_id,
            "fetched_at": self.fetched_at.isoformat(),
            "data_age_days": self.data_age_days,
            "truncated": self.truncated,
            "total": self.total,
            "strategy": self.strategy,
            "analysis_utc_timestamp": self.analysis_utc_timestamp,
            "final_url": self.final_url,
        }
        if self.limitations:
            meta["limitations"] = list(self.limitations)
        return meta


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
        payload = self._fetch(target, mode, requested)

        rows, warnings = _lab_rows(payload, requested)
        field_rows, field_warnings = _field_rows(payload)
        rows.extend(field_rows)
        run_warnings, limitations = _run_diagnostics(payload, target)
        return ProviderResult(
            source="psi",
            property_id=target,
            fetched_at=now_utc(),
            # Lab data is produced by this very call, so there is no provider lag.
            data_age_days=0,
            rows=rows,
            limitations=tuple(limitations),
            warnings=(*warnings, *field_warnings, *run_warnings),
        )

    def audits(
        self,
        url: str,
        *,
        strategy: str = "mobile",
        categories: Sequence[str] = ("performance", "seo"),
    ) -> PsiAudits:
        """Return bounded Lighthouse audits plus the category scores for one URL."""
        target = _require_url(url)
        mode = _require_strategy(strategy)
        requested = _normalise_categories(categories)
        payload = self._fetch(target, mode, requested)

        category_rows, category_warnings = _lab_rows(payload, requested)
        run_warnings, limitations = _run_diagnostics(payload, target)
        rows, total, truncated = _audit_rows(payload)
        if truncated:
            limitations.append(PSI_AUDITS_TRUNCATED)

        lab = _mapping(payload, "lighthouseResult")
        root: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
        analysis_timestamp = _text(root.get("analysisUTCTimestamp")) or _text(
            lab.get("analysisUTCTimestamp")
        )
        return PsiAudits(
            source="psi",
            property_id=target,
            fetched_at=now_utc(),
            data_age_days=0,
            strategy=mode,
            rows=rows,
            categories=category_rows,
            total=total,
            truncated=truncated,
            analysis_utc_timestamp=_bounded(analysis_timestamp, 40),
            final_url=_bounded(_text(lab.get("finalUrl")), 500),
            limitations=tuple(limitations),
            warnings=(*category_warnings, *run_warnings),
        )

    def _fetch(self, target: str, mode: str, requested: Sequence[str]) -> object:
        params: dict[str, Any] = {"url": target, "strategy": mode}
        if requested:
            # A list is encoded as repeated ``category`` parameters, as the API expects.
            params["category"] = list(requested)
        if self._api_key:
            params["key"] = self._api_key

        try:
            return self._http.get_json(self._endpoint(), params=params, rate_limit_key="psi")
        except SeoError as error:
            if error.code == INVALID_INPUT:
                raise _invalid_url(error) from error
            raise

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


def _run_diagnostics(payload: object, requested_url: str) -> tuple[list[str], list[str]]:
    """Turn provider-reported run problems into warnings and stable limitation codes."""
    lab = _mapping(payload, "lighthouseResult")
    root: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    warnings: list[str] = []
    limitations: list[str] = []

    runtime = lab.get("runtimeError")
    if isinstance(runtime, Mapping):
        code = _bounded(_text(runtime.get("code")), 80) or "unknown"
        message = _bounded(_text(runtime.get("message")), _TEXT_CAP)
        warning = f"psi runtime error: {code}"
        if message:
            warning = f"{warning}: {message}"
        warnings.append(warning)
        limitations.append(PSI_RUNTIME_ERROR)

    captcha = _text(root.get("captchaResult"))
    if captcha and captcha.casefold() not in {"captcha_not_needed"}:
        warnings.append(f"psi captcha: {_bounded(captcha, 80)}")
        limitations.append(PSI_CAPTCHA)

    final_url = _text(lab.get("finalUrl"))
    if final_url and not _same_url(final_url, requested_url):
        warnings.append(f"psi final url differs from requested: {_bounded(final_url, 300)}")
        limitations.append(PSI_FINAL_URL_DIFFERS)
    return warnings, limitations


def _audit_rows(payload: object) -> tuple[list[Row], int, bool]:
    """Return deterministic audit rows, the provider's total and the cap flag."""
    audits = _mapping(_mapping(payload, "lighthouseResult"), "audits")
    entries = sorted(
        (
            (key, value)
            for key, value in audits.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        ),
        key=lambda item: item[0],
    )
    total = len(entries)
    selected = entries[:PSI_AUDITS_CAP]
    return [_audit_row(key, value) for key, value in selected], total, total > len(selected)


def _audit_row(audit_id: str, value: Mapping[str, Any]) -> Row:
    details = value.get("details")
    detail_map: Mapping[str, Any] = details if isinstance(details, Mapping) else {}
    return {
        "id": _bounded(audit_id, 80),
        "title": _plain_text(value.get("title"), _TEXT_CAP),
        "score": _number(value.get("score")),
        "display_value": _plain_text(value.get("displayValue"), 80),
        "description": _plain_text(value.get("description"), _DESCRIPTION_CAP),
        "details_type": _bounded(_text(detail_map.get("type")), 60),
        "overall_savings_ms": _savings(detail_map.get("overallSavingsMs")),
        "overall_savings_bytes": _savings(detail_map.get("overallSavingsBytes")),
        "items_count": _items_count(detail_map.get("items")),
    }


def _score(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    return _number(value.get("score"))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _savings(value: object) -> int | float | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number) if number.is_integer() else number


def _items_count(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def _plain_text(value: object, limit: int) -> str:
    """Strip HTML tags, decode entities, collapse whitespace and bound the length."""
    text = _text(value)
    if not text:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", text))
    return _bounded(" ".join(text.split()), limit)


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _same_url(left: str, right: str) -> bool:
    return _url_key(left) == _url_key(right)


def _url_key(value: str) -> str:
    """Scheme + host + path without fragment or trailing slash, for redirect checks."""
    text = value.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    host = (parts.hostname or "").lower()
    if not host:
        return text
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme.lower()}://{host}{path}"


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


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: object, key: str) -> dict[Any, Any]:
    """Return ``value[key]`` when it is a mapping, otherwise an empty one."""
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    if not isinstance(nested, dict):
        return {}
    return nested
