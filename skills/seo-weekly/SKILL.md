---
name: seo-weekly
description: Use when the user asks how organic search is doing, why traffic or clicks dropped, what to fix this week, or wants a weekly SEO report for a site. Covers Google Search Console and Yandex Webmaster/Metrika numbers, deltas, opportunities and indexing health.
version: 0.1.0
requires_tools: [seo_weekly_report, gsc_search_analytics, yandex_query_stats, seo_opportunities, seo_indexing_triage]
---

# Weekly SEO review

## When to use

- "как SEO?", "почему упал трафик", "что делать на этой неделе"
- A scheduled Monday digest needs a narrative around the numbers.
- After a deploy, to confirm nothing regressed.

## When not to use

- The user asks only about one URL's index status → `seo-indexing-triage`.
- The user asks only about Core Web Vitals → run CrUX/PSI tools directly.
- No property is configured → run `lv24-seo doctor` first and ask for the property id.

## Inputs

Property (`sc-domain:example.com` or Yandex `https:example.com:443`), period (default: last 7 full days,
GSC lag 2–3 days), comparison (previous 7 days or same weekday last week). If the user does not specify,
use defaults and **state them**.

## Recipe

1. `seo_weekly_report()`
   → headline clicks/impressions/CTR/position (GSC windows only) + `data_age_days` + per-source `limitations` + `sources_failed`.
2. If clicks dropped ≥ 10%:
   - `gsc_search_analytics(property, start_date, end_date, dimensions=["device"])` → mobile vs desktop.
   - `gsc_search_analytics(property, start_date, end_date, dimensions=["page"], limit=100)` → top losing pages.
   - `seo_indexing_triage()` → new "not indexed" buckets.
3. If impressions grew but CTR fell: `seo_opportunities()` returns `ctr_gap` candidates — check title/snippet changes
   and SERP feature shifts (no API for features; infer from GSC `searchAppearance` dimensions).
4. If Yandex shows different dynamics: `yandex_query_stats(host_id, limit=100)` and label the Yandex
   caveats (`top_3000_queries`, `no_query_page_join`).
5. Pull 3–5 ranked actions from `seo_opportunities()` and filter by `kind`
   (`striking_distance` | `ctr_gap` | `decay`).

## Decision tree

```
clicks down?
├─ impressions down too → indexing / demand / seasonality
│   ├─ new "not indexed" URLs → seo_indexing_triage → fix robots/canonical/noindex first
│   └─ no indexing change → check Yandex gaps, then ask about business seasonality
├─ impressions flat, CTR down → titles/snippets or SERP layout
│   └─ seo_opportunities(kind="ctr_gap")
└─ impressions up, clicks flat → ranking on low-CTR queries; optimise titles, not content volume
```

## Output contract

The answer must contain these sections, in this order:

1. **Headline** — clicks, impressions, CTR, avg position with delta and the exact period.
2. **What moved** — top 3 gainers/losers by page and by query (with numbers).
3. **Indexing** — new exclusions or recoveries, if any.
4. **Actions** — 3–5 items, each: what, where, why (expected effect), effort.
5. **Data honesty** — `data_age_days`, truncated sources, engines that returned nothing.

## Hard rules

- Never invent a metric, a comparison or a cause. If a provider failed, say which and why.
- If `truncated` is true, say "top N by clicks", not "all queries".
- Never compare Yandex query-level positions with GSC query-level positions — different methodologies.
- Writes (recrawl, sitemap submit) require explicit user confirmation and a dry-run first.
