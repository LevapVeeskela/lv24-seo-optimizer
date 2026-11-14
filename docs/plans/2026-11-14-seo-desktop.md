# LV24 SEO Optimizer Desktop — implementation plan (2026-11-14)

Status: **plan only**, not implemented. Target repository:
`LevapVeeskela/lv24-seo-optimizer-desktop` (public, Apache-2.0).

## 1. Goal

A public, offline-first desktop control plane for `lv24-seo` with:

- convenient, validated settings for project, providers, reports, email/Telegram, crawl and storage;
- a first-class **configuration output** area: effective redacted config, raw TOML, diff, env
  overrides, validation, doctor checks and diagnostics;
- product-grade report views (weekly, diff, crawl, indexing/opportunities) instead of raw JSON;
- safe credential entry and explicit confirmation for every write/destructive action;
- Windows, macOS and Linux builds from free GitHub-hosted runners.

The desktop is a **UI + local process shell**. It does not reimplement SEO logic or provider APIs;
the Python `lv24-seo` package remains the engine of record.

## 2. Non-goals (v0.1)

- No cloud sync, accounts, telemetry or hosted backend.
- No SERP scraping and no paid SEO provider.
- No new provider adapters or analysis algorithms in the desktop.
- No automatic writes to sitemaps, recrawl queues or IndexNow without an explicit confirmation.
- No macOS notarization / Windows code signing until the owner approves a paid certificate
  (zero-spend constraint); unsigned builds must be labelled honestly.
- No mobile or web-hosted version.

## 3. Stack (same as `lv24-brain-desktop`)

| Layer | Choice |
|---|---|
| Shell | Tauri v2 (Rust) |
| UI | React 19 + TypeScript + Vite 8 |
| State | MobX 7 + `mobx-react-lite` |
| Architecture | Feature-Sliced Design: `app / pages / widgets / features / entities / shared` |
| Tests | Vitest + React Testing Library; Rust unit tests |
| i18n | typed dictionaries `en`, `ru`, `zh` |
| Theme | LV24 dark semantic tokens, WCAG AA, reduced motion |
| Node | 24 (CI), npm |
| Rust | stable, `rustfmt` + `clippy -D warnings` |

No new UI framework, CSS-in-JS or state library. Reuse the brain desktop primitives and token
discipline where they fit.

Tauri plugins: `dialog` (file/folder picker), `opener` (open report/folder), `updater`, `log`,
`single-instance` (one desktop per user; no deep-link feature). Rust crates: `toml_edit`, `sha2`,
`tempfile`, `tokio`, `serde`, `thiserror`, `log`. No `reqwest`: provider HTTP stays inside the
Python CLI.

## 4. Architecture

```
┌──────────────────────────────────────────────┐
│ Webview: React + TS + MobX + FSD             │
│  settings forms · config output · reports    │
│  invoke("config_read") · invoke("run_cli")   │
└───────────────────────┬──────────────────────┘
                        │ Tauri IPC (allow-listed commands)
┌───────────────────────▼──────────────────────┐
│ Rust shell                                   │
│  config read/validate/write (toml_edit)      │
│  CLI runner (args array, stdin, timeout)     │
│  job manager + cancel + events               │
│  path allow-list · secret stdin · updater    │
└───────────────────────┬──────────────────────┘
                        │ process
┌───────────────────────▼──────────────────────┐
│ lv24-seo Python CLI (bundled sidecar)        │
│  --json envelopes · reports · crawl · MCP    │
│  keychain/env/file secrets · SQLite history  │
└──────────────────────────────────────────────┘
```

Key decision: **no local HTTP API in v0.1**. The CLI already exposes stable `--json` envelopes;
adding a loopback server would duplicate auth/proxy work. If long-running progress or multi-client
access is needed later, add an opt-in local API behind the same Rust bridge.

### Sidecar resolution

1. `LV24_SEO_BIN` (absolute path, dev/advanced);
2. bundled resource `engine/lv24-seo[.exe]`;
3. `<app-dir>/engine/lv24-seo[.exe]`;
4. `PATH`;
5. dev fallback `uvx --from lv24-seo==<pinned> lv24-seo` (explicitly labelled as a dev fallback).

The desktop shows both versions (desktop and engine) and refuses incompatible major versions.
Release builds bundle a checksum-pinned sidecar; the webview never downloads or executes code.

### Reuse vs drop from `lv24-brain-desktop`

Reuse: Tauri builder/setup, strict CSP, `settings.rs`, sidecar resolution shape, CLI execution
patterns (piped stdout/stderr, bounded capture, `CREATE_NO_WINDOW` on Windows), updater flow,
FSD layout, MobX generation guards, i18n parity tests, UI primitives, theme tokens/contrast tests,
CI and manual desktop workflow.

Drop for v0.1: daemon supervisor/backoff, tray, close-to-tray, autostart, deep links/OAuth,
local HTTP API proxy/SSE, runtime token, connection wizard and the brain domain model. SEO is a
windowed, one-shot CLI control app; a generic `cli_run` with an allow-listed command table and
cancellation is enough.

## 5. Repository layout

```
lv24-seo-optimizer-desktop/
  src/
    app/            RootStore, providers, router, layout, global.css
    pages/          onboarding, dashboard, project, providers, report-settings,
                    crawl-settings, storage, config, reports, crawl, queries,
                    automation, settings, diagnostics
    widgets/        app-header, app-nav, health-strip, report-summary, config-panel
    features/       save-config, validate-config, set-secret, run-report, run-crawl,
                    schedule-preview, mcp-install, export-skills, update-check
    entities/       config, provider, secret, report, crawl, schedule, job, settings
    shared/         cli envelope, tauri transport, i18n, UI primitives, format/errors
  src-tauri/
    src/commands.rs, config.rs, cli.rs, jobs.rs, paths.rs, secrets.rs,
        settings.rs, updater.rs, error.rs
  contracts/
    cli-v1/         recorded JSON envelope fixtures from lv24-seo 0.4.0
    config.schema.json
  scripts/          build-sidecar.py, fetch-sidecar.mjs, verify-sidecar.mjs,
                    gen-contracts.py
  docs/             architecture, security, release, configuration
  .github/workflows/ci.yml, desktop.yml
```

## 6. Desktop ↔ CLI contract

All calls use an argument array (never a shell string), an absolute `--config` path and `--json`.
The Rust layer parses one envelope:

```ts
type Envelope<T> =
  | { ok: true; data: T; meta: Record<string, unknown>; warnings: string[] }
  | { ok: false; error: { code: string; message: string; next_action?: string; retryable?: boolean };
      meta: Record<string, unknown>; warnings: string[] };
```

The shell exposes one generic `cli_run { command, args[], allowWrite }` command backed by a
**fixed allow-list of command ids and argument schemas**. The webview cannot pass an executable,
a shell string, a config path outside the approved roots, or a write flag for a read command.
`cli_cancel { runId }` kills the exact child process (never a wildcard process name). Long jobs
emit `run-log` (line + stream) and `run-end` (exit code) events.

Contract rules:

- additive fields are tolerated; unknown `meta` keys are preserved for diagnostics;
- `ok=false` is a typed UI error, not a crash;
- stdout is JSON only; stderr is bounded and redacted before display;
- exit code is shown in diagnostics but the envelope is authoritative;
- every command has a timeout; long jobs are cancellable;
- child env sets `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` so Russian/Chinese paths and text
  survive Windows code pages;
- Windows child processes use `CREATE_NO_WINDOW`; stdout/stderr are captured as UTF-8 with a
  bounded buffer.

| Desktop command | CLI | Read/write | Notes |
|---|---|---|---|
| `desktop_status` | `version --json` | read | desktop/engine versions, paths |
| `config_read` | `config show --json` | read | effective redacted config |
| `config_init` | `init --property ... --data-dir ...` | write | creates `seo.toml` + data dir |
| `config_validate` | `config show --config <temp> --json` | read | validate a draft before replace |
| `doctor` | `doctor --json` | read | health checks, no network |
| `providers` | `properties --json` | read | configured/ready/token presence |
| `auth_status` | `auth status --json` | read | secret presence/source only |
| `auth_set` | `auth set --provider <p> --stdin` | write | secret via stdin, never argv |
| `auth_delete` | `auth logout --provider <p> --json` | write | confirm dialog |
| `gsc_query` | `gsc query --start ... --end ... --json` | read | bounded rows |
| `yandex_queries` | `yandex queries --json` | read | top queries |
| `yandex_indexing` | `yandex indexing --json` | read | history/problems/events |
| `crawl_run` | `crawl --start ... --max-urls ... --json` | write to SQLite | cancellable |
| `report_weekly` | `report weekly --days ... --lag-days ... --json` | read/write snapshot | optional `--out` |
| `report_send` | `report weekly --send --json` | external send | explicit confirm |
| `report_diff` | `report diff --base ... --head ... --json` | read | default last two |
| `schedule_preview` | `schedule print --kind ... --json` | read | no host change |
| `schedule_install` | `schedule install --kind ... --json` | host write | explicit confirm |
| `mcp_install` | `install --client ... --json` | host write | explicit confirm |
| `export_skills` | `export skills --out ... --json` | write files | path picker |
| `export_tools` | `export tools --out ... --json` | write files | optional |

### Contract gaps to close in `lv24-seo` before/while building the desktop

P0 prerequisites (small, additive CLI changes):

- `report weekly --no-save` and `crawl --no-save` (or `--dry-run`): a desktop preview must not
  create snapshots/pages/issues. Today both persist whenever the store exists.
- `report list --json`: snapshot periods/hashes for the report-history screen. v0.1 can ship
  without it (use the default last-two diff) but M3 needs it.
- `schedule install` must be able to target the desktop's bundled sidecar, e.g. `--bin <path>`
  or honoring `LV24_SEO_BIN`; today it resolves `shutil.which("lv24-seo")`, which may not exist
  for a packaged app.
- `install --client` should accept an explicit command/binary for the bundled sidecar (or the
  desktop writes the MCP client JSON itself); the current `uvx` command assumes uv on PATH.
- `report weekly --send` should expose per-channel delivery results in `meta` (sent/skipped/failed
  and reason), not only a boolean; the desktop must show which channel failed.
- `config resolve --json` for per-key source (`default/file/env`); MVP fallback shows the known
  env keys plus merged `config show`, labelled "detected" rather than exact provenance.
- `report render --locale` to preview an existing report in another language without re-collecting;
  v0.1 shows the stored report and links to Report settings.
- Optional `--progress ndjson` on `crawl`/`report weekly` for determinate progress. Until then the
  desktop shows phases and an indeterminate state, never fake percentages.
- Normalize `doctor` checks: success puts them in `data.checks`, failure in `meta.checks`; the
  desktop parser must accept both. `healthy` currently only requires `python` and `cwd_writable`,
  so the UI must display each check, not just the boolean.
- `report diff --markdown` bypasses the envelope; the desktop uses `--json` and renders markdown
  from `data.markdown`.
- `mcp serve` is not part of the generic `cli_run` path; if the desktop starts it, use a dedicated
  job and loopback-only HTTP.

### Contract hazards the desktop must handle

| Hazard | Desktop behavior |
|---|---|
| `WRITE_DISABLED` / `CONFIRM_REQUIRED` / config errors all exit `2` | branch on `error.code`, never exit code alone |
| `doctor` shape differs by outcome | parse `data.checks` or `meta.checks` |
| `auth set` JSON vs plain output | always pass `--json`; read the envelope |
| `install --json` returns the full merged host config, which may contain other servers' tokens | never log/persist it; show only client/path/written |
| `schedule install` warnings live in `data.warnings` | merge both `data.warnings` and envelope `warnings` |
| relative `storage.data_dir` resolves against process cwd | launch with `cwd = config parent` and prefer an absolute data dir |
| `report weekly --out` overwrites `report.json`/`report.html` and saves a snapshot | use a dedicated output dir and `--no-save` for preview |
| `crawl` issues are uncapped in CLI | virtualize/lazy-render the table, show counts |
| `sitemap`/`recrawl` execute paths are not wired | show `WRITE_DISABLED` honestly; do not fake success |
| IndexNow/confirm tokens in argv are visible in process listings | keep command lines short-lived; prefer stdin support when added |
| no concurrent jobs on one data dir | serialize all jobs per workspace; SQLite writer + delivery can block |


### Contract fixtures and schema

`scripts/gen-contracts.py` (CI/build-time, not shipped) imports the pinned `lv24-seo` wheel and
writes:

- `contracts/config.schema.json` from `SeoConfig.model_json_schema()`;
- `contracts/cli-v1/*.json` by running the CLI against a temporary workspace for `version`,
  `config show`, `doctor`, `properties`, `auth status`, a no-data `report weekly` and a
  `report diff` (or a recorded snapshot fixture).

`npm run contract:check` regenerates and fails on diff. The TS parser tests consume the fixtures,
so a CLI field change is caught before release. The desktop pins a minimum `lv24-seo` version and
shows a compatibility error instead of guessing.

## 7. Settings UX

### 7.1 Navigation

`Overview · Project · Providers · Report · Crawl · Storage · Configuration · Reports · Crawl run ·
Data · Automation · Settings`

Concrete route map:

| Group | Screen | Route | Primary action |
|---|---|---|---|
| Overview | Dashboard | `/` | Run weekly, Crawl, open last report |
| Workspace | Setup | `/setup` | locate engine → project folder → init → doctor/providers |
| Workspace | Project | `/project` | save project identity |
| Workspace | Providers | `/providers` | test connection, set/remove secret |
| Reports | Latest | `/reports` | open, preview HTML, export, diff |
| Reports | Run weekly | `/reports/run` | build only / build and send |
| Reports | Diff | `/reports/diff` | pick periods, copy Markdown |
| Reports | Report settings | `/reports/settings` | locale/timezone/thresholds/email/Telegram |
| Data | Crawl | `/crawl` | start/cancel, results, settings |
| Data | Queries | `/queries` | GSC/Yandex/indexing/properties |
| Automation | Schedule | `/automation/schedule` | preview/install |
| Automation | MCP & agents | `/automation/agents` | preview/install client JSON |
| Automation | Skills | `/automation/skills` | export skills |
| System | Configuration | `/config` | effective JSON/TOML/diff/env/validation/doctor |
| System | Storage | `/storage` | open data dir, inspect SQLite |
| System | Diagnostics | `/diagnostics` | doctor, logs, support bundle |
| System | About | `/diagnostics/about` | versions, updates, licenses |

### 7.2 Settings pages

- **Project**: name, base URL, default property, data directory; path picker; "Open folder".
- **Providers**: one card per GSC / Yandex Webmaster / Yandex Metrika / Bing / CrUX / PSI.
  Fields: enabled/configured, target (`property`, `host_id`, `counter`, `site`, `origin`, `urls`),
  token presence/source, "Set token", "Delete token", "Test" (`properties`), docs link.
- **Report**: UI/report locale `ru|en|zh`, timezone, week start, thresholds, email enabled/to/from/
  SMTP host/port/user, Telegram enabled/chat id/severity; template preview; "Send now" is a
  separate confirmation flow.
- **Crawl**: max URLs, respect robots, user agent; `concurrency` shown as reserved/disabled with an
  explanation, not a fake control.
- **Storage**: data dir, raw retention shown as reserved; SQLite path; backup/open folder.
- **Settings**: UI language, theme, update check, config path, diagnostics.

### 7.3 Save model

- Structured forms edit a draft model; a sticky bar shows **dirty**, **valid**, **warnings**.
- Save flow: build a TOML patch with `toml_edit` (preserve comments/order) → write temp file →
  `lv24-seo config show --config <temp>` validation → backup `<seo.toml>.bak-<timestamp>` (keep
  the last 5) → atomic replace.
- Raw TOML mode is an advanced tab with parse/validate; saving raw mode is allowed only after the
  same validation. Switching modes with a parse error is blocked.
- Conflict detection: compare mtime + hash at load vs save; on change offer Reload / Show diff /
  Overwrite. Handle Windows file locks with a retry message.
- "Reset to defaults" and "Import" require confirmation; "Export" writes the selected file.
- Environment overrides are shown as read-only badges with the variable name (values never shown);
  the UI explains that env wins over the file. The GUI sees its own process environment; a
  variable exported only in a terminal may not be visible here while still affecting CLI runs.

Validation ladder:

1. TOML syntax (instant, local, line/column);
2. engine schema via `config show --config <draft>` (debounced; strict unknown-key rejection);
3. semantic warnings: no provider target, email enabled without recipients/host, Telegram enabled
   without chat, enabled channel with missing secret, data dir missing/unwritable, invalid
   timezone, PSI list empty, locale-switch resend, reserved fields changed;
4. save-time revalidation + on-disk conflict check.

Reserved or unused fields must be labelled, not exposed as working controls:

| Field | Status |
|---|---|
| `providers.gsc.auth` | reserved label; credentials come from the secret store |
| `providers.yandex_metrika.goals` | parsed but unused |
| `report.week_start` | reserved; report windows use `compare_period` |
| `report.telegram.severity` | parsed but unused by delivery |
| `crawl.concurrency` | reserved; crawler is serial |
| `storage.raw_retention_days` | ignored; no raw archives are written |

`report.locale` defaults to `ru` and is independent from the desktop UI language. The UI language
is a desktop preference; the report language is a project setting.

## 8. Configuration output

A dedicated **Configuration** page with tabs:

1. **Form** — structured settings.
2. **Effective JSON** — `config show --json`, redacted, copy/download.
3. **TOML** — raw file, syntax-highlighted, copy/download; edit in advanced mode.
4. **Diff** — saved vs draft, field-level, destructive changes highlighted.
5. **Validation** — schema errors, CLI errors, unknown keys, deprecated/reserved fields.
6. **Environment** — set `LV24_SEO_*` names, override status, secret source, no values.
7. **Diagnostics** — doctor checks, desktop/engine versions, config path, data dir, SQLite path,
   Python/sidecar path, `config show` metadata, copy diagnostics bundle (redacted).

Every output has a copy button and a "Reveal file" action (allow-listed). The page must never show
token values, SMTP passwords or bot tokens.

The diagnostics bundle is a user-triggered JSON/Markdown export containing versions, paths, doctor
checks, redacted effective config, env variable names and recent non-secret job errors. It is
never uploaded automatically. The MCP client JSON shown in Automation contains only the
`lv24-seo` entry; `install --json` may return the whole merged host config with other servers'
tokens, so the desktop must extract/display only its own entry and never log the full document.

## 9. Reports and data UX

- **Dashboard**: health strip (config/data/tokens/doctor), configured providers, latest report
  summary, `complete`/`truncated`/`limitations`, next schedule, quick actions.
- **Report weekly**: native React summary from `report.json` (KPI cards, opportunities, issues,
  sources, notes) plus a sandboxed HTML preview of `report.html`; locale switch for preview;
  "Open in browser"; "Send" with confirmation; show `meta.sent`, warnings and failed channels.
- **Report diff**: base/head selectors when history exists; Markdown/structured diff; copy for PR.
- **Crawl run**: start/cancel, phase status, summary, issues table, `sitemap_coverage` buckets with
  exact counts and limitations; no fake progress.
- **Data queries**: GSC/Yandex tables with bounded rows, `truncated`/`total`/`data_age` and
  `limitations` visible; export CSV/JSON.
- **Automation**: schedule preview/install, MCP client JSON/copy/install, skills export.
- **Empty/error states**: unknown is not zero; a missing token is configuration, not an outage;
  no-data report is explicit; partial source failures are named.

Scope note: `report indexing` / `report opportunities` are not CLI commands in `lv24-seo 0.4.0`
(only MCP tools expose them). v0.1 shows weekly, diff, crawl and GSC/Yandex queries; indexing and
opportunity views are P1 and require either a CLI `report indexing|opportunities` or an MCP client
bridge. Do not scrape the MCP server from the desktop for this.

### Report render modes

1. **Native React summary (default)** — accessible, localized, KPI cards and tables from structured
   codes/numbers; no English builder prose.
2. **Sandboxed HTML preview** — exact localized email/HTML fidelity; `iframe sandbox` without
   scripts or same-origin, light canvas, widths 360/600/900, no network.
3. **Open in browser** — for printing/sharing; write a temp file and warn it is local.

The coverage banner (`complete`, `sources_failed/missing/incomplete`, `truncated`, `data_age_days`)
is persistent, not a toast. A no-data fallback is neutral, not an error. `report.html` is
light-only; never auto-invert it in the dark UI.

### Jobs

- Model: kind, params, status (`queued/running/succeeded/partial/failed/cancelled`), timestamps,
  exit code, envelope, log tail, cancel.
- No fake percentages: indeterminate bar + elapsed + phase ("collecting", "building", "storing",
  "delivering"), marked as an estimate. Real progress arrives with `--progress ndjson`.
- Cancel kills the exact child process tree. A cancelled crawl stores nothing (it persists at the
  end); cancelling during delivery may leave an unknown send state — tell the user to check the
  channel.
- Partial failure (`ok=true` + incomplete meta, or exit 5) is "completed with warnings", never a
  clean success.
- Serialize jobs per workspace/data dir; SQLite writer and delivery can block.

### Accessibility and i18n

- WCAG 2.2 AA, visible focus, skip link, focus restore after dialogs, keyboard table navigation.
- Never color-only: deltas carry arrow + sign + label; severity carries text.
- Screen-reader names for icon buttons; route changes focus the page heading; job/save results use
  polite live regions.
- Reduced motion, 200% zoom, 1024px width usable.
- UI locale (`en/ru/zh`) is independent from `report.locale`; both are labelled. Machine codes are
  never translated; unknown codes show raw. Use `Intl` with explicit timezone for dates.

## 10. Security model

- **No secrets in the webview**: token input is a password field sent once over IPC; Rust pipes it
  to `lv24-seo auth set --stdin` and zeroizes the buffer. Values are never logged, persisted in
  MobX or shown after save. Only presence/source is displayed.
- **Command allow-list**: fixed CLI commands and validated arguments; no shell, no arbitrary
  executable, no `eval`.
- **Path allow-list**: config/data/export paths must be absolute, canonicalized, inside a
  user-approved root; reject symlink escapes and control characters.
- **CSP**: production `default-src 'self'`, no remote scripts/connect, no remote IPC; dev CSP only
  for Vite.
- **Config writes**: atomic temp+rename, backup, permissions preserved, TOML comments preserved.
- **Sidecar integrity**: release sidecar checksum-pinned and verified; updater Ed25519 signature
  verified against a committed public key.
- **No telemetry**; diagnostics are user-triggered and redacted.
- **Write gating**: sitemap submit, recrawl, IndexNow, report send, schedule install and MCP
  install require a confirm dialog with the exact target and action.
- **Process cleanup**: cancel/quit kills the exact child handle/PID; never wildcard-kill
  `lv24*` (several LV24 apps can run concurrently).

## 11. Packaging and release

### Sidecar

Recommended release path: PyInstaller **onedir** sidecar built per target in CI, placed in Tauri
resources and launched from `engine/lv24-seo[.exe]`. Onefile is rejected: it extracts ~50 MB to a
temp directory on every call, starts in 2.3–2.6 s, is AV-prone and cannot be codesigned on macOS
(PyInstaller 6.x; onefile macOS `.app` is planned to be an error in v7). Dev fallback:
`LV24_SEO_BIN` or `uvx` (never silently download in a release build).

Measured on Windows x64 with `lv24-seo 0.4.0` + keyring, Python 3.13, PyInstaller 6.22.3:

| Option | Disk | Warm start | Decision |
|---|---|---|---|
| PyInstaller onedir | 50.7 MB (25.4 MB zip) | **0.58 s** | primary |
| PyInstaller onefile | 25.5 MB | 2.3–2.6 s per call | reject |
| python-build-standalone + venv | ~109 MB | 0.68 s | reject for v1 (venv is not relocatable) |
| `uvx` / installed Python | ~0 | network-dependent | dev fallback only |

Naming/bundling:

- onedir tree in `bundle.resources` map `{ "engine/lv24-seo/": "lv24-seo/" }` → the app's
  `$RESOURCES/lv24-seo/`; do **not** use `externalBin` for onedir (it copies one file and cannot
  keep `_internal` adjacent);
- Rust resolves `<resource_dir>/lv24-seo/lv24-seo[.exe]` and launches it with
  `std::process::Command` (no `tauri-plugin-shell`, no JS permission surface);
  `CREATE_NO_WINDOW` on Windows;
- resolution order: `LV24_SEO_BIN` (dev/explicit system engine) → bundled resource → sibling
  `lv24-seo/` → PATH only after a user opt-in; never auto-`uvx`;
- run with `cwd = config parent` (or the app workspace during onboarding) so a relative
  `storage.data_dir` resolves the same as a terminal run; prefer an absolute data dir; do not set
  `LV24_SEO_DATA_DIR` silently because it overrides the config file;
- `scripts/cli-checksums.json` (engine version + per-target SHA-256) is reviewed and checked by
  `verify-sidecar.mjs`; the runtime does not need to re-hash because the updater signature covers
  the bundle;
- `scripts/build-sidecar.py` uses `uv` + PyInstaller with explicit hidden imports:
  `--collect-submodules keyring.backends`, `--collect-submodules lv24_seo.crawl.rules`,
  hidden-import both `mcp.server.fastmcp` and `mcp.server.mcpserver`, `--collect-data lv24_seo`
  (`schema.sql`), `--add-data skills → lv24_seo_skills`;
- startup handshake: `lv24-seo version --json`, require the exact pinned version and show a
  blocking mismatch dialog unless `LV24_SEO_ALLOW_ENGINE_MISMATCH=1`; run `doctor --json` once;
- freeze a smoke suite into CI: `version`, `doctor`, `export skills`, MCP `initialize`, keyring
  set+logout.

Build matrix (free standard runners):

| Runner | Target | Bundles |
|---|---|---|
| `windows-latest` | `x86_64-pc-windows-msvc` | NSIS per-user |
| `ubuntu-22.04` | `x86_64-unknown-linux-gnu` | AppImage, deb, rpm |
| `macos-latest` | `aarch64-apple-darwin` | `.app`, DMG |
| `macos-15-intel` | `x86_64-apple-darwin` | `.app`, DMG |

`ubuntu-22.04` is deprecated in Sep 2026 and unsupported Apr 2027; move to `ubuntu-24.04` or a
22.04 container and document the glibc baseline. `macos-13`/`macos-14` are retired.

- Artifact retention: 7 days.
- Manual `desktop.yml` with `signed` / `publish_release` / `tag` inputs; publishing requires a
  signed build and explicit owner action.
- Updater: Tauri Ed25519/minisign, `latest.json`, `requireSignedVersion`; private key only in
  repository secrets. Linux auto-update works for AppImage only; deb/rpm are manual.
- OS code signing/notarization is deferred: macOS builds are ad-hoc signed (`signingIdentity: "-"`)
  and documented with the System Settings → Privacy & Security → Open Anyway flow; Windows builds
  are unsigned (SmartScreen/Smart App Control warning) unless the free SignPath Foundation OSS
  signing is approved. Do not add paid certificates, notarization or paid runners.

## 12. CI and tests

- **web**: `npm ci`, `npm run typecheck`, `npm test`, `npm run build`; i18n key parity; contract
  fixtures; config diff/TOML tests.
- **rust**: `cargo fmt --check`, `cargo clippy --all-targets -D warnings`, `cargo test` on
  Ubuntu/Windows/macOS; path allow-list, atomic write, CLI args, envelope parser, secret stdin,
  job cancel.
- **contract**: run recorded `lv24-seo 0.4.0` envelopes through the TS parser; fail on unknown
  error codes or missing `warnings`/`meta`; a Python job regenerates fixtures.
- **desktop**: manual matrix build; install and launch with bundled sidecar offline; version
  compatibility; updater check; artifact filenames/checksums.
- **security**: gitleaks on current files and history; `cargo audit` (free); no secrets in
  fixtures/logs; CSP check.
- **accessibility**: token contrast, keyboard focus, dialog focus restore, reduced motion,
  form labels, screen-reader names.

## 13. Swarm execution plan

Orchestrator keeps architecture, contracts, merge/review and DoD. Agents work in disjoint scopes;
each returns a compressed report and tests. Use separate worktrees for parallel file-heavy work.

### Wave 0 — bootstrap (1 agent)

Create public repo, Apache-2.0, README skeleton, `.gitignore`, CI skeleton, Tauri/React scaffold,
FSD folders, shared UI tokens, i18n skeleton, `docs/architecture.md`, `docs/security.md`.
Deliverable: `npm run build` + `cargo check` green; no product screens yet.

### Wave 1 — parallel vertical slices (5 agents)

| Agent | Scope | Deliverable |
|---|---|---|
| A — shell/security | `src-tauri/` CLI runner, config read/validate/write, path allow-list, jobs, secret stdin, settings | Rust unit tests; command fixtures |
| B — frontend foundation | `src/app`, `src/shared/ui`, theme, i18n, router, layout, feedback | typed UI kit; a11y tests |
| C — settings/config | project/providers/report/crawl/storage forms, config output/diff/validation | save/validate flow; config page |
| D — reports/data | dashboard, weekly report, diff, crawl, GSC/Yandex queries | native report views; HTML preview |
| E — automation/packaging | schedule/MCP/skills, sidecar build, CI/desktop workflows, updater | installers + docs |

Model routing: implementation/routine agents use `opencode-go/muse-spark-1.3-contributor`
(higher limits); research, web, security and audit agents use `opencode-go/deepseek-v4.1-flash`.
The orchestrator keeps architecture, contracts, merge, review and release.

### Wave 2 — integration (2 agents + orchestrator)

- Agent F: wire stores to Tauri commands, contract fixtures, error/empty/loading states.
- Agent G: public docs, screenshots, SECURITY/CONTRIBUTING, release checklist.
- Orchestrator: merge, resolve contracts, run full checks, cut 0.1.0.

### Wave 3 — skeptical review (2 agents)

- Security/a11y reviewer: secrets, path traversal, CSP, command injection, focus/contrast.
- Packaging reviewer: install/launch on all targets, sidecar checksum, updater, unsigned warnings.
- Fix findings, then release.

### Agent rules

- One area per commit; `[SEO](<area>) - ...`.
- No agent edits another agent's files; contract changes go through the orchestrator.
- Every slice includes tests and a short verification note.
- No paid services, no wildcard process kills, no secrets in fixtures/logs.

## 14. Milestones and DoD

| Milestone | Content | DoD |
|---|---|---|
| M0 | public repo + scaffold | CI green, docs skeleton, no secrets |
| M1 | shell + config read/validate/write + doctor | Rust tests, atomic save, path allow-list |
| M2 | settings + configuration output | dirty/validate/save/diff; redacted outputs |
| M3 | reports + crawl + queries | weekly/diff/crawl views, HTML preview, honest states |
| M4 | automation + MCP + skills | schedule preview/install, MCP install, export |
| M5 | packaging 0.1.0 | Windows/macOS/Linux artifacts, bundled sidecar, updater smoke |

Definition of done for 0.1.0:

- fresh install → init workspace → edit settings → validate → save → doctor → provider status;
- weekly report in ru/en/zh, preview and optional send with confirmation;
- report diff and crawl with limitations/coverage visible;
- config outputs redacted, copyable, diffable; comments preserved on save;
- no secret ever visible in UI/logs/fixtures; no arbitrary command execution;
- CI green on three OS; installers start offline with the bundled sidecar;
- public README, SECURITY, CONTRIBUTING, issue templates and release notes.

## 15. Acceptance criteria

### MVP (desktop 0.1)

Engine discovery + version gate; setup/init; project, providers, report/delivery/crawl/storage
settings with validation, dirty/diff/atomic save/backups/conflicts; env badges; secret status +
set/remove; test connection; configuration outputs (all tabs); weekly run, native summary,
sandboxed HTML, artifacts, send with confirmation; diff; crawl run/cancel/logs/results;
GSC/Yandex/indexing/properties queries with export; schedule/MCP/skills preview+install with
backups; doctor/logs/updates/support bundle; en/ru/zh; dark theme; accessibility baseline.

### Later

Reports library (`report list`), multi-project, background scheduler/tray, real progress, in-view
locale render, SMTP/Telegram test send, profiles import/export, DB backup/restore, light theme,
indexing-triage/opportunities workbenches, write actions (sitemap/recrawl/IndexNow with confirm
token), skill editor.

### Reviewer-testable criteria

1. A new user can create/select a workspace and see the effective config path and data dir.
2. Every settings field has a label, help text, validation and a visible default.
3. Saving never loses TOML comments and creates a timestamped backup (keep last 5).
4. Invalid TOML or an unknown key blocks save, shows the exact path/line, and leaves the file
   mtime unchanged.
5. An env override shows the `env` badge and the effective value; saving the file does not change
   the effective value until the variable is removed.
6. The config page shows effective JSON, TOML, diff, env override names and doctor checks.
7. Provider cards show configured/target/token presence and source, never the token value.
8. "Test" calls `properties`; it does not claim a provider works if the token is missing.
9. A secret set via the modal never appears in the UI, logs, clipboard or support bundle;
   env-provided secrets are read-only and removal explains that.
10. Weekly report shows KPI cards, opportunities, issues, sources, limitations, `complete` and
   `truncated`; unknown is not zero; CTR deltas are in percentage points.
11. Report preview supports `ru/en/zh`; JSON/MCP payloads remain canonical English; UI locale is
   independent from report locale.
12. Send, sitemap submit, recrawl, IndexNow, schedule install and MCP install require confirmation;
   `already_sent`/`only_on_change` are explained as skipped, not failed.
13. Crawl can be cancelled; a cancelled crawl stores nothing and says so; timeout/cancel is
   reported honestly.
14. The HTML preview cannot execute scripts or make network requests; "open in browser" works.
15. The app works offline with the bundled sidecar; `uvx` is only a labelled dev fallback.
16. Reserved fields (`crawl.concurrency`, `storage.raw_retention_days`, `report.week_start`,
   `report.telegram.severity`) are labelled no-effect, not presented as working controls.
17. `npm run typecheck`, `npm test`, `npm run build`, `cargo fmt/clippy/test` pass on CI.

## 16. Risks and mitigations

| Risk | Mitigation |
|---|---|
| PyInstaller sidecar fails on a platform | Build onedir per target, pin hidden imports, smoke-launch in CI; keep `LV24_SEO_BIN` fallback |
| macOS Gatekeeper friction | Ad-hoc sign, first-launch guide, do not claim seamless install; revisit $99 only on owner approval |
| Windows SmartScreen / Smart App Control / AV false positives | onedir, no UPX, free SignPath Foundation OSS signing if approved, Microsoft submission |
| Hidden-import breakage after dependency upgrade | `uv.lock`, explicit collect flags, frozen smoke matrix on every engine tag |
| glibc baseline retires with `ubuntu-22.04` | Containerized build or documented Ubuntu 24.04 minimum |
| CLI contract drift | Recorded fixtures, envelope parser tolerance, exact engine version handshake |
| TOML comment loss | `toml_edit` patch, raw mode, backup, diff |
| Secrets leak via webview/logs | stdin bridge, zeroize, redaction, gitleaks, no secret state |
| Long jobs / cancel | job manager with timeout and kill-by-PID; no fake progress |
| Provider quotas / slow APIs | bounded requests, `truncated`/`limitations` visible, no auto-retry loops |
| Public repo leaks | gitleaks current+history, SECURITY.md, no provider tokens in CI |
| Sidecar size | onedir, exclude dev/test assets, measure installers; ~50 MB engine |
| Orphaned child processes | kill on app exit by child handle/PID, serialize jobs per workspace |

## 17. Public repository checklist

- Name `lv24-seo-optimizer-desktop`, Apache-2.0, README with screenshots and install steps.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue/PR templates.
- `docs/architecture.md`, `docs/security.md`, `docs/release.md`, `docs/configuration.md`.
- CI + manual desktop workflow; artifact retention 7 days; no paid runners.
- Dependabot for npm/cargo/actions; gitleaks; `cargo audit`.
- Release notes with sidecar version, checksums, unsigned-build warning.
- No provider credentials, updater private key or personal data in the repository.

### Cost constraints

- GitHub/GitLab additional spend: **$0**. Use standard hosted runners only; no larger runners,
  paid minutes, paid storage, paid code signing, notarization, email QA or SaaS.
- Artifact/cache retention: 7 days. Check storage separately from runner minutes.
- Keep hard budgets/overage blocking enabled; do not buy certificate or signing services.
- Unsigned macOS/Windows builds are the honest default until the owner explicitly approves a
  paid signing budget. Document Gatekeeper/SmartScreen warnings.

## 18. Open decisions

1. Sidecar packaging: onedir (recommended) vs onefile for the first public build.
2. Default UI locale: system language vs English; report locale remains `report.locale`.
3. Add `report list --json` and `export config-schema` to `lv24-seo` now or after v0.1.
4. Whether to add a local HTTP API later for progress/multi-client; not needed for v0.1.
5. Owner decision on Apple Developer / Windows signing budget (currently $0).
