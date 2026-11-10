# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [SemVer](https://semver.org/).

## [Unreleased]

### Published

- **PyPI** `lv24-seo 0.1.0` (OIDC trusted publisher, `pypi` environment).
- **npm** `@lv24/seo 0.1.0` — thin launcher over `uvx`; trusted publishing is configured for future
  releases (first publish was manual).

### Added — engine (waves W0–W7)

- **Core**: config (`seo.toml` + `LV24_SEO_*` env), secret store (keychain → env → `0600` file),
  HTTP client with IPv4 pinning/retry/`Retry-After`, token-bucket rate limiter, redacting JSON logging,
  time/period helpers, typed error codes + response envelope.
- **Providers**: Google Search Console (search analytics with pagination, URL inspection, sites,
  sitemaps), Yandex Webmaster (hosts, summary, indexing history, search-urls events, diagnostics,
  popular queries, recrawl quota), Yandex Metrika (counters, goals, stat/v1), Bing Webmaster +
  IndexNow, Chrome UX Report, PageSpeed Insights. Every result carries `truncated`, `total`,
  `data_age_days`, `limitations[]`.
- **Crawler**: robots.txt (RFC 9309), manual redirects, sitemap reader (index, gzip, 50k/50MB caps),
  stdlib HTML extraction (title/meta/canonical/hreflang/JSON-LD/links/images/word count), internal
  link graph, render-parity hook, and **30 rules** (indexability, content, structured data, i18n,
  mixed content, sitemap membership).
- **Storage**: SQLite (WAL) with 9 tables, repositories, snapshot hashing, delivery ledger.
- **Analysis**: period deltas with thresholds, striking-distance/CTR-gap/decay opportunities, indexing
  triage buckets with next actions, GSC↔Yandex↔Metrika fusion with explicit caveats, nginx JSON log
  stats (bots, crawl waste, p95).
- **Reports**: weekly/indexing/opportunities builders, text/Markdown/HTML renderers (email-safe,
  escaped), SMTP + Telegram delivery with content hashing and idempotent sends, change-gated sends.
- **CLI**: `version`, `doctor`, `init`, `config show`, `auth status|set|login|logout`, `properties`,
  `gsc query`, `yandex queries|indexing`, `crawl`, `report weekly`, `schedule print|install`,
  `install --client`, `export tools` — every command supports `--json` and stable exit codes.
- **MCP**: 10 registry-driven tools (8 read + 2 gated write), resources `seo://config`,
  `seo://report/latest`, `seo://docs/tools`, prompts `weekly_seo_review` / `indexing_triage` /
  `content_opportunities`; stdio by default, stateless HTTP optional.
- **Automation**: cron / systemd user units / Windows Task Scheduler generators with `--print` and
  idempotent install; GitHub Actions and VPS cron recipes.

### Added — docs and repository

- Apache-2.0 license, agent contract (`AGENTS.md`), `llms.txt`, CI, issue/PR templates, gitleaks.
- `lv24_seo.mcp.registry` — canonical tool registry (10 tools) and generated `schemas/tools.json`.
- Docs: architecture, data sources and quotas, MCP surface, CLI, config, scheduling, skills,
  3 ADRs, quickstart, GitHub Actions and VPS guides.
- Research: 2026 landscape (Google/Yandex APIs, existing SEO MCP servers, paid tooling, audit signals).
- Skills: `seo-weekly`, `seo-indexing-triage`, `seo-technical-audit`.

### Known gaps

- GSC service-account auth is documented but not wired (OAuth bearer token only).
- Write operations exist in adapters but have no CLI/MCP confirm-token flow yet.
- No live end-to-end run against a real property; PyPI/MCP-registry publishing pending.

### Notes

- Pre-release: interfaces may change until `v0.1.0`.
