# CLI reference

The CLI has 21 top-level commands: 14 groups (`auth`, `config`, `gsc`, `yandex`, `sitemap`,
`indexnow`, `report`, `schedule`, `export`, `logs`, `tasks`, `backup`, `portfolio`, `mcp`) and 7
commands (`version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`).

Every read/report command supports `--json` (machine output) and a stable exit code; commands that
use the workspace also accept `--config PATH`.
Output envelope (`meta.version` is the package version, e.g. `0.1.2`):

```json
{ "ok": true, "data": {}, "meta": { "version": "0.1.2", "duration_ms": 812 }, "warnings": [] }
```

Exit codes: `0` ok · `2` usage/config · `3` auth · `4` quota/upstream · `5` strict truncation · `1` unexpected.

`--json` makes the command print exactly one envelope. Without it you get a short human summary on
stdout; no ANSI colours.

`--strict` is available on the read commands (`stats`, `gsc query`, `yandex queries`,
`yandex indexing`, `crawl`, `report weekly`, `report diff`). It always sets `meta.strict: true`;
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

## Data

```bash
lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 [--dimensions query,page] [--limit 100] [--strict]
lv24-seo yandex queries [--limit 100] [--strict] [--json]
lv24-seo yandex indexing [--strict] [--json]
lv24-seo crawl [--start URL] [--max-urls 500] [--strict] [--json]
```

GSC needs explicit `--start/--end` (there is no `--days` shorthand and no `--property`
override — the property comes from `seo.toml`). A GSC page that exactly fills the row cap is reported
as `truncated: true` with `rows_cap_50000` (default cap) or a caller-cap warning: a full page is not
proof that no row exists behind the cap, so an exact-cap result is never claimed complete. A
truncated GSC result also omits `total` (`null`) — an exact count next to `truncated: true` would
read as a complete answer. `data_age_days` is floored at the documented 3-day lag.

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
- CrUX adds `units` (`metric -> ms|score`) for p75 field metrics. PSI reports lab
  `performance`/`seo` scores (0..1) from the first configured URL plus field rows when present; with
  several URLs it adds the limitation "lab data, first URL only" and checks at most five URLs.
- `rows` is the number of provider rows returned, not a total. `coverage.complete` is true only
  when every configured service is `ok`, has rows, is not truncated, and none is missing or failed;
  it is `false` when no provider is configured.
- A provider failure is isolated: other services still return their data and the envelope stays
  `ok: true` with `coverage.failed` incremented.

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

When the store holds CrUX/PSI **field** rows for the compared windows, the weekly report also adds a
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

## Scheduling

```bash
lv24-seo schedule print --cron "0 6 * * 1" [--target cron|systemd|windows] [--json]
lv24-seo schedule install --kind weekly|daily [--cron "0 6 * * 1"] [--print] [--json]
```

`print` renders the artefact without touching the host; `install` writes the cron block / systemd user
units / Task Scheduler XML. Generated jobs call `report weekly --send --json`, so a scheduled run
delivers the report. The job embeds the **absolute** `--config` path and uses the config file's parent
as its working directory, so a scheduler that starts in another directory still reads the same config
and data directory.

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
  a dry run first, then `--allow-write` plus the HMAC `confirm_token` from the plan (ADR-0003).
  `backup restore --config` uses the same two-phase shape for a local destructive write: the
  plan's token is bound to the archive and target state, expires in 5 minutes and is not
  replayable. `backup restore --to` is not gated because it only creates a new directory that
  must not already exist; it never modifies the current workspace. IndexNow
  actually submits; GSC sitemap submit and Yandex recrawl refuse with `WRITE_DISABLED`
  when the provider path is not wired, instead of pretending. With an initialised workspace the
  apply uses the SQLite idempotency ledger, so replaying the same token returns
  `status: skipped` / `reason: duplicate` and the provider is called once. The ledger lives in the
  workspace selected by `--config`, so `--config /srv/site/seo.toml` opens `/srv/site/data` even
  when the shell is elsewhere; without a workspace the command still applies but warns that replay
  protection is off for the token's five-minute lifetime. A dry run opens no ledger and creates no
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

- `schedule install --target windows` writes a UTF-16 Task Scheduler XML (what `schtasks` expects).
- Paths are handled with `pathlib`; no POSIX-only assumptions in user-facing commands.
