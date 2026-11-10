"""Bing Webmaster Tools + IndexNow adapter.

Two different auth models live in this module:

* Bing Webmaster Tools uses a personal API key passed as the ``apikey`` query parameter.
  The key is never logged and never appears in an error message: :class:`HttpClient`
  reports ``host + path`` only, so the query string cannot leak.
* IndexNow authenticates with a key *file* published at ``https://<host>/<key>.txt``. The
  POST carries the key itself, so a mismatch is a configuration error on our side rather
  than a transport failure.

Bing wraps every JSON response in a WCF envelope (``{"d": ...}``); :func:`_unwrap_items`
accepts both the envelope and a bare list so recorded fixtures and newer endpoints keep
working.

Caps are enforced *before* the network call: 500 URLs per ``SubmitUrlbatch`` (Bing allows
roughly 10k submits/day/domain) and 10_000 URLs per IndexNow POST. An oversized batch is a
caller bug, not something to silently truncate.

Write gating (``--allow-write`` / confirm token / idempotency key) is deliberately not
implemented here: ADR-0003 puts it in ``core/write_gate.py`` so every adapter inherits one
policy instead of inventing its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import ProviderResult, Row, now_utc

__all__ = ["BingClient", "indexnow_submit"]

BING_API_KEY_ENV = "LV24_SEO_BING_API_KEY"
DEFAULT_BASE_URL = "https://ssl.bing.com/webmaster/api.svc/json"
DEFAULT_INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
SUBMIT_BATCH_LIMIT = 500
INDEXNOW_URL_LIMIT = 10_000

# Stable warning codes: consumers must be able to detect a lossy parse without a schema.
UNEXPECTED_SHAPE_WARNING = "unexpected_response_shape"
MALFORMED_ROWS_WARNING = "malformed_rows_skipped"

_CRAWL_MAPPED_KEYS = frozenset({"url", "issuetype", "severity", "datedetected", "detected"})


class BingClient:
    """Bing Webmaster Tools JSON API scoped to one verified site."""

    def __init__(
        self,
        *,
        api_key: str,
        site_url: str,
        http: HttpClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise SeoError(
                code=AUTH_REQUIRED,
                message=f"Bing Webmaster API key is empty: set {BING_API_KEY_ENV}",
                next_action=f"set {BING_API_KEY_ENV}",
            )

        site = site_url.strip()
        if not site:
            raise SeoError(
                code=INVALID_INPUT,
                message="site_url is required",
                next_action="pass the verified site URL from Bing Webmaster Tools",
            )

        base = base_url.strip().rstrip("/")
        if not base:
            raise SeoError(
                code=INVALID_INPUT,
                message="base_url is required",
                next_action="use the default Bing Webmaster endpoint",
            )

        self._api_key = key
        self._site_url = site
        self._base_url = base
        self._http = http if http is not None else HttpClient()
        self._owns_http = http is None

    def close(self) -> None:
        """Close the connection pool, but only when this client created it."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> BingClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def query_stats(self) -> ProviderResult[Row]:
        """Return query-level clicks, impressions and average position."""
        items, warnings = self._fetch_rows("GetQueryStats")
        rows: list[Row] = [_query_row(item) for item in items]
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=rows,
            total=len(rows),
            warnings=warnings,
        )

    def crawl_issues(self) -> ProviderResult[Row]:
        """Return crawl issues reported by Bing for this site."""
        items, warnings = self._fetch_rows("GetCrawlIssues")
        rows: list[Row] = [_crawl_row(item) for item in items]
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=rows,
            total=len(rows),
            warnings=warnings,
        )

    def submit_urls(self, urls: Sequence[str]) -> ProviderResult[Row]:
        """Submit up to 500 URLs through ``SubmitUrlbatch``."""
        batch = _collect_urls(
            urls,
            limit=SUBMIT_BATCH_LIMIT,
            limit_action=f"split into batches of {SUBMIT_BATCH_LIMIT}",
        )
        self._http.post_json(
            f"{self._base_url}/SubmitUrlbatch",
            json={"siteUrl": self._site_url, "urlList": batch},
        )
        row: Row = {"submitted": len(batch), "site_url": self._site_url}
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=[row],
        )

    def _fetch_rows(self, path: str) -> tuple[list[Mapping[str, Any]], tuple[str, ...]]:
        payload = self._http.get_json(
            f"{self._base_url}/{path}",
            params={"apikey": self._api_key, "siteUrl": self._site_url},
        )
        items, warnings = _unwrap_items(payload)
        rows: list[Mapping[str, Any]] = []
        skipped = False
        for item in items:
            if isinstance(item, Mapping):
                rows.append(item)
            else:
                skipped = True
        if skipped:
            warnings = (*warnings, MALFORMED_ROWS_WARNING)
        return rows, warnings


def indexnow_submit(
    *,
    host: str,
    key: str,
    urls: Sequence[str],
    key_location: str | None = None,
    http: HttpClient | None = None,
    endpoint: str = DEFAULT_INDEXNOW_ENDPOINT,
) -> ProviderResult[Row]:
    """Ping IndexNow (Bing, Yandex and others; Google does not participate)."""
    host_value = _clean_host(host)
    key_value = key.strip()
    if not key_value:
        raise SeoError(
            code=AUTH_REQUIRED,
            message="IndexNow key is empty",
            next_action=f"host the key at https://{host_value}/<key>.txt and pass the same key",
        )

    batch = _collect_urls(
        urls,
        limit=INDEXNOW_URL_LIMIT,
        limit_action=f"split into batches of {INDEXNOW_URL_LIMIT}",
    )

    target = endpoint.strip()
    if not target:
        raise SeoError(
            code=INVALID_INPUT,
            message="IndexNow endpoint is empty",
            next_action=f"use the default {DEFAULT_INDEXNOW_ENDPOINT}",
        )

    key_file = (key_location or "").strip()
    payload: dict[str, Any] = {"host": host_value, "key": key_value}
    if key_file:
        payload["keyLocation"] = key_file
    payload["urlList"] = batch

    client = http if http is not None else HttpClient()
    try:
        response = client.request("POST", target, json=payload, follow_redirects=True)
    except SeoError as exc:
        if exc.code == INVALID_INPUT and exc.detail.get("status_code") == 422:
            # 422 means the engine could not match the key to the key file at the host.
            location = key_file or f"https://{host_value}/{key_value}.txt"
            raise SeoError(
                code=INVALID_INPUT,
                message="IndexNow rejected the key (HTTP 422): key file missing or mismatched",
                next_action=f"publish the key at {location} with exactly the key as the body",
                detail={"status_code": 422},
            ) from exc
        raise
    finally:
        if http is None:
            client.close()

    row: Row = {
        "submitted": len(batch),
        "host": host_value,
        "status_code": response.status_code,
    }
    return ProviderResult(
        source="indexnow",
        property_id=host_value,
        fetched_at=now_utc(),
        data_age_days=0,
        rows=[row],
    )


def _unwrap_items(payload: Any) -> tuple[list[Any], tuple[str, ...]]:
    """Return the row list from a WCF ``{"d": [...]}`` envelope or a bare list."""
    if isinstance(payload, Mapping):
        if "d" in payload:
            inner = payload["d"]
            if inner is None:
                return [], ()
            if isinstance(inner, list):
                return inner, ()
        return [], (UNEXPECTED_SHAPE_WARNING,)
    if isinstance(payload, list):
        return payload, ()
    return [], (UNEXPECTED_SHAPE_WARNING,)


def _query_row(item: Mapping[str, Any]) -> Row:
    return {
        "query": _as_str(_pick(item, "Query", "query")),
        "clicks": _as_int(_pick(item, "Clicks", "clicks")),
        "impressions": _as_int(_pick(item, "Impressions", "impressions")),
        "position": _as_float(_pick(item, "AvgImpressionPos", "avgImpressionPos", "position")),
    }


def _crawl_row(item: Mapping[str, Any]) -> Row:
    raw = {
        str(key): value for key, value in item.items() if str(key).lower() not in _CRAWL_MAPPED_KEYS
    }
    return {
        "url": _pick(item, "Url", "URL", "url"),
        "issue_type": _pick(item, "IssueType", "issueType", "issue_type"),
        "severity": _pick(item, "Severity", "severity"),
        "detected": _pick(item, "DateDetected", "dateDetected", "Detected", "detected"),
        "raw": raw,
    }


def _pick(row: Mapping[str, Any], *names: str) -> Any:
    """Fetch the first present key, falling back to a case-insensitive match."""
    for name in names:
        if name in row:
            return row[name]
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _clean_host(host: str) -> str:
    value = host.strip()
    if not value:
        raise SeoError(
            code=INVALID_INPUT,
            message="IndexNow host is required",
            next_action="pass the site host, for example example.com",
        )
    if "://" in value or "/" in value:
        raise SeoError(
            code=INVALID_INPUT,
            message="IndexNow host must be a bare hostname",
            next_action="drop the scheme and path, for example example.com",
            detail={"host": value},
        )
    return value


def _collect_urls(urls: Sequence[str], *, limit: int, limit_action: str) -> list[str]:
    if isinstance(urls, str):
        raise SeoError(
            code=INVALID_INPUT,
            message="urls must be a sequence of URLs, not a single string",
            next_action='pass a list, for example ["https://example.com/page"]',
        )

    collected: list[str] = []
    for index, raw in enumerate(urls):
        if not isinstance(raw, str):
            raise SeoError(
                code=INVALID_INPUT,
                message=f"url at index {index} is not a string",
                detail={"index": index},
            )
        value = raw.strip()
        if not value:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"url at index {index} is empty",
                detail={"index": index},
            )
        collected.append(value)

    if not collected:
        raise SeoError(
            code=INVALID_INPUT,
            message="at least one URL is required",
            next_action="pass one or more absolute URLs",
        )
    if len(collected) > limit:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"too many URLs in one call: {len(collected)} > {limit}",
            next_action=limit_action,
            detail={"count": len(collected), "limit": limit},
        )
    return collected
