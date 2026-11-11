# AGENTS.md — contract for AI agents working in this repo

This file is the canonical instruction set for any coding agent (Claude Code, OpenCode, Codex,
Cursor, Gemini, Aider). `CLAUDE.md` is a pointer to this file — never let the two diverge.

## 1. What this project is

`lv24-seo-optimizer` is a Python package that ships four surfaces over one core:

| Surface | Entry point | Contract |
|---|---|---|
| CLI | `src/lv24_seo/cli/` | every command supports `--json`, returns `{ok,data,meta,warnings}`, stable exit codes |
| MCP server | `src/lv24_seo/mcp/` | 8–12 stable tools, read-only by default, `structuredContent` + text fallback |
| Skills | `skills/*/SKILL.md` | canonical skill, installed anywhere via `lv24-seo export skills` (editor mirrors planned) |
| Automation | `src/lv24_seo/automation/` | cron / systemd / Task Scheduler generators, email + Telegram delivery |

Single source of truth for tool metadata: `src/lv24_seo/mcp/registry.py`. CLI help, MCP schemas,
`schemas/tools.json` and the docs tables are **generated** from it. Never hand-edit generated files.

## 2. Hard rules

1. **Zero spend.** Default path must work on free tiers only. Do not add a dependency that requires a
   paid account, a card on file, or a paid CI runner. Optional paid adapters are allowed only behind
   an explicit provider key and must degrade gracefully.
2. **Read-only by default.** Any destructive or quota-consuming action (sitemap submit, recrawl
   request, IndexNow submit) requires `--allow-write` (CLI) / `destructiveHint` + confirm token (MCP),
   plus an idempotency key.
3. **Honesty over completeness.** Every bounded response must carry `truncated`, `total`, `data_age`
   and `limitations[]`. Never let an agent claim full coverage when a provider capped the data.
4. **No secrets in the repo, logs, fixtures or tests.** Tokens live in the OS keychain, `0600` files
   outside the repo, or `LV24_SEO_*` env vars. Tests use synthetic fixtures only.
5. **No scraping of SERPs.** Rank tracking uses provider APIs (GSC, Yandex Webmaster), never HTML
   scraping of Google/Yandex result pages.
6. **No vendor resale.** If a paid provider is integrated, the user supplies their own key and the
   data stays local.
7. **Windows, macOS and Linux must all work.** No POSIX-only paths in user-facing commands; shell
   snippets in docs come in both bash and PowerShell where relevant.
8. **Deterministic output.** Report and CLI output is stable enough to diff between runs (sorted keys,
   ISO-8601 UTC timestamps, no random ids in visible output).

## 3. Repo layout

```
src/lv24_seo/
  core/        config, errors, envelopes, rate limiting, http, time
  providers/   gsc, yandex_webmaster, yandex_metrika, bing, crux, psi, ga4 (optional)
  crawl/       fetcher, parsers, rules, link graph, log ingest
  store/       sqlite schema + repositories
  report/      builders, renderers (html/text/md), delivery
  mcp/         server, registry (SoT), resources, prompts
  cli/         Typer commands
  automation/  scheduler generators, GitHub Actions recipes
schemas/       generated JSON schemas (tools.json)
skills/        canonical SKILL.md files
docs/          architecture, limits, guides, plans, research
tests/         unit, contract (recorded fixtures), e2e (network-gated)
```

## 4. How to work

```bash
uv sync                    # install dev env
uv run ruff check .        # lint
uv run ruff format --check .
uv run mypy src            # types
uv run pytest -q           # offline tests only
uv run pytest -q -m live   # requires tokens; never in CI
uv run lv24-seo doctor     # environment self-check
```

Before claiming done:

- `ruff check` + `ruff format --check` + `mypy` + `pytest -q` are green.
- Generated artifacts are regenerated, not edited: `uv run python -m lv24_seo.mcp.registry --write`.
- Docs updated in the same change (command help, tool table, `docs/*`).
- No new required dependency without a note in the PR body explaining why and its license.
- Do not weaken `limitations[]`, truncation reporting or write-gating to make a test pass.

## 5. Change review protocol

- Small, focused commits. Subject format: `[SEO](<area>) - <what changed>`, e.g.
  `[SEO](providers) - add yandex recrawl quota guard`.
- One area per commit: `cli`, `mcp`, `providers`, `crawl`, `report`, `store`, `docs`, `infra`, `tests`.
- PR body: problem, solution, verification commands, and a note about provider limits touched.
- Never commit generated fixtures containing real customer data or personal queries.

## 6. Where decisions live

- Product and architecture decisions: `docs/decisions/ADR-*.md` (append a new ADR, do not rewrite old ones).
- Execution plan and wave status: `docs/plans/2026-11-10-implementation-plan.md`.
- Provider capability matrix: `docs/data-sources.md` — update it in the same PR that touches a provider.
