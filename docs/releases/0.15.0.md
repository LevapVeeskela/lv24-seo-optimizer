# 0.15.0 - Security, TLS, Schema.org and accessibility rules

Status: **released** (2026-11-18) on PyPI, npm and the MCP Registry. Desktop `0.9.5` bundles this
engine.

Minor release: the crawler gains four rule packs that close the audit gaps identified in the
`siteaudit-mcp` review. No new dependency, no provider call, no change to the write gates.

## Added

### `security-headers` (medium)

Status-200 HTML pages are checked for the baseline HTTP security headers:

* `Strict-Transport-Security` on HTTPS: missing or `max-age` below 180 days;
* `Content-Security-Policy`: missing, or containing `unsafe-inline` / `unsafe-eval`;
* `X-Frame-Options`: missing unless the CSP declares `frame-ancestors`;
* `X-Content-Type-Options`: missing or not `nosniff`;
* `Referrer-Policy`: missing or `unsafe-url`;
* `Permissions-Policy`: missing;
* cookie flags from every `Set-Cookie`: `Secure` on HTTPS, `HttpOnly`, and `SameSite=None`
  without `Secure`. Cookie values are never included in a finding.

### `tls-certificate` (high)

The fetcher now records the negotiated TLS facts in `PageData.tls`: protocol version, cipher,
certificate expiry, issuer, subject and SAN. The rule reports:

* an unreadable certificate (`unknown`);
* expired, expiring within 14 days, or expiring within 30 days;
* TLS 1.0/1.1;
* a hostname mismatch against SAN (single-label wildcard supported) or CN fallback;
* RC4/3DES/NULL/EXPORT ciphers.

Expiry comparisons use the page's `fetched_at` timestamp, so a crawl result is deterministic and
does not depend on when the report is rendered.

### `schema-required` (medium)

Required properties for the common rich-result types not already covered by `jsonld-required`,
`jsonld-event` and `jsonld-organization`: Product, Offer, BreadcrumbList, FAQPage (including nested
Question name/acceptedAnswer), Recipe, JobPosting, VideoObject, LocalBusiness, Course and
SoftwareApplication. `@graph` containers and `@type` lists are flattened; blank values count as
missing; findings are capped per page.

### `a11y-basics` (medium)

Basic accessibility checks backed by new parser fields:

* form inputs without a label, `aria-label`, `aria-labelledby` or `title` (explicit `<label for>`,
  wrapping labels and multiple labels are handled);
* buttons without visible text, `aria-label`, `aria-labelledby` or `title`;
* links with no text, accessible name or image alt;
* a missing `<main>` landmark or skip link;
* heading-level jumps greater than one.

## Changed

* `FetchResult`/`PageData` carry `tls`, `set_cookie` (every value, not just the last one) and
  `fetched_at`.
* `PageData` carries `link_details`, `inputs`, `buttons`, `headings`, `has_main` and
  `has_skip_link`; the tolerant HTML parser fills them without changing existing fields.
* The rule set grows from 30 to 34 rules. Discovery is still automatic; no registry edit.

## Not changed

* MCP stays at **12 tools**, SQLite schema at **version 5**, CLI at **25 top-level commands**.
* No new required dependency; no scraping; no paid provider.
* Existing rule IDs, severities and report envelopes are unchanged.

## Verification

Offline gate on the release commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

Each rule has focused tests for positive/negative/boundary cases, malformed input, JSON-safe
details and discovery; the full offline suite is green. TLS and Set-Cookie capture have their own
fetcher tests.

## Limitations

* `security-headers` checks presence and a few weak values, not full CSP policy quality.
* `tls-certificate` needs the TLS facts captured by the fetcher; a page fetched without them is
  reported as `unknown` rather than silently passing.
* `schema-required` checks the listed types only; nested `Product.offers` nodes are not inspected
  as separate items.
* `a11y-basics` is a heuristic subset, not a WCAG conformance audit; duplicate hrefs keep the first
  anchor's accessible-name data.
