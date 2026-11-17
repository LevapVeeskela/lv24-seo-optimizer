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
caller bug, not something to silently truncate; :meth:`BingClient.submit_url_batches` splits a
larger list into explicit 500-URL batches instead of dropping URLs.

Read-only sitemap views use ``GetFeeds`` (top-level sitemaps) and ``GetFeedDetails`` (the
children of a sitemap index). ``GetUrlSubmissionQuota`` is documented with ``DailyQuota`` and
``MonthlyQuota``; newer responses may add ``*Remaining`` fields, so every field is mapped when
present and everything unrecognised is kept in the row's ``raw`` object.

Write gating (``--allow-write`` / confirm token / idempotency key) is deliberately not
implemented here: ADR-0003 puts it in ``core/write_gate.py`` so every adapter inherits one
policy instead of inventing its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from lv24_seo.core.errors import (
    AUTH_INVALID,
    AUTH_REQUIRED,
    CONFIG_INVALID,
    INVALID_INPUT,
    PROPERTY_NOT_FOUND,
    QUOTA_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import (
    NO_PER_URL_SITEMAP_ERRORS,
    ProviderResult,
    Row,
    data_age_days,
    now_utc,
)

__all__ = ["BingClient", "indexnow_submit"]

BING_API_KEY_ENV = "LV24_SEO_BING_API_KEY"
DEFAULT_BASE_URL = "https://ssl.bing.com/webmaster/api.svc/json"
DEFAULT_INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
SUBMIT_BATCH_LIMIT = 500
INDEXNOW_URL_LIMIT = 10_000

# Stable warning codes: consumers must be able to detect a lossy parse without a schema.
UNEXPECTED_SHAPE_WARNING = "unexpected_response_shape"
MALFORMED_ROWS_WARNING = "malformed_rows_skipped"

# Stable limitation codes for read diagnostics. An empty stats response is "no data for the
# window", never "zero impressions": the two are indistinguishable once rendered as 0.
NO_ROWS_FOR_WINDOW = "no_rows_for_window"
PROVIDER_RETURNED_NO_ROWS = "provider_returned_no_rows"
NEVER_CRAWLED = "never_crawled"
CRAWL_STATS_SIX_MONTHS = "crawl_stats_six_month_window"

# The quota endpoint reports *remaining* submissions (verified against lv24.by). The monthly
# window can bind before the daily one and its reset schedule is not documented.
QUOTA_MONTHLY_CAN_BIND_FIRST = "quota_monthly_can_bind_first"
QUOTA_MONTHLY_RESET_UNDOCUMENTED = "quota_monthly_reset_undocumented"
# SubmitUrlbatch has no per-URL status and no documented idempotency: a duplicate URL may
# consume quota even though the provider accepts the call.
SUBMIT_IDEMPOTENCY_UNDOCUMENTED = "submit_idempotency_undocumented"
SITEMAP_STATUS_NOT_CRAWL_STATUS = "sitemap_status_is_not_crawl_status"
UNKNOWN_CRAWL_ISSUE_BITS = "unknown_crawl_issue_bits"

_CRAWL_MAPPED_KEYS = frozenset({"url", "httpcode", "issues", "inlinks"})
_CRAWL_ISSUE_BITS: tuple[tuple[int, str], ...] = (
    (1, "301"),
    (2, "302"),
    (4, "4xx"),
    (8, "5xx"),
    (16, "robots"),
    (32, "malware"),
    (64, "important_robots"),
    (128, "dns"),
    (256, "timeout"),
)
_CRAWL_KNOWN_BITS = sum(bit for bit, _label in _CRAWL_ISSUE_BITS)
_FEED_MAPPED_KEYS = frozenset(
    {
        "url",
        "type",
        "status",
        "submitted",
        "lastcrawled",
        "urlcount",
        "filesize",
        "compressed",
    }
)
_QUOTA_REMAINING_KEYS = ("daily_remaining", "monthly_remaining")
_AUTH_NEXT_ACTION = "run `lv24-seo auth login --provider bing` or replace LV24_SEO_BING_API_KEY"
_SITE_NEXT_ACTION = "set providers.bing.site in seo.toml or pass --site https://example.com"
# GetChildrenUrlInfo takes a ushort page; walking every page of a large directory is a
# caller decision, so one call stays one page.
CHILDREN_PAGE_MAX = 65_535
# WCF DateTime.MinValue (0001-01-01T00:00:00Z) is Bing's "never" sentinel for crawl and
# discovery dates. It must map to null, not to a 0001 date or a 739k-day age.
_WCF_NULL_DATE_MS = -62_135_596_800_000
_ROLE_NAMES: dict[int, str] = {0: "Administrator", 1: "ReadOnly", 2: "ReadWrite"}
# The JSON sample sends this object; all-zero filters mean "any" and never hide rows.
_CHILD_FILTER_PROPERTIES: dict[str, Any] = {
    "__type": "FilterProperties:#Microsoft.Bing.Webmaster.Api",
    "CrawlDateFilter": 0,
    "DiscoveredDateFilter": 0,
    "DocFlagsFilters": 0,
    "HttpCodeFilters": 0,
}


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
        # Site-agnostic reads (GetUserSites) construct the client without a site. A
        # site-scoped call raises CONFIG_INVALID through _require_site().
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

    def _require_site(self) -> str:
        """Return the verified site URL, or raise the CLI's ``CONFIG_INVALID`` contract."""
        if not self._site_url:
            raise SeoError(
                code=CONFIG_INVALID,
                message="Bing Webmaster site is not configured",
                next_action=_SITE_NEXT_ACTION,
            )
        return self._site_url

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
        """Return query-level clicks, impressions and average position.

        Bing aggregates query stats per day and stamps every row with ``Date``; the newest
        row date is the end of the reported window, so it drives ``data_age_days`` instead of
        a hardcoded zero that would present week-old data as fresh. Zero rows carry
        ``no_rows_for_window``: an empty response is missing data, not zero impressions.
        """
        items, warnings = self._fetch_rows("GetQueryStats")
        rows: list[Row] = [_query_row(item) for item in items]
        fetched_at = now_utc()
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=fetched_at,
            data_age_days=_newest_row_age(items, fetched_at, ("Date", "date")),
            rows=rows,
            total=len(rows),
            limitations=(NO_ROWS_FOR_WINDOW,) if not rows else (),
            warnings=warnings,
        )

    def page_stats(self) -> ProviderResult[Row]:
        """Return page-level traffic rows from ``GetPageStats``.

        The endpoint returns ``QueryStats`` objects whose ``Query`` field holds the page URL,
        not a search query. Each row therefore exposes ``page`` plus clicks, impressions, both
        average positions and the provider ``Date``. An empty response is reported with
        ``no_rows_for_window``; the engine never fabricates a zero row or a date.
        """
        items, warnings = self._fetch_rows("GetPageStats")
        rows: list[Row] = [_page_row(item) for item in items]
        return self._window_result(items, rows, warnings)

    def query_page_stats(self, query: str) -> ProviderResult[Row]:
        """Return the pages that received impressions for one query.

        ``GetQueryPageStats`` returns ``QueryStats`` rows; because the query is fixed, the
        row's ``Query`` field carries the page URL. The requested query is attached to every
        row so the join is explicit instead of implied by the call.
        """
        value = _required_text(query, name="query")
        payload = self._request_json(
            "GET",
            "GetQueryPageStats",
            params={"apikey": self._api_key, "siteUrl": self._require_site(), "query": value},
        )
        items, warnings = _mapping_items(payload)
        rows: list[Row] = [
            _join_row(
                item,
                query=value,
                page=_as_str(_pick(item, "Query", "query")),
            )
            for item in items
        ]
        return self._window_result(items, rows, warnings)

    def page_query_stats(self, page: str) -> ProviderResult[Row]:
        """Return the queries that produced impressions for one page.

        ``GetPageQueryStats`` is the mirror of :meth:`query_page_stats`: the page is fixed and
        the row's ``Query`` field carries the search query. The requested page is attached to
        every row.
        """
        target = _url_info_target(page, name="page")
        payload = self._request_json(
            "GET",
            "GetPageQueryStats",
            params={"apikey": self._api_key, "siteUrl": self._require_site(), "page": target},
        )
        items, warnings = _mapping_items(payload)
        rows: list[Row] = [
            _join_row(
                item,
                query=_as_str(_pick(item, "Query", "query")),
                page=target,
            )
            for item in items
        ]
        return self._window_result(items, rows, warnings)

    def query_page_detail_stats(self, query: str, page: str) -> ProviderResult[Row]:
        """Return daily detail rows for one query/page pair.

        ``GetQueryPageDetailStats`` returns ``DetailedQueryStats`` (``Clicks``, ``Impressions``,
        ``Position``, ``Date``) without the query or page columns, so the requested pair is
        attached to each row. There is no average click/impression position in this shape.
        """
        value = _required_text(query, name="query")
        target = _url_info_target(page, name="page")
        payload = self._request_json(
            "GET",
            "GetQueryPageDetailStats",
            params={
                "apikey": self._api_key,
                "siteUrl": self._require_site(),
                "query": value,
                "page": target,
            },
        )
        items, warnings = _mapping_items(payload)
        rows: list[Row] = [_join_row(item, query=value, page=target) for item in items]
        return self._window_result(items, rows, warnings)

    def rank_and_traffic(self) -> ProviderResult[Row]:
        """Return daily site clicks and impressions from ``GetRankAndTrafficStats``.

        The feed is daily (``Date``, ``Clicks``, ``Impressions``) and, since 2023-03-24,
        includes every Bing vertical, not only web search. Zero rows carry
        ``no_rows_for_window``.
        """
        items, warnings = self._fetch_rows("GetRankAndTrafficStats")
        rows: list[Row] = [
            {
                "date": _optional_date(_pick(item, "Date", "date")),
                "clicks": _as_int(_pick(item, "Clicks", "clicks")),
                "impressions": _as_int(_pick(item, "Impressions", "impressions")),
            }
            for item in items
        ]
        return self._window_result(items, rows, warnings)

    def crawl_stats(self) -> ProviderResult[Row]:
        """Return daily crawl statistics for the last six months.

        ``GetCrawlStats`` reports response-code buckets, robots/malware counts and index/link
        totals per day. The provider caps the window at six months, so the result always
        carries ``crawl_stats_six_month_window``; zero rows additionally carry
        ``no_rows_for_window``.
        """
        items, warnings = self._fetch_rows("GetCrawlStats")
        rows: list[Row] = [_crawl_stats_row(item) for item in items]
        return self._window_result(
            items,
            rows,
            warnings,
            limitations=(CRAWL_STATS_SIX_MONTHS,),
        )

    def crawl_issues(self) -> ProviderResult[Row]:
        """Return crawl issues reported by Bing for this site.

        A row is ``{Url, HttpCode, Issues, InLinks}``; ``Issues`` is an integer bitmask, not
        a type/severity/date. Every known bit becomes a label in ``issues`` and the raw value
        stays in ``issues_bits``. There is no date in this feed, so ``data_age_days`` is zero
        rather than an invented detection time.
        """
        items, warnings = self._fetch_rows("GetCrawlIssues")
        rows: list[Row] = [_crawl_row(item) for item in items]
        unknown = any((_as_int(row.get("issues_bits")) or 0) & ~_CRAWL_KNOWN_BITS for row in rows)
        limitations: tuple[str, ...] = (UNKNOWN_CRAWL_ISSUE_BITS,) if unknown else ()
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=rows,
            total=len(rows),
            limitations=limitations,
            warnings=warnings,
        )

    def url_info(self, url: str) -> ProviderResult[Row]:
        """Return Bing's index details for one page, directory or domain.

        ``GetUrlInfo`` accepts an absolute URL or ``domain:<host>``. Fields are ``Url``,
        ``AnchorCount``, ``DiscoveryDate``, ``DocumentSize``, ``HttpStatus``, ``IsPage``,
        ``LastCrawledDate`` and ``TotalChildUrlCount``. WCF's ``Date(-62135596800000)``
        sentinel means "never" and maps to ``null``; a never-crawled row also carries
        ``never_crawled`` instead of pretending to be fresh. An empty ``d`` is
        ``provider_returned_no_rows``.
        """
        target = _url_info_target(url, name="url")
        payload = self._request_json(
            "GET",
            "GetUrlInfo",
            params={"apikey": self._api_key, "siteUrl": self._require_site(), "url": target},
        )
        item, warnings = _unwrap_single(payload)
        rows: list[Row] = [] if item is None else [_url_info_row(item)]
        limitations: list[str] = []
        if not rows:
            limitations.append(PROVIDER_RETURNED_NO_ROWS)
        elif rows[0].get("last_crawled_date") is None:
            limitations.append(NEVER_CRAWLED)
        fetched_at = now_utc()
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=fetched_at,
            data_age_days=(
                0
                if item is None
                else _newest_row_age([item], fetched_at, ("LastCrawledDate", "lastCrawledDate"))
            ),
            rows=rows,
            total=len(rows),
            limitations=tuple(limitations),
            warnings=warnings,
        )

    def children_url_info(self, url: str, page: int = 0) -> ProviderResult[Row]:
        """Return one page of children for a directory or domain.

        ``GetChildrenUrlInfo`` is a read-only ``POST`` with
        ``{siteUrl, url, page, filterProperties}``; ``filterProperties`` is sent with
        all-zero (Any) filters so the call never hides rows. One call returns one page: the
        caller owns walking, and ``page`` is bounded to the provider's ``ushort`` range.
        """
        target = _url_info_target(url, name="url")
        number = _page_number(page)
        payload = self._request_json(
            "POST",
            "GetChildrenUrlInfo",
            params={"apikey": self._api_key},
            body={
                "siteUrl": self._require_site(),
                "url": target,
                "page": number,
                "filterProperties": dict(_CHILD_FILTER_PROPERTIES),
            },
            retry_post=True,
        )
        items, warnings = _mapping_items(payload)
        rows: list[Row] = [_url_info_row(item) for item in items]
        fetched_at = now_utc()
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=fetched_at,
            data_age_days=_newest_row_age(
                items, fetched_at, ("LastCrawledDate", "lastCrawledDate")
            ),
            rows=rows,
            total=len(rows),
            limitations=(PROVIDER_RETURNED_NO_ROWS,) if not rows else (),
            warnings=warnings,
        )

    def site_roles(self, *, include_all_subdomains: bool = False) -> ProviderResult[Row]:
        """Return the users and roles that can access this site (read-only).

        ``GetSiteRoles`` maps ``Email``, ``Role``, ``Site``, ``VerificationSite``,
        ``DelegatorEmail``, ``DelegatedCodeOwnerEmail``, ``Expired`` and ``Date``. JSON returns
        the role as an enum number (0 Administrator, 1 ReadOnly, 2 ReadWrite), so both
        ``role`` (name) and ``role_code`` are exposed. The delegated verification code is a
        secret and is deliberately not echoed.
        """
        items, warnings = self._fetch_rows(
            "GetSiteRoles",
            params={"includeAllSubdomains": "true" if include_all_subdomains else "false"},
        )
        rows: list[Row] = [_site_role_row(item) for item in items]
        fetched_at = now_utc()
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=fetched_at,
            data_age_days=_newest_row_age(items, fetched_at, ("Date", "date")),
            rows=rows,
            total=len(rows),
            limitations=(PROVIDER_RETURNED_NO_ROWS,) if not rows else (),
            warnings=warnings,
        )

    def user_sites(self) -> ProviderResult[Row]:
        """Return every site the API key can see, with its verification state.

        ``GetUserSites`` is account-scoped: it needs no ``siteUrl`` and works even when the
        project has no site configured. Only ``Url`` and ``IsVerified`` are exposed;
        authentication and DNS verification codes are secrets and are not echoed. Zero rows
        carry ``provider_returned_no_rows``.
        """
        payload = self._request_json("GET", "GetUserSites", params={"apikey": self._api_key})
        items, warnings = _mapping_items(payload)
        rows: list[Row] = [
            {
                "url": _as_str(_pick(item, "Url", "url")),
                "is_verified": _as_bool(_pick(item, "IsVerified", "isVerified")),
            }
            for item in items
        ]
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=rows,
            total=len(rows),
            limitations=(PROVIDER_RETURNED_NO_ROWS,) if not rows else (),
            warnings=warnings,
        )

    def submission_quota(self) -> ProviderResult[Row]:
        """Return the URL submission quota Bing reports for this site.

        The JSON shape is ``{"d": {"__type", "DailyQuota", "MonthlyQuota"}}`` and the two
        values are **remaining** submissions, not totals (verified against lv24.by). The row
        therefore exposes ``daily_remaining`` / ``monthly_remaining`` and keeps the provider
        object verbatim in ``raw``; used/total values are never computed. The monthly window
        can bind first and its reset schedule is undocumented, so both limitations are always
        attached.
        """
        payload = self._request_json(
            "GET",
            "GetUrlSubmissionQuota",
            params={"apikey": self._api_key, "siteUrl": self._require_site()},
        )
        item, warnings = _unwrap_single(payload)
        rows: list[Row] = []
        if item is not None:
            row = _quota_row(item)
            rows.append(row)
            if all(row[key] is None for key in _QUOTA_REMAINING_KEYS):
                warnings = (*warnings, UNEXPECTED_SHAPE_WARNING)
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=rows,
            total=len(rows),
            limitations=(
                QUOTA_MONTHLY_CAN_BIND_FIRST,
                QUOTA_MONTHLY_RESET_UNDOCUMENTED,
            ),
            warnings=warnings,
        )

    def feeds(self) -> ProviderResult[Row]:
        """Return the top-level sitemap feeds Bing knows for this site.

        ``GetFeeds`` returns ``Feed`` rows with ``Url``, ``Type``, ``Status``, ``Submitted``,
        ``LastCrawled``, ``UrlCount``, ``FileSize`` and ``Compressed``; there is no paging and
        no per-URL error text. ``Status`` is a free provider string (``Success`` observed), not
        a crawl or index status, so the result carries the matching limitations instead of
        pretending the feed is a full crawl report. ``LastCrawled`` bounds ``data_age_days``;
        a feed submitted but never crawled falls back to ``Submitted``.
        """
        return self._feed_result("GetFeeds")

    def feed_details(self, feed_url: str) -> ProviderResult[Row]:
        """Return the child feeds of one sitemap index, or the feed itself.

        ``GetFeedDetails`` is a plain ``GET`` with ``siteUrl`` and ``feedUrl``; it is only
        useful for sitemap-index children, so the CLI exposes it as
        ``bing sitemaps --details <feedUrl>`` rather than a separate command. The URL is
        validated before the call so a relative value is a caller error.
        """
        feed = _absolute_http_url(feed_url, name="feed_url")
        payload = self._request_json(
            "GET",
            "GetFeedDetails",
            params={"apikey": self._api_key, "siteUrl": self._require_site(), "feedUrl": feed},
        )
        items, warnings = _unwrap_rows(payload)
        return _feed_provider_result(
            items,
            warnings,
            site_url=self._site_url,
            fallback_date_keys=_FEED_DATE_KEYS,
        )

    def _feed_result(self, path: str) -> ProviderResult[Row]:
        payload = self._request_json(
            "GET",
            path,
            params={"apikey": self._api_key, "siteUrl": self._require_site()},
        )
        items, warnings = _unwrap_rows(payload)
        return _feed_provider_result(
            items,
            warnings,
            site_url=self._site_url,
            fallback_date_keys=_FEED_DATE_KEYS,
        )

    def submit_urls(self, urls: Sequence[str]) -> ProviderResult[Row]:
        """Submit up to 500 URLs through ``SubmitUrlbatch``.

        This is the low-level single-batch primitive; the caller owns the quota pre-check.
        URLs are deduplicated before the call because Bing documents no idempotency and a
        duplicate may consume quota. A success is ``{"d": null}`` with no per-URL status, so
        the row says ``accepted`` and never claims the URLs were indexed.
        """
        raw = _collect_url_list(urls)
        batch = _dedupe(raw)
        _check_url_limit(
            len(batch),
            limit=SUBMIT_BATCH_LIMIT,
            limit_action=f"split into batches of {SUBMIT_BATCH_LIMIT}",
        )
        self._post_url_batch(batch)
        row: Row = {
            "accepted": len(batch),
            "submitted": len(batch),
            "duplicates_removed": len(raw) - len(batch),
            "status": "accepted",
            "site_url": self._site_url,
        }
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=[row],
            limitations=(SUBMIT_IDEMPOTENCY_UNDOCUMENTED,),
        )

    def submit_url_batches(
        self, urls: Sequence[str], *, batch_size: int = SUBMIT_BATCH_LIMIT
    ) -> ProviderResult[Row]:
        """Submit any number of URLs in explicit ``SubmitUrlbatch`` batches.

        Bing accepts at most 500 URLs per call. The list is deduplicated, the remaining quota
        is checked once before the first call (daily and monthly, because the monthly window
        can bind first), and a larger list is split rather than truncated. The aggregate row
        reports ``accepted`` URLs and batches. If a later batch fails, the raised error carries
        ``submitted`` and ``batches_completed`` so the caller knows an earlier batch may
        already be live.
        """
        size = _batch_size(batch_size)
        raw = _collect_url_list(urls)
        collected = _dedupe(raw)
        self._precheck_quota(len(collected))

        submitted = 0
        completed = 0
        for start in range(0, len(collected), size):
            chunk = collected[start : start + size]
            try:
                self._post_url_batch(chunk)
            except SeoError as exc:
                exc.detail.setdefault("submitted", submitted)
                exc.detail.setdefault("batches_completed", completed)
                raise
            submitted += len(chunk)
            completed += 1

        row: Row = {
            "accepted": submitted,
            "submitted": submitted,
            "batches": completed,
            "batch_size": size,
            "duplicates_removed": len(raw) - len(collected),
            "status": "accepted",
            "site_url": self._site_url,
        }
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=[row],
            limitations=(SUBMIT_IDEMPOTENCY_UNDOCUMENTED,),
        )

    def _precheck_quota(self, requested: int) -> None:
        """Refuse a submission that exceeds the remaining daily or monthly quota."""
        quota = self.submission_quota()
        row = quota.rows[0] if quota.rows else {}
        checks = (
            ("daily_remaining", _as_int(row.get("daily_remaining"))),
            ("monthly_remaining", _as_int(row.get("monthly_remaining"))),
        )
        if all(value is None for _name, value in checks):
            raise SeoError(
                code=UPSTREAM_UNAVAILABLE,
                message="Bing did not report remaining URL submission quota",
                next_action="retry when the quota endpoint answers; refusing to submit blind",
                retryable=True,
            )
        for name, value in checks:
            if value is not None and value < requested:
                raise SeoError(
                    code=QUOTA_EXCEEDED,
                    message=(
                        f"Bing {name.replace('_', ' ')} is {value}, below the "
                        f"{requested} URL(s) requested"
                    ),
                    next_action=(
                        "wait for the quota window to reset or submit fewer URLs; "
                        "the monthly window can bind before the daily one"
                    ),
                    retryable=False,
                    detail={name: value, "requested": requested},
                )

    def _post_url_batch(self, batch: Sequence[str]) -> None:
        self._request_json(
            "POST",
            "SubmitUrlbatch",
            params={"apikey": self._api_key},
            body={"siteUrl": self._require_site(), "urlList": list(batch)},
        )

    def submit_feed(self, sitemap_url: str) -> ProviderResult[Row]:
        """Submit one sitemap through the current JSON ``SubmitFeed`` endpoint.

        Bing retired the SOAP/POX sitemap APIs; the JSON endpoint answers ``{"d": null}`` on
        success. The sitemap URL is validated before the network call so an empty or relative
        value is a caller error, never a provider rejection that looks like a Bing outage.
        Errors are parsed through :meth:`_request_json`: a Bing ``ErrorCode`` wins over the
        HTTP status (3 is ``AUTH_INVALID``, 14 is a site/authorization problem, 7 is
        ``INVALID_INPUT``, 4/5 are quota/rate-limit); 401/403/429/404/5xx remain fallbacks.
        The API key never reaches a message: only ``host + path`` is reported.
        """
        feed = _absolute_http_url(sitemap_url, name="sitemap_url")
        self._request_json(
            "POST",
            "SubmitFeed",
            params={"apikey": self._api_key},
            body={"siteUrl": self._require_site(), "feedUrl": feed},
        )
        row: Row = {"submitted": True, "site_url": self._site_url, "sitemap_url": feed}
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=[row],
        )

    def _fetch_rows(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[list[Mapping[str, Any]], tuple[str, ...]]:
        query: dict[str, Any] = {"apikey": self._api_key, "siteUrl": self._require_site()}
        if params:
            query.update(params)
        payload = self._request_json("GET", path, params=query)
        return _mapping_items(payload)

    def _window_result(
        self,
        items: Sequence[Mapping[str, Any]],
        rows: list[Row],
        warnings: tuple[str, ...],
        *,
        limitations: tuple[str, ...] = (),
    ) -> ProviderResult[Row]:
        """Build a dated stats result and mark an empty window as missing, not zero."""
        fetched_at = now_utc()
        notes = limitations
        if not rows:
            notes = (*notes, NO_ROWS_FOR_WINDOW)
        return ProviderResult(
            source="bing",
            property_id=self._site_url,
            fetched_at=fetched_at,
            data_age_days=_newest_row_age(items, fetched_at, ("Date", "date")),
            rows=rows,
            total=len(rows),
            limitations=notes,
            warnings=warnings,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any],
        body: Any = None,
        retry_post: bool = False,
    ) -> Any:
        """Perform one JSON call and map a Bing ``ErrorCode`` before the HTTP status.

        The shared client normally maps a non-2xx response to a generic error. Bing sends
        auth failures as HTTP 400 with ``{"ErrorCode": 3, "Message": ...}``, so the body is
        needed. ``Message`` is never echoed: it could carry request context, and the API key
        must never reach an error. Only ``host + path``, the status and the numeric code are
        kept in ``detail``. ``retry_post`` is set only for safe read POSTs such as
        ``GetChildrenUrlInfo``.
        """
        url = f"{self._base_url}/{path}"
        kwargs: dict[str, Any] = {
            "params": params,
            "rate_limit_key": "bing",
            "raise_for_status": False,
        }
        if body is not None:
            kwargs["json"] = body
        if retry_post:
            kwargs["retry_post"] = True
        response = self._http.request(method, url, **kwargs)
        try:
            if response.is_error:
                raise _bing_status_error(response, target=_endpoint_label(url))
            try:
                return response.json()
            except ValueError as exc:
                raise SeoError(
                    code=UPSTREAM_UNAVAILABLE,
                    message=f"invalid JSON response from {_endpoint_label(url)}",
                    retryable=False,
                    detail={"status_code": response.status_code, "target": _endpoint_label(url)},
                ) from exc
        finally:
            response.close()


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
        response = client.request(
            "POST", target, json=payload, follow_redirects=True, rate_limit_key="bing"
        )
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


def _unwrap_rows(payload: Any) -> tuple[list[Any], tuple[str, ...]]:
    """Like :func:`_unwrap_items`, but accept one WCF object as a one-row list."""
    if isinstance(payload, Mapping):
        inner = payload.get("d")
        if isinstance(inner, Mapping):
            return [inner], ()
    return _unwrap_items(payload)


def _mapping_items(payload: Any) -> tuple[list[Mapping[str, Any]], tuple[str, ...]]:
    """Unwrap a response and keep only object rows, flagging skipped scalars."""
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


def _unwrap_single(payload: Any) -> tuple[Mapping[str, Any] | None, tuple[str, ...]]:
    """Return the one object of a WCF ``{"d": {...}}`` response, or ``None``."""
    candidate: Any = payload
    if isinstance(payload, Mapping) and "d" in payload:
        candidate = payload["d"]
    if isinstance(candidate, Mapping):
        return candidate, ()
    if isinstance(candidate, list):
        if not candidate:
            return None, ()
        if len(candidate) == 1 and isinstance(candidate[0], Mapping):
            return candidate[0], ()
    return None, (UNEXPECTED_SHAPE_WARNING,)


_FEED_DATE_KEYS = ("LastCrawled", "lastCrawled", "Submitted", "submitted")


def _feed_provider_result(
    items: Sequence[Any],
    warnings: tuple[str, ...],
    *,
    site_url: str,
    fallback_date_keys: tuple[str, ...],
) -> ProviderResult[Row]:
    mapped: list[Mapping[str, Any]] = []
    skipped = False
    for item in items:
        if isinstance(item, Mapping):
            mapped.append(item)
        else:
            skipped = True
    if skipped:
        warnings = (*warnings, MALFORMED_ROWS_WARNING)
    rows = [_feed_row(item) for item in mapped]
    fetched_at = now_utc()
    return ProviderResult(
        source="bing",
        property_id=site_url,
        fetched_at=fetched_at,
        data_age_days=_newest_row_age(mapped, fetched_at, fallback_date_keys),
        rows=rows,
        total=len(rows),
        limitations=(NO_PER_URL_SITEMAP_ERRORS, SITEMAP_STATUS_NOT_CRAWL_STATUS),
        warnings=warnings,
    )


def _feed_row(item: Mapping[str, Any]) -> Row:
    raw = {
        str(key): value for key, value in item.items() if str(key).lower() not in _FEED_MAPPED_KEYS
    }
    return {
        "url": _pick(item, "Url", "URL", "url"),
        "type": _pick(item, "Type", "type"),
        "status": _pick(item, "Status", "status"),
        "submitted": _pick(item, "Submitted", "submitted"),
        "last_crawled": _pick(item, "LastCrawled", "lastCrawled"),
        "url_count": _as_int(_pick(item, "UrlCount", "urlCount")),
        "file_size": _as_int(_pick(item, "FileSize", "fileSize")),
        "compressed": _as_bool(_pick(item, "Compressed", "compressed")),
        "raw": raw,
    }


def _quota_row(item: Mapping[str, Any]) -> Row:
    # Keep the provider object verbatim: DailyQuota/MonthlyQuota are *remaining* values and
    # the raw row is the only place a future field can appear without a schema change.
    raw = {str(key): value for key, value in item.items()}
    return {
        "daily_remaining": _as_int(_pick(item, "DailyQuota", "dailyQuota")),
        "monthly_remaining": _as_int(_pick(item, "MonthlyQuota", "monthlyQuota")),
        "raw": raw,
    }


def _newest_row_age(
    items: Sequence[Mapping[str, Any]],
    fetched_at: datetime,
    keys: tuple[str, ...],
) -> int:
    """Age in whole days of the newest dated row; ``0`` when no row carries a date."""
    newest: datetime | None = None
    for item in items:
        parsed = _parse_row_date(_pick(item, *keys))
        # Year 1 is WCF DateTime.MinValue, Bing's "never" sentinel, not a real age.
        if parsed is not None and parsed.year > 1 and (newest is None or parsed > newest):
            newest = parsed
    if newest is None:
        return 0
    return data_age_days(end=newest, fetched_at=fetched_at)


def _optional_date(value: Any) -> str | None:
    """Return an ISO-8601 UTC timestamp, or ``None`` for a missing/never sentinel date."""
    parsed = _parse_row_date(value)
    if parsed is None or parsed.year <= 1:
        return None
    return parsed.astimezone(UTC).isoformat()


def _parse_row_date(value: Any) -> datetime | None:
    """Parse a WCF ``/Date(ms)/`` value, an ISO string, an epoch or a date object."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, bool):
        return None
    elif isinstance(value, int | float):
        # WCF JSON dates are milliseconds since the Unix epoch. Windows cannot convert
        # the DateTime.MinValue sentinel, so a failed conversion is "no date", not a crash.
        if float(value) <= _WCF_NULL_DATE_MS:
            return None
        try:
            parsed = datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("/Date(") and text.endswith(")/"):
            text = text[len("/Date(") : -len(")/")]
            # WCF appends the display offset, e.g. /Date(1315781995040-0700)/. The epoch
            # value is already absolute, so the offset is dropped, not applied.
            if len(text) > 5 and text[-5] in "+-" and text[-4:].isdigit():
                text = text[:-5]
        try:
            if text.isdigit():
                parsed = datetime.fromtimestamp(float(text) / 1000.0, tz=UTC)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (OverflowError, OSError, ValueError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _query_row(item: Mapping[str, Any]) -> Row:
    return {
        "query": _as_str(_pick(item, "Query", "query")),
        "clicks": _as_int(_pick(item, "Clicks", "clicks")),
        "impressions": _as_int(_pick(item, "Impressions", "impressions")),
        "position": _as_float(
            _pick(
                item,
                "AvgImpressionPosition",
                "AvgImpressionPos",
                "avgImpressionPosition",
                "position",
            )
        ),
    }


def _page_row(item: Mapping[str, Any]) -> Row:
    """Map one ``GetPageStats`` row: ``Query`` is the page URL, not a search query."""
    return {
        "page": _as_str(_pick(item, "Query", "query")),
        "clicks": _as_int(_pick(item, "Clicks", "clicks")),
        "impressions": _as_int(_pick(item, "Impressions", "impressions")),
        "click_position": _as_float(_pick(item, "AvgClickPosition", "avgClickPosition")),
        "impression_position": _as_float(
            _pick(item, "AvgImpressionPosition", "AvgImpressionPos", "avgImpressionPosition")
        ),
        "date": _optional_date(_pick(item, "Date", "date")),
    }


def _join_row(item: Mapping[str, Any], *, query: str | None, page: str | None) -> Row:
    """One query↔page row with both dimensions explicit and a stable key order."""
    return {
        "query": query,
        "page": page,
        "clicks": _as_int(_pick(item, "Clicks", "clicks")),
        "impressions": _as_int(_pick(item, "Impressions", "impressions")),
        "click_position": _as_float(_pick(item, "AvgClickPosition", "avgClickPosition")),
        "impression_position": _as_float(
            _pick(item, "AvgImpressionPosition", "AvgImpressionPos", "avgImpressionPosition")
        ),
        "position": _as_float(_pick(item, "Position", "position")),
        "date": _optional_date(_pick(item, "Date", "date")),
    }


def _crawl_stats_row(item: Mapping[str, Any]) -> Row:
    return {
        "date": _optional_date(_pick(item, "Date", "date")),
        "code_2xx": _as_int(_pick(item, "Code2xx", "code2xx")),
        "code_301": _as_int(_pick(item, "Code301", "code301")),
        "code_302": _as_int(_pick(item, "Code302", "code302")),
        "code_4xx": _as_int(_pick(item, "Code4xx", "code4xx")),
        "code_5xx": _as_int(_pick(item, "Code5xx", "code5xx")),
        "blocked_by_robots_txt": _as_int(_pick(item, "BlockedByRobotsTxt", "blockedByRobotsTxt")),
        "contains_malware": _as_int(_pick(item, "ContainsMalware", "containsMalware")),
        "crawl_errors": _as_int(_pick(item, "CrawlErrors", "crawlErrors")),
        "crawled_pages": _as_int(_pick(item, "CrawledPages", "crawledPages")),
        "in_index": _as_int(_pick(item, "InIndex", "inIndex")),
        "in_links": _as_int(_pick(item, "InLinks", "inLinks")),
        "all_other_codes": _as_int(_pick(item, "AllOtherCodes", "allOtherCodes")),
    }


def _url_info_row(item: Mapping[str, Any]) -> Row:
    return {
        "url": _as_str(_pick(item, "Url", "URL", "url")),
        "anchor_count": _as_int(_pick(item, "AnchorCount", "anchorCount")),
        "discovery_date": _optional_date(_pick(item, "DiscoveryDate", "discoveryDate")),
        "document_size": _as_int(_pick(item, "DocumentSize", "documentSize")),
        "http_status": _as_int(_pick(item, "HttpStatus", "httpStatus")),
        "is_page": _as_bool(_pick(item, "IsPage", "isPage")),
        "last_crawled_date": _optional_date(_pick(item, "LastCrawledDate", "lastCrawledDate")),
        "total_child_url_count": _as_int(_pick(item, "TotalChildUrlCount", "totalChildUrlCount")),
    }


def _site_role_row(item: Mapping[str, Any]) -> Row:
    code = _as_int(_pick(item, "Role", "role"))
    name = _as_str(_pick(item, "Role", "role")) or (
        _ROLE_NAMES.get(code) if code is not None else None
    )
    return {
        "email": _as_str(_pick(item, "Email", "email")),
        "role": name,
        "role_code": code,
        "site": _as_str(_pick(item, "Site", "site")),
        "verification_site": _as_str(_pick(item, "VerificationSite", "verificationSite")),
        "delegator_email": _as_str(_pick(item, "DelegatorEmail", "delegatorEmail")),
        "delegated_code_owner_email": _as_str(
            _pick(item, "DelegatedCodeOwnerEmail", "delegatedCodeOwnerEmail")
        ),
        "expired": _as_bool(_pick(item, "Expired", "expired")),
        "date": _optional_date(_pick(item, "Date", "date")),
    }


def _crawl_row(item: Mapping[str, Any]) -> Row:
    raw = {
        str(key): value for key, value in item.items() if str(key).lower() not in _CRAWL_MAPPED_KEYS
    }
    bits = _as_int(_pick(item, "Issues", "issues")) or 0
    return {
        "url": _pick(item, "Url", "URL", "url"),
        "http_code": _as_int(_pick(item, "HttpCode", "httpCode", "http_code")),
        "issues": _issue_labels(bits),
        "issues_bits": bits,
        "in_links": _as_int(_pick(item, "InLinks", "inLinks", "in_links")),
        "raw": raw,
    }


def _issue_labels(bits: int) -> list[str]:
    """Decode Bing's ``Issues`` bitmask into stable labels."""
    return [label for bit, label in _CRAWL_ISSUE_BITS if bits & bit]


def _bing_status_error(response: Any, *, target: str) -> SeoError:
    """Map a Bing JSON error, preferring ``ErrorCode`` over the HTTP status.

    Bing returns auth failures as HTTP 400 with ``{"ErrorCode": 3, ...}``. Codes 4/5 are
    rate-limit/quota, 7 is a bad request and 14 means the site is not authorized. The
    provider ``Message`` is never echoed: only the status, the numeric code and the endpoint
    (host + path) are kept. 401/403/404/429/5xx remain fallbacks for responses without a code.
    """
    payload = _safe_json(response)
    error_code = _bing_error_code(payload)
    detail: dict[str, Any] = {"status_code": response.status_code, "target": target}
    if error_code is not None:
        detail["error_code"] = error_code

    if error_code == 3:
        return SeoError(
            code=AUTH_INVALID,
            message="Bing rejected the API key (ErrorCode 3)",
            next_action=_AUTH_NEXT_ACTION,
            detail=detail,
        )
    if error_code == 14:
        return SeoError(
            code=PROPERTY_NOT_FOUND,
            message="Bing rejected the site or its authorization (ErrorCode 14)",
            next_action=(
                "verify providers.bing.site is an exact, verified site URL in Bing Webmaster Tools"
            ),
            detail=detail,
        )
    if error_code == 7:
        return SeoError(
            code=INVALID_INPUT,
            message="Bing rejected the request (ErrorCode 7)",
            next_action="check the site URL and request parameters",
            detail=detail,
        )
    if error_code in {4, 5}:
        return SeoError(
            code=QUOTA_EXCEEDED,
            message=f"Bing rate limit or quota exhausted (ErrorCode {error_code})",
            next_action="wait for the quota window to reset, then retry",
            retryable=True,
            detail=detail,
        )

    status = response.status_code
    if status in {401, 403}:
        return SeoError(
            code=AUTH_INVALID,
            message=f"Bing rejected the API key (HTTP {status})",
            next_action=_AUTH_NEXT_ACTION,
            detail=detail,
        )
    if status == 404:
        return SeoError(
            code=PROPERTY_NOT_FOUND,
            message="Bing did not find the site (HTTP 404)",
            next_action="verify providers.bing.site in Bing Webmaster Tools",
            detail=detail,
        )
    if status == 429:
        return SeoError(
            code=QUOTA_EXCEEDED,
            message="Bing rate limit or quota exhausted (HTTP 429)",
            next_action="wait for the quota window to reset, then retry",
            retryable=True,
            detail=detail,
        )
    if status >= 500:
        return SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"Bing is unavailable (HTTP {status})",
            retryable=True,
            detail=detail,
        )
    return SeoError(
        code=INVALID_INPUT,
        message=f"Bing rejected the request (HTTP {status})",
        next_action="check the site URL and request parameters",
        detail=detail,
    )


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _bing_error_code(payload: Any) -> int | None:
    candidates: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        inner = payload.get("d")
        if isinstance(inner, Mapping):
            candidates.append(inner)
        candidates.append(payload)
    for candidate in candidates:
        code = _as_int(_pick(candidate, "ErrorCode", "errorCode", "error_code"))
        if code is not None:
            return code
    return None


def _endpoint_label(url: str) -> str:
    """``scheme://host/path`` without the query string, so ``apikey`` cannot leak."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


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


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _required_text(value: str, *, name: str) -> str:
    """Return trimmed text or raise ``INVALID_INPUT`` before a network call."""
    text = value.strip()
    if not text:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must not be empty",
            next_action=f"pass a non-empty {name}",
        )
    return text


def _url_info_target(value: str, *, name: str) -> str:
    """Accept an absolute URL or Bing's ``domain:<host>`` form for URL info calls."""
    text = value.strip()
    if not text:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must not be empty",
            next_action=f"pass an absolute URL or domain:<host> to {name}",
        )
    if text.lower().startswith("domain:"):
        host = text[len("domain:") :].strip()
        if not host or "/" in host or " " in host:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"{name} has an invalid domain: target",
                next_action=f"pass domain:example.com to {name}",
                detail={"value": text},
            )
        return f"domain:{host}"
    return _absolute_http_url(text, name=name)


def _page_number(value: int) -> int:
    """Validate the ``GetChildrenUrlInfo`` page (a provider ``ushort``)."""
    valid = (
        not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= CHILDREN_PAGE_MAX
    )
    if not valid:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"page must be between 0 and {CHILDREN_PAGE_MAX}",
            next_action="pass a zero-based page number",
            detail={"page": value, "limit": CHILDREN_PAGE_MAX},
        )
    return value


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
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{name} must be an absolute http(s) URL",
            next_action=f"pass a URL such as https://example.com/sitemap.xml to {name}",
            detail={"value": url},
        )
    return url


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


def _dedupe(values: Sequence[str]) -> list[str]:
    """Deduplicate while preserving order; Bing has no documented idempotency."""
    return list(dict.fromkeys(values))


def _batch_size(value: int) -> int:
    valid = (
        not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= SUBMIT_BATCH_LIMIT
    )
    if not valid:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"batch_size must be between 1 and {SUBMIT_BATCH_LIMIT}",
            next_action=f"use the default {SUBMIT_BATCH_LIMIT} or a smaller positive batch",
            detail={"batch_size": value, "limit": SUBMIT_BATCH_LIMIT},
        )
    return value


def _collect_urls(urls: Sequence[str], *, limit: int, limit_action: str) -> list[str]:
    collected = _collect_url_list(urls)
    _check_url_limit(len(collected), limit=limit, limit_action=limit_action)
    return collected


def _check_url_limit(count: int, *, limit: int, limit_action: str) -> None:
    if count > limit:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"too many URLs in one call: {count} > {limit}",
            next_action=limit_action,
            detail={"count": count, "limit": limit},
        )


def _collect_url_list(urls: Sequence[str]) -> list[str]:
    """Validate every URL string and return them in order; an empty list is an error."""
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
    return collected
