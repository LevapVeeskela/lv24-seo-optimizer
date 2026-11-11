---
name: seo-technical-audit
description: Use before a release, after a deploy, after a redesign or migration, or when the user asks to check technical SEO of a site (robots, sitemaps, canonicals, structured data, hreflang, redirects, Core Web Vitals, crawl budget).
version: 0.1.0
requires_tools: [seo_crawl_site, gsc_search_analytics, yandex_indexing_state, seo_sitemap_submit]
---

# Technical SEO audit

## When to use

- Pre-release or post-deploy verification of a web app.
- "проверь техничку", "почему страницы не индексируются", after routing/SSR/prerender changes.
- Monthly hygiene run (sitemap drift, redirect chains, TLS expiry).

## When not to use

- Content/keyword strategy questions.
- Backlink analysis — needs a paid provider; say so.

## Inputs

Base URL, crawl budget (`max_urls`, default 500, max 5000), template list when the site is large
(audit one URL per template before crawling everything).

## Recipe

1. `seo_crawl_site(start_url, max_urls=50, include_rules=["robots-blocked","canonical-mismatch","noindex-on-200","client-error","server-error"])`
   → `{summary, issues:[{rule_id, severity, url, detail}]}`.
2. Read results by severity, not by rule order:
   - **critical**: `robots-blocked`, `sitemap-url-noindex`
   - **high**: `sitemap-url-not-ok`, `broken-internal-link`, `canonical-mismatch`, redirect chains, soft-404, missing JSON-LD required props
   - **medium**: duplicate title/description, missing H1, alt coverage, hreflang reciprocity
3. Compare with the source of truth: sitemap `loc` vs page canonical vs HTTP status.
4. `yandex_indexing_state()` → check `search-urls/events` reasons and diagnostics.
5. Produce a fix list grouped by owner: server/nginx, app code, content, provider settings.

## Decision tree

```
critical found?
├─ yes → stop content work; fix indexing blockers, re-crawl, then re-check the sitemap
└─ no
   ├─ sitemap membership broken → fix generator; IndexNow submit works today, GSC/Bing
   │   sitemap submit is gated and currently answers WRITE_DISABLED — say which path applies
   └─ only medium issues        → batch into the weekly backlog with expected impact
```

## Output contract

1. **Verdict** — pass / pass with warnings / fail, plus the counts per severity.
2. **Critical findings** — URL, evidence (status, header, DOM snippet), fix.
3. **High/medium table** — rule, count, example URL, effort.
4. **CWV** — p75 LCP/INP/CLS for the origin from the weekly report's CrUX section when the
   origin is configured (field data); lab numbers only when PSI URLs are configured.
5. **What I could not check** — paid-only signals, JS-heavy flows (no rendered-DOM audit exists),
   authenticated pages.

## Hard rules

- Never crawl a site without respecting robots.txt unless the owner explicitly opts out
  (there is no `--ignore-robots` flag; document the exception in the report instead) —
  and never a third-party domain.
- A `noindex` on a URL advertised in the sitemap (`sitemap-url-noindex`) is critical even if
  everything else passes.
- Every finding must include a reproducible command or URL.
