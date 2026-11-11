# Agent skills

A skill is a folder with `SKILL.md` (YAML frontmatter + markdown) and optional `references/`,
`scripts/`, `assets/`. It encodes *how to use the tools*, not new code paths.

## Layout

```
skills/
  seo-weekly/SKILL.md
  seo-indexing-triage/SKILL.md
  seo-technical-audit/SKILL.md
  manifest.json          # name, version, schema_hash, tool list
```

Mirrors (`.claude/skills/<name>`, `.opencode/skills/<name>`) are **planned, not shipped**:
there is no generator and no copy in this repo today. Install the canonical skills from any
distribution with the CLI — this works from the wheel, not just from a checkout:

```bash
lv24-seo export skills --out ./skills                 # canonical set + manifest.json
lv24-seo export skills --out ~/.claude/skills         # Claude Code
lv24-seo export skills --out .opencode/skills         # OpenCode
```

## Frontmatter

```yaml
---
name: seo-weekly
description: Use when the user asks how organic search is doing, why traffic dropped, what to fix this week, or wants a weekly SEO report for a site.
version: 0.1.0
requires_tools: [seo_weekly_report, gsc_search_analytics, yandex_query_stats, seo_opportunities]
---
```

The `description` is the trigger — it must contain the words a user (or a router model) would use.

## Body structure (mandatory)

1. **When to use / when not to use** — 3–5 bullets each.
2. **Inputs you need** — property, period, comparison window; ask if missing.
3. **Recipe** — ordered tool calls with concrete arguments.
4. **Decision tree** — e.g. clicks down → split by device → split by page → check indexing → check SERP
   feature changes; each branch names the next tool.
5. **Output contract** — headings the answer must contain, and the numbers that must be cited.
6. **Hard rules** — never invent metrics, always surface `truncated`/`limitations`, ask before writes,
   cite `data_age_days`.

## Skills we ship

| Skill | Trigger | Tools |
|---|---|---|
| `seo-weekly` | "как SEO?", "почему упал трафик", weekly report request | `seo_weekly_report`, `gsc_search_analytics`, `yandex_query_stats`, `seo_opportunities` |
| `seo-indexing-triage` | "страницы не в индексе", sitemap errors, sudden exclusion | `seo_indexing_triage`, `gsc_search_analytics` (page), `seo_crawl_site` |
| `seo-technical-audit` | "проверь техничку", after a deploy, before a release | `seo_crawl_site`, `yandex_indexing_state`, `seo_sitemap_submit` (write, ask first) |

## Tool-schema sync

`skills/manifest.json` stores `schema_hash` of `schemas/tools.json`. CI fails when a tool is renamed or
its inputs change without a skill bump. Agents that do not speak MCP can read `schemas/tools.json`
directly and call the CLI with `--json`.

## Anti-patterns

* A skill that restates the tool list instead of teaching a decision.
* A skill that asks the model to "analyse" without naming the numbers to compare.
* A skill that promises results the APIs cannot deliver (e.g. "list all queries for a page" in Yandex).
