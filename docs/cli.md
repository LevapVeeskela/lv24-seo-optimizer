# CLI reference

Every command supports `--json` (machine output), `--config PATH` and a stable exit code.
Output envelope (`meta.version` is the package version, e.g. `0.1.2`):

```json
{ "ok": true, "data": {}, "meta": { "version": "0.1.2", "duration_ms": 812 }, "warnings": [] }
```

Exit codes: `0` ok · `2` usage/config · `3` auth · `4` quota/upstream · `5` partial · `1` unexpected.

`--json` makes the command print exactly one envelope. Without it you get a short human summary on
stdout; no ANSI colours.

## Setup

```bash
lv24-seo version [--json]
lv24-seo doctor [--json]                      # offline: python, config, data dir, token presence
lv24-seo init [--property https://example.com] [--data-dir ./data] [--force]
lv24-seo config show [--json]                 # redacted effective config
lv24-seo properties [--json]                  # provider -> target + token present|missing
```

`init` refuses to overwrite an existing `seo.toml` unless `--force` is passed.

`doctor --json` reports `meta.data_dir` and `meta.data_dir_source` (`config` when the effective
directory is absolute, `working directory` when it is relative) and adds a warning when a relative
directory is ambiguous next to a config file. A relative directory does not fail the check; see
[configuration](config.md#data-directory). `doctor` makes no network calls.

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
lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 [--dimensions query,page] [--limit 100]
lv24-seo yandex queries [--limit 100] [--json]
lv24-seo yandex indexing [--json]
lv24-seo crawl [--start URL] [--max-urls 500] [--json]
```

GSC needs explicit `--start/--end` (there is no `--days` shorthand and no `--property`
override — the property comes from `seo.toml`). A GSC page that exactly fills the row cap is reported
as `truncated: true` with `rows_cap_50000` (default cap) or a caller-cap warning: a full page is not
proof that no row exists behind the cap, so an exact-cap result is never claimed complete.

`crawl` respects `robots.txt`, follows same-host links breadth-first, runs all rules and stores
pages/issues in SQLite when a store is configured. A sitemap that fails to load reports only the
exception type in warnings and `limitations[]` — never the URL or query string, which can carry a
token.

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

## Dashboard stats

```bash
lv24-seo stats [--days 7] [--lag-days 2] [--json]
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
lv24-seo report weekly [--days 7] [--lag-days 2] [--send] [--out reports/] [--json]
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

```bash
lv24-seo report diff [--base START..END] [--head START..END] [--markdown] [--json]
```

Diffs two **stored** weekly snapshots (default: the two newest periods) without calling any
provider: headline metric changes, issue counts per severity, opportunities added/resolved and
limitations added/lifted. A metric present in only one snapshot is listed as unknown (never
zero), and `comparable`/`meta.complete` is `false` when either snapshot had no headline
comparison or the period lengths differ. `--markdown` prints only GitHub-flavoured Markdown for a
PR comment or CI summary; warnings go to stderr, so stdout stays valid Markdown. Output is
deterministic so it diffs cleanly between runs. Needs `init` and at least two `report weekly` runs
for different periods.

Planned for the next wave: `report indexing`, `report opportunities`, `report history`
(see `docs/plans/2026-11-10-implementation-plan.md`, WP5.5).

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
  IndexNow actually submits; GSC sitemap submit and Yandex recrawl refuse with `WRITE_DISABLED`
  when the provider path is not wired, instead of pretending. With an initialised workspace the
  apply uses the SQLite idempotency ledger, so replaying the same token returns
  `status: skipped` / `reason: duplicate` and the provider is called once. Without a workspace the
  command still applies but warns that replay protection is off for the token's five-minute
  lifetime; a dry run opens no ledger and creates no database.
- Logging is opt-in: `LV24_SEO_LOG_LEVEL=INFO` (or `DEBUG`, `WARNING`, …) makes the CLI and
  `lv24-seo mcp serve` install the redacting handler. Unset means the process leaves logging to its
  caller; importing the library or calling `create_server()` never replaces root handlers. Output is
  one JSON line per record on a non-TTY stderr and a plain line on a terminal; the MCP transport logs
  to stderr only.
- No command prints a token, cookie or full `Authorization` header.

## Windows notes

- `schedule install --target windows` writes a UTF-16 Task Scheduler XML (what `schtasks` expects).
- Paths are handled with `pathlib`; no POSIX-only assumptions in user-facing commands.
