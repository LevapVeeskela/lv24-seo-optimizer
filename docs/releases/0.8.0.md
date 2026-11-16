# 0.8.0 - CWV writer, richer Yandex indicators, safe restore and schedule lifecycle

Status: **released** (2026-11-16). `0.8.0` is published on PyPI, npm and the MCP Registry.
Desktop `0.6.0` is the current desktop release and bundles engine `0.8.0`; its asset names use
`0.6.0` / `-0.6.0-`.

## Added

### Core Web Vitals collection (`cwv collect`)

`lv24-seo cwv collect --url ABS [--url ABS ...] [--device phone|desktop|tablet|all]
[--source crux|psi|both] [--limit 1..50] [--config ABS] [--json]` is the writer behind the weekly
`cwv` section:

* CrUX is queried **per URL and device** (`mobile` is accepted as an alias for `phone`) and every
  returned p75 becomes one `metric_daily` row with `source=crux`, `scope=<phone|desktop|tablet>`,
  `key=<absolute URL>`, `metric` (`LCP`, `INP`, `CLS`, `FCP`, `TTFB`), `value`, the fetch date in
  `report.timezone` and `accuracy=crux_field_28d`;
* PSI rows are returned under `data.lab` and are **never persisted**: the weekly builder treats every
  stored p75 as field data, so a lab score in `metric_daily` would pollute the field series. Every run
  that includes PSI reports `cwv_lab_ignored`;
* PSI supports mobile and desktop only. `tablet` is skipped instead of being silently mapped to
  mobile and is reported as `psi_tablet_unsupported`;
* CrUX returns a 28-day rolling window, so `meta.data_age_days` is at least 2. A low-traffic URL is
  `low_traffic` in `limitations[]` and writes no row — never a zero. Unsupported metrics (for example
  the deprecated FID) are skipped and named in `cwv_metric_unsupported`;
* `--limit` caps URLs (1..50; default 10). `meta.total` counts requested URLs, `meta.processed` the
  URLs inside the cap, `meta.pairs_total`/`meta.pairs_processed` the URL×device pairs, `meta.calls`
  the provider requests actually made. A capped run sets `meta.truncated: true` with `cwv_url_cap`;
  duplicate URLs are queried once;
* CrUX requires a key; a missing key is the standard `AUTH_REQUIRED` envelope with
  `next: lv24-seo auth login --provider crux` before any network call. Persisting field rows also
  requires a store (`lv24-seo init`); a missing store is `CONFIG_INVALID`;
* `report weekly` and `seo_weekly_report` now read stored CWV rows **through today**, not only through
  the provider-lagged traffic window, so a collection appears in the same weekly run.

### Google Search Console service-account authentication

GSC no longer requires a short-lived OAuth access token pasted by hand:

* `providers.gsc.service_account_file` (an absolute path) and
  `LV24_SEO_GSC_SERVICE_ACCOUNT_FILE` are read. The service-account file wins over the existing
  `gsc_token` secret; when both file settings are present, the environment override wins as usual;
* a `gsc_token` value that is a service-account JSON object (`"type": "service_account"` plus
  `private_key`) is also accepted; a plain token keeps the historical `Authorization: Bearer`
  behaviour and is not refreshed;
* the adapter signs an RS256 JWT (`iss`, `scope=webmasters.readonly`, `aud`, `iat`, `exp`), exchanges
  it at the key's `token_uri` with the JWT-bearer grant, and caches the access token, refreshing it
  60 seconds before expiry. `GscClient` asks the provider for a token on every request, so a
  long-lived MCP process survives expiry. The file is parsed once per process;
* `properties` reports `token_source: service_account_file` and marks GSC ready; `doctor` adds a
  non-fatal `gsc_auth` check. Neither prints the JSON or the private key. `auth status` stays
  keychain/env/file-secret only and does not inspect the file;
* a relative, missing, unreadable or invalid file is `CONFIG_INVALID` with a next action; a rejected
  token exchange is `AUTH_INVALID` and never echoes the response body;
* `cryptography>=43` is now a required dependency (Apache-2.0 OR BSD-3-Clause) for RS256 signing.
  `schemas/tools.json` and the MCP surface stay unchanged at **12 tools**;
* `properties` gains the additive `token_source` field (`service_account_file`, `secret` or
  `missing`); no existing field changed.

### Richer Yandex Webmaster indicators

`yandex queries` and the `yandex_query_performance` MCP tool keep more of the documented indicator
set:

* `AVG_CLICK_POSITION` is exposed as `avg_click_position` alongside `shows`, `clicks` and `position`;
* `query_id` is returned when the provider sends it;
* an indicator the client does not map is preserved under `extra_indicators` instead of dropping the
  whole row. The legacy list form of `indicators` is still accepted;
* `yandex indexing` and `yandex_indexing_state` merge the documented `indexing/history` indicator
  series (`HTTP_2XX`, `HTTP_3XX`, `HTTP_4XX`, `HTTP_5XX`, `OTHER`, upper- or lowercase) into one row
  per date (`http_2xx`, `http_3xx`, `http_4xx`, `http_5xx`, `other`). An omitted series stays absent;
  unknown series are kept under `extra_indicators`;
* `summary` now carries `site_problems` (severity → count: `FATAL`, `CRITICAL`, …) next to the
  existing counters. A counter the provider omits stays absent rather than becoming `0`.

### Non-destructive archive extraction (`backup restore --to DIR`)

`lv24-seo backup restore --file ABS --to ABS_DIR [--json]` unpacks an archive into a **new**
directory and never touches the current workspace:

* the archive is re-validated exactly like the in-place plan: manifest, member set, member names,
  sizes and every SHA-256;
* the target must be an absolute path that does not exist, with an existing parent. An existing,
  non-empty or symlinked target is `INVALID_INPUT` and is never merged or overwritten;
* members with absolute paths, `..` segments, backslashes or symlinks are rejected before extraction
  (zip-slip and link escapes are refused, not sanitized);
* the database passes `PRAGMA quick_check` both before and after it is moved into place, and the
  staging directory is removed;
* an archive without a database yields a config-only directory with `database.in_archive: false`,
  `database.restored: false`, `database.path: null` and an explicit warning — no empty database is
  invented. A failure removes only the directory this call created;
* no `--allow-write` and no confirm token are involved because no pre-existing file is modified.
  Passing `--allow-write` or `--confirm` with `--to` is `INVALID_INPUT`, and `--to` with `--config`
  is refused.

The two-phase in-place restore (`--config`, `--allow-write --confirm TOKEN`) is unchanged.

### Scheduler lifecycle (`schedule install|status|uninstall`)

* `schedule install` is **plan-only by default**: it prints the cron block, the systemd user units or
  the Task Scheduler XML plus the command it would run, with `dry_run: true` and `installed: false`.
  Nothing is written until `--allow-write`; `--print` remains an alias for the default and wins over
  `--allow-write`. Generated jobs embed the absolute `--config` path and run from the config
  directory;
* `schedule status` is read-only and reports `state: installed|outdated|partial|missing|unknown`, the
  expected `paths` and per-artefact `exists`/`matches`. `unknown` means the host scheduler could not
  be inspected (missing `crontab`/`schtasks`, permissions) and `matches` is then `null`;
* `schedule uninstall` is plan-only by default and, with `--allow-write`, removes **exactly** the
  generated artefacts: its own managed cron block, the two `<job>.service`/`<job>.timer` user units or
  the named Task Scheduler task. A second run is a no-op (`removed: false`, `state: missing`), and
  unrelated cron lines and tasks are never touched;
* a cron block that lost its end marker is repaired conservatively: only lines that look like
  lv24-seo entries are removed, everything else is kept, and the envelope warns so you can review
  `crontab -l`.

### Fixes

* Yandex Metrika `stat/v1/data` now sends the documented **1-based** `offset`; the previous `0` was
  rejected with HTTP 400 on every call, which made the Metrika dashboard card look configured but
  empty. Pagination advances by the rows already returned.
* CLI `--json` output is ASCII-escaped, and human output degrades unencodable glyphs instead of
  aborting, so a completed report redirected on a legacy Windows codepage (cp1251/cp866) no longer
  fails with `UnicodeEncodeError` while printing a `⚠` warning.

## Changed

* The CLI now has **22 top-level commands**: 15 groups (`auth`, `config`, `gsc`, `yandex`, `sitemap`,
  `indexnow`, `report`, `cwv`, `schedule`, `export`, `logs`, `tasks`, `backup`, `portfolio`, `mcp`)
  and 7 commands (`version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`).
* The MCP surface is unchanged at **12 tools** (10 read-class tools and 2 gated provider writes),
  3 resources and 3 prompts; no tool name, input or output field changed.
* The SQLite schema is unchanged at **version 4**; `schemas/tools.json` is unchanged. No migration is
  applied by this release.
* `schedule install` no longer writes on a bare invocation; scripts that relied on the old implicit
  install must add `--allow-write`.
* Desktop `0.6.0` is the current desktop release and bundles engine `0.8.0`. The previous desktop
  release, `0.5.0`, bundled engine `0.7.0`.

## Verification

The four feature waves merged into `main` at `b9bcf4e` with the full local gate green:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run pytest -q
uv run lv24-seo --help          # 22 top-level commands
uv run lv24-seo cwv collect --help
uv run lv24-seo backup restore --help
uv run lv24-seo schedule status --help
```

Focused checks for the merged waves:

```bash
uv run pytest -q tests/unit/test_cwv_writer.py
uv run pytest -q tests/unit/test_provider_gsc_service_account.py
uv run pytest -q tests/unit/test_backup_restore.py
uv run pytest -q tests/unit/test_schedule_lifecycle.py
uv run pytest -q tests/unit/test_provider_yandex_webmaster.py
```

Release artifacts, runs and installed-app checks:

| Check | Result |
|---|---|
| PyPI `lv24-seo` | `0.8.0` published |
| npm `@lv24/seo` | `0.8.0` published |
| MCP Registry `io.github.LevapVeeskela/lv24-seo` | `0.8.0` published |
| CI matrix (Ubuntu, Windows, macOS) | run `43962847574` green |
| Release workflow | run `43964671274` green |
| Desktop `0.6.0` | published, bundles engine `0.8.0`; assets named `0.6.0` / `-0.6.0-` |
| Windows in-app update `0.5.0` → `0.6.0` | Rust updater: UI Desktop 0.6.0 / Engine 0.8.0, engine ready, coverage 6 configured / 3 returned / 0 errors / 1 missing |

### Real workspace check (`lv24.by`)

The published build was run against the configured `lv24.by` workspace:

* GSC: 17 clicks (+1600%), 1941 impressions (+2558.9%), CTR 0.88% (-0.49 pp), average position
  16.1 (-4.1);
* Yandex Metrika: 40 visits / 12 users;
* PSI: performance 41%, SEO 100%;
* Yandex Webmaster: 2754 pages in search, 68 queries; no fresh window data (age 41d);
* CrUX: no URL-level field data for the configured URLs;
* Bing: token still missing, so the Bing adapter stayed unconfigured.

The provider caps and ages above are reported through `limitations[]` and `meta.data_age_days`; the
workspace is a live site, not a fixture.

## Known limitations

* `cwv collect` needs a CrUX API key; without one it returns `AUTH_REQUIRED` before any network call.
  It also needs a local store, because the point of the command is to persist field rows.
* PSI is lab data and is never stored; `--source psi` therefore cannot populate the weekly `cwv`
  section by itself. PSI has no `tablet` form factor, so `tablet` is skipped and reported.
* The CWV fetch date uses `report.timezone`, not UTC. A collection near midnight can land on a
  different date than a UTC-based consumer expects.
* `schedule status` is file-level: it compares the generated artefacts with the current spec and does
  not ask the host scheduler whether a job is enabled, last ran or succeeded.
* On Windows, a task edited outside `schedule install` may still show as `outdated` because the
  comparison is against the generated XML, not the live task definition.
* `backup restore --to` extracts files and never merges rows or settings; provider tokens are not in
  the archive, so re-authenticate with `lv24-seo auth set` afterwards.
* Yandex indicator coverage follows what the provider returns for the configured host; a missing
  indicator stays absent (`null`) rather than becoming zero, and `extra_indicators` is not a stable
  schema across API versions.
