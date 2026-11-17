# 0.14.0 - OAuth readiness diagnostics

Status: **released** (2026-11-18) on PyPI, npm and the MCP Registry. Desktop `0.9.1` bundles engine
`0.13.1`; desktop `0.9.2` follows with this engine and the indexing card.

Minor release: OAuth failures are now self-diagnosable. Live consent still requires a registered
Google/Yandex app, but the engine can say exactly which prerequisite is missing instead of returning
a generic error.

## Added

### `auth status`

`auth status --json` gains an `oauth` object for `gsc`, `yandex` and `metrika`. Each entry carries
`state`, `client_id_present`, `client_secret_present`, `access_token_present`,
`refresh_token_present`, `expires_at`, `scopes_recorded`, `next_action` and `limitations`.
The existing `providers`/`present`/`missing` fields are unchanged.

### `doctor`

`doctor --json` adds `gsc_oauth`, `yandex_oauth` and `metrika_oauth` checks with `id`, `ok`,
`detail`, `code` and `next_action`. A missing app is a warning with the registration link, never a
crash. The checks are non-fatal: `healthy` and the exit code still depend only on the existing fatal
checks. A provider that is not configured is labelled optional.

### Stable states

| Code | Meaning |
|---|---|
| `oauth_client_missing` | `[auth.<provider>].client_id` is empty |
| `oauth_client_secret_missing` | client id is set but the `*_oauth_client_secret` is absent |
| `oauth_token_missing` | client id + secret are set but no access/refresh token exists |
| `oauth_token_expired` | the stored expiry is in the past; says whether a refresh is available |
| `oauth_scope_missing` | the stored token scopes do not include the configured scopes |
| `oauth_scope_unknown` | the token predates scope recording; a warning, never a false pass |
| `oauth_redirect_invalid` | Google redirect is not loopback, or Yandex is not `verification_code` |
| `oauth_ready` | all prerequisites are present |

Precedence is redirect → client → client secret → token → scope, so the first actionable fix is the
one shown.

### Scope recording

`gsc_token_scopes`, `yandex_token_scopes` and `yandex_metrika_token_scopes` are added to
`OAUTH_SECRET_NAMES` (the public `SECRET_NAMES` tuple stays stable). The Google login/refresh and the
Yandex `store_tokens`/refresh paths store the scope list space-separated; logout removes it.

## Not changed

* MCP stays at **12 tools**, SQLite schema at **version 5**, CLI at **25 top-level commands**.
* No new required dependency and no provider call in the readiness checks.
* Existing OAuth token/secret names and the login/logout behavior are unchanged.

## Verification

Offline gate on the release commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

The new `tests/unit/test_oauth_status.py` covers all states for the three providers, scope
round-trip/delete, the redirect matrix, `auth status` backward compatibility, `doctor` codes and the
no-token-in-output/repr/log rule. No Google or Yandex request is made.

## Limitations

* Redirect configuration is checked before the token state, so a wrong redirect must be fixed before
  the login advice is shown.
* `oauth_scope_unknown` is a warning: a token created before scope recording cannot prove its
  permissions.
* A provider that is not configured still appears as an `ok:false` OAuth check in `doctor`, with an
  "optional" note; it does not change `healthy` or the exit code.
* Live OAuth consent remains untested until a real Google Desktop client and Yandex OAuth app are
  registered.
