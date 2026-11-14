# MCP tool surface

Design rules:

* **11 tools, not 50.** Agents pick better from a small, opinionated surface; the API shape stays
  behind the adapter.
* Names `{source}_{verb}_{object}` for primitives, `seo_{task}` for composed workflows.
* Read-only by default. Two tools persist local snapshots and carry `readOnlyHint: false` with
  `writes_local_state: true`; gated write tools carry `destructiveHint: true` and require a confirm
  token.
* Every response includes `truncated`, `total`, `data_age_days` and `limitations[]`.
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

Filters travel as `dimensionFilterGroups`, never as a top-level `filters` key. A final page that
exactly fills the row cap is reported as `truncated: true` with `rows_cap_50000` (or a caller-cap
warning); a full page is not proof of complete coverage.

### `yandex_query_stats`

Inputs: `host_id`, `limit` (1..500), `order_by` (`TOTAL_SHOWS` | `TOTAL_CLICKS`).
The provider accepts an explicit `date_from`/`date_to` window (weekly uses the current
window, capped at 3,000 rows); the tool exposes top rows without date inputs.
Returns shows/clicks/position per query with `limitations: ["top_3000_queries", "no_query_page_join"]`.
Coverage is partial by design: absent rows are unknown, never zero.

### `yandex_indexing_state`

Inputs: none beyond the configured host (the tool reads it from config).
Returns indexing history buckets, in-search page counts, diagnostics with severity and dates.
`limitations: ["no_per_url_sitemap_errors"]`.

### `seo_crawl_site`

Crawls a site (default 500 URLs, max 5000), respects robots.txt, returns a summary plus rule violations
grouped by `rule_id` and severity. Optional `include_rules` filter (real rule ids such as
`robots-blocked`, `broken-internal-link`, `canonical-mismatch`); no `rules` parameter and no
`--ignore-robots` flag exist. Serial crawl with polite `Crawl-delay` pacing. Page text is retained up
to 20,000 characters; `word_count` stays exact. Sitemap documents use a 50 MB body budget and a
50,000-URL cap. A sitemap that fails to load reports only the exception type, never the URL or
query string. The payload also carries `sitemap_coverage` (sitemap ↔ crawl diff with
`complete`/`limitations`; see `docs/cli.md`).

### `seo_weekly_report`

Inputs: none — the property, window (`days`, default 7, max 3660), lag and timezone
(`report.timezone`) come from config/defaults. Composes GSC (headline deltas) + Yandex query window +
Metrika sessions + Bing/CrUX/PSI when configured + stored crawl issues for the period. Returns
`{period, previous_period, headline, sources, sources_failed, complete, issues, opportunities, limitations}`.
The headline compares GSC windows only; CrUX is field data, PSI is lab data, and the two are never merged.

### `seo_report_diff`

Inputs: optional `base` and `head` periods (`START..END`); empty means the two newest stored weekly
snapshots. Read-only, no provider calls: diffs headline metrics, issue counts per severity,
opportunities added/resolved and limitations added/lifted, plus a deterministic `markdown` field.
A metric present in only one snapshot is listed in `unknown_metrics` (never zero); `comparable` and
`meta.complete` are `false` when either snapshot lacks a headline comparison or the period lengths
differ. Needs `lv24-seo init` and two `report weekly` runs; otherwise `INVALID_INPUT` /
`CONFIG_INVALID` with a `next_action`. Same data as `lv24-seo report diff`.

### `seo_indexing_triage`

Inputs: none. Buckets not-indexed URLs (GSC coverage, Yandex `search-urls/events`, crawl states)
into actionable groups with a `next_action` per bucket. No `property`/`urls`/`limit` inputs —
scope comes from stored crawl + provider data.

### `seo_opportunities`

Inputs: none. Returns ranked opportunities: striking distance, CTR gap, decayed pages.
Each item carries `evidence` (real numbers) and `expected_clicks` (a prioritisation estimate,
never a traffic promise). There is no `kind` filter and no `expected_gain` range field.

## Write tools (gated)

### `seo_recrawl_request`

Yandex recrawl queue. Reads the remaining quota first, refuses when `urls > quota_left`. Requires
`allow_write=true` plus the confirm token from a dry run (HMAC `confirm_token` + `plan_hash`).

### `seo_sitemap_submit`

GSC/Bing sitemap submit. Requires `allow_write=true` plus the confirm token. Google offers no
general URL submission — the tool says so instead of pretending.

## Annotations

`schemas/tools.json` exposes `writes_local_state` per tool. It is additive: a tool can be a `read`
tool and still write local snapshots.

| Tools | `kind` | `writes_local_state` | `readOnlyHint` | `destructiveHint` | `idempotentHint` |
|---|---|---|---|---|---|
| `seo_properties`, `gsc_search_analytics`, `yandex_query_stats`, `yandex_indexing_state`, `seo_report_diff`, `seo_indexing_triage`, `seo_opportunities` | read | `false` | `true` | `false` | `true` |
| `seo_crawl_site`, `seo_weekly_report` | read | `true` | `false` | `false` | `false` |
| `seo_recrawl_request`, `seo_sitemap_submit` | write | `false` | `false` | `true` | `false` |

The two local-state tools persist crawl or weekly snapshots; they never mutate provider state and
need no confirm token. The two write tools remain gated by `allow_write=true` plus the confirm token.

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

Total: 11 tools, 3 resources, 3 prompts. `schemas/tools.json` is generated from
`lv24_seo/mcp/registry.py` — never edit it by hand.

## Error contract

```json
{ "isError": true, "code": "QUOTA_EXCEEDED", "message": "Yandex recrawl quota is 12/day, 40 requested",
  "next_action": "split the batch or wait until 00:00 MSK", "retryable": false }
```

Codes: `AUTH_REQUIRED`, `AUTH_INVALID`, `PROPERTY_NOT_FOUND`, `QUOTA_EXCEEDED`, `UPSTREAM_UNAVAILABLE`,
`TRUNCATED`, `WRITE_DISABLED`, `CONFIRM_REQUIRED`, `INVALID_INPUT`.

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

## Registration

`lv24-seo install --client claude|opencode|vscode|cursor` writes the host config:

```json
{ "mcpServers": { "lv24-seo": { "command": "uvx", "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"] } } }
```

Registry metadata lives in `server.json` (`io.github.LevapVeeskela/lv24-seo`).
