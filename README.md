![LV24 SEO — SEO insights for humans and AI agents](https://raw.githubusercontent.com/LevapVeeskela/lv24-seo-optimizer/main/.github/assets/banner.png)

# lv24-seo-optimizer

<!-- mcp-name: io.github.LevapVeeskela/lv24-seo -->

[![ci](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lv24-seo.svg)](https://pypi.org/project/lv24-seo/)
[![npm](https://img.shields.io/npm/v/@lv24/seo.svg)](https://www.npmjs.com/package/@lv24/seo)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Free-first SEO control plane for humans and AI agents.**

> **Your site's SEO, without a dashboard.** Pull Search Console, Yandex Webmaster and Metrika, Bing,
> CrUX and your own crawl into one history — then get a weekly diff by email, an MCP server and a CLI
> that agents can actually use. No paid platform, no SERP scraping, no vendor account.

| 🚀 Start | 🧠 Use | 🛠 Operate |
| --- | --- | --- |
| [Install](#install) · [See it work](#see-it-work) · [Desktop](#desktop-app) | [CLI](docs/cli.md) · [MCP tools](docs/mcp.md) · [Skills](docs/skills.md) | [Scheduling](docs/scheduling.md) · [Data limits](docs/data-sources.md) · [Security](SECURITY.md) |

One tool that pulls Google Search Console, Yandex Webmaster, Yandex Metrika, Bing Webmaster,
Chrome UX Report and PageSpeed Insights into a single normalized model, crawls your site for
technical SEO regressions, and turns all of it into **scheduled email reports**, **MCP tools**,
**CLI commands** and **agent skills**.

No paid SEO platform required. Everything in the default path has a free tier.

```
┌──────────── data sources (free) ────────────┐      ┌──── surfaces ────┐
│ GSC · Yandex Webmaster · Metrika · Bing ·    │ ───▶ │ CLI (--json)     │
│ CrUX · PSI · your own crawl + nginx logs     │      │ MCP server       │
└──────────────────────────────────────────────┘      │ Skills / AGENTS  │
                                                      │ Email / Telegram │
                                                      │ SQLite history   │
                                                      └──────────────────┘
```

![From provider APIs and bounded crawl to reports, CLI, MCP and opt-in delivery](https://raw.githubusercontent.com/LevapVeeskela/lv24-seo-optimizer/main/docs/assets/workflow.svg)

## Why another SEO tool

* **Free-first fusion.** Existing MCP servers wrap one vendor each and hand raw vendor shapes to
  the model. We join GSC + Bing + Yandex + Metrika per property, and we *say out loud* what the
  APIs cannot give you (Yandex has no query×page join, GSC caps at 50k rows/day, data lags 2–3 days).
  Bounded provider responses expose coverage, freshness and limitations; unknown totals stay unknown.
* **Comparable periods.** The headline compares current and previous GSC windows. Yandex,
  Metrika, Bing, CrUX field data and PSI lab data keep separate sections. Opt-in delivery
  applies the configured significance thresholds and skips content already sent for the period.
* **Agent-native.** A small, stable MCP tool surface (no provider writes by default), a JSON envelope on every
  CLI command, and skills that encode *audit → plan → fix → verify* against a real repository.
* **Zero spend.** Public-repo CI, SQLite storage, SMTP/Telegram delivery, no SaaS in the default path.
  Paid-provider integrations are research/planned work, not dependencies or shipped adapters.

## Status

**Beta (v0.12.2).** Working today: six
provider adapters (GSC, Yandex Webmaster, Yandex Metrika, Bing/IndexNow, CrUX, PSI), a 30-rule
crawler, SQLite history, analysis (deltas, opportunities, indexing triage, multi-engine fusion,
nginx log analysis with UA-only bot triage, CWV by template), per-provider `stats`, report builders
+ HTML/text/Markdown renderers with an attributed change digest and a Core Web Vitals section, the
finding/task lifecycle, portable backup bundles with non-destructive and two-phase restore, a
read-only multi-site portfolio scan, SMTP/Telegram delivery with idempotency, Bing page/traffic/
crawl/URL diagnostics, Bing sitemap and URL submission, one-click IndexNow from the project
sitemap, GSC URL inspection + sitemap reads + filtered search analytics, Yandex events/sitemaps/
history/recrawl-queue reads, organic Metrika pages/goals/dimensions, quota-aware indexing automation
(status/run/history with Bing, Yandex, IndexNow and GSC checks), the 25-command CLI,
the MCP server
(12 tools, 3 resources, 3 prompts) and a plan-by-default scheduler lifecycle. Canonical source ids,
GSC-only headline deltas, partial provider isolation and window/daily storage separation are
enforced with regression tests. `0.12.2` is the current release: quota-aware indexing automation
across Bing, Yandex, IndexNow and GSC, with the indexing client-lifecycle and Yandex 5xx fixes
(PyPI, npm and the MCP Registry, 2026-11-17), and desktop `0.9.0` bundles engine `0.11.0`. See
[`docs/releases/0.12.2.md`](docs/releases/0.12.2.md),
[`docs/releases/0.12.1.md`](docs/releases/0.12.1.md),
[`docs/releases/0.12.0.md`](docs/releases/0.12.0.md),
[`docs/releases/0.11.0.md`](docs/releases/0.11.0.md),
[`docs/releases/0.10.0.md`](docs/releases/0.10.0.md),
[`docs/releases/0.9.0.md`](docs/releases/0.9.0.md),
[`docs/releases/0.8.0.md`](docs/releases/0.8.0.md) and
[`docs/releases/0.7.0.md`](docs/releases/0.7.0.md).

Still honest gaps before `v1.0`: GSC sitemap submit and Bing sitemap delete return an explicit
`WRITE_DISABLED` (Bing sitemap/URL submit, IndexNow and Yandex recrawl really write), and
`raw_retention_days` is reserved (no raw archives are written yet). GSC supports both a plain OAuth
access token and a service-account JSON key. See
[`docs/plans/2026-11-10-implementation-plan.md`](docs/plans/2026-11-10-implementation-plan.md) for
the wave status.

## Install

```bash
# Python (recommended)
uvx --from lv24-seo lv24-seo doctor
pipx install "lv24-seo[secrets]"       # or: uv tool install "lv24-seo[secrets]"

# Node users: thin launcher over the same CLI
npx @lv24/seo doctor --json
```

```bash
lv24-seo init --property https://example.com   # writes seo.toml + data/
lv24-seo auth set --provider gsc --stdin       # token to the OS keychain (needs lv24-seo[secrets])
lv24-seo auth set --provider metrika --stdin   # Metrika has its own auth provider and token
lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 --dimensions query,page
lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 --type image --data-state all --filter country:equals:BLR
lv24-seo gsc inspect --url https://lv24.by/ --language ru-RU --json   # one URL, last-crawl report
lv24-seo gsc sitemaps --sitemap-index https://lv24.by/sitemap-seo.xml --json
lv24-seo gsc sitemap --path https://lv24.by/sitemap.xml --json
lv24-seo yandex indexing --date-from 2026-10-01 --date-to 2026-10-31 --json  # history + appeared/removed
lv24-seo yandex sitemaps --json                 # sitemap ids, URLs, counts, errors (read-only)
lv24-seo yandex history --kind sqi --json       # SQI / in-search / queries / query series
lv24-seo yandex queue --json                    # recrawl tasks + provider total (read-only)
lv24-seo yandex recrawl --url https://example.com/page --allow-write  # plan; confirm to POST
lv24-seo indexing status --live --json          # indexed/submitted/queued per provider + quotas
lv24-seo indexing run --from-sitemap --json     # plan; add --allow-write to submit within quotas
lv24-seo report weekly --days 7 --out reports/
lv24-seo report diff --markdown                 # week-over-week diff of stored snapshots
lv24-seo stats --days 7 --json                  # read-only per-provider dashboard snapshot
lv24-seo cwv collect --url https://example.com/ --device phone --source crux --json
lv24-seo cwv audits --url https://example.com/ --category performance --json
lv24-seo cwv history --origin https://example.com --weeks 12 --json
lv24-seo metrika pages --days 7 --json           # organic landing pages + engagement
lv24-seo metrika goals --dimension page --json   # configured goals: reaches, users, CR
lv24-seo metrika bytime --group day --json       # organic visits/users per day
lv24-seo crawl --json                           # issues + sitemap↔crawl coverage diff
lv24-seo logs analyze --file /var/log/nginx/access.log --json   # bot rollups + crawl waste (read-only)
lv24-seo tasks list --json                      # local finding lifecycle (open → verified)
lv24-seo portfolio list --root "$HOME/sites" --json   # read-only roll-up over sibling projects
mkdir -p backups
lv24-seo backup create --out "$PWD/backups" --json   # portable zip: config + SQLite history
lv24-seo backup restore --file "$PWD/backups/lv24-seo-backup-<stamp>.zip" \
  --to "$PWD/restored" --json                    # new dir; existing files untouched, no token
lv24-seo backup restore --file "$PWD/backups/lv24-seo-backup-<stamp>.zip" \
  --config "$PWD/seo.toml" --json                # plan; add --allow-write --confirm TOKEN to apply
lv24-seo export skills --out ./skills           # packaged agent skills from the wheel
lv24-seo mcp serve                              # stdio MCP server
lv24-seo install --client claude|opencode|vscode|cursor
```

> `npx @lv24/seo` requires [uv](https://docs.astral.sh/uv/) (or an installed `lv24-seo` binary): the
> npm package is a launcher, not a second implementation.

## Desktop app

The same engine ships as **LV24 SEO Optimizer Desktop 0.9.0** — a Tauri v2 shell with a dashboard,
weekly report view, configuration editor/diff/validation, a recommendations-to-agent workflow and a
read-only Diagnostics screen. It bundles a pinned `lv24-seo` sidecar (`0.11.0` in the current desktop
build), requires engine `>= 0.11.0`, and adds no provider logic of its own.

| Platform | Assets | Auto-update |
|---|---|---|
| Windows 10/11 x64 | NSIS `LV24.SEO.Optimizer_0.9.0_x64-setup.exe` (+ `.exe.sig`), `LV24-SEO-Optimizer_0.9.0_x64-portable.zip` | NSIS only |
| Linux x64 | `LV24.SEO.Optimizer_0.9.0_amd64.AppImage` (+ `.AppImage.sig`), `LV24.SEO.Optimizer_0.9.0_amd64.deb`, `LV24.SEO.Optimizer-0.9.0-1.x86_64.rpm` | AppImage only |
| macOS 13+ arm64/x64 | `LV24.SEO.Optimizer_0.9.0_aarch64.dmg`, `LV24.SEO.Optimizer_0.9.0_x64.dmg`, `.app.tar.gz` (+ `.sig`) | `.app` only |

deb, rpm and the portable ZIP are manual-only (MSI is not produced); `0.1.0` has no updater. Releases
publish `latest.json` and `SHA256SUMS.txt`.

**One secret store.** The desktop and the CLI use the same OS keychain entry per provider (and per
delivery secret): service `lv24-seo`, usernames `gsc_token`, `yandex_webmaster_token`,
`yandex_metrika_token`, `bing_api_key`, `crux_api_key`, `psi_api_key`, `smtp_password`,
`telegram_bot_token`. A token entered with `lv24-seo auth set` in a terminal appears in the desktop's
`auth status`, and a token entered in the desktop is visible to the CLI. `LV24_SEO_*` environment
variables are inherited by the desktop's CLI runner. The opt-in `<data-dir>/secrets.json` fallback
(0600 on POSIX, ACLs on Windows) is shared only when both surfaces use the same workspace/data
directory.

Desktop repo: <https://github.com/LevapVeeskela/lv24-seo-optimizer-desktop> · design notes:
[`docs/plans/2026-11-14-seo-desktop.md`](docs/plans/2026-11-14-seo-desktop.md).

## See it work

```text
$ lv24-seo doctor

[OK  ] python: 3.13.12
[OK  ] platform: win32
[OK  ] config: C:\work\seo.toml
[OK  ] cwd_writable: C:\work
[FAIL] tokens: none present; run `lv24-seo auth status`
```

`doctor` checks the runtime, config, data dir, working-directory writability and token presence only
— no network calls.

```text
$ lv24-seo report weekly --days 7 --json --out reports/

# Writes report.json and report.html; the JSON envelope carries:
# meta.sources          successful providers
# meta.sources_failed   isolated source failures
# meta.complete         false for missing, capped or sampled inputs
# meta.limitations      caveats to read before interpreting the numbers
# No configured tokens: an explicit no-data report, with complete=false.
```

```text
$ lv24-seo schedule install --kind weekly --config ./seo.toml

# dry_run: true, installed: false — prints the cron block / systemd units / task XML only.
$ lv24-seo schedule install --kind weekly --config ./seo.toml --allow-write
# dry_run: false, installed: true — writes the managed cron block / user units / task.
$ lv24-seo schedule status --kind weekly --config ./seo.toml
# state: installed, matches: true (read-only)
```

The same data is available as MCP tools (`seo_weekly_report`, `gsc_search_analytics`,
`yandex_query_stats`, `seo_indexing_triage`, …) and as three skills that encode
audit → plan → fix → verify.

## What it will do

| Surface | Examples |
|---|---|
| **Reports** | weekly search performance + delta, indexing triage, striking-distance opportunities, content decay, technical regressions, and a Core Web Vitals section fed by `cwv collect` (CrUX URL+device p75, PSI lab); `cwv audits` returns bounded Lighthouse diagnostics and `cwv history` weekly CrUX p75 periods |
| **MCP tools** | 12 tools across GSC, Yandex, crawl, reports, logs and gated writes — see the table below |
| **CLI** | 25 top-level commands (18 groups + 7 commands): `version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`, `auth`, `config`, `gsc`, `yandex`, `metrika`, `sitemap`, `indexnow`, `bing`, `indexing`, `report`, `cwv`, `schedule`, `export`, `logs`, `tasks`, `backup`, `portfolio`, `mcp`; read commands accept `--strict` to exit `5` on truncated data |
| **Skills** | `seo-weekly`, `seo-indexing-triage`, `seo-technical-audit` — decision trees + tool recipes + output contracts (install from the wheel: `lv24-seo export skills --out ./skills`) |
| **Automation** | cron / systemd timer / Windows Task Scheduler generators with plan-first `schedule install` (`--allow-write` to apply), read-only `schedule status`, exact and idempotent `schedule uninstall`, a daily `indexing run` job, plus a GitHub Actions recipe |

### MCP tools

Twelve tools, three resources and three prompts. Ten are read-class: seven pure read-only tools that
never change provider or local state, plus `seo_crawl_site`, `seo_weekly_report` and
`yandex_indexing_state`, which persist local snapshots or indexing events and therefore carry
`readOnlyHint: false` (they are not destructive). The two provider writes require `allow_write=true`,
are annotated `destructiveHint: true`, and use the same two-phase `plan_hash` / `confirm_token` flow
and `LV24_SEO_WRITE_SECRET` as the CLI; Bing sitemap submit and Yandex recrawl are wired, while GSC
submit and Bing delete return `WRITE_DISABLED` and nothing is sent.

| Tool | Kind | `readOnlyHint` | `destructiveHint` | What it does |
|---|---|---|---|---|
| `seo_properties` | read | `true` | `false` | Configured providers, targets and token presence |
| `gsc_search_analytics` | read | `true` | `false` | GSC query/page/country/device rows |
| `yandex_query_stats` | read | `true` | `false` | Top Yandex queries for the host |
| `yandex_indexing_state` | read + local state | `false` | `false` | Indexing history, events and problems; persists provider events when a store exists |
| `seo_crawl_site` | read + local state | `false` | `false` | Bounded crawl; stores pages and issues (off-loop, 600 s budget) |
| `seo_weekly_report` | read + local state | `false` | `false` | Weekly digest; stores a report snapshot |
| `seo_report_diff` | read | `true` | `false` | Diff two stored weekly snapshots |
| `seo_log_analysis` | read | `true` | `false` | One local nginx JSON log: bot rollups and crawl-waste share (UA-only triage) |
| `seo_indexing_triage` | read | `true` | `false` | Group indexing problems into an action list |
| `seo_opportunities` | read | `true` | `false` | Striking-distance and decay opportunities |
| `seo_recrawl_request` | write | `false` | `true` | Queue Yandex recrawl URLs (two-phase gate; one POST per URL, capped to the remaining daily quota) |
| `seo_sitemap_submit` | write | `false` | `true` | Submit a sitemap to Bing or GSC (two-phase gate; Bing submit wired, GSC/delete `WRITE_DISABLED`) |

Resources: `seo://config`, `seo://report/latest`, `seo://docs/tools`. Prompts: `weekly_seo_review`,
`indexing_triage`, `content_opportunities`. The full inputs, outputs and limitations are in
[`docs/mcp.md`](docs/mcp.md); the generated contract is [`schemas/tools.json`](schemas/tools.json).

## Supported sources

| Source | Free tier | Used for | Write operations |
|---|---|---|---|
| Google Search Console | yes (OAuth access token or service-account JSON key) | queries, pages, countries, devices; filtered search analytics; URL Inspection (one URL per call); sitemap list/get with contents | sitemap submit/delete disabled (needs full `webmasters` scope) |
| Yandex Webmaster | yes (OAuth) | queries, indexing history/events, samples, sitemaps, SQI/in-search/query history, recrawl queue | recrawl (quota-capped, one URL per request) |
| Yandex Metrika | yes | organic and all-traffic sessions/users; organic pages, phrases, devices, countries, engagement, configured goals and bytime through `lv24-seo metrika`; separate `metrika` auth provider (`yandex_metrika_token`) | — |
| Bing Webmaster + IndexNow | yes | queries, pages, daily traffic, six-month crawl stats, query/page joins, URL index details, directory children, site roles, account sites, crawl issues (decoded bitmask), remaining URL quota, sitemap feeds, sitemap submit, URL submit, one-click IndexNow from the sitemap | sitemap submit + URL submit + IndexNow submit |
| Chrome UX Report | yes (API key) | URL+device field p75, weekly p75 history, collection period and URL-normalization warnings | — |
| PageSpeed Insights | yes | lab category scores and bounded Lighthouse audits per URL, with runtime-error/redirect warnings | — |
| Your site | — | crawl, robots, sitemap, JSON-LD, hreflang, links | — |
| nginx access logs | — | `logs analyze` + `seo_log_analysis`: bot/day rollups and crawl-waste share (UA-only triage, 100 MiB cap); nothing stored | — |

## Documents

* [Implementation plan](docs/plans/2026-11-10-implementation-plan.md) — waves, work packages, DoD
* [Architecture](docs/architecture.md) — layers, data model, provider contracts
* [Data sources & limits](docs/data-sources.md) — what each API can and cannot do
* [MCP tool surface](docs/mcp.md) — tool names, inputs, outputs, annotations
* [CLI reference](docs/cli.md) — commands, JSON envelope, exit codes
* [Configuration](docs/config.md) — `seo.toml`, env vars, secret storage
* [Scheduling & delivery](docs/scheduling.md) — cron/systemd/Task Scheduler, SMTP, Telegram
* [Agent skills](docs/skills.md) — skill format and mirrors
* [Release notes 0.12.2](docs/releases/0.12.2.md) — Yandex recrawl 5xx mapping
* [Release notes 0.12.1](docs/releases/0.12.1.md) — fix the indexing client lifecycle (Bing/Yandex `internal_error`)
* [Release notes 0.12.0](docs/releases/0.12.0.md) — quota-aware indexing automation: status/run/history, Bing/Yandex/IndexNow/GSC quotas, honest index counts
* [Release notes 0.11.0](docs/releases/0.11.0.md) — full-platform read depth: GSC inspection/sitemaps, Yandex events/sitemaps/recrawl, Bing page/crawl/URL diagnostics, organic Metrika, CWV audits/history
* [Release notes 0.10.0](docs/releases/0.10.0.md) — Bing URL submission, remaining quota, sitemap feeds, crawl-issue bitmask
* [Release notes 0.9.0](docs/releases/0.9.0.md) — Bing sitemap submit, one-click IndexNow
* [Release notes 0.8.0](docs/releases/0.8.0.md) — CWV writer, Yandex indicators, safe restore, schedule lifecycle
* [Release notes 0.7.0](docs/releases/0.7.0.md) — coverage release: portfolio, restore, indexing events, CWV
* [Release notes 0.6.0](docs/releases/0.6.0.md) — insights release: logs, digest, tasks, backup
* [Release notes 0.5.3](docs/releases/0.5.3.md) — P0 hardening release
* [Desktop app](https://github.com/LevapVeeskela/lv24-seo-optimizer-desktop) — Tauri shell, 0.8.0 assets and update matrix
* [Desktop plan](docs/plans/2026-11-14-seo-desktop.md) — shell scope and milestones
* [Security](SECURITY.md) — token handling, read-only defaults, write gating
* [Research 2026](docs/research/2026-11-landscape.md) — API limits, existing MCP servers, tool landscape

## For AI agents

Read [`AGENTS.md`](AGENTS.md) first. It is the canonical contract: repo layout, how to run things,
what is read-only, how to propose a change. Machine-readable helpers:

* `schemas/tools.json` — generated tool schemas (works without MCP)
* `llms.txt` — doc map for LLM crawlers
* `skills/*/SKILL.md` — portable skills (Claude Code, OpenCode, Codex)
* MCP Registry: [`io.github.LevapVeeskela/lv24-seo`](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.LevapVeeskela%2Flv24-seo)

## License

Apache-2.0. See [LICENSE](LICENSE) and [TRADEMARKS.md](TRADEMARKS.md).
Data pulled from Google/Yandex/Bing belongs to the property owner — this project never resells it.
