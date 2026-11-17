"""Google Search Console adapter (read-only).

Covers the three read surfaces Wave 1 needs:

* ``sites.list`` - which properties the token can see;
* ``sitemaps.list`` - what Google knows about the property's sitemaps;
* ``searchAnalytics.query`` - clicks/impressions by query, page, country, device or date;
* ``urlInspection.index:inspect`` - index status for a single URL.

The adapter is deliberately stateless apart from the token provider: pagination, row caps
and limitation codes are reported through
:class:`~lv24_seo.providers.base.ProviderResult` so the CLI/report layer never has to guess
how complete the data is. The provider is asked for a token before every request, which lets
a long-lived MCP process survive service-account access-token expiry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import quote

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import (
    INSPECTION_REFLECTS_LAST_CRAWL,
    ONE_URL_PER_CALL,
    ROWS_CAP_50000,
    SITEMAP_INDEX_CHILDREN,
    SITEMAP_INDEXED_DEPRECATED,
    URL_INSPECTION_QUOTA,
    ProviderResult,
    Row,
    data_age_days,
    now_utc,
)
from lv24_seo.providers.gsc.auth import GscTokenProvider, StaticTokenProvider

GSC_SOURCE = "gsc"
DEFAULT_BASE_URL = "https://www.googleapis.com/webmasters/v3"
DEFAULT_INSPECTION_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"

# The searchAnalytics API rejects rowLimit > 25,000 and caps rows per day/site/type at 50,000.
_MAX_PAGE_ROWS = 25_000
_MAX_TOTAL_ROWS = 50_000

# GSC publishes data 2-3 days behind the current date (docs/data-sources.md). ``data_age_days``
# must not claim a fresher window than the provider can have: floor at the documented lag so a
# window ending today never looks like fresh data.
GSC_REPORT_LAG_DAYS = 3

_AUTH_NEXT_ACTION = "lv24-seo auth login --provider gsc"
_END_OF_DAY = time(hour=23, minute=59)


class GscClient:
    """Synchronous Google Search Console client bound to one token provider."""

    def __init__(
        self,
        *,
        access_token: str = "",
        token_provider: GscTokenProvider | None = None,
        http: HttpClient | None = None,
        property_id: str = "",
        base_url: str = DEFAULT_BASE_URL,
        inspection_url: str = DEFAULT_INSPECTION_URL,
    ) -> None:
        if token_provider is None:
            token = access_token.strip()
            if not token:
                raise SeoError(
                    code=AUTH_REQUIRED,
                    message="Google Search Console access token is missing",
                    next_action=_AUTH_NEXT_ACTION,
                )
            token_provider = StaticTokenProvider(token)
        self._tokens = token_provider
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
            limitations=_sitemap_limitations(rows),
        )

    def get_sitemap(self, sitemap_url: str, *, site_url: str | None = None) -> ProviderResult[Row]:
        """Return one sitemap entry, including an index file's contents summary.

        GSC exposes the ``contents`` aggregate per content type; it does not list the child
        sitemap URLs of a sitemap index. The limitation says so instead of inventing children.
        """
        site = self._require_property(site_url)
        fetched_at = now_utc()
        payload = self._http.get_json(
            self._sitemap_url(site, sitemap_url),
            headers=self._auth_headers(),
            rate_limit_key="gsc",
        )
        row = _map_sitemap(payload if isinstance(payload, Mapping) else {})
        return ProviderResult(
            source=GSC_SOURCE,
            property_id=site,
            fetched_at=fetched_at,
            data_age_days=0,
            rows=[row],
            total=1,
            limitations=_sitemap_limitations([row]),
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
        filter_lists: Sequence[dict[str, Any]] | None = None,
        data_state: str | None = None,
        aggregation_type: str | None = None,
    ) -> ProviderResult[Row]:
        """Query search analytics, paging until a short batch or ``max_rows`` is reached."""
        site = self._require_property()
        fetched_at = now_utc()
        page_size = max(1, min(row_limit, _MAX_PAGE_ROWS))
        row_cap = max(0, min(max_rows, _MAX_TOTAL_ROWS))

        rows: list[Row] = []
        # A zero cap suppresses every row, which is a cap and not an empty result set.
        truncated = row_cap == 0
        start_row = 0
        extra_meta: dict[str, Any] = {}

        while len(rows) < row_cap:
            body: dict[str, Any] = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "dimensions": list(dimensions),
                "type": search_type,
                "rowLimit": page_size,
                "startRow": start_row,
            }
            if filters or filter_lists:
                group: dict[str, Any] = {"groupType": "and"}
                if filters:
                    group["filters"] = list(filters)
                if filter_lists:
                    group["filterLists"] = list(filter_lists)
                body["dimensionFilterGroups"] = [group]
            if data_state:
                body["dataState"] = data_state
            if aggregation_type:
                body["aggregationType"] = aggregation_type

            payload = self._http.post_json(
                self._search_analytics_url(site),
                json=body,
                headers=self._auth_headers(),
                retry=True,
                rate_limit_key="gsc",
            )
            _merge_response_meta(extra_meta, payload)
            batch = _entries(payload, "rows")
            if not batch:
                break

            remaining = row_cap - len(rows)
            if len(batch) > remaining:
                rows.extend(_map_search_row(item, dimensions) for item in batch[:remaining])
                truncated = True
                break

            rows.extend(_map_search_row(item, dimensions) for item in batch)
            if len(batch) < page_size:
                break
            start_row += page_size
            if len(rows) >= row_cap:
                # The last full page filled the cap exactly. There may be more rows behind
                # it, so report the cap instead of a complete result: an "exactly N rows"
                # answer that hides the cap would let a report claim full coverage.
                truncated = True
                break

        limitations: tuple[str, ...] = ()
        warnings: tuple[str, ...] = ()
        if truncated and max_rows >= _MAX_TOTAL_ROWS:
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
            # A capped result cannot prove completeness: an exact row count next to
            # ``truncated=true`` reads as a full answer. Only report a total when the
            # walk ended on a short batch.
            total=None if truncated else len(rows),
            truncated=truncated,
            limitations=limitations,
            warnings=warnings,
            extra_meta=extra_meta,
        )

    def inspect_url(self, url: str, *, language_code: str = "en-US") -> ProviderResult[Row]:
        """Inspect one URL (GSC allows a single URL per call, no batching).

        ``language_code`` localizes ``coverageState`` and the other human-readable enums;
        the verdicts and canonical URLs are language-independent.
        """
        site = self._require_property()
        fetched_at = now_utc()
        payload = self._http.post_json(
            self._inspection_url,
            json={
                "inspectionUrl": url,
                "siteUrl": site,
                "languageCode": language_code,
            },
            headers=self._auth_headers(),
            retry=True,
            rate_limit_key="gsc.url_inspection",
        )
        row = _map_inspection(url, payload)
        return ProviderResult(
            source=GSC_SOURCE,
            property_id=site,
            fetched_at=fetched_at,
            data_age_days=_inspection_data_age(row.get("last_crawl_time"), fetched_at),
            rows=[row],
            total=1,
            limitations=(
                ONE_URL_PER_CALL,
                URL_INSPECTION_QUOTA,
                INSPECTION_REFLECTS_LAST_CRAWL,
            ),
        )

    def _auth_headers(self) -> dict[str, str]:
        # Asked per request: a service-account provider refreshes an expired token here.
        return {"Authorization": f"Bearer {self._tokens.access_token()}"}

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

    def _sitemap_url(self, site: str, sitemap_url: str) -> str:
        # The feed path is a full URL and must be percent-encoded as one path segment.
        return f"{self._sitemaps_url(site)}/{quote(sitemap_url.strip(), safe='')}"


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
    row: Row = {
        "path": entry.get("path"),
        "last_submitted": entry.get("lastSubmitted"),
        "last_downloaded": entry.get("lastDownloaded"),
        "is_pending": bool(entry.get("isPending", False)),
        "is_sitemaps_index": bool(entry.get("isSitemapsIndex", False)),
        "type": entry.get("type"),
        "errors": _as_int(entry.get("errors")),
        "warnings": _as_int(entry.get("warnings")),
        "contents": _map_sitemap_contents(entry.get("contents")),
    }
    return row


def _map_sitemap_contents(raw: object) -> list[Row]:
    """Map ``contents[]``; GSC's ``indexed`` count is deprecated and always 0."""
    if not isinstance(raw, list):
        return []
    contents: list[Row] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        contents.append(
            {
                "type": item.get("type"),
                "submitted": _as_int(item.get("submitted")),
                "indexed": _as_int(item.get("indexed")),
                "indexed_deprecated": True,
            }
        )
    return contents


def _sitemap_limitations(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Limitation codes that make a sitemap read honest about what GSC does not expose."""
    limitations: list[str] = []
    if any(row.get("contents") for row in rows):
        limitations.append(SITEMAP_INDEXED_DEPRECATED)
    if any(row.get("is_sitemaps_index") for row in rows):
        # A sitemap index reports per-type totals, not the child sitemap URLs.
        limitations.append(SITEMAP_INDEX_CHILDREN)
    return tuple(limitations)


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


def _merge_response_meta(target: dict[str, Any], payload: Any) -> None:
    """Keep ``responseAggregationType`` and incomplete-data ``metadata`` from any page."""
    if not isinstance(payload, Mapping):
        return
    aggregation = payload.get("responseAggregationType")
    if isinstance(aggregation, str) and aggregation.strip():
        target["responseAggregationType"] = aggregation.strip()
    metadata = _map_incomplete_metadata(payload.get("metadata"))
    if metadata:
        target["metadata"] = metadata


def _map_incomplete_metadata(raw: object) -> dict[str, Any]:
    """Normalize the two documented incomplete-data markers, tolerating camel/snake case."""
    if not isinstance(raw, Mapping):
        return {}
    metadata: dict[str, Any] = {}
    known = (
        ("firstIncompleteDate", "first_incomplete_date"),
        ("first_incomplete_date", "first_incomplete_date"),
        ("firstIncompleteHour", "first_incomplete_hour"),
        ("first_incomplete_hour", "first_incomplete_hour"),
    )
    for source, target in known:
        value = raw.get(source)
        if isinstance(value, str) and value.strip():
            metadata[target] = value.strip()
    # Unknown scalar markers are passed through unchanged: dropping a provider field the
    # caller may need would be less honest than an extra key.
    known_keys = {source for source, _target in known}
    for key, value in raw.items():
        if not isinstance(key, str) or key in known_keys:
            continue
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            metadata.setdefault(key, value)
    return metadata


def _map_inspection(url: str, payload: Any) -> Row:
    result = payload.get("inspectionResult") if isinstance(payload, Mapping) else None
    inspection = result if isinstance(result, Mapping) else {}
    index_status = inspection.get("indexStatusResult")
    status = index_status if isinstance(index_status, Mapping) else {}
    google_canonical = _as_text(status.get("googleCanonical"))
    user_canonical = _as_text(status.get("userCanonical"))
    return {
        "url": url,
        "verdict": status.get("verdict"),
        "coverage_state": status.get("coverageState"),
        "robots_txt_state": status.get("robotsTxtState"),
        "indexing_state": status.get("indexingState"),
        "last_crawl_time": status.get("lastCrawlTime"),
        "page_fetch_state": status.get("pageFetchState"),
        "google_canonical": google_canonical,
        "user_canonical": user_canonical,
        # Backward-compatible alias: the first canonical Google reports.
        "canonical": google_canonical or user_canonical,
        "crawled_as": status.get("crawledAs"),
        "sitemap": _string_list(status.get("sitemap")),
        "referring_urls": _string_list(status.get("referringUrls")),
        "rich_results": _map_rich_results(inspection.get("richResultsResult")),
        "inspection_result_link": inspection.get("inspectionResultLink"),
    }


def _map_rich_results(raw: object) -> Row:
    rich = raw if isinstance(raw, Mapping) else {}
    detected = rich.get("detectedItems")
    items: list[Row] = []
    if isinstance(detected, list):
        for entry in detected:
            if not isinstance(entry, Mapping):
                continue
            items.append(
                {
                    "rich_result_type": entry.get("richResultType"),
                    "items": _map_rich_items(entry.get("items")),
                }
            )
    return {"verdict": rich.get("verdict"), "detected_items": items}


def _map_rich_items(raw: object) -> list[Row]:
    if not isinstance(raw, list):
        return []
    items: list[Row] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        issues: list[Row] = []
        raw_issues = entry.get("issues")
        if isinstance(raw_issues, list):
            for issue in raw_issues:
                if not isinstance(issue, Mapping):
                    continue
                issues.append(
                    {
                        "message": issue.get("issueMessage"),
                        "severity": issue.get("severity"),
                    }
                )
        items.append({"name": entry.get("name"), "issues": issues})
    return items


def _string_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _as_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _inspection_data_age(last_crawl_time: object, fetched_at: datetime) -> int:
    """Days since the last crawl; 0 when Google never crawled the URL."""
    if not isinstance(last_crawl_time, str) or not last_crawl_time.strip():
        return 0
    text = last_crawl_time.strip().replace("Z", "+00:00")
    try:
        crawled = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if crawled.tzinfo is None:
        crawled = crawled.replace(tzinfo=UTC)
    return max(0, (fetched_at - crawled).days)


def _search_data_age(end_date: date, fetched_at: datetime) -> int:
    """Age of the reporting window, never fresher than the documented GSC lag."""
    end_of_window = datetime.combine(end_date, _END_OF_DAY, tzinfo=UTC)
    measured = data_age_days(end=end_of_window, fetched_at=fetched_at)
    return max(GSC_REPORT_LAG_DAYS, measured)


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
