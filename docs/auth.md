# OAuth connectors

`lv24-seo auth login` can authorize providers with OAuth instead of a pasted token. OAuth is
available where the provider supports it:

| Provider | Connector | Guide |
|---|---|---|
| Google Search Console | Desktop app, loopback `127.0.0.1` + PKCE | [auth-google.md](auth-google.md) |
| Yandex Webmaster | Yandex OAuth, `verification_code` + refresh | [auth-yandex.md](auth-yandex.md) |
| Yandex Metrika | same Yandex app, separate token slot | [auth-yandex.md](auth-yandex.md) |
| Bing Webmaster | API key only | [cli.md](cli.md#authentication) |
| IndexNow | public key file | [data-sources.md](data-sources.md) |
| CrUX / PSI | Google API key | [cli.md](cli.md#authentication) |

## Quick start

```bash
# Google Search Console
lv24-seo auth set --provider gsc_oauth_secret --stdin
lv24-seo auth login --provider gsc

# Yandex Webmaster / Metrika (one app, two token slots)
lv24-seo auth set --provider yandex_oauth_secret --stdin
lv24-seo auth login --provider yandex --no-browser
lv24-seo auth login --provider yandex --code <code>
lv24-seo auth login --provider metrika
```

Client ids live in `[auth.gsc]` / `[auth.yandex]`; client secrets never do. Tokens are stored in
the OS keychain (or the explicit `0600` file fallback) and are never printed by `auth status`,
`doctor`, `--json` or the logs.

## Fallbacks

* GSC keeps the service-account path for unattended runs; OAuth wins when a complete grant is
  stored. Google OAuth apps in **Testing** issue refresh tokens that expire after 7 days.
* Yandex keeps the static `yandex_webmaster_token` / `yandex_metrika_token` path; OAuth refreshes
  when a refresh token and expiry are stored.
* Bing, IndexNow, CrUX and PSI have no OAuth connector by design.

## Readiness diagnostics

`lv24-seo auth status --json` and `lv24-seo doctor --json` classify every OAuth provider
(`gsc`, `yandex`, `metrika`) into one stable code. The checks are offline: they read the
config and the secret store, never the provider.

| Code | Meaning | Fix |
|---|---|---|
| `oauth_client_missing` | `[auth.gsc].client_id` or `[auth.yandex].client_id` is empty (or a documented placeholder). | Register an app and set the client id. Google: <https://console.cloud.google.com/apis/credentials> (Desktop app). Yandex: <https://oauth.yandex.ru/>. |
| `oauth_client_secret_missing` | The client id is set but the client secret is not stored. | `lv24-seo auth set --provider gsc_oauth_secret --stdin` or `--provider yandex_oauth_secret`. |
| `oauth_token_missing` | Client id and secret are present, but no access or refresh token is stored. | `lv24-seo auth login --provider gsc\|yandex\|metrika`. |
| `oauth_token_expired` | The stored expiry is in the past. With a refresh token the next request refreshes it; without one the user must log in again. | Run any read command, or `lv24-seo auth login --provider <name>` when no refresh token exists. |
| `oauth_scope_missing` | The recorded token scopes do not include the configured scopes. | Log in again and grant the missing scope, or remove it from `[auth.*].scopes`. |
| `oauth_scope_unknown` | No scope was ever recorded (a grant stored before scopes were persisted). This is a limitation, never a pass. | Run a login or a token refresh once; the returned scope list is recorded. |
| `oauth_redirect_invalid` | A configured redirect URI would be rejected. Google requires `http://127.0.0.1:<port>/...`; Yandex requires `https://oauth.yandex.ru/verification_code`. | Clear `[auth.*].redirect_uri` to use the provider default, or set a valid value. |
| `oauth_ready` | Client id, client secret, token, scopes and redirect all check out. | Nothing to do. |

The checks run in that order, with `oauth_redirect_invalid` evaluated right after the client
secret and before the token checks: a redirect that cannot complete a login must be fixed
before the user is told to log in. A missing app is reported as a warning (`ok: false` in
`doctor`) with the registration link; it never fails the environment check or the exit code.

`auth status --json` adds an `oauth` object per provider with `state`, `detail`,
`client_id_present`, `client_secret_present`, `access_token_present`,
`refresh_token_present`, `expires_at`, `scopes_recorded`, `next_action` and `limitations`.
The existing `providers`, `present` and `missing` fields are unchanged. `doctor --json` adds
the checks `gsc_oauth`, `yandex_oauth` and `metrika_oauth`, each with `id`, `ok`, `detail`,
`code` and `next_action`. Neither surface prints a token or a client secret.

### Recorded scopes

The granted scopes are stored as one space-separated string, the same shape the token
endpoints return, in `gsc_token_scopes`, `yandex_token_scopes` and
`yandex_metrika_token_scopes`. A login records the scopes the provider echoed, or the
requested scopes when the response omits them; a refresh records the returned list and keeps
the previous one when the response omits it. `auth logout` deletes the entry. A hand-edited
JSON array is also accepted when read. These names live in `OAUTH_SECRET_NAMES`, not in the
stable `SECRET_NAMES` tuple.
