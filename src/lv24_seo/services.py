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
import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast
from urllib.parse import urlsplit

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
    SeoError,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import compare_period, days_between, local_today, utc_now
from lv24_seo.crawl.fetcher import Fetcher, RobotsPolicy, is_allowed
from lv24_seo.crawl.graph import GraphStats, build_graph, normalize_url
from lv24_seo.crawl.html import parse_html
from lv24_seo.crawl.models import Issue, PageData, RuleContext
from lv24_seo.crawl.rules import run_all
from lv24_seo.providers.bing import BingClient
from lv24_seo.providers.crux import CruxClient
from lv24_seo.providers.gsc import GscClient
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

# GSC search analytics carries no HTTP status; 0 means "unknown", never a made-up 200.
_GSC_PAGE_STATUS = 0
_GSC_PAGE_INDEX_STATE = "gsc_search_analytics"

_HTTP_SCHEMES = frozenset({"http", "https"})
# PSI works keyless; a key only raises the quota, so it never blocks client construction.
_KEYLESS_CLIENTS = frozenset({"psi"})
_DELIVERY_UNAVAILABLE = "delivery: unavailable; report was not sent"
_BUILDERS_UNAVAILABLE = "report builders: unavailable; using a minimal fallback report"

ReportBuilder = Callable[..., Report]
Delivery = Callable[..., object]


def _load_weekly_builder() -> ReportBuilder | None:
    """Return ``report.builders.build_weekly`` when WP5 is installed."""
    try:
        from lv24_seo.report.builders import build_weekly
    except ImportError:
        return None
    return build_weekly if callable(build_weekly) else None


def _load_deliver() -> Delivery | None:
    """Return ``report.delivery.deliver`` when the delivery layer is importable.

    Delivery is optional and must never turn a finished report into a failed run, so even a
    broken optional module degrades to the "delivery unavailable" warning.
    """
    try:
        from lv24_seo.report.delivery import deliver
    except Exception:
        return None
    return deliver if callable(deliver) else None


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

    # -- discovery ---------------------------------------------------------------------------

    def properties(self) -> dict[str, Any]:
        """List configured providers, their target and whether a token is present."""
        warnings: list[str] = []
        return self._respond("properties", warnings, lambda: self._properties(warnings))

    def _properties(self, warnings: list[str]) -> dict[str, Any]:
        entries = [self._provider_entry(*spec) for spec in self._provider_specs()]

        for entry in entries:
            if entry["configured"] and entry["requires_token"] and not entry["token_present"]:
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
        present = self._secret_present(secret_name)
        state = "present" if present else "missing"
        return {
            "provider": provider,
            "configured": configured,
            "target": target,
            "requires_token": requires_token,
            "token": state,
            "token_present": present,
            "ready": configured and (present or not requires_token),
        }

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
        fallback_day = end
        counts = {"metrics": 0, "pages": 0}

        for row in rows:
            day = _row_day(row, dimensions, fallback_day)
            key = _metric_key(row, dimensions)
            for metric in ("clicks", "impressions"):
                value = _float_or_none(row.get(metric))
                if value is None:
                    continue
                store.upsert_metric_daily(
                    property_id,
                    "gsc",
                    day,
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
            store.upsert_page(
                property_id,
                page_url.strip(),
                status=_GSC_PAGE_STATUS,
                index_state=_GSC_PAGE_INDEX_STATE,
            )
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
        }
        graph = {
            "nodes": outcome.graph.nodes,
            "edges": outcome.graph.edges,
            "max_depth": outcome.graph.max_depth,
            "orphans": outcome.graph.orphans,
            "broken": outcome.graph.broken,
        }
        data = {
            "summary": summary,
            "issues": issues,
            "graph": graph,
            "errors": outcome.errors,
            "blocked": outcome.blocked,
        }

        if outcome.errors:
            warnings.append(f"crawl: {len(outcome.errors)} urls could not be fetched")
        if outcome.blocked:
            warnings.append(f"crawl: {len(outcome.blocked)} urls blocked by robots.txt")

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

        while queue:
            url = queue.popleft()
            path = _request_path(url)
            if respect_robots and not is_allowed(robots, path):
                blocked.append(url)
                pages.append(_blocked_page(url))
                continue

            result = fetcher.fetch(url)
            page = _page_from_fetch(url, result)
            pages.append(page)
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
                sitemap_urls=[],
                robots_sitemaps=list(robots.sitemaps),
                robots_disallow=list(robots.disallow),
            )
        )
        return _CrawlOutcome(
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
        span = _positive_int(days, name="days")
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

        self._collect_gsc_weekly(
            start, current_end, previous_start, previous_end, current, previous, sources, warnings
        )
        self._collect_yandex_weekly(start, current_end, current, sources, warnings)
        self._collect_metrika_weekly(
            start, current_end, previous_start, previous_end, current, previous, sources, warnings
        )

        for source in sources.values():
            limitations.extend(_list_of_str(source["meta"].get("limitations")))
            provider_warnings.extend(_list_of_str(source["meta"].get("warnings")))
        limitations = _unique(limitations)

        if self._store is None:
            warnings.append("store: not configured; snapshot was not saved")
        all_warnings = _unique([*warnings, *provider_warnings])
        property_id = _report_property_id(self._config)
        period = f"{start.isoformat()}..{current_end.isoformat()}"

        builder = _load_weekly_builder()
        report_obj: Report
        if builder is None:
            warnings.append(_BUILDERS_UNAVAILABLE)
            all_warnings = _unique([*all_warnings, _BUILDERS_UNAVAILABLE])
            report_obj = self._fallback_weekly_report(
                property_id=property_id,
                start=start,
                end=current_end,
                current=current,
                previous=previous,
                limitations=limitations,
                warnings=all_warnings,
            )
        else:
            try:
                report_obj = builder(
                    property_id=property_id,
                    period_start=start,
                    period_end=current_end,
                    current=_flatten_totals(current),
                    previous=_flatten_totals(previous),
                    gsc_rows=_source_rows(sources, "gsc"),
                    yandex_rows=_source_rows(sources, "yandex"),
                    metrika_rows=_source_rows(sources, "metrika"),
                    limitations=limitations,
                )
            except TypeError:
                all_warnings = _unique(
                    [*all_warnings, "builders: signature mismatch; fallback report used"]
                )
                report_obj = self._fallback_weekly_report(
                    property_id=property_id,
                    start=start,
                    end=current_end,
                    current=current,
                    previous=previous,
                    limitations=limitations,
                    warnings=all_warnings,
                )
        payload = _report_payload(report_obj)

        payload["warnings"] = _unique([*_list_of_str(payload.get("warnings")), *all_warnings])
        payload["limitations"] = _unique([*_list_of_str(payload.get("limitations")), *limitations])

        snapshot_hash: str | None = None
        if self._store is not None:
            snapshot_hash = self._store.save_snapshot("weekly", property_id, period, payload)

        sent = False
        if send:
            sent = self._deliver_report(report_obj, warnings)

        data_age = max(
            (_int_of(source["meta"].get("data_age_days")) for source in sources.values()),
            default=0,
        )
        meta: dict[str, Any] = {
            "sent": sent,
            "property_id": property_id,
            "period": {"start": start.isoformat(), "end": current_end.isoformat()},
            "previous_period": {
                "start": previous_start.isoformat(),
                "end": previous_end.isoformat(),
            },
            "sources": sorted(sources),
            "data_age_days": data_age,
            "truncated": any(bool(source["meta"].get("truncated")) for source in sources.values()),
            "limitations": limitations,
        }
        if snapshot_hash is not None:
            meta["snapshot_hash"] = snapshot_hash
        return ok(payload, meta=meta, warnings=_unique(warnings))

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
            )
        telegram = None
        if cfg.telegram.enabled and cfg.telegram.chat_id:
            telegram = TelegramConfig(
                bot_token=self._secrets.get("telegram_bot_token") or "",
                chat_id=cfg.telegram.chat_id,
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
        )
        for result in results:
            if result.status == "failed":
                warnings.append(f"delivery {result.channel}: {result.reason}")
        return any(result.status == "sent" for result in results)

    def _collect_gsc_weekly(
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
        client = self._maybe_client("gsc")
        if client is None:
            warnings.append("gsc: token missing")
            return
        if "gsc" not in self._overrides and not _gsc_configured_property(self._config):
            warnings.append("gsc: property missing")
            return

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
        current["gsc"] = _gsc_totals(current_rows)
        previous["gsc"] = _gsc_totals(previous_rows)
        sources["gsc"] = _source_entry(current_result, current_rows)

    def _collect_yandex_weekly(
        self,
        start: date,
        end: date,
        current: dict[str, dict[str, float]],
        sources: dict[str, dict[str, Any]],
        warnings: list[str],
    ) -> None:
        del start, end  # Yandex Webmaster exposes the current report, not a window.
        client = self._maybe_client("yandex_webmaster")
        if client is None:
            warnings.append("yandex_webmaster: token missing")
            return
        host_id = self._yandex_host_id(required=False)
        if not host_id:
            warnings.append("yandex_webmaster: host_id missing")
            return

        result = _call(client, "popular_queries", host_id, limit=_WEEKLY_YANDEX_LIMIT)
        rows = _result_rows(result)
        current["yandex_webmaster"] = _yandex_totals(rows)
        sources["yandex_webmaster"] = _source_entry(result, rows)

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
        client = self._maybe_client("yandex_metrika")
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
        current["yandex_metrika"] = _metrika_totals(current_rows)
        previous["yandex_metrika"] = _metrika_totals(previous_rows)
        sources["yandex_metrika"] = _source_entry(current_result, current_rows)

    def _deliver(
        self,
        deliver: Delivery,
        payload: Mapping[str, Any],
        warnings: list[str],
    ) -> bool:
        channels = _delivery_channels(self._config)
        try:
            outcome = _invoke_delivery(deliver, payload, channels, self._config, self._store)
        except SeoError as exc:
            warnings.append(f"delivery: failed ({exc.code})")
            return False
        except Exception:
            logger.warning("unexpected delivery failure")
            warnings.append("delivery: failed")
            return False
        return _delivery_sent(outcome)

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

        gsc_current = current.get("gsc")
        gsc_previous = previous.get("gsc")
        if gsc_current or gsc_previous:
            deltas = compute_delta(gsc_current or {}, gsc_previous or {}, thresholds=thresholds)
            sections.append(
                ReportSection(
                    key="gsc",
                    title="Google Search Console",
                    summary=delta_headline(deltas) or "no change",
                    rows=[
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
                    ],
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
                    notes=["Yandex Webmaster exposes the current query report only."],
                )
            )

        metrika_current = current.get("yandex_metrika")
        metrika_previous = previous.get("yandex_metrika")
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
        if not self._secret_present(_ENGINE_SECRETS[name]):
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

        token = self._require_secret(name)
        if name == "gsc":
            gsc = providers_cfg.gsc
            return GscClient(
                access_token=token,
                property_id=gsc.property if gsc is not None else "",
            )
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


def _source_rows(sources: Mapping[str, Mapping[str, Any]], engine: str) -> list[Any]:
    """Rows captured for one engine during the weekly collection."""
    entry = sources.get(engine) or {}
    data = entry.get("data")
    return list(data) if isinstance(data, list) else []


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


def _delivery_sent(outcome: object) -> bool:
    if outcome is None:
        return True
    if isinstance(outcome, bool):
        return outcome
    if isinstance(outcome, Mapping):
        for key in ("sent", "delivered", "ok"):
            if key in outcome:
                return bool(outcome[key])
        status = outcome.get("status")
        if isinstance(status, str):
            return status.strip().lower() in {"sent", "delivered", "ok", "success"}
        return True
    return bool(outcome)


def _delivery_channels(config: SeoConfig) -> list[str]:
    """Enabled delivery channels, in a stable order.

    ``report/delivery`` follows ``deliver(report, channels)`` (see docs/architecture.md §5).
    A disabled channel is simply absent; an empty list lets the delivery layer decide whether
    that is a warning or a no-op.
    """
    channels: list[str] = []
    if config.report.email.enabled:
        channels.append("email")
    if config.report.telegram.enabled:
        channels.append("telegram")
    return channels


def _invoke_delivery(
    deliver: Delivery,
    report: Mapping[str, Any],
    channels: Sequence[str],
    config: SeoConfig,
    store: Store | None,
) -> object:
    """Call ``deliver`` with the documented signature, plus config/store when accepted.

    WP5 is landing in parallel: the contract is ``deliver(report, channels)``, while a richer
    implementation may also want the workspace config and the idempotency store. Inspecting the
    signature once keeps the service compatible with both without swallowing a real TypeError
    from inside delivery.
    """
    try:
        signature = inspect.signature(deliver)
    except (TypeError, ValueError):
        return deliver(report, channels)

    parameters = signature.parameters
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    positional = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    args: list[object] = [report]
    if len(positional) >= 2:
        args.append(channels)

    kwargs: dict[str, object] = {}
    if len(positional) < 2 and "channels" in parameters:
        kwargs["channels"] = channels
    if accepts_kwargs or "config" in parameters:
        kwargs["config"] = config
    if accepts_kwargs or "store" in parameters:
        kwargs["store"] = store
    return deliver(*args, **kwargs)


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
    if error:
        return PageData(
            url=url,
            status=status,
            final_url=final_url,
            headers=dict(headers) if isinstance(headers, Mapping) else {},
            error=str(error),
            redirect_chain=redirect_chain,
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
    return "|".join(parts)


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
