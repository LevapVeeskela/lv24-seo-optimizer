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

* `truncated` is computed from the provider's own cap and evidence of more data (GSC reports it when
  the cap is reached, including a final page that fills it exactly; Metrika marks it when the walked
  rows are fewer than `total_rows`, a row/page cap stops the walk, or a full page arrives without a
  total), never guessed. A full page at the cap is potentially truncated, never claimed complete.
* `total` is `int | None`: a provider that cannot prove completeness omits it (GSC returns `null`
  when `truncated` is true; a bare page `count` is never promoted to a grand total).
* `data_age_days` is measured from the newest data the provider actually reports (Bing row dates,
  Metrika's `date2`) and is never fresher than the documented provider lag (GSC floors at 3 days).
* An empty answer is data, not a failure: Yandex empty indexing/search-URL history returns
  `total=0` with the `empty_history` limitation, while `degraded_host` is reserved for an
  unreachable or not-loaded host.
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
| `tasks` | finding/task lifecycle (v3) | `kind`, `property_id`, `url`, `rule_id`, `state` (`open`/`in_progress`/`verified`/`dismissed`), `payload_json`, `created_at`, `updated_at`; unique on `(kind, property_id, url, rule_id)` |
| `snapshots` | diff base for reports | `kind`, `property_id`, `period`, `content_hash`, `payload_gz` |
| `sends` | delivery ledger | `kind`, `property_id`, `period`, `content_hash`, `channel`, `sent_at`, `status` |
| `bot_daily` | nginx log rollup | `property_id`, `date`, `bot`, `status`, `hits`, `bytes`, `p95_ms` |
| `runs` | everything executed | `id`, `kind`, `property_id`, `started_at`, `finished_at`, `status`, `stats_json` |

Daily rows and window aggregates live in different tables by construction: a request with a
`date` dimension lands in `metric_daily` keyed by that date; anything else lands in
`metric_windows` keyed by its real window, so overlapping windows never overwrite each other.
A single-dimension `key` is stored raw; multi-dimension keys are JSON-encoded, so dimension values
containing `|` cannot collide.
Migration 2 marks pre-migration **GSC** daily rows (other sources are untouched) with `accuracy=legacy_unknown_granularity`; their
original window cannot be recovered. Normal daily reads exclude them. Repository callers may
explicitly use `include_legacy=True` to inspect retained data; no historical rows are deleted.
Migration 3 adds the `tasks` table (state `CHECK` plus two indexes) and is applied on the next open;
a fresh database records versions 1..3 in `schema_migrations`. The task natural key is
`(kind, property_id, url, rule_id)`, so re-running a crawl or decay scan never duplicates a task.
`bot_daily` stays a reserved table: `logs analyze` and `seo_log_analysis` return their rollups
in-memory and do not persist them, so log history is not stored.

Task states are closed (`open`, `in_progress`, `verified`, `dismissed`). The repository validates
moves against a fixed transition map and `upsert_task` returns `created`, `existing` or `reopened`:
a `verified` task observed again reopens to `open` (a proven regression), a `dismissed` task stays
dismissed until an explicit update, and a repeated observation without a payload is a no-op. Reads
and writes happen in one `BEGIN IMMEDIATE` transaction, so concurrent crawls cannot duplicate a
task or race a triage decision.

There are no on-disk raw provider archives: snapshots are gzipped canonical JSON inside the
`snapshots` table, and `raw_retention_days` is currently a reserved setting (pruning logs that it
has no effect). `data/seo.sqlite3` is git-ignored. Schema changes ship as numbered migrations in
`store/db.py` recorded in `schema_migrations`; user data is never deleted by an upgrade.

Raw `sqlite3` failures are translated at the store boundary into typed errors: `STORE_LOCKED`
(retryable, another writer holds the lock), `STORE_CORRUPT` (a byte copy is preserved as
`<db>.corrupt-<utc>`, the original is untouched) and `STORE_UNAVAILABLE` (permissions, path, disk).
A failure the layer does not recognise is re-raised unchanged, so a real programming bug keeps its
traceback instead of being disguised as a disk problem.

## 4. Analysis layer

| Module | Input | Output |
|---|---|---|
| `analysis/delta.py` | two `metric_daily` windows | headline deltas with significance thresholds |
| `analysis/opportunities.py` | current + previous GSC `query`/`page` rows | striking distance (pos 8–20, impressions ≥ N), CTR gaps, decayed pages (vanished rows count as zero only with complete, untruncated coverage) |
| `analysis/indexing.py` | URL Inspection + Yandex indexing + crawl | reason buckets + next action |
| `analysis/digest.py` | significant delta rows + stored issues/pages (+ optional indexing events) | attributed change digest: cause, confidence, evidence, next action, `unexplained` when nothing matches |
| `analysis/logs.py` | one local nginx JSON access log | bot/day/status rollups, nearest-rank p95 and crawl-waste share (read-only, UA-only bot classes) |
| `analysis/rules/` | crawl results | one module per rule, `RULE_ID`, `SEVERITY`, `check(ctx)` |
| `analysis/fusion.py` | per-provider query rows | joined view with explicit provider caveats (no fake query×page join) |

`fusion.py` never invents a Yandex query→page mapping. It joins at the page level (traffic) and at the
query level (clicks/impressions) separately, and the report labels which engine contributed what.

## 5. Report layer

* `report/builders.py` produce a `Report` dataclass (JSON-serialisable, canonical English).
  `Report.to_dict()` has no locale field: snapshots, report diff and MCP payloads do not change
  with the display language. `build_weekly` accepts an opt-in `digest_evidence` mapping; without
  it the historical section list is unchanged. With it, the builder adds a bounded `digest`
  section whose rows carry `metric`, `change`, `cause`, `confidence`, `evidence` and
  `next_action`; `ReportSection` gained optional `total`/`truncated`/`data_age` fields that are
  only serialised for sections that set them.
* The digest is honest by construction: only significant, non-flat delta rows are explained; a
  change with no matching evidence stays `unexplained`; rules that cannot move search metrics
  (image alt, HTML `lang`) are never candidates; unknown rules are low-confidence only; items are
  capped at 8 and evidence at 3 per item, with `total`/`truncated` describing the full set and
  `data_age` the oldest evidence date actually shown. Evidence rows without a rule id/URL are
  dropped and counted in `malformed_rows`, never guessed at. The weekly service supplies stored
  `issues` and `pages` rows only (there is no indexing-event table yet) and omits the section when
  the GSC windows are not comparable or the store has no evidence at all.
* `report/i18n.py` is presentation-only: locale catalogs (`en`, `ru`, `zh`), plural rules and
  number/percent/position/date formatting. An unknown locale falls back to English; an invalid
  numeric or date value renders as empty text, never as `0`.
* `report/render.py` renders the same object. Weekly text, email text, subject and HTML are
  localized; Markdown stays canonical English. The weekly email presenter is a 600px inline-CSS
  template with KPI cards, top-3 opportunities, issue severities, merged sources, data notes and
  a footer; no external images, stylesheets, scripts or tracking pixels.
* Markdown output is escaped: `escape_markdown` escapes backslashes, backticks, `*_[]()@`, and the
  Markdown renderer additionally escapes `<`/`>`, so a crawled title, query or URL cannot inject a
  link, emphasis, raw HTML or a GitHub `@` mention into `report diff --markdown` or a PR comment.
* `report/delivery.py` implements `deliver(report, …)` for SMTP email and Telegram with a
  verifying TLS context, a normalized transport result (an HTTP 400/429 is `failed`, never
  `sent`), a semantic content hash that includes the normalized render locale (volatile envelope
  fields and diagnostics excluded) and an at-least-once retry policy documented on the function.
  A locale change therefore re-sends the same period once; rebuilding it in the same language
  does not.
* Every report carries `data_age`, per-source `limitations`, and per-engine coverage: the weekly
  headline compares GSC windows only, `sources_failed` names isolated provider failures, and
  `meta.complete` also accounts for capped, sampled, empty and comparison inputs. GSC query rows
  carry an explicit top-rows limitation even below the caller cap; they do not prove exhaustive coverage.

## 6. Surfaces

* **CLI** parses args → builds one `composition.Workspace` (config + shared SQLite history) →
  runs one service operation → closes storage afterwards. Read commands accept `--strict`, which
  sets `meta.strict` and turns a truncated success into exit `5`; the envelope stays `ok: true`
  because the data is valid, only incomplete.
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
  CrUX queryRecord) with `Retry-After` support, capped at 3 attempts. Write POSTs never retry. The
  crawler separately retries a `429` **once**, honouring `Retry-After` (seconds or HTTP-date,
  clamped to 30 s, 1 s default); a `429` on `robots.txt` defers the crawl instead of failing open.
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
  the serial behaviour with it. `seo_crawl_site` is deliberately offloaded: it runs on a worker
  thread with a fresh service/store opened there, under a 600 s budget, so a long crawl neither
  blocks other tools nor touches the loop-bound connection. On timeout the client gets a
  retryable `UPSTREAM_UNAVAILABLE` and the crawl finishes in the background.
* Delivery is at-least-once: the sends-ledger claim and the send run inside `Store.atomic`; a
  journal failure after the message left is reported as `sent` with
  `reason=delivery_journal_failed`, never as `failed`. Telegram gets plain text (no `parse_mode`),
  with an explicit truncation marker when the report exceeds the message limit.
* Crawl persistence is one transaction per sync (`Store.atomic`); a crash mid-window cannot leave a
  half-synced period. Crawl and provider operations record `runs` rows with stats. Retained page
  text is capped at 20,000 characters; `word_count` uses the full visible text.
* Write-gate claims are `pending` until the provider confirms the write, then promoted to `sent`;
  a provider exception releases the claim so a retry is allowed. A completed or in-flight claim is
  a duplicate. A crash between provider success and promotion leaves a `pending` row that is
  treated as in-flight; the engine has no TTL or automatic cleanup.
* The crawler is serial with `Crawl-delay` pacing; `concurrency` stays reserved. Read POST retry
  opt-in changes retry safety, not concurrency.

## 8. Security boundaries

* Tokens live in the keychain or `0600` files, never in `seo.toml`.
* Provider clients are constructed per run with scoped credentials; nothing global.
* Write operations pass through `core/write_gate.py` (plan → confirm token → pending claim →
  provider call → `sent` or release). CLI and MCP share the same gate, the same
  `LV24_SEO_WRITE_SECRET` and the same SQLite idempotency ledger; an MCP apply needs
  `allow_write=true` and `confirm_token`, plus a matching `plan_hash` when one is supplied.
  Local-state MCP tools
  (`seo_crawl_site`, `seo_weekly_report`) are not write-gated because they never mutate provider
  state; they are marked `writes_local_state: true` and `readOnlyHint: false`. In this build the
  engine service still exposes no recrawl/sitemap provider handler, so those applies return
  `WRITE_DISABLED` and nothing is sent.
* Logs are redacted at the formatter level, not per call site.

## 9. Backup bundle

`lv24-seo backup create` (and `core/backup.py`) produces one portable zip without a cloud account:

```
manifest.json      format/version, engine + schema versions, created_at,
                   files:[{path,size,sha256}], totals, summary, limitations
seo.toml           validated config, secret-looking keys masked as ***,
                   storage.data_dir reset to ./data
data/seo.sqlite3   consistent copy via the SQLite backup API, DELETE journal
                   (report snapshots live in the snapshots table)
```

The source workspace is never modified: the config is only read and the database is opened with
`mode=ro` and `PRAGMA query_only`. `secrets.json`, provider tokens, logs, raw responses and caches
are excluded. Every payload member is hashed before it is written, the archive itself is hashed
(`archive_sha256` in the envelope), and the zip is written to a temporary file in the output
directory and renamed atomically, so a failed run leaves no half-written archive. Caps: 4 MiB config
and 2 GiB database (plus WAL); a larger source must be archived manually. A corrupt source is
`STORE_CORRUPT` and is left untouched — the store's usual `.corrupt-*` copy is deliberately not
written by this command. **Restore is not implemented**; the manifest is the documented contract
for a manual restore.
