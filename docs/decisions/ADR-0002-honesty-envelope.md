# ADR-0002 — Honesty envelope for every provider result

**Status:** accepted (2026-11-10)

## Context

SEO APIs are lossy in different ways: Google caps search analytics at 50,000 rows/day and silently
truncates by clicks; Yandex exposes only the top 3,000 queries and has no query×page report; Metrika
samples; CrUX returns 404 for low-traffic URLs. Agents and humans both tend to treat "no rows" as
"no problem".

## Decision

Every provider call returns `ProviderResult` with:

* `rows`, `total`, `truncated`, `data_age_days`;
* `limitations[]` — enum-like codes (`rows_cap_50000`, `top_3000_queries`, `no_query_page_join`,
  `sampled`, `low_traffic`, `degraded_host`, …);
* `warnings[]` — human hints.

Reports and MCP responses must render these fields. A missing limit is a bug, not a style issue.

## Consequences

* Slightly larger payloads.
* Agents cannot claim completeness — the contract forbids it.
* Provider adapters own the limit constants (`providers/<name>/limits.py`), so a quota change is a
  one-line change plus a doc update.

## Enforcement

* Contract tests assert `truncated` for a fake 50k response.
* Reviewer checklist item: "path where data is capped but `truncated=false`".
