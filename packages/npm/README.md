![LV24 SEO — SEO insights for humans and AI agents](https://raw.githubusercontent.com/LevapVeeskela/lv24-seo-optimizer/main/.github/assets/banner.png)

# @lv24/seo

[![npm](https://img.shields.io/npm/v/@lv24/seo.svg)](https://www.npmjs.com/package/@lv24/seo)
[![PyPI](https://img.shields.io/pypi/v/lv24-seo.svg)](https://pypi.org/project/lv24-seo/)
[![ci](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml/badge.svg)](https://github.com/LevapVeeskela/lv24-seo-optimizer/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/LevapVeeskela/lv24-seo-optimizer/blob/main/LICENSE)

`npx` launcher for the Python package [`lv24-seo`](https://pypi.org/project/lv24-seo/) —
a free-first SEO control plane for humans and AI agents: Google Search Console, Yandex Webmaster,
Yandex Metrika, Bing/IndexNow, CrUX, PageSpeed Insights, a 30-rule technical crawl, weekly
report diffs, an MCP server and agent skills.

This npm package contains **no SEO logic**. It forwards every argument to
`uvx --from lv24-seo==<this-version> lv24-seo …` (or to a `lv24-seo` binary already on `PATH`),
so the CLI, its `--json` envelope and the documented exit codes behave exactly as in the Python
package. The version is pinned to this package's own version; set `LV24_SEO_PIN=0` to opt back
into the unpinned (`latest`) lookup.

## Status

Beta. `@lv24/seo 0.9.0` launches `lv24-seo 0.9.0` (release candidate; `0.8.0` is the
current published engine) and ships 12 MCP tools (10 read-class: 8 pure read-only plus the two
local-state writers annotated `readOnlyHint: false`), 3 resources, 3 prompts, 3 skills, the
22-command CLI, scheduler generators and a product-grade weekly email. Recent additions:

* **Bing sitemap submit and one-click IndexNow** — `sitemap submit --provider bing` calls the
  current JSON `SubmitFeed` endpoint through the two-phase write gate. `indexnow submit
  --from-sitemap` reads a bounded urlset/sitemapindex (root plus five children, 10k URLs), merges
  explicit `--url` values, and resolves host/key from `[providers.bing]`. GSC sitemap submit and
  Bing delete stay `WRITE_DISABLED`.
* **Logs, digest, tasks and backup** — `logs analyze` / `seo_log_analysis` triage one local nginx
  JSON access log (UA-only bots, 100 MiB cap, read-only); `report weekly` adds an attributed change
  digest; `tasks` tracks findings through `open`/`in_progress`/`verified`/`dismissed` (schema v3);
  `backup create` writes a portable zip with the config and SQLite history (restore not implemented).

* **Localized product email** — `report weekly --send` renders a 600px inline-CSS digest in
  `ru`, `en` or `zh`: KPI cards for clicks/impressions/CTR/position, top-3 opportunities,
  issue severities, merged sources, data notes and a property/UTC footer. Plain-text fallback
  and subject are localized too; JSON/MCP payloads stay canonical English. Unknown values stay
  unknown, CTR changes are shown in percentage points, and Metrika is labelled as sessions,
  never as clicks. Set the language once with `report.locale`.
* **Week-over-week report diff** — `report diff` and the `seo_report_diff` MCP tool compare two
  stored weekly snapshots without provider calls. Metrics present on one side only stay unknown,
  not zero; differing period lengths make the pair not comparable. `--markdown` is deterministic
  for PR comments and CI summaries.
* **Sitemap ↔ crawl coverage diff** — `crawl` / `seo_crawl_site` return `data.sitemap_coverage`
  with six buckets (`unlinked`, `missing_from_sitemap`, `not_ok`, `noindex`, `blocked`,
  `unvisited`), exact counts, capped lists, `complete` and `limitations[]`. A site without a
  sitemap is "empty, not clean"; unvisited URLs are unknown, never broken.
* **Honest bounds** — `meta.complete`, `truncated`, `total` and `limitations[]` are carried through
  MCP, CLI and reports; unknown totals stay `null`; delivery is idempotent per period and channel.

### MCP tools

| Tool | Kind | Notes |
|---|---|---|
| `seo_properties` | read | Configured providers, targets, token presence |
| `gsc_search_analytics` | read | GSC query/page/country/device rows |
| `yandex_query_stats` | read | Top Yandex queries |
| `yandex_indexing_state` | read | Indexing history, events, problems |
| `seo_crawl_site` | read + local state | Crawl; stores pages/issues, `readOnlyHint: false` |
| `seo_weekly_report` | read + local state | Weekly digest; stores a snapshot, `readOnlyHint: false` |
| `seo_report_diff` | read | Diff two stored snapshots |
| `seo_indexing_triage` | read | Group indexing problems into actions |
| `seo_log_analysis` | read | One local nginx JSON log: bot/day rollups and crawl waste, UA-only |
| `seo_opportunities` | read | Striking-distance and decay opportunities |
| `seo_recrawl_request` | write | Yandex recrawl queue, `allow_write=true` |
| `seo_sitemap_submit` | write | GSC/Bing sitemap, `allow_write=true`; Bing submit wired |

The two local-state tools are not destructive; the two provider writes are
annotated `destructiveHint: true` and gated by `allow_write=true`. Bing sitemap submit is wired;
GSC sitemap submit, Bing sitemap delete and Yandex recrawl return `WRITE_DISABLED` and nothing is
sent. IndexNow is CLI/desktop-only. Yandex Metrika has its own auth provider:
`npx @lv24/seo auth set --provider metrika --stdin` stores `yandex_metrika_token`, separate from the
Webmaster token. Full inputs and limits:
[`docs/mcp.md`](https://github.com/LevapVeeskela/lv24-seo-optimizer/blob/main/docs/mcp.md).

## Requirements

* [uv](https://docs.astral.sh/uv/) (recommended) **or** an installed `lv24-seo` binary
  (`pipx install lv24-seo` / `uv tool install lv24-seo`). Python itself does not need to be installed
  when uv is present — uv downloads the right interpreter.

## Usage

```bash
npx @lv24/seo doctor --json
npx @lv24/seo init --property https://example.com
npx @lv24/seo auth set --provider gsc --stdin
npx @lv24/seo auth set --provider metrika --stdin
npx @lv24/seo properties --json               # configured providers + token presence
npx @lv24/seo gsc query --start 2026-11-01 --end 2026-11-07 --dimensions query,page
npx @lv24/seo report weekly --json --out reports/
npx @lv24/seo report weekly --send              # product email/Telegram digest
npx @lv24/seo report diff --markdown          # diff of two stored weekly snapshots
npx @lv24/seo stats --days 7 --json           # per-provider dashboard snapshot
npx @lv24/seo crawl --json                    # issues + sitemap↔crawl coverage diff
npx @lv24/seo logs analyze --file /var/log/nginx/access.log --json   # bot rollups + crawl waste
npx @lv24/seo tasks list --json               # local finding lifecycle
npx @lv24/seo backup create --out "$PWD/backups" --json   # portable config + SQLite zip
npx @lv24/seo sitemap submit --provider bing --json       # dry run; site/sitemap from config
npx @lv24/seo indexnow submit --from-sitemap --json       # one-click; host/key from [providers.bing]
npx @lv24/seo mcp serve                       # stdio MCP server for Claude/OpenCode/Cursor
npx @lv24/seo export skills --out ./skills    # packaged agent skills
```

The first run may take a few seconds while uv resolves and caches the Python package.

## Email reports

`report weekly --send` builds the digest and delivers it over the configured SMTP/Telegram
channels. The email is a product template, not a raw data dump: dark branded header/footer,
KPI cards, top-3 opportunities, issue severities, merged sources, data notes/warnings and a
footer with the exact property id. It contains no external images, stylesheets, scripts or
tracking pixels; unknown values stay unknown and CTR changes are shown in percentage points.

Language is selected once for all human-readable output:

```toml
[report]
locale = "ru"                  # ru | en | zh; JSON/MCP payloads stay canonical English

[report.email]
enabled = true
to = ["owner@example.com"]
from = "seo@example.com"
smtp_host = "smtp.example.com"
smtp_port = 587
smtp_user = "seo@example.com"
only_on_change = true          # set false for an email every run, even without material change
```

Store the SMTP password in the keychain with `npx @lv24/seo auth set --provider smtp --stdin`
(or set `LV24_SEO_SMTP_PASSWORD`). The job generated by `schedule install --allow-write` includes
`--send`, so a weekly/daily schedule delivers automatically; without `--allow-write` the command only
prints the plan. `only_on_change` and the configured `report.thresholds` can suppress a send when
nothing material moved; switching `report.locale` re-sends the same period once because the delivery
hash includes the language.

## MCP surface

Twelve tools over stdio (or streamable HTTP via `mcp serve --transport http`): `seo_properties`,
`gsc_search_analytics`, `yandex_query_stats`, `yandex_indexing_state`, `seo_crawl_site`,
`seo_weekly_report`, `seo_report_diff`, `seo_indexing_triage`, `seo_log_analysis`,
`seo_opportunities`, plus the gated `seo_recrawl_request` and `seo_sitemap_submit`. Read tools
never touch a provider unless
called; write tools require `allow_write=true` and are disabled by default.

## Configuration

Tokens are read from the keychain (set them with `lv24-seo auth set`) or from `LV24_SEO_*`
environment variables. Nothing is written by this wrapper. `providers.bing.indexnow_key` and
`providers.bing.indexnow_key_location` are public IndexNow identifiers (the key file is served
openly), so they belong in `seo.toml`; the Bing API key stays a secret.

The desktop app and the CLI share the same OS keychain entry per provider (and per delivery secret):
service `lv24-seo`, usernames `gsc_token`, `yandex_webmaster_token`, `yandex_metrika_token`,
`bing_api_key`, `crux_api_key`, `psi_api_key`, `smtp_password`, `telegram_bot_token`. A token entered
with `lv24-seo auth set` is visible to the desktop's `auth status` and vice versa. `LV24_SEO_*`
variables are inherited by the desktop's CLI runner. The opt-in `<data-dir>/secrets.json` fallback
(0600 on POSIX, ACLs on Windows) is shared only when both surfaces use the same workspace/data
directory.

If auto-detection picks the wrong binary, point it explicitly (must be absolute):

```bash
LV24_SEO_BIN=/usr/local/bin/lv24-seo npx @lv24/seo doctor
```

Guards: the fallback resolves `lv24-seo` through `PATH` but never execs the launcher's own
file (or an npm wrapper pointing back at it) — a recursion attempt exits loudly instead of
spawning itself. A relative `LV24_SEO_BIN` is rejected with exit code 2. Exit code, stdout
and stderr pass through unchanged.

## Files

`bin/lv24-seo.js`, `README.md` and `LICENSE` (copied from the repo root by the `prepack`
script, so the published tarball carries the licence text).

## Tests

```bash
npm test   # node --test test/launcher.test.js, offline: pinning, guards, exit codes
```

## Links

* Repository and full docs: <https://github.com/LevapVeeskela/lv24-seo-optimizer>
* Desktop app (same engine): Windows NSIS/portable ZIP, Linux AppImage/deb/rpm, macOS arm64/x64
  DMG; signed updates for NSIS, `.app` and AppImage:
  <https://github.com/LevapVeeskela/lv24-seo-optimizer-desktop>
* CLI, MCP tools, email scheduling and limits: `docs/cli.md`, `docs/mcp.md`,
  `docs/scheduling.md`, `docs/data-sources.md`
* Release notes: <https://github.com/LevapVeeskela/lv24-seo-optimizer/tree/main/docs/releases>
* Issues: <https://github.com/LevapVeeskela/lv24-seo-optimizer/issues>

## License

Apache-2.0. Not affiliated with Google, Yandex or Microsoft; provider data belongs to the property
owner and is never resold.
