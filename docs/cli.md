# CLI reference

The CLI has 25 top-level commands: 18 groups (`auth`, `config`, `gsc`, `yandex`, `metrika`,
`sitemap`, `indexnow`, `bing`, `indexing`, `report`, `cwv`, `schedule`, `export`, `logs`, `tasks`,
`backup`, `portfolio`, `mcp`) and 7 commands (`version`, `doctor`, `init`, `properties`, `stats`,
`crawl`, `install`).

Every read/report command supports `--json` (machine output) and a stable exit code; commands that
use the workspace also accept `--config PATH`.
Output envelope (`meta.version` is the package version, e.g. `0.12.2`):

```json
{ "ok": true, "data": {}, "meta": { "version": "0.12.2", "duration_ms": 812 }, "warnings": [] }
```

Exit codes: `0` ok · `2` usage/config · `3` auth · `4` quota/upstream · `5` strict truncation · `1` unexpected.

`--json` makes the command print exactly one envelope. Without it you get a short human summary on
stdout; no ANSI colours.

`--strict` is available on the read commands (`stats`, `gsc query`, `yandex queries`,
`yandex indexing`, `yandex samples`, `yandex sitemaps`, `yandex history`, `yandex queue`,
`metrika pages|goals|queries|devices|countries|engagement|bytime`,
`bing pages`, `bing traffic`, `bing crawl`, `bing urlinfo`, `bing children`, `bing users`,
`bing sites`, `bing joins`, `bing quota`, `bing sitemaps`, `bing issues`, `crawl`,
`report weekly`, `report diff`). It always sets `meta.strict: true`;
when the payload is also truncated, the command exits `5` and `meta.limitations` explains what is
missing (a generic `truncated: …` note is added when the provider did not name a limitation).
Complete data still exits `0`, and without `--strict` truncation stays `meta.truncated` with exit
`0`. Use it as a CI gate when a capped answer must fail the build.

## Setup

```bash
lv24-seo version [--json]
lv24-seo doctor [--json]                      # offline: python, config, data dir, token presence
lv24-seo init [--property https://example.com] [--data-dir ./data] [--force]
lv24-seo config show [--json]                 # redacted effective config
lv24-seo properties [--json]                  # provider -> target + token present|missing
```

`init` refuses to overwrite an existing `seo.toml` unless `--force` is passed.

`doctor --json` reports `meta.data_dir`, `meta.data_dir_source` (`config` when the effective
directory is absolute, `working directory` when it is relative) and `meta.data_dir_declared`.
A relative directory does not fail the check; see
[configuration](config.md#data-directory). When a data directory **is declared** (a config file
resolves or `LV24_SEO_DATA_DIR` is set), the `data_dir` check becomes fatal: a missing or unwritable
directory returns `ok: false` with `meta.healthy: false` and exit `1`, instead of a green
`healthy: true`. `doctor` makes no network calls.

## Authentication

```bash
lv24-seo auth status [--json]                                  # presence + source, never values
lv24-seo auth set --provider gsc|yandex|metrika|bing|crux|psi|smtp|telegram [--stdin]
lv24-seo auth login --provider gsc|yandex                      # prints the provider-specific steps
lv24-seo auth logout --provider <name>
```

`auth set` reads the secret from stdin or a hidden prompt, stores it in the OS keychain (or the
`0600` file fallback), and prints only `{provider, stored, source}`.

### Google Search Console credentials

`auth set --provider gsc` stores a **plain OAuth access token** (`Authorization: Bearer`), which
normally expires after about an hour. For unattended runs use a service-account JSON key instead:

```bash
export LV24_SEO_GSC_SERVICE_ACCOUNT_FILE=/etc/lv24-seo/gsc-service-account.json   # absolute
lv24-seo properties --json                                                         # gsc ready
```

```powershell
$env:LV24_SEO_GSC_SERVICE_ACCOUNT_FILE = "C:\lv24-seo\gsc-service-account.json"
lv24-seo properties --json
```

The same path can live in `seo.toml` as `providers.gsc.service_account_file`. The service-account
file is tried first (`LV24_SEO_GSC_SERVICE_ACCOUNT_FILE` overrides the config value when both are
set), then the `gsc_token` secret. A `gsc_token` value that is a service-account JSON object is also
accepted; a plain token keeps the old bearer behaviour.
The engine reads the key once per process, signs an RS256 JWT, exchanges it at the Google token
endpoint, caches the access token and refreshes it 60 seconds before expiry. Relative, missing or
unreadable files return `CONFIG_INVALID` with a next action.

`auth status` reports only stored secrets (`env`, `keyring`, `file`); it does not inspect the
service-account file. `properties` and `doctor` (the `gsc_auth` check) are the readiness surfaces,
and neither prints the key.

## Data

```bash
lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 [--dimensions query,page] [--limit 100] [--strict]
lv24-seo gsc query ... [--type web|image|video|news|discover|googleNews|webMultimodal]
lv24-seo gsc query ... [--filter dimension:operator:expression]... [--filter-list country:FRA,DEU]
lv24-seo gsc query ... [--data-state all|final|hourly_all] [--aggregation auto|byPage|byProperty|byNewsShowcasePanel]
lv24-seo gsc query ... [--search-appearance AMP_BLUE_LINK] [--branded branded|non_branded]
lv24-seo gsc inspect --url https://example.com/page [--language en-US] [--json]
lv24-seo gsc sitemaps [--sitemap-index https://example.com/sitemap-seo.xml] [--json]
lv24-seo gsc sitemap --path https://example.com/sitemap.xml [--json]
lv24-seo yandex queries [--limit 100] [--strict] [--json]
lv24-seo yandex indexing [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD] [--strict] [--json]
lv24-seo yandex samples --kind indexing|events [--limit 100] [--offset 0] [--strict] [--json]
lv24-seo yandex sitemaps [--id SITEMAP_ID] [--parent-id ID] [--limit 100] [--from ID] [--strict] [--json]
lv24-seo yandex history --kind sqi|in-search|queries|query [--query-id ID] [--date-from D] [--date-to D] [--strict] [--json]
lv24-seo yandex queue [--id TASK_ID] [--limit 100] [--offset 0] [--date-from D] [--date-to D] [--strict] [--json]
lv24-seo crawl [--start URL] [--max-urls 500] [--strict] [--json]
```

GSC needs explicit `--start/--end` (there is no `--days` shorthand and no `--property`
override — the property comes from `seo.toml`). A GSC page that exactly fills the row cap is reported
as `truncated: true` with `rows_cap_50000` (default cap) or a caller-cap warning: a full page is not
proof that no row exists behind the cap, so an exact-cap result is never claimed complete. A
truncated GSC result also omits `total` (`null`) — an exact count next to `truncated: true` would
read as a complete answer. `data_age_days` is floored at the documented 3-day lag. The hard API
limits stay: **25,000 rows per page** and **50,000 rows/day/site/type**; the engine never asks for
more and reports the cap.

`gsc query` forwards every Search Analytics selector without changing the row shape. `--filter`
takes `dimension:operator:expression` and may be repeated (one AND group; operators `contains`,
`equals`, `notContains`, `notEquals`, `includingRegex`, `excludingRegex`). `--filter-list` takes
`dimension:expr1,expr2` (multi-value, `country`/`device` only) and may be repeated. `--type` accepts
`web`, `image`, `video`, `news`, `discover`, `googleNews` and `webMultimodal`; `--data-state` accepts
`all`, `final`, `hourly_all`; `--aggregation` accepts `auto`, `byPage`, `byProperty` and
`byNewsShowcasePanel`. `--search-appearance VALUE` adds a `searchAppearance equals VALUE` filter and
`--branded branded|non_branded` adds a `brandedClassification` filter (only `equals`; rejected
together with a `query` filter, as the API requires). The response adds
`meta.responseAggregationType` and `meta.metadata` (`first_incomplete_date`,
`first_incomplete_hour`) when Google returns them, plus the echoed `meta.type`,
`meta.dataState`, `meta.aggregationType`, `meta.filters` and `meta.filterLists`.

`gsc inspect` calls URL Inspection for exactly one absolute URL. It returns the full
`indexStatusResult` mapping (`verdict`, `coverage_state`, `robots_txt_state`, `indexing_state`,
`last_crawl_time`, `page_fetch_state`, `google_canonical`, `user_canonical`, `canonical`,
`crawled_as`, `sitemap[]`, `referring_urls[]`), `rich_results`
(`verdict`, `detected_items[].rich_result_type`, per-item `issues[]`) and
`inspection_result_link`. `--language` (BCP-47, default `en-US`) localizes `coverage_state` and
other human-readable states. It is a read-only report about the last crawl, not a live test; the
result carries `one_url_per_call`, `url_inspection_quota_2000_per_day` and
`inspection_reflects_last_crawl`.

`gsc sitemaps` lists what Google reports for the property: `path`, `last_submitted`,
`last_downloaded`, `is_pending`, `is_sitemaps_index`, `type`, `errors`, `warnings` and
`contents[{type,submitted,indexed}]`. `--sitemap-index URL` also reads that index entry and returns
it as `data.sitemap_index` (separate from `rows`, so the list total stays exact). `gsc sitemap --path URL`
reads one entry. GSC's `indexed` count is deprecated, so each content item carries
`indexed_deprecated: true` and the meta carries `sitemap_indexed_count_deprecated`; an index adds
`sitemap_index_children_not_exposed` because child sitemap URLs are not available from the API.
Sitemap submit/delete stay `WRITE_DISABLED`: they need the full
`https://www.googleapis.com/auth/webmasters` scope, while the engine signs
`webmasters.readonly`.

`yandex queries` returns the documented query indicators as `shows`, `clicks`, `position` and
`avg_click_position` when Yandex returns them; an indicator the engine does not map is kept under
`extra_indicators` instead of dropping the row. `yandex indexing` returns the documented
`indexing/history` series merged per date (`http_2xx`, `http_3xx`, `http_4xx`, `http_5xx`, `other`),
the `search-urls/events/history` series merged per date (`appeared`, `removed`) and the `summary`
counters including `site_problems`; a counter or series the provider omits stays absent. Both
history calls forward `--date-from/--date-to` as the provider's `YYYY-MM-DD` window. Live
2026-11-17 showed `search-urls/events/history` closing the connection when the window is present;
the client retries once without it, filters the full series locally and reports
`client_side_date_filter` in `warnings` and `meta.limitations`.

`yandex samples --kind indexing` reads `indexing/samples` (`status`, `http_code`, `url`,
`access_date`); `--kind events` reads `search-urls/events/samples` (`url`, `title`, `event_date`,
`last_access`, `event`, `excluded_url_status`, `bad_http_status`, `target_url`). The provider pages
at 100 rows; the client walks pages up to `--limit` (max 1000) and reports the provider's `count`
as `meta.total`, with `meta.truncated: true` whenever rows remain. `--offset` starts at a provider
row. A provider that disconnects the samples endpoint is an upstream error, never an empty result.

`yandex sitemaps` lists `sitemap_id`, `sitemap_url`, `last_access_date`, `errors_count`,
`urls_count`, `children_count`, `sources` and `sitemap_type`; `--id` reads one sitemap and
`--parent-id` lists the children of a sitemap index. `--from` is the provider paging cursor (a
sitemap id). Adding or deleting sitemaps is not implemented; the command is read-only.

`yandex history --kind sqi` returns `{date, value}` points from `sqi-history`; `in-search` returns
the provider's `{date, value}` pages-in-search history; `queries` returns daily `shows`, `clicks`,
`position` and `avg_click_position`; `query --query-id ID` returns the same series for one query.
The `queries` and `query` calls send the four documented `query_indicator` parameters (without
them Yandex returns an empty `indicators` object). `--date-from/--date-to` bound the window.

`yandex queue` reads `recrawl/queue` (`task_id`, `url`, `added_time`, `state`) with `meta.total`
from the provider's `count`; `--id` reads one task. `yandex recrawl --url ... --allow-write`
plans, and `--confirm TOKEN` applies one provider POST per URL (Yandex has no batch endpoint). The
remaining daily quota is read first and the list is capped to it; `202` rows carry `task_id` and
`quota_remainder`, `409` rows are `already_queued`, and a `429` leaves earlier task ids in place,
lists the rest under `not_attempted`, sets `meta.truncated: true` and returns
`limitations: ["one_url_per_call", "recrawl_quota_exceeded"]`.

`crawl` respects `robots.txt`, follows same-host links breadth-first, runs all rules and stores
pages/issues in SQLite when a store is configured. A sitemap that fails to load reports only the
exception type in warnings and `limitations[]` — never the URL or query string, which can carry a
token. Redirect chains are flagged only above two hops; a soft 404 needs a not-found phrase plus an
empty body or an explicit `404`/`410` marker; robots patterns are matched linearly; a `429` is
retried once with `Retry-After`; sitemap `<loc>` values outside the site are reported as
`off_origin_loc`. Details in [data sources](data-sources.md#7-own-crawl-and-logs).

`crawl` also returns `data.sitemap_coverage`: the sitemap (what the site advertises) compared
with what the crawl saw, in six buckets — `unlinked` (in the sitemap, OK, but no crawled page
links to it), `missing_from_sitemap` (indexable 200 HTML the sitemap omits), `not_ok`
(non-200 or redirected), `noindex`, `blocked` (robots/off-origin) and `unvisited` (never
fetched: status **unknown**, not broken). `complete` is `false` and `limitations[]` says which
buckets are only candidates when the crawl or sitemap inventory was truncated, a sitemap failed
to load, or URLs went unvisited. No sitemap means an empty diff that says "empty, not clean".
Lists are capped at 50 per bucket; `counts` are exact. Stored page text is capped at 20,000
characters (`word_count` stays exact), and sitemap documents get a 50 MB body budget with the
50,000-URL protocol cap.

`yandex indexing` also persists provider-reported per-URL events when a workspace exists (schema v4,
table `indexing_events`). A row is stored only when it carries an absolute URL, an event name and an
event date; the identity is `(provider, property_id, url, event, event_date)`, so replaying a sync
never duplicates an event. Daily aggregate history rows have no URL and are skipped instead of being
assigned a guessed one. When a store exists and events were considered, `meta.indexing_events`
reports `new`, `existing`, `skipped` and `truncated` counts (at most 500 provider rows are considered
per call), and a limitation is added when rows were skipped or the 500-row budget was hit. A storage
failure is a warning; the provider read still succeeds.

## Yandex Metrika

```bash
lv24-seo metrika pages      [--days 7] [--lag-days 2] [--limit 100] [--strict] [--json]
lv24-seo metrika goals      [--days 7] [--lag-days 2] [--dimension page|source] [--limit 100] [--json]
lv24-seo metrika queries    [--days 7] [--lag-days 2] [--limit 100] [--json]
lv24-seo metrika devices    [--days 7] [--lag-days 2] [--limit 100] [--json]
lv24-seo metrika countries  [--days 7] [--lag-days 2] [--limit 100] [--json]
lv24-seo metrika engagement [--days 7] [--lag-days 2] [--json]
lv24-seo metrika bytime     [--days 7] [--lag-days 2] [--group day|week|month] [--json]
```

All seven commands are read-only and use the configured counter
(`providers.yandex_metrika.counter`). The window is `--days` long and ends `--lag-days` before today
in `report.timezone`; the same rule as `stats`, so the defaults cover the last complete week after
the provider's 2-day lag.

Every command applies the organic filter `ym:s:lastTrafficSource=='organic'`; `meta.organic_only`
is `true`, and `meta.filters` repeats the expression. The commands use one shared
`stat/v1/data` helper, so `limit`, `accuracy`, `sort`, paging and the row cap behave identically.

| Command | Dimension | Metrics |
|---|---|---|
| `pages` | `ym:s:startURLPath` | `visits`, `users`, `bounceRate`, `avgVisitDurationSeconds`, `pageDepth`, `pageviews` |
| `queries` | `ym:s:lastSearchPhrase`, `ym:s:lastSearchEngineRoot` | `visits`, `users` |
| `devices` | `ym:s:deviceCategory` | `visits`, `users`, `bounceRate`, `avgVisitDurationSeconds`, `pageviews` |
| `countries` | `ym:s:regionCountry` | `visits`, `users`, `bounceRate`, `avgVisitDurationSeconds`, `pageviews` |
| `engagement` | none (one total row) | `bounceRate`, `avgVisitDurationSeconds`, `pageDepth`, `pageviews`, `visits`, `users` |
| `bytime` | none (`stat/v1/data/bytime`) | `visits`, `users` per interval (`date` is the interval start) |

`goals` reads the goal ids from `providers.yandex_metrika.goals` and requests
`ym:s:goal<id>reaches|visits|users|conversionRate` for each one, optionally grouped by `page`
(`ym:s:startURLPath`) or `source` (`ym:s:lastTrafficSource`). An empty `goals` list is not an
error: the envelope stays `ok: true`, makes no provider call, and carries an explicit warning and
limitation. Goal names are not resolved; ids are the stable identity. Metrika accepts at most 20
metrics per request, so at most five goal ids are queried per call; a longer list is truncated with
`truncated: true` and a limitation that names the configured count.

`queries` always adds two limitations: Google withholds search phrases, and Metrika hides phrases
with fewer than 10 users under its sensitive-data rule. The provider's own
`contains_sensitive_data` flag is carried into `meta` and `limitations` when present.

Sampling is never presented as exact. When Metrika reports `sampled: true`, `meta.sampled` is
`true`, `meta.sample_share`, `sample_size`, `sample_space`, `data_lag` and `total_rows_rounded` are
passed through, `limitations` contains `sampled`, and the human output says "values are estimates".
`data_age_days` describes the **current** window only; the previous window is older by design and
never raises the headline age.

## Local logs

```bash
lv24-seo logs analyze --file /var/log/nginx/access.log [--bot googlebot|yandexbot|bingbot|other] [--json]
```

Read-only analysis of exactly one local nginx JSON access log (`log_format ... escape=json`). The
path must be absolute and name one regular file of at most **100 MiB**. Relative paths, globs,
UNC/network paths, Windows device names, directories and larger files fail with `INVALID_INPUT`
(exit `2`) before any bytes are read. The file is opened read-only once; nothing is uploaded,
stored or added to the SQLite history.

`data` contains `total_lines`, `parsed_lines`, `skipped_lines`, `bot_daily` and `crawl_waste`:

- `bot_daily` is one row per `(date, bot, status class)` with `hits`, `bytes` and `p95_ms` (the
  nearest-rank p95 of `request_time`, in milliseconds). Malformed or blank lines are skipped and
  reported in `warnings`; an entry without a usable timestamp lands in the `"unknown"` date bucket.
- `crawl_waste` reports `hits`, `bytes`, `share`, `total_hits`, `total_bytes` and a `by_bot` split.
  Redirects (3xx), errors (4xx+5xx), assets (`/assets/`, `/static/`, asset extensions), `/api/`
  calls and requests carrying tracking parameters (`utm_`, `fbclid`, `gclid`) are waste. A request
  matching several categories counts **once**, so `share` stays inside `0..1`.
- `--bot` scopes `bot_daily` and the headline waste numbers to one class; `by_bot` always covers
  every parsed request, so one call still shows which crawler causes the waste. Without `--bot`,
  every request in the log counts, including human and non-search traffic.

Bot classes are **user-agent only** (`googlebot`, `yandexbot`, `bingbot`, `other`). A spoofed UA is
not a verified crawler: there is no reverse-DNS or published-IP check, and non-search crawlers such
as AhrefsBot or SemrushBot are deliberately reported as `other`. `meta` carries `source: "log_file"`,
`file`, `size_bytes`, `bot`, `total` (rollup rows), `truncated: false`, `data_age_days: null` and the
limitations list. The same contract is exposed as the `seo_log_analysis` MCP tool.

## Dashboard stats

```bash
lv24-seo stats [--days 7] [--lag-days 2] [--strict] [--json]
```

Read-only snapshot for the desktop dashboard. It never writes snapshots, pages or issues and does
not need a SQLite store. Services always appear in this order: `gsc`, `yandex_webmaster`,
`yandex_metrika`, `bing`, `crux`, `psi`. `--days` is the current window length (1..3660; larger
values fail with `INVALID_INPUT`); `--lag-days` shifts both windows back, and the previous window is
the equally long window immediately before it.

```json
{
  "period": { "start": "2026-11-07", "end": "2026-11-13" },
  "previous_period": { "start": "2026-10-31", "end": "2026-11-06" },
  "generated_at": "2026-11-14T12:00:00+00:00",
  "services": [
    {
      "provider": "gsc",
      "label": "Google Search Console",
      "configured": true,
      "ready": true,
      "status": "ok",
      "metrics": { "clicks": 1284, "impressions": 48213, "ctr": 0.0266, "position": 8.4 },
      "previous": { "clicks": 1048, "impressions": 46761, "ctr": 0.0224, "position": 9.2 },
      "rows": 7,
      "data_age_days": 2,
      "truncated": false,
      "limitations": [],
      "warnings": [],
      "error": null
    }
  ],
  "coverage": { "configured": 6, "returned": 5, "failed": 0, "missing": 1, "complete": false },
  "limitations": [],
  "warnings": []
}
```

- `status`: `ok` (rows returned), `no_data` (reachable, no rows), `missing_token`,
  `not_configured`, `error` (`error` is `{code, message}`, never a traceback).
- `configured` means the target is present in `seo.toml`; `ready` also requires the provider token
  (PSI works keyless).
- `metrics` carries only reported values: no zero fill and no cross-engine totals. `ctr` is a 0..1
  ratio; `position` is the provider's average position (lower is better).
- `previous` exists only for comparable current/previous windows (GSC, Yandex Webmaster, Metrika);
  Bing, CrUX and PSI return `null`. Bing has no window in its API.
- Metrika reports **organic** and **all-traffic** numbers separately: `organic_visits`/`organic_users`
  apply `filters=ym:s:lastTrafficSource=='organic'`, while `all_visits`/`all_users` cover every
  traffic source. The two are never merged, and `limitations` repeats the scope rule.
- CrUX adds `units` (`metric -> ms|score`) for p75 field metrics. PSI reports lab
  `performance`/`seo` scores (0..1) from the first configured URL plus field rows when present; with
  several URLs it adds the limitation "lab data, first URL only" and checks at most five URLs.
- `rows` is the number of provider rows returned, not a total. `coverage.complete` is true only
  when every configured service is `ok`, has rows, is not truncated, and none is missing or failed;
  it is `false` when no provider is configured.
- A provider failure is isolated: other services still return their data and the envelope stays
  `ok: true` with `coverage.failed` incremented.

## Core Web Vitals

```bash
lv24-seo cwv collect --url https://example.com/ --url https://example.com/pricing \
  --device all --source crux --limit 10 [--category performance ...] [--config seo.toml] [--json]
lv24-seo cwv audits --url https://example.com/ [--strategy mobile|desktop] \
  [--category performance|seo|accessibility|best-practices ...] [--config seo.toml] [--json]
lv24-seo cwv history (--url https://example.com/page | --origin https://example.com) \
  [--device phone|desktop|tablet] [--weeks 1..40] [--config seo.toml] [--json]
```

`cwv collect` is the writer that makes the weekly `cwv` section populate in real runs. It queries
CrUX **per URL and device** (`--device phone|desktop|tablet|all`; `mobile` is accepted as an alias
for `phone`) and stores each p75 as one `metric_daily` row:

| Column | Value |
|---|---|
| `source` | `crux` (field data of record) |
| `scope` | canonical device: `phone`, `desktop` or `tablet` |
| `key` | the absolute page URL passed with `--url` |
| `metric` | `LCP`, `INP`, `CLS`, `FCP` or `TTFB` |
| `value` | CrUX p75 (ms; CLS is a 0..1 score) |
| `date` | fetch date in `report.timezone` |
| `accuracy` | `crux_field_28d` |

CrUX reports a 28-day rolling window, so `meta.data_age_days` is computed from the
`collectionPeriod.lastDate` the provider returned, not from a fixed guess. Each CrUX row carries
`first_date`/`last_date`, and `meta.collection_period` carries the same bounds; when the provider
omits the period (including a 404) the old two-day heuristic is used and `crux_period_missing` says
so. A low-traffic URL is
`404` upstream; it becomes `low_traffic` in `limitations[]` and writes no row — never a zero. Metrics
outside the five canonical names (for example deprecated FID) are skipped and named in
`cwv_metric_unsupported`. `--limit` caps the number of URLs (1..50); `meta.total` counts requested
URLs, `meta.processed` counts the URLs inside the cap, `meta.pairs_total`/`meta.pairs_processed`
count the URL×device pairs, `meta.calls` counts provider requests actually made, and a capped run
sets `meta.truncated: true` with `cwv_url_cap`. Duplicate URLs are queried once. When CrUX
normalizes the requested URL, the run carries a warning and `crux_url_normalized`.

PSI is **lab** data. `--source psi` runs Lighthouse for diagnostics, but its rows are returned under
`data.lab` and are never written to `metric_daily`: the CWV builder treats every stored p75 as field
data, so persisting a lab score would pollute the field series. Every run that includes PSI reports
`cwv_lab_ignored`. PSI supports only mobile and desktop; `tablet` is skipped (not silently mapped) and
reported as `psi_tablet_unsupported`. PSI works without a key; CrUX requires one, and a missing CrUX
key returns the standard `AUTH_REQUIRED` envelope with
`next: lv24-seo auth login --provider crux` before any network call.

`--category` (repeatable) selects the Lighthouse categories for the PSI path; the default stays
`performance` + `seo`, and a crux-only run reports `cwv_categories_ignored` instead of pretending the
flag applied.

`cwv audits` is the read-only Lighthouse diagnostics surface for one URL. It returns at most 100
audit rows (`meta.total`/`meta.truncated`/`psi_audits_cap` carry the bound) with `id`, `title`,
`score`, `display_value`, `description`, `details_type`, `overall_savings_ms`,
`overall_savings_bytes` and `items_count`; descriptions are plain text (HTML stripped) and item
arrays are never included. Category scores are returned under `data.categories`. A provider
`runtimeError` becomes `psi_runtime_error`, a CAPTCHA becomes `psi_captcha`, and a `finalUrl` that
differs from the requested URL becomes `psi_final_url_differs` — a run is never called clean when
Lighthouse reported a problem. `meta.analysis_utc_timestamp` and `meta.final_url` expose the run
identity. Nothing is persisted.

`cwv history` reads `records:queryHistoryRecord` and returns weekly p75 periods, newest last. Each
row has `first_date`/`last_date`, an optional canonical `device`, and the metric keys `lcp`, `inp`,
`cls`, `fcp`, `ttfb`, `fid` when present. `--weeks` (1..40) is sent as `collectionPeriodCount` and
also enforced locally, so `meta.total`/`meta.truncated` stay honest if a provider returns more.
Missing p75 cells are reported as `crux_history_gaps`, never filled; fewer periods than requested
sets `crux_history_short`; non-CWV metrics are filtered and named in `crux_history_metrics_filtered`.
A low-traffic target is `low_traffic`, and `data_age_days` comes from the newest period's
`lastDate`. Nothing is persisted.

`report weekly` and `seo_weekly_report` read the stored rows end-to-end. The CWV read includes today,
not only the provider-lagged traffic window, so a collection is visible in the same weekly run.

## Reports

```bash
lv24-seo report weekly [--days 7] [--lag-days 2] [--send] [--out reports/] [--strict] [--json]
```

- `--days` current window (1..3660; larger values fail with `INVALID_INPUT`), `--lag-days`
  provider lag (GSC 2–3 days).
- `--send` delivers through the channels enabled in `seo.toml` (email/Telegram). Without it the
  report is only built and stored as a snapshot.
- `--out DIR` writes `report.json` (canonical English) and `report.html` (rendered in
  `report.locale`).
- Reports are idempotent: a second run for the same period and content is skipped
  (`already_sent`), and `only_on_change` skips a send when nothing material changed.

Human-readable output follows `report.locale` (`ru`, `en`, `zh`; default `ru`): the weekly CLI
summary, `report.html` from `--out`, and delivered email/Telegram messages, including number,
percent, position and date formatting. The weekly envelope also carries `meta.locale`, the language
used for that rendering. `--json` envelopes, `report.json` and `report diff --markdown` stay
canonical English, and machine codes are never translated, so scripts and snapshots do not depend
on the display language. Switching `report.locale` to `ru` or `zh` re-sends the same period once in
the new language; the English digest stays compatible with pre-localization ledger rows. The
material gate runs first, so with `only_on_change` enabled a pure locale switch can still be
suppressed when the period has no material change.

When the two GSC windows are comparable and the local store holds at least one crawl issue, page row
or indexing event, the weekly report adds an **attributed change digest** section. Each item pairs a
significant, non-flat headline change with a candidate cause (`new_crawl_issue`,
`resolved_crawl_issue`, `indexing_event`, `page_state` or `unexplained`), a confidence
(`high`/`medium`/`low`), up to three evidence rows (rule id and URL, indexing event, or page field)
and a `next_action`. With no matching
evidence the item stays `unexplained` and says so — the digest never invents a cause to fill the gap.
A rule known not to move search metrics (image alt text, HTML `lang`) is never offered as a cause;
an unknown rule is only low-confidence evidence. Items are capped at 8 and evidence at 3 per item;
the section carries `total`, `truncated` and `data_age` (the oldest evidence date actually shown),
and its limitation codes are merged into the report's `limitations`. Stored evidence is the `issues`
and `pages` tables plus, since schema v4, `indexing_events`; indexing events are read only inside the
two compared windows, because an event outside them cannot explain this period's change. Each source
is capped at 500 rows per report and the limitation says so when the cap is hit. Without comparable
GSC windows the section is absent.

When the store holds CrUX **field** rows written by `cwv collect` (read through today, so a fresh
snapshot is visible even when the traffic windows are provider-lagged), the weekly report also adds a
**Core Web Vitals by template** section (`cwv`). It is opt-in for the caller: the service passes
stored rows only, and a report built without them keeps the historical section list. Rows are
grouped by normalized URL template (`/blog/{id}`) and device (`phone`, `desktop`, `tablet`), and the
section shows at most the **top 5 templates**: regressed templates first, then the largest relative
regression, then poor-metric count and worst status. Each row carries the template, device, URL
count, sample/compared counts, core-metric coverage, per-metric `p75` with `status`, previous p75,
delta and `regressed`, plus the template's worst regression and `poor_metrics`. A regression means a
p75 increase of at least 5% (and above 0.01 for CLS). Origin aggregates, lab/PSI scores and rows
without a URL or a known device are not merged into a template; they are counted in
`limitations` (`cwv_template_missing`, `cwv_device_missing`, `cwv_lab_ignored`,
`cwv_values_ignored`). The section carries `total`, `truncated` and `data_age`; the
`cwv_p75_worst_page` limitation states that a template p75 is the worst page p75 in that group,
not a site-wide average. Missing previous p75 yields `cwv_no_comparison` or
`cwv_partial_comparison` instead of an invented delta. `report weekly` and `seo_weekly_report`
share this section; no provider is called to build it.

```bash
lv24-seo report diff [--base START..END] [--head START..END] [--markdown] [--strict] [--json]
```

Diffs two **stored** weekly snapshots (default: the two newest periods) without calling any
provider: headline metric changes, issue counts per severity, opportunities added/resolved and
limitations added/lifted. A metric present in only one snapshot is listed as unknown (never
zero), and `comparable`/`meta.complete` is `false` when either snapshot had no headline
comparison or the period lengths differ. `--markdown` prints only GitHub-flavoured Markdown for a
PR comment or CI summary; warnings go to stderr, so stdout stays valid Markdown. Every value that
comes from provider data (queries, pages, property ids, limitation text) is Markdown-escaped, so a
crawled title cannot inject a link, emphasis or `@` mention. The Markdown path exits with the
envelope code, so `--strict` on truncated data exits `5` after printing the diff. Output is
deterministic so it diffs cleanly between runs. Needs `init` and at least two `report weekly` runs
for different periods.

Planned for the next wave: `report indexing`, `report opportunities`, `report history`
(see `docs/plans/2026-11-10-implementation-plan.md`, WP5.5).

## Tasks

```bash
lv24-seo tasks list [--property ID] [--kind KIND] [--state open|in_progress|verified|dismissed] [--limit 200] [--json]
lv24-seo tasks add --kind KIND --url URL --rule RULE [--property ID] [--payload JSON] [--json]
lv24-seo tasks update TASK_ID --state open|in_progress|verified|dismissed [--json]
```

The local finding lifecycle lives in SQLite (migration v3; the current schema is v4). A task is keyed
by `(kind, property_id, url, rule_id)`, so `tasks add` is idempotent: it returns
`data.outcome: created`, `existing` or `reopened` instead of duplicating a finding. A `verified` task
observed again reopens to `open` — the recurrence is a proven regression; a `dismissed` task stays
dismissed until an explicit update. A repeated add without `--payload` is a true no-op; with
`--payload` it refreshes the evidence and `updated_at` without resetting triage or `created_at`.

| From | Allowed to |
|---|---|
| `open` | `in_progress`, `verified`, `dismissed` |
| `in_progress` | `open`, `verified`, `dismissed` |
| `verified` | `open`, `dismissed` |
| `dismissed` | `open`, `in_progress` |

Re-applying the current state is a no-op; any other move is `INVALID_INPUT` and names the allowed
set. `list` is newest-first and bounded (`--limit` 1..1000, default 200); when capped it returns
`ok: true` with `meta.truncated: true` and a limitation. `--payload` must be a JSON object and
rejects credential-shaped keys (`token`, `secret`, `password`, `api_key`, `authorization`), so
evidence can never become a secret store. The commands need an initialised workspace; without the
data directory they fail with `CONFIG_INVALID` and a `next_action`.

## Portfolio

```bash
lv24-seo portfolio list --root /absolute/sites [--limit 1..50] [--json]
```

Read-only roll-up over sibling project folders. `--root` must be an absolute, existing directory;
the scan looks for `seo.toml` up to three levels below it and skips hidden, `node_modules`,
`__pycache__` and `venv` directories plus symlinked directories (cycle safety). At most 50 sites are
inspected (`--limit` 1..50, default 50). Discovery still counts every config, so a capped result
returns `meta.truncated: true` with an honest `total` and a limitation instead of presenting a
partial portfolio as complete.

Each `data.sites[]` row carries `config_path`, `name`, `property`, `base_url`, `data_dir`,
`db_path`, `db_present`, `crawl_runs`, `last_crawl_at`, `report_snapshots`, `last_report_at`,
`last_report_period`, `issues_open`, `issues_total`, `issues_critical`, `last_issue_at`,
`last_activity_at` and `error`. Rows are newest activity first, then by config path. A broken TOML
file or an unreadable database becomes a per-site `error` object (`CONFIG_INVALID`, `STORE_LOCKED`,
`STORE_CORRUPT`, `STORE_UNAVAILABLE`) and the rest of the portfolio still scans.

The scan never writes, never calls a provider and never reads a secret: configs are loaded with an
empty environment (an ambient `LV24_SEO_DATA_DIR` cannot redirect every site), a relative data dir is
anchored to its own config, and SQLite is opened with `immutable=1` for a quiescent file or
`mode=ro` for a live WAL. A database with a pending rollback journal or an uncheckpointed WAL
without its `-shm` is reported as unavailable rather than opened read-write. `meta` carries `root`,
`limit`, `count`, `total`, `truncated`, `data_age_days: 0`, `read_only: true`, `max_depth` and
`limitations`.

## Backup

```bash
lv24-seo backup create --out /absolute/existing/dir [--force] [--json]
lv24-seo backup restore --file /absolute/backup.zip --to /absolute/new-dir [--json]
lv24-seo backup restore --file /absolute/backup.zip --config /absolute/seo.toml [--json]
lv24-seo backup restore --file /absolute/backup.zip --config /absolute/seo.toml \
  --allow-write --confirm TOKEN --json
```

`backup create` writes one portable `lv24-seo-backup-YYYYMMDDTHHMMSSZ.zip` to an existing absolute
directory. The archive contains:

- `manifest.json` — format `lv24-seo-backup` v1, engine and schema versions, creation time, every
  payload file with its size and SHA-256, totals, a bounded summary and limitations;
- `seo.toml` — the validated config with secret-looking keys masked as `***` and
  `storage.data_dir` reset to `./data`, so an unpacked bundle keeps its data next to its own
  config. A config that does not validate is kept as a redacted raw copy with a warning;
- `data/seo.sqlite3` — a consistent copy made through the SQLite backup API, forced to a
  single-file DELETE journal. Report snapshots live in the `snapshots` table and travel inside it.

`secrets.json`, provider tokens, logs, raw provider responses and caches are never included. The
source workspace is only read (`mode=ro`, `PRAGMA query_only`); the archive is written to a
temporary file and renamed atomically. `--force` replaces an archive with the same timestamped
name; without it an existing file is `INVALID_INPUT`. Limits: 4 MiB config, 2 GiB database (plus
WAL). The envelope carries `archive_path`, `archive_size`, `archive_sha256`, the full `manifest`
and a flat `summary`. A corrupt source is `STORE_CORRUPT` and is left untouched (no `.corrupt-*`
copy is written by this command).

`backup restore` has two mutually exclusive modes: `--to DIR` and `--config ABS`.

**New directory (`--to`).** The archive is validated exactly like the in-place plan (manifest,
member set, member names, sizes and every SHA-256), then `seo.toml` and `data/seo.sqlite3` are
extracted into a directory that must not exist; its parent must exist. The current workspace,
config and database are never modified, so no `--allow-write` or confirm token is involved.
The target must be an absolute path; an existing, non-empty or symlinked target is
`INVALID_INPUT` and is never merged or overwritten. Members with absolute paths, `..` segments,
backslashes or symlinks are rejected before extraction. The database passes `PRAGMA quick_check`
both before and after it is moved into place, and the staging directory is removed. An archive
without a database produces a config-only directory with `database.in_archive: false`,
`database.restored: false`, `database.path: null` and an explicit warning — no empty database is
invented. A failure removes only the directory this call created. The envelope carries
`mode: to_directory`, `target.directory`, `target.config_path`, `target.database_path`,
`config.valid`, `database.quick_check`, `database.properties`, `database.snapshots` and
`warnings`; there is no `confirm_token`. Report snapshots travel inside the SQLite file and are
counted, not written as separate files.

**In place (`--config`)** is **two-phase**. Without `--confirm` it only validates the archive and
returns a plan (`status: planned`): the archive hash and manifest hash, the target config/data
directory, the current and archived database hashes, the config action (`create`, `replace` or
`write_alongside`) and a short-lived `confirm_token` with `expires_at`/`ttl_seconds` (300
seconds). Nothing outside a temporary directory is written in this phase. Applying needs both
`--allow-write` and the token: `--allow-write` alone only plans and warns, and a token without
`--allow-write` is `WRITE_DISABLED`. The token is bound to the archive bytes and the target
state, so a changed archive, a changed workspace, an expired token or a replay is
`CONFIRM_REQUIRED`. The database is always restored to `<config dir>/data/seo.sqlite3`; an
archived config whose `data_dir` points elsewhere is `INVALID_INPUT`. Before replacing anything,
the current database is copied to `<db>.pre-restore-<utc>` (and the current config to
`seo.toml.pre-restore-<utc>` when it is replaced). Every member hash is re-verified, the restored
database passes `PRAGMA quick_check`, and the files are replaced atomically. A config that does
not validate is written alongside as `seo.toml.restored-<utc>` so the existing config stays
usable. Passing `--allow-write` or `--confirm` together with `--to` is `INVALID_INPUT`, and
`--to` with `--config` is refused.

The in-place mode replaces files; neither mode merges rows or settings. Provider tokens are not in
the archive, so re-authenticate with `lv24-seo auth set`. For the in-place mode, stop other
CLI/desktop processes first — a database held open cannot be replaced on Windows. The in-place token
prevents accidental or stale applies; it is not access control, because anyone who can run the CLI
can restore. Prefer `--to` when you want to inspect an archive or compare it with the current
workspace before replacing anything.

## Bing reads, sitemap/URL submission and IndexNow

```bash
lv24-seo sitemap submit --provider gsc --property sc-domain:example.com --url https://example.com/sitemap.xml [--allow-write] [--confirm TOKEN] [--json]
lv24-seo sitemap submit --provider bing [--property https://example.com] [--url https://example.com/sitemap.xml] [--allow-write] [--confirm TOKEN] [--json]
lv24-seo bing submit [--url https://example.com/a ...] [--from-sitemap] [--sitemap-url https://example.com/sitemap.xml] [--site https://example.com] [--allow-write] [--confirm TOKEN] [--json]
lv24-seo bing quota [--config ABS] [--json]
lv24-seo bing sitemaps [--details https://example.com/sitemap.xml] [--config ABS] [--json]
lv24-seo bing issues [--config ABS] [--json]
lv24-seo bing pages [--site https://example.com] [--config ABS] [--json]
lv24-seo bing traffic [--site https://example.com] [--config ABS] [--json]
lv24-seo bing crawl [--site https://example.com] [--config ABS] [--json]
lv24-seo bing urlinfo --url https://example.com/page|domain:example.com [--site https://example.com] [--config ABS] [--json]
lv24-seo bing children --url https://example.com|domain:example.com [--page 0] [--pages 1] [--site https://example.com] [--config ABS] [--json]
lv24-seo bing users [--site https://example.com] [--config ABS] [--json]
lv24-seo bing sites [--site https://example.com] [--config ABS] [--json]
lv24-seo bing joins [--query "seo tools"] [--page https://example.com/page] [--site https://example.com] [--config ABS] [--json]
lv24-seo indexnow submit --host example.com --key KEY --url https://example.com/a [--allow-write] [--confirm TOKEN] [--json]
lv24-seo indexnow submit --from-sitemap [--sitemap-url https://example.com/sitemap.xml] [--url ...] [--allow-write] [--confirm TOKEN] [--json]
```

`sitemap submit --provider bing` is a real gated write. Pass `--allow-write` and set
`LV24_SEO_WRITE_SECRET` to get a plan; apply with `--allow-write --confirm TOKEN`. The site comes
from `providers.bing.site` or `--property`, and the sitemap from `--url` or
`<project.base_url>/sitemap.xml`. The `bing_api_key` secret is required; a missing key is
`AUTH_REQUIRED` and the plan also checks it. The apply calls the current JSON `SubmitFeed`
endpoint (SOAP/POX are retired) and returns `{provider, status, site_url, sitemap_url}`.
GSC keeps `--property`/`--url` required and still returns `WRITE_DISABLED` on apply; Bing
`action=delete` is also `WRITE_DISABLED`.

`bing submit` is the URL-level write. The site comes from `--site` or `providers.bing.site`; a
missing site is `CONFIG_INVALID` and a missing `bing_api_key` is `AUTH_REQUIRED`. `--url` may be
repeated and is merged with `--from-sitemap`; a `<urlset>` is read directly and a
`<sitemapindex>` is bounded to the root plus five children and 10,000 URLs. URLs are
deduplicated, off-site URLs are skipped, and `meta.truncated`/`meta.total`/`meta.limitations`
report any cap. The plan reports the URL count, the 500-URL batch count and the current
remaining quota. Apply re-fetches the sitemap and a changed URL set invalidates the token with
`CONFIRM_REQUIRED`. `SubmitUrlbatch` returns no per-URL status, so the result reports
`status: accepted`, `accepted`, `batches`, `batch_size`, `duplicates_removed` and
`quota_after` — never "indexed". Bing documents no idempotency and a duplicate URL may consume
quota (`submit_idempotency_undocumented`); the SQLite write ledger still blocks a replayed
confirm token.

`bing quota` reports `daily_remaining` and `monthly_remaining`. Bing's `DailyQuota` /
`MonthlyQuota` fields are remaining values, not totals (verified against lv24.by), so no
used/total value is computed. The monthly window can bind before the daily one and its reset is
undocumented. `bing sitemaps` lists `GetFeeds` rows (`url`, `type`, `status`, `submitted`,
`last_crawled`, `url_count`, `file_size`, `compressed`); `Status` is a free provider string, not
a crawl/index status, and per-URL errors are not exposed. `--details FEED_URL` reads
`GetFeedDetails` for sitemap-index children. `bing issues` decodes the `Issues` bitmask:
`1=301`, `2=302`, `4=4xx`, `8=5xx`, `16=robots`, `32=malware`, `64=important_robots`,
`128=dns`, `256=timeout`; rows expose `issues` labels, `issues_bits`, `url`, `http_code` and
`in_links`. The feed has no date, so `data_age_days` is `0`.

`bing pages`, `bing traffic`, `bing crawl` and `bing joins` are read-only stats views. All of
them return `{site_url, rows}` (joins add `mode`, `query`, `page`), sorted as Bing returns them,
with `meta.data_age_days` from the newest provider `Date` and ISO-8601 UTC dates in rows. An
empty stats response carries `no_rows_for_window` and prints an explicit note: `rows: 0` is
missing data for the window, **not** zero impressions. `bing pages` reads `GetPageStats` rows
(`page`, `clicks`, `impressions`, `click_position`, `impression_position`, `date`); `bing
traffic` reads daily `GetRankAndTrafficStats` rows (`date`, `clicks`, `impressions`) covering
all Bing verticals since 2023-03-24; `bing crawl` reads the six-month `GetCrawlStats` window
(`date`, `code_2xx`, `code_301`, `code_302`, `code_4xx`, `code_5xx`, `blocked_by_robots_txt`,
`contains_malware`, `crawl_errors`, `crawled_pages`, `in_index`, `in_links`, `all_other_codes`)
and always reports `crawl_stats_six_month_window`.

`bing urlinfo --url URL|domain:HOST` reads `GetUrlInfo` (`url`, `anchor_count`,
`discovery_date`, `document_size`, `http_status`, `is_page`, `last_crawled_date`,
`total_child_url_count`). The WCF `Date(-62135596800000)` sentinel maps to `null`; a never-crawled
row carries `never_crawled`. `bing children --url URL|domain:HOST` reads one page per
`GetChildrenUrlInfo` POST; `--page` is zero-based, `--pages` walks up to 10 pages. A full final
page means more children may exist, so `meta.truncated` is `true` with `children_page_cap`
instead of looking complete. `bing users` reads `GetSiteRoles` (`email`, `role`, `role_code`,
`site`, `verification_site`, `delegator_email`, `delegated_code_owner_email`, `expired`, `date`)
and never echoes the delegated verification code. `bing sites` reads account-scoped
`GetUserSites` (`url`, `is_verified`) and works without `providers.bing.site`; verification codes
are not echoed.

`bing joins` selects one of Bing's three query/page joins. `--query Q` calls
`GetQueryPageStats` and lists pages for that query (`mode: query_pages`); `--page P` calls
`GetPageQueryStats` and lists queries for that page (`mode: page_queries`); both call
`GetQueryPageDetailStats` for daily detail rows (`mode: query_page_detail`). Every row has the
same compact shape: `query`, `page`, `clicks`, `impressions`, `click_position`,
`impression_position`, `position`, `date` (null when the endpoint does not return that field).
The requested dimension is attached explicitly, so the join is never implied by the call.

`indexnow submit` resolves defaults from config: `providers.bing.site` supplies the host,
`providers.bing.indexnow_key` the key, and `providers.bing.indexnow_key_location` the public
key file (default `https://<host>/<key>.txt`). `--host`, `--key` and `--url` still work. The key
must be 8–128 characters from `A-Z`, `a-z`, `0-9` and `-`; it is validated before the plan and
before any network call, and never echoed in a message or detail. When `keyLocation` is set, every
submitted URL must be on the same host and under its directory prefix (a key at
`https://example.com/catalog/<key>.txt` allows only `https://example.com/catalog/...`); the dry run
fails with `INVALID_INPUT` instead of discovering the mismatch at apply time.
`--from-sitemap` reads a `<urlset>` or a bounded `<sitemapindex>` (root plus five child
sitemaps, 10,000 URLs) during the plan, merges explicit `--url` values, deduplicates and
skips URLs outside the host. `meta.truncated`, `meta.total` and `meta.limitations` report any
cap or skipped URL. The apply re-fetches the sitemap; if its URL set changed, the plan token
is invalidated with `CONFIRM_REQUIRED` and nothing is sent. A missing key is `AUTH_REQUIRED`.
Provider statuses are actionable: `200` accepted, `202` accepted-pending (warning plus
`indexnow_accepted_pending`, never "indexed"), `400` invalid format (`INVALID_INPUT`), `403` key file
missing/mismatched (`AUTH_INVALID` with the exact key-file URL), `422` key/host mismatch
(`INVALID_INPUT`) and `429` spam/quota (`QUOTA_EXCEEDED`, retryable).

## Indexing automation

```bash
lv24-seo indexing status [--provider all|bing|yandex|gsc|indexnow] [--limit 50] [--live] [--config seo.toml] [--json]
lv24-seo indexing run    [--provider all|bing|yandex|gsc|indexnow] [--from-sitemap] [--sitemap-url URL] [--url URL ...] [--max-submit N] [--max-inspect N] [--force] [--allow-write] [--config seo.toml] [--json]
lv24-seo indexing history [--limit 20] [--config seo.toml] [--json]
```

`status` is read-only. `--live` reads Bing `GetUrlSubmissionQuota` and Yandex
`recrawl/quota`; without it the last recorded quota is shown with `quota_not_live`. GSC and
IndexNow use local UTC-day counters because neither provider exposes a remaining-count endpoint.
Each provider entry carries `urls`, `submitted`, `inspected`, `indexed`, `not_indexed`, `crawled`,
`queued`, `pending`, `stale`, `quota` and `limitations`.

The GSC entry additionally reports `indexed_total` from `gsc sitemaps` `contents[].indexed`
(summed across sitemaps) with `indexed_total_source: gsc_sitemaps_contents`. That field is
deprecated by Google, so it always carries `gsc_indexed_count_deprecated` and is kept separate
from the per-URL `indexed` count. It is read from GSC whenever the GSC client is available;
`--live` additionally reads Bing and Yandex quotas. Bing, Yandex and IndexNow expose no
per-URL index count: their `indexed_total` stays `null` and the entry carries
`no_per_url_index_count`. Bing also reports `site_scan_no_api` (Site Scan has no public API) and
`bing_no_per_url_index_status`; Yandex reports `yandex_no_per_url_index_status`; IndexNow reports
`indexnow_no_status_api`. None of these is an indexed total, and a zero is never rendered as
"all indexed".

`quota` always has `remaining` and `reset` where known. GSC/IndexNow reset at the next UTC
midnight (`reset` is an ISO timestamp), Yandex resets at the provider's daily boundary
(`reset: provider_midnight`), and Bing's monthly reset is undocumented (`reset: null` plus
`bing_quota_reset_undocumented`).

`run` builds a same-host queue from `<project.base_url>/sitemap.xml` (bounded to 10,000 URLs and
six sitemap documents) and/or repeatable `--url`, reads `lastmod` when present, and plans in
priority order: never submitted, then `lastmod` newer than `last_submitted_at`, then a check older
than `check_interval_days`, then the rest. Checks are read-only and run in both modes: GSC URL
Inspection (`PASS`/`Submitted and indexed` -> `indexed`, `NEUTRAL`/`FAIL` -> `not_indexed`) and
Bing `GetUrlInfo` (`crawled` when a crawl date exists, otherwise `unknown`). Without
`--allow-write` nothing is submitted; the output is a plan. With it, the engine re-reads quotas,
re-validates the plan hash, mints and consumes an internal confirm token, and records one
`indexing_runs` row per provider.

Caps are enforced before the provider call: `max_submit_per_run` (default 500), Bing's remaining
daily/monthly quota, Yandex's `quota_remainder`, `max_inspect_per_run` (default 20), GSC's local
`gsc_inspect_daily_limit` (2000) and IndexNow's local `indexnow_daily_limit` (1000). Bing batches
never exceed 500 URLs, Yandex queues one URL per request, and IndexNow never exceeds 10,000 URLs
per POST. An exhausted quota is reported as `quota_exhausted`, `submitted: 0` and
`not_attempted`; the command never claims that zero means everything is indexed.

A URL whose `last_submitted_at` is newer than its `lastmod` is skipped unless `--force` is set.
`--allow-write` requires `[indexing] enabled = true` and `LV24_SEO_WRITE_SECRET`; without either
the command fails with `WRITE_DISABLED`. `status` reports `write_secret_present` (a boolean only,
never the value) and adds `write_secret_missing` when an apply would be refused.

## Scheduling

```bash
lv24-seo schedule print --kind weekly|daily|indexing [--cron "0 6 * * 1"] [--target cron|systemd|windows] [--json]
lv24-seo schedule install --kind weekly|daily|indexing [--cron "0 6 * * 1"] [--target auto|cron|systemd|windows] [--allow-write] [--json]
lv24-seo schedule status --kind weekly|daily|indexing [--cron "0 6 * * 1"] [--target auto|cron|systemd|windows] [--json]
lv24-seo schedule uninstall --kind weekly|daily|indexing [--target auto|cron|systemd|windows] [--allow-write] [--json]
```

`print` renders the artefact without touching the host. `install` is **plan-only by default**: it
prints the cron block / systemd user units / Task Scheduler XML and the command it would run, with
`dry_run: true` and `installed: false`. Nothing is written until `--allow-write`; `--print` stays as
an alias for the default and wins over `--allow-write`. Report jobs call
`report weekly --send --json`; the `indexing` job calls
`indexing run --allow-write --config <absolute path> --json` and is named `lv24-seo-indexing`
(daily at 04:00). It still requires `[indexing] enabled = true` and
`LV24_SEO_WRITE_SECRET` in the scheduler environment; see [scheduling](scheduling.md). The job embeds
the **absolute** `--config` path and uses the config file's parent as its working directory, so a
scheduler that starts in another directory still reads the same config and data directory.

`status` is read-only. It reports `state: installed|outdated|partial|missing|unknown`, the expected
`paths`, and per-artefact `exists`/`matches` flags. `outdated` means the host copy differs from the
current spec; `partial` means only some artefacts exist (for example a systemd timer without its
service); `unknown` means the host scheduler could not be inspected (missing `crontab`/`schtasks`,
permissions) and `matches` is then `null`.

`uninstall` is also plan-only by default; with `--allow-write` it removes exactly the generated
managed cron block, the two `<job>.service`/`<job>.timer` user units or the named Task Scheduler
task, and reports `removed_items` (plus `removed_paths` for files). A second run is a no-op
(`removed: false`, `state: missing`).
Unrelated cron lines and tasks are never touched, and there are no wildcard process kills.

Only simple cron expressions (`M H * * *` and `M H * * DOW`) can be converted to systemd/Windows.

`schedule install --target cron` replaces only its own `# >>> lv24-seo:<job> >>>` block. If that
block lost its end marker, repair is conservative: only lines that look like lv24-seo entries
(`CRON_TZ=` near the top, a `# lv24-seo` comment, or a cron command invoking `lv24-seo`) are
removed, every other line is kept, and the envelope warns that the crontab was repaired so you can
review `crontab -l`. A terminated block is still replaced as a whole.

## Agent integration

```bash
lv24-seo install --client claude|opencode|vscode|cursor [--print]   # merges the MCP server entry
lv24-seo export tools [--out schemas/tools.json]                    # registry for non-MCP agents
```

`install` merges into the host config instead of replacing it, and `--print` shows the exact JSON.

## Conventions

- Dates are ISO-8601 (`2026-11-10`); durations are `7d`, `28d`, `90d` where a flag accepts them.
- Write operations (sitemap submit, recrawl queue, IndexNow submit) **are** exposed with gating:
  pass `--allow-write` (with `LV24_SEO_WRITE_SECRET` set) to get the plan and its HMAC
  `confirm_token`, then apply with `--allow-write --confirm TOKEN` (ADR-0003).
  `backup restore --config` uses the same two-phase shape for a local destructive write: the
  plan's token is bound to the archive and target state, expires in 5 minutes and is not
  replayable. `backup restore --to` is not gated because it only creates a new directory that
  must not already exist; it never modifies the current workspace. Bing sitemap submit, Bing URL
  submit and IndexNow actually submit; GSC sitemap submit, Bing sitemap delete and Yandex recrawl
  refuse with `WRITE_DISABLED` when the provider path is not wired, instead of pretending. With an
  initialised workspace the
  apply uses the SQLite idempotency ledger, so replaying the same token returns
  `status: skipped` / `reason: duplicate` and the provider is called once. The ledger lives in the
  workspace selected by `--config`, so `--config /srv/site/seo.toml` opens `/srv/site/data` even
  when the shell is elsewhere; without a workspace the command still applies but warns that replay
  protection is off for the token's five-minute lifetime. The plan opens no ledger and creates no
  database.
- Storage failures are typed, not generic `INTERNAL`: `STORE_LOCKED` (another process holds the
  write lock; `retryable: true`), `STORE_CORRUPT` (the file is not SQLite; a byte copy is preserved
  as `<db>.corrupt-<utc>` and `detail.backup_path` names it) and `STORE_UNAVAILABLE` (permissions,
  missing directory, disk). All three carry a `next_action`; see
  [troubleshooting](guides/troubleshooting.md#store_locked--store_corrupt--store_unavailable).
- Logging is opt-in: `LV24_SEO_LOG_LEVEL=INFO` (or `DEBUG`, `WARNING`, …) makes the CLI and
  `lv24-seo mcp serve` install the redacting handler. Unset means the process leaves logging to its
  caller; importing the library or calling `create_server()` never replaces root handlers. Output is
  one JSON line per record on a non-TTY stderr and a plain line on a terminal; the MCP transport logs
  to stderr only.
- No command prints a token, cookie or full `Authorization` header.

## Windows notes

- `schedule install --target windows --allow-write` writes a UTF-16 Task Scheduler XML (what
  `schtasks` expects) and registers the task; without `--allow-write` only the XML is printed.
- Paths are handled with `pathlib`; no POSIX-only assumptions in user-facing commands.
