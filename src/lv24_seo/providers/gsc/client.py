"""Google Search Console adapter (read-only).

Covers the three read surfaces Wave 1 needs:

* ``sites.list`` - which properties the token can see;
* ``sitemaps.list`` - what Google knows about the property's sitemaps;
* ``searchAnalytics.query`` - clicks/impressions by query, page, country, device or date;
* ``urlInspection.index:inspect`` - index status for a single URL.

The adapter is deliberately stateless apart from the token: pagination, row caps and
limitation codes are reported through :class:`~lv24_seo.providers.base.ProviderResult` so
the CLI/report layer never has to guess how complete the data is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import quote

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import (
    ONE_URL_PER_CALL,
    ROWS_CAP_50000,
    ProviderResult,
    Row,
    data_age_days,
    now_utc,
)

GSC_SOURCE = "gsc"
DEFAULT_BASE_URL = "https://www.googleapis.com/webmasters/v3"
DEFAULT_INSPECTION_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"

# The searchAnalytics API rejects rowLimit > 25,000 and caps rows per day/site/type at 50,000.
_MAX_PAGE_ROWS = 25_000
_MAX_TOTAL_ROWS = 50_000

_AUTH_NEXT_ACTION = "lv24-seo auth login --provider gsc"
_END_OF_DAY = time(hour=23, minute=59)


class GscClient:
    """Synchronous Google Search Console client bound to one access token."""

    def __init__(
        self,
        *,
        access_token: str,
        http: HttpClient | None = None,
        property_id: str = "",
        base_url: str = DEFAULT_BASE_URL,
        inspection_url: str = DEFAULT_INSPECTION_URL,
    ) -> None:
        token = access_token.strip()
        if not token:
            raise SeoError(
                code=AUTH_REQUIRED,
                message="Google Search Console access token is missing",
                next_action=_AUTH_NEXT_ACTION,
            )
        self._access_token = token
        self._http = http if http is not None else HttpClient()
        self._property_id = property_id.strip()
        self._base_url = base_url.rstrip("/")
        self._inspection_url = inspection_url

    def list_sites(self) -> ProviderResult[Row]:
        """Return every property the token can access."""
        fetched_at = now_utc()
        payload = self._http.get_json(
            f"{self._base_url}/sites", headers=self._auth_headers(), rate_limit_key="gsc"
        )
        rows = [_map_site(entry) for entry in _entries(payload, "siteEntry")]
        return ProviderResult(
            source=GSC_SOURCE,
            property_id=self._property_id,
            fetched_at=fetched_at,
            data_age_days=0,
            rows=rows,
            total=len(rows),
        )

    def list_sitemaps(self, *, site_url: str | None = None) -> ProviderResult[Row]:
        """Return the sitemaps registered for ``site_url`` (defaults to ``property_id``)."""
        site = self._require_property(site_url)
        fetched_at = now_utc()
        payload = self._http.get_json(
            self._sitemaps_url(site), headers=self._auth_headers(), rate_limit_key="gsc"
        )
        rows = [_map_sitemap(entry) for entry in _entries(payload, "sitemap")]
        return ProviderResult(
            source=GSC_SOURCE,
            property_id=site,
            fetched_at=fetched_at,
            data_age_days=0,
            rows=rows,
            total=len(rows),
        )

    def search_analytics(
        self,
        *,
        start_date: date,
        end_date: date,
        dimensions: Sequence[str] = ("query",),
        search_type: str = "web",
        row_limit: int = 25_000,
        max_rows: int = 50_000,
        filters: Sequence[dict[str, Any]] | None = None,
    ) -> ProviderResult[Row]:
        """Query search analytics, paging until a short batch or ``max_rows`` is reached."""
        site = self._require_property()
        fetched_at = now_utc()
        page_size = max(1, min(row_limit, _MAX_PAGE_ROWS))
        row_cap = max(0, min(max_rows, _MAX_TOTAL_ROWS))

        rows: list[Row] = []
        truncated = False
        start_row = 0

        while len(rows) < row_cap:
            body: dict[str, Any] = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "dimensions": list(dimensions),
                "type": search_type,
                "rowLimit": page_size,
                "startRow": start_row,
            }
            if filters:
                body["dimensionFilterGroups"] = [{"groupType": "and", "filters": list(filters)}]

            payload = self._http.post_json(
                self._search_analytics_url(site),
                json=body,
                headers=self._auth_headers(),
                retry=True,
                rate_limit_key="gsc",
            )
            batch = _entries(payload, "rows")
            if not batch:
                break

            remaining = row_cap - len(rows)
            if len(batch) >= remaining:
                rows.extend(_map_search_row(item, dimensions) for item in batch[:remaining])
                truncated = True
                break

            rows.extend(_map_search_row(item, dimensions) for item in batch)
            if len(batch) < page_size:
                break
            start_row += page_size

        limitations: tuple[str, ...] = ()
        warnings: tuple[str, ...] = ()
        if max_rows >= _MAX_TOTAL_ROWS:
            limitations = (ROWS_CAP_50000,)
        elif truncated:
            warnings = (
                f"results capped at {row_cap} rows by the caller; "
                f"GSC allows up to {_MAX_TOTAL_ROWS} rows per day",
            )

        return ProviderResult(
            source=GSC_SOURCE,
            property_id=site,
            fetched_at=fetched_at,
            data_age_days=_search_data_age(end_date, fetched_at),
            rows=rows,
            total=len(rows),
            truncated=truncated,
            limitations=limitations,
            warnings=warnings,
        )

    def inspect_url(self, url: str) -> ProviderResult[Row]:
        """Inspect one URL (GSC allows a single URL per call, no batching)."""
        site = self._require_property()
        fetched_at = now_utc()
        payload = self._http.post_json(
            self._inspection_url,
            json={"inspectionUrl": url, "siteUrl": site},
            headers=self._auth_headers(),
            retry=True,
            rate_limit_key="gsc.url_inspection",
        )
        row = _map_inspection(url, payload)
        return ProviderResult(
            source=GSC_SOURCE,
            property_id=site,
            fetched_at=fetched_at,
            data_age_days=0,
            rows=[row],
            total=1,
            limitations=(ONE_URL_PER_CALL,),
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _require_property(self, site_url: str | None = None) -> str:
        site = (site_url if site_url is not None else self._property_id).strip()
        if not site:
            raise SeoError(
                code=INVALID_INPUT,
                message="Google Search Console property is not configured",
                next_action="pass a property id or run lv24-seo init",
            )
        return site

    def _search_analytics_url(self, site: str) -> str:
        return f"{self._base_url}/sites/{quote(site, safe='')}/searchAnalytics/query"

    def _sitemaps_url(self, site: str) -> str:
        return f"{self._base_url}/sites/{quote(site, safe='')}/sitemaps"


def _entries(payload: Any, key: str) -> list[Mapping[str, Any]]:
    """Return the list under ``key`` as mappings; tolerate a missing/odd payload."""
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _map_site(entry: Mapping[str, Any]) -> Row:
    return {
        "site_url": entry.get("siteUrl"),
        "permission_level": entry.get("permissionLevel"),
    }


def _map_sitemap(entry: Mapping[str, Any]) -> Row:
    return {
        "path": entry.get("path"),
        "last_submitted": entry.get("lastSubmitted"),
        "last_downloaded": entry.get("lastDownloaded"),
        "is_pending": bool(entry.get("isPending", False)),
        "errors": _as_int(entry.get("errors")),
        "warnings": _as_int(entry.get("warnings")),
    }


def _map_search_row(entry: Mapping[str, Any], dimensions: Sequence[str]) -> Row:
    keys = entry.get("keys")
    key_values = keys if isinstance(keys, list) else []
    row: Row = {
        dimension: key_values[index] if index < len(key_values) else None
        for index, dimension in enumerate(dimensions)
    }
    row["clicks"] = _as_float(entry.get("clicks"))
    row["impressions"] = _as_float(entry.get("impressions"))
    row["ctr"] = _as_float(entry.get("ctr"))
    row["position"] = _as_float(entry.get("position"))
    return row


def _map_inspection(url: str, payload: Any) -> Row:
    result = payload.get("inspectionResult") if isinstance(payload, Mapping) else None
    index_status = result.get("indexStatusResult") if isinstance(result, Mapping) else None
    status = index_status if isinstance(index_status, Mapping) else {}
    return {
        "url": url,
        "verdict": status.get("verdict"),
        "coverage_state": status.get("coverageState"),
        "indexing_state": status.get("indexingState"),
        "last_crawl_time": status.get("lastCrawlTime"),
        "canonical": status.get("googleCanonical") or status.get("userCanonical"),
        "robots_txt_state": status.get("robotsTxtState"),
        "page_fetch_state": status.get("pageFetchState"),
    }


def _search_data_age(end_date: date, fetched_at: datetime) -> int:
    end_of_window = datetime.combine(end_date, _END_OF_DAY, tzinfo=UTC)
    return data_age_days(end=end_of_window, fetched_at=fetched_at)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
