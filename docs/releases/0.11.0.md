# 0.11.0 - Full-platform read depth

Status: **released** (2026-11-17) on PyPI, npm and the MCP Registry. Desktop `0.9.0` bundles engine
`0.11.0` and surfaces these reads in the UI.

Minor release: every supported platform gains the read APIs that were previously adapter-only,
partial or missing. The CLI now has **24 top-level commands** (17 groups + 7 commands), the MCP
surface stays at **12 tools**, and the SQLite schema stays at version 4. No paid provider or new
required dependency is added.

## Added

### Google Search Console

* `gsc inspect --url ABS [--language en-US] [--config ABS] [--json]` wires URL Inspection
  end-to-end. The result maps the full `indexStatusResult` (`verdict`, `coverageState`,
  `robotsTxtState`, `indexingState`, `lastCrawlTime`, `pageFetchState`, `googleCanonical`,
  `userCanonical`, `crawledAs`, `sitemap[]`, `referringUrls[]`), `richResultsResult`
  (`verdict`, `detectedItems[].items[].issues[]`) and `inspectionResultLink`.
  Limitations: `one_url_per_call`, `url_inspection_quota_2000_per_day`,
  `inspection_reflects_last_crawl` (it is not a live test).
* `gsc sitemaps [--sitemap-index URL]` and `gsc sitemap --path URL` map `path`, `lastSubmitted`,
  `lastDownloaded`, `isPending`, `isSitemapsIndex`, `type`, `errors`, `warnings` and
  `contents[{type,submitted,indexed}]`. GSC's `indexed` count is marked deprecated, and child
  sitemap URLs of a sitemap index are reported as not exposed rather than guessed.
* `gsc query` gains `--type`, repeatable `--filter dimension:operator:expression`,
  repeatable `--filter-list dimension:expr1,expr2`, `--data-state`, `--aggregation`,
  `--search-appearance` and `--branded`. Responses add `responseAggregationType` and the
  incomplete-data `metadata` (`firstIncompleteDate`/`firstIncompleteHour`) when Google returns it.
  Existing defaults and the 25k rows/page, 50k rows/call caps are unchanged.
* MCP stays at 12 tools: `seo_indexing_triage` accepts an optional `url` and includes the GSC
  inspection result when provided. Sitemap submit/delete remain `WRITE_DISABLED` because the
  engine signs `webmasters.readonly`; a full `webmasters` scope is documented as the requirement.

### Yandex Webmaster

* **Fixed** `search-urls/events/history`: the client read an `events` list, but the API returns
  `indicators{APPEARED_IN_SEARCH,REMOVED_FROM_SEARCH}[{date,value}]`, so every call reported
  `empty_history` and dropped real data. Live check (2026-10-02): **57 appeared / 0 removed**.
* `yandex indexing` accepts `--date-from/--date-to` and merges the appeared/removed event series
  with the indexing HTTP series per date. When the events endpoint resets the connection on a date
  window, the client retries unfiltered, filters locally and warns `client_side_date_filter`.
* `yandex samples --kind indexing|events [--limit] [--offset]` pages the provider sample endpoints
  with honest `total`/`truncated` (provider limit 100 per page).
* `yandex sitemaps [--id ID] [--parent-id ID] [--limit N] [--from ID]` reads sitemap lists and
  details: `sitemap_id`, `sitemap_url`, `last_access_date`, `errors_count`, `urls_count`,
  `children_count`, `sources[]`, `sitemap_type`. Sitemap add/delete is not implemented.
* `yandex history --kind sqi|in-search|queries|query [--query-id ID]` reads SQI, pages-in-search,
  all-query and per-query series.
* `yandex queue [--id TASK]` reads recrawl tasks with `count`; `yandex recrawl` is now wired:
  plan with `--allow-write`, then apply **one POST per URL**, capped to `quota_remainder`, handling
  `202` (`task_id`, `quota_remainder`), `409 URL_ALREADY_ADDED` and `429 QUOTA_EXCEEDED` with a
  partial `not_attempted` list.

### Bing Webmaster

* `bing pages` (`GetPageStats`), `bing traffic` (`GetRankAndTrafficStats`, daily), `bing crawl`
  (`GetCrawlStats`, six-month daily buckets), `bing urlinfo --url URL|domain:HOST` (`GetUrlInfo`),
  `bing children --url ... [--page] [--pages]` (`GetChildrenUrlInfo`, read POST, bounded to 10
  pages), `bing users` (`GetSiteRoles`, read-only), `bing sites` (`GetUserSites`) and
  `bing joins --query|--page` (`GetQueryPageStats`, `GetPageQueryStats`,
  `GetQueryPageDetailStats`).
* The `Date(-62135596800000)` sentinel is mapped to `null` and flagged `never_crawled`; the
  provider's `TotalChildUrlCount` is kept as a separate field.
* Empty answers are honest: stats/joins add `no_rows_for_window` and `crawl` always adds
  `crawl_stats_six_month_window`; lookups/lists add `provider_returned_no_rows`. A zero row count
  is no longer presented as "zero impressions".

### Yandex Metrika

* New read-only `metrika` group: `pages`, `goals`, `queries`, `devices`, `countries`,
  `engagement` and `bytime`.
* The SEO collector now applies `filters=ym:s:lastTrafficSource=='organic'`. `stats` and
  `report weekly` expose `organic_visits`/`organic_users` and keep all traffic explicit as
  `all_visits`/`all_users`. Live (2026-11-09..15): **4 organic visits/4 users vs 40 all visits/12
  users**, so the previous card overstated SEO traffic tenfold.
* `metrika pages` returns organic landing pages (`ym:s:startURLPath`) with visits/users,
  `bounceRate`, `avgVisitDurationSeconds`, `pageDepth` and `pageviews`, sorted by `-ym:s:visits`.
* `metrika goals` queries the configured goal ids with `ym:s:goal<id>reaches|visits|users|conversionRate`,
  optionally by `page` or `source`; a missing `goals` list is an honest warning, and more than five
  goals are truncated at Metrika's 20-metric cap with `truncated: true`.
* Sampling honesty: `sample_share`, `sample_size`, `sample_space`, `data_lag`,
  `contains_sensitive_data` and `total_rows_rounded` are carried into `meta`/limitations.
  Only an explicit `sampled=false` is treated as exact.

### Core Web Vitals and IndexNow

* `cwv audits --url ABS [--strategy mobile|desktop] [--category ...]` returns a bounded list of
  Lighthouse audits (`id`, `title`, `score`, `display_value`, `description` with HTML stripped,
  `details_type`, `overall_savings_ms`, `overall_savings_bytes`, `items_count`, capped at 100).
  `runtimeError`, CAPTCHA and redirects become warnings/limitations instead of a clean run.
  `cwv collect` gains repeatable `--category` (performance, seo, accessibility, best-practices).
* `cwv history (--url ABS | --origin ABS) [--device phone|desktop|tablet] [--weeks 1..40]` reads
  CrUX `queryHistoryRecord` weekly p75 rows with honest gaps and truncation.
* CrUX `collectionPeriod.firstDate/lastDate` now drives `data_age_days` (the hardcoded 2-day
  heuristic is only a fallback with `crux_period_missing`); `urlNormalizationDetails` produces
  `crux_url_normalized`.
* IndexNow validates the key (`[A-Za-z0-9-]{8,128}`) before planning, enforces a same-host
  `keyLocation` directory prefix, and maps `200 accepted`, `202 accepted_pending`, `400` format,
  `403 AUTH_INVALID` with the exact expected key URL, `422` host/key mismatch and `429`
  quota/spam. The key is never echoed.

## Changed

* The CLI now has **24 top-level commands**: 17 groups (`auth`, `config`, `gsc`, `yandex`,
  `metrika`, `sitemap`, `indexnow`, `bing`, `report`, `cwv`, `schedule`, `export`, `logs`, `tasks`,
  `backup`, `portfolio`, `mcp`) and 7 commands (`version`, `doctor`, `init`, `properties`, `stats`,
  `crawl`, `install`).
* `ProviderResult` gains an `extra` metadata mapping (GSC aggregation type, CrUX collection period)
  and shared sampling fields; `to_meta()` now always emits `sampled`.
* Metrika `stats`/weekly sections render organic and all-traffic rows separately.
* Documentation drift is fixed: `GetRankAndTrafficStats` is now wired, GSC rich-result and sitemap
  fields are documented, and the IndexNow key/location rules match the implementation.

## Fixed

* Yandex events history data loss (`empty_history` despite real appeared/removed series).
* Metrika `data_age_days` was inflated by the previous comparison window (`max(current, previous)`);
  it now reflects the current window (live: 1 day).
* GSC sitemap mapping dropped `type`, `isSitemapsIndex` and `contents[]`.
* PSI Lighthouse audits and run metadata were discarded.
* CrUX reported a hardcoded 2-day age and ignored `collectionPeriod`.

## Not changed

* MCP stays at **12 tools**; richer GSC query selectors are CLI-only and documented as such.
* SQLite schema version 4.
* GSC sitemap submit/delete stay `WRITE_DISABLED`; Bing sitemap/URL submit, IndexNow and Yandex
  recrawl remain gated writes with `--allow-write` + confirm token.
* GA4 remains an optional, unimplemented provider (no code, config or tests).

## Verification notes

Live read-only checks against the real `lv24.by` workspace (`sc-domain:lv24.by`,
`https:lv24.by:443`, Metrika counter `111384866`):

| Check | Result |
|---|---|
| `gsc inspect` `/` | `PASS`, "Submitted and indexed", canonical match, `crawledAs: MOBILE`, 2 referring URLs |
| `gsc inspect` `/places` | `NEUTRAL`, "URL is unknown to Google" |
| `gsc sitemaps` | 3 sitemaps; `sitemap.xml` 3179 submitted, `sitemap-auto.xml` 1551, `sitemap-seo.xml` is an index |
| `gsc query --filter country:equals:BLR --data-state all` | filtered rows returned; `metadata.firstIncompleteDate` present |
| `yandex indexing --date-from 2026-10-01 --date-to 2026-10-03` | 427 event rows; 2026-10-02 = 57 appeared / 0 removed |
| `yandex sitemaps` | 1 sitemap, 6532 URLs, 0 errors |
| `yandex history --kind sqi` | 898 points |
| `yandex queue` | 28 tasks, all `DONE` |
| `yandex samples --kind events` | total 5642 |
| `bing sites` / `bing users` | 1 verified site; 1 `Administrator` |
| `bing urlinfo domain:lv24.by` | 8380 children, sentinel dates `null` |
| `bing pages` / `traffic` / `crawl` / `joins` | 0 rows with `no_rows_for_window` (not zero impressions) |
| `bing children domain:lv24.by` | 0 rows with `provider_returned_no_rows` despite `TotalChildUrlCount=8380` |
| `metrika pages` | 4 organic landing pages, 4 visits / 4 users |
| `metrika goals` | 5 configured goals, 0 reaches in the window (honest empty result) |
| `metrika devices` / `countries` | 3 Smartphones + 1 PC; Belarus 4 |
| `metrika engagement` / `bytime` | 25% bounce, 13.75 s, 4 pageviews; 7 daily intervals |
| `cwv audits` | 58 audits, 2 categories |
| `cwv history` | 0 rows with `low_traffic` + `crux_period_missing` (no CrUX field data for lv24.by) |

Local quality gate on the tagged commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

## Limitations

* Bing stats and joins return 0 rows for `lv24.by`, so their field semantics are covered by
  fixtures, not by a live non-empty response. `GetSiteMoves` is retired (live 404) and is not wired.
* Bing `GetChildrenUrlInfo` returns 0 rows for this host even though `GetUrlInfo` reports 8380
  children; the engine reports the provider answer instead of synthesising rows.
* Yandex `indexing/samples` resets the connection provider-side for this host; it surfaces as
  `UPSTREAM_UNAVAILABLE`, not as empty data.
* Yandex sitemap add/delete is not implemented; recrawl `202/409/429` handling is covered offline
  only (no live POST was made).
* Metrika hides search phrases with fewer than 10 users and Google withholds keywords; goal names
  are not resolved (ids only).
* CrUX has no field data for `lv24.by`, and the history API is bounded to 40 weekly periods.
* IndexNow has no key file configured for `lv24.by`; native Bing URL submission remains the working
  write path. Google does not participate in IndexNow.
* Bing Site Scan has no public API: the desktop opens the official page and the engine cannot
  import its report.
