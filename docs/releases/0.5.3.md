# 0.5.3 - honest provider data, hardened crawl and gated writes

Status: draft. This file describes the planned 0.5.3 engine wave. It has no release date and is not
published; version numbers, assets and dates are set only when the tag is cut.

## Fixed

### GSC no longer presents a capped result as complete

A truncated search-analytics walk now omits `total` (`null`). The previous exact row count next to
`truncated: true` read as a full answer. `data_age_days` is floored at the documented 3-day lag, so
a window ending today is never advertised as fresher than GSC can be.

### Metrika pages through the table and reports its caps

`stat/v1/data` is walked with `offset` until `total_rows`, a short page, or a cap is reached: at
most 10,000 rows and 100 pages per call. A full page with no `total_rows` is `truncated: true`
instead of looking complete, and a request without dimensions is a single row rather than a page
walk.

### Bing reports the real age of its feeds

`data_age_days` comes from the newest dated row in each endpoint (`Date` for query stats,
`DateDetected` for crawl issues) instead of a hardcoded zero. An empty or undated feed still
reports 0, which is not proof of freshness.

### Yandex empty history is data, not an outage

An empty indexing history or search-URL event list returns `rows: []`, `total: 0` and
`limitations: ["empty_history"]`; `degraded_host` is reserved for a host-scoped 404 or an explicit
`HOST_NOT_LOADED` payload. Pagination totals come from explicit grand-total keys only — a bare
`count` is the current page size and no longer stops paging early.

### Crawl parsing is harder to fool

* Redirect chains are flagged only above two hops; one redirect is normal and is no longer noise.
* Soft 404 requires a not-found phrase **and** an empty body or an explicit `404`/`410` marker; a
  guide titled "How to fix 404 errors" is no longer a soft 404.
* robots.txt patterns are matched linearly (no `.*` regex backtracking), capped at 1,024-char
  patterns and 4,096-char paths. A `429` is retried once with `Retry-After` (clamped to 30 s), and
  a `429` on robots.txt defers the crawl instead of failing open.
* Sitemap `<loc>` values must share the site origin; an off-origin loc is dropped and reported as
  `off_origin_loc` instead of being fetched.
* Markdown output escapes provider-controlled text, so a crawled title cannot inject links,
  emphasis, raw HTML or `@` mentions into `report diff --markdown` or a PR comment.

### Storage failures are typed

`STORE_LOCKED` is retryable; `STORE_CORRUPT` preserves a byte copy as `<db>.corrupt-<utc>` and
names it in `detail.backup_path`; `STORE_UNAVAILABLE` covers permissions, paths and disk errors.
Each carries a `next_action`. Unrecognised SQLite failures are re-raised unchanged.

### Doctor fails on a declared but unusable data directory

When a config file resolves or `LV24_SEO_DATA_DIR` is set, the `data_dir` check is fatal: a missing
or unwritable directory returns `ok: false` / `meta.healthy: false` instead of a green result.
`meta.data_dir_declared` records whether the directory was declared.

### Cron repair no longer deletes user jobs

An unterminated managed cron block is repaired conservatively: only lines that look like lv24-seo
entries are removed, other lines are kept, and the envelope warns to review `crontab -l`.

## Changed

* `--strict` on `stats`, `gsc query`, `yandex queries`, `yandex indexing`, `crawl`,
  `report weekly` and `report diff`: it always sets `meta.strict`, and a truncated success exits
  `5` with at least one `meta.limitations` entry. Without `--strict`, truncation still exits `0`.
  `report diff --markdown` now exits with the envelope code.
* Write commands resolve the idempotency ledger from the workspace selected by `--config`, so
  `--config /srv/site/seo.toml` opens `/srv/site/data` even when the shell is elsewhere.
* MCP write tools implement the two-phase contract: `allow_write=true` opt-in, a dry run returning
  `plan_hash` / `confirm_token` (300 s TTL), an apply that checks the plan hash, and the shared
  SQLite ledger for replay protection. The secret is `LV24_SEO_WRITE_SECRET` from the environment.
  With the shipped engine service the handler lookup runs before the dry run, so calls still return
  `WRITE_DISABLED` (see Known limitations).
* `seo_crawl_site` runs off the event loop on a worker thread with its own store and a 600 s
  budget; other tools keep serving, and a timeout returns a retryable `UPSTREAM_UNAVAILABLE`.
* MCP registry inputs now match the real tool signatures (`seo_weekly_report`, `seo_indexing_triage`,
  `seo_opportunities`, both write tools), and `schemas/tools.json` is regenerated from the registry.
* CI: a wheel-contract job runs the frozen CLI envelope contract against the installed wheel,
  Windows and macOS join the offline test matrix, pip-audit gates the locked runtime set,
  dependabot is configured, the release workflow requires release notes for the tag, and
  `paths-ignore` no longer skips `skills/*/SKILL.md`.

## Verification

Before tagging, the wave must pass the gates that already exist in the repo:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python -m lv24_seo.mcp.registry --check
uv run pytest tests/contract -q
```

CI additionally builds the wheel and runs `tests/contract` against the installed binary, runs the
offline suite on Ubuntu, Windows and macOS, audits the exported runtime requirements with pip-audit,
and checks that `schemas/tools.json` has no drift.

## Known limitations

* The engine service still exposes no recrawl or sitemap write handler: CLI applies and every MCP
  write call return `WRITE_DISABLED` and nothing is sent. Only IndexNow reaches a provider.
* Metrika walks stop at 10,000 rows / 100 pages per call; larger tables are truncated and say so.
* Bing reports `data_age_days: 0` for an empty or undated feed; that is not evidence of freshness.
* `STORE_LOCKED`, `STORE_CORRUPT` and `STORE_UNAVAILABLE` have no dedicated CLI exit code; they
  exit `1` and must be read from `error.code`.
* `--strict` is not available on write commands; a dry run always exits `0` when the plan is valid.
* `doctor` treats an unusable declared data directory as fatal, so a deliberately absent directory
  now fails the check instead of warning.
* The cron repair is heuristic: it preserves lines it cannot attribute to lv24-seo and asks the
  operator to review the crontab rather than guessing.
