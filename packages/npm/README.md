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

Beta (0.3.x). The current Python/NPM line ships 11 read-only-by-default MCP tools, 3 resources,
3 prompts, the full CLI and scheduler generators. Recent additions:

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

## Requirements

* [uv](https://docs.astral.sh/uv/) (recommended) **or** an installed `lv24-seo` binary
  (`pipx install lv24-seo` / `uv tool install lv24-seo`). Python itself does not need to be installed
  when uv is present — uv downloads the right interpreter.

## Usage

```bash
npx @lv24/seo doctor --json
npx @lv24/seo init --property https://example.com
npx @lv24/seo auth set --provider gsc --stdin
npx @lv24/seo gsc query --start 2026-11-01 --end 2026-11-07 --dimensions query,page
npx @lv24/seo report weekly --json --out reports/
npx @lv24/seo report diff --markdown          # diff of two stored weekly snapshots
npx @lv24/seo crawl --json                    # issues + sitemap↔crawl coverage diff
npx @lv24/seo mcp serve                       # stdio MCP server for Claude/OpenCode/Cursor
npx @lv24/seo export skills --out ./skills    # packaged agent skills
```

The first run may take a few seconds while uv resolves and caches the Python package.

## MCP surface

Eleven tools over stdio (or streamable HTTP via `mcp serve --transport http`): `seo_properties`,
`gsc_search_analytics`, `yandex_query_stats`, `yandex_indexing_state`, `seo_crawl_site`,
`seo_weekly_report`, `seo_report_diff`, `seo_indexing_triage`, `seo_opportunities`, plus the
gated `seo_recrawl_request` and `seo_sitemap_submit`. Read tools never touch a provider unless
called; write tools require `allow_write=true` and are disabled by default.

## Configuration

Tokens are read from the keychain (set them with `lv24-seo auth set`) or from `LV24_SEO_*`
environment variables. Nothing is written by this wrapper.

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
* CLI, MCP tools and limits: `docs/cli.md`, `docs/mcp.md`, `docs/data-sources.md`
* Release notes: <https://github.com/LevapVeeskela/lv24-seo-optimizer/tree/main/docs/releases>
* Issues: <https://github.com/LevapVeeskela/lv24-seo-optimizer/issues>

## License

Apache-2.0. Not affiliated with Google, Yandex or Microsoft; provider data belongs to the property
owner and is never resold.
