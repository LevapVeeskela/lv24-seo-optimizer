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

Property, URL set (from the sitemap or a supplied list) and engine (Google / Yandex / both).
Scope comes from the configured property, the stored crawl and provider data — the tools take
no `property`/`urls`/`limit` inputs. URL Inspection allows one URL per call, so triage works off
stored buckets, not live per-URL checks.

## Recipe

1. `seo_indexing_triage()`
   → buckets: `noindex`, `blocked_by_robots`, `canonical_mismatch`, `soft_404`, `redirect`,
   `discovered_not_indexed`, `crawled_not_indexed`, `duplicate`, `error_5xx`, `ok`.
2. For every bucket, confirm with a cheap crawl before recommending a fix:
   `seo_crawl_site(start_url, max_urls=50, include_rules=["robots-blocked","canonical-mismatch","noindex-on-200","client-error","server-error"])`.
3. Compare with the source of truth: sitemap `loc` vs page canonical vs HTTP status.
4. Yandex: `yandex_indexing_state()` → check `search-urls/events` reasons and diagnostics (the host
   comes from config; the tool takes no inputs).
5. Propose fixes in priority order: `noindex`/`Disallow` → canonical → status codes → sitemap membership.
6. After the fix ships: `seo_recrawl_request` (Yandex, quota-checked). Google has no URL-submission
   API — say so; the GSC sitemap path is gated and currently answers `WRITE_DISABLED`.
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
