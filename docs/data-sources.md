# Data sources, quotas and honesty rules

Every number this tool prints comes from one of the sources below. Each has hard limits; the tool
must surface them instead of pretending the data is complete.

Legend: **R** = read, **W** = write (gated), *free* = no payment required.

## 1. Google Search Console (GSC)

| | |
|---|---|
| Auth | OAuth 2.0 user token (`webmasters.readonly` for every read; `webmasters` is required for sitemap submit/delete and is **not** requested) or a service-account JSON key added to the property as Full/Owner |
| Base | `https://www.googleapis.com/webmasters/v3/` |
| Free quota | 1,200 QPM per site and per user; 40k QPM / 30M QPD per project; **50,000 rows/day/site/type** for search analytics; URL Inspection 2,000/day/site, 600/min |
| Methods | `POST /sites/{siteUrl}/searchAnalytics/query` (**R**), `GET /sites` (**R**), `GET /sites/{siteUrl}/sitemaps` (**R**), `GET /sites/{siteUrl}/sitemaps/{feedpath}` (**R**), `PUT/DELETE /sites/{siteUrl}/sitemaps/{feedpath}` (**W**, disabled) |
| URL Inspection | `POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect` (**R**, one URL per call, no batch, no live test) |
| Dimensions | `query, page, country, device, date, hour, searchAppearance` |
| Types | `web, image, video, news, discover, googleNews, webMultimodal` |
| Latency | 2–3 days; `dataState=all` returns fresher but incomplete data (`metadata.first_incomplete_date`) |

**URL Inspection.** One URL per call (2,000/day/site, 600/min). The adapter maps the full
`indexStatusResult` (`verdict`, `coverageState`, `robotsTxtState`, `indexingState`,
`lastCrawlTime`, `pageFetchState`, `googleCanonical`, `userCanonical`, `crawledAs`, `sitemap[]`,
`referringUrls[]`), `richResultsResult` (`verdict`, `detectedItems[].richResultType`, per-item
`issues[].issueMessage`/`severity`), `inspectionResultLink` and the request `languageCode`
(default `en-US`; it localizes `coverageState` but not verdicts or canonicals). It is a report
about Google's **last crawl**, not a live test, and the result carries
`one_url_per_call`, `url_inspection_quota_2000_per_day` and `inspection_reflects_last_crawl`.

**Sitemaps.** `sitemaps.list`/`sitemaps.get` map `path`, `lastSubmitted`, `lastDownloaded`,
`isPending`, `isSitemapsIndex`, `type`, `errors`, `warnings` and
`contents[{type,submitted,indexed}]`. GSC's `indexed` count is deprecated (always 0), so each
content item carries `indexed_deprecated: true` and the result carries
`sitemap_indexed_count_deprecated`. A sitemap index reports per-type totals only: GSC does not
expose the child sitemap URLs, so `sitemap_index_children_not_exposed` is added instead of
inventing children. Submit/delete require the full `webmasters` scope and remain
`WRITE_DISABLED`; the engine signs `webmasters.readonly` only.

**Search Analytics.** The CLI forwards `type`, `dimensionFilterGroups[].filters[]`,
`dimensionFilterGroups[].filterLists[]`, `dataState` (`all`/`final`/`hourly_all`),
`aggregationType` (`auto`/`byPage`/`byProperty`/`byNewsShowcasePanel`), a
`searchAppearance` filter and a `brandedClassification` filter (`BRANDED`/`NON_BRANDED`, only
`equals`, and never together with a `query` filter). Responses add
`meta.responseAggregationType` and `meta.metadata` (normalized
`first_incomplete_date`/`first_incomplete_hour`) without changing the row shape. Row limits are
unchanged: at most **25,000 rows per page** and **50,000 rows/day/site/type**.

**Hard rules for this tool**

1. Never request `page` and `query` together *and* compare the sums with an ungrouped total — grouping
   drops rows by design. Store both groupings separately, labelled.
2. Paginate with `startRow` until a short page. A final page that exactly fills the 50,000-row cap is
   reported as `truncated: true` with `rows_cap_50000`: a full page is not proof that no row exists
   behind the cap, so exact-cap data is never claimed complete. A truncated GSC result omits `total`
   (`null`): an exact row count next to `truncated: true` would read as a complete answer.
3. `data_age_days` is floored at the documented 3-day lag, so a window ending today is never
   presented as fresh data even when the local clock says it is 0 days old.
4. Cache daily pulls in SQLite; never re-query a month repeatedly (the "load quota" is opaque).
5. A service account cannot complete interactive verification for `sc-domain:` — a human publishes the
   DNS TXT once (or `sites.add` is called by an already-verified principal).
6. Auth resolution tries the service-account file first (`providers.gsc.service_account_file`, with
   `LV24_SEO_GSC_SERVICE_ACCOUNT_FILE` overriding it), then the `gsc_token` secret. The path must be
   absolute; the engine reads the key once, signs an RS256 JWT with `webmasters.readonly`, caches the
   access token and refreshes it 60 seconds before expiry. A plain `gsc_token` is a bearer access
   token and is not refreshed.

**Not available:** general URL submission (the Indexing API only supports `JobPosting`/`BroadcastEvent`),
a standalone rich-result validation API (URL Inspection reports detected items/issues for the one
inspected URL only), query data beyond 16 months, real-time data, and sitemap submit/delete from this
engine (full `webmasters` scope, disabled).

## 2. Yandex Webmaster

| | |
|---|---|
| Auth | `Authorization: OAuth <token>`, scope `webmaster:hostinfo` (+ `webmaster:verify` to add hosts) |
| Base | `https://api.webmaster.yandex.net/v4/` |
| Free quota | free; no published global RPD/RPS; `query-analytics/list` is fragile (>10k req/h → 429) |
| Identity | `GET /user/` → `user_id`; hosts via `GET /user/{uid}/hosts/`; `host_id` format `https:example.com:443` (never guess it) |
| Read | `summary`, `sqi-history`, `indexing/history`, `indexing/samples`, `search-urls/in-search/history`, `search-urls/events/history`, `search-urls/events/samples`, `search-queries/popular`, `search-queries/all/history`, `search-queries/{query_id}/history`, `diagnostics`, `links/internal/broken`, `sitemaps`, `sitemaps/{id}`, `recrawl/quota`, `recrawl/queue`, `recrawl/queue/{task_id}` |
| Write | `POST /user/{uid}/hosts/{host}/recrawl/queue` (**W**, one URL per request, hard daily quota), `POST /sitemaps/{id}/recrawl` (**W**, not implemented), `POST /user-added-sitemaps/` (**W**, not implemented) |
| Query data | top **3,000** queries per period, ≤500 per page; **no query × page join** |
| Latency | ~4 days; the service can return `HOST_NOT_LOADED` during maintenance |

**Hard rules**

1. Resolve `user_id` and `host_id` on every cold start; cache only successful lookups.
2. Repeated query params, not comma lists (`?query_indicator=TOTAL_SHOWS&query_indicator=TOTAL_CLICKS`).
3. `401` = token dead, `403` = host not verified for this account. Never re-auth on 403.
4. Recrawl: read `GET /recrawl/quota` first, queue only up to the remaining quota, store task ids.
   There is no batch endpoint: one `POST /recrawl/queue {"url": ...}` per URL. `202` returns
   `{task_id, quota_remainder}`, `409 URL_ALREADY_ADDED` is a benign duplicate and `429
   QUOTA_EXCEEDED {daily_quota, exceeded_until}` stops the loop; earlier task ids stay reported.
5. Percent-encode Cyrillic URLs; send a descriptive `User-Agent` and `Accept: application/json`.
6. An **empty** indexing or search-URL history is a real answer, not an outage: the host replied,
   there are simply no buckets or events in the window. It returns `rows: []`, `total: 0` and
   `limitations: ["empty_history"]`, never `degraded_host`. Only a host-scoped `404` or an explicit
   `HOST_NOT_LOADED` payload is degraded.
7. Pagination totals come from explicit grand-total keys only (`total`, `total_queries`,
   `total_count`). A bare `count` is not trusted as a grand total: if it is page-scoped, using it
   as the total would stop paging early and hide rows. The walk therefore ends on a short page or
   at the documented 3,000-query cap.

**Field mapping (v4)**

- `search-queries/popular` → one row per query: `query`, `query_id`, `shows` (`TOTAL_SHOWS`),
  `clicks` (`TOTAL_CLICKS`), `position` (`AVG_SHOW_POSITION`) and `avg_click_position`
  (`AVG_CLICK_POSITION`). Only returned indicators are written; a missing legacy key is `null`,
  never `0`. An indicator the client does not know is kept under `extra_indicators`, so the row
  is never dropped.
- `summary` → `sqi`, `pages_in_search` (`searchable_pages_count`), `pages_excluded`
  (`excluded_pages_count`) and `site_problems` (severity → count: `FATAL`, `CRITICAL`,
  `POSSIBLE_PROBLEM`, `RECOMMENDATION`). The current schema does not list HTTP status counters
  here; if a host returns `http_2xx`…`http_5xx` (upper- or lowercase) they are passed through, and
  an omitted counter stays absent.
- `indexing/history` → the documented `indicators` object with `HTTP_2XX`, `HTTP_3XX`, `HTTP_4XX`,
  `HTTP_5XX` and `OTHER` series of `{date, value}`. Series are merged into one row per date
  (`date`, `http_2xx`, `http_3xx`, `http_4xx`, `http_5xx`, `other`); an omitted series stays
  absent. Unknown status series are kept under `extra_indicators`. Dates are returned as the
  provider sends them, and the legacy `history` list form is still accepted unchanged.
- `search-urls/events/history` → the documented `indicators` object with `APPEARED_IN_SEARCH` and
  `REMOVED_FROM_SEARCH` series. Series are merged into one row per date (`date`, `appeared`,
  `removed`); an omitted series stays absent and an unknown event series is kept under
  `extra_indicators`. The legacy `events` list form is still accepted. A real live window on
  2026-10-02 returned `appeared: 57, removed: 0`; an empty answer is `empty_history`, not
  `degraded_host`. Live 2026-11-17: the endpoint closed the connection when `date_from/date_to`
  were present, so the client retries once without the window, filters the full series locally and
  reports `client_side_date_filter` in `warnings` and `limitations`.
- `indexing/samples` and `search-urls/events/samples` → `{count, samples[]}`. Indexing samples
  carry `status`, `http_code`, `url`, `access_date`; event samples carry `url`, `title`,
  `event_date`, `last_access`, `event`, `excluded_url_status`, `bad_http_status`, `target_url`.
  Both page at 100 rows and report `count` as the grand total, so `meta.total` and
  `meta.truncated` are honest even when the caller capped the rows. A live check on 2026-11-17
  found `indexing/samples` closing the connection without a response for one host (surfaced as
  `UPSTREAM_UNAVAILABLE`, never as an empty list) while `search-urls/events/samples` answered
  normally.
- `sitemaps` and `sitemaps/{id}` → `sitemap_id`, `sitemap_url`, `last_access_date`, `errors_count`,
  `urls_count`, `children_count`, `sources[]`, `sitemap_type`. `parent_id` lists the children of a
  sitemap index; `from` is a sitemap-id cursor. Add/delete are not implemented.
- `sqi-history` → `points[{date, value}]`; `search-urls/in-search/history` → `history[{date,
  value}]` (pages in search); `search-queries/all/history` and
  `search-queries/{query_id}/history` → `indicators` series. The query history calls must send the
  four repeated `query_indicator` parameters, otherwise Yandex returns an empty `indicators`
  object. Per-query history uses the same series keys as `all/history`.
- `recrawl/queue` and `recrawl/queue/{task_id}` → `task_id`, `url`, `added_time`, `state`; the
  list reports the provider's `count` as `meta.total`. The queue read is read-only.

**Not available via API:** per-URL sitemap error detail, diagnostic recommendation text, Webmaster
notification settings, Yandex Business card/reviews.

## 3. Yandex Metrika

| | |
|---|---|
| Auth | `Authorization: OAuth <token>`, scope `metrika:read` |
| Base | `https://api-metrika.yandex.net/stat/v1/` |
| Free quota | 5,000 requests/day/user; 30 rps/IP; 3 parallel; `/stat/v1/data` 200 req / 5 min; ≤100k rows, ≤20 metrics, ≤10 dimensions per request |
| Useful dimensions | `ym:s:lastTrafficSource`, `ym:s:lastSearchEngineRoot`, `ym:s:lastSearchPhrase`, `ym:s:startURLPath`, `ym:s:deviceCategory`, `ym:s:regionCountry` |
| Useful metrics | `ym:s:visits`, `ym:s:users`, `ym:s:bounceRate`, `ym:s:avgVisitDurationSeconds`, `ym:s:pageDepth`, `ym:s:pageviews`, `ym:s:goal<id>reaches`, `ym:s:goal<id>conversionRate` |
| Sampling | `accuracy=medium` by default; `full` is slower and eats the 5-min quota |

**Hard rules**

1. Two dimensions max for daily reports (`date` + one), otherwise rows explode.
2. Check `sampled` / `sample_share` before publishing a number; store the accuracy flag with the value.
3. Organic only: `filters=ym:s:lastTrafficSource=='organic'`. `stats` and the weekly digest read
   organic and all-traffic aggregates separately (`organic_visits`/`organic_users` vs
   `all_visits`/`all_users`); all-traffic is never labelled SEO traffic.
4. Goal ids come from `GET /management/v1/counters/{id}/goals` once and are stored in config.
   `metrika goals` reads them from `providers.yandex_metrika.goals`; an empty list warns and makes
   no call instead of inventing metrics. Metrika caps a request at 20 metrics, so one call queries
   at most five goals (four metrics each); a longer list is reported as truncated.
5. The client uses documented `GET /stat/v1/data` and `GET /stat/v1/data/bytime` with query
   parameters. Offline contract tests check the method and parameters; live property acceptance
   still needs owner credentials.
6. `limit` is the page size, not the result size. The table is walked with `offset` until
   `total_rows` is reached, a short page arrives, or a cap stops the walk: at most **10,000 rows**
   and **100 pages** per call. A full page with no `total_rows` is reported as `truncated: true`
   instead of looking complete; a request without dimensions is treated as a single row.
7. Sampling fields are passed through, not flattened away: `sampled`, `sample_share`,
   `sample_size`, `sample_space`, `data_lag`, `contains_sensitive_data` and `total_rows_rounded`
   appear in `meta`; `sampled`, `total_rows_rounded` and `sensitive_data_hidden` become
   `limitations`, and a sampled table is never described as exact.
8. `data_age_days` is computed from the current window's `date2`; the previous window in the same
   comparison is older by design and never inflates the headline age.
9. Search phrases are partial by contract: Google withholds them, and Metrika hides phrases with
   fewer than 10 users. `metrika queries` states both limitations every time; the provider's
   `contains_sensitive_data` flag is carried when it appears.

**Exposed by `lv24-seo metrika`:** `pages` (`ym:s:startURLPath`), `queries`
(`ym:s:lastSearchPhrase` + `ym:s:lastSearchEngineRoot`), `devices` (`ym:s:deviceCategory`),
`countries` (`ym:s:regionCountry`), `engagement` (totals), `goals` (config ids) and `bytime`
(`group=day|week|month`). All are read-only, organic-filtered, bounded by `--limit` and expose
`data_age_days`, `truncated` and `limitations`.

**GA4 is a separate provider gap.** Google Analytics 4 data (sessions, conversions, channel
grouping) is not part of the Metrika adapter and is not queried by any command or MCP tool today; a
GA4 adapter would need its own OAuth/service-account path and provider contract.

## 4. Bing Webmaster + IndexNow

| | |
|---|---|
| Bing API | `https://ssl.bing.com/webmaster/api.svc/json/` — reads: `GetQueryStats`, `GetPageStats`, `GetQueryPageStats`, `GetPageQueryStats`, `GetQueryPageDetailStats`, `GetRankAndTrafficStats`, `GetCrawlStats`, `GetCrawlIssues`, `GetUrlInfo`, `GetChildrenUrlInfo`, `GetSiteRoles`, `GetUserSites`, `GetFeeds`, `GetFeedDetails`, `GetUrlSubmissionQuota`; writes: `SubmitUrlbatch`, `SubmitFeed` |
| Auth | one API key per user (Bing Webmaster → Settings → API access) |
| Free quota | free; remaining daily/monthly submission quota is reported by `GetUrlSubmissionQuota`, 500 URLs per `SubmitUrlbatch` |
| Freshness | `data_age_days` is the age of the newest dated row in that feed: `Date` for query/page/traffic/crawl/join stats, `LastCrawled` (falling back to `Submitted`) for `GetFeeds`, `LastCrawledDate` for `GetUrlInfo`/`GetChildrenUrlInfo`. `GetCrawlIssues` and `GetUserSites` have no date and report `0`, which is not proof of freshness. |
| IndexNow | `https://api.indexnow.org/indexnow` (also `yandex.com/indexnow`), key file `https://host/<key>.txt` (8–128 chars, `[A-Za-z0-9-]`) |
| Limits | 10k URLs per IndexNow POST; engines apply their own daily caps; **Google does not participate**. A non-root `keyLocation` only allows URLs under its directory prefix. |

**URL submission.** `SubmitUrlbatch` accepts at most **500 URLs** per call:
`POST .../SubmitUrlbatch?apikey=KEY` with `{"siteUrl": "...", "urlList": [...]}` and returns
`200 {"d": null}` with **no per-URL status**. The engine reports `accepted`, never "indexed",
deduplicates the list client-side and pre-checks the remaining quota. Bing documents no
idempotency: a duplicate URL may consume quota even though the call succeeds, so the result
carries `submit_idempotency_undocumented`; the write-gate ledger only prevents replaying the same
confirm token. `bing submit` splits a larger list into 500-URL batches, never truncates it, and
reports `accepted`, `batches`, `batch_size`, `duplicates_removed` and `quota_after`.

**Quota.** `GET .../GetUrlSubmissionQuota?siteUrl=...` returns
`{"d": {"__type", "DailyQuota", "MonthlyQuota"}}`. Verified against lv24.by: both values are
**remaining** submissions, not totals, so the row exposes `daily_remaining` /
`monthly_remaining` and keeps the provider object in `raw`; no used/total value is computed. The
monthly window can bind before the daily one (lv24.by: 1400 monthly vs 100 daily) and its reset
schedule is undocumented (`quota_monthly_can_bind_first`, `quota_monthly_reset_undocumented`).

**Sitemap feeds.** `GetFeeds` returns `{Compressed, FileSize, LastCrawled, Status, Submitted,
Type, Url, UrlCount}` with no paging. `Status` is a free provider string (`Success` observed),
not a crawl/index status; Bing exposes no per-URL errors, so `bing sitemaps` reports
`no_per_url_sitemap_errors` and `sitemap_status_is_not_crawl_status`. `GetFeedDetails` returns
the same row shape for the children of a sitemap index and is exposed as
`bing sitemaps --details FEED_URL`. `GetFeedDetails` is documented but was not verified against
a live response in this wave; `GetFeeds`, quota and crawl issues were.

**Crawl issues.** `GetCrawlIssues` returns `{Url, HttpCode, Issues, InLinks}`; `Issues` is an
integer bitmask, not a type/severity/date. The engine decodes `1=301`, `2=302`, `4=4xx`,
`8=5xx`, `16=robots`, `32=malware`, `64=important_robots`, `128=dns`, `256=timeout` into
`issues` labels, keeps `issues_bits`, and reports unknown bits as `unknown_crawl_issue_bits`
instead of guessing. The feed has no date, so `data_age_days` is `0`.

**Stats and URL diagnostics.** `GetQueryStats` and `GetPageStats` return weekly `QueryStats`
rows (`Clicks`, `Impressions`, `AvgClickPosition`, `AvgImpressionPosition`, `Date`);
`GetPageStats` puts the **page URL** in `Query`, so the CLI exposes it as `page`. The three
joins are the only query/page links Bing provides: `GetQueryPageStats(siteUrl,query)` lists
pages for a query, `GetPageQueryStats(siteUrl,page)` lists queries for a page, and
`GetQueryPageDetailStats(siteUrl,query,page)` returns daily `Clicks`/`Impressions`/`Position`
rows without the dimensions, so the requested pair is attached to each row. `bing joins`
returns one compact row shape: `query`, `page`, `clicks`, `impressions`, `click_position`,
`impression_position`, `position`, `date`.

`GetRankAndTrafficStats` is wired as `bing traffic`: daily `Date`, `Clicks`, `Impressions`
covering every Bing vertical since 2023-03-24. It was listed in this table before the adapter
existed; that gap is now closed. `GetCrawlStats` is the daily six-month window
(`Code2xx`, `Code301`, `Code302`, `Code4xx`, `Code5xx`, `BlockedByRobotsTxt`,
`ContainsMalware`, `CrawlErrors`, `CrawledPages`, `InIndex`, `InLinks`, `AllOtherCodes`), so
`bing crawl` always carries `crawl_stats_six_month_window`.

`GetUrlInfo(siteUrl,url)` accepts an absolute URL or `domain:<host>` and returns `Url`,
`AnchorCount`, `DiscoveryDate`, `DocumentSize`, `HttpStatus`, `IsPage`, `LastCrawledDate` and
`TotalChildUrlCount`. The WCF `Date(-62135596800000)` value is DateTime.MinValue, Bing's
"never" sentinel; it maps to `null` (and `never_crawled` when the last crawl is null), never
to a year-1 date or a huge age. `GetChildrenUrlInfo` is a read-only `POST`
(`{siteUrl,url,page,filterProperties}`) returning the same `UrlInfo` rows; `page` is
zero-based and the JSON `filterProperties` object is sent with all-zero (Any) filters. The
CLI walks at most 10 pages; a full final page marks the result `truncated` with
`children_page_cap`. Live check (lv24.by, 2026-11-17): `GetUrlInfo("domain:lv24.by")` reports
`TotalChildUrlCount: 8380` but `GetChildrenUrlInfo` returned 0 rows for the same target and
page 0/1, so `bing children` reports `provider_returned_no_rows` instead of pretending the
directory is empty.

`GetSiteRoles(siteUrl,includeAllSubdomains)` returns `Email`, `Role` (JSON enum:
0 Administrator, 1 ReadOnly, 2 ReadWrite), `Site`, `VerificationSite`, `DelegatorEmail`,
`DelegatedCodeOwnerEmail`, `Expired` and `Date`. `GetUserSites` is account-scoped and returns
`Url` and `IsVerified`. Delegated codes, authentication codes and DNS verification codes are
not echoed.

**Empty stats are missing data, not zero.** Live checks against lv24.by return 0 rows for the
stats endpoints. A 0-row stats response carries `no_rows_for_window`; a lookup/list that
returns no object or rows carries `provider_returned_no_rows`. Neither invents a date or a
zero-impression row, so an agent cannot mistake an empty window for a traffic drop.

**Errors.** Bing sends auth failures as **HTTP 400** with `{"ErrorCode": N, "Message": ...}`.
The adapter parses `ErrorCode` first: `3 -> AUTH_INVALID`, `14 -> PROPERTY_NOT_FOUND`,
`7 -> INVALID_INPUT`, `4/5 -> QUOTA_EXCEEDED`; `401/403/404/429/5xx` remain fallbacks. The
provider `Message` is never echoed and errors keep only `host + path`, status and numeric code,
so the API key cannot leak.

**Sitemap submit.** `SubmitFeed` is the current JSON endpoint:
`POST .../SubmitFeed?apikey=KEY` with `{"siteUrl": "...", "feedUrl": "https://host/sitemap.xml"}`
returns `200 {"d": null}`. The retired SOAP/POX endpoints are not used. The write is gated by
`--allow-write` plus the confirm token (MCP: `allow_write=true`), and the site/sitemap URL are
resolved before the request. GSC sitemap submit and Bing sitemap delete remain disabled; only
Bing submit is wired.

**One-click IndexNow.** The CLI can read the URL list from the project sitemap:
`lv24-seo indexnow submit --from-sitemap`. The host, key and key location resolve from
`providers.bing.site`, `providers.bing.indexnow_key` and
`providers.bing.indexnow_key_location` (default `https://<host>/<key>.txt`); both new fields are
public identifiers, safe in `seo.toml`, unlike the `bing_api_key` secret. The key must be 8–128
characters from `A-Z`, `a-z`, `0-9` and `-`; it is validated before the plan and never echoed.
When `keyLocation` is set, the protocol scopes the submission to that key file's directory: a key at
`https://example.com/catalog/<key>.txt` may submit only URLs under `https://example.com/catalog/`,
and the host must match. The engine checks this during the dry run (and again in the adapter), so a
bad location is `INVALID_INPUT` before any request. A `<urlset>` is read
directly and a `<sitemapindex>` is bounded to the root plus five child sitemaps and 10,000 URLs.
Explicit `--url` values are merged, URLs outside the host are skipped, and the envelope reports
`truncated`, `total` and `limitations`. The apply re-fetches the sitemap; a changed URL set
invalidates the plan token with `CONFIRM_REQUIRED`.

**IndexNow responses.** The protocol documents no per-URL status. `200` means accepted (never
"indexed"), `202` means accepted-pending: the engine still has to validate the key, so the result
carries a warning and `indexnow_accepted_pending`. `400` is an invalid format (`INVALID_INPUT`),
`403` is a missing/mismatched key file (`AUTH_INVALID`, with the exact expected key URL), `422` is a
host/key-schema mismatch (`INVALID_INPUT`) and `429` is potential spam or quota (`QUOTA_EXCEEDED`,
retryable). Error messages keep the host, path and status; the key appears only inside the public
key-file URL that the operator must publish.

## 5. Chrome UX Report (CrUX) and PageSpeed Insights (PSI)

| | |
|---|---|
| CrUX | `POST https://chromeuxreport.googleapis.com/v1/records:queryRecord?key=…` and `records:queryHistoryRecord` — 150 QPM/project, cannot be raised; 28-day rolling p75; 404 when a URL has too little traffic |
| PSI | `GET https://www.googleapis.com/pagespeedonline/v5/runPagespeed` — ~25k/day; Lighthouse lab categories, audits and (deprecated) field block |
| Metrics | LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 (p75) |

**Field vs lab.** CrUX is the field source of record. `lv24-seo cwv collect` queries it per URL and
`formFactor` (`PHONE`, `DESKTOP`, `TABLET`) and persists each p75 into `metric_daily` as
`source=crux`, `scope=<device>`, `key=<url>`, metric `LCP`/`INP`/`CLS`/`FCP`/`TTFB`, dated on the
fetch day. That is the exact shape the weekly CWV-by-template section reads. A 404 is "no field
data" (`low_traffic`), not a zero row.

The collection period is not guessed: `data_age_days` is `today - collectionPeriod.lastDate`, and
each row carries `first_date`/`last_date` while `meta.collection_period` carries the same bounds.
Only when the provider omits the period (or returns 404)
does the adapter fall back to the old two-day heuristic, adding `crux_period_missing`. A URL that
CrUX silently normalizes produces a warning plus `crux_url_normalized`, so a caller never assumes
the requested spelling was queried verbatim.

`lv24-seo cwv history` uses `records:queryHistoryRecord`: one row per weekly collection period
(newest last), `first_date`/`last_date`, an optional canonical device and the p75 values
`lcp`/`inp`/`cls`/`fcp`/`ttfb`/`fid`. `--weeks` (1..40) is sent as `collectionPeriodCount` and
enforced locally. The API returns `null` p75 entries for periods without eligible data; those
become `crux_history_gaps` and are never filled. Fewer periods than requested adds
`crux_history_short`; non-CWV metrics are filtered with `crux_history_metrics_filtered`. History is
read-only and not persisted.

PSI is **lab** data: one synthetic Lighthouse load, never averaged across runs and never merged with
field p75. `cwv collect --source psi` returns lab rows for diagnosis but does not persist them; a
stored Lighthouse score would be read back as field data by the CWV builder. PSI has only `mobile`
and `desktop` strategies: `tablet` is skipped and reported as `psi_tablet_unsupported`, never mapped
to another device. PSI works keyless; CrUX requires a Google API key.

`lv24-seo cwv audits` exposes the Lighthouse diagnostics that the category score alone hides: up to
100 audit rows with `id`, `title`, `score`, `display_value`, `description` (plain text, HTML
stripped), `details_type`, `overall_savings_ms`, `overall_savings_bytes` and `items_count`. Item
arrays are never returned; `meta.total`/`meta.truncated`/`psi_audits_cap` state the bound. The
adapter also surfaces `runtimeError` (`psi_runtime_error`), a CAPTCHA (`psi_captcha`) and a
`finalUrl` that differs from the requested URL (`psi_final_url_differs`), plus
`analysisUTCTimestamp` and `finalUrl` in meta. An HTTP 200 Lighthouse run with a runtime error is
therefore not reported as clean. Nothing is persisted.

**Rules:** use CrUX for field data, PSI for lab diagnosis only, cache 24 h, never average lab runs.

## 6. GA4 (optional)

`POST https://analyticsdata.googleapis.com/v1beta/properties/{id}:runReport` — organic landing pages,
sessions and key events. Join to GSC on the URL path, not on the query. Thresholding can hide rows
(`(not set)`), so GA4 numbers are labelled as directional.

## 7. Own crawl and logs

* Crawl: serial `httpx` fetcher over the stdlib `html.parser` (no `lxml`), respecting
  `robots.txt` (RFC 9309 longest-match, `Allow` wins ties, 5xx/network defers instead of failing
  open), `Crawl-delay` pacing between requests, an outbound SSRF policy (loopback/private/link-local
  refused unless `allow_private` is set for local audits, DNS re-resolved per redirect hop) and a
  request budget (`1 + max_urls × (max_redirects + 1)`, reported as `request_count/request_budget`).
  `concurrency` is reserved and has no effect today. `/a` and `/a/` stay distinct until a fetch
  proves otherwise; unvisited link targets are reported as `unvisited`, never as broken. Retained
  page text is capped at 20,000 characters; `word_count` is computed from the full visible text.
  Ordinary pages use a 5 MB body budget, while sitemap documents get 50 MB per document and the
  50,000-URL protocol cap.
* robots.txt patterns are matched linearly (glob segments, not a `.*` regex), so a hostile file
  cannot trigger exponential backtracking. Patterns are capped at 1,024 characters and paths at
  4,096. A `429` is not treated as a page answer: the fetcher retries **once**, honouring
  `Retry-After` (seconds or HTTP-date, clamped to 30 s, 1 s default). A `429` on `robots.txt`
  itself defers the crawl instead of failing open.
* Redirect chains are flagged only above **two hops** (`len(chain) - 1 > 2`); a single redirect is
  normal housekeeping and is not reported. The issue detail carries `hops`.
* Soft-404 needs two independent signals: a not-found *phrase* in the title (for example
  "page not found", "страница не найдена") **and** an empty body (under 30 words) or an explicit
  `404`/`410` marker. A guide titled "How to fix 404 errors" is not a soft 404. A near-empty page
  without any phrase is reported separately as `thin`.
* Sitemaps are read same-origin: every `<loc>` and child sitemap must share the site origin
  (scheme + host + port). Anything else is dropped and reported once as `off_origin_loc`, which
  the service turns into a warning; it is never fetched. (The reader can widen scope only through
  an explicit `allow_url` predicate, which the service sets to the site origin.)
* Crawl scope: the seed and every redirect hop are checked against the start origin
  (scheme + host + port) and `robots.txt` **before** the request is sent; an off-origin or
  disallowed hop is never requested and is reported as `robots_blocked` with a coverage note.
  Sitemap discovery applies the same origin check to `Sitemap:` lines, index children and
  sitemap redirects. The single exception is `robots.txt` itself, whose redirects are followed
  as RFC 9309 §2.3.1.2 requires (still under the SSRF policy and the redirect cap).
* Logs: `logs analyze` / `seo_log_analysis` reads **one** local nginx JSON access log
  (`log_format ... escape=json`) at an absolute path, at most **100 MiB**. Relative paths, globs,
  UNC/network paths, Windows device names, directories and larger files are rejected with
  `INVALID_INPUT` before any bytes are read. The file is opened read-only once; nothing is uploaded,
  stored or appended to the SQLite history. Parsing is tolerant: blank or malformed lines are
  skipped and counted, and a missing timestamp lands in the `"unknown"` date bucket.
  Bot classes (`googlebot`, `yandexbot`, `bingbot`, `other`) come from the **user-agent string
  only**. This is fast triage, not crawler verification: a spoofed UA is not a verified crawler,
  there is no reverse-DNS or published-IP check, and non-search crawlers (AhrefsBot, SemrushBot)
  are reported as `other`. `crawl_waste` counts redirects, errors, assets, `/api/` calls and
  tracking-parameter requests once per request, so the share stays inside `0..1`; `p95_ms` is the
  nearest-rank 95th percentile of `request_time`. Log-based findings are not written to the store.

## 8. Optional paid adapters (BYO key, off by default)

| Provider | Free tier | Entry price | What it adds | Status |
|---|---|---|---|---|
| DataForSEO | ~$1 trial | pay per task | SERP + backlinks + on-page | planned, not shipped |
| SerpApi | 250 searches/mo | $25/mo | SERP snapshots, rank tracking | planned, not shipped |
| Ahrefs | none | $129/mo | backlinks, keywords | documented only |
| Semrush | none | API units | backlinks, keywords | documented only |
| SE Ranking / Serpstat / Moz / Majestic | trial / small | $20–400/mo | rank tracking, backlinks | documented only |
| Screaming Frog / Sitebulb | 500 URLs / trial | £199/yr / $13+/mo | deep crawls | documented only (licence is per seat) |

The default path must never require any of these. Integrations are thin adapters with the same
`ProviderResult` envelope and an explicit `paid: true` flag in reports.

## 9. What this project will never do

* Scrape Google/Yandex SERPs or bypass CAPTCHA.
* Automate the Yandex Business (Sprav) card or review replies through a browser session.
* Resell or republish provider data; every report is generated locally for the property owner.
* Present truncated data as complete.
