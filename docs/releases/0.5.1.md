# 0.5.1 — write-gate retries and bounded crawl data

This is a bugfix release for the 0.5.x engine. It closes release blockers found after 0.5.0: a
provider failure could permanently mark a write as a duplicate, crawl text was retained without a
bound, MCP annotations claimed two local-state tools were read-only, and several documented limits
were not actually reachable.

No schema migration and no new CLI command or MCP tool. The JSON envelope shape is unchanged;
`schemas/tools.json` gains the additive `writes_local_state` field and two annotation values change
(see below). Existing configs, stored snapshots and report output keep working.

## Fixed

### Write gate retries

An apply now claims the idempotency slot as `pending`, runs the provider call, and only then
promotes the claim to `sent`. If the provider raises, the pending claim is released before the
original exception is re-raised, so a retry with the same plan is allowed. A completed claim, or one
still in flight, is still a duplicate and returns `skipped`.

The `sends` ledger gains `mark_send_sent()` and `release_send()`; a `sent` row is never deleted or
rewritten by a late retry. A process crash between the provider success and the promotion leaves a
`pending` row, which is treated as in-flight (no TTL or automatic cleanup ships in 0.5.1).

### Crawl text bound

`PageData.text` retains at most 20,000 characters. `word_count` is still computed from the full
visible text, so crawl rules and reported counts do not change. No current rule reads `page.text`;
the cap keeps a large crawl from holding every page body in memory.

### MCP annotations

`seo_crawl_site` and `seo_weekly_report` persist local crawl/weekly snapshots, so they are no longer
advertised as read-only:

| Tool | `kind` | `writes_local_state` | `readOnlyHint` | `destructiveHint` | `idempotentHint` |
|---|---|---|---|---|---|
| `seo_crawl_site` | read | `true` | `false` | `false` | `false` |
| `seo_weekly_report` | read | `true` | `false` | `false` | `false` |

They still never mutate provider state and need no confirm token. The other nine tools keep their
previous annotations; the two gated write tools keep `destructiveHint: true`.

### Reachable limits

- Sitemap discovery uses a per-request body budget, so the documented 50 MB per-document cap is
  actually reachable. Ordinary pages keep the 5 MB cap; the 50,000-URL protocol cap is unchanged.
- `stats` and `report weekly` reject `--days` above 3,660 with `INVALID_INPUT` and exit 2 instead of
  building an unbounded window.

### Weekly builder failures

The weekly builder signature is checked once before the call. An incompatible signature gets the
documented fallback report and a warning; a `TypeError` raised *inside* a compatible builder is no
longer swallowed. The command returns `INTERNAL` and no snapshot is persisted, so a fallback payload
cannot poison the stored period.

### GSC truncation

A final page that exactly fills the 50,000-row cap is complete, not truncated. `rows_cap_50000` is
reported only when the provider actually has another row beyond the cap, removing a false positive
for sites that hit the cap exactly.

### Smaller fixes

- The npm launcher on Windows ignores `lv24-seo.cmd` shims (Node cannot spawn them with
  `shell: false`; it fails with `EINVAL`) and prefers `lv24-seo.exe` or an extensionless binary
  before falling back to `uvx`.
- MCP default windows for `gsc_search_analytics` and `seo_opportunities` use `report.timezone`
  instead of a hardcoded zone.
- Multi-dimension metric keys are JSON-encoded, so `("a|b", "c")` and `("a", "b|c")` no longer
  collide. Single-dimension keys are unchanged.
- Store list queries clamp negative limits to `0` instead of passing them to SQLite.
- `report diff --markdown` writes warnings to stderr; stdout stays valid Markdown.
- Scheduler install replaces every managed block, including a damaged block with a missing end
  marker, instead of appending a duplicate job.
- `auth status` includes Yandex Metrika (eight rows), and `auth set --provider metrika` is accepted.
- `python -m lv24_seo.mcp.registry --write` and `--check` now work as documented.

## Verified

- Offline suite: 1114 passed, 1 skipped (1115 collected).
- `ruff check`, `ruff format --check` and `mypy src` are clean.
- New regression tests cover write-gate release/retry, the 20k text cap with exact `word_count`,
  MCP annotations and `writes_local_state`, the 50 MB sitemap budget, `--days` rejection, builder
  signature mismatch versus an internal `TypeError`, GSC exact-cap truncation, the Windows `.cmd`
  launcher case, configured-timezone windows, metric-key collisions, negative limits, stderr
  warnings, scheduler block repair and Metrika auth.

## Limits

- A `pending` write claim left by a crash has no TTL; it is treated as in-flight until an operator
  releases it. This is deliberate: a false retry can duplicate a provider write.
- The crawl text cap is a retention bound, not a parsing bound: link extraction, JSON-LD, hreflang
  and word counting still use the full document.
- The 50 MB sitemap budget applies per document; a sitemap index can still spend that budget on
  several children, bounded by the existing 20-document limit.
- MCP clients that branch on `readOnlyHint` may now ask for confirmation before `seo_crawl_site` or
  `seo_weekly_report`. That is intentional: both persist local state.
- The sitemap cap is a body-size limit, not a time limit; a slow server can still hit the request
  timeout first.
