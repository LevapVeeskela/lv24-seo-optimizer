---
name: seo-indexing-triage
description: Use when pages are missing from Google or Yandex, after a deploy or migration, when Search Console reports "Crawled - currently not indexed" or "Discovered - currently not indexed", when sitemaps show errors, or when a site suddenly loses indexed pages.
version: 0.1.0
requires_tools: [seo_indexing_triage, gsc_search_analytics, seo_crawl_site, yandex_indexing_state, seo_recrawl_request]
---

# Indexing triage

## When to use

- "страницы не в индексе", "выпали из поиска", sitemap errors, coverage anomalies.
- Right after a release that changed routing, canonical tags or robots.txt.
- A weekly report flagged a drop in indexed pages.

## When not to use

- The pages rank but traffic is low → performance/CTR skill (`seo-weekly`).
- A single URL needs a manual "Request indexing" — Google has no API for that; explain it.

## Inputs

Property, URL set (from the sitemap or a supplied list), engine (Google / Yandex / both).
Cap triage at 200 URLs per run — URL Inspection allows 2,000/day/site but each call is one URL.

## Recipe

1. `seo_indexing_triage(property, urls="sitemap", limit=200)`
   → buckets: `noindex`, `blocked_by_robots`, `canonical_mismatch`, `soft_404`, `redirect`,
   `discovered_not_indexed`, `crawled_not_indexed`, `duplicate`, `error_5xx`, `ok`.
2. For every bucket, confirm with a cheap crawl before recommending a fix:
   `seo_crawl_site(start_url, max_urls=50, rules=["robots","canonical","meta_robots","status"])`.
3. Compare with the source of truth: sitemap `loc` vs page canonical vs HTTP status.
4. Yandex: `yandex_indexing_state(host_id)` → check `search-urls/events` reasons and diagnostics.
5. Propose fixes in priority order: `noindex`/`Disallow` → canonical → status codes → sitemap membership.
6. After the fix ships: `seo_recrawl_request` (Yandex, quota-checked) and a sitemap re-submit (Google).
   Both are writes — dry-run first, then confirm.

## Decision tree

```
not indexed
├─ robots.txt disallow or meta noindex → critical, fix first, no content work
├─ canonical points elsewhere          → fix canonical, then re-check sitemap loc
├─ 404/410/5xx                         → fix status, then sitemap
├─ discovered, not crawled             → internal links, sitemap freshness, crawl budget
├─ crawled, not indexed                → thin/duplicate content, wait 2–4 weeks after content fix
└─ indexed in Yandex but not Google    → normal divergence; compare quality signals, don't panic
```

## Output contract

1. **Counts per bucket** with 3 example URLs each.
2. **Root cause** — one sentence per bucket, evidence-based.
3. **Fix plan** — ordered, each item with the exact file/config location when known.
4. **Verification** — the command to re-run after the fix and the expected result.
5. **Quota notes** — URL Inspection calls used, Yandex recrawl quota left.

## Hard rules

- A 200 response with a "not found" page is a soft-404 — treat it as an error, not a success.
- Never recommend "request indexing" as a primary fix; it is not a mechanism, it is a nudge.
- Do not queue recrawl for URLs that still return non-200 — fix the status first.
