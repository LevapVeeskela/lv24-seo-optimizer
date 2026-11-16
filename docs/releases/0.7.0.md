# 0.7.0 - portfolio, restore, indexing events and CWV by template

Status: **released** (2026-11-16). This document describes the shipped `0.7.0` behavior. `0.7.0` is
the latest build on PyPI, npm and the MCP Registry, and desktop `0.5.0` bundles engine `0.7.0`.

## Added

### Multi-site portfolio (`portfolio list`)

`lv24-seo portfolio list --root ABS [--limit 1..50] [--json]` scans sibling project folders and
returns each site's local storage state and last activity without opening a provider, reading a
token or writing to disk:

* `--root` must be an absolute, existing directory. The scan looks for `seo.toml` up to three
  levels below it and skips hidden, `node_modules`, `__pycache__`, `venv` and symlinked directories;
* at most 50 sites are inspected (`--limit` 1..50, default 50). Discovery still counts every
  config, so a capped scan returns `meta.truncated: true`, the real `total` and a limitation;
* each `data.sites[]` row carries `config_path`, `name`, `property`, `base_url`, `data_dir`,
  `db_path`, `db_present`, `crawl_runs`, `last_crawl_at`, `report_snapshots`, `last_report_at`,
  `last_report_period`, `issues_open`, `issues_total`, `issues_critical`, `last_issue_at`,
  `last_activity_at` and `error`. Rows are newest activity first, then by config path;
* a broken TOML file or unreadable database becomes a per-site `error` (`CONFIG_INVALID`,
  `STORE_LOCKED`, `STORE_CORRUPT`, `STORE_UNAVAILABLE`); the rest of the portfolio still scans;
* configs are loaded with an empty environment, so an ambient `LV24_SEO_DATA_DIR` cannot redirect
  every site. SQLite is opened with `immutable=1` for a quiescent file or `mode=ro` for a live WAL;
  a pending rollback journal or an uncheckpointed WAL without `-shm` is reported as unavailable
  rather than opened read-write.

`portfolio list` is CLI-only in this release. The scan lives in `core/portfolio.py` so the desktop
and a future MCP tool can reuse the same contract; no MCP tool was added.

### Two-phase project restore (`backup restore`)

`backup create` already produced a portable zip. `backup restore` now validates and applies it:

```bash
# phase 1: validate and plan; writes nothing outside a temporary directory
lv24-seo backup restore --file /absolute/backup.zip --config /absolute/seo.toml --json

# phase 2: apply with the plan's token
lv24-seo backup restore --file /absolute/backup.zip --config /absolute/seo.toml \
  --allow-write --confirm TOKEN --json
```

* the plan reports `status: planned`, the archive and manifest hashes, the target config/data
  directory, current and archived database hashes, the config action (`create`, `replace` or
  `write_alongside`) and a `confirm_token` with `expires_at`/`ttl_seconds` (300 seconds);
* applying needs both `--allow-write` and the token. `--allow-write` alone only plans and warns; a
  token without `--allow-write` is `WRITE_DISABLED`;
* the token is bound to the archive bytes and the target state. A changed archive, a changed
  workspace, an expired token or a replay is `CONFIRM_REQUIRED`;
* the database is restored to `<config dir>/data/seo.sqlite3`; an archived config whose `data_dir`
  points elsewhere is `INVALID_INPUT`;
* before replacement the current database is copied to `<db>.pre-restore-<utc>` (and the current
  config to `seo.toml.pre-restore-<utc>` when it is replaced). Every member hash is re-verified,
  the restored database passes `PRAGMA quick_check`, and files are replaced atomically;
* a config that does not validate is written alongside as `seo.toml.restored-<utc>`, leaving the
  existing config usable.

Restore replaces files; it never merges rows or settings. Provider tokens are not in the archive.

### Schema v4: `indexing_events`

Migration **v4** adds the `indexing_events` table, keyed by
`(provider, property_id, url, event, event_date)`. `yandex indexing` (and the MCP
`yandex_indexing_state` call) persists provider-reported per-URL events whenever a store exists:

* a row is stored only with an absolute URL, an event name and an event date. Daily aggregate
  history rows have no per-URL identity and are skipped instead of being assigned a guessed URL;
* a replay updates `last_seen`/`detail_json` and never duplicates the event; `first_seen` is
  immutable;
* `meta.indexing_events` reports `new`, `existing`, `skipped` and `truncated` counts. At most 500
  provider rows are considered per call; a limitation is added when rows were dropped or the event
  read was capped;
* storage failures become warnings, never a failed provider read.

The weekly digest now reads these events inside the two compared windows, so `indexing_event`
evidence no longer depends on a caller supplying rows.

### Core Web Vitals by template in `report weekly`

When the store holds CrUX/PSI **field** rows for the compared windows, `report weekly` (and
`seo_weekly_report`) adds a `cwv` section:

* rows are grouped by normalized URL template (`/blog/{id}`) and device (`phone`, `desktop`,
  `tablet`); the section shows at most the top 5 templates — regressed first, then the largest
  relative regression, then poor-metric count and worst status;
* each row carries template, device, URL count, sample/compared counts, core-metric coverage,
  per-metric `p75` with `status`, previous p75, delta and `regressed`, plus the template's worst
  regression and `poor_metrics`;
* a regression is a p75 increase of at least 5% (above 0.01 for CLS). A template p75 is the worst
  page p75 in that group, not a site-wide average (`cwv_p75_worst_page`);
* origin aggregates, lab/PSI scores and rows without a URL or known device are never merged into a
  template; they are reported through `cwv_template_missing`, `cwv_device_missing`,
  `cwv_lab_ignored` and `cwv_values_ignored`;
* missing previous p75 yields `cwv_no_comparison` or `cwv_partial_comparison`, never an invented
  delta. The section carries `total`, `truncated` and `data_age`.

The section is opt-in for the caller: a report built without stored CWV rows keeps the historical
section list, and no provider is called to build it.

## Changed

* The CLI now has **21 top-level commands**: 14 groups (`auth`, `config`, `gsc`, `yandex`,
  `sitemap`, `indexnow`, `report`, `schedule`, `export`, `logs`, `tasks`, `backup`, `portfolio`,
  `mcp`) and 7 commands (`version`, `doctor`, `init`, `properties`, `stats`, `crawl`, `install`).
* Schema version is **4**. Migration 4 is additive and is applied on the next open; no user data is
  rewritten or deleted.
* The weekly digest can now use stored indexing events; its evidence is capped at 500 issues, 500
  pages and 500 indexing events per report, with a limitation when a cap is hit.
* `report weekly` snapshots and `seo_weekly_report` payloads may now include the additive `cwv`
  section. Existing sections and envelope keys are unchanged.
* The MCP surface stays at **12 tools**, 3 resources and 3 prompts; no new tool and no changed input.
  `yandex_indexing_state` now carries `writes_local_state: true` / `readOnlyHint: false` because it
  persists provider events when a workspace exists, so the surface is 9 read-class tools (7 pure
  read-only plus 3 local-state writers) and 2 gated provider writes. Portfolio and restore are
  CLI-only.
* Desktop `0.5.0` is the current desktop release and bundles engine `0.7.0`; desktop `0.4.0`
  bundled `0.6.0`.

## Verification

The coverage wave merged as PR #7 (`6d5879c`) with the Ubuntu, Windows and macOS CI matrices green.
The release commit `00e85aa` (PR #8) passed the same CI matrix, and the tag-triggered `release`
workflow ran green. The full gate on the release commit:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python -m lv24_seo.mcp.registry --check
uv build
```

Focused checks for this wave:

```bash
uv run pytest -q tests/unit/test_portfolio.py tests/unit/test_backup_restore.py
uv run pytest -q tests/unit/test_analysis_cwv.py tests/unit/test_store_indexing_events.py
uv run pytest -q tests/unit/test_services.py tests/unit/test_report_builders.py
uv run lv24-seo --help          # 21 top-level commands
uv run lv24-seo backup restore --help
uv run lv24-seo portfolio list --help
```

Published artifacts verified after the release workflow:

* PyPI wheel and sdist report `lv24-seo 0.7.0`, and `uvx --from lv24-seo lv24-seo version` prints it;
* npm launcher `@lv24/seo 0.7.0`;
* MCP Registry `io.github.LevapVeeskela/lv24-seo` `0.7.0` (`isLatest: true`);
* desktop `0.5.0` bundles engine `0.7.0`.

## Known limitations

* `portfolio list` is read-only and CLI-only. It reports local history only: it never calls a
  provider, so it cannot show remote index status or traffic for a site whose local store is empty.
* The portfolio scan is capped at 50 sites per run; larger agencies must scan narrower roots.
* `backup restore` replaces files and never merges rows or settings. Provider tokens are excluded,
  so re-authenticate with `lv24-seo auth set` afterwards. A database held open by another CLI or
  desktop process cannot be replaced on Windows.
* The restore confirm token prevents accidental or stale applies; it is not access control —
  anyone who can run the CLI can restore.
* Indexing events come only from Yandex Webmaster indexing calls. Rows without a URL, event name or
  event date are skipped, and each call considers at most 500 provider rows.
* The CWV section needs stored CrUX/PSI field rows; it does not fetch metrics itself and does not
  appear in a report built without them. It shows at most five templates, and its p75 is the worst
  page p75 per template, not a site-wide aggregate.
* CWV and portfolio data are not attributed in the change digest; the digest explains GSC headline
  deltas only.
