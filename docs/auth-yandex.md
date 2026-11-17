# Yandex OAuth for Webmaster and Metrika

`lv24-seo` can authorize Yandex Webmaster and Yandex Metrika with an OAuth application
instead of pasting a token by hand. One Yandex app can carry both scopes, but the CLI keeps
separate token slots for the two engines so either one can be revoked independently.

## 1. Register the Yandex app

1. Open <https://oauth.yandex.ru/> and create an application (**API access** / web service).
2. Add the redirect URI `https://oauth.yandex.ru/verification_code`. Yandex shows a short
   verification code there after approval; the CLI exchanges that code for tokens. This
   works on a desktop, over SSH and on a VPS without a loopback callback server.
3. Request the scopes you need:
   - `webmaster:hostinfo` — Yandex Webmaster hosts, queries and indexing data;
   - `metrika:read` — Yandex Metrika counters, goals and statistics.
4. Copy the **ClientID** and **Client secret**. The client id is public configuration; the
   secret is a credential and belongs in the secret store.

## 2. Configure the client id

```toml
[auth.yandex]
client_id = "0123456789abcdef0123456789abcdef"
scopes = ["webmaster:hostinfo", "metrika:read"]
# Optional; this is the default for API-access apps.
# redirect_uri = "https://oauth.yandex.ru/verification_code"
```

Environment overrides (they win over `seo.toml`):

```bash
export LV24_SEO_YANDEX_CLIENT_ID="0123456789abcdef0123456789abcdef"
export LV24_SEO_YANDEX_REDIRECT_URI="https://oauth.yandex.ru/verification_code"
```

## 3. Store the client secret

The secret is never written to `seo.toml`, an envelope or a log line. Pipe it into the
secret store:

```bash
printf '%s\n' "$YANDEX_CLIENT_SECRET" | lv24-seo auth set --provider yandex_oauth_secret --stdin
```

```powershell
$env:YANDEX_CLIENT_SECRET | lv24-seo auth set --provider yandex_oauth_secret --stdin
```

The value resolves from the OS keychain (recommended) or, when explicitly allowed, a
`0600` secrets file. Environment values are read-only and always win.

## 4. Authorize

Interactive desktop:

```bash
lv24-seo auth login --provider yandex
```

The command opens the authorization URL and prompts for the verification code shown by
Yandex. On a headless machine, print the URL and pass the code back explicitly:

```bash
lv24-seo auth login --provider yandex --no-browser
# open the printed URL, approve access, copy the code from
# https://oauth.yandex.ru/verification_code
lv24-seo auth login --provider yandex --code <code>
```

`--no-browser` prints the authorization URL to stderr so `--json` on stdout stays
parseable. The JSON payload carries the provider, flow, scopes, expiry and target only —
never an access or refresh token.

Metrika uses the same app but a separate token slot:

```bash
lv24-seo auth login --provider metrika
```

Stored secret names:

| Provider | Access token | Refresh token | Expiry | Scopes |
|---|---|---|---|---|
| `yandex` | `yandex_webmaster_token` | `yandex_refresh_token` | `yandex_token_expires_at` | `yandex_token_scopes` |
| `metrika` | `yandex_metrika_token` | `yandex_metrika_refresh_token` | `yandex_metrika_token_expires_at` | `yandex_metrika_token_scopes` |

The app secret (`yandex_oauth_client_secret`) is shared and survives `auth logout`. Scopes
are stored as one space-separated string and are deleted with the rest of the grant.

## 5. Automatic refresh

When an access token is expired (or expires within 60 seconds), the next Webmaster or
Metrika command refreshes it from the stored refresh token, saves the new access token and
expiry, and reuses the existing refresh token if Yandex does not return a new one. This
requires the client id (`[auth.yandex] client_id` or `LV24_SEO_YANDEX_CLIENT_ID`) and
`yandex_oauth_client_secret`.

If the refresh fails, the command returns `AUTH_INVALID` with the provider-specific login
command instead of sending a token that is known to be expired:

```bash
lv24-seo auth login --provider yandex
```

A token without an expiry and without a refresh token is treated as a long-lived static
token and keeps working. A token whose expiry is stored but has no refresh token is
reported as expired, not silently used.

## 6. Check and remove

```bash
lv24-seo auth status
lv24-seo auth status --json
```

`auth status` reports presence and source only; it never reads a value back into the
output. `logout` removes the access token, refresh token, recorded scopes and expiry for one
provider and keeps the app secret:

```bash
lv24-seo auth logout --provider yandex
lv24-seo auth logout --provider metrika
```

Both `auth status --json` and `doctor --json` classify each provider as `oauth_ready`,
`oauth_client_missing`, `oauth_client_secret_missing`, `oauth_token_missing`,
`oauth_token_expired`, `oauth_scope_missing`, `oauth_scope_unknown` or
`oauth_redirect_invalid`; see [OAuth connectors](auth.md#readiness-diagnostics) for the fix
behind each code. A configured `[auth.yandex].redirect_uri` must be exactly
`https://oauth.yandex.ru/verification_code`; an empty value is the default and is valid.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `AUTH_REQUIRED: Yandex OAuth client_id is not configured` | Set `[auth.yandex] client_id` or `LV24_SEO_YANDEX_CLIENT_ID`. |
| `AUTH_REQUIRED: Yandex OAuth client secret is not configured` | Run `lv24-seo auth set --provider yandex_oauth_secret --stdin`. |
| `INVALID_INPUT: no Yandex authorization code was provided` | Re-run with `--code <code>`; the verification code is not read from a pipe. |
| `AUTH_INVALID` with `invalid_grant` | The code is single-use or expired, or the refresh token was revoked. Run `auth login` again. |
| `AUTH_INVALID` with `invalid_client` | The client id/secret pair is wrong or the app was deleted. Re-check the app and store the secret again. |
| Webmaster or Metrika returns a scope error | Add the missing scope to the Yandex app, then authorize again so the new token carries it. |
| `WRITE_DISABLED` when storing the secret | The matching `LV24_SEO_*` environment variable is set and is read-only. Unset it, then store the value. |
