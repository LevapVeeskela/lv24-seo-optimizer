# @lv24/seo

`npx` launcher for the Python package [`lv24-seo`](https://pypi.org/project/lv24-seo/) —
a free-first SEO control plane for humans and AI agents (Google Search Console, Yandex Webmaster,
Yandex Metrika, Bing/IndexNow, CrUX, PageSpeed Insights, a 30-rule technical crawl, scheduled
email/Telegram reports, an MCP server and agent skills).

This npm package contains **no SEO logic**. It forwards every argument to
`uvx --from lv24-seo lv24-seo …` (or to a `lv24-seo` binary already on `PATH`), so the CLI, its
`--json` envelope and the documented exit codes behave exactly as in the Python package.

## Requirements

* [uv](https://docs.astral.sh/uv/) (recommended) **or** an installed `lv24-seo` binary
  (`pipx install lv24-seo` / `uv tool install lv24-seo`). Python itself does not need to be installed
  when uv is present — uv downloads the right interpreter.

## Usage

```bash
npx @lv24/seo doctor --json
npx @lv24/seo init --property https://example.com
npx @lv24/seo auth set --provider gsc --stdin
npx @lv24/seo report weekly --json --out reports/
npx @lv24/seo mcp serve            # stdio MCP server for Claude/OpenCode/Cursor
```

The first run may take a few seconds while uv resolves and caches the Python package.

## Configuration

Tokens are read from the keychain (set them with `lv24-seo auth set`) or from `LV24_SEO_*`
environment variables. Nothing is written by this wrapper.

If auto-detection picks the wrong binary, point it explicitly:

```bash
LV24_SEO_BIN=/usr/local/bin/lv24-seo npx @lv24/seo doctor
```

## Links

* Documentation: <https://github.com/LevapVeeskela/lv24-seo-optimizer>
* MCP tools and skills: see `docs/mcp.md` and `skills/` in the repository
* Issues: <https://github.com/LevapVeeskela/lv24-seo-optimizer/issues>

## License

Apache-2.0. Not affiliated with Google, Yandex or Microsoft; provider data belongs to the property
owner and is never resold.
