# Architecture

## 1. Layers

```
            ┌──────────────────────── surfaces ────────────────────────┐
            │  CLI (Typer)   MCP server (stdio/HTTP)   Skills/Automation │
            └───────────────▲──────────────────────────▲───────────────┘
                            │                          │
            ┌───────────────┴──────────┐   ┌───────────┴────────────┐
            │ analysis / report        │   │ automation             │
            │ (deltas, rules, digests) │   │ (scheduler, delivery)  │
            └───────────────▲──────────┘   └────────────▲───────────┘
                            │                          │
            ┌───────────────┴──────────────────────────┴───────────┐
            │ store (SQLite + gzipped JSONL raw)                    │
            └───────────────▲──────────────────────────────────────┘
                            │
            ┌───────────────┴──────────────────────────────────────┐
            │ providers (adapters) + crawl + log ingest             │
            └───────────────▲──────────────────────────────────────┘
                            │
            ┌───────────────┴──────────────────────────────────────┐
            │ core: config, http (retry/rate limit), errors, time   │
            └──────────────────────────────────────────────────────┘
```

Dependencies point downward only. `providers` and `crawl` never import `report` or `cli`. `analysis`
depends on `store` models, not on provider client classes.

## 2. The provider contract

Every adapter returns the same envelope — this is what makes multi-engine fusion possible and honest:

```python
@dataclass(frozen=True)
class ProviderResult[T]:
    source: (
        str  # canonical id: "gsc" | "yandex_webmaster" | "yandex_metrika" | "bing" | "crux" | "psi"
    )
    property_id: str  # provider property id (GSC "sc-domain:x", Yandex host_id)
    fetched_at: datetime  # UTC
    data_age_days: int  # provider lag at fetch time
    rows: list[T]
    total: int | None  # total available if the provider reports it
    truncated: bool  # True when a cap was hit
    limitations: list[str]  # machine-readable codes, e.g. "rows_cap_50000", "no_query_page_join"
    warnings: list[str]  # human hints, e.g. "Yandex returned HOST_NOT_LOADED"
```

Rules:

* `truncated` is computed from the provider's own cap (e.g. `len(rows) == 50_000` for GSC, `total > limit`
  for Yandex), never guessed.
* `limitations` codes are enum-like and translated to text in the report renderer.
* Adapters never mutate the database; the caller (`analysis` or `store`) persists.

## 3. Storage model (SQLite, WAL)

| Table | Purpose | Key columns |
|---|---|---|
| `properties` | configured sites | `provider`, `property_id`, `display_name`, `host_id`, `counter_id`, `verified` |
| `metric_daily` | true per-day rows only (queries with a `date` dimension) | `property_id`, `source`, `date`, `scope` (`total`/`query`/`page`/`device`/`country`), `key`, `metric`, `value`, `accuracy` |
| `metric_windows` | window aggregates with their real `[start, end]` (weekly GSC totals, Metrika visits) | `property_id`, `source`, `start`, `end`, `scope`, `key`, `metric`, `value`, `accuracy` |
| `schema_migrations` | applied migration ledger (append-only) | `version`, `description`, `applied_at` |
| `pages` | URL inventory | `property_id`, `url`, `status`, `index_state`, `canonical`, `title_hash`, `last_seen`, `first_seen` |
| `crawl_runs` | legacy crawl metadata (superseded by `runs kind=crawl`) | `id`, `start_url`, `started_at`, `finished_at`, `urls`, `errors`, `status` |
| `issues` | crawl + provider findings | `rule_id`, `severity`, `property_id`, `url`, `detail_json`, `first_seen`, `last_seen`, `state` (`open`/`fixed`/`ignored`) |
| `snapshots` | diff base for reports | `kind`, `property_id`, `period`, `content_hash`, `payload_gz` |
| `sends` | delivery ledger | `kind`, `property_id`, `period`, `content_hash`, `channel`, `sent_at`, `status` |
| `bot_daily` | nginx log rollup | `property_id`, `date`, `bot`, `status`, `hits`, `bytes`, `p95_ms` |
| `runs` | everything executed | `id`, `kind`, `property_id`, `started_at`, `finished_at`, `status`, `stats_json` |

Daily rows and window aggregates live in different tables by construction: a request with a
`date` dimension lands in `metric_daily` keyed by that date; anything else lands in
`metric_windows` keyed by its real window, so overlapping windows never overwrite each other.
Migration 2 marks pre-migration **GSC** daily rows (other sources are untouched) with `accuracy=legacy_unknown_granularity`; their
original window cannot be recovered. Normal daily reads exclude them. Repository callers may
explicitly use `include_legacy=True` to inspect retained data; no historical rows are deleted.

There are no on-disk raw provider archives: snapshots are gzipped canonical JSON inside the
`snapshots` table, and `raw_retention_days` is currently a reserved setting (pruning logs that it
has no effect). `data/seo.sqlite3` is git-ignored. Schema changes ship as numbered migrations in
`store/db.py` recorded in `schema_migrations`; user data is never deleted by an upgrade.

## 4. Analysis layer

| Module | Input | Output |
|---|---|---|
| `analysis/delta.py` | two `metric_daily` windows | headline deltas with significance thresholds |
| `analysis/opportunities.py` | current + previous GSC `query`/`page` rows | striking distance (pos 8–20, impressions ≥ N), CTR gaps, decayed pages (vanished rows count as zero only with complete, untruncated coverage) |
| `analysis/indexing.py` | URL Inspection + Yandex indexing + crawl | reason buckets + next action |
| `analysis/rules/` | crawl results | one module per rule, `RULE_ID`, `SEVERITY`, `check(ctx)` |
| `analysis/fusion.py` | per-provider query rows | joined view with explicit provider caveats (no fake query×page join) |

`fusion.py` never invents a Yandex query→page mapping. It joins at the page level (traffic) and at the
query level (clicks/impressions) separately, and the report labels which engine contributed what.

## 5. Report layer

* `report/builders.py` produce a `Report` dataclass (JSON-serialisable).
* `report/render.py` render the same object; no business logic in renderers.
* `report/delivery.py` implements `deliver(report, …)` for SMTP email and Telegram with a
  verifying TLS context, a normalized transport result (an HTTP 400/429 is `failed`, never
  `sent`), a semantic content hash (volatile envelope fields excluded) and an at-least-once
  retry policy documented on the function.
* Every report carries `data_age`, per-source `limitations`, and per-engine coverage: the weekly
  headline compares GSC windows only, `sources_failed` names isolated provider failures, and
  `meta.complete` also accounts for capped, sampled, empty and comparison inputs. GSC query rows
  carry an explicit top-rows limitation even below the caller cap; they do not prove exhaustive coverage.

## 6. Surfaces

* **CLI** parses args → builds one `composition.Workspace` (config + shared SQLite history) →
  runs one service operation → closes storage afterwards.
* **MCP** tools call the same services through the same composition root, so CLI and MCP share
  one history; the store closes when the transport stops. Without `init`, both run store-less
  with an explicit warning.
* **Skills** are documentation + recipes over the MCP tools; they contain no code paths of their own.
* **Automation** only composes CLI/service calls; no business logic.

## 7. Concurrency and failure model

* Synchronous provider clients share a process-local token-bucket limiter keyed by provider/method.
  Every retry acquires capacity. This limits request frequency, not persistent daily or cross-process
  quotas. The crawler separately pins validated destination IPs while retaining the original Host/SNI.
* Retries: idempotent GET/HEAD plus explicitly opted-in safe *read* POSTs (GSC searchAnalytics,
  CrUX queryRecord) with `Retry-After` support, capped at 3 attempts. Write POSTs never retry.
* Partial failure is normal: a weekly report with failing sources still produces available sections
  plus `sources_failed` and `complete: false`; it never cancels the whole run for one engine's
  401/403/429/timeout. A total outage yields an explicit no-data report. `complete` is `true`
  only when every configured source answered with rows, nothing was truncated and the two GSC
  date windows are comparable; otherwise `sources_failed` / `sources_missing` /
  `sources_incomplete` name the reason. A combined `total` is `null` (unknown) unless every
  dataset reported one, and GSC query/page rows that vanish between windows are unknown, not zero.
* MCP threading: the SDK (2.x) runs synchronous handlers in `anyio` worker threads, while the
  `sqlite3` connection is bound to its creating thread. Every tool is registered through an
  `async` adapter, so the synchronous core runs on the event-loop thread, one call at a time
  (blocking the loop while it runs). Concurrent `tools/call` requests are therefore serial, not
  parallel; `tests/unit/test_mcp_threading.py` proves both the failure without the adapter and
  the serial behaviour with it.
* Delivery is at-least-once: the sends-ledger claim and the send run inside `Store.atomic`; a
  journal failure after the message left is reported as `sent` with
  `reason=delivery_journal_failed`, never as `failed`. Telegram gets plain text (no `parse_mode`),
  with an explicit truncation marker when the report exceeds the message limit.
* Crawl persistence is one transaction per sync (`Store.atomic`); a crash mid-window cannot leave a
  half-synced period. Crawl and provider operations record `runs` rows with stats.
* The crawler is serial with `Crawl-delay` pacing; `concurrency` stays reserved. Read POST retry
  opt-in changes retry safety, not concurrency.

## 8. Security boundaries

* Tokens live in the keychain or `0600` files, never in `seo.toml`.
* Provider clients are constructed per run with scoped credentials; nothing global.
* Write operations pass through `core/write_gate.py` (plan → confirm token → apply → audit row).
* Logs are redacted at the formatter level, not per call site.
