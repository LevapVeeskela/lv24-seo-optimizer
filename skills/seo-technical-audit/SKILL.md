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

1. `seo_crawl_site(start_url, max_urls=500, rules="all")`
   → `{summary, issues:[{rule_id, severity, url, detail}]}`.
2. Read results by severity, not by rule order:
   - **critical**: `noindex_on_canonical`, `robots_blocked`, `canonical_conflict`, `5xx_to_bots`
   - **high**: sitemap URL not 200/canonical, redirect chain > 1, soft-404, missing JSON-LD required props
   - **medium**: duplicate title/description, missing H1, alt coverage, hreflang reciprocity
3. For SPA targets, run the rendering parity rule on 3–5 templates (raw HTML vs hydrated DOM).
4. `crux origin` + `psi url` for the top 5 landing pages (field first, lab for diagnosis).
5. Cross-check with `gsc_search_analytics(dimensions=["page"])` — pages with impressions but errors in
   the crawl are the urgent ones.
6. Produce a fix list grouped by owner: server/nginx, app code, content, provider settings.

## Decision tree

```
critical found?
├─ yes → stop content work; fix indexing blockers, re-crawl, then re-check the sitemap
└─ no
   ├─ sitemap membership broken → fix generator, resubmit (write, confirm)
   ├─ rendering parity broken   → prerender/bot-shell or SSR fix; re-test with Googlebot UA
   └─ only medium issues        → batch into the weekly backlog with expected impact
```

## Output contract

1. **Verdict** — pass / pass with warnings / fail, plus the counts per severity.
2. **Critical findings** — URL, evidence (status, header, DOM snippet), fix.
3. **High/medium table** — rule, count, example URL, effort.
4. **CWV** — p75 LCP/INP/CLS for the origin and the worst landing pages.
5. **What I could not check** — paid-only signals, JS-heavy flows, authenticated pages.

## Hard rules

- Never crawl a site without respecting robots.txt unless the owner explicitly passes `--ignore-robots`
  (and even then, never a third-party domain).
- Severity is not negotiable: a `noindex` on a canonical URL is critical even if everything else passes.
- Every finding must include a reproducible command or URL.
