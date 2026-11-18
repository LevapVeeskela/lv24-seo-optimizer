# ADR-0005 — OAuth readiness diagnostics in the engine

Status: accepted (2026-11-18, engine 0.14.0)

## Context

The OAuth connectors added in 0.13.0 need a Google Cloud Desktop client and a Yandex OAuth app.
When one of those prerequisites is missing, `auth login` used to fail with a provider error page or a
generic message. The CLI, the MCP server and the desktop all need the same answer, and none of them
may read a token to produce it. Users also configure providers in different orders: a client id
without a secret, a token without scopes, a token that expired before the first refresh.

## Decision

1. Readiness is classified in the engine (`src/lv24_seo/providers/oauth_status.py`), not in each
   surface. `auth status --json` exposes an `oauth` object per provider and `doctor --json` exposes
   one check per provider.
2. The classifier uses eight stable codes: `oauth_client_missing`,
   `oauth_client_secret_missing`, `oauth_token_missing`, `oauth_token_expired`, `oauth_scope_missing`,
   `oauth_scope_unknown`, `oauth_redirect_invalid`, `oauth_ready`.
3. Precedence is redirect → client id → client secret → token → scope. The first actionable fix is
   the one reported; later states are not guessed.
4. The checks are read-only and make no provider call. They never read or print a token value; they
   report presence booleans, expiry and recorded scopes only.
5. A missing OAuth app is a non-fatal `doctor` warning with the registration link. Existing users who
   authenticate with service accounts or plain tokens must not fail `doctor` because an optional
   OAuth app is absent.
6. Scope lists are stored under OAuth-only secret names (`gsc_token_scopes`, `yandex_token_scopes`,
   `yandex_metrika_token_scopes`) in `OAUTH_SECRET_NAMES`. The public `SECRET_NAMES` tuple stays
   stable. A token created before scope recording reports `oauth_scope_unknown` as a warning, never a
   pass.

## Alternatives considered

* **Classify in the desktop only.** Rejected: CLI and MCP users have the same failure, and the
  desktop would duplicate provider rules.
* **Treat a missing app as fatal in `doctor`.** Rejected: OAuth is optional; key- and
  service-account users would see a false failure.
* **Infer scopes from the configured scope list.** Rejected: that would report a pass without
  evidence from the token.

## Consequences

* One classifier and one set of codes serve CLI, MCP and the desktop.
* `doctor` can return `ok:false` for an optional provider without changing `healthy` or the exit code.
* A redirect misconfiguration is reported before token advice, so the config must be fixed first.
* Live consent remains untested until a real Google Desktop client and Yandex OAuth app exist; the
  classifier only proves that the local prerequisites are consistent.
