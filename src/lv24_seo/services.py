"""Integration service layer shared by the CLI and the MCP server.

The service is the only place where configuration, secrets, provider clients, storage and
report builders meet. Surfaces stay thin: every public method returns the standard
``{ok, data, meta, warnings}`` envelope and never lets a provider failure escape as an
exception.

Design notes
------------
* Clients are built lazily. A token is read from :class:`~lv24_seo.core.secrets.SecretStore`
  only when the matching engine is actually used, so ``properties()`` never needs a token
  and an offline command cannot trigger an authentication error.
* ``clients={...}`` replaces wired clients for tests. Overrides are used even when no token
  is configured; this is what keeps unit tests hermetic.
* Missing credentials are configuration facts, not provider outages: an aggregated report
  skips the engine and records ``"<engine>: token missing"``. A single-engine command fails
  with ``AUTH_REQUIRED`` because the caller explicitly asked for that engine.
* Report builders and delivery are imported lazily. WP5 is still landing in parallel, and a
  missing builder module must not make the service unimportable; the weekly report degrades
  to a minimal honest report with a warning instead of pretending to be the full digest.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast
from urllib.parse import urlsplit

from lv24_seo.analysis.coverage import sitemap_crawl_diff
from lv24_seo.analysis.cwv import DEVICES as CWV_DEVICES
from lv24_seo.analysis.delta import DeltaThresholds, compute_delta
from lv24_seo.analysis.delta import headline as delta_headline
from lv24_seo.core.config import SeoConfig
from lv24_seo.core.envelope import fail, ok
from lv24_seo.core.errors import (
    AUTH_REQUIRED,
    CONFIG_INVALID,
    INTERNAL,
    INVALID_INPUT,
    UPSTREAM_UNAVAILABLE,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import compare_period, days_between, local_today, utc_now
from lv24_seo.crawl.fetcher import Fetcher, RobotsPolicy, is_allowed
from lv24_seo.crawl.graph import GraphStats, build_graph, normalize_url
from lv24_seo.crawl.html import parse_html
from lv24_seo.crawl.models import FetchResult, Issue, PageData, RuleContext
from lv24_seo.crawl.rules import run_all
from lv24_seo.crawl.sitemap import MAX_BYTES as SITEMAP_MAX_BYTES
from lv24_seo.crawl.sitemap import OFF_ORIGIN_LOC, SitemapReader
from lv24_seo.providers.base import (
    SOURCE_BING,
    SOURCE_CRUX,
    SOURCE_GSC,
    SOURCE_PSI,
    SOURCE_YANDEX_METRIKA,
    SOURCE_YANDEX_WEBMASTER,
)
from lv24_seo.providers.bing import BingClient
from lv24_seo.providers.crux import CruxClient
from lv24_seo.providers.gsc import GscClient
from lv24_seo.providers.gsc.auth import (
    SERVICE_ACCOUNT_FILE_NEXT_ACTION,
    ServiceAccountTokenProvider,
    load_service_account_file,
    service_account_file_problem,
    token_provider_from_token,
)
from lv24_seo.providers.psi import PsiClient
from lv24_seo.providers.yandex_metrika import YandexMetrikaClient
from lv24_seo.providers.yandex_webmaster import YandexWebmasterClient
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.store.repositories import Store

__all__ = ["SeoService"]

logger = logging.getLogger(__name__)

_CLIENT_NAMES = frozenset({"gsc", "yandex_webmaster", "yandex_metrika", "bing", "crux", "psi"})
_ENGINE_SECRETS: dict[str, str] = {
    "gsc": "gsc_token",
    "yandex_webmaster": "yandex_webmaster_token",
    "yandex_metrika": "yandex_metrika_token",
    "bing": "bing_api_key",
    "crux": "crux_api_key",
    "psi": "psi_api_key",
}
_ENGINE_LABELS: dict[str, str] = {
    "gsc": "Google Search Console",
    "yandex_webmaster": "Yandex Webmaster",
    "yandex_metrika": "Yandex Metrika",
    "bing": "Bing Webmaster",
    "crux": "Chrome UX Report",
    "psi": "PageSpeed Insights",
}
_ENGINE_NEXT_ACTIONS: dict[str, str] = {
    "gsc": "lv24-seo auth login --provider gsc",
    "yandex_webmaster": "lv24-seo auth login --provider yandex",
    "yandex_metrika": "lv24-seo auth login --provider yandex",
    "bing": "lv24-seo auth login --provider bing",
    "crux": "lv24-seo auth login --provider crux",
    "psi": "set LV24_SEO_PSI_API_KEY or run without a key (PSI works keyless)",
}

# A weekly digest must stay a reasonable payload; GSC's own hard cap is 50k per day/site.
_WEEKLY_GSC_LIMIT = 5_000
_WEEKLY_YANDEX_LIMIT = 500
_GSC_MAX_LIMIT = 50_000
_YANDEX_MAX_LIMIT = 500

# Dashboard stats: a date-dimensioned GSC window has one row per day, and the
# Yandex top-queries report is capped at 3,000 rows server-side. PSI is run for
# at most five URLs (the weekly report uses the same bound).
_STATS_GSC_LIMIT = 5_000
_STATS_YANDEX_LIMIT = 500
_STATS_PSI_URL_LIMIT = 5
# Digest evidence is joined against the bounded delta list, so a larger cap would only
# slow the report without changing the rendered section.
_DIGEST_EVIDENCE_LIMIT = 500
# The CWV section is opt-in: it only appears when the store holds URL+device field rows.
# Five templates is the builder's report bound; the row cap keeps one read bounded.
_WEEKLY_CWV_LIMIT = 5
_CWV_ROW_LIMIT = 2_000
# CWV collection: one command must not fan out without bound. The CLI caps URLs at 50;
# devices multiply that, so 50 URLs on all three devices is the worst case (150 CrUX calls,
# which the shared 2.5 rps bucket paces below the 150 QPM quota).
_CWV_URL_LIMIT_MAX = 50
_CWV_ACCURACY = "crux_field_28d"
_CWV_SOURCES: tuple[str, ...] = ("crux", "psi", "both")
_CWV_DEVICE_ALIASES: dict[str, str] = {
    "phone": "phone",
    "mobile": "phone",
    "smartphone": "phone",
    "desktop": "desktop",
    "tablet": "tablet",
    "tablets": "tablet",
    "all": "all",
}
# CrUX metric names (and already-short labels) -> the stored canonical label. FID is
# deliberately absent: it is deprecated and not part of the current CWV set.
_CWV_METRICS: dict[str, str] = {
    "largest_contentful_paint": "LCP",
    "lcp": "LCP",
    "interaction_to_next_paint": "INP",
    "inp": "INP",
    "cumulative_layout_shift": "CLS",
    "cls": "CLS",
    "first_contentful_paint": "FCP",
    "fcp": "FCP",
    "experimental_time_to_first_byte": "TTFB",
    "ttfb": "TTFB",
}
# PSI has no tablet strategy. Mapping tablet to mobile/desktop would fabricate a device,
# so the pair is skipped and reported as a limitation instead.
_CWV_PSI_STRATEGIES: dict[str, str] = {"phone": "mobile", "desktop": "desktop"}
# Provider event history can be long; one command must not write an unbounded number of rows.
# The digest reads at most _DIGEST_EVIDENCE_LIMIT events, so this is already generous.
_INDEXING_EVENTS_LIMIT = 500
_INDEXING_EVENT_ALIASES: dict[str, str] = {
    "appeared_in_search": "indexed",
    "url_added": "indexed",
    "entered": "indexed",
    "appeared": "indexed",
    "removed_from_search": "excluded",
    "url_removed": "excluded",
    "exited": "excluded",
    "removed": "excluded",
}
_INDEXING_EVENT_DETAIL_KEYS: tuple[str, ...] = (
    "title",
    "excluded_url_status",
    "bad_http_status",
    "target_url",
    "last_access",
)

_HTTP_SCHEMES = frozenset({"http", "https"})
_SITEMAP_PROVIDERS = frozenset({"gsc", "bing"})
_BING_KEY_NEXT_ACTION = "run `lv24-seo auth login --provider bing` or set LV24_SEO_BING_API_KEY"
# PSI works keyless; a key only raises the quota, so it never blocks client construction.
_KEYLESS_CLIENTS = frozenset({"psi"})
_HEADLINE_METRICS: tuple[str, ...] = ("clicks", "impressions", "ctr", "position")
_DELIVERY_UNAVAILABLE = "delivery: unavailable; report was not sent"
_BUILDERS_UNAVAILABLE = "report builders: unavailable; using a minimal fallback report"
_BUILDERS_SIGNATURE_MISMATCH = (
    "report builders: signature mismatch; using a minimal fallback report"
)
# Ten years is far beyond any provider's retention; a larger value is a caller bug, not a window.
MAX_REPORT_DAYS = 3660
_WEEKLY_BUILDER_KEYWORDS = frozenset(
    {
        "property_id",
        "period_start",
        "period_end",
        "current",
        "previous",
        "gsc_rows",
        "gsc_previous_rows",
        "gsc_previous_complete",
        "yandex_rows",
        "metrika_rows",
        "issues",
        "extra_engines",
        "limitations",
        "thresholds",
    }
)

ReportBuilder = Callable[..., Report]
_StatsCollector = Callable[[dict[str, Any], object, date, date, date, date], None]


def _load_weekly_builder() -> ReportBuilder | None:
    """Return ``report.builders.build_weekly`` when WP5 is installed."""
    try:
        from lv24_seo.report.builders import build_weekly
    except ImportError:
        return None
    return build_weekly if callable(build_weekly) else None


def _weekly_builder_compatible(builder: ReportBuilder) -> bool:
    """Check the builder signature once instead of catching a real ``TypeError`` later.

    A missing parameter is a wiring/version mismatch and gets the documented fallback. A
    ``TypeError`` raised *inside* a compatible builder is a real bug and must surface (the
    service returns ``INTERNAL`` and no snapshot is persisted).
    """
    try:
        parameters = inspect.signature(builder).parameters
    except (TypeError, ValueError):  # exotic callables: assume the documented contract
        return True
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return True
    return set(parameters) >= _WEEKLY_BUILDER_KEYWORDS


def _weekly_builder_accepts(builder: ReportBuilder, keyword: str) -> bool:
    """Whether an optional builder keyword can be passed without breaking an older builder.

    ``digest_evidence`` is opt-in: a builder from an older wheel still produces the historical
    report instead of failing with a ``TypeError`` the service would report as ``INTERNAL``.
    """
    try:
        parameters = inspect.signature(builder).parameters
    except (TypeError, ValueError):
        return True
    if keyword in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


@dataclass(slots=True)
class _CrawlOutcome:
    """Everything one crawl collected before it is persisted."""

    start_url: str
    site_url: str
    pages: list[PageData]
    issues: list[Issue]
    errors: list[str]
    blocked: list[str]
    truncated: bool
    graph: GraphStats
    robots: RobotsPolicy
    sitemap_urls: list[str] = field(default_factory=list)
    sitemap_truncated: bool = False
    sitemap_errors: list[str] = field(default_factory=list)
    request_count: int = 0
    request_budget: int = 0
    coverage_notes: list[str] = field(default_factory=list)


class SeoService:
    """One configured workspace: providers, storage and reports behind envelopes.

    ``store`` is optional on purpose: read-only surfaces (``properties``) and tests that do
    not need persistence can omit it. Every method that writes checks for it and returns a
    warning instead of creating a database as a side effect.
    """

    def __init__(
        self,
        config: SeoConfig,
        *,
        store: Store | None = None,
        secrets: SecretStore | None = None,
        clients: dict[str, object] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._secrets = (
            secrets if secrets is not None else SecretStore(data_dir=config.storage.data_dir)
        )

        overrides = dict(clients or {})
        unknown = sorted(set(overrides) - _CLIENT_NAMES)
        if unknown:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"unknown client override: {', '.join(unknown)}",
                next_action=f"use one of: {', '.join(sorted(_CLIENT_NAMES))}",
            )
        self._overrides = overrides
        self._wired: dict[str, object] = {}

    @property
    def timezone(self) -> str:
        """Report timezone resolved from the workspace config."""
        return self._config.report.timezone

    # -- discovery ---------------------------------------------------------------------------

    def properties(self) -> dict[str, Any]:
        """List configured providers, their target and whether a token is present."""
        warnings: list[str] = []
        return self._respond("properties", warnings, lambda: self._properties(warnings))

    def _properties(self, warnings: list[str]) -> dict[str, Any]:
        entries = [self._provider_entry(*spec) for spec in self._provider_specs()]

        for entry in entries:
            if not (entry["configured"] and entry["requires_token"]):
                continue
            if entry["token_present"]:
                continue
            problem = (
                self._gsc_service_account_problem()
                if entry["provider"] == "gsc" and self._gsc_service_account_file()
                else None
            )
            if problem is not None:
                warnings.append(f"gsc: {problem}")
            else:
                warnings.append(f"{entry['provider']}: token missing")

        data = {
            "project": {
                "name": self._config.project.name,
                "base_url": self._config.project.base_url,
                "default_property": self._config.project.default_property,
            },
            "providers": entries,
        }
        meta = {
            "configured": [entry["provider"] for entry in entries if entry["configured"]],
            "ready": [entry["provider"] for entry in entries if entry["ready"]],
            "tokens_present": [entry["provider"] for entry in entries if entry["token_present"]],
        }
        return ok(data, meta=meta, warnings=warnings)

    def _provider_specs(self) -> tuple[tuple[str, object | None, str, str, bool], ...]:
        providers = self._config.providers
        gsc = providers.gsc
        webmaster = providers.yandex_webmaster
        metrika = providers.yandex_metrika
        bing = providers.bing
        crux = providers.crux
        psi = providers.psi
        return (
            ("gsc", gsc, "gsc_token", gsc.property if gsc else "", True),
            (
                "yandex_webmaster",
                webmaster,
                "yandex_webmaster_token",
                webmaster.host_id if webmaster else "",
                True,
            ),
            (
                "yandex_metrika",
                metrika,
                "yandex_metrika_token",
                metrika.counter if metrika else "",
                True,
            ),
            ("bing", bing, "bing_api_key", bing.site if bing else "", True),
            ("crux", crux, "crux_api_key", crux.origin if crux else "", True),
            ("psi", psi, "psi_api_key", ", ".join(psi.urls) if psi else "", False),
        )

    def _provider_entry(
        self,
        provider: str,
        block: object | None,
        secret_name: str,
        target: str,
        requires_token: bool,
    ) -> dict[str, Any]:
        configured = block is not None and bool(target.strip())
        present, source = self._credential_state(provider, secret_name)
        state = "present" if present else "missing"
        return {
            "provider": provider,
            "configured": configured,
            "target": target,
            "requires_token": requires_token,
            "token": state,
            "token_present": present,
            "token_source": source,
            "ready": configured and (present or not requires_token),
        }

    # -- dashboard stats ---------------------------------------------------------------------

    def stats(self, *, days: int = 7, lag_days: int = 2) -> dict[str, Any]:
        """Per-provider dashboard metrics for one period. Read-only: no store, no writes."""
        warnings: list[str] = []
        return self._respond("stats", warnings, lambda: self._stats(days, lag_days, warnings))

    def _stats(self, days: int, lag_days: int, warnings: list[str]) -> dict[str, Any]:
        span = _positive_int(days, name="days", maximum=MAX_REPORT_DAYS)
        lag = _non_negative_int(lag_days, name="lag_days")
        end = local_today(self._config.report.timezone)
        (start, current_end), (previous_start, previous_end) = compare_period(
            span, end=end, lag_days=lag
        )
        specs = {spec[0]: spec for spec in self._provider_specs()}
        collectors: tuple[tuple[str, _StatsCollector], ...] = (
            (SOURCE_GSC, self._stats_gsc),
            (SOURCE_YANDEX_WEBMASTER, self._stats_yandex),
            (SOURCE_YANDEX_METRIKA, self._stats_metrika),
            (SOURCE_BING, self._stats_bing),
            (SOURCE_CRUX, self._stats_crux),
            (SOURCE_PSI, self._stats_psi),
        )
        services: list[dict[str, Any]] = []
        for provider, collect in collectors:
            block, _, secret_name, target, requires_token = specs[provider]
            configured = block is not None and bool(target.strip())
            ready = configured and (
                self._credential_present(provider, secret_name) or not requires_token
            )
            entry = _stats_entry(provider, configured=configured, ready=ready)
            if not configured:
                entry["status"] = "not_configured"
            elif not ready:
                entry["status"] = "missing_token"
                if provider == "gsc" and self._gsc_service_account_file():
                    problem = self._gsc_service_account_problem()
                    if problem is not None:
                        warnings.append(f"gsc: {problem}")
            else:
                try:
                    client = self._client_for(provider, warnings)
                    collect(entry, client, start, current_end, previous_start, previous_end)
                except SeoError as exc:
                    entry["status"] = "error"
                    entry["error"] = {"code": exc.code, "message": exc.message}
                except Exception:
                    # One broken provider must never cancel the dashboard.
                    logger.warning("stats: unexpected provider failure for %s", provider)
                    entry["status"] = "error"
                    entry["error"] = {
                        "code": INTERNAL,
                        "message": f"unexpected error while collecting {provider}",
                    }
            services.append(entry)

        configured_entries = [entry for entry in services if entry["configured"]]
        returned = sum(1 for entry in configured_entries if entry["status"] == "ok")
        failed = sum(1 for entry in configured_entries if entry["status"] == "error")
        missing = sum(1 for entry in configured_entries if entry["status"] == "missing_token")
        truncated = any(bool(entry["truncated"]) for entry in configured_entries)
        complete = bool(
            configured_entries
            and returned == len(configured_entries)
            and not failed
            and not missing
            and not truncated
        )
        coverage = {
            "configured": len(configured_entries),
            "returned": returned,
            "failed": failed,
            "missing": missing,
            "complete": complete,
        }
        limitations = _unique([item for entry in services for item in entry["limitations"]])
        service_warnings = _unique(
            [*warnings, *(item for entry in services for item in entry["warnings"])]
        )
        data_age = max((_int_of(entry["data_age_days"]) for entry in services), default=0)
        period = {"start": start.isoformat(), "end": current_end.isoformat()}
        previous_period = {
            "start": previous_start.isoformat(),
            "end": previous_end.isoformat(),
        }
        data = {
            "period": period,
            "previous_period": previous_period,
            "generated_at": utc_now().isoformat(timespec="seconds"),
            "services": services,
            "coverage": coverage,
            "limitations": limitations,
            "warnings": service_warnings,
        }
        meta = {
            "period": period,
            "previous_period": previous_period,
            "days": span,
            "lag_days": lag,
            "complete": complete,
            "truncated": truncated,
            "data_age_days": data_age,
            "limitations": limitations,
        }
        return ok(data, meta=meta, warnings=service_warnings)

    def _stats_gsc(
        self,
        entry: dict[str, Any],
        client: object,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
    ) -> None:
        current = _call(
            client,
            "search_analytics",
            start_date=start,
            end_date=end,
            dimensions=("date",),
            row_limit=_STATS_GSC_LIMIT,
            max_rows=_STATS_GSC_LIMIT,
        )
        previous = _call(
            client,
            "search_analytics",
            start_date=previous_start,
            end_date=previous_end,
            dimensions=("date",),
            row_limit=_STATS_GSC_LIMIT,
            max_rows=_STATS_GSC_LIMIT,
        )
        _stats_fill(entry, _combined_result_meta((current, previous), source=SOURCE_GSC))
        current_rows = _result_rows(current)
        previous_rows = _result_rows(previous)
        entry["rows"] = len(current_rows)
        if current_rows:
            entry["status"] = "ok"
            entry["metrics"] = _gsc_totals(current_rows)
        else:
            entry["status"] = "no_data"
        if previous_rows:
            entry["previous"] = _gsc_totals(previous_rows)

    def _stats_yandex(
        self,
        entry: dict[str, Any],
        client: object,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
    ) -> None:
        host_id = self._yandex_host_id()
        current = _call(
            client,
            "popular_queries",
            host_id,
            limit=_STATS_YANDEX_LIMIT,
            date_from=start,
            date_to=end,
        )
        previous = _call(
            client,
            "popular_queries",
            host_id,
            limit=_STATS_YANDEX_LIMIT,
            date_from=previous_start,
            date_to=previous_end,
        )
        _stats_fill(
            entry, _combined_result_meta((current, previous), source=SOURCE_YANDEX_WEBMASTER)
        )
        current_rows = _result_rows(current)
        previous_rows = _result_rows(previous)
        entry["rows"] = len(current_rows)
        if current_rows:
            entry["status"] = "ok"
            entry["metrics"] = _yandex_totals(current_rows)
        else:
            entry["status"] = "no_data"
        if previous_rows:
            entry["previous"] = _yandex_totals(previous_rows)

    def _stats_metrika(
        self,
        entry: dict[str, Any],
        client: object,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
    ) -> None:
        metrika = self._config.providers.yandex_metrika
        counter_id = metrika.counter if metrika is not None else ""
        metrics = ("ym:s:visits", "ym:s:users")
        current = _call(
            client,
            "stat_data",
            counter=counter_id,
            metrics=metrics,
            date1=start,
            date2=end,
            limit=1,
        )
        previous = _call(
            client,
            "stat_data",
            counter=counter_id,
            metrics=metrics,
            date1=previous_start,
            date2=previous_end,
            limit=1,
        )
        _stats_fill(entry, _combined_result_meta((current, previous), source=SOURCE_YANDEX_METRIKA))
        current_rows = _result_rows(current)
        previous_rows = _result_rows(previous)
        entry["rows"] = len(current_rows)
        if current_rows:
            entry["status"] = "ok"
            entry["metrics"] = _metrika_totals(current_rows)
        else:
            entry["status"] = "no_data"
        if previous_rows:
            entry["previous"] = _metrika_totals(previous_rows)

    def _stats_bing(
        self,
        entry: dict[str, Any],
        client: object,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
    ) -> None:
        del start, end, previous_start, previous_end
        result = _call(client, "query_stats")
        rows = _result_rows(result)
        _stats_fill(entry, _result_meta(result, source=SOURCE_BING, rows=rows))
        entry["limitations"] = _result_limitations(result)
        entry["warnings"] = _result_warnings(result)
        entry["rows"] = len(rows)
        if rows:
            entry["status"] = "ok"
            entry["metrics"] = _bing_totals(rows)
        else:
            entry["status"] = "no_data"

    def _stats_crux(
        self,
        entry: dict[str, Any],
        client: object,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
    ) -> None:
        del start, end, previous_start, previous_end
        crux = self._config.providers.crux
        origin = crux.origin.strip() if crux is not None else ""
        result = _call(client, "origin", origin)
        rows = _result_rows(result)
        _stats_fill(entry, _result_meta(result, source=SOURCE_CRUX, rows=rows))
        entry["limitations"] = _result_limitations(result)
        entry["warnings"] = _result_warnings(result)
        entry["rows"] = len(rows)
        metrics, units = _crux_metrics(rows)
        entry["metrics"] = metrics
        entry["units"] = units
        entry["status"] = "ok" if rows else "no_data"

    def _stats_psi(
        self,
        entry: dict[str, Any],
        client: object,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
    ) -> None:
        del start, end, previous_start, previous_end
        psi = self._config.providers.psi
        urls = list(psi.urls) if psi is not None else []
        strategy = psi.strategy if psi is not None else "mobile"
        collected: list[dict[str, Any]] = []
        first_rows: list[dict[str, Any]] = []
        psi_warnings: list[str] = []
        limitations: list[str] = []
        data_age = 0
        truncated = False
        for index, url in enumerate(urls[:_STATS_PSI_URL_LIMIT]):
            result = _call(client, "run", url, strategy=strategy)
            rows = _result_rows(result)
            if index == 0:
                first_rows = rows
            collected.extend(rows)
            psi_warnings.extend(_result_warnings(result))
            limitations.extend(_result_limitations(result))
            data_age = max(data_age, _result_data_age(result))
            truncated = truncated or _result_truncated(result)
        if len(urls) > 1:
            limitations.append("lab data, first URL only")
        if len(urls) > _STATS_PSI_URL_LIMIT:
            truncated = True
            limitations.append(
                f"psi: only the first {_STATS_PSI_URL_LIMIT} configured urls were checked"
            )
        entry["rows"] = len(collected)
        entry["metrics"] = _psi_metrics(first_rows) if first_rows else {}
        entry["status"] = "ok" if collected else "no_data"
        entry["data_age_days"] = data_age
        entry["truncated"] = truncated
        entry["limitations"] = _unique(limitations)
        entry["warnings"] = _unique(psi_warnings)

    # -- data --------------------------------------------------------------------------------

    def gsc_query(
        self,
        *,
        start: date,
        end: date,
        dimensions: Sequence[str] = ("query",),
        limit: int = 100,
    ) -> dict[str, Any]:
        """Query GSC search analytics and persist the returned metrics/pages."""
        warnings: list[str] = []
        return self._respond(
            "gsc_query",
            warnings,
            lambda: self._gsc_query(start, end, dimensions, limit, warnings),
        )

    def _gsc_query(
        self,
        start: date,
        end: date,
        dimensions: Sequence[str],
        limit: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        first = _as_date(start, name="start")
        last = _as_date(end, name="end")
        days_between(first, last)
        dims = _dimensions(dimensions)
        size = _positive_int(limit, name="limit", maximum=_GSC_MAX_LIMIT)

        client = self._client_for("gsc", warnings)
        result = _call(
            client,
            "search_analytics",
            start_date=first,
            end_date=last,
            dimensions=dims,
            row_limit=size,
            max_rows=size,
        )
        rows = _result_rows(result)
        warnings.extend(_result_warnings(result))

        meta = _result_meta(result, source="gsc", rows=rows)
        meta.update(
            {
                "start": first.isoformat(),
                "end": last.isoformat(),
                "dimensions": list(dims),
                "limit": size,
            }
        )

        property_id = _gsc_property(result, self._config)
        if self._store is None:
            warnings.append("store: not configured; rows were not stored")
        elif not property_id:
            warnings.append("gsc: property missing; rows were not stored")
        else:
            meta["persisted"] = self._persist_gsc(rows, dims, first, last, property_id)

        return ok({"rows": rows}, meta=meta, warnings=warnings)

    def _persist_gsc(
        self,
        rows: Sequence[Mapping[str, Any]],
        dimensions: Sequence[str],
        start: date,
        end: date,
        property_id: str,
    ) -> dict[str, int]:
        store = self._store
        if store is None:  # pragma: no cover - callers check first
            return {"metrics": 0, "pages": 0}

        scope = _scope_for_dimensions(dimensions)
        daily = "date" in dimensions
        counts = {"metrics": 0, "pages": 0}

        with store.atomic():
            for row in rows:
                key = _metric_key(row, dimensions)
                for metric in ("clicks", "impressions"):
                    value = _float_or_none(row.get(metric))
                    if value is None:
                        continue
                    if daily:
                        day = _row_day(row, dimensions, end)
                        store.upsert_metric_daily(
                            property_id,
                            "gsc",
                            day,
                            scope=scope,
                            key=key,
                            metric=metric,
                            value=value,
                        )
                    else:
                        store.upsert_metric_window(
                            property_id,
                            "gsc",
                            start,
                            end,
                            scope=scope,
                            key=key,
                            metric=metric,
                            value=value,
                        )
                    counts["metrics"] += 1

            for row in rows:
                page_url = row.get("page")
                if not isinstance(page_url, str) or not page_url.strip():
                    continue
                # A sighting, not a crawl: never overwrite crawl observations.
                store.record_gsc_sighting(property_id, page_url.strip())
                counts["pages"] += 1

        return counts

    def yandex_queries(self, *, limit: int = 100) -> dict[str, Any]:
        """Top Yandex queries for the configured host."""
        warnings: list[str] = []
        return self._respond(
            "yandex_queries",
            warnings,
            lambda: self._yandex_queries(limit, warnings),
        )

    def _yandex_queries(self, limit: int, warnings: list[str]) -> dict[str, Any]:
        size = _positive_int(limit, name="limit", maximum=_YANDEX_MAX_LIMIT)
        client = self._client_for("yandex_webmaster", warnings)
        host_id = self._yandex_host_id()
        result = _call(client, "popular_queries", host_id, limit=size)
        rows = _result_rows(result)
        warnings.extend(_result_warnings(result))

        meta = _result_meta(result, source="yandex_webmaster", rows=rows)
        meta.update({"host_id": host_id, "limit": size})
        return ok({"host_id": host_id, "rows": rows}, meta=meta, warnings=warnings)

    def yandex_indexing(self) -> dict[str, Any]:
        """Yandex indexing history, search-URL events, problems and current counters."""
        warnings: list[str] = []
        return self._respond(
            "yandex_indexing",
            warnings,
            lambda: self._yandex_indexing(warnings),
        )

    def _yandex_indexing(self, warnings: list[str]) -> dict[str, Any]:
        client = self._client_for("yandex_webmaster", warnings)
        host_id = self._yandex_host_id()

        summary = _call(client, "summary", host_id)
        history = _call(client, "indexing_history", host_id)
        events = _optional_call(client, "search_urls_events", host_id)
        problems = _call(client, "diagnostics", host_id)

        parts = {
            "summary": summary,
            "history": history,
            "problems": problems,
        }
        if events is not None:
            parts["events"] = events
        data: dict[str, Any] = {"host_id": host_id, "events": []}
        calls: dict[str, Any] = {}
        limitations: list[str] = []
        data_age = 0
        truncated = False

        for name, result in parts.items():
            rows = _result_rows(result)
            data[name] = rows
            calls[name] = _result_meta(result, source="yandex_webmaster", rows=rows)
            warnings.extend(_result_warnings(result))
            limitations.extend(_result_limitations(result))
            data_age = max(data_age, _result_data_age(result))
            truncated = truncated or _result_truncated(result)

        meta = {
            "host_id": host_id,
            "calls": calls,
            "data_age_days": data_age,
            "truncated": truncated,
            "limitations": _unique(limitations),
        }
        persisted = self._persist_indexing_events(
            provider=SOURCE_YANDEX_WEBMASTER,
            property_id=_report_property_id(self._config),
            rows=data["events"],
            limitations=limitations,
            warnings=warnings,
        )
        if persisted is not None:
            meta["indexing_events"] = persisted
            meta["limitations"] = _unique(limitations)
        return ok(data, meta=meta, warnings=_unique(warnings))

    def _persist_indexing_events(
        self,
        *,
        provider: str,
        property_id: str,
        rows: Sequence[Mapping[str, Any]],
        limitations: list[str],
        warnings: list[str],
    ) -> dict[str, int] | None:
        """Store provider-reported per-URL events without failing the read command.

        Yandex's event *history* endpoint returns daily aggregate counts, not URLs; such a
        row has no per-URL identity and is skipped instead of being assigned a guessed URL.
        A row is stored only when it carries an absolute URL, an unambiguous event name and
        an event date. The write is one transaction and every storage failure becomes a
        warning: an indexing read must still return the provider data.
        """
        store = self._store
        if store is None or not rows:
            return None

        considered = rows[:_INDEXING_EVENTS_LIMIT]
        truncated = len(rows) - len(considered)
        candidates: list[tuple[str, str, date, dict[str, Any] | None]] = []
        skipped = 0
        for row in considered:
            candidate = _indexing_event_candidate(row)
            if candidate is None:
                skipped += 1
                continue
            candidates.append(candidate)

        counts = {"new": 0, "existing": 0, "skipped": skipped, "truncated": truncated}
        if truncated:
            limitations.append(
                f"indexing events: only the first {_INDEXING_EVENTS_LIMIT} of {len(rows)} "
                "provider rows were considered"
            )
        if not candidates:
            limitations.append(
                "indexing events: provider rows carry no per-URL identity; nothing was stored"
            )
            return counts

        try:
            with store.atomic():
                for url, event, event_date, detail in candidates:
                    status = store.upsert_indexing_event(
                        provider,
                        property_id,
                        url,
                        event,
                        event_date,
                        detail=detail,
                    )
                    counts[status] += 1
        except Exception:  # storage must never cancel the provider read
            logger.warning("indexing events: could not persist provider rows")
            warnings.append("indexing events: stored rows could not be saved")
            return None
        if skipped:
            limitations.append(
                f"indexing events: {skipped} provider row(s) without url/event/date were not stored"
            )
        return counts

    # -- sitemap writes ----------------------------------------------------------------------

    def sitemap_target(
        self,
        *,
        provider: str = "gsc",
        property: str = "",
        sitemap_url: str = "",
    ) -> dict[str, str]:
        """Resolve the site and sitemap URL a sitemap write would target.

        Shared by the CLI and the MCP tool so both surfaces print the same plan target. It is
        read-only: no provider is contacted and no secret value is returned. For Bing it also
        checks that the API key is present, so a dry run fails with ``AUTH_REQUIRED`` instead
        of handing out a token that cannot be applied. ``property`` overrides
        ``providers.bing.site`` when given; otherwise the configured site is used.
        """
        name = _sitemap_provider(provider)
        site = property.strip()
        url = sitemap_url.strip()

        if name == "gsc":
            if not site:
                raise SeoError(
                    code=INVALID_INPUT,
                    message="property is required for gsc",
                    next_action="pass --property sc-domain:example.com",
                )
            if not url:
                raise SeoError(
                    code=INVALID_INPUT,
                    message="sitemap_url is required for gsc",
                    next_action="pass --url https://example.com/sitemap.xml",
                )
            return {
                "provider": "gsc",
                "site_url": site,
                "sitemap_url": _absolute_http_url(url, name="sitemap_url"),
            }

        bing = self._config.providers.bing
        configured_site = bing.site.strip() if bing is not None else ""
        site = site or configured_site
        if not site:
            raise SeoError(
                code=CONFIG_INVALID,
                message="Bing Webmaster site is not configured",
                next_action=(
                    "set providers.bing.site in seo.toml or pass --property https://example.com"
                ),
            )
        if not url:
            base_url = self._config.project.base_url.strip().rstrip("/")
            if not base_url:
                raise SeoError(
                    code=CONFIG_INVALID,
                    message=(
                        "project.base_url is not configured; the default sitemap URL is unknown"
                    ),
                    next_action=(
                        "set project.base_url in seo.toml or pass --url "
                        "https://example.com/sitemap.xml"
                    ),
                )
            url = f"{base_url}/sitemap.xml"
        if not self._bing_key_present():
            raise SeoError(
                code=AUTH_REQUIRED,
                message="Bing Webmaster API key is missing",
                next_action=_BING_KEY_NEXT_ACTION,
            )
        return {
            "provider": "bing",
            "site_url": site,
            "sitemap_url": _absolute_http_url(url, name="sitemap_url"),
        }

    def submit_sitemap(
        self,
        *,
        provider: str = "gsc",
        property: str = "",
        sitemap_url: str = "",
        action: str = "submit",
    ) -> dict[str, Any]:
        """Submit one sitemap through the provider write path shared by CLI and MCP.

        Only Bing is wired: the JSON ``SubmitFeed`` endpoint is current, while GSC has no
        sitemap write adapter yet and Bing ``delete`` is not implemented. Those cases raise
        ``WRITE_DISABLED`` with a next action instead of pretending the write happened.
        """
        warnings: list[str] = []
        return self._respond(
            "submit_sitemap",
            warnings,
            lambda: self._submit_sitemap(provider, property, sitemap_url, action, warnings),
        )

    def _submit_sitemap(
        self,
        provider: str,
        property: str,
        sitemap_url: str,
        action: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        verb = action.strip().lower()
        if verb not in {"submit", "delete"}:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"unknown sitemap action {action!r}",
                next_action="pass submit or delete",
            )
        target = self.sitemap_target(
            provider=provider,
            property=property,
            sitemap_url=sitemap_url,
        )
        if target["provider"] == "gsc":
            raise SeoError(
                code=WRITE_DISABLED,
                message="gsc sitemap submit is not wired yet",
                next_action=(
                    "submit the sitemap in Search Console until the GSC write adapter lands; "
                    "the engine never pretends a write happened"
                ),
                retryable=False,
            )
        if verb == "delete":
            raise SeoError(
                code=WRITE_DISABLED,
                message="bing sitemap delete is not wired yet",
                next_action=(
                    "delete the sitemap in Bing Webmaster Tools until the delete adapter lands; "
                    "the engine never pretends a write happened"
                ),
                retryable=False,
            )

        client = self._bing_submit_client(target["site_url"])
        try:
            result = client.submit_feed(target["sitemap_url"])
        finally:
            if "bing" not in self._overrides:
                client.close()
        warnings.extend(result.warnings)
        return ok(
            {
                "provider": "bing",
                "status": "submitted",
                "site_url": target["site_url"],
                "sitemap_url": target["sitemap_url"],
            },
            meta={"source": "bing", "submitted": 1, "data_age_days": 0},
            warnings=_unique(warnings),
        )

    def _bing_key_present(self) -> bool:
        """Whether a Bing API key is available, including a test-injected client."""
        return "bing" in self._overrides or self._secret_present("bing_api_key")

    def _bing_submit_client(self, site_url: str) -> BingClient:
        override = self._overrides.get("bing")
        if override is not None:
            return cast(BingClient, override)
        return BingClient(api_key=self._require_secret("bing"), site_url=site_url)

    def cwv_collect(
        self,
        *,
        urls: Sequence[str],
        device: str = "phone",
        source: str = "crux",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Collect URL+device Core Web Vitals and persist CrUX field rows.

        CrUX is the field source of record: its p75 rows are written to
        ``metric_daily`` as ``source=crux``, ``scope=<device>`` and ``key=<url>``,
        which is exactly what the weekly CWV section reads. PSI is lab data and is
        never persisted: the builder treats every stored p75 as field data, so
        writing a Lighthouse score there would pollute the field series.
        """
        warnings: list[str] = []
        return self._respond(
            "cwv_collect",
            warnings,
            lambda: self._cwv_collect(urls, device, source, limit, warnings),
        )

    def _cwv_collect(
        self,
        urls: Sequence[str],
        device: str,
        source: str,
        limit: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        source_name = _cwv_source(source)
        devices = _cwv_devices(device)
        span = _positive_int(limit, name="limit", maximum=_CWV_URL_LIMIT_MAX)
        targets = _cwv_urls(urls)
        selected = targets[:span]
        skipped = targets[span:]
        total = len(targets)
        processed = len(selected)
        pairs_total = total * len(devices)
        pairs_processed = processed * len(devices)
        observed_on = local_today(self._config.report.timezone)
        property_id = _report_property_id(self._config)
        limitations: list[str] = []
        data_ages: list[int] = []

        if skipped:
            limitations.append("cwv_url_cap")
        if source_name in {"psi", "both"}:
            # Lab rows are reported, never stored: a stored p75 would be read as field data.
            limitations.append("cwv_lab_ignored")
            if "tablet" in devices:
                limitations.append("psi_tablet_unsupported")

        crux_client: object | None = None
        if source_name in {"crux", "both"}:
            # Resolve the key first: a missing CrUX key is the documented AUTH_REQUIRED
            # contract even when the local store is also absent.
            crux_client = self._client_for(SOURCE_CRUX, warnings)
            if self._store is None:
                raise SeoError(
                    code=CONFIG_INVALID,
                    message="store is not configured; CWV field rows cannot be persisted",
                    next_action="run `lv24-seo init` so the data directory exists, then retry",
                )

        field_rows: list[dict[str, Any]] = []
        lab_rows: list[dict[str, Any]] = []
        unsupported: set[str] = set()
        crux_empty: list[str] = []
        crux_calls = 0
        psi_calls = 0

        if source_name in {"crux", "both"}:
            assert crux_client is not None
            for url in selected:
                for form_factor in devices:
                    result = _call(crux_client, "url", url, form_factor=form_factor)
                    crux_calls += 1
                    data_ages.append(_result_data_age(result))
                    limitations.extend(_result_limitations(result))
                    warnings.extend(_result_warnings(result))
                    rows = _result_rows(result)
                    if not rows:
                        crux_empty.append(f"{url} ({form_factor})")
                    for row in rows:
                        metric = _cwv_metric(row.get("metric"))
                        if metric is None:
                            name = row.get("metric")
                            if isinstance(name, str) and name.strip():
                                unsupported.add(name.strip())
                            continue
                        p75 = _float_or_none(row.get("p75"))
                        if p75 is None or not math.isfinite(p75) or p75 < 0:
                            continue
                        field_rows.append(
                            {
                                "url": url,
                                "device": form_factor,
                                "metric": metric,
                                "p75": p75,
                                "unit": "score" if metric == "CLS" else "ms",
                                "source": SOURCE_CRUX,
                            }
                        )

        if source_name in {"psi", "both"}:
            psi = self._maybe_client(SOURCE_PSI)
            strategies = [
                (form_factor, _CWV_PSI_STRATEGIES[form_factor])
                for form_factor in devices
                if form_factor in _CWV_PSI_STRATEGIES
            ]
            if not strategies:
                warnings.append("psi: tablet is not supported; no lab run was made")
            for url in selected:
                for form_factor, strategy in strategies:
                    result = _call(psi, "run", url, strategy=strategy)
                    psi_calls += 1
                    data_ages.append(_result_data_age(result))
                    limitations.extend(_result_limitations(result))
                    warnings.extend(_result_warnings(result))
                    for row in _result_rows(result):
                        lab_rows.append(
                            {
                                **row,
                                "url": url,
                                "device": form_factor,
                                "kind": "lab",
                                "source": SOURCE_PSI,
                            }
                        )

        if unsupported:
            limitations.append("cwv_metric_unsupported: " + ", ".join(sorted(unsupported)))
        if source_name in {"crux", "both"} and not field_rows:
            limitations.append("cwv_no_field_rows")

        if field_rows and self._store is not None:
            with self._store.atomic():
                for row in field_rows:
                    self._store.upsert_metric_daily(
                        property_id,
                        SOURCE_CRUX,
                        observed_on,
                        scope=row["device"],
                        key=row["url"],
                        metric=row["metric"],
                        value=row["p75"],
                        accuracy=_CWV_ACCURACY,
                    )

        sources: dict[str, Any] = {}
        if source_name in {"crux", "both"}:
            sources[SOURCE_CRUX] = {
                "calls": crux_calls,
                "rows": len(field_rows),
                "no_field_data": crux_empty,
                "persisted": len(field_rows),
            }
        if source_name in {"psi", "both"}:
            sources[SOURCE_PSI] = {
                "calls": psi_calls,
                "rows": len(lab_rows),
                "persisted": 0,
            }

        data = {
            "date": observed_on.isoformat(),
            "property_id": property_id,
            "rows": field_rows,
            "lab": lab_rows,
            "skipped_urls": skipped,
            "written": {"crux": len(field_rows), "psi": 0},
            "sources": sources,
        }
        meta = {
            "source": source_name,
            "devices": devices,
            "date": observed_on.isoformat(),
            "urls_total": len(targets),
            "urls_selected": len(selected),
            "total": total,
            "processed": processed,
            "pairs_total": pairs_total,
            "pairs_processed": pairs_processed,
            "calls": crux_calls + psi_calls,
            "truncated": bool(skipped),
            "data_age_days": max(data_ages, default=0),
            "written": len(field_rows),
            "store": self._store is not None,
            "limitations": _unique(limitations),
        }
        return ok(data, meta=meta, warnings=_unique(warnings))

    def crawl_and_store(
        self,
        *,
        start_url: str | None = None,
        max_urls: int | None = None,
    ) -> dict[str, Any]:
        """Crawl one site within ``max_urls`` and persist pages and rule violations."""
        warnings: list[str] = []
        return self._respond(
            "crawl_and_store",
            warnings,
            lambda: self._crawl_and_store(start_url, max_urls, warnings),
        )

    def _crawl_and_store(
        self,
        start_url: str | None,
        max_urls: int | None,
        warnings: list[str],
    ) -> dict[str, Any]:
        resolved_start = _start_url(start_url, self._config)
        limit = (
            self._config.crawl.max_urls
            if max_urls is None
            else _positive_int(max_urls, name="max_urls")
        )
        started_at = utc_now()

        fetcher = Fetcher(user_agent=self._config.crawl.user_agent)
        try:
            outcome = self._walk_site(fetcher, resolved_start, limit)
        finally:
            close = getattr(fetcher, "close", None)
            if callable(close):
                close()
        finished_at = utc_now()

        property_id = self._crawl_property_id(resolved_start)
        stored_pages, stored_issues = self._persist_crawl(
            outcome, property_id, started_at, finished_at, warnings
        )

        issues = [
            {
                "rule_id": issue.rule_id,
                "severity": issue.severity,
                "url": issue.url,
                "detail": issue.detail,
            }
            for issue in outcome.issues
        ]
        fetched = sum(1 for page in outcome.pages if page.status != 0 and page.error is None)
        summary = {
            "start_url": outcome.start_url,
            "site_url": outcome.site_url,
            "urls": len(outcome.pages),
            "fetched": fetched,
            "errors": len(outcome.errors),
            "blocked": len(outcome.blocked),
            "issues": len(outcome.issues),
            "truncated": outcome.truncated,
            "sitemap_urls": len(outcome.sitemap_urls),
            "sitemap_truncated": outcome.sitemap_truncated,
            "request_count": outcome.request_count,
            "request_budget": outcome.request_budget,
        }
        graph = {
            "nodes": outcome.graph.nodes,
            "edges": outcome.graph.edges,
            "max_depth": outcome.graph.max_depth,
            "orphans": outcome.graph.orphans,
            "broken": outcome.graph.broken,
            "unvisited": outcome.graph.unvisited,
            "failed": outcome.graph.failed,
        }
        coverage = sitemap_crawl_diff(
            outcome.pages,
            outcome.sitemap_urls,
            crawl_truncated=outcome.truncated,
            sitemap_truncated=outcome.sitemap_truncated,
            sitemap_errors=outcome.sitemap_errors,
        )
        data = {
            "summary": summary,
            "issues": issues,
            "graph": graph,
            "errors": outcome.errors,
            "blocked": outcome.blocked,
            "sitemap_coverage": coverage.to_dict(),
        }

        if outcome.errors:
            warnings.append(f"crawl: {len(outcome.errors)} urls could not be fetched")
        if outcome.blocked:
            warnings.append(f"crawl: {len(outcome.blocked)} urls blocked by robots.txt")
        if outcome.robots.deferred:
            warnings.append("crawl: robots.txt unreachable; crawl deferred (no fail-open)")
        if outcome.sitemap_errors:
            warnings.append(f"crawl: {len(outcome.sitemap_errors)} sitemap fetch errors")
        if outcome.sitemap_truncated:
            warnings.append("crawl: sitemap inventory truncated by budget")
        for note in outcome.coverage_notes:
            warnings.append(f"crawl coverage: {note}")

        meta = {
            "property_id": property_id,
            "max_urls": limit,
            "truncated": outcome.truncated,
            "pages_stored": stored_pages,
            "issues_stored": stored_issues,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
        }
        return ok(data, meta=meta, warnings=_unique(warnings))

    def _walk_site(self, fetcher: Fetcher, start_url: str, max_urls: int) -> _CrawlOutcome:
        parts = urlsplit(start_url)
        site_url = f"{parts.scheme}://{parts.netloc}"
        host = (parts.hostname or "").lower()
        respect_robots = self._config.crawl.respect_robots
        fetch_robots = getattr(fetcher, "fetch_robots", None)
        robots = (
            fetch_robots(site_url)
            if respect_robots and callable(fetch_robots)
            else RobotsPolicy(disallow=[], allow=[], sitemaps=[])
        )

        queue: deque[str] = deque([start_url])
        discovered = {normalize_url(start_url)}
        pages: list[PageData] = []
        errors: list[str] = []
        blocked: list[str] = []
        truncated = False
        coverage_notes: list[str] = []
        downgrades = 0
        sitemap_urls: list[str] = []
        sitemap_truncated = False
        sitemap_errors: list[str] = []

        def allow_url(target: str) -> bool:
            target_parts = urlsplit(target)
            if (target_parts.scheme.lower(), target_parts.netloc.lower()) != (
                parts.scheme.lower(),
                parts.netloc.lower(),
            ):
                coverage_notes.append("off-origin URL excluded from crawl scope")
                return False
            return not respect_robots or is_allowed(robots, _request_path(target))

        supports_allow_url = "allow_url" in inspect.signature(fetcher.fetch).parameters

        max_redirects = getattr(fetcher, "max_redirects", 5)
        if not isinstance(max_redirects, int) or max_redirects < 0:
            max_redirects = 5
        request_budget = 1 + max_urls * (max_redirects + 1)

        def _request_count() -> int:
            count = getattr(fetcher, "request_count", 0)
            return count if isinstance(count, int) else 0

        if robots.deferred:
            blocked.append(f"{start_url}: robots_unreachable_deferred")
            coverage_notes.append("robots.txt unreachable (5xx/network): crawl deferred")
        else:
            if respect_robots and robots.crawl_delay:
                try:
                    fetcher.politeness_delay = robots.crawl_delay
                except AttributeError:
                    coverage_notes.append("crawl-delay ignored: fetcher has no pacing hook")
            sitemap_urls, sitemap_truncated, sitemap_errors = self._discover_sitemap_urls(
                fetcher, site_url, host, robots, max_urls, discovered, queue, coverage_notes
            )

            while queue:
                if _request_count() >= request_budget:
                    truncated = True
                    coverage_notes.append(
                        f"request budget exhausted ({_request_count()}/{request_budget})"
                    )
                    break
                url = queue.popleft()
                path = _request_path(url)
                if respect_robots and not is_allowed(robots, path):
                    blocked.append(url)
                    pages.append(_blocked_page(url))
                    continue

                if supports_allow_url:
                    result = fetcher.fetch(url, allow_url=allow_url)
                else:
                    result = fetcher.fetch(url)
                if _request_count() > request_budget:
                    truncated = True
                    coverage_notes.append(
                        f"request budget exhausted ({_request_count()}/{request_budget})"
                    )
                    break
                page = _page_from_fetch(url, result)
                if page.error == "robots_blocked":
                    blocked.append(url)
                    pages.append(page)
                    continue
                if page.error in ("blocked_private",):
                    errors.append(f"{url}: {page.error}")
                    pages.append(page)
                    continue
                if page.error is None and page.status == 200 and respect_robots:
                    # The seed check ran before the fetch; a redirect may have
                    # landed on a disallowed path — re-check the final address.
                    final_path = _request_path(page.final_url or url)
                    if not is_allowed(robots, final_path):
                        blocked.append(url)
                        pages.append(_blocked_page(url))
                        continue
                pages.append(page)
                if page.downgraded:
                    downgrades += 1
                if result.error:
                    errors.append(f"{url}: {result.error}")
                    continue
                if not 200 <= page.status < 300:
                    continue

                for link in page.links:
                    if _host_of(link) != host:
                        continue
                    key = normalize_url(link)
                    if key in discovered:
                        continue
                    if len(discovered) >= max_urls:
                        truncated = True
                        continue
                    discovered.add(key)
                    queue.append(link)

        graph = build_graph(pages, site_host=host, root_url=start_url)
        issues = run_all(
            RuleContext(
                pages=pages,
                site_url=site_url,
                sitemap_urls=list(sitemap_urls),
                robots_sitemaps=list(robots.sitemaps),
                robots_disallow=list(robots.disallow),
                robots_allow=list(robots.allow),
            )
        )
        outcome = _CrawlOutcome(
            start_url=start_url,
            site_url=site_url,
            pages=pages,
            issues=issues,
            errors=errors,
            blocked=blocked,
            truncated=truncated,
            graph=graph,
            robots=robots,
        )
        outcome.sitemap_urls = list(sitemap_urls)
        outcome.sitemap_truncated = sitemap_truncated
        outcome.sitemap_errors = list(sitemap_errors)
        outcome.request_count = _request_count()
        outcome.request_budget = request_budget
        if downgrades:
            coverage_notes.append(f"{downgrades} https downgrade(s) observed")
        outcome.coverage_notes = _unique(coverage_notes)
        return outcome

    def _discover_sitemap_urls(
        self,
        fetcher: Fetcher,
        site_url: str,
        host: str,
        robots: RobotsPolicy,
        max_urls: int,
        discovered: set[str],
        queue: deque[str],
        notes: list[str],
    ) -> tuple[list[str], bool, list[str]]:
        """Seed the frontier with sitemap inventory (bounded; failures are notes)."""
        candidates = list(robots.sitemaps)
        standard = f"{site_url}/sitemap.xml"
        if standard not in candidates:
            candidates.append(standard)
        site_parts = urlsplit(site_url)
        site_origin = (site_parts.scheme.lower(), site_parts.netloc.lower())
        supports_allow_url = "allow_url" in inspect.signature(fetcher.fetch).parameters
        supports_max_bytes = "max_bytes" in inspect.signature(fetcher.fetch).parameters

        def _sitemap_in_scope(target: str) -> bool:
            target_parts = urlsplit(target)
            return (target_parts.scheme.lower(), target_parts.netloc.lower()) == site_origin

        def _scoped_fetch(url: str) -> FetchResult:
            if _host_of(url) != host or not _sitemap_in_scope(url):
                return FetchResult(url=url, status=0, final_url=url, error="robots_blocked")
            kwargs: dict[str, Any] = {}
            if supports_allow_url:
                kwargs["allow_url"] = _sitemap_in_scope
            if supports_max_bytes:
                kwargs["max_bytes"] = SITEMAP_MAX_BYTES
            return fetcher.fetch(url, **kwargs)

        reader = SitemapReader(
            _scoped_fetch,
            allow_url=_sitemap_in_scope,
            max_urls=max_urls,
            max_depth=2,
            max_docs=20,
        )
        collected: list[str] = []
        truncated = False
        reader_errors: list[str] = []
        for candidate in candidates:
            if _host_of(candidate) != host or not _sitemap_in_scope(candidate):
                notes.append(f"off-origin sitemap skipped: {candidate}")
                continue
            try:
                doc = reader.read(candidate)
            except Exception as exc:  # the reader is total, but the fetch may not be
                reader_errors.append(f"{candidate}: {type(exc).__name__}")
                continue
            for error in doc.errors:
                # A missing standard sitemap is normal (no sitemap published),
                # not a fetch failure worth warning about.
                if candidate == standard and error == "http_404":
                    continue
                if error == "robots_blocked":
                    notes.append(f"off-origin sitemap target excluded: {candidate}")
                    continue
                if error == OFF_ORIGIN_LOC:
                    # A <loc> outside the site is a scope note, not a fetch failure.
                    notes.append(f"off-origin sitemap loc excluded: {candidate}")
                    continue
                reader_errors.append(f"{candidate}: {error}")
            truncated = truncated or doc.truncated
            for url in doc.urls:
                if _host_of(url) != host:
                    continue
                if url not in collected:
                    collected.append(url)
            if len(collected) >= max_urls:
                truncated = True
                break
        added = 0
        for url in collected:
            key = normalize_url(url)
            if key in discovered:
                continue
            if len(discovered) >= max_urls:
                truncated = True
                break
            discovered.add(key)
            queue.append(url)
            added += 1
        if truncated:
            notes.append("sitemap inventory truncated")
        if reader_errors:
            notes.append(f"sitemap errors: {len(reader_errors)}")
        if added:
            notes.append(f"sitemap-only seeds: {added}")
        return collected, truncated, reader_errors

    def _persist_crawl(
        self,
        outcome: _CrawlOutcome,
        property_id: str,
        started_at: datetime,
        finished_at: datetime,
        warnings: list[str],
    ) -> tuple[int, int]:
        store = self._store
        if store is None:
            warnings.append("store: not configured; crawl results were not persisted")
            return 0, 0

        with store.atomic():
            for page in outcome.pages:
                store.upsert_page(
                    property_id,
                    page.url,
                    status=page.status,
                    index_state=_index_state(page),
                    canonical=page.canonical,
                    title_hash=_title_hash(page.title),
                )

            for issue in outcome.issues:
                store.upsert_issue(
                    issue.rule_id,
                    issue.severity,
                    property_id,
                    issue.url,
                    issue.detail,
                )

            status = "partial" if outcome.errors else "ok"
            store.record_run(
                "crawl",
                property_id,
                status,
                started_at=started_at,
                finished_at=finished_at,
                stats={
                    "urls": len(outcome.pages),
                    "errors": len(outcome.errors),
                    "blocked": len(outcome.blocked),
                    "issues": len(outcome.issues),
                    "truncated": outcome.truncated,
                },
            )
        return len(outcome.pages), len(outcome.issues)

    # -- reports -----------------------------------------------------------------------------

    def report_diff(
        self,
        *,
        base: str | None = None,
        head: str | None = None,
        kind: str = "weekly",
    ) -> dict[str, Any]:
        """Diff two stored report snapshots (default: the two newest periods). Read-only."""
        warnings: list[str] = []
        return self._respond(
            "report_diff",
            warnings,
            lambda: self._report_diff(base, head, kind, warnings),
        )

    def _report_diff(
        self, base: str | None, head: str | None, kind: str, warnings: list[str]
    ) -> dict[str, Any]:
        from lv24_seo.report.diff import diff_snapshots, render_diff_markdown

        store = self._store
        if store is None:
            raise SeoError(
                code=CONFIG_INVALID,
                message="report diff needs stored snapshots, but no store is configured",
                next_action="run `lv24-seo init`, then `lv24-seo report weekly` on two runs",
            )
        property_id = _report_property_id(self._config)
        periods = store.list_snapshot_periods(kind, property_id, limit=50)
        head_period = head or (periods[0] if periods else None)
        base_period = base or next((p for p in periods if p != head_period), None)
        if head_period is None or base_period is None:
            raise SeoError(
                code=INVALID_INPUT,
                message=(
                    f"need two stored {kind} snapshots for {property_id}; found {len(periods)}"
                ),
                next_action="run `lv24-seo report weekly` for another period or pass --base/--head",
            )
        snapshots: dict[str, Mapping[str, Any]] = {}
        for label, period in (("base", base_period), ("head", head_period)):
            loaded = store.load_snapshot(kind, property_id, period)
            if loaded is None:
                raise SeoError(
                    code=INVALID_INPUT,
                    message=f"no stored {kind} snapshot for {label} period {period}",
                    next_action="list stored periods with `lv24-seo report diff --json`",
                )
            snapshots[label] = loaded
        diff = diff_snapshots(snapshots["base"], snapshots["head"])
        diff["markdown"] = render_diff_markdown(diff)
        meta = {
            "property_id": property_id,
            "kind": kind,
            "base_period": base_period,
            "head_period": head_period,
            "available_periods": periods,
            "complete": bool(diff["comparable"]),
            "truncated": False,
            "limitations": list(diff["limitations"]),
        }
        return ok(diff, meta=meta, warnings=warnings)

    def weekly_report(
        self,
        *,
        days: int = 7,
        lag_days: int = 2,
        send: bool = False,
    ) -> dict[str, Any]:
        """Build the weekly digest, save a snapshot and optionally deliver it."""
        warnings: list[str] = []
        return self._respond(
            "weekly_report",
            warnings,
            lambda: self._weekly_report(days, lag_days, send, warnings),
        )

    def _weekly_report(
        self,
        days: int,
        lag_days: int,
        send: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        span = _positive_int(days, name="days", maximum=MAX_REPORT_DAYS)
        lag = _non_negative_int(lag_days, name="lag_days")
        end = local_today(self._config.report.timezone)
        (start, current_end), (previous_start, previous_end) = compare_period(
            span, end=end, lag_days=lag
        )

        current: dict[str, dict[str, float]] = {}
        previous: dict[str, dict[str, float]] = {}
        sources: dict[str, dict[str, Any]] = {}
        limitations: list[str] = []
        provider_warnings: list[str] = []
        sources_failed: list[str] = []
        gsc_query_current: list[dict[str, Any]] = []
        gsc_query_previous: list[dict[str, Any]] = []

        self._isolated_collect(
            "gsc",
            warnings,
            sources_failed,
            lambda: self._collect_gsc_weekly(
                start,
                current_end,
                previous_start,
                previous_end,
                current,
                previous,
                sources,
                gsc_query_current,
                gsc_query_previous,
                warnings,
            ),
        )
        self._isolated_collect(
            SOURCE_YANDEX_WEBMASTER,
            warnings,
            sources_failed,
            lambda: self._collect_yandex_weekly(start, current_end, current, sources, warnings),
        )
        self._isolated_collect(
            SOURCE_YANDEX_METRIKA,
            warnings,
            sources_failed,
            lambda: self._collect_metrika_weekly(
                start,
                current_end,
                previous_start,
                previous_end,
                current,
                previous,
                sources,
                warnings,
            ),
        )
        self._isolated_collect(
            SOURCE_BING,
            warnings,
            sources_failed,
            lambda: self._collect_bing_weekly(sources, warnings),
        )
        self._isolated_collect(
            SOURCE_CRUX,
            warnings,
            sources_failed,
            lambda: self._collect_crux_weekly(sources, warnings),
        )
        self._isolated_collect(
            SOURCE_PSI,
            warnings,
            sources_failed,
            lambda: self._collect_psi_weekly(sources, warnings),
        )

        for source in sources.values():
            source_meta = source.get("meta", {})
            if isinstance(source_meta, Mapping):
                limitations.extend(_list_of_str(source_meta.get("limitations")))
                provider_warnings.extend(_list_of_str(source_meta.get("warnings")))

        requested = {
            name
            for name, block, _, target, _ in self._provider_specs()
            if block is not None and target.strip()
        } | set(self._overrides)
        missing = sorted(requested - sources.keys() - set(sources_failed))
        if missing:
            limitations.append("configured sources unavailable: " + ", ".join(missing))
        if sources_failed:
            limitations.append(
                "source collection failed: " + ", ".join(sorted(set(sources_failed)))
            )
        if not sources or not any(source.get("rows") for source in sources.values()):
            limitations.append("no source returned usable rows; report is a no-data fallback")

        # The headline compares one engine across two identical windows: GSC.
        # Yandex exposes a windowed top-queries report without a previous window
        # for a delta, Metrika counts
        # sessions (not clicks) — merging any of them into the delta would
        # fabricate growth (F01). Per-engine numbers stay visible in the
        # traffic section; the headline never claims a cross-engine total.
        headline_current = {SOURCE_GSC: current.get(SOURCE_GSC, {})}
        headline_previous = {SOURCE_GSC: previous.get(SOURCE_GSC, {})}
        gsc_complete = (
            SOURCE_GSC in sources
            and SOURCE_GSC not in sources_failed
            and bool(current.get(SOURCE_GSC))
            and bool(previous.get(SOURCE_GSC))
            and bool(sources[SOURCE_GSC].get("comparable"))
        )
        if not gsc_complete:
            # Keep the current totals: they are real even when no comparable
            # previous window exists, so the report shows values with
            # ``previous=None`` instead of an empty KPI card. A previous window
            # is only used when it is comparable; otherwise the builder would
            # invent a baseline and a false verdict.
            headline_previous = {}
            limitations.append(
                "headline delta unavailable: no comparable GSC windows; "
                "per-engine numbers are shown without a total"
            )
        else:
            limitations.append(
                "headline delta covers GSC windows only; yandex query window and "
                "metrika sessions are shown per-engine, never merged"
            )
        limitations = _unique(limitations)

        # Search Analytics omits anonymized/top rows even below the requested cap.
        # No successful query/page response proves that an absent row had zero traffic.
        query_complete = False
        if gsc_query_current or gsc_query_previous:
            limitations.append(
                "gsc query/page coverage is not exhaustive: vanished rows are unknown, not zero"
            )
            limitations = _unique(limitations)

        if self._store is None:
            warnings.append("store: not configured; snapshot was not saved")
        all_warnings = _unique([*warnings, *provider_warnings])
        property_id = _report_property_id(self._config)
        period = f"{start.isoformat()}..{current_end.isoformat()}"

        stored_issues = self._stored_issue_rows(property_id)
        # The digest is opt-in: without a comparable GSC window there is nothing to
        # attribute, and with no stored evidence the section stays absent instead of
        # claiming a cause. Indexing events are bounded to the compared windows: an
        # event outside them cannot explain this period's change.
        digest_evidence = (
            self._digest_evidence(
                property_id,
                limitations,
                start=previous_start,
                end=current_end,
            )
            if gsc_complete
            else None
        )
        cwv_rows = self._stored_cwv_rows(
            property_id,
            start=previous_start,
            # A CWV observation is a snapshot fetched on its own date, not a daily
            # traffic count. Read through today so the row written by `cwv collect`
            # is visible in the same run even though the report window itself is
            # provider-lagged.
            end=end,
            limitations=limitations,
        )

        # Current GSC totals are real even without a comparable previous window;
        # only the previous side is gated on comparability. The fallback gets the
        # same rule so it cannot compare a truncated/missing window either.
        gsc_current_totals = (
            _flatten_totals(headline_current) if headline_current.get(SOURCE_GSC) else {}
        )
        gsc_previous_totals = (
            _flatten_totals(headline_previous)
            if gsc_complete and headline_previous.get(SOURCE_GSC)
            else {}
        )
        fallback_previous = dict(previous)
        if not gsc_complete:
            fallback_previous.pop(SOURCE_GSC, None)

        builder = _load_weekly_builder()
        report_obj: Report
        if builder is not None and not _weekly_builder_compatible(builder):
            warnings.append(_BUILDERS_SIGNATURE_MISMATCH)
            all_warnings = _unique([*all_warnings, _BUILDERS_SIGNATURE_MISMATCH])
            report_obj = self._fallback_weekly_report(
                property_id=property_id,
                start=start,
                end=current_end,
                current=current,
                previous=fallback_previous,
                limitations=limitations,
                warnings=all_warnings,
            )
        elif builder is None:
            warnings.append(_BUILDERS_UNAVAILABLE)
            all_warnings = _unique([*all_warnings, _BUILDERS_UNAVAILABLE])
            report_obj = self._fallback_weekly_report(
                property_id=property_id,
                start=start,
                end=current_end,
                current=current,
                previous=fallback_previous,
                limitations=limitations,
                warnings=all_warnings,
            )
        else:
            digest_kwargs: dict[str, Any] = {}
            if digest_evidence is not None and _weekly_builder_accepts(builder, "digest_evidence"):
                digest_kwargs["digest_evidence"] = digest_evidence
            if cwv_rows and _weekly_builder_accepts(builder, "cwv_rows"):
                digest_kwargs["cwv_rows"] = cwv_rows
                if _weekly_builder_accepts(builder, "cwv_limit"):
                    digest_kwargs["cwv_limit"] = _WEEKLY_CWV_LIMIT
            report_obj = builder(
                property_id=property_id,
                period_start=start,
                period_end=current_end,
                current=gsc_current_totals,
                previous=gsc_previous_totals,
                gsc_rows=gsc_query_current,
                gsc_previous_rows=gsc_query_previous,
                gsc_previous_complete=query_complete,
                yandex_rows=_source_rows(sources, SOURCE_YANDEX_WEBMASTER),
                metrika_rows=_source_rows(sources, SOURCE_YANDEX_METRIKA),
                issues=stored_issues,
                extra_engines=_extra_engine_rows(sources),
                limitations=limitations,
                thresholds=DeltaThresholds(
                    clicks_pct=self._config.report.thresholds.clicks_pct,
                    impressions_pct=self._config.report.thresholds.impressions_pct,
                    position_abs=self._config.report.thresholds.position_abs,
                ),
                **digest_kwargs,
            )
        payload = _report_payload(report_obj)

        report_obj.warnings = _unique([*report_obj.warnings, *all_warnings])
        report_obj.limitations = _unique([*report_obj.limitations, *limitations])
        payload["warnings"] = list(report_obj.warnings)
        payload["limitations"] = list(report_obj.limitations)

        snapshot_hash: str | None = None
        if self._store is not None:
            snapshot_hash = self._store.save_snapshot("weekly", property_id, period, payload)

        sent = False
        if send:
            sent = self._deliver_report(report_obj, warnings)

        data_age = max(
            (
                _int_of(source["meta"].get("data_age_days"))
                for source in sources.values()
                if isinstance(source.get("meta"), Mapping)
            ),
            default=0,
        )
        failed = sorted(set(sources_failed))
        incomplete = sorted(
            name
            for name, source in sources.items()
            if not source.get("rows")
            or source.get("meta", {}).get("truncated")
            or (name == SOURCE_GSC and not gsc_complete)
        )
        complete = bool(sources) and not (failed or missing or incomplete)
        meta: dict[str, Any] = {
            "sent": sent,
            "property_id": property_id,
            "period": {"start": start.isoformat(), "end": current_end.isoformat()},
            "previous_period": {
                "start": previous_start.isoformat(),
                "end": previous_end.isoformat(),
            },
            "sources": sorted(sources),
            "sources_failed": failed,
            "sources_missing": missing,
            "sources_incomplete": incomplete,
            "complete": complete,
            "data_age_days": data_age,
            "locale": self._config.report.locale,
            "truncated": any(
                bool(source["meta"].get("truncated"))
                for source in sources.values()
                if isinstance(source.get("meta"), Mapping)
            ),
            "limitations": limitations,
        }
        if snapshot_hash is not None:
            meta["snapshot_hash"] = snapshot_hash
        return ok(payload, meta=meta, warnings=_unique(warnings))

    def _stored_issue_rows(self, property_id: str) -> list[dict[str, Any]]:
        """Latest open crawl issues for the report, or [] without a store."""
        if self._store is None:
            return []
        try:
            issues = self._store.list_issues(property_id, limit=200)
        except Exception:  # storage must never cancel a report
            logger.warning("weekly: could not read stored issues")
            return []
        rows: list[dict[str, Any]] = []
        for issue in issues:
            rows.append(
                {
                    "rule_id": issue.get("rule_id"),
                    "severity": issue.get("severity"),
                    "url": issue.get("url"),
                    "detail": issue.get("detail", {}),
                }
            )
        return rows

    def _stored_cwv_rows(
        self,
        property_id: str,
        *,
        start: date,
        end: date,
        limitations: list[str],
    ) -> list[dict[str, Any]]:
        """Latest stored CrUX/PSI field row per URL, device and metric.

        The store keeps provider metrics in ``metric_daily``/``metric_windows`` with a
        ``scope`` and a ``key``. A CWV producer writes the device as ``scope`` and the page
        URL as ``key``; only rows carrying both are returned, because an origin aggregate or
        an unknown scope cannot be assigned to a template without guessing. The newest
        observation wins per ``(url, device, metric)``: feeding every day would turn the
        template p75 into a worst-of-the-window value instead of the current one. ``end`` is
        the caller's today, not the lagged report window: a field snapshot fetched today is
        the current value even when the traffic windows end two days earlier. No row means
        no section, never an empty or invented baseline.
        """
        store = self._store
        if store is None:
            return []
        try:
            daily = store.get_metric_daily(property_id, source=SOURCE_CRUX, start=start, end=end)
            daily += store.get_metric_daily(property_id, source=SOURCE_PSI, start=start, end=end)
            windows = store.get_metric_windows(
                property_id, source=SOURCE_CRUX, start=start, end=end
            )
            windows += store.get_metric_windows(
                property_id, source=SOURCE_PSI, start=start, end=end
            )
        except Exception:  # storage must never cancel a report
            logger.warning("weekly: could not read stored cwv rows")
            limitations.append("cwv: stored metrics could not be read")
            return []

        latest: dict[tuple[str, str, str], tuple[date, dict[str, Any]]] = {}
        for row in [*daily, *windows]:
            url = row.get("key")
            device = str(row.get("scope") or "").strip().casefold()
            metric = row.get("metric")
            if not isinstance(url, str) or not url.strip():
                continue
            if device not in CWV_DEVICES:
                continue
            if not isinstance(metric, str) or not metric.strip():
                continue
            observed = _stored_day(row.get("date") or row.get("end"))
            if observed is None:
                continue
            candidate_url = url.strip()
            candidate_metric = metric.strip()
            candidate: dict[str, Any] = {
                "url": candidate_url,
                "device": device,
                "metric": candidate_metric,
                "p75": row.get("value"),
                "date": observed.isoformat(),
            }
            identity = (candidate_url, device, candidate_metric)
            previous = latest.get(identity)
            if previous is None or observed >= previous[0]:
                latest[identity] = (observed, candidate)

        rows = [candidate for _, candidate in latest.values()]
        rows.sort(key=lambda item: (item["url"], item["device"], item["metric"]))
        if len(rows) > _CWV_ROW_LIMIT:
            limitations.append(
                f"cwv: stored metric rows capped at {_CWV_ROW_LIMIT}; a template may be incomplete"
            )
            rows = rows[:_CWV_ROW_LIMIT]
        return rows

    def _digest_evidence(
        self,
        property_id: str,
        limitations: list[str],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, list[dict[str, Any]]] | None:
        """Stored evidence for the attributed change digest, or ``None`` when there is none.

        The store keeps one row per issue with ``first_seen``/``last_seen`` and the triage
        ``state`` rather than a per-run issue snapshot. That is enough to say what is new
        since the previous window (``first_seen`` inside it) and what was triaged as fixed;
        ``analysis.digest`` applies those rules, so the rows are passed without a guessed
        ``change``. Indexing events are read only inside ``[start, end]`` -- the compared
        windows -- because an event outside them cannot explain this period's change; the
        digest counts a row without a URL or event name as malformed instead of guessing.
        A missing store, a read failure or an empty result keeps the digest section out of
        the report entirely.
        """
        if self._store is None:
            return None
        try:
            issues = self._store.list_issues(property_id, state=None, limit=_DIGEST_EVIDENCE_LIMIT)
            pages = self._store.list_pages(property_id, limit=_DIGEST_EVIDENCE_LIMIT)
            events = self._store.list_indexing_events(
                property_id=property_id,
                start=start,
                end=end,
                limit=_DIGEST_EVIDENCE_LIMIT,
            )
            event_total = (
                self._store.count_indexing_events(property_id=property_id, start=start, end=end)
                if len(events) >= _DIGEST_EVIDENCE_LIMIT
                else len(events)
            )
        except Exception:  # storage must never cancel a report
            logger.warning("weekly: could not read digest evidence")
            return None
        if (
            len(issues) >= _DIGEST_EVIDENCE_LIMIT
            or len(pages) >= _DIGEST_EVIDENCE_LIMIT
            or event_total > len(events)
        ):
            limitations.append(
                f"digest evidence capped at {_DIGEST_EVIDENCE_LIMIT} issues, "
                f"{_DIGEST_EVIDENCE_LIMIT} pages and {_DIGEST_EVIDENCE_LIMIT} indexing events; "
                "an unexplained change may be missing"
            )
        issue_changes = [
            {
                "rule_id": issue.get("rule_id"),
                "severity": issue.get("severity"),
                "url": issue.get("url"),
                "state": issue.get("state"),
                "first_seen": issue.get("first_seen"),
                "last_seen": issue.get("last_seen"),
            }
            for issue in issues
        ]
        page_rows = [
            {
                "url": page.get("url"),
                "status": page.get("status"),
                "index_state": page.get("index_state"),
                "canonical": page.get("canonical"),
                "last_seen": page.get("last_seen"),
            }
            for page in pages
        ]
        event_rows = [
            {
                "url": event.get("url"),
                "event": event.get("event"),
                "event_date": event.get("event_date"),
            }
            for event in events
        ]
        evidence: dict[str, list[dict[str, Any]]] = {
            "issue_changes": issue_changes,
            "pages": page_rows,
        }
        if event_rows:
            # Absent events stay absent: the digest reports the gap instead of an empty list
            # that a reader could mistake for "checked, nothing happened".
            evidence["indexing_events"] = event_rows
        if not issue_changes and not page_rows and not event_rows:
            return None
        return evidence

    def _deliver_report(self, report: Report, warnings: list[str]) -> bool:
        """Send a built report through the configured channels (best-effort)."""
        try:
            from lv24_seo.report.delivery import EmailConfig, TelegramConfig, deliver
        except ImportError:  # pragma: no cover - delivery ships with the same wheel
            warnings.append(_DELIVERY_UNAVAILABLE)
            return False

        cfg = self._config.report
        email = None
        if cfg.email.enabled and cfg.email.to and cfg.email.smtp_host:
            email = EmailConfig(
                host=cfg.email.smtp_host,
                port=cfg.email.smtp_port,
                user=cfg.email.smtp_user,
                password=self._secrets.get("smtp_password") or "",
                sender=cfg.email.email_from,
                recipients=tuple(cfg.email.to),
                locale=cfg.locale,
            )
        telegram = None
        if cfg.telegram.enabled and cfg.telegram.chat_id:
            telegram = TelegramConfig(
                bot_token=self._secrets.get("telegram_bot_token") or "",
                chat_id=cfg.telegram.chat_id,
                locale=cfg.locale,
            )
        if email is None and telegram is None:
            warnings.append("delivery: no channel configured")
            return False

        results = deliver(
            report,
            email=email,
            telegram=telegram,
            store=self._store,
            only_on_change=cfg.email.only_on_change,
            material_thresholds={
                "clicks_pct": cfg.thresholds.clicks_pct,
                "impressions_pct": cfg.thresholds.impressions_pct,
                "position_abs": cfg.thresholds.position_abs,
            },
            locale=cfg.locale,
        )
        for result in results:
            if result.status == "failed":
                warnings.append(f"delivery {result.channel}: {result.reason}")
        return any(result.status == "sent" for result in results)

    @staticmethod
    def _isolated_collect(
        name: str,
        warnings: list[str],
        sources_failed: list[str],
        action: Callable[[], None],
    ) -> None:
        """Run one source collector; a provider failure isolates to that source.

        A 401/403/429/timeout in one engine must not cancel the whole weekly
        digest: the failure is recorded in ``sources_failed`` with a warning and
        the available sources still produce a report. Only when *no* source
        yields usable rows does the report degrade to a no-data fallback.
        """
        try:
            action()
        except SeoError as exc:
            sources_failed.append(name)
            warnings.append(f"{name}: {exc.code}: {exc.message}")

    def _collect_gsc_weekly(
        self,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
        current: dict[str, dict[str, float]],
        previous: dict[str, dict[str, float]],
        sources: dict[str, dict[str, Any]],
        gsc_query_current: list[dict[str, Any]],
        gsc_query_previous: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        client = self._maybe_client(SOURCE_GSC)
        if client is None:
            warnings.append("gsc: token missing")
            return
        if SOURCE_GSC not in self._overrides and not _gsc_configured_property(self._config):
            warnings.append("gsc: property missing")
            return

        # Dataset 1 (date dimension): comparable window totals for the headline.
        current_result = _call(
            client,
            "search_analytics",
            start_date=start,
            end_date=end,
            dimensions=("date",),
            row_limit=_WEEKLY_GSC_LIMIT,
            max_rows=_WEEKLY_GSC_LIMIT,
        )
        previous_result = _call(
            client,
            "search_analytics",
            start_date=previous_start,
            end_date=previous_end,
            dimensions=("date",),
            row_limit=_WEEKLY_GSC_LIMIT,
            max_rows=_WEEKLY_GSC_LIMIT,
        )
        current_rows = _result_rows(current_result)
        previous_rows = _result_rows(previous_result)
        current[SOURCE_GSC] = _gsc_totals(current_rows) if current_rows else {}
        previous[SOURCE_GSC] = _gsc_totals(previous_rows) if previous_rows else {}
        sources[SOURCE_GSC] = _source_entry(current_result, current_rows)
        sources[SOURCE_GSC]["comparable"] = bool(
            current_rows
            and previous_rows
            and not _result_truncated(current_result)
            and not _result_truncated(previous_result)
        )
        sources[SOURCE_GSC]["meta"] = _combined_result_meta(
            (current_result, previous_result), source=SOURCE_GSC
        )

        # Dataset 2 (query+page dimensions): per-URL/query rows for opportunities
        # and fusion. A date-dimensioned row carries no query/page and would make
        # find_opportunities blind, so it must never feed that path (F25).
        query_current_result = _call(
            client,
            "search_analytics",
            start_date=start,
            end_date=end,
            dimensions=("query", "page"),
            row_limit=_WEEKLY_GSC_LIMIT,
            max_rows=_WEEKLY_GSC_LIMIT,
        )
        query_previous_result = _call(
            client,
            "search_analytics",
            start_date=previous_start,
            end_date=previous_end,
            dimensions=("query", "page"),
            row_limit=_WEEKLY_GSC_LIMIT,
            max_rows=_WEEKLY_GSC_LIMIT,
        )
        gsc_query_current.extend(_result_rows(query_current_result))
        gsc_query_previous.extend(_result_rows(query_previous_result))
        warnings.extend(_result_warnings(query_current_result))
        warnings.extend(_result_warnings(query_previous_result))
        query_truncated = bool(
            getattr(query_current_result, "truncated", False)
            or getattr(query_previous_result, "truncated", False)
        )
        sources[SOURCE_GSC]["query_rows"] = len(gsc_query_current)
        sources[SOURCE_GSC]["query_truncated"] = query_truncated
        sources[SOURCE_GSC]["meta"] = _combined_result_meta(
            (current_result, previous_result, query_current_result, query_previous_result),
            source=SOURCE_GSC,
        )

    def _collect_yandex_weekly(
        self,
        start: date,
        end: date,
        current: dict[str, dict[str, float]],
        sources: dict[str, dict[str, Any]],
        warnings: list[str],
    ) -> None:
        client = self._maybe_client(SOURCE_YANDEX_WEBMASTER)
        if client is None:
            warnings.append("yandex_webmaster: token missing")
            return
        host_id = self._yandex_host_id(required=False)
        if not host_id:
            warnings.append("yandex_webmaster: host_id missing")
            return

        result = _call(
            client,
            "popular_queries",
            host_id,
            limit=_WEEKLY_YANDEX_LIMIT,
            date_from=start,
            date_to=end,
        )
        rows = _result_rows(result)
        current[SOURCE_YANDEX_WEBMASTER] = _yandex_totals(rows)
        sources[SOURCE_YANDEX_WEBMASTER] = _source_entry(result, rows)

    def _collect_metrika_weekly(
        self,
        start: date,
        end: date,
        previous_start: date,
        previous_end: date,
        current: dict[str, dict[str, float]],
        previous: dict[str, dict[str, float]],
        sources: dict[str, dict[str, Any]],
        warnings: list[str],
    ) -> None:
        client = self._maybe_client(SOURCE_YANDEX_METRIKA)
        if client is None:
            warnings.append("yandex_metrika: token missing")
            return
        counter = self._config.providers.yandex_metrika
        counter_id = counter.counter if counter is not None else ""
        if not counter_id:
            warnings.append("yandex_metrika: counter missing")
            return

        metrics = ("ym:s:visits", "ym:s:users")
        current_result = _call(
            client,
            "stat_data",
            counter=counter_id,
            metrics=metrics,
            date1=start,
            date2=end,
            limit=1,
        )
        previous_result = _call(
            client,
            "stat_data",
            counter=counter_id,
            metrics=metrics,
            date1=previous_start,
            date2=previous_end,
            limit=1,
        )
        current_rows = _result_rows(current_result)
        previous_rows = _result_rows(previous_result)
        current[SOURCE_YANDEX_METRIKA] = _metrika_totals(current_rows)
        previous[SOURCE_YANDEX_METRIKA] = _metrika_totals(previous_rows)
        sources[SOURCE_YANDEX_METRIKA] = _source_entry(current_result, current_rows)
        sources[SOURCE_YANDEX_METRIKA]["meta"] = _combined_result_meta(
            (current_result, previous_result), source=SOURCE_YANDEX_METRIKA
        )

    def _collect_bing_weekly(self, sources: dict[str, dict[str, Any]], warnings: list[str]) -> None:
        """Collect Bing query stats when the site is configured (traffic only).

        Bing rows are shown per-engine; they never merge into the GSC headline
        because the sampling and windows differ.
        """
        client = self._maybe_client(SOURCE_BING)
        if client is None:
            return
        bing = self._config.providers.bing
        if bing is None or not bing.site.strip():
            warnings.append("bing: site missing")
            return
        result = _call(client, "query_stats")
        rows = _result_rows(result)
        sources[SOURCE_BING] = _source_entry(result, rows)

    def _collect_crux_weekly(self, sources: dict[str, dict[str, Any]], warnings: list[str]) -> None:
        """Collect CrUX *field* data for the configured origin (real-user p75s)."""
        client = self._maybe_client(SOURCE_CRUX)
        if client is None:
            return
        crux = self._config.providers.crux
        origin = crux.origin.strip() if crux is not None else ""
        if not origin:
            warnings.append("crux: origin missing")
            return
        result = _call(client, "origin", origin)
        rows = _result_rows(result)
        entry = _source_entry(result, rows)
        entry["kind"] = "field"
        sources[SOURCE_CRUX] = entry

    def _collect_psi_weekly(self, sources: dict[str, dict[str, Any]], warnings: list[str]) -> None:
        """Collect PSI *lab* data for the configured URLs (synthetic, not field).

        Lab numbers describe a simulated load, not real users, so they are
        labelled ``lab`` and kept out of every field-data comparison.
        """
        client = self._maybe_client(SOURCE_PSI)
        if client is None:
            return
        psi = self._config.providers.psi
        urls = list(psi.urls) if psi is not None else []
        if not urls:
            warnings.append("psi: urls missing")
            return
        collected: list[dict[str, Any]] = []
        psi_warnings: list[str] = []
        strategy = psi.strategy if psi is not None else "mobile"
        for url in urls[:5]:
            result = _call(client, "run", url, strategy=strategy)
            rows = _result_rows(result)
            collected.extend(rows)
            psi_warnings.extend(_result_warnings(result))
        warnings.extend(psi_warnings)
        entry: dict[str, Any] = {
            "meta": {
                "source": SOURCE_PSI,
                "rows": len(collected),
                "kind": "lab",
                "data_age_days": 0,
                "truncated": len(urls) > 5,
            },
            "rows": len(collected),
            "data": collected,
            "kind": "lab",
        }
        if len(urls) > 5:
            warnings.append("psi: url list capped at 5 per weekly run")
        sources[SOURCE_PSI] = entry

    def _fallback_weekly_report(
        self,
        *,
        property_id: str,
        start: date,
        end: date,
        current: Mapping[str, Mapping[str, float]],
        previous: Mapping[str, Mapping[str, float]],
        limitations: Sequence[str],
        warnings: Sequence[str],
    ) -> Report:
        sections: list[ReportSection] = []
        thresholds = DeltaThresholds(
            clicks_pct=self._config.report.thresholds.clicks_pct,
            impressions_pct=self._config.report.thresholds.impressions_pct,
            position_abs=self._config.report.thresholds.position_abs,
        )

        gsc_current = current.get(SOURCE_GSC) or {}
        gsc_previous = previous.get(SOURCE_GSC) or {}
        if gsc_current:
            # A delta needs both sides. With only the current window, emit the
            # same current-only rows as the builder: previous/abs/pct stay None
            # so no zero baseline, false delta or verdict is produced.
            if gsc_previous:
                deltas = compute_delta(gsc_current, gsc_previous, thresholds=thresholds)
                gsc_rows: list[dict[str, Any]] = [
                    {
                        "metric": row.metric,
                        "current": row.current,
                        "previous": row.previous,
                        "abs_delta": row.abs_delta,
                        "pct_delta": row.pct_delta,
                        "significant": row.significant,
                        "good": row.good,
                    }
                    for row in deltas
                ]
                gsc_summary = delta_headline(deltas) or "no change"
            else:
                gsc_rows = _current_only_gsc_rows(gsc_current)
                gsc_summary = "Comparison unavailable: no previous GSC window"
            sections.append(
                ReportSection(
                    key="gsc",
                    title="Google Search Console",
                    summary=gsc_summary,
                    rows=gsc_rows,
                )
            )

        yandex_current = current.get("yandex_webmaster")
        if yandex_current:
            rows = [
                {"metric": metric, "current": value}
                for metric, value in sorted(yandex_current.items())
            ]
            sections.append(
                ReportSection(
                    key="yandex_webmaster",
                    title="Yandex Webmaster",
                    summary=", ".join(f"{row['metric']} {row['current']:.1f}" for row in rows),
                    rows=rows,
                    notes=["Yandex Webmaster exposes a windowed top-queries report only."],
                )
            )

        metrika_current = current.get(SOURCE_YANDEX_METRIKA)
        metrika_previous = previous.get(SOURCE_YANDEX_METRIKA)
        if metrika_current or metrika_previous:
            rows = [
                {
                    "metric": metric,
                    "current": float((metrika_current or {}).get(metric, 0.0)),
                    "previous": float((metrika_previous or {}).get(metric, 0.0)),
                }
                for metric in ("visits", "users")
            ]
            sections.append(
                ReportSection(
                    key="yandex_metrika",
                    title="Yandex Metrika",
                    summary=", ".join(
                        f"{row['metric']} {row['current']:.0f} (prev {row['previous']:.0f})"
                        for row in rows
                    ),
                    rows=rows,
                )
            )

        if not sections:
            sections.append(
                ReportSection(
                    key="sources",
                    title="Sources",
                    summary="No provider data available",
                    notes=list(warnings) or ["configure a provider token to collect data"],
                )
            )

        headline = "; ".join(section.summary for section in sections if section.summary)
        return Report(
            kind="weekly",
            property_id=property_id,
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            headline=headline or "No provider data available",
            sections=sections,
            generated_at=utc_now().isoformat(timespec="seconds"),
            limitations=list(limitations),
            warnings=list(warnings),
        )

    # -- client wiring -----------------------------------------------------------------------

    def _maybe_client(self, name: str) -> object | None:
        if name in self._overrides:
            return self._overrides[name]
        if name in _KEYLESS_CLIENTS:
            return self._wired_client(name)
        if name == "gsc" and self._gsc_service_account_file():
            problem = self._gsc_service_account_problem()
            if problem is not None:
                raise SeoError(
                    code=CONFIG_INVALID,
                    message=problem,
                    next_action=SERVICE_ACCOUNT_FILE_NEXT_ACTION,
                )
        if not self._credential_present(name, _ENGINE_SECRETS[name]):
            return None
        return self._wired_client(name)

    def _client_for(self, name: str, warnings: list[str]) -> object:
        client = self._maybe_client(name)
        if client is None:
            warnings.append(f"{name}: token missing")
            raise SeoError(
                code=AUTH_REQUIRED,
                message=f"{_ENGINE_LABELS[name]} token is missing",
                next_action=_ENGINE_NEXT_ACTIONS[name],
            )
        return client

    def _wired_client(self, name: str) -> object:
        if name not in self._wired:
            self._wired[name] = self._build_client(name)
        return self._wired[name]

    def _build_client(self, name: str) -> object:
        providers_cfg = self._config.providers
        if name == "psi":
            key = self._secrets.get(_ENGINE_SECRETS["psi"]) or ""
            return PsiClient(api_key=key)

        if name == "gsc":
            gsc = providers_cfg.gsc
            property_id = gsc.property if gsc is not None else ""
            # Resolution order: config/env service_account_file, then the gsc_token secret.
            # The file wins even when a stale access token is still stored.
            account_file = self._gsc_service_account_file()
            if account_file:
                return GscClient(
                    token_provider=ServiceAccountTokenProvider(
                        load_service_account_file(account_file)
                    ),
                    property_id=property_id,
                )
            return GscClient(
                token_provider=token_provider_from_token(self._require_secret("gsc")),
                property_id=property_id,
            )

        token = self._require_secret(name)
        if name == "yandex_webmaster":
            return YandexWebmasterClient(token=token)
        if name == "yandex_metrika":
            return YandexMetrikaClient(token=token)
        if name == "bing":
            bing = providers_cfg.bing
            return BingClient(
                api_key=token,
                site_url=bing.site if bing is not None else "",
            )
        return CruxClient(api_key=token)

    def _require_secret(self, name: str) -> str:
        value = self._secrets.get(_ENGINE_SECRETS[name])
        if value is None or not value.strip():
            raise SeoError(
                code=AUTH_REQUIRED,
                message=f"{_ENGINE_LABELS[name]} token is missing",
                next_action=_ENGINE_NEXT_ACTIONS[name],
            )
        return value

    def _secret_present(self, secret_name: str) -> bool:
        value = self._secrets.get(secret_name)
        return value is not None and bool(value.strip())

    def _gsc_service_account_file(self) -> str:
        gsc = self._config.providers.gsc
        return gsc.service_account_file.strip() if gsc is not None else ""

    def _gsc_service_account_problem(self) -> str | None:
        return service_account_file_problem(self._gsc_service_account_file())

    def _credential_state(self, provider: str, secret_name: str) -> tuple[bool, str]:
        """Presence plus a non-secret source label for ``properties``.

        A configured GSC service-account file wins over a stored token, mirroring the
        resolution order used to build the client. The check only stats the file; the
        JSON is read once when the client is constructed.
        """
        if provider == "gsc" and self._gsc_service_account_file():
            return (self._gsc_service_account_problem() is None, "service_account_file")
        if self._secret_present(secret_name):
            return (True, "secret")
        return (False, "missing")

    def _credential_present(self, provider: str, secret_name: str) -> bool:
        return self._credential_state(provider, secret_name)[0]

    # -- shared helpers ----------------------------------------------------------------------

    def _yandex_host_id(self, *, required: bool = True) -> str:
        webmaster = self._config.providers.yandex_webmaster
        host_id = webmaster.host_id.strip() if webmaster is not None else ""
        if not host_id and required:
            raise SeoError(
                code=CONFIG_INVALID,
                message="Yandex Webmaster host_id is not configured",
                next_action="set providers.yandex_webmaster.host_id in seo.toml",
            )
        return host_id

    def _crawl_property_id(self, start_url: str) -> str:
        project = self._config.project
        return project.default_property or project.base_url or start_url

    def _respond(
        self,
        operation: str,
        warnings: list[str],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            return action()
        except SeoError as exc:
            return fail(exc, warnings=warnings)
        except Exception:
            # Never put a traceback or an exception message into the payload: provider
            # exceptions may echo request headers. The log line is deliberately generic too.
            logger.warning("unexpected failure in seo service operation %s", operation)
            return fail(
                SeoError(
                    code=INTERNAL,
                    message=f"unexpected internal error while running {operation}",
                    next_action="retry; if it persists run `lv24-seo doctor`",
                ),
                warnings=warnings,
            )


# -- result helpers --------------------------------------------------------------------------


def _call(client: object, method: str, *args: object, **kwargs: object) -> object:
    function = getattr(client, method, None)
    if not callable(function):
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"provider client does not implement {method}",
            retryable=False,
        )
    return function(*args, **kwargs)


def _optional_call(client: object, method: str, *args: object, **kwargs: object) -> object | None:
    """Call an optional read when the client implements it; ``None`` means unsupported."""
    function = getattr(client, method, None)
    if not callable(function):
        return None
    return cast(object, function(*args, **kwargs))


def _result_rows(result: object) -> list[dict[str, Any]]:
    raw = getattr(result, "rows", None)
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message="provider returned malformed rows",
            retryable=True,
        )
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise SeoError(
                code=UPSTREAM_UNAVAILABLE,
                message="provider returned malformed rows",
                retryable=True,
            )
        rows.append(dict(item))
    return rows


def _result_meta(result: object, *, source: str, rows: Sequence[Any]) -> dict[str, Any]:
    to_meta = getattr(result, "to_meta", None)
    if callable(to_meta):
        payload = to_meta()
        meta = dict(payload) if isinstance(payload, Mapping) else {}
    else:
        meta = {
            "source": str(getattr(result, "source", source)),
            "property_id": str(getattr(result, "property_id", "")),
            "fetched_at": _iso_or_empty(getattr(result, "fetched_at", None)),
            "data_age_days": _result_data_age(result),
            "truncated": _result_truncated(result),
        }
        total = getattr(result, "total", None)
        if isinstance(total, int) and not isinstance(total, bool):
            meta["total"] = total
    meta["rows"] = len(rows)
    meta.setdefault("source", source)
    return meta


def _result_warnings(result: object) -> list[str]:
    return _list_of_str(getattr(result, "warnings", ()))


def _result_limitations(result: object) -> list[str]:
    limitations = _list_of_str(getattr(result, "limitations", ()))
    meta = getattr(result, "to_meta", None)
    if callable(meta):
        payload = meta()
        if isinstance(payload, Mapping):
            limitations.extend(_list_of_str(payload.get("limitations")))
    return _unique(limitations)


def _result_data_age(result: object) -> int:
    return _int_of(getattr(result, "data_age_days", 0))


def _result_truncated(result: object) -> bool:
    return bool(getattr(result, "truncated", False))


def _source_entry(result: object, rows: Sequence[Any]) -> dict[str, Any]:
    meta = _result_meta(result, source=str(getattr(result, "source", "")), rows=rows)
    meta["warnings"] = _result_warnings(result)
    return {"meta": meta, "rows": len(rows), "data": list(rows)}


def _combined_result_meta(results: Sequence[object], *, source: str) -> dict[str, Any]:
    """Carry bounds and caveats from every dataset, including previous windows."""
    metadata = [
        _result_meta(result, source=source, rows=_result_rows(result)) for result in results
    ]
    totals = [meta.get("total") for meta in metadata]
    # A dataset without a reported total is unknown, never zero: summing it as 0
    # would turn "provider did not say" into a known count.
    known = bool(totals) and all(
        isinstance(value, int) and not isinstance(value, bool) for value in totals
    )
    return {
        "source": source,
        "truncated": any(meta.get("truncated") for meta in metadata),
        "total": sum(_int_of(value) for value in totals) if known else None,
        "data_age_days": max((_result_data_age(result) for result in results), default=0),
        "limitations": _unique(
            [item for result in results for item in _result_limitations(result)]
        ),
        "warnings": _unique([item for result in results for item in _result_warnings(result)]),
        "datasets": metadata,
    }


def _source_rows(sources: Mapping[str, Mapping[str, Any]], engine: str) -> list[Any]:
    """Rows captured for one engine during the weekly collection."""
    entry = sources.get(engine) or {}
    data = entry.get("data")
    return list(data) if isinstance(data, list) else []


def _extra_engine_rows(sources: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Traffic rows for engines outside the GSC/Yandex/Metrika fusion.

    Bing query stats, CrUX field p75s and PSI lab scores are shown per-engine in
    the sources section; they are never merged into a cross-engine total and
    field (CrUX) and lab (PSI) data stay labelled apart.
    """
    extras: list[dict[str, Any]] = []
    for engine in (SOURCE_BING, SOURCE_CRUX, SOURCE_PSI):
        entry = sources.get(engine)
        if not entry:
            continue
        rows = entry.get("data")
        row_list = list(rows) if isinstance(rows, list) else []
        meta = entry.get("meta", {})
        meta_map = meta if isinstance(meta, Mapping) else {}
        extras.append(
            {
                "engine": engine,
                "kind": entry.get("kind", "field" if engine == SOURCE_CRUX else "traffic"),
                "rows": row_list,
                "limitations": list(meta_map.get("limitations", [])),
            }
        )
    return extras


def _flatten_totals(totals: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """Merge per-engine totals into one comparable block (sums + impression-weighted position)."""
    clicks = sum(float(values.get("clicks") or 0.0) for values in totals.values())
    impressions = sum(float(values.get("impressions") or 0.0) for values in totals.values())
    weighted = sum(
        float(values.get("position") or 0.0) * float(values.get("impressions") or 0.0)
        for values in totals.values()
    )
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": (clicks / impressions) if impressions > 0 else 0.0,
        "position": (weighted / impressions) if impressions > 0 else 0.0,
    }


def _gsc_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    clicks = sum(_float_or_none(row.get("clicks")) or 0.0 for row in rows)
    impressions = sum(_float_or_none(row.get("impressions")) or 0.0 for row in rows)
    weighted = sum(
        (_float_or_none(row.get("position")) or 0.0)
        * (_float_or_none(row.get("impressions")) or 0.0)
        for row in rows
    )
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": (clicks / impressions) if impressions > 0 else 0.0,
        "position": (weighted / impressions) if impressions > 0 else 0.0,
    }


def _current_only_gsc_rows(current: Mapping[str, float]) -> list[dict[str, Any]]:
    """Current GSC totals with no baseline; every comparison field is ``None``.

    Mirrors the ``report.builders`` current-only headline rows. The builders
    module is imported lazily and may be unavailable on this fallback path, so
    the shape is kept here instead of imported.
    """
    return [
        {
            "metric": metric,
            "current": float(current[metric]) if metric in current else None,
            "previous": None,
            "abs_delta": None,
            "pct_delta": None,
            "significant": False,
            "direction": "flat",
            "good": None,
        }
        for metric in _HEADLINE_METRICS
    ]


def _yandex_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    shows = sum(_float_or_none(row.get("shows")) or 0.0 for row in rows)
    weighted = sum(
        (_float_or_none(row.get("position")) or 0.0) * (_float_or_none(row.get("shows")) or 0.0)
        for row in rows
    )
    return {
        "shows": shows,
        "clicks": sum(_float_or_none(row.get("clicks")) or 0.0 for row in rows),
        "position": (weighted / shows) if shows > 0 else 0.0,
    }


def _metrika_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    return {
        "visits": sum(_float_or_none(row.get("visits")) or 0.0 for row in rows),
        "users": sum(_float_or_none(row.get("users")) or 0.0 for row in rows),
    }


def _bing_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    clicks = sum(_float_or_none(row.get("clicks")) or 0.0 for row in rows)
    impressions = sum(_float_or_none(row.get("impressions")) or 0.0 for row in rows)
    weighted = sum(
        (_float_or_none(row.get("position")) or 0.0)
        * (_float_or_none(row.get("impressions")) or 0.0)
        for row in rows
    )
    return {
        "clicks": clicks,
        "impressions": impressions,
        "position": (weighted / impressions) if impressions > 0 else 0.0,
    }


def _stats_entry(provider: str, *, configured: bool, ready: bool) -> dict[str, Any]:
    """The stable per-service skeleton; collectors fill metrics and status."""
    return {
        "provider": provider,
        "label": _ENGINE_LABELS[provider],
        "configured": configured,
        "ready": ready,
        "status": "not_configured",
        "metrics": {},
        "previous": None,
        "rows": 0,
        "data_age_days": 0,
        "truncated": False,
        "limitations": [],
        "warnings": [],
        "error": None,
    }


def _stats_fill(entry: dict[str, Any], meta: Mapping[str, Any]) -> None:
    entry["data_age_days"] = _int_of(meta.get("data_age_days"))
    entry["truncated"] = bool(meta.get("truncated"))
    entry["limitations"] = _list_of_str(meta.get("limitations"))
    entry["warnings"] = _list_of_str(meta.get("warnings"))


def _crux_metrics(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, float], dict[str, str]]:
    """Map CrUX rows to ``metric -> p75`` plus the unit the provider reported."""
    metrics: dict[str, float] = {}
    units: dict[str, str] = {}
    for row in rows:
        raw_name = row.get("metric")
        value = _float_or_none(row.get("p75"))
        if not isinstance(raw_name, str) or not raw_name.strip() or value is None:
            continue
        name = raw_name.strip()
        metrics[name] = value
        raw_unit = row.get("unit")
        if raw_unit in ("ms", "score"):
            units[name] = str(raw_unit)
        else:
            units[name] = "score" if name == "cumulative_layout_shift" else "ms"
    return metrics, units


def _psi_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Lab scores plus field p75s from one PSI run; never averaged across URLs."""
    metrics: dict[str, float] = {}
    for row in rows:
        category = row.get("category")
        if category in ("performance", "seo"):
            score = _float_or_none(row.get("score"))
            if score is not None:
                metrics[str(category)] = score
            continue
        if category != "field":
            continue
        label = row.get("metric")
        p75 = _float_or_none(row.get("p75"))
        if isinstance(label, str) and label.strip() and p75 is not None:
            metrics[label.strip()] = p75
    return metrics


def _report_payload(report: object) -> dict[str, Any]:
    if isinstance(report, Mapping):
        return dict(report)
    to_dict = getattr(report, "to_dict", None)
    if not callable(to_dict):
        raise SeoError(
            code=INTERNAL,
            message="report builder returned an unsupported payload",
        )
    payload = to_dict()
    if not isinstance(payload, Mapping):
        raise SeoError(
            code=INTERNAL,
            message="report builder returned an unsupported payload",
        )
    return dict(payload)


# -- sitemap write helpers --------------------------------------------------------------------


def _sitemap_provider(value: str) -> str:
    """Normalise a sitemap provider name; an unknown name is a caller error."""
    name = value.strip().lower()
    if name not in _SITEMAP_PROVIDERS:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unknown sitemap provider {value!r}",
            next_action=f"use one of: {', '.join(sorted(_SITEMAP_PROVIDERS))}",
        )
    return name


def _absolute_http_url(value: str, *, name: str) -> str:
    """Return a trimmed absolute http(s) URL or raise ``INVALID_INPUT``."""
    url = value.strip()
    if not url:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must not be empty",
            next_action=f"pass an absolute http(s) URL to {name}",
        )
    parts = urlsplit(url)
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an absolute http(s) URL",
            next_action=f"pass a URL such as https://example.com/sitemap.xml to {name}",
            detail={"value": url},
        )
    return url


# -- crawl helpers ---------------------------------------------------------------------------


def _start_url(start_url: str | None, config: SeoConfig) -> str:
    raw = start_url if start_url is not None else config.project.base_url
    if not isinstance(raw, str) or not raw.strip():
        raise SeoError(
            code=INVALID_INPUT,
            message="start_url is required",
            next_action="pass --start https://example.com/ or set project.base_url",
        )
    candidate = raw.strip()
    parts = urlsplit(candidate)
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
        raise SeoError(
            code=INVALID_INPUT,
            message="start_url must be an absolute http(s) URL",
            next_action="pass a URL such as https://example.com/",
        )
    return candidate


def _request_path(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _blocked_page(url: str) -> PageData:
    return PageData(url=url, status=0, final_url=url, error="robots_blocked")


def _page_from_fetch(url: str, result: object) -> PageData:
    status = _int_of(getattr(result, "status", 0))
    headers = getattr(result, "headers", {})
    final_url = str(getattr(result, "final_url", url) or url)
    error = getattr(result, "error", None)
    redirect_chain = list(getattr(result, "redirect_chain", ()) or ())
    downgraded = bool(getattr(result, "downgraded", False))
    if error:
        return PageData(
            url=url,
            status=status,
            final_url=final_url,
            headers=dict(headers) if isinstance(headers, Mapping) else {},
            error=str(error),
            redirect_chain=redirect_chain,
            downgraded=downgraded,
        )
    body = getattr(result, "body", b"")
    page = parse_html(
        url,
        body if isinstance(body, (bytes, str)) else b"",
        status=status,
        headers=headers if isinstance(headers, Mapping) else {},
        final_url=final_url,
    )
    page.redirect_chain = redirect_chain
    page.downgraded = downgraded
    return page


def _index_state(page: PageData) -> str:
    if page.error is not None or page.status == 0 or page.status >= 400:
        return "error"
    if len(page.redirect_chain) > 1:
        return "redirect"
    if "noindex" in page.robots_directives:
        return "noindex"
    if page.status == 200:
        return "indexable"
    return "other"


def _title_hash(title: str | None) -> str | None:
    if title is None or not title.strip():
        return None
    normalized = " ".join(title.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# -- persistence helpers ---------------------------------------------------------------------


def _gsc_property(result: object, config: SeoConfig) -> str:
    value = getattr(result, "property_id", "")
    if isinstance(value, str) and value.strip():
        return value.strip()
    gsc = config.providers.gsc
    return gsc.property.strip() if gsc is not None else ""


def _gsc_configured_property(config: SeoConfig) -> str:
    gsc = config.providers.gsc
    return gsc.property.strip() if gsc is not None else ""


def _report_property_id(config: SeoConfig) -> str:
    return (
        config.project.default_property.strip()
        or _gsc_configured_property(config)
        or config.project.base_url.strip()
        or config.project.name.strip()
        or "default"
    )


def _scope_for_dimensions(dimensions: Sequence[str]) -> str:
    for scope in ("query", "page", "device", "country"):
        if scope in dimensions:
            return scope
    return "total"


def _metric_key(row: Mapping[str, Any], dimensions: Sequence[str]) -> str:
    parts: list[str] = []
    for dimension in dimensions:
        if dimension == "date":
            continue
        value = row.get(dimension)
        if value is None or value == "":
            continue
        parts.append(str(value))
    # A single dimension needs no separator. Multiple values are JSON-encoded so values
    # containing "|" stay distinct: ("a|b", "c") != ("a", "b|c").
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def _stored_day(value: object) -> date | None:
    """Parse a stored metric date without inventing a day for an unknown value."""
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


def _indexing_event_candidate(
    row: Mapping[str, Any],
) -> tuple[str, str, date, dict[str, Any] | None] | None:
    """Extract one storable per-URL event, or ``None`` when the row has no identity."""
    url = _event_url(row.get("url") or row.get("URL"))
    event = _event_name(row.get("event") or row.get("event_type") or row.get("type"))
    event_date = _event_day(row.get("event_date") or row.get("date"))
    if url is None or event is None or event_date is None:
        return None
    return url, event, event_date, _event_detail(row, event)


def _event_url(value: object) -> str | None:
    """Only an absolute http(s) URL can be a per-URL event; a count or path is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parts = urlsplit(text)
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
        return None
    return text


def _event_name(value: object) -> str | None:
    """Normalise a provider event name to the store's stable vocabulary.

    ``APPEARED_IN_SEARCH``/``URL_ADDED``/``entered`` all mean the page became indexed;
    ``REMOVED_FROM_SEARCH``/``URL_REMOVED``/``exited`` mean it was excluded. Unknown names
    keep their provider spelling (lowercase, underscores) so two distinct events never
    collapse into one label.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    folded = "_".join(text.casefold().replace("-", " ").split())
    mapped = _INDEXING_EVENT_ALIASES.get(folded)
    if mapped is not None:
        return mapped
    return folded[:64] or None


def _event_day(value: object) -> date | None:
    """Accept a date or an ISO timestamp (Yandex uses a comma before milliseconds)."""
    return _stored_day(value)


def _event_detail(row: Mapping[str, Any], event: str) -> dict[str, Any] | None:
    """Keep the compact provider evidence for one event, never the whole payload."""
    detail: dict[str, Any] = {}
    raw_event = row.get("event") or row.get("event_type") or row.get("type")
    if isinstance(raw_event, str):
        text = raw_event.strip()
        if text and text.casefold() != event:
            detail["provider_event"] = text[:64]
    for key in _INDEXING_EVENT_DETAIL_KEYS:
        value = row.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                detail[key] = text[:500]
        elif isinstance(value, (int, float, bool)):
            detail[key] = value
    return detail or None


def _row_day(row: Mapping[str, Any], dimensions: Sequence[str], fallback: date) -> date:
    if "date" not in dimensions:
        return fallback
    value = row.get("date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return fallback
    return fallback


# -- primitive helpers -----------------------------------------------------------------------


def _as_date(value: object, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise SeoError(
        code=INVALID_INPUT,
        message=f"{name} must be a date, got {type(value).__name__}",
        next_action="pass an ISO date such as 2026-11-01",
    )


def _dimensions(dimensions: object) -> tuple[str, ...]:
    if isinstance(dimensions, str) or not isinstance(dimensions, Sequence):
        raise SeoError(
            code=INVALID_INPUT,
            message="dimensions must be a sequence of names",
            next_action="pass dimensions=('query',) or ('page',)",
        )
    names: list[str] = []
    for value in dimensions:
        if not isinstance(value, str) or not value.strip():
            raise SeoError(
                code=INVALID_INPUT,
                message="dimensions entries must be non-empty strings",
                next_action="use query, page, country, device or date",
            )
        names.append(value.strip())
    if not names:
        raise SeoError(
            code=INVALID_INPUT,
            message="dimensions must not be empty",
            next_action="use at least one of query, page, country, device or date",
        )
    return tuple(names)


def _cwv_source(value: str) -> str:
    name = value.strip().casefold()
    if name not in _CWV_SOURCES:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unsupported cwv source: {value!r}",
            next_action="pass --source crux, psi or both",
        )
    return name


def _cwv_devices(value: str) -> list[str]:
    canonical = _CWV_DEVICE_ALIASES.get(value.strip().casefold())
    if canonical is None:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"unsupported cwv device: {value!r}",
            next_action="pass --device phone, desktop, tablet or all",
        )
    if canonical == "all":
        return list(CWV_DEVICES)
    return [canonical]


def _cwv_urls(values: Sequence[str]) -> list[str]:
    """Validate, strip and de-duplicate CWV target URLs without echoing query strings."""
    cleaned: list[str] = []
    for raw in values:
        candidate = raw.strip()
        if not candidate:
            continue
        parts = urlsplit(candidate)
        if parts.scheme not in _HTTP_SCHEMES or not parts.hostname:
            raise SeoError(
                code=INVALID_INPUT,
                message="cwv url must be an absolute http(s) URL",
                next_action="pass a URL such as https://example.com/page",
            )
        if candidate not in cleaned:
            cleaned.append(candidate)
    if not cleaned:
        raise SeoError(
            code=INVALID_INPUT,
            message="at least one cwv url is required",
            next_action="pass --url https://example.com/page (repeat the flag for more pages)",
        )
    return cleaned


def _cwv_metric(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _CWV_METRICS.get(value.strip().casefold())


def _positive_int(value: object, *, name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an integer",
            next_action=f"pass a positive integer for {name}",
        )
    if value < 1:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be >= 1",
            next_action=f"pass a positive integer for {name}",
        )
    if maximum is not None and value > maximum:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be <= {maximum}",
            next_action=f"pass a value between 1 and {maximum}",
        )
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an integer >= 0",
            next_action=f"pass a non-negative integer for {name}",
        )
    return value


def _float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int_of(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


def _iso_or_empty(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return ""


def _list_of_str(value: object) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
