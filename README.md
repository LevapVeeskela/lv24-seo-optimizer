# lv24-seo-optimizer

[![ci](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Free-first SEO control plane for humans and AI agents.**

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

## Why another SEO tool

* **Free-first fusion.** Existing MCP servers wrap one vendor each and hand raw vendor shapes to
  the model. We join GSC + Bing + Yandex + Metrika per property, and we *say out loud* what the
  APIs cannot give you (Yandex has no query×page join, GSC caps at 50k rows/day, data lags 2–3 days).
  Every response carries `truncated`, `total`, `data_age` and `limitations[]`.
* **Diffs, not dashboards.** Reports compare against the last stored snapshot and only email you when
  something material changed (traffic drop, new indexing errors, sitemap regressions, TLS expiry).
* **Agent-native.** A small, stable MCP tool surface (read-only by default), a JSON envelope on every
  CLI command, and skills that encode *audit → plan → fix → verify* against a real repository.
* **Zero spend.** Public-repo CI, SQLite storage, SMTP/Telegram delivery, no SaaS in the default path.
  Paid providers (Ahrefs, Semrush, DataForSEO, SerpApi) are optional adapters that use *your* key.

## Status

**Pre-alpha — design and scaffolding.** The implementation plan lives in
[`docs/plans/2026-11-10-implementation-plan.md`](docs/plans/2026-11-10-implementation-plan.md).
Landscape research (2026) is in [`docs/research/`](docs/research/).

## Install (planned, v0.1)

```bash
uvx --from lv24-seo lv24-seo doctor        # checks tokens, DNS, TLS, sitemap
uvx --from lv24-seo lv24-seo init          # writes seo.toml + data/ layout
uvx --from lv24-seo lv24-seo auth login    # OAuth device/loopback flow
uvx --from lv24-seo lv24-seo mcp serve     # stdio MCP server for Claude/OpenCode/Cursor
```

```bash
# register with an agent host
lv24-seo install --client claude|opencode|vscode|cursor
```

## What it will do

| Surface | Examples |
|---|---|
| **Reports** | weekly search performance + delta, indexing triage, striking-distance opportunities, content decay, technical regressions |
| **MCP tools** | `gsc_search_analytics`, `yandex_query_stats`, `yandex_indexing_state`, `crawl_site`, `seo_weekly_report`, `seo_indexing_triage`, `seo_opportunities`, `seo_recrawl_request` (gated) |
| **CLI** | `init`, `doctor`, `auth`, `gsc`, `yandex`, `crawl`, `audit`, `report`, `schedule`, `mcp serve` |
| **Skills** | `seo-weekly`, `seo-indexing-triage`, `seo-technical-audit` — decision trees + tool recipes + output contracts |
| **Automation** | cron / systemd timer / Windows Task Scheduler generators, GitHub Actions recipe, nginx log ingest |

## Supported sources

| Source | Free tier | Used for | Write operations |
|---|---|---|---|
| Google Search Console | yes (OAuth or service account) | queries, pages, countries, devices, URL inspection, sitemaps | sitemap submit (gated) |
| Yandex Webmaster | yes (OAuth) | queries, indexing history, diagnostics, sitemaps, recrawl | recrawl queue (quota-gated) |
| Yandex Metrika | yes | organic sessions, landing pages, goals | — |
| Bing Webmaster + IndexNow | yes | queries, crawl issues, instant URL ping | IndexNow submit |
| Chrome UX Report | yes (API key) | origin-level field CWV | — |
| PageSpeed Insights | yes | lab audits per URL | — |
| Your site | — | crawl, robots, sitemap, JSON-LD, hreflang, links | — |
| nginx access logs | — | bot crawl budget, 5xx to bots | — |

## Documents

* [Implementation plan](docs/plans/2026-11-10-implementation-plan.md) — waves, work packages, DoD
* [Architecture](docs/architecture.md) — layers, data model, provider contracts
* [Data sources & limits](docs/data-sources.md) — what each API can and cannot do
* [MCP tool surface](docs/mcp.md) — tool names, inputs, outputs, annotations
* [CLI reference](docs/cli.md) — commands, JSON envelope, exit codes
* [Configuration](docs/config.md) — `seo.toml`, env vars, secret storage
* [Scheduling & delivery](docs/scheduling.md) — cron/systemd/Task Scheduler, SMTP, Telegram
* [Agent skills](docs/skills.md) — skill format and mirrors
* [Security](SECURITY.md) — token handling, read-only defaults, write gating
* [Research 2026](docs/research/2026-11-landscape.md) — API limits, existing MCP servers, tool landscape

## For AI agents

Read [`AGENTS.md`](AGENTS.md) first. It is the canonical contract: repo layout, how to run things,
what is read-only, how to propose a change. Machine-readable helpers:

* `schemas/tools.json` — generated tool schemas (works without MCP)
* `llms.txt` — doc map for LLM crawlers
* `skills/*/SKILL.md` — portable skills (Claude Code, OpenCode, Codex)

## License

Apache-2.0. See [LICENSE](LICENSE) and [TRADEMARKS.md](TRADEMARKS.md).
Data pulled from Google/Yandex/Bing belongs to the property owner — this project never resells it.
