# MCP tool surface

Design rules:

* **12 tools, not 50.** Agents pick better from a small, opinionated surface; the API shape stays
  behind the adapter.
* Names `{source}_{verb}_{object}` for primitives, `seo_{task}` for composed workflows.
* Read-only by default. Three tools persist local state (crawl/weekly snapshots and indexing
  events) and carry `readOnlyHint: false` with `writes_local_state: true`; gated write tools carry
  `destructiveHint: true`, require `allow_write=true` and a confirm token from a dry run, and use the
  same idempotency ledger as the CLI.
* Every read response includes `truncated`, `total`, `data_age_days` and `limitations[]`.
* Read tools take no `format` parameter; responses are compact JSON, and the weekly report renders
  markdown through `seo://report/latest` (or `report weekly --out`).

## Read tools

### `seo_properties`

Lists every property the configured tokens can see.

```json
{ "providers": ["gsc", "yandex_webmaster", "bing"] }
```

→ `{ "properties": [{ "provider": "gsc", "property_id": "sc-domain:example.com", "display_name": "example.com", "verified": true }] }`

### `gsc_search_analytics`

| Input | Type | Default |
|---|---|---|
| `property` | string, required | — |
| `start_date` / `end_date` | ISO date | last 7 full days in `report.timezone` |
| `dimensions` | `query,page,country,device,date` | `["query"]` |
| `limit` | 1..500 | 50 |

→ `{ "rows": [{ "keys": ["..."], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 8.4 }], "total": 1200, "truncated": false, "data_age_days": 2, "limitations": ["no_query_page_join"] }`

The richer Search Analytics selectors (`type`, filters, `filterLists`, `dataState`,
`aggregationType`, `searchAppearance`, `brandedClassification`) are exposed by the CLI
(`gsc query`) but not by this MCP tool yet; the tool keeps its stable five inputs and the row
shape above. The response metadata (`responseAggregationType`, `metadata`) is additive when the
service returns it.

Filters travel as `dimensionFilterGroups`, never as a top-level `filters` key. A final page that
exactly fills the row cap is reported as `truncated: true` with `rows_cap_50000` (or a caller-cap
warning); a full page is not proof of complete coverage.

### `yandex_query_stats`

Inputs: `host_id`, `limit` (1..500), `order_by` (`TOTAL_SHOWS` | `TOTAL_CLICKS`).
The provider accepts an explicit `date_from`/`date_to` window (weekly uses the current
window, capped at 3,000 rows); the tool exposes top rows without date inputs.
Returns shows/clicks/position and, when Yandex returns them, `avg_click_position` and
`query_id`; an unmapped indicator is kept under `extra_indicators` instead of dropping the row,
with `limitations: ["top_3000_queries", "no_query_page_join"]`.
Coverage is partial by design: absent rows are unknown, never zero.

### `yandex_indexing_state`

Inputs: none beyond the configured host (the tool reads it from config).
Returns indexing history buckets (the documented `HTTP_2XX`/`3XX`/`4XX`/`5XX`/`OTHER` series
merged per date), summary counters including `site_problems`, in-search page counts, diagnostics
with severity and dates.
`limitations: ["no_per_url_sitemap_errors"]`.

When a workspace store exists, the call also upserts provider-reported per-URL events into
`indexing_events` (schema v4). The write is idempotent per
`(provider, property_id, url, event, event_date)`, and a storage failure is a warning rather than a
failed read. Inputs and the returned shape are unchanged; the tool count is unchanged.

### `seo_crawl_site`

Crawls a site (default 500 URLs, max 5000), respects robots.txt, returns a summary plus rule violations
grouped by `rule_id` and severity. Optional `include_rules` filter (real rule ids such as
`robots-blocked`, `broken-internal-link`, `canonical-mismatch`); no `rules` parameter and no
`--ignore-robots` flag exist. Serial crawl with polite `Crawl-delay` pacing. Page text is retained up
to 20,000 characters; `word_count` stays exact. Sitemap documents use a 50 MB body budget and a
50,000-URL cap. A sitemap that fails to load reports only the exception type, never the URL or
query string. The payload also carries `sitemap_coverage` (sitemap ↔ crawl diff with
`complete`/`limitations`; see `docs/cli.md`).

This is the one tool that runs **off the event loop**: the server dispatches it to a worker thread
with its own short-lived store, under a hard 600 s budget (`CRAWL_TIMEOUT_SECONDS`). Other
`tools/call` requests keep being served while it runs. When the budget expires the client gets a
retryable `UPSTREAM_UNAVAILABLE` envelope (the thread cannot be killed and finishes in the
background); retry with a smaller `max_urls` or run `lv24-seo crawl` in a terminal.

### `seo_weekly_report`

Inputs: `days` (1..90, default 7) and `lag_days` (0..14, default 2). The property, timezone
(`report.timezone`) and remaining defaults come from config. Composes GSC (headline deltas) + Yandex
query window + Metrika sessions + Bing/CrUX/PSI when configured + stored crawl issues for the
period. Returns
`{period, previous_period, headline, sources, sources_failed, complete, issues, opportunities, limitations}`.
The headline compares GSC windows only; CrUX is field data, PSI is lab data, and the two are never merged.
When the GSC windows are comparable and the store holds crawl issues, page rows or indexing events,
the report also carries the additive `digest` section (cause, confidence, evidence, next action per
material change; see `docs/cli.md`). When the store holds CrUX/PSI field rows, it adds the additive
`cwv` section (top 5 templates by device, p75/status/regression; see `docs/cli.md`).

### `seo_report_diff`

Inputs: optional `base` and `head` periods (`START..END`); empty means the two newest stored weekly
snapshots. Read-only, no provider calls: diffs headline metrics, issue counts per severity,
opportunities added/resolved and limitations added/lifted, plus a deterministic `markdown` field.
A metric present in only one snapshot is listed in `unknown_metrics` (never zero); `comparable` and
`meta.complete` are `false` when either snapshot lacks a headline comparison or the period lengths
differ. Needs `lv24-seo init` and two `report weekly` runs; otherwise `INVALID_INPUT` /
`CONFIG_INVALID` with a `next_action`. Same data as `lv24-seo report diff`.

### `seo_log_analysis`

Inputs: `file` (required, absolute path to one nginx JSON access log, max 100 MiB) and `bot`
(optional: `googlebot`, `yandexbot`, `bingbot` or `other`; default `""` means every request).
Read-only and local: no provider call, no store and no workspace, nothing is uploaded or persisted.
Returns `{total_lines, parsed_lines, skipped_lines, bot_daily:[{date,bot,status,hits,bytes,p95_ms}],
crawl_waste:{hits,bytes,share,total_hits,total_bytes,by_bot}}` with `meta.source: "log_file"`,
`meta.truncated: false`, `meta.data_age_days: null` and a limitations list. Relative paths, globs,
UNC/device paths, directories and files over 100 MiB return `INVALID_INPUT` before reading. Bot
classification is **user-agent only** (`googlebot`/`yandexbot`/`bingbot`/`other`): a spoofed UA is
not a verified crawler, there is no reverse-DNS or published-IP check, and non-search crawlers are
reported as `other`. Waste counts redirects, errors, assets, `/api/` calls and tracking-parameter
requests once per request, so `share` stays inside `0..1`. Same data as `lv24-seo logs analyze`.

### `seo_indexing_triage`

Inputs: `max_examples` (0..20, default 5) - example URLs per bucket - and optional `url` - one
absolute URL whose live GSC URL Inspection result is added as `data.inspection`. Buckets
not-indexed URLs (GSC coverage, Yandex `search-urls/events`, crawl states) into actionable groups
with a `next_action` per bucket. Without `url`, the response keeps the honest limitation that
Google URL-level coverage is not exposed by this call; with it, `sources` gains `gsc` and the
inspection limitations (`one_url_per_call`, `url_inspection_quota_2000_per_day`,
`inspection_reflects_last_crawl`) are merged into `meta.limitations`. There is still no bulk
inspection and no live-test request. A failed inspection adds a warning and leaves the triage
usable rather than failing the whole call.

### `seo_opportunities`

Inputs: `days` (1..90, default 28), `limit` (1..500, default 50) and `min_impressions` (default 100).
Returns ranked opportunities: striking distance, CTR gap, decayed pages.
Each item carries `evidence` (real numbers) and `expected_clicks` (a prioritisation estimate,
never a traffic promise). There is no `kind` filter and no `expected_gain` range field.

## Write tools (gated)

The MCP layer implements the ADR-0003 two-phase contract. A call **without** `confirm_token` is a
dry run: with `allow_write=true` it returns `status: dry_run`, the full `plan`, a `plan_hash`, a
`confirm_token` and `ttl_seconds: 300`. Applying needs the same plan plus that token (and, when
supplied, a matching `plan_hash`; a mismatch is `CONFIRM_REQUIRED`). The secret is
`LV24_SEO_WRITE_SECRET` from the server environment, never a tool argument. With a workspace store
the call claims the idempotency slot, so replaying the token returns `status: skipped` /
`reason: duplicate` and the provider is called once; without a store the envelope warns that replay
protection is off. A provider failure releases the claim so a genuine retry is allowed.

**Current build:** `seo_recrawl_request` is wired to the Yandex recrawl queue (one POST per URL,
capped to the remaining daily quota). `seo_sitemap_submit` is wired for Bing submit; GSC submit and
Bing delete return `WRITE_DISABLED` instead of pretending.

### `seo_recrawl_request`

Yandex recrawl queue. Reads the remaining quota first and caps the list to `quota_left` (the extra
URLs are returned in `not_attempted` instead of being sent). Inputs: `host_id` (defaults to the
configured host), `urls`, `allow_write`, `plan_hash`, `confirm_token`. Apply returns one row per
URL (`status: queued`, `task_id`, `quota_remainder`, or `status: already_queued` on 409); a 429
stops the loop and reports `quota` plus `not_attempted`.

### `seo_sitemap_submit`

Bing/GSC sitemap submit. Inputs: `provider` (`gsc|bing`, default `gsc`), `property` (GSC property
id or Bing site URL; Bing defaults to `providers.bing.site`), `sitemap_url` (absolute; Bing
defaults to `<project.base_url>/sitemap.xml`), `action` (`submit|delete`), `allow_write`,
`plan_hash`, `confirm_token`. Bing submit calls the same `SeoService.submit_sitemap` path as the
CLI and returns `{provider, status, site_url, sitemap_url}`; a missing Bing key is
`AUTH_REQUIRED` during the dry run. GSC submit and Bing delete stay `WRITE_DISABLED`. IndexNow
and Bing URL submission are CLI/desktop-only and have no MCP tool: the 12-tool surface is
unchanged.

## Annotations

`schemas/tools.json` exposes `writes_local_state` per tool. It is additive: a tool can be a `read`
tool and still write local snapshots.

| Tools | `kind` | `writes_local_state` | `readOnlyHint` | `destructiveHint` | `idempotentHint` |
|---|---|---|---|---|---|
| `seo_properties`, `gsc_search_analytics`, `yandex_query_stats`, `seo_report_diff`, `seo_indexing_triage`, `seo_log_analysis`, `seo_opportunities` | read | `false` | `true` | `false` | `true` |
| `seo_crawl_site`, `seo_weekly_report`, `yandex_indexing_state` | read | `true` | `false` | `false` | `false` |
| `seo_recrawl_request`, `seo_sitemap_submit` | write | `false` | `false` | `true` | `false` |

The three local-state tools persist crawl/weekly snapshots or indexing events; they never mutate
provider state and need no confirm token. The two write tools remain gated by `allow_write=true` plus
the confirm token.

## Resources

| URI | Content |
|---|---|
| `seo://config` | effective config with secrets redacted |
| `seo://report/latest` | last generated report (markdown) |
| `seo://docs/tools` | generated tool reference |

Exactly three resources — there is no `seo://property/{id}/baseline`.

## Prompts

| Name | Purpose |
|---|---|
| `weekly_seo_review` | full pipeline: report → anomalies → top 5 actions |
| `indexing_triage` | triage only, output as a fix plan |
| `content_opportunities` | opportunities → prioritised backlog |

Total: 12 tools, 3 resources, 3 prompts. `schemas/tools.json` is generated from
`lv24_seo/mcp/registry.py` - never edit it by hand. The 0.12.2 release adds no MCP tool: the
surface stays at 12. Bing URL submission, quota, sitemap-feed and crawl-issue views are
CLI/desktop-only; `seo_sitemap_submit` still submits a sitemap, not a URL batch. The 0.9.0 release
added optional `provider`/`property`/`sitemap_url` inputs to `seo_sitemap_submit` and wired Bing
submit through the shared service path.

GSC depth stays inside the same 12 tools: `gsc_search_analytics` keeps its existing inputs (the
extra Search Analytics selectors are CLI-only for now), `seo_indexing_triage` gained the optional
`url` input for one URL Inspection, and `gsc sitemaps`/`gsc sitemap` are CLI-only. No 13th tool was
added.

## Error contract

```json
{ "isError": true, "code": "QUOTA_EXCEEDED", "message": "Yandex recrawl quota is 12/day, 40 requested",
  "next_action": "split the batch or wait until 00:00 MSK", "retryable": false }
```

Codes: `AUTH_REQUIRED`, `AUTH_INVALID`, `PROPERTY_NOT_FOUND`, `QUOTA_EXCEEDED`, `UPSTREAM_UNAVAILABLE`,
`TRUNCATED`, `WRITE_DISABLED`, `CONFIRM_REQUIRED`, `INVALID_INPUT`, `STORE_LOCKED`, `STORE_CORRUPT`,
`STORE_UNAVAILABLE`.

## Transports

* `stdio` (default): local process, credentials on the user's machine.
* `http`: streamable HTTP for a VPS or multi-client setup (`mcp serve --transport http [--port 8765]`).
  Streamable HTTP only — the deprecated HTTP+SSE transport is intentionally rejected.

## Threading

* MCP 2 dispatches sync handlers in worker threads; the project's SQLite connection uses
  `check_same_thread=True`, so tools are registered as `async` owner-loop adapters.
* The sync core runs serially on the event-loop thread and blocks the loop while it runs:
  concurrent `tools/call` requests are serialised instead of racing in SQLite.
  `check_same_thread=False` is deliberately not used.
* `seo_crawl_site` is the exception: it runs on a worker thread with a **fresh** service and store
  opened on that thread, under a 600 s budget. A long crawl therefore does not block other tools;
  on timeout the client gets a retryable `UPSTREAM_UNAVAILABLE` and the crawl finishes and closes
  its own connection in the background.

## Registration

`lv24-seo install --client claude|opencode|vscode|cursor` writes the host config:

```json
{ "mcpServers": { "lv24-seo": { "command": "uvx", "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"] } } }
```

Registry metadata lives in `server.json` (`io.github.LevapVeeskela/lv24-seo`).
