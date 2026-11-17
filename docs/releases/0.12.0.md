# 0.12.0 - Quota-aware indexing automation

Status: **released** (2026-11-17) on PyPI, npm and the MCP Registry. Desktop `0.9.0` bundles engine
`0.11.0`; desktop `0.9.1` follows with the indexing status card.

Minor release: a quota-aware, idempotent indexing pipeline across Bing, Yandex, IndexNow and Google
Search Console. The CLI now has **25 top-level commands** (18 groups + 7 commands), the MCP surface
stays at **12 tools**, and the SQLite schema moves to **version 5**.

## Added

### `indexing` group

```bash
lv24-seo indexing status [--provider all|bing|yandex|gsc|indexnow] [--limit 50] [--live] [--json]
lv24-seo indexing run    [--provider ...] [--from-sitemap|--sitemap-url ABS|--url ABS ...] \
                         [--max-submit N] [--max-inspect N] [--force] [--allow-write] [--json]
lv24-seo indexing history [--limit 20] [--json]
```

* **One queue, four providers.** The queue is read from the project sitemap by default (root plus
  five child documents, 10,000 URLs) and can be merged with explicit `--url` values. Off-host URLs
  are skipped and every cap is reported (`queue_cap`, `sitemap_truncated`, `sitemap_off_host`).
* **Priority.** Never-submitted URLs first, then URLs whose sitemap `lastmod` is newer than
  `last_submitted_at`, then URLs whose `last_checked_at` is older than `check_interval_days`. A URL
  whose `last_submitted_at` is newer than its `lastmod` is skipped unless `--force` is set.
* **Quota-aware planning.**
  * Bing: `GetUrlSubmissionQuota` remaining daily/monthly; batches of 500; the monthly window can
    bind first and its reset is undocumented (`bing_quota_reset_undocumented`).
  * Yandex: `recrawl/quota` `quota_remainder`; one request per URL, capped to the remainder.
  * GSC: URL Inspection is capped by a local `quota_usage` counter (default 2,000/day/site), because
    Google exposes no remaining count.
  * IndexNow: a local daily cap (default 1,000) and a 10,000-URL POST bound; a missing key is
    `AUTH_REQUIRED`.
* **Index checks are honest.**
  * GSC URL Inspection is the only per-URL verdict: `PASS`/`Submitted and indexed` → `indexed`;
    `NEUTRAL`/`FAIL` → `not_indexed`; anything else → `unknown`.
  * The GSC sitemap `contents[].indexed` aggregate is reported separately as `indexed_total` and
    always tagged `gsc_indexed_count_deprecated`.
  * Bing `GetUrlInfo` yields `http_status` and `last_crawled_at`; a crawled URL is `crawled`, not
    `indexed` (`bing_no_per_url_index_status`).
  * Yandex and IndexNow have no per-URL index status (`yandex_no_per_url_index_status`,
    `indexnow_no_status_api`). Their `indexed_total` is `null` with `no_per_url_index_count`.
  * Bing Site Scan has no public API: every Bing result carries `site_scan_no_api`; the engine never
    pretends to automate it.
* **Two-phase writes.** `indexing run` without `--allow-write` only checks and plans. With it, the
  engine re-reads quotas, rebuilds the plan, verifies the plan hash, mints and consumes an internal
  confirm token through `WriteGate`, and records one `indexing_runs` row per provider. The
  scheduled job is the explicit automation opt-in; the secret is never embedded in the command.
* **Status.** `indexing status` shows per provider `urls`, `submitted`, `crawled`, `queued`,
  `indexed` (sample), `indexed_total` (GSC aggregate), `pending`, `quota` and `limitations`.
  `write_secret_present` reports only whether `LV24_SEO_WRITE_SECRET` is set; the value is never
  read into the envelope. `--live` reads Bing and Yandex quotas now.

### Scheduler

`lv24-seo schedule install --kind indexing` renders a daily `lv24-seo-indexing` job at `0 4 * * *`
with `lv24-seo indexing run --allow-write --config <abs> --json` for cron, systemd and Windows Task
Scheduler. `schedule status` and `schedule uninstall` handle it like the report jobs. The host
scheduler must provide `LV24_SEO_WRITE_SECRET` in its environment (systemd `Environment=`/
`EnvironmentFile=`, Windows user environment, cron export); the generated command never contains it.

### Store and configuration

* Schema v5 adds `indexing_state`, `indexing_runs` and `quota_usage`. Existing databases migrate in
  place; the migration is idempotent and append-only.
* `[indexing]` config: `enabled=false`, `providers=["bing","yandex","indexnow","gsc"]`,
  `max_submit_per_run=500`, `max_inspect_per_run=20`, `stale_days=7`, `check_interval_days=7`,
  `gsc_inspect_daily_limit=2000`, `indexnow_daily_limit=1000`.

## Changed

* The CLI now has **25 top-level commands**: 18 groups (`auth`, `config`, `gsc`, `yandex`,
  `metrika`, `indexnow`, `bing`, `indexing`, `sitemap`, `report`, `cwv`, `schedule`, `export`,
  `logs`, `tasks`, `backup`, `portfolio`, `mcp`) and 7 commands (`version`, `doctor`, `init`,
  `properties`, `stats`, `crawl`, `install`).
* `schedule --kind` accepts `weekly`, `daily` and `indexing`; the report jobs are unchanged.
* Documentation adds the quota/index-status matrix, the `[indexing]` section, the scheduler secret
  requirement and the honest-count limitations.

## Not changed

* MCP stays at **12 tools**; indexing automation is CLI/scheduler-only.
* No new paid provider or required dependency.
* Bing URL submission, Bing sitemap submit, IndexNow and Yandex recrawl keep their existing write
  gates; indexing reuses them rather than adding a parallel write path.

## Verification notes

Local gate on the release commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

Live read-only checks against `lv24.by`:

| Check | Result |
|---|---|
| `gsc inspect https://lv24.by/` | `PASS`, "Submitted and indexed" |
| `indexing status --live` | GSC sitemap aggregate and local quotas read; `write_secret_present` true |
| IndexNow key file | `https://lv24.by/<key>.txt` 200; plan → apply accepted; replay `skipped/duplicate` |
| Bing/Yandex quota reads | covered by unit tests; the release environment had no keychain tokens for a live read |

## Limitations

* GSC's sitemap `indexed` aggregate is deprecated and can be `0`; it is reported separately and never
  mixed with the per-URL verdict.
* Bing, Yandex and IndexNow expose no per-URL indexed count. The status says so instead of inventing
  a number.
* Bing Site Scan has no public API; the desktop only opens the official page.
* IndexNow has no status API and no published quota; the local daily cap is a conservative guard.
* Scheduled writes require `[indexing].enabled=true` **and** `LV24_SEO_WRITE_SECRET` in the host
  scheduler environment.
* `indexing run` checks at most `max_inspect_per_run` URLs per run (default 20); a large site is
  sampled over several runs, and `meta.truncated` says so.
