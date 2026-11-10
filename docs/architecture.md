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
    source: str  # "gsc" | "yandex_webmaster" | "yandex_metrika" | "bing" | "crux" | "psi" | "crawl"
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
| `metric_daily` | long-format aggregates | `property_id`, `source`, `date`, `scope` (`total`/`query`/`page`/`device`/`country`), `key`, `metric`, `value`, `accuracy` |
| `pages` | URL inventory | `property_id`, `url`, `status_code`, `index_state`, `canonical`, `title_hash`, `last_seen`, `first_seen` |
| `crawl_runs` | crawl metadata | `id`, `start_url`, `started_at`, `finished_at`, `urls`, `errors`, `status` |
| `issues` | crawl + provider findings | `rule_id`, `severity`, `property_id`, `url`, `detail_json`, `first_seen`, `last_seen`, `state` (`open`/`fixed`/`ignored`) |
| `snapshots` | diff base for reports | `kind`, `property_id`, `period`, `payload_hash`, `payload_gz` |
| `sends` | delivery ledger | `kind`, `property_id`, `period`, `content_hash`, `channel`, `sent_at`, `status` |
| `bot_daily` | nginx log rollup | `property_id`, `date`, `bot`, `status`, `hits`, `bytes`, `p95_ms` |
| `runs` | everything executed | `id`, `kind`, `property_id`, `started_at`, `finished_at`, `status`, `stats_json` |

Raw provider payloads are written to `data/raw/<source>/<date>/<sha>.json.gz` and pruned after 90 days;
aggregates stay forever (they are tiny). `data/seo.sqlite3` is git-ignored.

## 4. Analysis layer

| Module | Input | Output |
|---|---|---|
| `analysis/delta.py` | two `metric_daily` windows | headline deltas with significance thresholds |
| `analysis/opportunities.py` | GSC/Yandex query rows + positions | striking distance (pos 8–20, impressions ≥ N), CTR gaps, decayed pages |
| `analysis/indexing.py` | URL Inspection + Yandex indexing + crawl | reason buckets + next action |
| `analysis/rules/` | crawl results | one module per rule, `RULE_ID`, `SEVERITY`, `check(ctx)` |
| `analysis/fusion.py` | per-provider query rows | joined view with explicit provider caveats (no fake query×page join) |

`fusion.py` never invents a Yandex query→page mapping. It joins at the page level (traffic) and at the
query level (clicks/impressions) separately, and the report labels which engine contributed what.

## 5. Report layer

* `report/builders/*` produce a `Report` dataclass (JSON-serialisable).
* `report/render/{text,html,md}.py` render the same object; no business logic in renderers.
* `report/delivery/{smtp,resend,telegram}.py` implement `deliver(report, channels)` with idempotency
  (`sends` ledger + `content_hash`).
* Every report carries `data_age`, per-source `limitations`, and a "what changed since last time" block.

## 6. Surfaces

* **CLI** parses args → calls the same service functions as MCP → prints `{ok,data,meta,warnings}`.
* **MCP** tools call the same services; schemas are generated from `mcp/registry.py`.
* **Skills** are documentation + recipes over the MCP tools; they contain no code paths of their own.
* **Automation** only composes CLI/service calls; no business logic.

## 7. Concurrency and failure model

* One `httpx.AsyncClient` per provider with `limits(max_connections=4)`, per-provider semaphore, and a
  shared token-bucket limiter (`core/ratelimit.py`).
* Retries: idempotent GETs only, exponential backoff with jitter, respect `Retry-After`, cap 3 attempts.
* Partial failure is normal: a weekly report with 4 of 5 sources produces a report plus a
  `sources_failed` block; it never fails the whole run.
* Every run writes a `runs` row, including failures, so the next run can explain "no data since".

## 8. Security boundaries

* Tokens live in the keychain or `0600` files, never in `seo.toml`.
* Provider clients are constructed per run with scoped credentials; nothing global.
* Write operations pass through `core/write_gate.py` (plan → confirm token → apply → audit row).
* Logs are redacted at the formatter level, not per call site.
