# Data sources, quotas and honesty rules

Every number this tool prints comes from one of the sources below. Each has hard limits; the tool
must surface them instead of pretending the data is complete.

Legend: **R** = read, **W** = write (gated), *free* = no payment required.

## 1. Google Search Console (GSC)

| | |
|---|---|
| Auth | OAuth 2.0 user token (`webmasters.readonly` for reports, `webmasters` for sitemaps) or a service-account JSON key added to the property as Full/Owner |
| Base | `https://www.googleapis.com/webmasters/v3/` |
| Free quota | 1,200 QPM per site and per user; 40k QPM / 30M QPD per project; **50,000 rows/day/site/type** for search analytics; URL Inspection 2,000/day/site, 600/min |
| Methods | `POST /sites/{siteUrl}/searchAnalytics/query` (**R**), `GET /sites` (**R**), `PUT/DELETE /sites/{siteUrl}/sitemaps/{feedpath}` (**W**), `GET /sites/{siteUrl}/sitemaps` (**R**) |
| URL Inspection | `POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect` (**R**, one URL per call, no batch, no live test) |
| Dimensions | `query, page, country, device, date, hour, searchAppearance` |
| Types | `web, image, video, news, discover, googleNews` |
| Latency | 2–3 days; `dataState=all` returns fresher but incomplete data (`metadata.first_incomplete_date`) |

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
rich-result validation (no public API), query data beyond 16 months, real-time data.

## 2. Yandex Webmaster

| | |
|---|---|
| Auth | `Authorization: OAuth <token>`, scope `webmaster:hostinfo` (+ `webmaster:verify` to add hosts) |
| Base | `https://api.webmaster.yandex.net/v4/` |
| Free quota | free; no published global RPD/RPS; `query-analytics/list` is fragile (>10k req/h → 429) |
| Identity | `GET /user/` → `user_id`; hosts via `GET /user/{uid}/hosts/`; `host_id` format `https:example.com:443` (never guess it) |
| Read | `summary`, `sqi-history`, `indexing/history`, `indexing/samples`, `search-urls/in-search/history`, `search-urls/events/history`, `search-queries/popular`, `search-queries/all/history`, `diagnostics`, `links/internal/broken`, `sitemaps` |
| Write | `POST /user/{uid}/hosts/{host}/recrawl/queue` (**W**, hard daily quota), `POST /sitemaps/{id}/recrawl` (**W**), `POST /user-added-sitemaps/` (**W**) |
| Query data | top **3,000** queries per period, ≤500 per page; **no query × page join** |
| Latency | ~4 days; the service can return `HOST_NOT_LOADED` during maintenance |

**Hard rules**

1. Resolve `user_id` and `host_id` on every cold start; cache only successful lookups.
2. Repeated query params, not comma lists (`?query_indicator=TOTAL_SHOWS&query_indicator=TOTAL_CLICKS`).
3. `401` = token dead, `403` = host not verified for this account. Never re-auth on 403.
4. Recrawl: read `GET /recrawl/quota` first, queue only up to the remaining quota, store task ids.
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

**Not available via API:** per-URL sitemap error detail, diagnostic recommendation text, Webmaster
notification settings, Yandex Business card/reviews.

## 3. Yandex Metrika

| | |
|---|---|
| Auth | `Authorization: OAuth <token>`, scope `metrika:read` |
| Base | `https://api-metrika.yandex.net/stat/v1/` |
| Free quota | 5,000 requests/day/user; 30 rps/IP; 3 parallel; `/stat/v1/data` 200 req / 5 min; ≤100k rows, ≤20 metrics, ≤10 dimensions per request |
| Useful dimensions | `ym:s:lastTrafficSource`, `ym:s:lastSearchEngineRoot`, `ym:s:lastSearchPhrase`, `ym:s:startURLPath`, `ym:s:deviceCategory` |
| Useful metrics | `ym:s:visits`, `ym:s:users`, `ym:s:bounceRate`, `ym:s:avgVisitDurationSeconds`, `ym:s:goal<id>reaches`, `ym:s:goal<id>conversionRate` |
| Sampling | `accuracy=medium` by default; `full` is slower and eats the 5-min quota |

**Hard rules**

1. Two dimensions max for daily reports (`date` + one), otherwise rows explode.
2. Check `sampled` / `sample_share` before publishing a number; store the accuracy flag with the value.
3. Organic only: `filters=ym:s:lastTrafficSource=='organic'`.
4. Goal ids come from `GET /management/v1/counters/{id}/goals` once and are stored in config.
5. The client uses documented `GET /stat/v1/data` with query parameters. Offline contract
   tests check the method and parameters; live property acceptance still needs owner credentials.
6. `limit` is the page size, not the result size. The table is walked with `offset` until
   `total_rows` is reached, a short page arrives, or a cap stops the walk: at most **10,000 rows**
   and **100 pages** per call. A full page with no `total_rows` is reported as `truncated: true`
   instead of looking complete; a request without dimensions is treated as a single row.

## 4. Bing Webmaster + IndexNow

| | |
|---|---|
| Bing API | `https://ssl.bing.com/webmaster/api.svc/json/` — `GetQueryStats`, `GetRankAndTrafficStats`, `GetCrawlIssues`, `GetUrlSubmissionQuota`, `GetFeeds`, `GetFeedDetails`, `SubmitUrlbatch`, `SubmitFeed` |
| Auth | one API key per user (Bing Webmaster → Settings → API access) |
| Free quota | free; remaining daily/monthly submission quota is reported by `GetUrlSubmissionQuota`, 500 URLs per `SubmitUrlbatch` |
| Freshness | `data_age_days` is the age of the newest dated row in that feed: `Date` for `GetQueryStats`, `LastCrawled` (falling back to `Submitted`) for `GetFeeds`. `GetCrawlIssues` has no date and reports `0`, which is not proof of freshness. |
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
