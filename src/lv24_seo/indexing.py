"""Quota-aware indexing automation.

The engine keeps a local queue position for every URL and provider (``indexing_state``),
plans submissions against provider-reported or locally counted quotas, and records one
``indexing_runs`` row per provider per run. It never invents an index verdict:

* GSC URL Inspection is the only per-URL index check. ``PASS`` or ``Submitted and indexed``
  maps to ``indexed``; ``NEUTRAL``/``FAIL`` maps to ``not_indexed``; anything else is
  ``unknown``.
* Bing ``GetUrlInfo`` reports a crawl date and HTTP status, not an index flag. A crawled URL
  is ``crawled``, a missing crawl is ``unknown``, and the result carries
  ``bing_no_per_url_index_status``.
* Yandex and IndexNow expose no per-URL index status; their rows stay ``unknown`` with
  ``yandex_no_per_url_index_status`` / ``indexnow_no_status_api``.
* Bing Site Scan has no public API. The result always carries ``site_scan_no_api``; the
  engine never pretends to automate it.

Submissions are gated: without ``allow_write`` the engine only checks and plans. With it, the
engine re-reads quotas, rebuilds the plan, verifies that its hash did not change, mints and
consumes a confirm token through :class:`~lv24_seo.core.write_gate.WriteGate`, and records the
run. A URL whose ``last_submitted_at`` is newer than its sitemap ``lastmod`` is not submitted
again unless ``force`` is set.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from lv24_seo.core.config import IndexingConfig, SeoConfig
from lv24_seo.core.errors import (
    CONFIG_INVALID,
    CONFIRM_REQUIRED,
    INVALID_INPUT,
    QUOTA_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.core.timeutil import utc_now
from lv24_seo.core.write_gate import WriteGate, WritePlan, plan_hash
from lv24_seo.crawl.fetcher import Fetcher
from lv24_seo.crawl.models import FetchResult
from lv24_seo.crawl.sitemap import MAX_BYTES as SITEMAP_MAX_BYTES
from lv24_seo.crawl.sitemap import SitemapReader
from lv24_seo.providers.base import ONE_URL_PER_CALL, RECRAWL_QUOTA_EXCEEDED
from lv24_seo.providers.bing import indexnow_submit, validate_indexnow_key_location
from lv24_seo.store.repositories import Store

__all__ = [
    "BING_NO_PER_URL_INDEX_STATUS",
    "INDEXNOW_NO_STATUS_API",
    "QUEUE_LIMIT",
    "SITE_SCAN_NO_API",
    "YANDEX_NO_PER_URL_INDEX_STATUS",
    "IndexingEngine",
    "IndexingTarget",
    "Queue",
    "QueueItem",
    "build_queue",
    "gsc_index_status",
    "plan_provider",
]

logger = logging.getLogger(__name__)

#: One sitemap tree is bounded to the root plus five child documents and 10,000 URLs, the
#: same bound the one-click IndexNow command uses.
QUEUE_LIMIT = 10_000
SITEMAP_DOCS = 6
BING_BATCH_LIMIT = 500
INDEXNOW_BATCH_LIMIT = 10_000

PROVIDERS: tuple[str, ...] = ("bing", "yandex", "gsc", "indexnow")

# Stable limitation codes. Consumers must be able to explain a capped or partial result.
SITE_SCAN_NO_API = "site_scan_no_api"
BING_NO_PER_URL_INDEX_STATUS = "bing_no_per_url_index_status"
YANDEX_NO_PER_URL_INDEX_STATUS = "yandex_no_per_url_index_status"
INDEXNOW_NO_STATUS_API = "indexnow_no_status_api"
NO_PER_URL_INDEX_COUNT = "no_per_url_index_count"
GSC_INDEXED_COUNT_DEPRECATED = "gsc_indexed_count_deprecated"
BING_QUOTA_RESET_UNDOCUMENTED = "bing_quota_reset_undocumented"
QUOTA_EXHAUSTED = "quota_exhausted"
QUOTA_UNAVAILABLE = "quota_unavailable"
QUOTA_NOT_LIVE = "quota_not_live"
QUEUE_CAP = "queue_cap"
SITEMAP_TRUNCATED = "sitemap_truncated"
SITEMAP_OFF_HOST = "sitemap_off_host"
SITEMAP_ERRORS = "sitemap_errors"
WRITE_SECRET_MISSING = "write_secret_missing"  # noqa: S105 - a limitation code, not a secret
PROVIDER_NOT_CONFIGURED = "provider_not_configured"
PROVIDER_AUTH_REQUIRED = "provider_auth_required"
CHECK_QUOTA_EXHAUSTED = "check_quota_exhausted"
SUBMIT_QUOTA_EXHAUSTED = "submit_quota_exhausted"
NO_STATE_YET = "no_state_yet"
PLAN_CHANGED = "plan_changed"

_PROVIDER_LIMITATIONS: dict[str, tuple[str, ...]] = {
    "bing": (
        SITE_SCAN_NO_API,
        BING_NO_PER_URL_INDEX_STATUS,
        NO_PER_URL_INDEX_COUNT,
        BING_QUOTA_RESET_UNDOCUMENTED,
    ),
    "yandex": (YANDEX_NO_PER_URL_INDEX_STATUS, NO_PER_URL_INDEX_COUNT),
    "gsc": (),
    "indexnow": (INDEXNOW_NO_STATUS_API, NO_PER_URL_INDEX_COUNT),
}

_INDEXED_COVERAGE = "submitted and indexed"


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One URL in the run queue and its sitemap ``lastmod`` when the sitemap had one."""

    url: str
    lastmod: datetime | None = None


@dataclass(slots=True)
class Queue:
    """A bounded, deduplicated, same-host URL queue."""

    items: list[QueueItem]
    sitemap_url: str = ""
    total: int = 0
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    off_host: int = 0
    limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IndexingTarget:
    """One provider/property pair selected for a run or a status read.

    ``ready`` is false when the provider block or credential is missing; the engine then
    reports ``reason`` instead of making a call or inventing an empty success.
    """

    provider: str
    property_id: str
    client_name: str = ""
    ready: bool = True
    reason: str = PROVIDER_NOT_CONFIGURED


@dataclass(slots=True)
class ProviderPlan:
    """What one provider would do before any write."""

    provider: str
    property_id: str
    submit: list[str] = field(default_factory=list)
    check: list[str] = field(default_factory=list)
    skipped: int = 0
    not_attempted: int = 0
    quota_before: dict[str, Any] | None = None
    limitations: list[str] = field(default_factory=list)
    status: str = "planned"


@dataclass(slots=True)
class ProviderOutcome:
    """What one provider actually did (or planned) in this run."""

    provider: str
    property_id: str
    planned: int = 0
    submitted: int = 0
    checked: int = 0
    skipped: int = 0
    not_attempted: int = 0
    quota_before: dict[str, Any] | None = None
    quota_after: dict[str, Any] | None = None
    status: str = "planned"
    limitations: list[str] = field(default_factory=list)
    errors: int = 0
    error_items: list[dict[str, Any]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape consumed by the CLI and the run journal."""
        payload: dict[str, Any] = {
            "provider": self.provider,
            "property_id": self.property_id,
            "planned": self.planned,
            "submitted": self.submitted,
            "checked": self.checked,
            "skipped": self.skipped,
            "not_attempted": self.not_attempted,
            "quota_before": self.quota_before,
            "quota_after": self.quota_after,
            "status": self.status,
            "limitations": _unique(self.limitations),
            "errors": self.errors,
        }
        if self.error_items:
            payload["error_items"] = self.error_items[:20]
        if self.detail:
            payload["detail"] = self.detail
        return payload


# -- queue -----------------------------------------------------------------------------------


def build_queue(
    config: SeoConfig,
    *,
    from_sitemap: bool = False,
    sitemap_url: str = "",
    urls: Sequence[str] | None = None,
    fetch: Callable[[str], FetchResult] | None = None,
) -> Queue:
    """Build the same-host URL queue from a sitemap and/or explicit URLs.

    A sitemap is read when ``from_sitemap`` is set, when ``sitemap_url`` is supplied, or when
    no explicit URL is given (the scheduled command relies on this default). The sitemap
    defaults to ``<project.base_url>/sitemap.xml`` and is bounded to 10,000 URLs and six
    documents. Explicit URLs are merged, deduplicated and filtered to the same host.
    """
    base_url = config.project.base_url.strip().rstrip("/")
    explicit = _clean_urls(urls or (), name="urls")
    host = _host_of(base_url)
    if not host and explicit:
        host = _host_of(explicit[0])
    if not host:
        raise SeoError(
            code=CONFIG_INVALID,
            message="project.base_url is not configured; the URL queue host is unknown",
            next_action="set project.base_url in seo.toml or pass explicit --url values",
        )

    on_host: list[str] = []
    off_host = 0
    for url in explicit:
        if _host_of(url) == host:
            on_host.append(url)
        else:
            off_host += 1

    use_sitemap = from_sitemap or bool(sitemap_url.strip()) or not explicit
    limitations: list[str] = []
    errors: list[str] = []
    sitemap_target = ""
    sitemap_items: list[QueueItem] = []
    sitemap_truncated = False
    sitemap_off_host = 0
    if use_sitemap:
        sitemap_target = _sitemap_target(sitemap_url, base_url)
        sitemap_items, errors, sitemap_truncated, sitemap_off_host = _read_sitemap(
            sitemap_target, host=host, fetch=fetch
        )
        off_host += sitemap_off_host
        if not sitemap_items and not on_host:
            if errors:
                raise SeoError(
                    code=UPSTREAM_UNAVAILABLE,
                    message="could not read the sitemap: " + ", ".join(sorted(set(errors))),
                    next_action="check the sitemap URL, or pass explicit --url values instead",
                    retryable=True,
                )
            raise SeoError(
                code=INVALID_INPUT,
                message=f"the sitemap has no URLs for {host}",
                next_action="check the sitemap or pass explicit --url values",
            )

    merged: list[QueueItem] = []
    seen: set[str] = set()
    for item in [*(QueueItem(url) for url in on_host), *sitemap_items]:
        if item.url in seen:
            continue
        seen.add(item.url)
        merged.append(item)
    total = len(merged)
    items = merged[:QUEUE_LIMIT]
    truncated = sitemap_truncated or total > len(items)
    if sitemap_truncated:
        limitations.append(SITEMAP_TRUNCATED)
    if off_host:
        limitations.append(SITEMAP_OFF_HOST)
    if errors:
        limitations.append(SITEMAP_ERRORS)
    if total > len(items):
        limitations.append(QUEUE_CAP)
    return Queue(
        items=items,
        sitemap_url=sitemap_target,
        total=total,
        truncated=truncated,
        errors=errors,
        off_host=off_host,
        limitations=limitations,
    )


def _read_sitemap(
    sitemap_url: str,
    *,
    host: str,
    fetch: Callable[[str], FetchResult] | None,
) -> tuple[list[QueueItem], list[str], bool, int]:
    seed = urlsplit(sitemap_url)
    seed_origin = (seed.scheme.lower(), seed.netloc.lower())
    wanted = host.lower()

    def in_scope(url: str) -> bool:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() == wanted:
            return True
        return (parts.scheme.lower(), parts.netloc.lower()) == seed_origin

    fetcher: Fetcher | None = None
    try:
        if fetch is None:
            fetcher = Fetcher()
            fetch = lambda url: fetcher.fetch(url, max_bytes=SITEMAP_MAX_BYTES)  # noqa: E731
        reader = SitemapReader(
            fetch,
            allow_url=in_scope,
            max_urls=QUEUE_LIMIT,
            max_depth=1,
            max_docs=SITEMAP_DOCS,
        )
        doc = reader.read(sitemap_url)
    finally:
        if fetcher is not None:
            fetcher.close()
    items = [
        QueueItem(url=url, lastmod=parse_timestamp(doc.lastmod.get(url)))
        for url in doc.urls
        if _host_of(url) == wanted
    ]
    off_host = sum(1 for url in doc.urls if _host_of(url) != wanted)
    return items, list(doc.errors), doc.truncated, off_host


def _sitemap_target(explicit: str, base_url: str) -> str:
    value = explicit.strip()
    if value:
        return _absolute_url(value, name="sitemap_url")
    if not base_url:
        raise SeoError(
            code=CONFIG_INVALID,
            message="project.base_url is not configured; the default sitemap URL is unknown",
            next_action="set project.base_url in seo.toml or pass --sitemap-url",
        )
    return _absolute_url(f"{base_url}/sitemap.xml", name="sitemap_url")


def _clean_urls(values: Sequence[str], *, name: str) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        url = _absolute_url(str(value), name=name)
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def _absolute_url(value: str, *, name: str) -> str:
    url = value.strip()
    parts = urlsplit(url)
    if not url or parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an absolute http(s) URL",
            next_action=f"pass a URL such as https://example.com/page to {name}",
            detail={"value": url},
        )
    return url


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


# -- priority and planning -------------------------------------------------------------------


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 or date-only value into an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _state_stamp(state: Mapping[str, Any] | None, key: str) -> datetime | None:
    if not state:
        return None
    return parse_timestamp(state.get(key))


def _priority(
    item: QueueItem,
    state: Mapping[str, Any] | None,
    *,
    now: datetime,
    check_interval_days: int,
) -> tuple[int, datetime, str]:
    """Priority bucket, oldest relevant timestamp, URL (deterministic tie-break)."""
    last_submitted = _state_stamp(state, "last_submitted_at")
    last_checked = _state_stamp(state, "last_checked_at")
    if last_submitted is None:
        return (0, _epoch(), item.url)
    if item.lastmod is not None and item.lastmod > last_submitted:
        return (1, last_submitted, item.url)
    interval = timedelta(days=max(0, check_interval_days))
    if last_checked is None or now - last_checked >= interval:
        return (2, last_checked or _epoch(), item.url)
    return (3, last_submitted, item.url)


def _epoch() -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC)


def _sorted_candidates(
    items: Sequence[QueueItem],
    states: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
    check_interval_days: int,
) -> list[QueueItem]:
    return sorted(
        items,
        key=lambda item: _priority(
            item,
            states.get(item.url),
            now=now,
            check_interval_days=check_interval_days,
        ),
    )


def _submission_candidates(
    queue: Sequence[QueueItem],
    states: Mapping[str, Mapping[str, Any]],
    *,
    force: bool,
    now: datetime,
    check_interval_days: int,
) -> tuple[list[QueueItem], list[str]]:
    """Candidates in priority order plus URLs skipped as already submitted."""
    candidates: list[QueueItem] = []
    skipped: list[str] = []
    for item in queue:
        state = states.get(item.url)
        last_submitted = _state_stamp(state, "last_submitted_at")
        if (
            not force
            and last_submitted is not None
            and (item.lastmod is None or last_submitted >= item.lastmod)
        ):
            skipped.append(item.url)
            continue
        candidates.append(item)
    return (
        _sorted_candidates(
            candidates,
            states,
            now=now,
            check_interval_days=check_interval_days,
        ),
        skipped,
    )


def _check_candidates(
    queue: Sequence[QueueItem],
    states: Mapping[str, Mapping[str, Any]],
    *,
    force: bool,
    now: datetime,
    check_interval_days: int,
    submitted_only: bool,
) -> tuple[list[QueueItem], list[str]]:
    """URLs due for an index check plus URLs skipped as checked recently."""
    interval = timedelta(days=max(0, check_interval_days))
    candidates: list[QueueItem] = []
    skipped: list[str] = []
    for item in queue:
        state = states.get(item.url)
        if submitted_only and _state_stamp(state, "last_submitted_at") is None:
            continue
        last_checked = _state_stamp(state, "last_checked_at")
        if not force and last_checked is not None and now - last_checked < interval:
            skipped.append(item.url)
            continue
        candidates.append(item)
    return (
        _sorted_candidates(
            candidates,
            states,
            now=now,
            check_interval_days=check_interval_days,
        ),
        skipped,
    )


def plan_provider(
    provider: str,
    property_id: str,
    queue: Sequence[QueueItem],
    states: Mapping[str, Mapping[str, Any]],
    *,
    config: IndexingConfig,
    now: datetime,
    force: bool,
    quota_before: Mapping[str, Any] | None,
    quota_remaining: int | None,
    inspect_remaining: int | None = None,
    max_submit: int | None = None,
    max_inspect: int | None = None,
    limitations: Sequence[str] = (),
) -> ProviderPlan:
    """Plan one provider: priority order, idempotency skip and quota caps.

    ``quota_remaining`` is the provider-reported (Bing/Yandex) or locally computed
    (IndexNow) number of submissions available now. ``None`` means the quota could not be
    read; the planner then refuses to submit blind. ``inspect_remaining`` is the local GSC
    inspection budget.
    """
    submit_cap = config.max_submit_per_run if max_submit is None else max_submit
    inspect_cap = config.max_inspect_per_run if max_inspect is None else max_inspect
    submit_cap = max(0, min(submit_cap, QUEUE_LIMIT))
    inspect_cap = max(0, min(inspect_cap, QUEUE_LIMIT))
    plan = ProviderPlan(
        provider=provider,
        property_id=property_id,
        quota_before=dict(quota_before) if quota_before is not None else None,
        limitations=list(limitations),
    )
    skipped_urls: set[str] = set()
    not_attempted_urls: set[str] = set()
    if provider in {"bing", "yandex", "indexnow"}:
        candidates, skipped = _submission_candidates(
            queue,
            states,
            force=force,
            now=now,
            check_interval_days=config.check_interval_days,
        )
        skipped_urls.update(skipped)
        allowed = submit_cap
        if quota_remaining is None:
            allowed = 0
            if candidates:
                plan.limitations.append(QUOTA_UNAVAILABLE)
        else:
            allowed = min(allowed, max(0, quota_remaining))
        plan.submit = [item.url for item in candidates[:allowed]]
        not_attempted_urls.update(item.url for item in candidates[allowed:])
        if candidates and not plan.submit:
            plan.status = "quota_exhausted"
            plan.limitations.append(QUOTA_EXHAUSTED)

    if provider in {"gsc", "bing"}:
        candidates, skipped = _check_candidates(
            queue,
            states,
            force=force,
            now=now,
            check_interval_days=config.check_interval_days,
            submitted_only=provider == "bing",
        )
        skipped_urls.update(skipped)
        allowed = inspect_cap
        if provider == "gsc":
            if inspect_remaining is None:
                allowed = 0
                if candidates:
                    plan.limitations.append(CHECK_QUOTA_EXHAUSTED)
            else:
                allowed = min(allowed, max(0, inspect_remaining))
        plan.check = [item.url for item in candidates[:allowed]]
        not_attempted_urls.update(item.url for item in candidates[allowed:])
        if candidates and not plan.check:
            plan.status = "quota_exhausted"
            plan.limitations.append(CHECK_QUOTA_EXHAUSTED)

    plan.skipped = len(skipped_urls)
    plan.not_attempted = len(not_attempted_urls)
    plan.limitations = _unique([*plan.limitations, *_PROVIDER_LIMITATIONS.get(provider, ())])
    return plan


def gsc_index_status(verdict: object, coverage_state: object) -> str:
    """Map a GSC URL Inspection verdict to ``indexed``, ``not_indexed`` or ``unknown``.

    Only ``PASS`` and ``Submitted and indexed`` are indexed. ``NEUTRAL`` and ``FAIL`` are
    not indexed. A missing or unknown verdict stays ``unknown`` instead of guessing.
    """
    raw_verdict = str(verdict or "").strip().upper()
    coverage = str(coverage_state or "").strip().lower()
    if raw_verdict == "PASS" or _INDEXED_COVERAGE in coverage:
        return "indexed"
    if raw_verdict in {"NEUTRAL", "FAIL"}:
        return "not_indexed"
    return "unknown"


# -- engine ----------------------------------------------------------------------------------


class IndexingEngine:
    """Run the indexing pipeline for one workspace.

    ``client_for`` and ``close_client`` are injected by :class:`SeoService` so secrets and
    test overrides stay in one place. ``store`` is optional only for a status read; a run
    needs it and returns ``CONFIG_INVALID`` without one.
    """

    def __init__(
        self,
        *,
        config: SeoConfig,
        store: Store | None,
        targets: Sequence[IndexingTarget],
        client_for: Callable[[str], object],
        close_client: Callable[[str, object], None] | None = None,
        allow_write: bool = False,
        force: bool = False,
        max_submit: int | None = None,
        max_inspect: int | None = None,
        write_secret: str = "",
    ) -> None:
        self._config = config
        self._indexing = config.indexing
        self._store = store
        self._targets = list(targets)
        self._client_for = client_for
        self._close_client = close_client
        self._allow_write = bool(allow_write)
        self._force = bool(force)
        self._max_submit = max_submit
        self._max_inspect = max_inspect
        self._write_secret = write_secret if isinstance(write_secret, str) else ""
        self._clients: dict[str, object] = {}

    # -- status -----------------------------------------------------------------------------

    def status(
        self, *, provider: str = "all", limit: int = 50, live: bool = False
    ) -> dict[str, Any]:
        """Read stored state and (optionally) live quotas without submitting anything."""
        targets = self._select_targets(provider)
        entries: dict[str, Any] = {}
        limitations: list[str] = []
        total = 0
        truncated = False
        ages: list[int] = []
        for target in targets:
            entry, entry_limits, entry_total, entry_truncated, age = self._status_target(
                target, limit=limit, live=live
            )
            entries[target.provider] = entry
            limitations.extend(entry_limits)
            total += entry_total
            truncated = truncated or entry_truncated
            ages.append(age)
        secret_present = bool(self._write_secret.strip())
        if not secret_present:
            limitations.append(WRITE_SECRET_MISSING)
        return {
            "providers": entries,
            "write_secret_present": secret_present,
            "limitations": _unique(limitations),
            "total": total,
            "truncated": truncated,
            "data_age_days": max(ages, default=0),
        }

    def _status_target(
        self, target: IndexingTarget, *, limit: int, live: bool
    ) -> tuple[dict[str, Any], list[str], int, bool, int]:
        limits = list(_PROVIDER_LIMITATIONS.get(target.provider, ()))
        if not target.ready:
            limits.append(target.reason)
            return (
                _empty_status_entry(target, quota=None, limitations=limits),
                limits,
                0,
                False,
                0,
            )
        store = self._store
        counts = (
            store.indexing_state_counts(
                target.provider,
                target.property_id,
                stale_before=_days_ago(self._indexing.stale_days),
            )
            if store is not None
            else {
                "urls": 0,
                "submitted": 0,
                "inspected": 0,
                "indexed": 0,
                "not_indexed": 0,
                "crawled": 0,
                "unknown": 0,
                "queued": 0,
                "stale": 0,
                "pending": 0,
            }
        )
        rows = (
            store.list_indexing_state(
                provider=target.provider,
                property_id=target.property_id,
                limit=max(1, limit),
            )
            if store is not None
            else []
        )
        quota: dict[str, Any] | None
        try:
            quota, quota_limits, _ = self._status_quota(target, live=live)
        except SeoError as exc:
            quota = None
            quota_limits = [QUOTA_UNAVAILABLE]
            limits.append(f"{target.provider}: quota read failed ({exc.code})")
        limits.extend(quota_limits)
        if not live and target.provider in {"bing", "yandex"}:
            limits.append(QUOTA_NOT_LIVE)
        indexed_total: int | None = None
        indexed_total_source: str | None = None
        if target.provider == "gsc":
            try:
                indexed_total, gsc_limits = self._gsc_indexed_total(target)
            except SeoError as exc:
                gsc_limits = [GSC_INDEXED_COUNT_DEPRECATED]
                limits.append(f"gsc: sitemap indexed total read failed ({exc.code})")
            limits.extend(gsc_limits)
            indexed_total_source = "gsc_sitemaps_contents"
        age = max(
            (
                _date_age(
                    parse_timestamp(row.get("last_checked_at"))
                    or parse_timestamp(row.get("last_submitted_at"))
                )
                for row in rows
            ),
            default=0,
        )
        entry = {
            **_empty_status_entry(target, quota=quota, limitations=_unique(limits)),
            **counts,
            "indexed_total": indexed_total,
            "indexed_total_source": indexed_total_source,
            "rows": rows,
            "data_age_days": age,
        }
        return entry, _unique(limits), int(counts["urls"]), len(rows) < int(counts["urls"]), age

    def _status_quota(
        self, target: IndexingTarget, *, live: bool
    ) -> tuple[dict[str, Any] | None, list[str], int]:
        if target.provider in {"gsc", "indexnow"}:
            quota = self._local_quota(target.provider, target.property_id)
            return quota, [], 0
        if not live:
            return self._stored_quota(target.provider), [], 0
        client = self._client(target)
        try:
            if target.provider == "bing":
                row, remaining, limitations, warnings = _read_bing_quota(client)
                del warnings
                return (
                    {
                        "source": "provider",
                        "remaining": remaining,
                        "reset": None,
                        **row,
                    },
                    _unique([*limitations, BING_QUOTA_RESET_UNDOCUMENTED]),
                    0,
                )
            row, remaining, limitations, warnings = _read_yandex_quota(client, target.property_id)
            del warnings
            return (
                {
                    "source": "provider",
                    "remaining": remaining,
                    "reset": "provider_midnight",
                    **row,
                },
                limitations,
                0,
            )
        finally:
            self._close(target)

    def _stored_quota(self, provider: str) -> dict[str, Any] | None:
        if self._store is None:
            return None
        runs = self._store.list_indexing_runs(provider=provider, limit=1)
        if not runs:
            return None
        quota = runs[0].get("quota_after")
        if not isinstance(quota, dict) or not quota:
            return None
        stored = dict(quota)
        stored.setdefault(
            "remaining",
            _min_remaining(
                stored.get("daily_remaining"),
                stored.get("monthly_remaining"),
                stored.get("quota_remainder"),
            ),
        )
        if provider == "bing":
            stored.setdefault("reset", None)
        elif provider == "yandex":
            stored.setdefault("reset", "provider_midnight")
        return stored

    def _local_quota(self, provider: str, property_id: str) -> dict[str, Any]:
        kind = "inspect" if provider == "gsc" else "submit"
        limit = (
            self._indexing.gsc_inspect_daily_limit
            if provider == "gsc"
            else self._indexing.indexnow_daily_limit
        )
        today = utc_now().date()
        used = (
            self._store.get_quota_usage(provider, property_id, today, kind)
            if self._store is not None
            else 0
        )
        return {
            "source": "local",
            "kind": kind,
            "day": today.isoformat(),
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
            "reset": _utc_midnight(today),
        }

    # -- run --------------------------------------------------------------------------------

    def run(self, queue: Queue, *, provider: str = "all") -> dict[str, Any]:
        """Plan every selected provider and apply submissions only with ``allow_write``."""
        if self._store is None:
            raise SeoError(
                code=CONFIG_INVALID,
                message="store is not configured; indexing state cannot be recorded",
                next_action="run `lv24-seo init` so the data directory exists, then retry",
            )
        if self._allow_write and not self._write_secret.strip():
            raise SeoError(
                code=WRITE_DISABLED,
                message="write secret is empty: set LV24_SEO_WRITE_SECRET",
                next_action=(
                    "set LV24_SEO_WRITE_SECRET to a long random value before using "
                    "--allow-write; never pass it on the command line"
                ),
                retryable=False,
            )
        targets = self._select_targets(provider)
        outcomes: dict[str, Any] = {}
        for target in targets:
            started = utc_now()
            run_id = self._store.insert_indexing_run(
                target.provider, started_at=started, status="running"
            )
            try:
                outcome = self._run_target(target, queue)
            except SeoError as exc:
                outcome = ProviderOutcome(
                    provider=target.provider,
                    property_id=target.property_id,
                    status="error",
                    limitations=_unique(
                        [*_PROVIDER_LIMITATIONS.get(target.provider, ()), exc.code]
                    ),
                    errors=1,
                    error_items=[{"url": None, "code": exc.code, "message": exc.message}],
                )
            except Exception:
                logger.warning("indexing: unexpected failure for %s", target.provider)
                outcome = ProviderOutcome(
                    provider=target.provider,
                    property_id=target.property_id,
                    status="error",
                    limitations=_unique(
                        [*_PROVIDER_LIMITATIONS.get(target.provider, ()), "internal_error"]
                    ),
                    errors=1,
                )
            self._store.finish_indexing_run(
                run_id,
                status=outcome.status,
                planned=outcome.planned,
                submitted=outcome.submitted,
                checked=outcome.checked,
                skipped=outcome.skipped,
                quota_before=outcome.quota_before,
                quota_after=outcome.quota_after,
                detail={
                    "not_attempted": outcome.not_attempted,
                    "errors": outcome.errors,
                    "error_items": outcome.error_items[:20],
                    **outcome.detail,
                },
            )
            outcomes[target.provider] = outcome.to_dict()
        limitations = _unique(
            [item for outcome in outcomes.values() for item in outcome.get("limitations", [])]
        )
        return {
            "providers": outcomes,
            "queue": {
                "total": queue.total,
                "selected": len(queue.items),
                "sitemap_url": queue.sitemap_url or None,
                "truncated": queue.truncated,
                "errors": queue.errors,
                "off_host": queue.off_host,
            },
            "limitations": limitations,
            "dry_run": not self._allow_write,
        }

    def _run_target(self, target: IndexingTarget, queue: Queue) -> ProviderOutcome:
        limitations = list(_PROVIDER_LIMITATIONS.get(target.provider, ()))
        outcome = ProviderOutcome(
            provider=target.provider,
            property_id=target.property_id,
            limitations=limitations,
        )
        if not target.ready:
            outcome.status = "not_configured"
            outcome.limitations = _unique([*limitations, target.reason])
            return outcome
        store = self._store
        if store is None:  # pragma: no cover - run() rejects a missing store
            raise SeoError(
                code=CONFIG_INVALID,
                message="store is not configured; indexing state cannot be recorded",
                next_action="run `lv24-seo init` so the data directory exists, then retry",
            )
        states = {
            str(row["url"]): row
            for row in store.list_indexing_state(
                provider=target.provider,
                property_id=target.property_id,
                limit=QUEUE_LIMIT,
            )
        }
        with store.atomic():
            for item in queue.items:
                store.ensure_indexing_state(
                    target.provider,
                    target.property_id,
                    item.url,
                    next_check_at=utc_now(),
                )
        try:
            quota_before, quota_remaining, quota_limits, _ = self._quota_before(target)
        except SeoError as exc:
            quota_before = None
            quota_remaining = None
            quota_limits = [QUOTA_UNAVAILABLE]
            limitations.append(f"{target.provider}: quota read failed ({exc.code})")
        limitations.extend(quota_limits)
        inspect_remaining = self._inspect_remaining(target)
        plan = plan_provider(
            target.provider,
            target.property_id,
            queue.items,
            states,
            config=self._indexing,
            now=utc_now(),
            force=self._force,
            quota_before=quota_before,
            quota_remaining=quota_remaining,
            inspect_remaining=inspect_remaining,
            max_submit=self._max_submit,
            max_inspect=self._max_inspect,
            limitations=limitations,
        )
        outcome.quota_before = plan.quota_before
        outcome.skipped = plan.skipped
        outcome.not_attempted = plan.not_attempted
        outcome.limitations = _unique(plan.limitations)
        outcome.planned = len(plan.submit) if target.provider != "gsc" else len(plan.check)
        if plan.status == "quota_exhausted":
            outcome.status = "quota_exhausted"

        checked, check_errors, check_items = self._run_checks(target, plan, queue)
        outcome.checked = checked
        outcome.errors += check_errors
        outcome.error_items.extend(check_items)

        submitted = 0
        if self._allow_write and plan.submit:
            submit_outcome = self._run_submit(target, plan)
            submitted = submit_outcome["submitted"]
            outcome.submitted = submitted
            outcome.errors += submit_outcome["errors"]
            outcome.error_items.extend(submit_outcome["error_items"])
            outcome.detail.update(submit_outcome["detail"])
            if submit_outcome.get("not_attempted"):
                outcome.not_attempted += int(submit_outcome["not_attempted"])
            outcome.limitations = _unique([*outcome.limitations, *submit_outcome["limitations"]])
            if submitted and outcome.errors:
                outcome.status = "partial"
            elif outcome.errors and not submitted:
                outcome.status = "error"
            elif outcome.status != "quota_exhausted":
                outcome.status = "applied"
        elif not self._allow_write:
            outcome.status = "planned" if plan.status != "quota_exhausted" else "quota_exhausted"
            outcome.detail["dry_run"] = True
        elif not plan.submit and not plan.check and outcome.status != "quota_exhausted":
            outcome.status = "skipped" if plan.skipped else "noop"
        if target.provider == "gsc" and self._allow_write and checked:
            outcome.status = "checked"

        try:
            quota_after, _, after_limits, _ = self._quota_before(target)
        except SeoError as exc:
            quota_after = None
            after_limits = [QUOTA_UNAVAILABLE]
            outcome.detail["quota_after_error"] = exc.code
        outcome.quota_after = quota_after
        outcome.limitations = _unique([*outcome.limitations, *after_limits])
        if outcome.status == "quota_exhausted":
            outcome.limitations = _unique([*outcome.limitations, QUOTA_EXHAUSTED])
        return outcome

    def _run_checks(
        self, target: IndexingTarget, plan: ProviderPlan, queue: Queue
    ) -> tuple[int, int, list[dict[str, Any]]]:
        if target.provider not in {"gsc", "bing"} or not plan.check:
            return 0, 0, []
        client = self._client(target)
        checked = 0
        errors = 0
        error_items: list[dict[str, Any]] = []
        now = utc_now()
        next_check = now + timedelta(days=max(0, self._indexing.check_interval_days))
        try:
            for url in plan.check:
                try:
                    if target.provider == "gsc":
                        checked += self._check_gsc(
                            client, target, url, now=now, next_check=next_check
                        )
                    else:
                        checked += self._check_bing(
                            client, target, url, now=now, next_check=next_check
                        )
                except SeoError as exc:
                    errors += 1
                    error_items.append({"url": url, "code": exc.code, "message": exc.message})
                    self._store.upsert_indexing_state(  # type: ignore[union-attr]
                        target.provider,
                        target.property_id,
                        url,
                        detail={"check_error": {"code": exc.code, "message": exc.message}},
                    )
                    if exc.code == QUOTA_EXCEEDED:
                        break
        finally:
            self._close(target)
        return checked, errors, error_items

    def _check_gsc(
        self,
        client: object,
        target: IndexingTarget,
        url: str,
        *,
        now: datetime,
        next_check: datetime,
    ) -> int:
        result = _call(client, "inspect_url", url, language_code="en-US")
        rows = _result_rows(result)
        row = rows[0] if rows else {}
        status = gsc_index_status(row.get("verdict"), row.get("coverage_state"))
        last_crawled = parse_timestamp(row.get("last_crawl_time"))
        self._store.upsert_indexing_state(  # type: ignore[union-attr]
            target.provider,
            target.property_id,
            url,
            last_checked_at=now,
            index_status=status,
            coverage_state=_text_or_none(row.get("coverage_state")),
            last_crawled_at=last_crawled,
            next_check_at=next_check,
            detail={
                "verdict": row.get("verdict"),
                "coverage_state": row.get("coverage_state"),
                "indexing_state": row.get("indexing_state"),
                "page_fetch_state": row.get("page_fetch_state"),
                "last_crawl_time": row.get("last_crawl_time"),
            },
        )
        if self._store is not None:
            self._store.increment_quota_usage("gsc", target.property_id, now.date(), "inspect")
        return 1

    def _check_bing(
        self,
        client: object,
        target: IndexingTarget,
        url: str,
        *,
        now: datetime,
        next_check: datetime,
    ) -> int:
        result = _call(client, "url_info", url)
        rows = _result_rows(result)
        row = rows[0] if rows else {}
        last_crawled = parse_timestamp(row.get("last_crawled_date"))
        self._store.upsert_indexing_state(  # type: ignore[union-attr]
            target.provider,
            target.property_id,
            url,
            last_checked_at=now,
            index_status="crawled" if last_crawled is not None else "unknown",
            http_status=_int_or_none(row.get("http_status")),
            last_crawled_at=last_crawled,
            next_check_at=next_check,
            detail={
                "http_status": row.get("http_status"),
                "last_crawled_date": row.get("last_crawled_date"),
                "discovery_date": row.get("discovery_date"),
                "limitations": _result_limitations(result),
            },
        )
        return 1

    def _run_submit(self, target: IndexingTarget, plan: ProviderPlan) -> dict[str, Any]:
        if target.provider == "bing":
            return self._submit_bing(target, plan)
        if target.provider == "yandex":
            return self._submit_yandex(target, plan)
        if target.provider == "indexnow":
            return self._submit_indexnow(target, plan)
        return {
            "submitted": 0,
            "errors": 0,
            "error_items": [],
            "limitations": [],
            "detail": {},
        }

    def _submit_bing(self, target: IndexingTarget, plan: ProviderPlan) -> dict[str, Any]:
        try:
            urls, replan_limits = self._replan_for_submit(target, plan.submit)
        except SeoError as exc:
            self._mark_error(target, plan.submit, exc)
            return {
                "submitted": 0,
                "errors": len(plan.submit),
                "error_items": [
                    {"url": url, "code": exc.code, "message": exc.message} for url in plan.submit
                ],
                "limitations": [exc.code],
                "detail": {},
            }
        client = cast(Any, self._client(target))
        try:
            gate = self._gate()
            write_plan = self._write_plan(target, urls)
            token = gate.plan(write_plan).confirm_token
            result = gate.apply(
                write_plan,
                token,
                idempotency_key=self._idempotency_key(target.provider),
                execute=lambda _plan: client.submit_url_batches(urls, batch_size=BING_BATCH_LIMIT),
            )
            if result.status == "skipped":
                return {
                    "submitted": 0,
                    "errors": 0,
                    "error_items": [],
                    "limitations": ["write_gate_duplicate"],
                    "detail": {"skipped": "duplicate"},
                }
        except SeoError as exc:
            accepted = _int_or_none(exc.detail.get("submitted")) or 0
            accepted = min(max(0, accepted), len(urls))
            now = utc_now()
            next_check = now + timedelta(days=max(0, self._indexing.check_interval_days))
            for url in urls[:accepted]:
                self._store.upsert_indexing_state(  # type: ignore[union-attr]
                    target.provider,
                    target.property_id,
                    url,
                    last_submitted_at=now,
                    submit_status="accepted",
                    next_check_at=next_check,
                    detail={"accepted": True, "partial_batch": True},
                )
            failed = urls[accepted:]
            self._mark_error(target, failed, exc)
            return {
                "submitted": accepted,
                "errors": len(failed),
                "error_items": [
                    {"url": url, "code": exc.code, "message": exc.message} for url in failed
                ],
                "limitations": [*replan_limits, exc.code],
                "detail": {"partial_batches": accepted > 0},
            }
        finally:
            self._close(target)
        now = utc_now()
        next_check = now + timedelta(days=max(0, self._indexing.check_interval_days))
        for url in urls:
            self._store.upsert_indexing_state(  # type: ignore[union-attr]
                target.provider,
                target.property_id,
                url,
                last_submitted_at=now,
                submit_status="accepted",
                next_check_at=next_check,
                detail={"accepted": True, "batches": _batch_count(len(urls), BING_BATCH_LIMIT)},
            )
        if self._store is not None:
            self._store.increment_quota_usage(
                "bing", target.property_id, now.date(), "submit", len(urls)
            )
        return {
            "submitted": len(urls),
            "errors": 0,
            "error_items": [],
            "limitations": [*replan_limits, "submit_idempotency_undocumented"],
            "detail": {"batches": _batch_count(len(urls), BING_BATCH_LIMIT)},
        }

    def _submit_yandex(self, target: IndexingTarget, plan: ProviderPlan) -> dict[str, Any]:
        try:
            urls, replan_limits = self._replan_for_submit(target, plan.submit)
        except SeoError as exc:
            self._mark_error(target, plan.submit, exc)
            return {
                "submitted": 0,
                "errors": len(plan.submit),
                "error_items": [
                    {"url": url, "code": exc.code, "message": exc.message} for url in plan.submit
                ],
                "limitations": [exc.code],
                "detail": {},
                "not_attempted": 0,
            }
        client = self._client(target)
        queued = 0
        already = 0
        errors = 0
        error_items: list[dict[str, Any]] = []
        limitations: list[str] = [ONE_URL_PER_CALL, *replan_limits]
        not_attempted = 0
        now = utc_now()
        next_check = now + timedelta(days=max(0, self._indexing.check_interval_days))
        try:
            gate = self._gate()
            write_plan = self._write_plan(target, urls)

            def execute(_plan: WritePlan) -> dict[str, Any]:
                nonlocal queued, already, errors, not_attempted
                for index, url in enumerate(urls):
                    try:
                        result = _call(client, "recrawl_enqueue", target.property_id, url)
                    except SeoError as exc:
                        if exc.code == QUOTA_EXCEEDED:
                            limitations.append(RECRAWL_QUOTA_EXCEEDED)
                            not_attempted = len(urls) - index
                            break
                        errors += 1
                        error_items.append({"url": url, "code": exc.code, "message": exc.message})
                        self._mark_error(target, [url], exc)
                        continue
                    rows = _result_rows(result)
                    row = rows[0] if rows else {}
                    status = (
                        "already_queued" if row.get("status") == "already_queued" else "accepted"
                    )
                    if status == "already_queued":
                        already += 1
                    else:
                        queued += 1
                    self._store.upsert_indexing_state(  # type: ignore[union-attr]
                        target.provider,
                        target.property_id,
                        url,
                        last_submitted_at=now,
                        submit_status=status,
                        next_check_at=next_check,
                        detail={
                            "task_id": row.get("task_id"),
                            "quota_remainder": row.get("quota_remainder"),
                        },
                    )
                return {"queued": queued, "already_queued": already, "errors": errors}

            token = gate.plan(write_plan).confirm_token
            result = gate.apply(
                write_plan,
                token,
                idempotency_key=self._idempotency_key(target.provider),
                execute=execute,
            )
            if result.status == "skipped":
                return {
                    "submitted": 0,
                    "errors": 0,
                    "error_items": [],
                    "limitations": ["write_gate_duplicate"],
                    "detail": {"skipped": "duplicate"},
                    "not_attempted": 0,
                }
        except SeoError as exc:
            self._mark_error(target, urls, exc)
            return {
                "submitted": 0,
                "errors": len(urls),
                "error_items": [
                    {"url": url, "code": exc.code, "message": exc.message} for url in urls
                ],
                "limitations": [*limitations, exc.code],
                "detail": {},
                "not_attempted": 0,
            }
        finally:
            self._close(target)
        return {
            "submitted": queued + already,
            "errors": errors,
            "error_items": error_items,
            "limitations": limitations,
            "detail": {"queued": queued, "already_queued": already},
            "not_attempted": not_attempted,
        }

    def _submit_indexnow(self, target: IndexingTarget, plan: ProviderPlan) -> dict[str, Any]:
        bing = self._config.providers.bing
        key = bing.indexnow_key.strip() if bing is not None else ""
        key_location = bing.indexnow_key_location.strip() if bing is not None else ""
        if not key:
            return {
                "submitted": 0,
                "errors": len(plan.submit),
                "error_items": [
                    {"url": url, "code": "AUTH_REQUIRED", "message": "IndexNow key is missing"}
                    for url in plan.submit
                ],
                "limitations": ["indexnow_key_missing"],
                "detail": {},
            }
        location = key_location or f"https://{target.property_id}/{key}.txt"
        try:
            validate_indexnow_key_location(
                key_location=location, host=target.property_id, urls=plan.submit
            )
        except SeoError as exc:
            self._mark_error(target, plan.submit, exc)
            return {
                "submitted": 0,
                "errors": len(plan.submit),
                "error_items": [
                    {"url": url, "code": exc.code, "message": exc.message} for url in plan.submit
                ],
                "limitations": [exc.code],
                "detail": {},
            }
        try:
            urls, replan_limits = self._replan_for_submit(target, plan.submit)
        except SeoError as exc:
            self._mark_error(target, plan.submit, exc)
            return {
                "submitted": 0,
                "errors": len(plan.submit),
                "error_items": [
                    {"url": url, "code": exc.code, "message": exc.message} for url in plan.submit
                ],
                "limitations": [exc.code],
                "detail": {},
            }
        try:
            gate = self._gate()
            write_plan = self._write_plan(target, urls)
            token = gate.plan(write_plan).confirm_token
            result = gate.apply(
                write_plan,
                token,
                idempotency_key=self._idempotency_key(target.provider),
                execute=lambda _plan: indexnow_submit(
                    host=target.property_id,
                    key=key,
                    urls=urls,
                    key_location=location,
                ),
            )
            if result.status == "skipped":
                return {
                    "submitted": 0,
                    "errors": 0,
                    "error_items": [],
                    "limitations": ["write_gate_duplicate"],
                    "detail": {"skipped": "duplicate"},
                }
        except SeoError as exc:
            self._mark_error(target, urls, exc)
            return {
                "submitted": 0,
                "errors": len(urls),
                "error_items": [
                    {"url": url, "code": exc.code, "message": exc.message} for url in urls
                ],
                "limitations": [*replan_limits, exc.code],
                "detail": {},
            }
        now = utc_now()
        next_check = now + timedelta(days=max(0, self._indexing.check_interval_days))
        for url in urls:
            self._store.upsert_indexing_state(  # type: ignore[union-attr]
                target.provider,
                target.property_id,
                url,
                last_submitted_at=now,
                submit_status="accepted",
                next_check_at=next_check,
                detail={"accepted": True},
            )
        if self._store is not None:
            self._store.increment_quota_usage(
                "indexnow", target.property_id, now.date(), "submit", len(urls)
            )
        return {
            "submitted": len(urls),
            "errors": 0,
            "error_items": [],
            "limitations": [*replan_limits, INDEXNOW_NO_STATUS_API],
            "detail": {},
        }

    # -- quota ------------------------------------------------------------------------------

    def _quota_before(
        self, target: IndexingTarget
    ) -> tuple[dict[str, Any] | None, int | None, list[str], int]:
        if target.provider in {"gsc", "indexnow"}:
            quota = self._local_quota(target.provider, target.property_id)
            return quota, int(quota["remaining"]), [], 0
        client = self._client(target)
        try:
            if target.provider == "bing":
                row, remaining, limitations, _ = _read_bing_quota(client)
                return (
                    {
                        "source": "provider",
                        "remaining": remaining,
                        "reset": None,
                        **row,
                    },
                    remaining,
                    _unique([*limitations, BING_QUOTA_RESET_UNDOCUMENTED]),
                    0,
                )
            row, remaining, limitations, _ = _read_yandex_quota(client, target.property_id)
            return (
                {
                    "source": "provider",
                    "remaining": remaining,
                    "reset": "provider_midnight",
                    **row,
                },
                remaining,
                limitations,
                0,
            )
        finally:
            self._close(target)

    def _inspect_remaining(self, target: IndexingTarget) -> int | None:
        if target.provider != "gsc":
            return None
        quota = self._local_quota("gsc", target.property_id)
        return int(quota["remaining"])

    # -- write gate -------------------------------------------------------------------------

    def _gate(self) -> WriteGate:
        return WriteGate(
            allow_write=True,
            secret=self._write_secret,
            store=self._store,
        )

    def _write_plan(self, target: IndexingTarget, urls: Sequence[str]) -> WritePlan:
        action = {
            "bing": "bing_submit",
            "yandex": "recrawl",
            "indexnow": "indexnow",
        }.get(target.provider, "bing_submit")
        payload: dict[str, Any] = {"urls": list(urls)}
        if target.provider == "bing":
            payload.update({"site": target.property_id, "batch_size": BING_BATCH_LIMIT})
        elif target.provider == "yandex":
            payload.update({"host_id": target.property_id})
        elif target.provider == "indexnow":
            payload.update({"host": target.property_id})
        return WritePlan(
            action=action,
            target=target.property_id,
            summary=(f"submit {len(urls)} URL(s) to {target.provider} for {target.property_id}"),
            payload=payload,
        )

    def _replan_for_submit(
        self, target: IndexingTarget, planned: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        """Re-read the quota and verify the reviewed URL set still fits it.

        The engine plans, then checks, then re-reads the quota immediately before a write.
        Rebuilding the plan from the reviewed URLs keeps the second decision small and makes
        the hash check meaningful: if the remaining quota shrank, the fresh plan differs and
        ``CONFIRM_REQUIRED`` stops the write instead of silently sending a different set.
        """
        fresh_quota, remaining, quota_limits, _ = self._quota_before(target)
        fresh_plan = plan_provider(
            target.provider,
            target.property_id,
            [QueueItem(url) for url in planned],
            {},
            config=self._indexing,
            now=utc_now(),
            force=self._force,
            quota_before=fresh_quota,
            quota_remaining=remaining,
            inspect_remaining=self._inspect_remaining(target),
            max_submit=self._max_submit,
            max_inspect=self._max_inspect,
            limitations=quota_limits,
        )
        self._verify_plan_hash(target, planned, fresh_plan.submit)
        return fresh_plan.submit, fresh_plan.limitations

    def _verify_plan_hash(
        self, target: IndexingTarget, planned: Sequence[str], fresh: Sequence[str]
    ) -> None:
        before = plan_hash(self._write_plan(target, planned))
        after = plan_hash(self._write_plan(target, fresh))
        if before != after:
            raise SeoError(
                code=CONFIRM_REQUIRED,
                message="the indexing plan changed while quotas were re-read; nothing was sent",
                next_action="rerun the plan to review the new URL set",
                retryable=False,
            )

    def _idempotency_key(self, provider: str) -> str:
        day = utc_now().date().isoformat()
        if self._force:
            return f"indexing:{provider}:{day}:force:{utc_now().isoformat(timespec='seconds')}"
        return f"indexing:{provider}:{day}"

    def _mark_error(self, target: IndexingTarget, urls: Sequence[str], exc: SeoError) -> None:
        for url in urls:
            self._store.upsert_indexing_state(  # type: ignore[union-attr]
                target.provider,
                target.property_id,
                url,
                submit_status="error",
                detail={"error": {"code": exc.code, "message": exc.message}},
            )

    # -- targets ----------------------------------------------------------------------------

    def _select_targets(self, provider: str) -> list[IndexingTarget]:
        wanted = provider.strip().lower()
        if wanted == "all":
            selected = [
                target for target in self._targets if target.provider in self._indexing.providers
            ]
        else:
            if wanted not in PROVIDERS:
                raise SeoError(
                    code=INVALID_INPUT,
                    message=f"unknown indexing provider {provider!r}",
                    next_action="use --provider all, bing, yandex, gsc or indexnow",
                )
            selected = [target for target in self._targets if target.provider == wanted]
        if not selected:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"provider {wanted!r} is not enabled in [indexing].providers",
                next_action="add it to [indexing].providers in seo.toml",
            )
        return selected

    def _close(self, target: IndexingTarget) -> None:
        client = self._clients.pop(target.client_name, None)
        if client is not None and self._close_client is not None and target.client_name:
            self._close_client(target.client_name, client)

    def _client(self, target: IndexingTarget) -> object:
        if target.client_name not in self._clients:
            self._clients[target.client_name] = self._client_for(target.client_name)
        return self._clients[target.client_name]

    def _gsc_indexed_total(self, target: IndexingTarget) -> tuple[int, list[str]]:
        """Sum GSC ``sitemaps[].contents[].indexed``; the field is deprecated.

        The count is an aggregate the provider still returns, not a per-URL verdict. It is
        reported separately from ``indexing_state`` and always carries the deprecation note.
        """
        client = self._client(target)
        try:
            result = _call(client, "list_sitemaps")
        finally:
            self._close(target)
        rows = _result_rows(result)
        total = 0
        for row in rows:
            contents = row.get("contents")
            if not isinstance(contents, list):
                continue
            for item in contents:
                if isinstance(item, Mapping):
                    value = _int_or_none(item.get("indexed"))
                    if value is not None:
                        total += value
        return total, _unique([GSC_INDEXED_COUNT_DEPRECATED, *_result_limitations(result)])


# -- provider helpers ------------------------------------------------------------------------


def _read_bing_quota(
    client: object,
) -> tuple[dict[str, Any], int | None, list[str], list[str]]:
    result = _call(client, "submission_quota")
    rows = _result_rows(result)
    row = dict(rows[0]) if rows else {}
    remaining = _min_remaining(row.get("daily_remaining"), row.get("monthly_remaining"))
    return row, remaining, _result_limitations(result), _result_warnings(result)


def _read_yandex_quota(
    client: object, host_id: str
) -> tuple[dict[str, Any], int | None, list[str], list[str]]:
    result = _call(client, "recrawl_quota", host_id)
    rows = _result_rows(result)
    row = dict(rows[0]) if rows else {}
    remainder = _int_or_none(row.get("quota_remainder"))
    return row, remainder, _result_limitations(result), _result_warnings(result)


def _min_remaining(*values: object) -> int | None:
    numbers = [_int_or_none(value) for value in values]
    known = [value for value in numbers if value is not None]
    return min(known) if known else None


def _empty_status_entry(
    target: IndexingTarget,
    *,
    quota: dict[str, Any] | None,
    limitations: list[str],
) -> dict[str, Any]:
    return {
        "provider": target.provider,
        "property_id": target.property_id,
        "urls": 0,
        "submitted": 0,
        "inspected": 0,
        "indexed": 0,
        "not_indexed": 0,
        "crawled": 0,
        "unknown": 0,
        "queued": 0,
        "pending": 0,
        "stale": 0,
        "indexed_total": None,
        "indexed_total_source": None,
        "quota": quota,
        "limitations": _unique(limitations),
        "rows": [],
        "data_age_days": 0,
    }


def _call(client: object, method: str, *args: object, **kwargs: object) -> object:
    function = getattr(client, method, None)
    if not callable(function):
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"provider client does not implement {method}",
            retryable=False,
        )
    return function(*args, **kwargs)


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


def _result_limitations(result: object) -> list[str]:
    raw = getattr(result, "limitations", ())
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw] if isinstance(raw, Sequence) else []


def _result_warnings(result: object) -> list[str]:
    raw = getattr(result, "warnings", ())
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw] if isinstance(raw, Sequence) else []


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _batch_count(count: int, size: int) -> int:
    return 0 if count <= 0 else (count + size - 1) // size


def _days_ago(days: int) -> datetime:
    return utc_now() - timedelta(days=max(0, days))


def _utc_midnight(day: date) -> str:
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC).isoformat(
        timespec="seconds"
    )


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _date_age(value: datetime | None, today: date | None = None) -> int:
    if value is None:
        return 0
    current = today or utc_now().date()
    return max(0, (current - value.date()).days)
