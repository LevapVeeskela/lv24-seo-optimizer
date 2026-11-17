# 0.13.0 - OAuth connectors for Google and Yandex

Status: **released** (2026-11-18) on PyPI, npm and the MCP Registry. Desktop `0.9.0` bundles engine
`0.11.0`; desktop `0.9.1` follows with the Connect buttons.

Minor release: Google Search Console, Yandex Webmaster and Yandex Metrika can be authorized with an
OAuth connector instead of pasting a token. Bing Webmaster, IndexNow, CrUX and PSI stay key-based:
those providers have no OAuth flow.

## Added

### Google Search Console

`lv24-seo auth login --provider gsc [--no-browser] [--port N] [--code CODE] [--json]`:

* Google Cloud **Desktop app** client, loopback callback `http://127.0.0.1:<port>/callback`, S256
  PKCE and a random `state`; `access_type=offline` + `prompt=consent` for a refresh token.
* Stores `gsc_access_token`, `gsc_refresh_token` and `gsc_token_expires_at` in the OS keychain;
  the client secret is set once with
  `lv24-seo auth set --provider gsc_oauth_secret --stdin`.
* `OAuthTokenProvider` refreshes within 60 seconds of expiry under a lock, reading the store on
  every call so a running MCP process sees a CLI login.
* `--no-browser` prints the URL to stderr (stdout stays one JSON document) and waits; `--code`
  reuses the stored PKCE verifier and needs `--port` or `[auth.gsc].redirect_uri` to repeat the
  redirect exactly.
* `services.py` precedence: OAuth grant > service-account file > static `gsc_token`. The
  service-account path is unchanged for unattended runs.
* `doctor` recognizes an OAuth grant instead of reporting a missing credential.

### Yandex Webmaster and Yandex Metrika

`lv24-seo auth login --provider yandex|metrika [--no-browser] [--code CODE] [--json]`:

* One Yandex OAuth app can carry `webmaster:hostinfo` and `metrika:read`; the CLI keeps separate
  token slots so either engine can be revoked independently.
* API-access apps use the `https://oauth.yandex.ru/verification_code` redirect: the user approves
  access, Yandex shows a short code, and the CLI exchanges it. This works on a desktop, over SSH and
  on a VPS without a loopback server.
* Form-encoded code exchange and `grant_type=refresh_token`; access/refresh/expiry are stored per
  provider. `ensure_fresh_access_token` refreshes inside the 60-second margin, keeps static or
  long-lived tokens working, and maps a failed refresh to `AUTH_INVALID` with the exact
  `auth login` next action.
* `auth logout --provider yandex|metrika` removes the matching OAuth tokens.

### Configuration and secrets

* `[auth.gsc]` / `[auth.yandex]`: `client_id`, `scopes`, `redirect_uri`; environment overrides
  `LV24_SEO_GSC_CLIENT_ID`, `LV24_SEO_GSC_REDIRECT_URI`, `LV24_SEO_YANDEX_CLIENT_ID`,
  `LV24_SEO_YANDEX_REDIRECT_URI`. Client secrets are never part of the config.
* `OAUTH_SECRET_NAMES` keeps the public `SECRET_NAMES` tuple stable while accepting the companion
  OAuth secrets; `auth set --provider gsc_oauth_secret|yandex_oauth_secret --stdin` stores the
  client secret.
* `auth login` text output says `OAuth login for ...` and shows scopes/expiry; JSON carries no
  tokens.

## Not changed

* MCP stays at **12 tools**; SQLite schema stays at **version 5**; CLI stays at **25 top-level
  commands**.
* No new required dependency: the connectors use the existing `httpx` client and the OS keychain.
* Bing Webmaster, IndexNow, CrUX and PSI keep their API-key/key-file auth; there is no OAuth to add.
* Existing service-account, static-token and API-key paths keep working.

## Verification

Offline gate on the release commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

The suites include 34 Google OAuth tests and 32 Yandex OAuth tests: PKCE/state/URL, a real
`127.0.0.1` loopback callback, code exchange and refresh against `httpx.MockTransport`, expiry
margins, store/delete, service precedence, CLI `--code`/`--no-browser`, missing secrets, error
mapping and token-free envelopes/reprs.

**Live consent was not exercised**: no Google Cloud Desktop client or Yandex OAuth app was
registered at release time. The registration steps are in
[`docs/auth-google.md`](auth-google.md) and [`docs/auth-yandex.md`](auth-yandex.md).

## Limitations

* Google OAuth apps in **Testing** issue refresh tokens that expire after 7 days; unattended GSC
  automation should keep the service account. A public app needs Google verification for the
  sensitive `webmasters.readonly` scope.
* The Yandex `verification_code` flow cannot verify `state` across separate `--no-browser` and
  `--code` commands; the code is short-lived (10 minutes) and the client secret is still required.
  A single interactive command is the recommended path.
* A long-running MCP process caches its provider client; a token that expires mid-process is
  refreshed on the next client build.
* Bing, IndexNow, CrUX and PSI have no OAuth connector by design.
