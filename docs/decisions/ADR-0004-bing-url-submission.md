# ADR-0004 — Bing URL submission, quota and read-only sitemap/issue views

**Status:** accepted (2026-11-17). Extends ADR-0003 (write gating) with a fourth gated action.

## Context

Bing Webmaster Tools exposes URL-level submission through `SubmitUrlbatch`, plus read-only
`GetUrlSubmissionQuota`, `GetFeeds` and `GetCrawlIssues`. Real lv24.by responses showed that the
documented field names do not match the usual assumptions:

* `GetUrlSubmissionQuota` returns `{DailyQuota, MonthlyQuota}` but both values are **remaining**
  submissions, not totals. The monthly window can bind before the daily one and its reset is
  undocumented.
* `SubmitUrlbatch` succeeds with `200 {"d": null}` and no per-URL status. Bing documents no
  idempotency; a duplicate URL may consume quota.
* `GetCrawlIssues` returns `{Url, HttpCode, Issues, InLinks}` where `Issues` is an integer
  bitmask, not a type/severity/date.
* Auth failures arrive as HTTP 400 with `{"ErrorCode": N, "Message": ...}`, not 401/403.

## Decision

1. **Surface.** `bing submit`, `bing quota`, `bing sitemaps [--details FEED_URL]` and `bing issues`
   are CLI/desktop commands. No 13th MCP tool is added; the MCP surface stays at 12.
2. **Write gate.** `bing submit` is a fourth `WriteGate` action (`bing_submit`) with the same
   `--allow-write` plan, confirm token and SQLite ledger as the other writes.
3. **Quota.** Expose `daily_remaining` / `monthly_remaining` and keep the provider object in
   `raw`. Never compute used/total. Pre-check both windows before the first batch and attach
   `quota_monthly_can_bind_first` / `quota_monthly_reset_undocumented`.
4. **Submission.** Deduplicate client-side, split at 500 URLs per `SubmitUrlbatch`, and report
   `status: accepted` / `accepted`, never "indexed". Attach
   `submit_idempotency_undocumented`; the ledger still blocks a replayed token.
5. **Errors.** Parse `ErrorCode` before the HTTP status: `3 -> AUTH_INVALID`,
   `14 -> PROPERTY_NOT_FOUND`, `7 -> INVALID_INPUT`, `4/5 -> QUOTA_EXCEEDED`; keep
   401/403/404/429/5xx as fallbacks. Never echo the provider `Message` or the API key.
6. **Read-only views.** `GetFeeds` rows map `Url`, `Type`, `Status`, `Submitted`, `LastCrawled`,
   `UrlCount`, `FileSize`, `Compressed`; state that per-URL errors and crawl/index status are not
   exposed. `GetCrawlIssues` decodes the bitmask into labels and keeps `issues_bits` plus unknown
   fields in `raw`. `GetFeedDetails` is exposed only as `bing sitemaps --details` and was not
   verified against a live response in this wave.

## Consequences

* A URL submission cannot silently exceed the remaining daily or monthly quota.
* The result wording cannot be mistaken for an indexing guarantee.
* The shared HTTP client gains `raise_for_status=False` so a provider adapter can parse a
  structured error body; default behavior is unchanged.

## Rejected

* Exposing URL submission as an MCP tool now: it would break the 12-tool contract before the
  desktop workflow is validated.
* Treating `DailyQuota` / `MonthlyQuota` as totals: contradicted by real responses and would
  compute a false "used" value.
