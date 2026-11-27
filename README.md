![LV24 SEO — SEO insights for humans and AI agents](https://raw.githubusercontent.com/LevapVeeskela/lv24-seo-optimizer/main/.github/assets/banner.png)

# lv24-seo-optimizer

<!-- mcp-name: io.github.LevapVeeskela/lv24-seo -->

[![ci](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lv24-seo.svg)](https://pypi.org/project/lv24-seo/)
[![npm](https://img.shields.io/npm/v/@lv24/seo.svg)](https://www.npmjs.com/package/@lv24/seo)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**Free-first SEO control plane for humans and AI agents.** Pull Search Console, Yandex Webmaster and
Metrika, Bing, CrUX and your own crawl into one local history — then read it as a weekly diff, a
25-command CLI or a small MCP server. No paid platform, no SERP scraping, no vendor account.

**Docs:** [Architecture](docs/ARCHITECTURE.md) · [Agent entry](AGENTS.md) · [seo.lv24.by](https://seo.lv24.by/)

## What it is / What it is not

- **Is:** a local Python engine that normalizes six provider APIs into one `ProviderResult` envelope,
  runs a 34-rule crawl, stores both in SQLite and renders comparable weekly reports.
- **Is:** agent-native — every CLI command accepts `--json`, and the MCP server exposes 12 stable
  tools, 3 resources and 3 prompts over the same services and the same history.
- **Is not:** an LLM or a hosted analytics service. There is no server component, no telemetry and
  no provider data upload; tokens stay in the OS keychain or a local `0600` file.
- **Is not:** a rank tracker built on scraping. Rank data comes from provider APIs, with the caps
  and limitations they report.

## Status

| Item | Value |
|---|---|
| Version | `0.15.4` (2026-11-20) |
| Ship state | Beta; published on PyPI, npm and the MCP Registry (2026-11-20) |
| Visibility | public, Apache-2.0 |
| Repo | <https://github.com/LevapVeeskela/lv24-seo-optimizer> |
| Prod | none — local tool; documentation at <https://seo.lv24.by/> |
| Desktop | `0.9.9`, bundles engine `0.15.3` |

## At a glance

| Stack | Entry point | Ports | Data | Deploy |
|---|---|---|---|---|
| Python 3.11+ (Typer, httpx, pydantic) | `lv24-seo` / `lv24-seo mcp serve` | none (stdio MCP) | SQLite `data/seo.sqlite3`, schema v5 | PyPI, npm wrapper, MCP Registry; host scheduler |

## Quick start

```bash
git clone https://github.com/LevapVeeskela/lv24-seo-optimizer && cd lv24-seo-optimizer
uv sync                                              # dev environment from the lockfile
uv run lv24-seo doctor                               # offline self-check, no network calls
uv run lv24-seo init --property https://example.com  # writes seo.toml + data/
uv run lv24-seo auth set --provider gsc --stdin      # token goes to the OS keychain
uv run lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 --dimensions query,page
uv run lv24-seo report weekly --days 7 --out reports/
uv run lv24-seo mcp serve                            # stdio MCP server
```

Run the published package without cloning:

```bash
uvx --from lv24-seo lv24-seo doctor
npx @lv24/seo doctor --json       # thin Node launcher; needs uv or an installed lv24-seo
```

`doctor` checks Python, the config, the data directory and token presence only. It makes no network
calls. A missing token is configuration, not an outage: a report without tokens is an explicit
no-data report with `complete: false`.

## Three surfaces, one core

| Surface | What you get | Contract |
|---|---|---|
| **CLI** | 25 top-level commands (18 groups + 7): `auth`, `config`, `gsc`, `yandex`, `metrika`, `bing`, `indexing`, `report`, `cwv`, `crawl`, `logs`, `schedule`, `backup`, `portfolio`, `mcp`, … | Every command supports `--json`, returns `{ok,data,meta,warnings}` and a stable exit code. Read commands accept `--strict` to exit `5` on truncated data. |
| **MCP** | 12 tools, 3 resources, 3 prompts; read-only by default | Two provider writes require `allow_write=true` plus a two-phase `plan_hash`/`confirm_token`; GSC sitemap submit and Bing delete stay `WRITE_DISABLED`. |
| **Skills** | `seo-weekly`, `seo-indexing-triage`, `seo-technical-audit` | `lv24-seo export skills --out ./skills`; skills are decision trees over the same tools, not a second implementation. |

Commands and flags live in [docs/cli.md](docs/cli.md); tool inputs, outputs and annotations live in
[docs/mcp.md](docs/mcp.md). This README does not duplicate those tables.

## Architecture in four lines

- **Sources → adapters.** GSC, Yandex Webmaster/Metrika, Bing/IndexNow, CrUX, PSI and the crawler
  return one `ProviderResult` carrying `total`, `truncated`, `data_age_days`, sampling fields and
  `limitations[]`; an outage is isolated as `sources_failed`, never guessed into zeros.
- **One history.** Daily rows and window aggregates are separate SQLite tables (schema v5) so
  overlapping windows never overwrite each other; report snapshots and the delivery ledger are
  stored alongside.
- **Analysis → report → delivery.** Deltas, striking-distance opportunities, indexing triage, an
  attributed change digest and CWV-by-template feed JSON/HTML/text/Markdown renderers; opt-in
  SMTP/Telegram delivery is content-hash idempotent.
- **Write gate and secrets.** Provider writes go through plan → confirm token → pending claim →
  provider call → `sent`, shared by CLI and MCP; tokens live in the OS keychain, `0600` files or
  `LV24_SEO_*` env vars. No LLM and no SaaS in any path.

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[diagrams](docs/architecture/README.md) · [ADRs](docs/decisions/).

## Supported sources

| Source | Free tier | Used for |
|---|---|---|
| Google Search Console | yes (OAuth token or service-account JSON) | queries, pages, countries, devices, filtered search analytics, URL Inspection, sitemap reads |
| Yandex Webmaster | yes (OAuth) | queries, indexing history/events, samples, sitemaps, SQI history, recrawl queue |
| Yandex Metrika | yes (separate `metrika` token) | organic pages, goals, dimensions, engagement, bytime |
| Bing Webmaster + IndexNow | yes | queries, pages, traffic, crawl stats, URL diagnostics, sitemap/URL/IndexNow submit |
| Chrome UX Report | yes (API key) | URL+device field p75 and weekly history |
| PageSpeed Insights | yes | lab scores and bounded Lighthouse audits |
| Your site / nginx logs | — | bounded crawl, robots, sitemap, JSON-LD, hreflang, links; read-only log rollups |

GSC sitemap submit and Bing sitemap delete return an explicit `WRITE_DISABLED`, and
`raw_retention_days` is reserved (no raw archives are written). Limits per API are documented in
[docs/data-sources.md](docs/data-sources.md).

## Desktop app

The same engine ships as **LV24 SEO Optimizer Desktop 0.9.9** — a Tauri v2 shell with a dashboard,
weekly report view, configuration editor/diff/validation, read-only diagnostics, OAuth connectors
and signed updates. It bundles a pinned `lv24-seo 0.15.3` sidecar and adds no provider logic:
<https://github.com/LevapVeeskela/lv24-seo-optimizer-desktop>.

## Repository map

| Path | Purpose |
|---|---|
| `src/lv24_seo/core/` | config, envelopes, errors, HTTP/retry, rate limiting, write gate, backup |
| `src/lv24_seo/providers/` | GSC, Yandex Webmaster/Metrika, Bing, CrUX, PSI adapters |
| `src/lv24_seo/crawl/` | fetcher, parsers, rules, link graph, nginx log ingest |
| `src/lv24_seo/store/` | SQLite schema (v5), migrations and repositories |
| `src/lv24_seo/analysis/` | deltas, opportunities, indexing triage, digest, CWV, fusion, rules |
| `src/lv24_seo/report/` | builders, renderers (html/text/md), localization, delivery |
| `src/lv24_seo/mcp/` | server, registry (tool metadata SoT), resources, prompts |
| `src/lv24_seo/cli/` | Typer commands |
| `schemas/tools.json` | generated tool schemas for non-MCP agents |
| `skills/` | canonical `SKILL.md` files |
| `docs/` | architecture, CLI/MCP reference, limits, guides, ADRs, releases |
| `tests/` | unit, contract (recorded fixtures) and network-gated e2e |

## Documentation & links

| Doc / surface | Link |
|---|---|
| Documentation portal (en/ru/zh) | <https://seo.lv24.by/> |
| Architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| CLI reference / MCP tools | [docs/cli.md](docs/cli.md) · [docs/mcp.md](docs/mcp.md) |
| Data sources and limits | [docs/data-sources.md](docs/data-sources.md) |
| Configuration / scheduling | [docs/config.md](docs/config.md) · [docs/scheduling.md](docs/scheduling.md) |
| OAuth connectors | [docs/auth.md](docs/auth.md) |
| Release notes | [docs/releases/](docs/releases/) · [CHANGELOG.md](CHANGELOG.md) |
| Security / contributing | [SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) |
| MCP Registry | <https://registry.modelcontextprotocol.io/v0/servers?search=io.github.LevapVeeskela%2Flv24-seo> |
| Doc map for LLM crawlers | [llms.txt](llms.txt) |

## For AI agents

Read [AGENTS.md](AGENTS.md) first — it is the canonical contract (layout, commands, hard rules,
review protocol). Then [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the system design and
[docs/README.md](docs/README.md) for the doc map. Machine-readable helpers: `schemas/tools.json`,
`skills/*/SKILL.md` and `llms.txt`.

Hard invariants: zero spend on the default path, read-only by default, honesty over completeness
(`truncated`/`total`/`limitations` are never weakened), no secrets in the repo, no SERP scraping,
and deterministic output.

## License

Apache-2.0. See [LICENSE](LICENSE) and [TRADEMARKS.md](TRADEMARKS.md). Data pulled from
Google/Yandex/Bing belongs to the property owner — this project never resells it.
