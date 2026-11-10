# ADR-0003 — Write gating: dry-run, confirm token, idempotency key

**Status:** accepted (2026-11-10)

## Context

Three provider calls change state: Yandex recrawl queue (hard daily quota), sitemap submit/delete
(Google, Bing), IndexNow submit. A wrong automatic call can burn a quota or delete a sitemap.

## Decision

1. **Default is read-only.** Without `--allow-write` (CLI) the command prints the plan and exits 0 with
   `"dry_run": true`.
2. **Two-phase apply.** A dry run returns `confirm_token` = HMAC(payload + config + 5 min TTL). Applying
   requires the token; a changed payload invalidates it.
3. **Idempotency.** Every apply stores `(kind, target, payload_hash, idempotency_key)` in the audit
   table; a repeat returns the previous result instead of calling the provider again.
4. **Quota guard.** Recrawl checks `GET /recrawl/quota` before queueing and refuses `urls > quota_left`
   with `QUOTA_EXCEEDED`.
5. **Audit.** `runs` gets a row with actor (`cli` / `mcp:<client>`), plan, applied ids and result.

## Consequences

* Agents cannot silently mutate a provider.
* Slightly more clicks for a human — acceptable for destructive operations.
* MCP exposes the same semantics via `destructiveHint: true` + `confirm_token`.

## Rejected

* Auto-apply with a config flag: too easy to leave on in an agent loop.
