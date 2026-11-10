"""Yandex Webmaster API v4 read adapter.

Yandex Webmaster is the free source of Yandex query and indexing data, but it is lossy in ways
the caller must see:

* query reports cover at most the top 3,000 queries and never join a query to a page;
* reporting lags about four days and can return ``HOST_NOT_LOADED`` during maintenance;
* per-URL sitemap error detail is not exposed by the API at all.

This module is read-only. Recrawl queueing is a write and stays behind ``core/write_gate.py``;
:meth:`YandexWebmasterClient.recrawl_quota` only reads the remaining daily quota.

Degradation policy
------------------
A host-scoped call that finds no loaded data returns an empty :class:`ProviderResult` with
``warnings=("host_not_loaded",)`` and :data:`DEGRADED_HOST` instead of raising: a new or
temporarily unavailable host is a data state, not a tool failure. Yandex answers ``404`` both
for an unknown ``host_id`` and for a host that is not loaded yet, and the shared HTTP layer does
not expose error bodies, so a host-scoped ``404`` is also reported as degraded.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from lv24_seo.core.errors import (
    AUTH_REQUIRED,
    INVALID_INPUT,
    PROPERTY_NOT_FOUND,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import (
    DEGRADED_HOST,
    NO_PER_URL_SITEMAP_ERRORS,
    NO_QUERY_PAGE_JOIN,
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

_QUERY_INDICATORS = ("TOTAL_SHOWS", "TOTAL_CLICKS", "AVG_SHOW_POSITION")
_SUMMARY_NUMERIC_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pages_in_search", ("searchable_pages_count", "pages_in_search")),
    ("pages_excluded", ("excluded_pages_count", "pages_excluded")),
    ("http_2xx", ("http_2xx",)),
    ("http_3xx", ("http_3xx",)),
    ("http_4xx", ("http_4xx",)),
    ("http_5xx", ("http_5xx",)),
)
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

    def indexing_history(self, host_id: str) -> ProviderResult[Row]:
        """Daily indexing buckets (date plus HTTP status counters)."""
        scope = _require_host_id(host_id)
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "indexing/history/")
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        rows = _copy_rows(_rows_from(payload, "history", "indexing_history"))
        if not rows:
            return _degraded(scope, fetched_at)
        return _result(property_id=scope, rows=rows, fetched_at=fetched_at)

    def search_urls_events(self, host_id: str) -> ProviderResult[Row]:
        """History of search-URL events (pages entering or leaving the index)."""
        scope = _require_host_id(host_id)
        fetched_at = now_utc()
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "search-urls/events/history/")
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        rows = _copy_rows(_rows_from(payload, "events", "history", "search_urls_events"))
        if not rows:
            return _degraded(scope, fetched_at)
        return _result(property_id=scope, rows=rows, fetched_at=fetched_at)

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
    ) -> ProviderResult[Row]:
        """Top queries for the host, ordered by ``order_by``.

        Indicators are requested as repeated ``query_indicator`` parameters, not a comma list.
        The report is capped by Yandex at 3,000 queries and has no query-to-page join.
        """
        scope = _require_host_id(host_id)
        page_limit = _require_limit(limit)
        ordering = _require_order_by(order_by)
        fetched_at = now_utc()
        params: dict[str, Any] = {
            "order_by": ordering,
            "query_indicator": list(_QUERY_INDICATORS),
            "limit": page_limit,
        }
        payload, missing = self._get_host_scoped(
            _host_path(self._ensure_user_id(), scope, "search-queries/popular/"),
            params=params,
        )
        if missing or _is_host_not_loaded(payload):
            return _degraded(scope, fetched_at)

        total = _reported_total(payload)
        return _result(
            property_id=scope,
            rows=[_query_row(item) for item in _rows_from(payload, "queries")],
            fetched_at=fetched_at,
            data_age=_query_report_age(payload, fetched_at),
            total=total,
            truncated=total is not None and total > page_limit,
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


def _unexpected_payload(path: str) -> SeoError:
    return SeoError(
        code=UPSTREAM_UNAVAILABLE,
        message=f"unexpected Yandex Webmaster payload for {path}",
        retryable=False,
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
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SeoError(
            code=INVALID_INPUT,
            message="limit must be a positive integer",
            next_action="use a limit between 1 and 500",
        )
    return limit


def _require_order_by(order_by: object) -> str:
    if not isinstance(order_by, str) or not order_by.strip():
        raise SeoError(
            code=INVALID_INPUT,
            message="order_by must be a non-empty string",
            next_action="use TOTAL_SHOWS, TOTAL_CLICKS or AVG_SHOW_POSITION",
        )
    return order_by.strip()


def _host_path(user_id: int, host_id: str, suffix: str) -> str:
    return f"/user/{user_id}/hosts/{quote(host_id, safe=':')}/{suffix}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows_from(payload: object, *container_keys: str) -> list[Mapping[str, Any]]:
    """Return object rows from a JSON array or from the first known container key."""
    if isinstance(payload, list):
        candidates: object = payload
    else:
        body = _mapping(payload)
        candidates = next((body[key] for key in container_keys if key in body), None)
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
    return row or None


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
    indicators = _mapping(item.get("indicators"))
    return {
        "query": _text(item.get("query")),
        "shows": _indicator(indicators, item, "TOTAL_SHOWS", "shows"),
        "clicks": _indicator(indicators, item, "TOTAL_CLICKS", "clicks"),
        "position": _indicator(indicators, item, "AVG_SHOW_POSITION", "position"),
    }


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
