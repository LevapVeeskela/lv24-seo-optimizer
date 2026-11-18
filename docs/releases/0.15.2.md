# 0.15.2 - Data-access status per provider

Status: **released** (2026-11-18) on PyPI, npm and the MCP Registry. Desktop `0.9.7` bundles this
engine.

Patch release: `auth status` now says how each provider is actually authenticated for data access,
so a saved token or service account is not mistaken for "not connected" just because no OAuth app is
configured.

## Added

### `access` in `auth status --json`

Each provider (`gsc`, `yandex`, `metrika`, `bing`, `crux`, `psi`) gets:

```json
"access": { "yandex": { "mode": "token", "ready": true } }
```

`mode` is one of:

* `service_account` - GSC `service_account_file` is configured;
* `token` - a static Webmaster/Metrika/Bing/CrUX/PSI token or API key is stored;
* `oauth` - a refresh token from an OAuth login is stored;
* `none` - no data-access credential is configured.

The existing `providers`, `present`, `missing` and `oauth` fields are unchanged. `access` describes
data access, while `oauth` describes whether the optional OAuth app can be used.

### Desktop

Desktop `0.9.7` uses `access` for the Connected/Not connected badge. Yandex and Metrika with saved
tokens, GSC with a service account, and Bing/CrUX/PSI keys now show as connected. The OAuth badge
stays separate and says "OAuth not configured (optional)" when the provider already works without
it.

## Not changed

* MCP stays at **12 tools**, SQLite schema at **version 5**, CLI at **25 top-level commands**.
* No new dependency; no provider call; no change to the write gates or the OAuth flows.
* `auth status` remains value-free: it reports modes and presence, never a secret.

## Verification

Offline gate on the release commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

New tests cover every mode, the GSC service-account precedence, the shared Yandex refresh token for
Metrika, and the blank service-account path. The full offline suite is green.

## Limitations

* `access` reports configuration/presence, not a live provider call; an expired or revoked token is
  still discovered on the next read.
* A syntactically valid but unregistered OAuth client id cannot be detected locally.
