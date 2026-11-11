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

## Authentication

```bash
lv24-seo auth status [--json]                                  # presence + source, never values
lv24-seo auth set --provider gsc|yandex|bing|crux|psi|smtp|telegram [--stdin]
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
override — the property comes from `seo.toml`). `crawl` respects `robots.txt`, follows
same-host links breadth-first, runs all rules and stores pages/issues in SQLite when a store is
configured.

## Reports

```bash
lv24-seo report weekly [--days 7] [--lag-days 2] [--send] [--out reports/] [--json]
```

- `--days` current window, `--lag-days` provider lag (GSC 2–3 days).
- `--send` delivers through the channels enabled in `seo.toml` (email/Telegram). Without it the
  report is only built and stored as a snapshot.
- `--out DIR` writes `report.json` and `report.html`.
- Reports are idempotent: a second run for the same period and content is skipped
  (`already_sent`), and `only_on_change` skips a send when nothing material changed.

Planned for the next wave: `report indexing`, `report opportunities`, `report history`
(see `docs/plans/2026-11-10-implementation-plan.md`, WP5.5).

## Scheduling

```bash
lv24-seo schedule print --cron "0 6 * * 1" [--target cron|systemd|windows] [--json]
lv24-seo schedule install --kind weekly|daily [--cron "0 6 * * 1"] [--print] [--json]
```

`print` renders the artefact without touching the host; `install` writes the cron block / systemd user
units / Task Scheduler XML. Generated jobs call `report weekly --send --json`, so a scheduled run
delivers the report.

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
  when the provider path is not wired, instead of pretending.
- No command prints a token, cookie or full `Authorization` header.

## Windows notes

- `schedule install --target windows` writes a UTF-16 Task Scheduler XML (what `schtasks` expects).
- Paths are handled with `pathlib`; no POSIX-only assumptions in user-facing commands.
