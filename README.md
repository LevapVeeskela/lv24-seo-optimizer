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

**Beta (v0.5.2).** Working today: six provider adapters (GSC, Yandex Webmaster, Yandex
Metrika, Bing/IndexNow, CrUX, PSI), a 30-rule crawler, SQLite history, analysis (deltas, opportunities,
indexing triage, multi-engine fusion, nginx log stats), per-provider `stats`, report builders +
HTML/text/Markdown renderers,
SMTP/Telegram delivery with idempotency, the 17-command CLI, the MCP server (11 tools, 3 resources,
3 prompts) and scheduler generators. Canonical source ids, GSC-only headline deltas, partial
provider isolation and window/daily storage separation are enforced with regression tests.
`0.5.2` is a bugfix release; see [`docs/releases/0.5.2.md`](docs/releases/0.5.2.md).

Still honest gaps before `v1.0`: GSC service-account auth (OAuth token only today),
sitemap submit / Yandex recrawl return an explicit `WRITE_DISABLED` (IndexNow really submits),
and `raw_retention_days` is reserved (no raw archives are written yet). See
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
lv24-seo report weekly --days 7 --out reports/
lv24-seo report diff --markdown                 # week-over-week diff of stored snapshots
lv24-seo stats --days 7 --json                  # read-only per-provider dashboard snapshot
lv24-seo crawl --json                           # issues + sitemap↔crawl coverage diff
lv24-seo export skills --out ./skills           # packaged agent skills from the wheel
lv24-seo mcp serve                              # stdio MCP server
lv24-seo install --client claude|opencode|vscode|cursor
```

> `npx @lv24/seo` requires [uv](https://docs.astral.sh/uv/) (or an installed `lv24-seo` binary): the
> npm package is a launcher, not a second implementation.

## Desktop app

The same engine ships as **LV24 SEO Optimizer Desktop 0.2.2** — a Tauri v2 shell with a dashboard,
weekly report view, configuration editor/diff/validation and a recommendations-to-agent workflow. It
bundles a pinned `lv24-seo` sidecar (`0.5.2` in the current desktop build) and adds no provider logic
of its own.

| Platform | Assets | Auto-update |
|---|---|---|
| Windows 10/11 x64 | NSIS `LV24.SEO.Optimizer_0.2.2_x64-setup.exe` (+ `.exe.sig`), `LV24-SEO-Optimizer_0.2.2_x64-portable.zip` | NSIS only |
| Linux x64 | `LV24.SEO.Optimizer_0.2.2_amd64.AppImage` (+ `.AppImage.sig`), `LV24.SEO.Optimizer_0.2.2_amd64.deb`, `LV24.SEO.Optimizer-0.2.2-1.x86_64.rpm` | AppImage only |
| macOS 13+ arm64/x64 | `LV24.SEO.Optimizer_0.2.2_aarch64.dmg`, `LV24.SEO.Optimizer_0.2.2_x64.dmg`, `.app.tar.gz` (+ `.sig`) | `.app` only |

deb, rpm and the portable ZIP are manual-only (MSI is not produced); `0.1.0` has no updater. The
`0.2.x` releases publish `latest.json` and `SHA256SUMS.txt`.

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

The same data is available as MCP tools (`seo_weekly_report`, `gsc_search_analytics`,
`yandex_query_stats`, `seo_indexing_triage`, …) and as three skills that encode
audit → plan → fix → verify.

## What it will do

| Surface | Examples |
|---|---|
| **Reports** | weekly search performance + delta, indexing triage, striking-distance opportunities, content decay, technical regressions |
| **MCP tools** | 11 tools across GSC, Yandex, crawl, reports and gated writes — see the table below |
| **CLI** | 17 top-level commands (10 groups + 7 commands): `version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`, `auth`, `config`, `gsc`, `yandex`, `sitemap`, `indexnow`, `report`, `schedule`, `export`, `mcp` |
| **Skills** | `seo-weekly`, `seo-indexing-triage`, `seo-technical-audit` — decision trees + tool recipes + output contracts (install from the wheel: `lv24-seo export skills --out ./skills`) |
| **Automation** | cron / systemd timer / Windows Task Scheduler generators and a GitHub Actions recipe |

### MCP tools

Eleven tools, three resources and three prompts. Nine are read-class: seven pure read-only tools that
never change provider or local state, plus `seo_crawl_site` and `seo_weekly_report`, which persist
local snapshots and therefore carry `readOnlyHint: false` (they are not destructive). The two
provider writes require `allow_write=true`, are annotated `destructiveHint: true`, and return
`WRITE_DISABLED` in 0.5.2 because the write adapters are not wired yet.

| Tool | Kind | `readOnlyHint` | `destructiveHint` | What it does |
|---|---|---|---|---|
| `seo_properties` | read | `true` | `false` | Configured providers, targets and token presence |
| `gsc_search_analytics` | read | `true` | `false` | GSC query/page/country/device rows |
| `yandex_query_stats` | read | `true` | `false` | Top Yandex queries for the host |
| `yandex_indexing_state` | read | `true` | `false` | Indexing history, events and problems |
| `seo_crawl_site` | read + local state | `false` | `false` | Bounded crawl; stores pages and issues |
| `seo_weekly_report` | read + local state | `false` | `false` | Weekly digest; stores a report snapshot |
| `seo_report_diff` | read | `true` | `false` | Diff two stored weekly snapshots |
| `seo_indexing_triage` | read | `true` | `false` | Group indexing problems into an action list |
| `seo_opportunities` | read | `true` | `false` | Striking-distance and decay opportunities |
| `seo_recrawl_request` | write | `false` | `true` | Queue Yandex recrawl URLs (gated; `WRITE_DISABLED` in 0.5.2) |
| `seo_sitemap_submit` | write | `false` | `true` | Submit or delete a sitemap (gated; `WRITE_DISABLED` in 0.5.2) |

Resources: `seo://config`, `seo://report/latest`, `seo://docs/tools`. Prompts: `weekly_seo_review`,
`indexing_triage`, `content_opportunities`. The full inputs, outputs and limitations are in
[`docs/mcp.md`](docs/mcp.md); the generated contract is [`schemas/tools.json`](schemas/tools.json).

## Supported sources

| Source | Free tier | Used for | Write operations |
|---|---|---|---|
| Google Search Console | yes (access token; service-account flow planned) | queries, pages, countries, devices; URL Inspection adapter | sitemap submit disabled |
| Yandex Webmaster | yes (OAuth) | queries, indexing history, diagnostics | recrawl disabled |
| Yandex Metrika | yes | sessions and users; filtered reports through adapter; separate `metrika` auth provider (`yandex_metrika_token`) | — |
| Bing Webmaster + IndexNow | yes | queries, crawl issues, instant URL ping | IndexNow submit |
| Chrome UX Report | yes (API key) | origin-level field CWV | — |
| PageSpeed Insights | yes | lab audits per URL | — |
| Your site | — | crawl, robots, sitemap, JSON-LD, hreflang, links | — |
| nginx access logs | — | Python analysis helpers; no log-ingest CLI command yet | — |

## Documents

* [Implementation plan](docs/plans/2026-11-10-implementation-plan.md) — waves, work packages, DoD
* [Architecture](docs/architecture.md) — layers, data model, provider contracts
* [Data sources & limits](docs/data-sources.md) — what each API can and cannot do
* [MCP tool surface](docs/mcp.md) — tool names, inputs, outputs, annotations
* [CLI reference](docs/cli.md) — commands, JSON envelope, exit codes
* [Configuration](docs/config.md) — `seo.toml`, env vars, secret storage
* [Scheduling & delivery](docs/scheduling.md) — cron/systemd/Task Scheduler, SMTP, Telegram
* [Agent skills](docs/skills.md) — skill format and mirrors
* [Release notes 0.5.2](docs/releases/0.5.2.md) — what changed in the current engine release
* [Desktop app](https://github.com/LevapVeeskela/lv24-seo-optimizer-desktop) — Tauri shell, 0.2.2 assets and update matrix
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
