# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [SemVer](https://semver.org/).

## [0.15.1] - 2026-11-18

Release notes: [`docs/releases/0.15.1.md`](docs/releases/0.15.1.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-18); desktop `0.9.5` bundles this engine. Patch release: reject
documented placeholder OAuth client ids.

### Fixed

* The docs example `0123456789abcdef0123456789abcdef` (Yandex) and
  `1234567890-abc.apps.googleusercontent.com` (Google), plus `your-*-client-id`, are rejected with
  `CONFIG_INVALID` before a consent URL is built.
* `yandex_oauth.authorization_url()` enforces the same guard for every caller.
* Desktop `0.9.5` disables Connect while the OAuth client is missing/misconfigured and ignores a
  second Open click while the first is in flight.

## [0.15.0] - 2026-11-18

Release notes: [`docs/releases/0.15.0.md`](docs/releases/0.15.0.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-18); desktop `0.9.5` bundles this engine. Minor release: four new
crawl rule packs.

### Added

* `security-headers` (medium): HSTS, CSP, X-Frame-Options, X-Content-Type-Options,
  Referrer-Policy, Permissions-Policy and cookie flags (`Secure`, `HttpOnly`, `SameSite=None`).
* `tls-certificate` (high): expiry, protocol, SAN/CN hostname and weak ciphers, using TLS facts
  captured by the fetcher and the page `fetched_at` timestamp.
* `schema-required` (medium): required fields for Product, Offer, BreadcrumbList, FAQPage, Recipe,
  JobPosting, VideoObject, LocalBusiness, Course and SoftwareApplication.
* `a11y-basics` (medium): input labels, button/link accessible names, `<main>`, skip link and
  heading order, backed by new parser fields.
* `FetchResult`/`PageData` carry `tls`, every `set_cookie` value and `fetched_at`; `PageData` also
  carries the accessibility fields.

### Not changed

* MCP 12 tools, schema 5, CLI 25 commands, no new dependency, no scraping, no write-gate change.

## [0.14.1] - 2026-11-18

Release notes: [`docs/releases/0.14.1.md`](docs/releases/0.14.1.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-18); desktop `0.9.3` bundles engine `0.14.0` and desktop `0.9.4`
follows with this engine. Patch release: hidden Windows scheduled jobs.

### Fixed

* Windows Task Scheduler jobs no longer open a console window: `schedule install --target windows`
  registers a generated `.vbs` launcher via `wscript.exe //B //Nologo`; the launcher runs the engine
  hidden (`WScript.Shell.Run(..., 0, True)`) and forwards the exit code.
* `schedule status --target windows` lists the `task`, `xml` and `vbs` artefacts; a missing or
  changed launcher is `outdated`/`partial`, and `schedule uninstall` removes it.
* Existing Windows tasks must be reinstalled once; `schedule status` reports them as `outdated`.

### Not changed

* MCP 12 tools, schema 5, CLI 25 commands, no new dependency, no write-gate change.

## [0.14.0] - 2026-11-18

Release notes: [`docs/releases/0.14.0.md`](docs/releases/0.14.0.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-18); desktop `0.9.1` bundles engine `0.13.1` and desktop `0.9.2`
follows with this engine. Minor release: OAuth readiness diagnostics.

### Added

* `auth status --json` gains an `oauth` object per provider with `state`, presence booleans,
  `expires_at`, `scopes_recorded`, `next_action` and `limitations`; existing fields are unchanged.
* `doctor --json` adds `gsc_oauth`/`yandex_oauth`/`metrika_oauth` checks with stable codes; they are
  non-fatal and never print a token.
* Eight states: `oauth_client_missing`, `oauth_client_secret_missing`, `oauth_token_missing`,
  `oauth_token_expired`, `oauth_scope_missing`, `oauth_scope_unknown`, `oauth_redirect_invalid`,
  `oauth_ready`; precedence redirect → client → secret → token → scope.
* Scope recording in `gsc_token_scopes`, `yandex_token_scopes`, `yandex_metrika_token_scopes`
  (`OAUTH_SECRET_NAMES` only; `SECRET_NAMES` stays stable).

### Not changed

* MCP 12 tools, schema 5, CLI 25 commands, no new dependency, no live provider call in the checks.

## [0.13.1] - 2026-11-18

Release notes: [`docs/releases/0.13.1.md`](docs/releases/0.13.1.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-18). Patch release.

### Added

* `auth login --provider yandex|metrika --url-only` returns the authorization URL without opening a
  browser, prompting for a code or writing a token; the desktop uses it for the Connect flow.
  Yandex-only; no API, MCP, schema or config change.

## [0.13.0] - 2026-11-18

Release notes: [`docs/releases/0.13.0.md`](docs/releases/0.13.0.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-18); desktop `0.9.1` bundles engine `0.13.1`. Minor release: OAuth connectors for Google Search Console and Yandex Webmaster/Metrika.

### Added

* `auth login --provider gsc`: Google Cloud Desktop app client, loopback `127.0.0.1` + S256 PKCE
  and `state`, `access_type=offline`, refresh token, `OAuthTokenProvider`, `--no-browser`/`--code`.
* `auth login --provider yandex|metrika`: Yandex authorization code via
  `https://oauth.yandex.ru/verification_code`, refresh token, separate token slots and
  `ensure_fresh_access_token`.
* `[auth.gsc]`/`[auth.yandex]` public client configuration (`client_id`, `scopes`, `redirect_uri`),
  `OAUTH_SECRET_NAMES`, client-secret storage via `auth set --provider *_oauth_secret`, and
  `doctor` recognition of an OAuth grant.
* `docs/auth.md`, `docs/auth-google.md`, `docs/auth-yandex.md`.

### Not changed

* MCP stays at **12 tools**, SQLite schema at **version 5**, CLI at **25 top-level commands**.
* No new required dependency; Bing/IndexNow/CrUX/PSI stay key-based.

## [0.12.2] - 2026-11-17

Release notes: [`docs/releases/0.12.2.md`](docs/releases/0.12.2.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-17). Patch release.

### Fixed

* Yandex `recrawl/queue` 5xx responses (including a 504 with an empty body) are reported as
  `UPSTREAM_UNAVAILABLE` with the HTTP status, not as "invalid JSON". `202`/`409`/`429` behavior is
  unchanged. No API, CLI, schema or config changes.

## [0.12.1] - 2026-11-17

Release notes: [`docs/releases/0.12.1.md`](docs/releases/0.12.1.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-17). Patch release.

### Fixed

* `indexing run` reported `internal_error` for Bing and Yandex because the indexing engine closed a
  provider client between the quota read and the checks/submit phase while `SeoService` kept the
  closed client cached. The service now owns the client for the whole run; a regression test covers
  the cached-client failure. No API, CLI, schema or config changes.

## [0.12.0] - 2026-11-17

Release notes: [`docs/releases/0.12.0.md`](docs/releases/0.12.0.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-17); desktop `0.9.0` bundles engine `0.11.0` and desktop `0.9.1`
follows. Minor release: quota-aware indexing automation across Bing, Yandex, IndexNow and GSC.
The CLI now has **25 top-level commands** (18 groups + 7 commands), the MCP surface stays at
**12 tools**, and the SQLite schema moves to **version 5**.

### Added

* `indexing status`, `indexing run` and `indexing history`: a sitemap-backed URL queue, priority by
  `lastmod`/last check, quota-aware planning and idempotent submissions.
* Quota guards: Bing remaining daily/monthly, Yandex recrawl remainder, a local GSC inspection cap
  (2,000/day) and a local IndexNow cap (1,000/day).
* Honest index checks: GSC URL Inspection per URL, the GSC sitemap `contents[].indexed` aggregate
  (tagged deprecated), Bing `http_status`/`last_crawled_at`, and explicit unknown status for Yandex
  and IndexNow.
* Schema v5: `indexing_state`, `indexing_runs`, `quota_usage`.
* `[indexing]` config: disabled by default, four providers, submit/inspect caps, stale/check cadence.
* `schedule install --kind indexing`: a daily `lv24-seo-indexing` job at `0 4 * * *`; the host
  scheduler must provide `LV24_SEO_WRITE_SECRET` in its environment.

### Changed

* CLI command count 24 → 25 (new `indexing` group).
* `schedule --kind` accepts `weekly`, `daily` and `indexing`.
* Docs add the quota/index-status matrix, the `[indexing]` section and the scheduler secret rules.

### Not changed

* MCP stays at 12 tools; indexing automation is CLI/scheduler-only.
* Existing write gates for Bing URL/sitemap submit, IndexNow and Yandex recrawl are reused.

## [0.11.0] - 2026-11-17

Release notes: [`docs/releases/0.11.0.md`](docs/releases/0.11.0.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-17); desktop `0.9.0` bundles engine `0.11.0`. Minor release: full-platform read depth. GSC gains URL Inspection, sitemap reads and
filtered search analytics; Yandex gains events samples, sitemaps, history and a wired recrawl queue;
Bing gains page/traffic/crawl/URL/role/join diagnostics; Metrika gains an organic-only collector and
a `metrika` read group; CrUX gains collection periods and weekly history; PSI gains Lighthouse
audits; IndexNow gains real error semantics. The CLI now has **24 top-level commands** (17 groups +
7 commands), the MCP surface stays at **12 tools**, and the SQLite schema stays at version 4.

### Added

* `gsc inspect`, `gsc sitemaps`, `gsc sitemap` and richer `gsc query` selectors (`--type`,
  `--filter`, `--filter-list`, `--data-state`, `--aggregation`, `--search-appearance`,
  `--branded`); `seo_indexing_triage` accepts an optional `url`.
* `yandex samples`, `yandex sitemaps`, `yandex history`, `yandex queue`; `yandex recrawl` applies
  one gated POST per URL, capped to the remaining quota.
* `bing pages`, `bing traffic`, `bing crawl`, `bing urlinfo`, `bing children`, `bing users`,
  `bing sites`, `bing joins`.
* `metrika pages|goals|queries|devices|countries|engagement|bytime`, organic-filtered.
* `cwv audits` (bounded Lighthouse audits) and `cwv history` (CrUX weekly p75).
* IndexNow key validation and `202`/`400`/`403`/`422`/`429` mapping.

### Fixed

* Yandex `search-urls/events/history` dropped the real `indicators` series and always reported
  `empty_history` (live 2026-10-02: 57 appeared / 0 removed).
* Metrika `data_age_days` was inflated by the previous comparison window.
* GSC sitemap mapping dropped `type`, `isSitemapsIndex` and `contents[]`.
* PSI audits and run metadata were discarded; CrUX used a hardcoded 2-day age.

### Changed

* Metrika SEO numbers are organic-only; all traffic is kept explicit as `all_visits`/`all_users`.
* `ProviderResult` carries `extra` metadata and sampling fields; `to_meta()` always emits `sampled`.
* MCP stays at 12 tools; GA4 remains an optional, unimplemented provider.

## [0.10.0] - 2026-11-17

Release notes: [`docs/releases/0.10.0.md`](docs/releases/0.10.0.md). Status: **released** (PyPI,
npm and the MCP Registry, 2026-11-17); desktop `0.8.0` bundles engine `0.10.0`. Minor release:
Bing URL submission is a real gated write, and Bing quota, sitemap-feed and crawl-issue views are
read-only commands. The CLI now has **23 top-level commands** (16 groups + 7 commands), the MCP
surface stays at **12 tools**, and the SQLite schema stays at version 4. Bing URL submission is
CLI/desktop-only.

### Added

- `bing submit [--url ABS ...] [--from-sitemap] [--sitemap-url ABS] [--site URL] [--allow-write]
  [--confirm TOKEN] [--config ABS] [--json]`: plans with the URL count, 500-URL batch count and
  current remaining quota; applies through `SubmitUrlbatch` in 500-URL batches with client-side
  deduplication and daily/monthly quota pre-checks. A success is `{"d": null}` with no per-URL
  status, so the result says `accepted`, never "indexed". A changed sitemap invalidates the
  confirm token. Missing site is `CONFIG_INVALID`; missing key is `AUTH_REQUIRED`.
- `bing quota`, `bing sitemaps [--details FEED_URL]` and `bing issues`: read-only Bing views.
  Quota exposes `daily_remaining`/`monthly_remaining` (verified: Bing reports remaining, not
  totals) and states that the monthly window can bind first and its reset is undocumented.
  Sitemaps map `Url`, `Type`, `Status`, `Submitted`, `LastCrawled`, `UrlCount`, `FileSize` and
  `Compressed`, and state that per-URL errors/crawl status are not exposed. Crawl issues decode
  the `Issues` bitmask (`301`, `302`, `4xx`, `5xx`, `robots`, `malware`, `important_robots`,
  `dns`, `timeout`) and keep `issues_bits` plus unknown fields in `raw`.
- Bing `ErrorCode` mapping for HTTP 400 bodies: `3 -> AUTH_INVALID`, `14 -> PROPERTY_NOT_FOUND`,
  `7 -> INVALID_INPUT`, `4/5 -> QUOTA_EXCEEDED`; 401/403/404/429/5xx remain fallbacks. The
  provider message and API key never reach an error.
- The shared HTTP client accepts `raise_for_status=False` for adapters that need to parse a
  structured provider error body; the default is unchanged.

### Changed

- The CLI now has 23 top-level commands: 16 groups (adds `bing`) and 7 commands.
- `seo_sitemap_submit` and the MCP surface are unchanged; Bing URL submission is CLI/desktop-only.

## [0.9.0] - 2026-11-17

Release notes: [`docs/releases/0.9.0.md`](docs/releases/0.9.0.md). Status: **released**
(PyPI, npm and the MCP Registry, 2026-11-17); desktop `0.7.0` bundles engine `0.9.0`. Minor
release: Bing sitemap submission is wired through the current JSON `SubmitFeed` endpoint, and
IndexNow can read its URL list from the project sitemap and its host/key from `seo.toml`. The CLI
still has **22 top-level commands**, the MCP surface stays at **12 tools**, and the SQLite schema
stays at version 4.

### Added

- `sitemap submit --provider gsc|bing` (default `gsc`): Bing resolves the site from
  `providers.bing.site` or `--property`, the sitemap from `--url` or
  `<project.base_url>/sitemap.xml`, and the `bing_api_key` secret. The apply calls the JSON
  `SubmitFeed` endpoint and returns `{provider, status, site_url, sitemap_url}`. The plan
  (`--allow-write`) contacts no provider; the two-phase gate and idempotency ledger are shared with
  the other writes. GSC keeps `--property`/`--url` required and still returns `WRITE_DISABLED` on
  apply; Bing `delete` is also `WRITE_DISABLED`.
- `indexnow submit --from-sitemap [--sitemap-url ABS] [--url ...]`: host, key and key
  location resolve from `providers.bing.site`, `providers.bing.indexnow_key` and
  `providers.bing.indexnow_key_location` (or `https://<host>/<key>.txt`). A urlset or a
  bounded sitemapindex (root plus five children, 10,000 URLs) is read during the plan,
  explicit URLs are merged and deduplicated, off-host URLs are skipped, and
  `truncated`/`total`/`limitations` report any cap. The apply re-fetches the sitemap and a
  changed URL set invalidates the confirm token. A missing key is `AUTH_REQUIRED`; the 422
  key-file mismatch keeps its next action.
- MCP `seo_sitemap_submit` accepts an optional `provider` (`gsc|bing`, default `gsc`),
  optional `property` and optional `sitemap_url`; Bing apply uses the same
  `SeoService.submit_sitemap` path as the CLI. The tool count stays at 12.
- `providers.bing.indexnow_key` and `providers.bing.indexnow_key_location` are public
  identifiers and safe in `seo.toml`; the Bing API key remains a secret.

### Changed

- `sitemap submit --property`/`--url` are optional at the parser level for Bing defaults;
  GSC still requires them and reports `INVALID_INPUT` in the envelope.
- IndexNow's plan payload adds `key_location` and, in sitemap mode, `sitemap_url`.
- `schemas/tools.json` is regenerated for the optional `seo_sitemap_submit` inputs. No tool
  was added, removed or renamed.

## [0.8.0] - 2026-11-16

Release notes: [`docs/releases/0.8.0.md`](docs/releases/0.8.0.md). Status: released 2026-11-16;
`0.8.0` is the latest published build on PyPI, npm and the MCP Registry.

Minor release: adds the URL+device CWV writer, GSC service-account authentication, richer Yandex
Webmaster indicators, non-destructive `backup restore --to`, and a plan-by-default `schedule`
lifecycle. The CLI now has **22 top-level commands**; the MCP surface, SQLite schema and
`schemas/tools.json` are unchanged.

### Added

- `cwv collect --url ABS [--url ABS ...] [--device phone|desktop|tablet|all] [--source crux|psi|both]
  [--limit 1..50] [--json]`: queries CrUX per URL and device and persists every p75 as a
  `metric_daily` row with `source=crux`, `scope=<device>` and `key=<url>`. PSI lab rows are returned
  under `data.lab` and never persisted; `tablet` is unsupported by PSI and reported as
  `psi_tablet_unsupported`. `report weekly` now reads stored CWV rows through today.
- Google Search Console service-account authentication: `providers.gsc.service_account_file` or
  `LV24_SEO_GSC_SERVICE_ACCOUNT_FILE` (absolute path) is read before the `gsc_token` secret. The
  engine signs an RS256 JWT, exchanges it at the Google token endpoint and caches/refreshes the
  access token 60 seconds before expiry; a `gsc_token` that is a service-account JSON object also
  works, and a plain token keeps the old bearer behaviour. `properties` adds `token_source`;
  `doctor` adds a non-fatal `gsc_auth` check. `cryptography>=43` is now a required dependency.
- Yandex Webmaster indicators: `AVG_CLICK_POSITION` (`avg_click_position`), `query_id`,
  `extra_indicators` for unmapped query indicators, `site_problems` in indexing summary and the
  documented `HTTP_2XX`…`OTHER` indexing-history series merged per date. The legacy list form is kept.
- `backup restore --file ABS --to ABS_DIR [--json]`: extracts an archive into a new directory after
  re-validating every member. The target must not exist, zip-slip paths and symlinks are rejected,
  the database passes `PRAGMA quick_check`, and no `--allow-write` or confirm token is involved
  because no existing file is modified. The in-place `--config` restore is unchanged.
- `schedule status [--kind weekly|daily]`: read-only report of `installed|outdated|partial|missing|
  unknown` with expected paths and per-artefact `exists`/`matches`.
- `schedule uninstall [--allow-write]`: plan-only by default; with `--allow-write` removes exactly
  the managed cron block, the two systemd user units or the named Task Scheduler task. A second run
  is a no-op and unrelated entries are never touched.

### Changed

- `schedule install` is plan-only by default (`dry_run: true`, `installed: false`) and requires
  `--allow-write` to touch the host; `--print` stays an alias for the default. Scripts that relied on
  the old implicit install must add `--allow-write`.
- The CLI has 22 top-level commands: 15 groups (adds `cwv`) and 7 commands.
- The MCP surface stays at 12 tools (10 read-class, 2 gated writes), 3 resources and 3 prompts; no
  tool input or output changed. The SQLite schema stays at version 4 and `schemas/tools.json` is
  unchanged.

### Fixed

- Yandex Metrika `stat/v1/data` uses the documented 1-based `offset`; the previous `0` made every
  dashboard call return HTTP 400, so the provider looked configured but reported no data.
- CLI JSON output is ASCII-escaped and human output degrades unencodable glyphs, so redirected
  Windows output on cp1251/cp866 no longer aborts with `UnicodeEncodeError` on a `⚠` warning.

## [0.7.0] - 2026-11-16

Release notes: [`docs/releases/0.7.0.md`](docs/releases/0.7.0.md).

Minor release: adds a read-only multi-site portfolio, two-phase backup restore, persisted Yandex
indexing events (schema migration v4) and a Core Web Vitals by template section in `report weekly`.
The CLI now has **21 top-level commands**; the MCP surface stays at **12 tools** with no changed
input.

### Added

- `portfolio list --root ABS [--limit 1..50] [--json]`: read-only roll-up over sibling project
  folders. Scans `seo.toml` up to three levels deep, reports local crawl/report/issue activity per
  site, converts per-site failures into `error` objects and never calls a provider or reads a
  secret. A capped scan reports `truncated`, the real `total` and a limitation.
- `backup restore --file ABS --config ABS [--allow-write] [--confirm TOKEN] [--json]`: a two-phase
  restore. The plan validates the archive and returns a 300-second `confirm_token` bound to the
  archive bytes and target state; apply re-verifies every member hash, runs `PRAGMA quick_check`,
  copies the current database/config to `.pre-restore-<utc>`, and replaces files atomically.
  Missing/expired/replayed tokens are `CONFIRM_REQUIRED`; a token without `--allow-write` is
  `WRITE_DISABLED`.
- Schema v4 adds `indexing_events`, keyed by `(provider, property_id, url, event, event_date)`.
  `yandex indexing` persists provider-reported per-URL events when a store exists and reports
  `new`/`existing`/`skipped`/`truncated` in `meta.indexing_events`. Daily aggregate rows without a
  URL are skipped; a storage failure is a warning.
- `report weekly`: an additive `Core Web Vitals by template` (`cwv`) section built from stored
  CrUX/PSI field rows. It groups by normalized URL template and device, shows at most five
  templates (regressed first), reports p75/status/regression per metric, and adds explicit
  limitations for origin aggregates, lab rows, missing URLs/devices and absent comparisons.

### Changed

- The CLI has 21 top-level commands: 14 groups (`auth`, `config`, `gsc`, `yandex`, `sitemap`,
  `indexnow`, `report`, `schedule`, `export`, `logs`, `tasks`, `backup`, `portfolio`, `mcp`) and 7
  commands (`version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`).
- The weekly digest now reads stored `indexing_events` inside the compared windows, in addition to
  `issues` and `pages`. Evidence is capped at 500 rows per source with a limitation when hit.
- Schema version is 4. Migration 4 is additive and applied on the next open; no user data is
  rewritten or deleted.
- The MCP surface is unchanged (12 tools, 3 resources, 3 prompts). Portfolio and restore are
  CLI-only; `yandex_indexing_state` keeps its tool contract.

## [0.6.0] - 2026-11-16

Release notes: [`docs/releases/0.6.0.md`](docs/releases/0.6.0.md).

Minor release: adds local log analysis, an attributed weekly digest, the finding/task lifecycle
(schema migration v3) and portable project backups. The CLI now has **20 top-level commands** and the
MCP server has **12 tools**. Existing envelope keys and tool inputs are unchanged;
`schemas/tools.json` gains the additive `seo_log_analysis` schema. CI and contract gates are
unchanged from 0.5.3.

### Added

- `logs analyze --file ABS [--bot googlebot|yandexbot|bingbot|other] [--json]`: one local nginx JSON
  access log (100 MiB cap, absolute regular file only), `bot_daily` rollups with nearest-rank
  `p95_ms`, and a `crawl_waste` summary. Read-only: nothing is uploaded, stored or added to SQLite.
- `seo_log_analysis(file, bot="")`: the 12th MCP tool, read-only and store-less, returning the same
  `data`, `meta` and `warnings` as the CLI.
- `report weekly`: an additive `Attributed change digest` section pairs significant GSC headline
  changes with a candidate cause (`new_crawl_issue`, `resolved_crawl_issue`, `indexing_event`,
  `page_state`, `unexplained`), confidence, up to three evidence rows and a `next_action`. Items are
  capped at 8, evidence at 3; digest limitations merge into the report.
- `tasks list|add|update`: a finding lifecycle keyed by `(kind, property_id, url, rule_id)` with
  states `open`, `in_progress`, `verified` and `dismissed`, idempotent adds, validated transitions
  and a bounded newest-first list.
- `backup create --out ABS [--force]`: one portable `lv24-seo-backup-YYYYMMDDTHHMMSSZ.zip` with a
  hashed `manifest.json`, a secret-masked `seo.toml` and a consistent SQLite copy made through the
  backup API; written to a temporary file and renamed atomically. Restore is not implemented.

### Changed

- The CLI has 20 top-level commands: 13 groups (`auth`, `config`, `gsc`, `yandex`, `sitemap`,
  `indexnow`, `report`, `schedule`, `export`, `logs`, `tasks`, `backup`, `mcp`) and 7 commands
  (`version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`).
- The MCP surface has 12 tools: 10 read-class (8 pure read-only, including `seo_log_analysis`, plus
  the 2 local-state writers) and 2 gated provider writes that still return `WRITE_DISABLED`.
  Resources and prompts are unchanged (3 and 3).
- Schema version is 3. Migration 3 adds the `tasks` table on the next open; it is additive and
  rewrites or deletes no user data. A concurrent first open retries the WAL mode switch briefly
  instead of failing on `SQLITE_BUSY`.
- `ReportSection` gained optional `total`, `truncated` and `data_age` fields, serialised only when a
  section sets them; existing report payloads keep their shape.

## [0.5.3] - 2026-11-16

Release notes: [`docs/releases/0.5.3.md`](docs/releases/0.5.3.md).

P0 wave: honest provider data, a hardened crawl and a two-phase MCP write gate. No schema migration
and no new CLI command or MCP tool. Read commands gain `--strict`, which adds `meta.strict` and exits
`5` on a truncated success; the two MCP write tools gain dry-run and confirm inputs.

### Fixed

- GSC: a truncated search-analytics walk omits `total` (`null`) instead of presenting the capped row
  count as a full answer; `data_age_days` is floored at the documented 3-day lag.
- Yandex Metrika: `stat/v1/data` pages by `offset` until `total_rows`, a short page or a cap (10,000
  rows / 100 pages); a full page without `total_rows` is `truncated: true`.
- Bing: `data_age_days` comes from the newest dated row in each endpoint; an empty or undated feed
  still reports 0, which is not proof of freshness.
- Yandex Webmaster: an empty indexing or search-URL history returns `rows: []`, `total: 0` and the
  `empty_history` limitation; `degraded_host` is reserved for a host-scoped 404 or `HOST_NOT_LOADED`.
  Pagination totals come only from explicit grand-total keys, never a bare page `count`.
- Crawl: redirect chains are flagged only above two hops; soft 404 requires a not-found phrase and an
  empty body or explicit `404`/`410` marker; robots patterns match linearly with length caps, a `429`
  is retried once with `Retry-After`, and a robots `429` defers the crawl instead of failing open.
- Crawl: sitemap `<loc>` values must share the site origin (`off_origin_loc` otherwise), and Markdown
  output escapes provider-controlled text so a title cannot inject links, HTML or `@` mentions.
- Store: SQLite failures are typed as retryable `STORE_LOCKED`, `STORE_CORRUPT` (byte copy preserved
  as `<db>.corrupt-<utc>` in `detail.backup_path`) and `STORE_UNAVAILABLE`, each with `next_action`.
- `doctor`: a declared but missing or unwritable data directory is fatal (`ok: false`), and
  `meta.data_dir_declared` records whether it was declared.
- Scheduler: repairing an unterminated managed cron block removes only lv24-seo lines, keeps other
  user jobs and warns to review `crontab -l`.

### Changed

- `--strict` on `stats`, `gsc query`, `yandex queries`, `yandex indexing`, `crawl`, `report weekly`
  and `report diff`: a truncated success exits `5` with at least one `meta.limitations` entry;
  without `--strict` truncation still exits `0`. `report diff --markdown` now exits with the envelope
  code.
- Write commands resolve the idempotency ledger from the workspace selected by `--config`, so
  `--config /srv/site/seo.toml` uses `/srv/site/data` from any working directory.
- MCP writes implement the two-phase contract: `allow_write=true`, a dry run returning `plan_hash` /
  `confirm_token` (300 s TTL), an apply that checks the plan hash, and the shared SQLite replay
  ledger; the secret is `LV24_SEO_WRITE_SECRET`. The shipped engine service still returns
  `WRITE_DISABLED`.
- `seo_crawl_site` runs off the event loop on a worker thread with its own store and a 600 s budget;
  other tools keep serving, and a timeout returns a retryable `UPSTREAM_UNAVAILABLE`.
- MCP registry inputs match the real tool signatures, and `schemas/tools.json` is regenerated.

### Infrastructure

- CI adds a wheel-contract job against the installed wheel, Windows and macOS to the offline test
  matrix, pip-audit over the locked runtime set and dependabot; the release workflow requires release
  notes for the tag, and `paths-ignore` no longer skips `skills/*/SKILL.md`.

## [0.5.2] - 2026-11-15

Release notes: [`docs/releases/0.5.2.md`](docs/releases/0.5.2.md).

Bugfix release. No schema migration and no new CLI command or MCP tool. `doctor` gains the additive
`meta.data_dir` and `meta.data_dir_source` fields and may add a warning; every other envelope key is
unchanged.

### Fixed

- Config: a relative `storage.data_dir` written in `seo.toml` is anchored to that file's parent, so
  `--config /srv/site/seo.toml` uses `/srv/site/data` from any working directory. The built-in
  default and `LV24_SEO_DATA_DIR` stay process-scoped.
- `doctor`: reports the effective data directory and its scope (`config` or `working directory`) and
  warns when a relative directory is ambiguous next to a config file. `config show` reports the
  effective absolute path.
- `resolve_config_path` always returns an absolute, normalised path; scheduler artifacts embed an
  absolute `--config` and use the config's parent as the working directory.
- CLI writes: `--confirm` uses the workspace SQLite idempotency ledger, so a replayed confirm token
  returns `skipped`/`duplicate` and the provider is called once. A run without an initialised
  workspace still applies but warns that replay protection is off; a dry run creates no database.
- Sitemap: a fetch failure reports only the exception type, never the URL or query token, in CLI
  warnings, report limitations and MCP payloads.
- GSC: a full final page at the row cap is reported as `truncated: true` with `rows_cap_50000` (or
  the caller-cap warning) instead of claiming complete data. A zero cap is a cap, not an empty set.
- Logging: the redacting handler is installed only when `LV24_SEO_LOG_LEVEL` is set, and only by the
  CLI callback and `lv24-seo mcp serve`. Library imports no longer leave a replaced root handler.

### Changed

- GSC exact-cap results are truncated by design: a full page is not proof that no row exists behind
  the cap.
- Relative `data_dir` values in existing configs now resolve next to the config file; use an
  absolute value or `LV24_SEO_DATA_DIR` to keep the old working-directory behaviour.

### Documentation

- README (EN/RU), CLI, configuration, scheduling, MCP, data sources, architecture, troubleshooting,
  the npm launcher README and `llms.txt` describe the 0.5.2 behaviour.

## [0.5.1] - 2026-11-14

Release notes: [`docs/releases/0.5.1.md`](docs/releases/0.5.1.md).

Bugfix release. No schema migration and no new CLI command or MCP tool. `schemas/tools.json` gains
the additive `writes_local_state` field, and two MCP tools now report `readOnlyHint: false` because
they persist local snapshots.

### Fixed

- Write gate: a claim is stored as `pending`, promoted to `sent` after the provider confirms the
  write, and released when the provider raises. A retry after a provider failure is no longer
  mistaken for a duplicate; a completed or in-flight claim still is.
- Crawl: stored page text is capped at 20,000 characters while `word_count` stays exact for the full
  visible text. No crawl rule reads the retained text.
- MCP: `seo_crawl_site` and `seo_weekly_report` report `readOnlyHint: false` and the new
  `writes_local_state: true`; they write local crawl/weekly snapshots but never provider state and
  still need no confirm token. `destructiveHint` stays `false`.
- Sitemap discovery: the documented 50 MB per-document cap is now reachable through a per-request
  body budget; ordinary pages keep the 5 MB cap and the 50,000-URL protocol cap is unchanged.
- `--days` is capped at 3,660 for `stats` and `report weekly`; larger values fail with
  `INVALID_INPUT` (exit 2) instead of building an unbounded window.
- Weekly report: the builder signature is checked before the call. An incompatible signature gets
  the documented fallback report; a `TypeError` raised inside a compatible builder is no longer
  swallowed and cannot poison a snapshot with a fallback payload.
- GSC: a final batch that exactly fills 50,000 rows is no longer marked truncated; `rows_cap_50000`
  appears only when another row actually exists beyond the cap.
- npm launcher: Windows skips `lv24-seo.cmd` shims, which Node cannot spawn with `shell: false`
  (`EINVAL`), and uses `lv24-seo.exe` or an extensionless binary before falling back to `uvx`.
- MCP: default `gsc_search_analytics` and `seo_opportunities` windows resolve in
  `report.timezone`, not a hardcoded zone.
- Metric keys: multi-dimension keys are JSON-encoded, so values containing `|` no longer collide;
  single-dimension keys are unchanged.
- Store list queries clamp negative limits to `0` instead of passing them to SQLite.
- `report diff --markdown` writes warnings to stderr, keeping stdout valid Markdown.
- Scheduler install replaces every managed block, including a damaged block with a missing end
  marker, instead of appending a duplicate job.
- `auth status` includes Yandex Metrika (eight rows), and `auth set --provider metrika` is accepted.
- `python -m lv24_seo.mcp.registry --write` and `--check` now work as documented.

### Changed

- `--days` above 3,660 is rejected instead of accepted.
- Crawl results retain at most 20,000 characters of page text per page.
- MCP clients that read annotations see `readOnlyHint: false` for the two local-state tools.

### Documentation

- README (EN/RU), MCP surface, CLI, configuration, data sources, architecture, scheduling,
  troubleshooting and the write-gating ADR now describe the 0.5.1 behaviour.

## [0.5.0] - 2026-11-14

Release notes: [`docs/releases/0.5.0.md`](docs/releases/0.5.0.md).

### Added

- `lv24-seo stats --days N --lag-days N --json`: read-only per-provider dashboard metrics for
  GSC, Yandex Webmaster, Yandex Metrika, Bing, CrUX and PSI. Current and previous values where
  comparable, rows, data age, truncation, limitations, warnings, per-service status/error and a
  coverage summary. No store writes and no cross-engine totals.
- Statuses distinguish `ok`, `no_data`, `missing_token`, `not_configured` and `error`, so a
  desktop dashboard can show configuration gaps instead of zeros.
- CrUX returns p75 values with units; PSI returns lab scores and field p75s from the first
  configured URL without averaging; Bing returns clicks/impressions/position; Metrika returns
  visits/users.
- CLI human table for `stats` and documentation of the JSON shape.

## [0.4.0] - 2026-11-13

Release notes: [`docs/releases/0.4.0.md`](docs/releases/0.4.0.md).

### Added

- Product-grade weekly email: localized subject, HTML and 78-column plain text for `ru`, `en`
  and `zh` — a 600px inline-CSS card with KPI cards, top-3 opportunities, issue severities,
  merged sources, data notes/warnings and a property/UTC footer. No external images, stylesheets,
  scripts or tracking pixels; unknown values stay unknown and Metrica is labelled as sessions,
  never clicks.
- `report.locale` accepts `zh` (default remains `ru`). Locale is presentation-only: JSON
  snapshots, `report diff`, MCP payloads and machine codes stay canonical English.
- Current-only KPI rows when a current GSC window exists without a comparable previous window:
  the email shows real current values plus "No comparison" instead of "No data".

### Changed

- Delivery de-duplication includes the normalized render locale in `content_hash`, so switching
  language re-sends the same period once; the English digest stays compatible with existing
  `sends` ledgers, `generated_at` and diagnostics remain excluded.
- CTR movement is shown in percentage points (`pp` / `п.п.` / `个百分点`), not relative percent.
- Email layout is responsive (`width:100%;max-width:600px`), preserves RU NBSP formatting and
  wraps CJK text by display width.

### Fixed

- Raw limitation codes (`rows_cap_50000`, `top_3000_queries`, `no_query_page_join`,
  `sessions_not_clicks`, `sampled`, ...) are mapped to localized sentences; unknown codes are
  labelled as "Other limitation (code)".
- No invented `Previous 0` / `Worse` / "stable" verdict when the previous window is missing.
- No false "No opportunities found" when only metric totals exist without query/page rows.
- Non-weekly reports no longer show the weekly "No previous period to compare" verdict.
- Unknown metric rows no longer dump raw schema fields into the email.
- RU severity labels, ZH source units and the estimate footnote are grammatically correct.

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
