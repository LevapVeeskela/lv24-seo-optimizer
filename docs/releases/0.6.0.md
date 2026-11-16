# 0.6.0 - log analysis, attributed digest, task lifecycle and backup

Status: released (2026-11-16). Tagged and published: PyPI, npm and the MCP Registry serve `0.6.0`,
and desktop `0.4.0` bundles it. The counts and contracts below describe this release.

## Added

### Local nginx log analysis (`logs analyze`, `seo_log_analysis`)

`lv24-seo logs analyze --file ABS [--bot googlebot|yandexbot|bingbot|other] [--json]` reads exactly
one local nginx JSON access log (`log_format ... escape=json`) and returns `bot_daily` rows plus a
`crawl_waste` summary:

* the path must be absolute and name one regular file of at most **100 MiB**; relative paths,
  globs, UNC/network paths, Windows device names, directories and larger files fail with
  `INVALID_INPUT` (exit `2`) before anything is read;
* `bot_daily` is one row per `(date, bot, status class)` with `hits`, `bytes` and `p95_ms`
  (nearest-rank p95 of `request_time`);
* `crawl_waste` reports `hits`, `bytes`, `share`, `total_hits`, `total_bytes` and a `by_bot` split.
  Redirects, errors, assets, `/api/` calls and tracking-parameter requests are waste; each request
  counts once, so `share` stays inside `0..1`;
* malformed or blank lines are skipped and reported in `warnings`; a missing timestamp lands in the
  `"unknown"` date bucket;
* the file is opened read-only once. Nothing is uploaded, stored or added to the SQLite history.

`seo_log_analysis(file, bot="")` is the **12th MCP tool**. It is read-only (`readOnlyHint: true`,
`writes_local_state: false`), needs no service, store or workspace, and returns the same `data`,
`meta` and `warnings` as the CLI.

**Limitation:** bot classes are user-agent only. A spoofed UA is not a verified crawler; there is
no reverse-DNS or published-IP check, and non-search crawlers (AhrefsBot, SemrushBot) are reported
as `other`. The analysis is a bounded triage, not proof of crawl identity.

### Attributed change digest in `report weekly`

When the two GSC windows are comparable and the store holds at least one crawl issue or page row,
the weekly report adds an `Attributed change digest` section. Each item pairs a significant,
non-flat headline change with:

* a candidate cause: `new_crawl_issue`, `resolved_crawl_issue`, `indexing_event`, `page_state` or
  `unexplained`;
* a confidence: `high`, `medium` or `low`;
* up to three evidence rows (rule id and URL, indexing event, or page field and value);
* a `next_action` for that cause.

The digest never invents a cause: with no matching evidence the item stays `unexplained` and a
limitation says so. A rule known not to move search metrics (image alt text, HTML `lang`) is never
offered; an unknown rule is only low-confidence evidence. Items are capped at 8 and evidence at 3
per item; the section carries `total`, `truncated` and `data_age` (the oldest evidence date actually
shown), and digest limitations are merged into the report. The service supplies stored `issues` and
`pages` rows only — there is no indexing-event table yet, so `indexing_event` appears only when a
caller provides those rows. Without comparable GSC windows the section is absent.

### Finding/task lifecycle (`tasks`, schema v3)

Migration **v3** adds the `tasks` table. A task is keyed by `(kind, property_id, url, rule_id)`, so
re-running a crawl or decay scan never duplicates a finding:

```bash
lv24-seo tasks list [--property ID] [--kind KIND] [--state STATE] [--limit 200] [--json]
lv24-seo tasks add --kind KIND --url URL --rule RULE [--property ID] [--payload JSON] [--json]
lv24-seo tasks update TASK_ID --state STATE [--json]
```

* States: `open`, `in_progress`, `verified`, `dismissed`.
* `add` is idempotent and returns `outcome: created`, `existing` or `reopened`. A `verified` task
  observed again reopens to `open` (a proven regression); a `dismissed` task stays dismissed until
  an explicit update. A repeated add without `--payload` is a no-op; with a payload it refreshes
  the evidence without resetting triage or `created_at`.
* Allowed transitions: `open → in_progress|verified|dismissed`,
  `in_progress → open|verified|dismissed`, `verified → open|dismissed`,
  `dismissed → open|in_progress`. Re-applying the current state is a no-op; any other move is
  `INVALID_INPUT` and names the allowed set.
* `list` is newest-first and bounded (`--limit` 1..1000, default 200); when capped it returns
  `ok: true` with `meta.truncated: true` and a limitation.
* `--payload` rejects credential-shaped keys, so evidence cannot become a secret store.

The commands need an initialised workspace and fail with `CONFIG_INVALID` without one.

### Portable project backup (`backup create`)

`lv24-seo backup create --out /absolute/existing/dir [--force] [--json]` writes one
`lv24-seo-backup-YYYYMMDDTHHMMSSZ.zip` containing:

* `manifest.json` — format `lv24-seo-backup` v1, engine and schema versions, creation time, every
  payload file with its size and SHA-256, totals, a bounded summary and limitations;
* `seo.toml` — validated config with secret-looking keys masked as `***` and `storage.data_dir`
  reset to `./data`. An invalid config is kept as a redacted raw copy with a warning;
* `data/seo.sqlite3` — a consistent copy made through the SQLite backup API and forced to a
  single-file DELETE journal. Report snapshots live in the `snapshots` table and travel inside it.

`secrets.json`, provider tokens, logs, raw responses and caches are never included. The source is
read-only (`mode=ro`, `PRAGMA query_only`), every payload member is hashed, the archive itself is
hashed (`archive_sha256`), and the zip is written to a temporary file and renamed atomically.
Caps: 4 MiB config and 2 GiB database (plus WAL). `--force` replaces an archive with the same
timestamped name. **Restore is not implemented:** unpack the archive and re-run `init`/`auth set`
as needed. A corrupt source is `STORE_CORRUPT` and is left untouched (no `.corrupt-*` copy).

## Changed

* The CLI now has **20 top-level commands**: 13 groups (`auth`, `config`, `gsc`, `yandex`,
  `sitemap`, `indexnow`, `report`, `schedule`, `export`, `logs`, `tasks`, `backup`, `mcp`) and
  7 commands (`version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`).
* The MCP surface now has **12 tools**: 10 read-class (8 pure read-only, including
  `seo_log_analysis`, plus 2 local-state writers) and 2 gated provider writes that still return
  `WRITE_DISABLED` in this build. Resources and prompts are unchanged (3 and 3).
* `ReportSection` gained optional `total`, `truncated` and `data_age` fields. They are only
  serialised when a section sets them, so existing report payloads keep their shape; the digest is
  the first section that uses them.
* `report weekly` snapshots and `seo_weekly_report` payloads may now include the additive `digest`
  section. A report built without comparable GSC windows or without stored evidence is unchanged.
* Schema version is **3**. Migration 3 is applied on the next open and is additive; no user data is
  rewritten or deleted.
* A concurrent first open now retries the WAL mode switch briefly instead of failing on
  `SQLITE_BUSY` when another process is initialising the same database.

## Verification

Released and verified on 2026-11-16:

* PyPI: `lv24-seo` **0.6.0** wheel and sdist published.
* npm: `@lv24/seo` **0.6.0** published.
* MCP Registry: `io.github.LevapVeeskela/lv24-seo` **0.6.0** published.
* Release workflow: green on the tagged commit.
* Tests: the full suite passed, including the installed-wheel CLI envelope contract, and the
  Ubuntu, Windows and macOS CI jobs passed.
* Desktop: `0.4.0` bundles engine `0.6.0`.

The same gates can be reproduced locally:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python -m lv24_seo.mcp.registry --check
uv build
```

## Known limitations

* Bot detection is user-agent only; it is not a verified-crawler check (see above).
* Log analysis accepts one file of at most 100 MiB. There is no rotation, glob, remote fetch or
  persisted log history, so trends across files must be computed outside the tool.
* `backup create` has no restore command. The manifest is the contract for a manual restore, and
  provider tokens are excluded, so re-authenticate with `lv24-seo auth set` after unpacking.
* Tasks are CLI-only; no MCP tool or report section consumes them yet.
* The digest can only use stored `issues` and `pages` rows. Indexing events are supported by the
  analysis module but are not read from the store (there is no table for them yet).
* Digest evidence is capped at 500 issues and 500 pages per report; when the cap is hit, a
  limitation warns that an unexplained change may be missing evidence.
* The digest explains GSC headline deltas only. Yandex, Metrika, Bing, CrUX and PSI sections are
  not attributed.
