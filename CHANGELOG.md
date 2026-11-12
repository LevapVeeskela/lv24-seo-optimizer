# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [SemVer](https://semver.org/).

## [Unreleased]

### Added

- Product-grade weekly email: localized subject, HTML and 78-column plain text for `ru`, `en`
  and `zh` — a 600px inline-CSS card with KPI cards, top-3 opportunities, issue severities,
  merged sources, data notes/warnings and a property/UTC footer. No external images, stylesheets,
  scripts or tracking pixels; unknown values stay unknown and Metrica is reported as sessions.
- `report.locale` accepts `zh` (default remains `ru`). Locale is presentation-only: JSON
  snapshots, `report diff`, MCP payloads and machine codes stay canonical English.

### Changed

- Delivery de-duplication includes the normalized render locale in `content_hash`, so switching
  language re-sends the same period once; `generated_at` and diagnostics remain excluded.

## [0.3.1] - 2026-11-11

Release notes: [`docs/releases/0.3.1.md`](docs/releases/0.3.1.md).

Documentation-only patch: the npm package now carries the same current picture as the repository
README. No code, schema or CLI/MCP contract changed.

### Documentation

- `packages/npm/README.md` rewritten for 0.3.x: banner and badges, 11 MCP tools including
  `seo_report_diff`, week-over-week `report diff`, sitemap ↔ crawl coverage diff, honest
  `complete`/`total`/`limitations` semantics, current usage examples and links to the release notes.
- Root README (EN/RU), `docs/releases.md` and version metadata moved to `0.3.1` so PyPI, npm and
  the MCP Registry stay aligned.

## [0.3.0] - 2026-11-11

Release notes: [`docs/releases/0.3.0.md`](docs/releases/0.3.0.md).

### Added

- `crawl` / `seo_crawl_site` return `sitemap_coverage`: a sitemap ↔ crawl diff in six buckets
  (`unlinked`, `missing_from_sitemap`, `not_ok`, `noindex`, `blocked`, `unvisited`) with exact
  counts, capped lists, `complete` and `limitations[]`. Unvisited sitemap URLs are unknown, never
  broken; a missing sitemap is "empty, not clean".
- `lv24-seo report diff [--base] [--head] [--markdown]`: deterministic diff of two stored weekly
  snapshots (headline metrics, issue counts, opportunity churn, limitation changes) for PR
  comments and CI summaries. Read-only, no provider calls; metrics missing on one side are
  unknown, not zero. `Store.list_snapshot_periods()` backs it.
- MCP tool `seo_report_diff` (11th tool, read-only): the same snapshot diff for agents, with
  optional `base`/`head` periods validated as `START..END`.

### Fixed

- Weekly GSC source `meta.total` no longer becomes a known `0` when a dataset reports no total;
  it is `null` (unknown) unless every dataset reported one.
- Delivery: a journal COMMIT failure after the message already left is reported as `sent` with
  `reason=delivery_journal_failed`, not `failed`.
- Telegram text cut at the message limit now ends with an explicit truncation marker instead of
  being shortened silently.

### Documentation

- Architecture §7 now states the MCP threading model, the exact `complete` conditions, the
  unknown-total rule and delivery semantics; crawl-scope docs describe the pre-request origin and
  robots checks (and the RFC 9309 `robots.txt` redirect exception); paid adapters are no longer
  described as shipped in the GitHub Actions guide; Migration 2 is documented as GSC-only.

### Tests

- MCP threading: negative control proving MCP 2.x runs raw sync tools off the loop thread,
  serialisation under concurrent `call_tool`, and the default-workspace lifecycle through
  `create_server()` / `close_workspaces()` with a durable re-open.
- Redirect scope: second-hop off-origin redirect, disallowed seed, and sitemap discovery
  (`Sitemap:` line, index child, sitemap redirect) all asserted by the absence of the request.
- Store: Migration 2 marks only GSC rows, `include_legacy` opt-in, failed migration rolls back
  DDL and ledger; weekly `complete=false` for truncated, empty, missing and incomparable sources.

## [0.2.0] - 2026-11-11

### Fixed

- Follow-up review: preserve Yandex diagnostic codes from mapping keys, use explicit query
  windows and documented Metrika GET requests; rate-limit every retry. Unknown legacy daily
  rows are marked and excluded from normal reads.
- Pin crawler connections to validated addresses with original Host/SNI, check robots before
  redirect requests, count failed sitemap requests against budgets and pace every hop.
- Preserve incomplete/baseline metadata and source warnings in delivered reports; serialize
  delivery attempts through SQLite and send Telegram as plain text.
- Scheduler commands use target-specific quoting; systemd carries an explicit IANA timezone,
  unavailable zones fail before installation and Windows seasonal zones warn about fixed offsets.
- Explicit npm wrapper overrides cannot bypass recursion guards. Release verification also
  checks the npm launcher and packaged skills; manual registry publishing requires verification.

- Weekly headline compares GSC windows only: the Yandex query window (no previous
  window for a delta) and Metrika sessions are shown per-engine and never merged
  into a cross-engine total (no phantom growth or CTR).
- Collector/builder source ids unified (`gsc`, `yandex_webmaster`, `yandex_metrika`): Yandex and
  Metrika rows reach the traffic section again.
- One failing provider no longer cancels the weekly report: per-source isolation with
  `sources_failed` and `complete` in the envelope; a total outage yields an explicit no-data
  report instead of an exception.
- Yandex popular queries read `query_text` and list-shaped `indicators`, honour `count` and
  paginate with `offset`; diagnostics expand the `problems` object (code/state/severity).
- GSC sends `dimensionFilterGroups`; Bing `SubmitUrlbatch` authenticates with `apikey`.
- CrUX reads `cumulative_layout_shift`, `histogram[].density` and string p75 CLS; unknown metrics
  stay unknown instead of zero-filled.
- SMTP uses a verifying TLS context (no login after a TLS failure); Telegram HTTP 400/429 and Bot
  API error dicts report `failed`, never `sent`; delivery hash excludes `generated_at`/diagnostics;
  configured thresholds gate sends; concurrent claims surface `duplicate_race` (at-least-once).
- Crawler: outbound SSRF policy (private/loopback/link-local refused per hop, DNS re-resolved,
  explicit local opt-in, HTTPS downgrade flagged); sitemap discovery wired into the walk with
  budgets and truncation; confirmed-broken vs unvisited vs failed separated (`/a` ≠ `/a/`);
  robots 5xx/network defers instead of failing open; longest-match `Allow` semantics; `Crawl-delay`
  pacing; request budget with honest truncation; `<base href>` honoured; canonical compared to the
  final URL; decay matches vanished queries/pages as zero only with complete coverage.
- Store: `metric_daily` holds true daily rows, window aggregates go to `metric_windows`
  (pre-migration GSC daily rows are marked `legacy_unknown_granularity` and excluded
  from normal daily reads; `include_legacy=True` preserves access, no history deleted);
  GSC sightings no longer overwrite crawl truth;
  multi-write syncs are atomic; `schema_migrations` ledger added.
- MCP tools are registered as async owner-loop adapters: the synchronous service and
  SQLite connection stay on the event-loop thread (no `check_same_thread=False`),
  the core runs serially and blocks the loop while it runs.
- MCP and CLI share one SQLite history through `lv24_seo.composition` with lifecycle close.
- Scheduler embeds the absolute `--config`; cron carries a real `CRON_TZ` assignment; Windows
  boundaries carry UTC offsets when the zone resolves; every backend reports its timezone limits
  (Windows needs the new `tzdata` dependency).
- npm launcher pins `uvx --from lv24-seo==<version>`, validates `LV24_SEO_BIN`, refuses to exec
  its own shim (content + depth guards) and ships `LICENSE` via `prepack`.
- Docs reflect the real CLI/MCP surface (flags, inputs, resources, transports), the automatic
  OIDC release pipeline with a same-SHA verify gate, checksum-verified binary downloads, narrowed
  gitleaks allowlists and skill installation from the wheel (`lv24-seo export skills`).

### Added

- `lv24-seo export skills` copies the packaged skills (+ `manifest.json`) anywhere.
- `py.typed` ships in the wheel (the `Typing :: Typed` classifier is now true).
- Neon README banner and an editable workflow diagram in English and Russian READMEs.
- SHA-pinned Actions, `uv sync --locked`, Python 3.12 in the CI matrix, functional npm launcher
  tests, installed-wheel smoke test in the release `verify` job.

## [0.1.2] - 2026-11-10

### Changed

- Sharper package descriptions and npm keywords for `lv24-seo` / `@lv24/seo`; README now leads with
  the problem, a Start/Use/Operate nav and a real `doctor` / `report weekly` transcript.
- `pyproject.toml` summary and the npm launcher description now say what the tool actually does
  (audits + weekly diff reports from GSC, Yandex, Bing, CrUX, PSI) instead of internal positioning.

## [0.1.1] - 2026-11-10

### Fixed

- `server.json` migrated to the current MCP Registry schema `2025-12-11` (the previous `2025-09-29`
  form is deprecated); the entry is now published as `io.github.LevapVeeskela/lv24-seo@0.1.1`.
- Release workflow is idempotent: PyPI and npm jobs skip versions that already exist, so a failed
  channel can be retried from the same tag.
- npm publishing uses **Trusted Publishing (OIDC)** with automatic provenance instead of a long-lived
  token, matching the `lv24-brain` setup.

### Added

- npm launcher `@lv24/seo` (thin `uvx` wrapper) and its CI syntax/package check.

## [0.1.0] - 2026-11-10

### Published

- **PyPI** `lv24-seo 0.1.0` (OIDC trusted publisher, `pypi` environment).
- **npm** `@lv24/seo 0.1.0` — thin launcher over `uvx`; trusted publishing is configured for future
  releases (first publish was manual).
- **MCP Registry** `io.github.LevapVeeskela/lv24-seo@0.1.0` — `active`, discoverable by MCP clients.

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
