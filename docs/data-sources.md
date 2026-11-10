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
2. Paginate with `startRow` until a short page; if `rows == 50,000` mark `truncated: true`.
3. Cache daily pulls in SQLite; never re-query a month repeatedly (the "load quota" is opaque).
4. A service account cannot complete interactive verification for `sc-domain:` — a human publishes the
   DNS TXT once (or `sites.add` is called by an already-verified principal).

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
| Latency | ~4 days; the service can return `HOST_NOT_LOADED` / empty history during maintenance |

**Hard rules**

1. Resolve `user_id` and `host_id` on every cold start; cache only successful lookups.
2. Repeated query params, not comma lists (`?query_indicator=TOTAL_SHOWS&query_indicator=TOTAL_CLICKS`).
3. `401` = token dead, `403` = host not verified for this account. Never re-auth on 403.
4. Recrawl: read `GET /recrawl/quota` first, queue only up to the remaining quota, store task ids.
5. Percent-encode Cyrillic URLs; send a descriptive `User-Agent` and `Accept: application/json`.

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

## 4. Bing Webmaster + IndexNow

| | |
|---|---|
| Bing API | `https://ssl.bing.com/webmaster/api.svc/json/` — `GetQueryStats`, `GetRankAndTrafficStats`, `GetCrawlIssues`, `SubmitUrlbatch`, `SubmitFeed` |
| Auth | one API key per user (Bing Webmaster → Settings → API access) |
| Free quota | free; ~10k URL submits/day/domain, 500 per batch |
| IndexNow | `https://api.indexnow.org/indexnow` (also `yandex.com/indexnow`), key file `https://host/<key>.txt` (8–128 hex) |
| Limits | 10k URLs per POST; engines apply their own daily caps; **Google does not participate** |

## 5. Chrome UX Report (CrUX) and PageSpeed Insights (PSI)

| | |
|---|---|
| CrUX | `POST https://chromeuxreport.googleapis.com/v1/records:queryRecord?key=…` — 150 QPM/project, cannot be raised; 28-day rolling p75; 404 when a URL has too little traffic |
| PSI | `GET https://www.googleapis.com/pagespeedonline/v5/runPagespeed` — ~25k/day; Lighthouse lab + (deprecated) field block |
| Metrics | LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 (p75) |

**Rules:** use CrUX for field data, PSI for lab diagnosis only, cache 24 h, never average lab runs.

## 6. GA4 (optional)

`POST https://analyticsdata.googleapis.com/v1beta/properties/{id}:runReport` — organic landing pages,
sessions and key events. Join to GSC on the URL path, not on the query. Thresholding can hide rows
(`(not set)`), so GA4 numbers are labelled as directional.

## 7. Own crawl and logs

* Crawl: `httpx` + `lxml`, respecting `robots.txt`, `Crawl-delay` and a global concurrency cap (default 4).
* Logs: nginx JSON access log → bot verification by **reverse DNS / published IP ranges**, never by UA
  string alone.

## 8. Optional paid adapters (BYO key, off by default)

| Provider | Free tier | Entry price | What it adds | Status |
|---|---|---|---|---|
| DataForSEO | ~$1 trial | pay per task | SERP + backlinks + on-page | adapter in W8 |
| SerpApi | 250 searches/mo | $25/mo | SERP snapshots, rank tracking | adapter in W8 |
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
