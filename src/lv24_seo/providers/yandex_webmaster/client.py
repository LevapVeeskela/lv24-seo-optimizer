"""Yandex Webmaster API v4 read adapter.

Yandex Webmaster is the free source of Yandex query and indexing data, but it is lossy in ways
the caller must see:

* query reports cover at most the top 3,000 queries and never join a query to a page;
* reporting lags about four days and can return ``HOST_NOT_LOADED`` during maintenance;
* per-URL sitemap error detail is not exposed by the API at all.

This module is read-only except for :meth:`YandexWebmasterClient.recrawl_enqueue`, which is a
provider write and must stay behind ``core/write_gate.py``. The queue read methods
(:meth:`recrawl_queue`, :meth:`recrawl_task`, :meth:`recrawl_quota`) are safe reads.

Degradation policy
------------------
A host-scoped call that finds no loaded data returns an empty :class:`ProviderResult` with
``warnings=("host_not_loaded",)`` and :data:`DEGRADED_HOST` instead of raising: a new or
temporarily unavailable host is a data state, not a tool failure. Yandex answers ``404`` both
for an unknown ``host_id`` and for a host that is not loaded yet, and the shared HTTP layer does
not expose error bodies, so a host-scoped ``404`` is also reported as degraded.

An *empty* indexing or search-URL history is different: the host answered, so the list is
genuinely empty. Those results carry :data:`~lv24_seo.providers.base.EMPTY_HISTORY` and
``total=0`` instead of pretending the host is down.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from lv24_seo.core.errors import (
    AUTH_INVALID,
    AUTH_REQUIRED,
    INVALID_INPUT,
    PROPERTY_NOT_FOUND,
    QUOTA_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import (
    CLIENT_SIDE_DATE_FILTER,
    DEGRADED_HOST,
    EMPTY_HISTORY,
    NO_PER_URL_SITEMAP_ERRORS,
    NO_QUERY_PAGE_JOIN,
    ONE_URL_PER_CALL,
    TOP_3000_QUERIES,
    ProviderResult,
    Row,
    data_age_days,
    now_utc,
)

__all__ = ["YandexWebmasterClient"]

SOURCE = "yandex_webmaster"
QUERY_REPORT_LAG_DAYS = 4
LOGIN_NEXT_ACTION = "lv24-seo auth login --provider yandex"
HOST_NOT_LOADED_WARNING = "host_not_loaded"

#: Documented ``ApiQueryIndicator`` values for ``search-queries/popular``.
_QUERY_INDICATORS = (
    "TOTAL_SHOWS",
    "TOTAL_CLICKS",
    "AVG_SHOW_POSITION",
    "AVG_CLICK_POSITION",
)
#: Indicators with a stable, documented row key; anything else is kept in ``extra_indicators``.
_QUERY_INDICATOR_KEYS = {
    "TOTAL_SHOWS": "shows",
    "TOTAL_CLICKS": "clicks",
    "AVG_SHOW_POSITION": "position",
    "AVG_CLICK_POSITION": "avg_click_position",
}
_SUMMARY_NUMERIC_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pages_in_search", ("searchable_pages_count", "pages_in_search")),
    ("pages_excluded", ("excluded_pages_count", "pages_excluded")),
    ("http_2xx", ("http_2xx", "HTTP_2XX")),
    ("http_3xx", ("http_3xx", "HTTP_3XX")),
    ("http_4xx", ("http_4xx", "HTTP_4XX")),
    ("http_5xx", ("http_5xx", "HTTP_5XX")),
)
#: Documented ``IndexingStatusEnum`` series returned by ``indexing/history``.
_INDEXING_INDICATOR_KEYS = {
    "HTTP_2XX": "http_2xx",
    "HTTP_3XX": "http_3xx",
    "HTTP_4XX": "http_4xx",
    "HTTP_5XX": "http_5xx",
    "OTHER": "other",
}
#: ``search-urls/events/history`` series: pages entering or leaving the Yandex index.
_EVENT_INDICATOR_KEYS = {
    "APPEARED_IN_SEARCH": "appeared",
    "REMOVED_FROM_SEARCH": "removed",
}
#: ``search-urls/in-search/history`` series.
_IN_SEARCH_INDICATOR_KEYS = {
    "SEARCHABLE": "searchable",
    "EXCLUDED": "excluded",
    "SEARCHABLE_PAGES": "searchable",
    "EXCLUDED_PAGES": "excluded",
}
#: ``search-queries/all/history`` and ``search-queries/{query_id}/history`` series.
_QUERY_HISTORY_INDICATOR_KEYS = {
    "TOTAL_SHOWS": "shows",
    "TOTAL_CLICKS": "clicks",
    "AVG_SHOW_POSITION": "position",
    "AVG_CLICK_POSITION": "avg_click_position",
}
#: ``indexing/samples`` and ``search-urls/events/samples`` page at most 100 rows.
_SAMPLE_PAGE_LIMIT = 100
_SAMPLE_MAX_ROWS = 1000
_SITEMAP_MAX_LIMIT = 500
_QUEUE_MAX_LIMIT = 500
_REPORT_DATE_KEYS = ("date_to", "end_date", "date", "last_update", "updated_at")
_HOST_NOT_LOADED = "HOST_NOT_LOADED"


class YandexWebmasterClient:
    """Synchronous client for the read-only Yandex Webmaster v4 endpoints.

    Pass a shared :class:`HttpClient` to reuse its connection pool (the client then does not own
    it); pass nothing to create and own a private one for this adapter.
    """

    def __init__(
        self,
        *,
        token: str,
        http: HttpClient | None = None,
        base_url: str = "https://api.webmaster.yandex.net/v4",
    ) -> None:
        raw_token: object = token
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise SeoError(
                code=AUTH_REQUIRED,
                message="Yandex Webmaster token is missing",
                next_action=LOGIN_NEXT_ACTION,
            )

        self._base_url = base_url.strip().rstrip("/")
        self._owns_http = http is None
        self._http = http if http is not None else HttpClient()
        self._headers = {
            "Authorization": f"OAuth {raw_token.strip()}",
            "Accept": "application/json",
        }
        self._user_id: int | None = None

    def close(self) -> None:
        """Close the HTTP pool when this adapter created it."""
        if self._owns_http:
            self._http.close()

    def user(self) -> ProviderResult[Row]:
        """Resolve the authenticated account and cache its ``user_id``."""
        fetched_at = now_utc()
        user_id = _user_id_from(self._get("/user/"))
        if user_id is None:
            raise _unexpected_payload("/user/")
        self._user_id = user_id
        return _result(property_id="user", rows=[{"user_id": user_id}], fetched_at=fetched_at)

    def hosts(self) -> ProviderResult[Row]:
        """List the hosts verified for the authenticated account."""
        fetched_at = now_utc()
        user_id = self._ensure_user_id()
        payload = self._get(f"/user/{user_id}/hosts/")

        rows: list[Row] = []
        for item in _rows_from(payload, "hosts"):
            row = _host_row(item)
            if row is not None:
                rows.append(row)
        return _result(property_id=f"user:{user_id}", rows=rows, fetched_at=fetched_at)

    def summary(self, host_id: str) -> ProviderResult[Row]:
        """SQI and indexed-page counters for one host."""
        scope = _require_host_id(host_id)
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "summary/")
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        row = _summary_row(payload)
        if row is None:
            return _degraded(scope, fetched_at)
        return _result(property_id=scope, rows=[row], fetched_at=fetched_at)

    def indexing_history(
        self,
        host_id: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """Daily indexing buckets (date plus HTTP status counters).

        The documented payload is ``{"indicators": {"HTTP_2XX": [{"date", "value"}], ...}}``
        with the ``IndexingStatusEnum`` series ``HTTP_2XX``/``HTTP_3XX``/``HTTP_4XX``/
        ``HTTP_5XX``/``OTHER``. Series are merged into one row per date; an indicator the
        provider omitted stays absent instead of being zero-filled. The older
        ``history``/``indexing_history`` list form is still accepted unchanged.

        ``date_from``/``date_to`` are forwarded as the documented ``YYYY-MM-DD`` window.

        An empty history means the host is loaded but has no daily buckets yet; that is an
        empty answer with the :data:`~lv24_seo.providers.base.EMPTY_HISTORY` limitation, not a
        degraded host. Only a ``404`` or an explicit ``HOST_NOT_LOADED`` payload is degraded.
        """
        scope = _require_host_id(host_id)
        params = _date_params(date_from, date_to)
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "indexing/history/"),
            params=params or None,
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        rows = _indexing_rows(payload)
        if not rows:
            return _empty_history(scope, fetched_at)
        return _result(property_id=scope, rows=rows, fetched_at=fetched_at, total=len(rows))

    def search_urls_events(
        self,
        host_id: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """History of search-URL events (pages entering or leaving the index).

        The current v4 payload is ``{"indicators": {"APPEARED_IN_SEARCH": [{"date", "value"}],
        "REMOVED_FROM_SEARCH": [...]}}``. The series are merged into one row per date
        (``date``, ``appeared``, ``removed``); an omitted series stays absent and an unknown
        indicator is kept under ``extra_indicators``. The legacy ``events`` list form is still
        accepted unchanged.

        An empty event history is a real answer: the host is loaded and nothing was recorded in
        the window. It is reported with the ``EMPTY_HISTORY`` limitation, never as a degraded
        host.
        """
        scope = _require_host_id(host_id)
        params = _date_params(date_from, date_to)
        fetched_at = now_utc()
        filtered_client_side = False
        try:
            payload, missing = self._get_host_scoped(
                _host_path(self._ensure_user_id(), scope, "search-urls/events/history/"),
                params=params or None,
            )
        except SeoError as exc:
            # Live 2026-11-17: this endpoint closes the connection when date_from/date_to are
            # present, although the documented contract lists them. Retry once without the
            # window and filter the returned series locally; the warning says so.
            if not params or exc.code != UPSTREAM_UNAVAILABLE:
                raise
            payload, missing = self._get_host_scoped(
                _host_path(self._ensure_user_id(), scope, "search-urls/events/history/")
            )
            filtered_client_side = True
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        rows = _events_rows(payload)
        if filtered_client_side:
            rows = _filter_rows_by_date(rows, date_from, date_to)
        if not rows:
            return _empty_history(scope, fetched_at)
        return _result(
            property_id=scope,
            rows=rows,
            fetched_at=fetched_at,
            total=len(rows),
            limitations=(CLIENT_SIDE_DATE_FILTER,) if filtered_client_side else (),
            warnings=(CLIENT_SIDE_DATE_FILTER,) if filtered_client_side else (),
        )

    def indexing_samples(
        self,
        host_id: str,
        *,
        limit: int = _SAMPLE_PAGE_LIMIT,
        offset: int = 0,
    ) -> ProviderResult[Row]:
        """Indexing samples (``status``, ``http_code``, ``url``, ``access_date``).

        The endpoint pages at 100 rows; ``limit`` caps the returned rows and ``offset`` is the
        first provider row. The payload's ``count`` is the grand total, so the result reports
        an honest ``total``/``truncated`` pair instead of pretending the page is complete.
        """
        return self._sample_rows(
            host_id,
            "indexing/samples",
            limit=limit,
            offset=offset,
        )

    def search_event_samples(
        self,
        host_id: str,
        *,
        limit: int = _SAMPLE_PAGE_LIMIT,
        offset: int = 0,
    ) -> ProviderResult[Row]:
        """Per-URL search event samples (the endpoint behind ``events/history``).

        Rows carry ``url``, ``title``, ``event_date``, ``last_access``, ``event``,
        ``excluded_url_status``, ``bad_http_status`` and ``target_url``. The provider caps a
        page at 100 rows; ``count`` is the grand total.
        """
        return self._sample_rows(
            host_id,
            "search-urls/events/samples",
            limit=limit,
            offset=offset,
        )

    def sitemaps(
        self,
        host_id: str,
        *,
        parent_id: str | None = None,
        limit: int | None = None,
        from_: str | None = None,
    ) -> ProviderResult[Row]:
        """List the host's sitemaps (or the children of one sitemap index).

        ``parent_id`` selects the children of a sitemap index, ``limit`` bounds the page and
        ``from_`` is the provider's ``from`` cursor (a sitemap id). This is a read: adding or
        deleting sitemaps is not implemented.
        """
        scope = _require_host_id(host_id)
        params: dict[str, Any] = {}
        if parent_id is not None and parent_id.strip():
            params["parent_id"] = parent_id.strip()
        if limit is not None:
            params["limit"] = _require_bounded_limit(
                limit,
                name="limit",
                maximum=_SITEMAP_MAX_LIMIT,
            )
        if from_ is not None and from_.strip():
            params["from"] = from_.strip()

        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "sitemaps/"),
            params=params or None,
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        rows = [_sitemap_row(item) for item in _rows_from(payload, "sitemaps")]
        reported = _count_total(payload)
        return _result(
            property_id=scope,
            rows=rows,
            fetched_at=fetched_at,
            total=reported if reported is not None else len(rows),
            truncated=(reported is not None and reported > len(rows)),
        )

    def sitemap(self, host_id: str, sitemap_id: str) -> ProviderResult[Row]:
        """Read one sitemap by its provider id (read-only)."""
        scope = _require_host_id(host_id)
        target = _require_non_empty(sitemap_id, name="sitemap_id")
        fetched_at = now_utc()
        payload = self._get(
            _host_path(
                self._ensure_user_id(),
                scope,
                f"sitemaps/{quote(target, safe='')}",
            )
        )
        if _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)
        body = _mapping(payload)
        item = _mapping(body.get("sitemap")) or body
        if not item:
            raise _unexpected_payload("sitemaps/{id}")
        return _result(
            property_id=scope,
            rows=[_sitemap_row(item)],
            fetched_at=fetched_at,
            total=1,
        )

    def sqi_history(
        self,
        host_id: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """SQI points as ``{date, value}`` rows (the documented ``points`` list)."""
        return self._series_history(
            host_id,
            "sqi-history/",
            key_map={},
            date_from=date_from,
            date_to=date_to,
            points=True,
        )

    def in_search_history(
        self,
        host_id: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """Daily pages-in-search series (searchable/excluded) from ``search-urls/in-search``."""
        return self._series_history(
            host_id,
            "search-urls/in-search/history/",
            key_map=_IN_SEARCH_INDICATOR_KEYS,
            date_from=date_from,
            date_to=date_to,
        )

    def all_queries_history(
        self,
        host_id: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """Daily totals across all queries (shows, clicks and average positions)."""
        return self._series_history(
            host_id,
            "search-queries/all/history/",
            key_map=_QUERY_HISTORY_INDICATOR_KEYS,
            date_from=date_from,
            date_to=date_to,
            extra_params={"query_indicator": list(_QUERY_INDICATORS)},
        )

    def query_history(
        self,
        host_id: str,
        query_id: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """Daily indicators for one query id (the same series as ``all/history``)."""
        target = _require_non_empty(query_id, name="query_id")
        return self._series_history(
            host_id,
            f"search-queries/{quote(target, safe='')}/history/",
            key_map=_QUERY_HISTORY_INDICATOR_KEYS,
            date_from=date_from,
            date_to=date_to,
            extra_params={"query_indicator": list(_QUERY_INDICATORS)},
        )

    def recrawl_queue(
        self,
        host_id: str,
        *,
        offset: int = 0,
        limit: int = _SAMPLE_PAGE_LIMIT,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """Read one page of the recrawl queue (``tasks`` plus the reported ``count``)."""
        scope = _require_host_id(host_id)
        start = _require_offset(offset)
        size = _require_bounded_limit(limit, name="limit", maximum=_QUEUE_MAX_LIMIT)
        params: dict[str, Any] = {"offset": start, "limit": size}
        params.update(_date_params(date_from, date_to))
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "recrawl/queue"),
            params=params,
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        rows = [_queue_row(item) for item in _rows_from(payload, "tasks")]
        reported = _count_total(payload)
        return _result(
            property_id=scope,
            rows=rows,
            fetched_at=fetched_at,
            total=reported if reported is not None else len(rows),
            truncated=(reported is not None and reported > len(rows)),
        )

    def recrawl_task(self, host_id: str, task_id: str) -> ProviderResult[Row]:
        """Read one recrawl task by id (read-only)."""
        scope = _require_host_id(host_id)
        target = _require_non_empty(task_id, name="task_id")
        fetched_at = now_utc()
        payload = self._get(
            _host_path(
                self._ensure_user_id(),
                scope,
                f"recrawl/queue/{quote(target, safe='')}",
            )
        )
        if _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)
        body = _mapping(payload)
        item = _mapping(body.get("task")) or body
        if not item:
            raise _unexpected_payload("recrawl/queue/{id}")
        return _result(property_id=scope, rows=[_queue_row(item)], fetched_at=fetched_at, total=1)

    def recrawl_enqueue(self, host_id: str, url: str) -> ProviderResult[Row]:
        """Queue one URL for recrawl (provider write; the caller owns the write gate).

        Yandex has no batch endpoint: one request queues one URL. ``202`` returns the
        ``task_id`` and the remaining daily quota, ``409`` means the URL is already queued and
        ``429`` is a quota rejection. The 429 error carries ``daily_quota`` and
        ``exceeded_until`` so the caller can stop the loop honestly.
        """
        scope = _require_host_id(host_id)
        target = _require_http_url(url)
        path = _host_path(self._ensure_user_id(), scope, "recrawl/queue")
        response = self._http.request(
            "POST",
            f"{self._base_url}{path}",
            json={"url": target},
            headers=self._headers,
            rate_limit_key="yandex_webmaster",
            raise_for_status=False,
        )
        status = response.status_code
        if status >= 500:
            # A gateway timeout/5xx may carry an empty body; parsing it as JSON would
            # misreport a transient provider failure as a malformed payload.
            raise SeoError(
                code=UPSTREAM_UNAVAILABLE,
                message=f"recrawl/queue upstream HTTP {status}",
                retryable=True,
                detail={"status_code": status},
            )
        try:
            body = _response_json(response, path="recrawl/queue")
            if status in (200, 202):
                task_id = _text(body.get("task_id"))
                if task_id is None:
                    raise _unexpected_payload("recrawl/queue")
                row: Row = {
                    "url": target,
                    "status": "queued",
                    "task_id": task_id,
                    "quota_remainder": _first_number(body, ("quota_remainder", "quota")),
                }
                return _result(
                    property_id=scope,
                    rows=[row],
                    fetched_at=now_utc(),
                    limitations=(ONE_URL_PER_CALL,),
                )
            if status == 409:
                row = {
                    "url": target,
                    "status": "already_queued",
                    "error_code": _error_code(body),
                }
                return _result(
                    property_id=scope,
                    rows=[row],
                    fetched_at=now_utc(),
                    limitations=(ONE_URL_PER_CALL,),
                )
            raise _enqueue_error(status, body, target)
        finally:
            response.close()

    def _sample_rows(
        self,
        host_id: str,
        suffix: str,
        *,
        limit: int,
        offset: int,
    ) -> ProviderResult[Row]:
        """Walk the documented 100-row sample pages up to ``limit`` rows."""
        scope = _require_host_id(host_id)
        max_rows = _require_bounded_limit(limit, name="limit", maximum=_SAMPLE_MAX_ROWS)
        position = _require_offset(offset)
        fetched_at = now_utc()
        rows: list[Row] = []
        total: int | None = None

        while len(rows) < max_rows:
            request_limit = min(_SAMPLE_PAGE_LIMIT, max_rows - len(rows))
            payload, missing = self._get_host_scoped(
                _host_path(self._ensure_user_id(), scope, suffix),
                params={"offset": position, "limit": request_limit},
            )
            if missing or _is_host_not_loaded(payload):
                return _degraded(scope, fetched_at)
            if total is None:
                total = _count_total(payload)
            batch = _copy_rows(_rows_from(payload, "samples"))
            if not batch:
                break
            rows.extend(batch[:request_limit])
            if len(batch) < request_limit:
                break
            position += request_limit
            if total is not None and position >= total:
                break

        truncated = (total is not None and len(rows) < total) or (
            total is None and len(rows) >= max_rows
        )
        return _result(
            property_id=scope,
            rows=rows,
            fetched_at=fetched_at,
            total=total if total is not None else len(rows),
            truncated=truncated,
        )

    def _series_history(
        self,
        host_id: str,
        suffix: str,
        *,
        key_map: Mapping[str, str],
        date_from: date | None,
        date_to: date | None,
        points: bool = False,
        extra_params: Mapping[str, Any] | None = None,
    ) -> ProviderResult[Row]:
        scope = _require_host_id(host_id)
        params = _date_params(date_from, date_to)
        if extra_params:
            params.update(extra_params)
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, suffix),
            params=params or None,
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)
        rows = _points_rows(payload) if points else _indicator_rows(payload, key_map)
        if not rows:
            return _empty_history(scope, fetched_at)
        return _result(property_id=scope, rows=rows, fetched_at=fetched_at, total=len(rows))

    def diagnostics(self, host_id: str) -> ProviderResult[Row]:
        """Flattened host problems: severity, code, state and last state update.

        An empty list means no problems reported, not a degraded host. The API exposes no per-URL
        sitemap error detail, so that limitation is attached only when a sitemap problem exists.
        """
        scope = _require_host_id(host_id)
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "diagnostics/")
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        body = _mapping(payload)
        problems = body.get("problems", body.get("diagnostics"))
        if not isinstance(problems, (Mapping, list)):
            raise _unexpected_payload("diagnostics/")
        values = problems.values() if isinstance(problems, Mapping) else problems
        if any(not isinstance(value, Mapping) for value in values):
            raise _unexpected_payload("diagnostics/")
        rows = [_diagnostic_row(item) for item in _rows_from(payload, "problems", "diagnostics")]
        limitations = (
            (NO_PER_URL_SITEMAP_ERRORS,) if any(_is_sitemap_problem(row) for row in rows) else ()
        )
        return _result(
            property_id=scope,
            rows=rows,
            fetched_at=fetched_at,
            limitations=limitations,
        )

    def popular_queries(
        self,
        host_id: str,
        *,
        limit: int = 500,
        order_by: str = "TOTAL_SHOWS",
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[Row]:
        """Top queries for the host, ordered by ``order_by``.

        Indicators are requested as repeated ``query_indicator`` parameters, not a comma list.
        The report is capped by Yandex at 3,000 queries and has no query-to-page join.
        """
        scope = _require_host_id(host_id)
        page_limit = _require_limit(limit)
        ordering = _require_order_by(order_by)
        if date_from is not None and not isinstance(date_from, date):
            raise SeoError(code=INVALID_INPUT, message="date_from must be a date")
        if date_to is not None and not isinstance(date_to, date):
            raise SeoError(code=INVALID_INPUT, message="date_to must be a date")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise SeoError(code=INVALID_INPUT, message="date_from must not be after date_to")
        fetched_at = now_utc()

        rows: list[Row] = []
        total: int | None = None
        offset = 0
        # The report covers at most the top 3,000 queries: bound the page walk
        # even if the server keeps returning full batches.
        max_rows = 3000
        while len(rows) < max_rows:
            request_limit = min(page_limit, max_rows - len(rows))
            params: dict[str, Any] = {
                "order_by": ordering,
                "query_indicator": list(_QUERY_INDICATORS),
                "limit": request_limit,
                "offset": offset,
            }
            if date_from is not None:
                params["date_from"] = date_from.isoformat()
            if date_to is not None:
                params["date_to"] = date_to.isoformat()
            payload, missing = self._get_host_scoped(
                _host_path(self._ensure_user_id(), scope, "search-queries/popular/"),
                params=params,
            )
            if missing or _is_host_not_loaded(payload):
                return _degraded(scope, fetched_at)

            if total is None:
                total = _reported_total(payload)
            batch = [_query_row(item) for item in _rows_from(payload, "queries")]
            if not batch:
                break
            rows.extend(batch[:request_limit])
            if len(batch) < request_limit:
                break
            offset += request_limit
            if total is not None and len(rows) >= total:
                rows = rows[:total]
                break

        reported = total
        return _result(
            property_id=scope,
            rows=rows,
            fetched_at=fetched_at,
            data_age=_query_report_age(payload, fetched_at),
            total=reported,
            truncated=(reported is not None and reported > len(rows))
            or (reported is None and len(rows) >= max_rows),
            limitations=(TOP_3000_QUERIES, NO_QUERY_PAGE_JOIN),
        )

    def recrawl_quota(self, host_id: str) -> ProviderResult[Row]:
        """Read the remaining daily recrawl quota (the queue write is gated elsewhere)."""
        scope = _require_host_id(host_id)
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "recrawl/quota")
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        body = _mapping(payload)
        if not body:
            return _degraded(scope, fetched_at)
        row: Row = {
            "daily_quota": _first_number(body, ("daily_quota", "quota")),
            "quota_remainder": _first_number(body, ("quota_remainder", "remainder")),
            "raw": _copy_row(body),
        }
        return _result(property_id=scope, rows=[row], fetched_at=fetched_at)

    def _ensure_user_id(self) -> int:
        if self._user_id is None:
            self.user()
        user_id = self._user_id
        if user_id is None:  # pragma: no cover - user() caches on a valid payload
            raise _unexpected_payload("/user/")
        return user_id

    def _get(self, path: str, *, params: Mapping[str, Any] | None = None) -> object:
        payload: object = self._http.get_json(
            f"{self._base_url}{path}",
            params=params,
            headers=self._headers,
            rate_limit_key="yandex_webmaster",
        )
        return payload

    def _get_host_scoped(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[object | None, bool]:
        """GET a host-scoped path; ``(None, True)`` marks a ``404`` as a degraded host."""
        try:
            return self._get(path, params=params), False
        except SeoError as exc:
            if exc.code == PROPERTY_NOT_FOUND:
                return None, True
            raise


def _result(
    *,
    property_id: str,
    rows: list[Row],
    fetched_at: datetime,
    data_age: int = 0,
    total: int | None = None,
    truncated: bool = False,
    limitations: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> ProviderResult[Row]:
    return ProviderResult(
        source=SOURCE,
        property_id=property_id,
        fetched_at=fetched_at,
        data_age_days=data_age,
        rows=rows,
        total=total,
        truncated=truncated,
        limitations=limitations,
        warnings=warnings,
    )


def _degraded(property_id: str, fetched_at: datetime) -> ProviderResult[Row]:
    return ProviderResult(
        source=SOURCE,
        property_id=property_id,
        fetched_at=fetched_at,
        data_age_days=0,
        limitations=(DEGRADED_HOST,),
        warnings=(HOST_NOT_LOADED_WARNING,),
    )


def _empty_history(property_id: str, fetched_at: datetime) -> ProviderResult[Row]:
    """A loaded host with no recorded buckets: empty, complete and labelled as such."""
    return ProviderResult(
        source=SOURCE,
        property_id=property_id,
        fetched_at=fetched_at,
        data_age_days=0,
        rows=[],
        total=0,
        limitations=(EMPTY_HISTORY,),
    )


def _unexpected_payload(path: str) -> SeoError:
    return SeoError(
        code=UPSTREAM_UNAVAILABLE,
        message=f"unexpected Yandex Webmaster payload for {path}",
        retryable=False,
    )


def _response_json(response: httpx.Response, *, path: str) -> Mapping[str, Any]:
    """Decode a write response body, keeping only object payloads."""
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"invalid JSON response from {path}",
            retryable=False,
            detail={"status_code": response.status_code},
        ) from exc
    return _mapping(payload)


def _error_code(body: Mapping[str, Any]) -> str | None:
    """Provider error code from the top level or a nested ``error`` object."""
    for source in (body, _mapping(body.get("error"))):
        for key in ("error_code", "code"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _enqueue_error(status: int, body: Mapping[str, Any], url: str) -> SeoError:
    """Map a recrawl enqueue failure; 429 keeps the documented quota fields."""
    code = _error_code(body)
    if status == 429:
        quota = _mapping(body.get("error")) or body
        return SeoError(
            code=QUOTA_EXCEEDED,
            message=f"Yandex recrawl quota exceeded for {url}",
            next_action="wait for the daily quota to reset or queue fewer URLs",
            retryable=False,
            detail={
                "status_code": status,
                "error_code": code,
                "daily_quota": _first_number(quota, ("daily_quota", "quota")),
                "exceeded_until": _text(quota.get("exceeded_until")),
                "url": url,
            },
        )
    if status in (401, 403):
        return SeoError(
            code=AUTH_INVALID,
            message=f"Yandex Webmaster rejected the recrawl request (HTTP {status})",
            next_action=LOGIN_NEXT_ACTION,
            retryable=False,
            detail={"status_code": status, "error_code": code},
        )
    if status == 404:
        return SeoError(
            code=PROPERTY_NOT_FOUND,
            message="Yandex Webmaster host was not found for recrawl",
            retryable=False,
            detail={"status_code": status, "error_code": code},
        )
    if status >= 500:
        return SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"Yandex Webmaster is unavailable (HTTP {status})",
            retryable=True,
            detail={"status_code": status, "error_code": code},
        )
    return SeoError(
        code=INVALID_INPUT,
        message=f"Yandex Webmaster rejected the recrawl URL (HTTP {status})",
        next_action="check that the URL belongs to the configured host",
        retryable=False,
        detail={"status_code": status, "error_code": code, "url": url},
    )


def _require_host_id(host_id: object) -> str:
    if not isinstance(host_id, str) or not host_id.strip():
        raise SeoError(
            code=INVALID_INPUT,
            message="host_id must be a non-empty Yandex host id",
            next_action="run `lv24-seo yandex hosts` to list host ids",
        )
    return host_id.strip()


def _require_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise SeoError(
            code=INVALID_INPUT,
            message="limit must be an integer between 1 and 500",
            next_action="use a limit between 1 and 500",
        )
    return limit


def _require_bounded_limit(limit: object, *, name: str, maximum: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an integer between 1 and {maximum}",
            next_action=f"use {name} between 1 and {maximum}",
        )
    return limit


def _require_offset(offset: object) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise SeoError(
            code=INVALID_INPUT,
            message="offset must be a non-negative integer",
            next_action="use an offset of 0 or more",
        )
    return offset


def _require_non_empty(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be a non-empty string",
            next_action=f"pass a provider {name}",
        )
    return value.strip()


def _require_http_url(value: object) -> str:
    url = value.strip() if isinstance(value, str) else ""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SeoError(
            code=INVALID_INPUT,
            message="url must be an absolute http(s) URL",
            next_action="pass a URL such as https://example.com/page",
            detail={"value": url},
        )
    return url


def _date_params(date_from: date | None, date_to: date | None) -> dict[str, str]:
    """Validate the documented ``date_from``/``date_to`` window and render query params."""
    if date_from is not None and not isinstance(date_from, date):
        raise SeoError(code=INVALID_INPUT, message="date_from must be a date")
    if date_to is not None and not isinstance(date_to, date):
        raise SeoError(code=INVALID_INPUT, message="date_to must be a date")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise SeoError(code=INVALID_INPUT, message="date_from must not be after date_to")
    params: dict[str, str] = {}
    if date_from is not None:
        params["date_from"] = date_from.isoformat()
    if date_to is not None:
        params["date_to"] = date_to.isoformat()
    return params


def _require_order_by(order_by: object) -> str:
    if not isinstance(order_by, str) or order_by.strip() not in {"TOTAL_SHOWS", "TOTAL_CLICKS"}:
        raise SeoError(
            code=INVALID_INPUT,
            message="order_by must be TOTAL_SHOWS or TOTAL_CLICKS",
            next_action="use TOTAL_SHOWS or TOTAL_CLICKS",
        )
    return order_by.strip()


def _host_path(user_id: int, host_id: str, suffix: str) -> str:
    return f"/user/{user_id}/hosts/{quote(host_id, safe=':')}/{suffix}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows_from(payload: object, *container_keys: str) -> list[Mapping[str, Any]]:
    """Return object rows from a JSON array or from the first known container key.

    The ``diagnostics`` endpoint returns ``problems`` as an *object* whose keys
    are problem codes (``{"NO_SITEMAPS": {...}, ...}``), not a list, so a
    mapping container is expanded to its values. An empty object means no
    reported problems, not a degraded host.
    """
    if isinstance(payload, list):
        candidates: object = payload
    else:
        body = _mapping(payload)
        candidates = next((body[key] for key in container_keys if key in body), None)
    if isinstance(candidates, Mapping):
        candidates = [
            {**value, "code": str(key)}
            for key, value in candidates.items()
            if isinstance(value, Mapping)
        ]
    if not isinstance(candidates, list):
        return []
    return [
        {str(key): value for key, value in item.items()}
        for item in candidates
        if isinstance(item, Mapping)
    ]


def _copy_row(row: Mapping[str, Any]) -> Row:
    return {str(key): value for key, value in row.items()}


def _copy_rows(rows: list[Mapping[str, Any]]) -> list[Row]:
    return [_copy_row(row) for row in rows]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _first_number(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> int | float | None:
    for key in keys:
        value = _number(mapping.get(key))
        if value is not None:
            return value
    return None


def _user_id_from(payload: object) -> int | None:
    body = _mapping(payload)
    user_id = _as_int(body.get("user_id"))
    if user_id is None:
        user_id = _as_int(_mapping(body.get("user")).get("user_id"))
    return user_id


def _host_row(item: Mapping[str, Any]) -> Row | None:
    host_id = _text(item.get("host_id"))
    if host_id is None:
        return None
    return {
        "host_id": host_id,
        "ascii_host_url": _text(item.get("ascii_host_url")),
        "unicode_host_url": _text(item.get("unicode_host_url")),
        "verified": _flag(item.get("verified")),
    }


def _summary_row(payload: object) -> Row | None:
    body = _mapping(payload)
    nested = _mapping(body.get("summary"))
    if nested:
        body = nested

    row: Row = {}
    sqi = _number(body.get("sqi"))
    if sqi is not None:
        row["sqi"] = sqi
    for field_name, aliases in _SUMMARY_NUMERIC_FIELDS:
        value = _first_number(body, aliases)
        if value is not None:
            row[field_name] = value
    problems = body.get("site_problems")
    if isinstance(problems, Mapping):
        row["site_problems"] = _severity_counts(problems)
    return row or None


def _severity_counts(problems: Mapping[str, Any]) -> dict[str, int]:
    """Documented ``site_problems`` counters: severity → count, zero is a real value."""
    counts: dict[str, int] = {}
    for severity, raw in problems.items():
        count = _as_int(raw)
        if count is not None:
            counts[str(severity)] = count
    return dict(sorted(counts.items()))


def _indexing_rows(payload: object) -> list[Row]:
    """Rows from the documented indicator series, or the legacy list form unchanged."""
    body = _mapping(payload)
    indicators = body.get("indicators")
    if isinstance(indicators, Mapping):
        return _indexing_rows_from_indicators(indicators)
    return _copy_rows(_rows_from(payload, "history", "indexing_history"))


def _indexing_rows_from_indicators(indicators: Mapping[str, Any]) -> list[Row]:
    """Merge ``{"HTTP_2XX": [{"date", "value"}], ...}`` into one row per date.

    Only returned indicators are written: a missing series leaves its key absent, and a
    status the enum does not document is preserved under ``extra_indicators``.
    """
    return _indicator_rows_from_series(indicators, _INDEXING_INDICATOR_KEYS)


def _events_rows(payload: object) -> list[Row]:
    """Rows from the documented event indicator series, or the legacy list form unchanged."""
    body = _mapping(payload)
    indicators = body.get("indicators")
    if isinstance(indicators, Mapping):
        return _indicator_rows_from_series(indicators, _EVENT_INDICATOR_KEYS)
    return _copy_rows(_rows_from(payload, "events", "history", "search_urls_events"))


def _indicator_rows(payload: object, key_map: Mapping[str, str]) -> list[Row]:
    """Rows from ``indicators`` series, or the legacy list form unchanged."""
    body = _mapping(payload)
    indicators = body.get("indicators")
    if isinstance(indicators, Mapping):
        return _indicator_rows_from_series(indicators, key_map)
    return _copy_rows(_rows_from(payload, "history", "indicators"))


def _indicator_rows_from_series(
    indicators: Mapping[str, Any],
    key_map: Mapping[str, str],
) -> list[Row]:
    """Merge indicator series into one row per date, preserving unknown indicators.

    A missing series leaves its key absent (never zero-filled). Indicators the caller does
    not know are kept under ``extra_indicators`` so a new provider series is not dropped.
    """
    rows: dict[str, Row] = {}
    extra: dict[str, dict[str, int | float]] = {}
    for raw_name, series in indicators.items():
        name = str(raw_name)
        row_key = key_map.get(name.upper())
        if not isinstance(series, list):
            continue
        for entry in series:
            if not isinstance(entry, Mapping):
                continue
            date_text = _text(entry.get("date"))
            value = _number(entry.get("value"))
            if date_text is None or value is None:
                continue
            row = rows.setdefault(date_text, {"date": date_text})
            if row_key is not None:
                row[row_key] = value
            else:
                extra.setdefault(date_text, {})[name] = value
    for date_text, values in extra.items():
        rows[date_text]["extra_indicators"] = dict(sorted(values.items()))
    return [rows[date_text] for date_text in sorted(rows)]


def _points_rows(payload: object) -> list[Row]:
    """``{"points": [{"date", "value"}]}`` → one row per date."""
    rows: list[Row] = []
    for item in _rows_from(payload, "points", "history", "sqi_history"):
        date_text = _text(item.get("date"))
        value = _number(item.get("value"))
        if date_text is None or value is None:
            continue
        rows.append({"date": date_text, "value": value})
    return sorted(rows, key=lambda row: str(row.get("date", "")))


def _filter_rows_by_date(
    rows: list[Row],
    date_from: date | None,
    date_to: date | None,
) -> list[Row]:
    """Filter rows by the ``YYYY-MM-DD`` prefix of their provider date string."""
    if date_from is None and date_to is None:
        return rows
    start = date_from.isoformat() if date_from is not None else ""
    end = date_to.isoformat() if date_to is not None else ""
    kept: list[Row] = []
    for row in rows:
        raw = row.get("date") or row.get("event_date")
        if not isinstance(raw, str) or len(raw) < 10:
            kept.append(row)
            continue
        day = raw[:10]
        if start and day < start:
            continue
        if end and day > end:
            continue
        kept.append(row)
    return kept


def _sitemap_row(item: Mapping[str, Any]) -> Row:
    row: Row = {
        "sitemap_id": _text(item.get("sitemap_id")),
        "sitemap_url": _text(item.get("sitemap_url")),
        "last_access_date": _text(item.get("last_access_date")),
        "errors_count": _as_int(item.get("errors_count")),
        "urls_count": _as_int(item.get("urls_count")),
        "children_count": _as_int(item.get("children_count")),
        "sitemap_type": _text(item.get("sitemap_type")),
    }
    sources = item.get("sources")
    if isinstance(sources, list):
        row["sources"] = [str(value) for value in sources if isinstance(value, str)]
    return row


def _queue_row(item: Mapping[str, Any]) -> Row:
    return {
        "task_id": _text(item.get("task_id")),
        "url": _text(item.get("url")),
        "added_time": _text(item.get("added_time")),
        "state": _text(item.get("state")),
    }


def _count_total(payload: object) -> int | None:
    """Grand total reported by list endpoints that document ``count`` as the total."""
    return _as_int(_mapping(payload).get("count"))


def _diagnostic_row(item: Mapping[str, Any]) -> Row:
    return {
        "severity": _text(item.get("severity")),
        "code": _text(item.get("code")),
        "state": _text(item.get("state")),
        "last_state_update": _text(item.get("last_state_update")),
    }


def _is_sitemap_problem(row: Row) -> bool:
    code = row.get("code")
    return isinstance(code, str) and "SITEMAP" in code.upper()


def _query_row(item: Mapping[str, Any]) -> Row:
    indicators = _indicator_map(item.get("indicators"))
    row: Row = {
        "query": _text(item.get("query_text", item.get("query"))),
        "shows": _indicator(indicators, item, "TOTAL_SHOWS", "shows"),
        "clicks": _indicator(indicators, item, "TOTAL_CLICKS", "clicks"),
        "position": _indicator(indicators, item, "AVG_SHOW_POSITION", "position"),
    }
    query_id = _text(item.get("query_id"))
    if query_id is not None:
        row["query_id"] = query_id
    click_position = _indicator(indicators, item, "AVG_CLICK_POSITION", "avg_click_position")
    if click_position is not None:
        row["avg_click_position"] = click_position
    extra = {
        name: value
        for name, value in indicators.items()
        if name not in _QUERY_INDICATOR_KEYS and _is_scalar(value)
    }
    if extra:
        row["extra_indicators"] = dict(sorted(extra.items()))
    return row


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool))


def _indicator_map(raw: object) -> Mapping[str, Any]:
    """Normalize ``indicators`` to a name→value mapping.

    The documented API uses a mapping; accept name/value lists for compatibility.
    """
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, list):
        merged: dict[str, Any] = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            if isinstance(name, str) and "value" in entry:
                merged[name] = entry["value"]
        return merged
    return {}


def _indicator(
    indicators: Mapping[str, Any],
    item: Mapping[str, Any],
    indicator: str,
    fallback: str,
) -> int | float | None:
    value = _number(indicators.get(indicator))
    if value is None:
        value = _number(item.get(fallback))
    return value


def _reported_total(payload: object) -> int | None:
    """Grand total reported by the payload.

    Only explicit grand-total keys count. A bare ``count`` is not trusted as a total: if it is
    page-scoped, treating it as the grand total would stop pagination early and hide rows. The
    walk therefore ends on a short page or at the documented 3,000-query cap.
    """
    body = _mapping(payload)
    for key in ("total", "total_queries", "total_count"):
        total = _as_int(body.get(key))
        if total is not None:
            return total
    return None


def _query_report_age(payload: object, fetched_at: datetime) -> int:
    reported_end = _reported_date(payload)
    if reported_end is None:
        return QUERY_REPORT_LAG_DAYS
    return data_age_days(end=reported_end, fetched_at=fetched_at)


def _reported_date(payload: object) -> datetime | None:
    body = _mapping(payload)
    for key in _REPORT_DATE_KEYS:
        parsed = _parse_datetime(body.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_host_not_loaded(payload: object) -> bool:
    body = _mapping(payload)
    for key in ("error_code", "code", "error"):
        value = body.get(key)
        if isinstance(value, str) and _HOST_NOT_LOADED in value.upper():
            return True
        nested = _mapping(value)
        nested_code = nested.get("error_code") or nested.get("code")
        if isinstance(nested_code, str) and _HOST_NOT_LOADED in nested_code.upper():
            return True
    return False
