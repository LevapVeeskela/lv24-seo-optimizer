# MCP tool surface

Design rules:

* **10 tools, not 50.** Agents pick better from a small, opinionated surface; the API shape stays
  behind the adapter.
* Names `{source}_{verb}_{object}` for primitives, `seo_{task}` for composed workflows.
* Read-only by default. Write tools carry `destructiveHint: true` and require a confirm token.
* Every response includes `truncated`, `total`, `data_age_days`, `limitations[]`.
* Compact JSON by default (`format: "json"`), markdown on request (`format: "md"`).

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
| `start_date` / `end_date` | ISO date | last 7 full days |
| `dimensions` | `query,page,country,device,date` | `["query"]` |
| `type` | `web,image,video,news,discover` | `web` |
| `limit` | 1..500 | 50 |
| `format` | `json,md` | `json` |

→ `{ "rows": [{ "keys": ["..."], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 8.4 }], "total": 1200, "truncated": false, "data_age_days": 2, "limitations": ["no_query_page_join"] }`

### `yandex_query_stats`

Inputs: `host_id`, `limit` (1..500), `order_by` (`TOTAL_SHOWS` | `TOTAL_CLICKS`), `period`.
Returns shows/clicks/position per query with `limitations: ["top_3000_queries", "no_query_page_join"]`.

### `yandex_indexing_state`

Returns indexing history buckets, in-search page counts, diagnostics with severity and dates.
`limitations: ["no_per_url_sitemap_errors"]`.

### `seo_crawl_site`

Crawls a site (default 500 URLs, max 5000), respects robots.txt, returns a summary plus rule violations
grouped by `rule_id` and severity. Optional `rules` filter and `respect_robots` (default true).

### `seo_weekly_report`

Composes GSC + Yandex + Metrika + CrUX + crawl for one property and a 7-day window. Returns
`{period, headline, deltas, issues, opportunities, sources, limitations}`. This is the tool agents should
reach for first when a user asks "how is SEO doing".

### `seo_indexing_triage`

Buckets not-indexed URLs (`coverageState` from GSC, `search-urls/events` reasons from Yandex, crawl
status) into actionable groups with a `next_action` per bucket.

### `seo_opportunities`

Returns ranked opportunities: striking distance, CTR gap, content decay, cannibalisation candidates.
Each item carries `evidence` (real numbers) and `expected_gain` (a range, never a single invented number).

## Write tools (gated)

### `seo_recrawl_request`

Yandex recrawl queue. Checks `GET /recrawl/quota` first, refuses when `urls > quota_left`, returns
`{queued, quota_left, task_ids}`. Requires `confirm_token` from a dry-run response.

### `seo_sitemap_submit`

GSC/Bing sitemap submit or delete. Requires `confirm_token`. Google offers no general URL submission —
the tool says so instead of pretending.

## Resources

| URI | Content |
|---|---|
| `seo://config` | effective config with secrets redacted |
| `seo://report/latest` | last generated report (markdown) |
| `seo://property/{id}/baseline` | last snapshot used for deltas |
| `seo://docs/tools` | generated tool reference |

## Prompts

| Name | Purpose |
|---|---|
| `weekly_seo_review` | full pipeline: report → anomalies → top 5 actions |
| `indexing_triage` | triage only, output as a fix plan |
| `content_opportunities` | opportunities → prioritised backlog |

## Error contract

```json
{ "isError": true, "code": "QUOTA_EXCEEDED", "message": "Yandex recrawl quota is 12/day, 40 requested",
  "next_action": "split the batch or wait until 00:00 MSK", "retryable": false }
```

Codes: `AUTH_REQUIRED`, `AUTH_INVALID`, `PROPERTY_NOT_FOUND`, `QUOTA_EXCEEDED`, `UPSTREAM_UNAVAILABLE`,
`TRUNCATED`, `WRITE_DISABLED`, `CONFIRM_REQUIRED`, `INVALID_INPUT`.

## Transports

* `stdio` (default): local process, credentials on the user's machine.
* `--transport http --stateless`: for a VPS or multi-client setups. Streamable HTTP only — do not ship
  the deprecated HTTP+SSE transport.

## Registration

`lv24-seo install --client claude|opencode|vscode|cursor` writes the host config:

```json
{ "mcpServers": { "lv24-seo": { "command": "uvx", "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"] } } }
```

Registry metadata lives in `server.json` (`io.github.LevapVeeskela/lv24-seo`).
