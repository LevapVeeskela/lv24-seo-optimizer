# Landscape research — November 2026

Five parallel research tracks: Google APIs, Yandex APIs, existing SEO MCP servers and tooling,
technical-audit signals, and project/tooling design. Facts below were gathered on 2026-11-10 and must
be re-verified before each release (provider limits change silently).

## 1. Google

| API | Auth | Free quota | Verdict |
|---|---|---|---|
| Search Analytics | OAuth / service account | 1,200 QPM/site; **50k rows/day/site/type** | core |
| URL Inspection | same | 2,000/day/site, one URL per call | core |
| Sitemaps + Sites | OAuth with `webmasters` | 20 QPS | core (write, gated) |
| Indexing API | service account | 200/day | **skip** — JobPosting/BroadcastEvent only |
| Bulk export → BigQuery | GSC setting + billing account | free within BQ free tier | optional, only after outgrowing the API |
| CrUX | API key | 150 QPM, cannot be raised | core (field CWV) |
| PageSpeed Insights | API key | ~25k/day | core (lab) |
| GA4 Data API | OAuth / SA | 200k tokens/day/property | optional |
| Bing Webmaster | API key | 10k URL submits/day | core |
| IndexNow | key file | free | core (Bing/Yandex; **not** Google) |

Key facts: GSC data lags 2–3 days; grouping by `page` **and** `query` drops rows by design; 50k rows is a
silent truncation sorted by clicks; rich-result validation has no public API; Google Trends has no open
API; the OOB OAuth flow is dead (loopback only); a desktop OAuth app in "Testing" mode expires refresh
tokens after 7 days.

## 2. Yandex

| API | Auth | Free quota | Verdict |
|---|---|---|---|
| Webmaster v4 hosts & summary | `webmaster:hostinfo` | free, no published RPD | core |
| Webmaster indexing / search-urls / diagnostics | same | free | core |
| Webmaster queries | same | top **3,000**/period, ≤500/page | core |
| Webmaster sitemaps + recrawl | same | recrawl has a **hard daily quota** | core (write, gated) |
| Webmaster PRO | paid | — | excluded |
| Metrika stat v1 | `metrika:read` | 5,000 req/day; 200 req/5 min | core |
| Yandex Search API (XML successor) | Cloud API key | paid (~488 ₽/1000) | excluded |
| Yandex Business / Sprav | none | — | **no API** — browser only, out of scope |

Key facts: `host_id` is `scheme:host:port` (`https:lv24.by:443`) and must be resolved via `/hosts/`;
repeated query params, not CSV; `401` = dead token, `403` = host not verified; no query×page join;
~4-day lag; the service degrades to `HOST_NOT_LOADED` during maintenance; tokens last ~6 months and the
implicit flow returns no refresh token.

## 3. Existing MCP servers (what already exists)

| Project | Lang | License | Scope | Gap we fill |
|---|---|---|---|---|
| `AminForou/mcp-gsc` | Py | MIT | ~20 GSC tools | single property, raw shapes |
| `ahonn/mcp-server-gsc` | TS | **no license** | GSC basics | not reusable |
| `every-app/open-seo` | TS | MIT | 57 tools + skills, GSC/GA4/**DataForSEO** | requires a paid DataForSEO key |
| `dataforseo/mcp-server-typescript` | TS | Apache-2.0 | DataForSEO v3 | pay-per-call |
| `serpapi/serpapi-mcp` | Py | MIT | SERP search | 250 free searches |
| `bzsasson/screaming-frog-mcp` | Py | MIT | headless SF crawl | needs a £199 licence |
| `ChromeDevTools/chrome-devtools-mcp` | TS | Apache-2.0 | Lighthouse/perf | per-page lab only |
| Yandex Webmaster MCPs (3 small forks) | TS | MIT | queries/indexing | no canonical server, ★0–6 |
| `seyka-ai/yandex-tools-mcp` | JS | MIT | Search/Wordstat/Webmaster/Metrika | broad but shallow |
| `tmolavi/siteprobe` | Py | MIT | audit + fix | single site, no scheduling |

**Three unowned problems:** (1) free-first multi-engine fusion per property; (2) scheduled diffable
reports with deltas; (3) an honesty/quota layer inside tool responses.

## 4. Tooling landscape (paid, optional)

| Platform | API | Free tier | Entry price | Public-repo safe? |
|---|---|---|---|---|
| DataForSEO | REST | ~$1 trial | pay per task | BYO key |
| SerpApi | REST | 250/mo | $25/mo | BYO key |
| Ahrefs | REST + MCP | none | $129/mo | BYO key, no resale |
| Semrush | REST + MCP | none | API units | BYO key, no resale |
| SE Ranking | REST | trial | $50 min | BYO key |
| Serpstat | REST | none | ~$59/mo | BYO key |
| Moz | REST | 50 rows/mo | $20/mo | skip (useless tier) |
| Majestic | REST | none | $399/mo | skip |
| Screaming Frog | CLI + MCP | 500 URLs | £199/yr per seat | document only |
| Sitebulb | MCP only, no REST | trial | $13+/mo | skip |
| JetOctopus / Botify / Oncrawl / Similarweb | REST/quote | none | $349+/mo | skip |

## 5. Technical audit signals (ranked by impact)

1. Accidental `noindex` / `X-Robots-Tag` / robots `Disallow` — kills pages outright.
2. Sitemap URLs that are not 200 + indexable + self-canonical.
3. Bot-shell vs browser parity (SPA cloaking/soft-block).
4. Canonical conflicts (DOM vs header vs sitemap vs hreflang).
5. 5xx/429 served to Googlebot/YandexBot.
6. Redirect chains, soft-404s, correct 410s.
7. Broken internal links and orphans from the raw-HTML link graph.
8. JSON-LD required properties, ISO dates, real prices.
9. Host/HTTP→HTTPS single hop, TLS expiry.
10. Duplicate titles/descriptions, missing H1.
11. hreflang reciprocity, self-reference, `x-default`.
12. Crawl-waste ratio and indexed-count delta.

Needs paid tools or humans: backlinks/authority, rank tracking beyond the APIs, E-E-A-T, manual actions,
legal/consent review.

## 6. Engineering decisions taken from this research

* **Python 3.12 + uv** — one language for the MCP server, CLI, crawler and reports.
* **stdio-first MCP**, optional stateless Streamable HTTP; no deprecated SSE.
* **10 tools**, composed workflows (`seo_weekly_report`) instead of an API mirror.
* **Typer CLI**, JSON envelope, `--allow-write` gating, generated schedulers.
* **SQLite WAL + gzipped JSONL**; 90-day raw retention.
* **SMTP by default**, Resend/Brevo optional; Telegram for criticals.
* **Apache-2.0**, DCO, no CLA.
* **CI**: public repo, `ubuntu-latest` only, two Python versions, `concurrency`, `permissions: read`,
  SHA-pinned actions, artifacts ≤7 days — zero billable minutes beyond the free public-repo tier.

## Sources

Google: `developers.google.com/webmaster-tools/limits`, `/v1/urlInspection.index/inspect`,
`/search/apis/indexing-api/v3/quota-pricing`, `developer.chrome.com/docs/crux/api`,
`developers.google.com/speed/docs/insights/v5/get-started`,
`developers.google.com/analytics/devguides/reporting/data/v1/quotas`,
`learn.microsoft.com/bingwebmaster`, `indexnow.org/documentation`.

Yandex: `yandex.ru/dev/webmaster/doc/ru/llms.txt`, `…/concepts/getting-started.md`,
`…/tasks/how-to-get-oauth.md`, `…/reference/host-query-analytics.md`, `…/reference/host-sitemaps-get.md`,
`yandex.com/dev/metrika/en/intro/quotas`, `yandex.com/dev/metrika/en/stat/sampling`,
`aistudio.yandex.ru/ru/docs/search-api/pricing`.

Ecosystem: `modelcontextprotocol.io/registry`, `py.sdk.modelcontextprotocol.io`,
`agents.md`, `llmstxt.org`, `agentskills.io/specification`, `code.claude.com/docs/en/skills.md`,
`opencode.ai/docs/skills`, `docs.github.com/billing/concepts/product-billing/github-actions`.
