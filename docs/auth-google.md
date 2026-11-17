# Google Search Console OAuth

`lv24-seo auth login --provider gsc` authorizes Search Console with a Google **Desktop**
OAuth client and stores a refreshable grant in the secret store. It replaces the manual
"paste an access token" step for interactive use; the service-account path keeps working for
unattended runs.

The connector is read-only: the default scope is
`https://www.googleapis.com/auth/webmasters.readonly`. It never submits sitemaps or requests
recrawls.

## 1. Create the Google Cloud client

1. Open <https://console.cloud.google.com/> and select (or create) a project.
2. **APIs & Services → Library → Google Search Console API → Enable.**
3. **APIs & Services → OAuth consent screen**:
   - User type **External** is enough for a single owner; **Internal** works in a Workspace.
   - Add your Google account as a **test user** while the app is in *Testing*.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - No redirect URI has to be registered: Google allows the loopback redirect for Desktop
     clients.
5. Copy the **client ID** and **client secret**.

The client ID is public configuration. The client secret is a secret: it goes to the OS
keychain (or the `0600` secrets file), never into `seo.toml`, a shell history entry or a log.

## 2. Configure the client id

`seo.toml`:

```toml
[auth.gsc]
client_id = "1234567890-abc.apps.googleusercontent.com"
# Optional; the default is the read-only Search Console scope.
# scopes = ["https://www.googleapis.com/auth/webmasters.readonly"]
# Optional; only needed for the manual --code exchange (see below).
# redirect_uri = "http://127.0.0.1:8123/callback"
```

Or through the environment:

```bash
export LV24_SEO_GSC_CLIENT_ID="1234567890-abc.apps.googleusercontent.com"
export LV24_SEO_GSC_REDIRECT_URI="http://127.0.0.1:8123/callback"   # optional
```

PowerShell:

```powershell
$env:LV24_SEO_GSC_CLIENT_ID = "1234567890-abc.apps.googleusercontent.com"
```

## 3. Store the client secret

```bash
printf '%s' "$GSC_CLIENT_SECRET" | lv24-seo auth set --provider gsc_oauth_secret --stdin
```

PowerShell:

```powershell
$env:GSC_CLIENT_SECRET | lv24-seo auth set --provider gsc_oauth_secret --stdin
```

The value is read from stdin, stored under `gsc_oauth_client_secret` and never echoed.
`lv24-seo auth status` reports presence and source only.

## 4. Log in

```bash
lv24-seo auth login --provider gsc
```

The command binds `127.0.0.1` on an ephemeral port, prints the consent URL and opens the
default browser. After you approve access, Google redirects to
`http://127.0.0.1:<port>/callback`, the page says *"You can close this window"*, and the
command exchanges the code with PKCE (S256).

Useful flags:

| Flag | Meaning |
|---|---|
| `--no-browser` | Print the URL and wait for the loopback callback; nothing is opened. |
| `--port 8123` | Bind a fixed port (useful when a firewall or a proxy requires it). |
| `--code <code>` | Skip the listener and exchange a code obtained elsewhere. |
| `--json` | Machine-readable envelope. |

`--json` output carries `provider`, `flow`, `scopes`, `expires_at`, `target` and `next`.
Tokens are never part of the envelope, the text output or a log line.

### Headless machines

The loopback callback only works on the machine that runs the command. On a headless host:

1. Run `lv24-seo auth login --provider gsc --no-browser --port 8123` and copy the printed
   URL.
2. Open it in a browser **on another machine** and approve access. The browser is redirected
   to `http://127.0.0.1:8123/callback?...&code=...` and fails to load; copy the `code` value
   from the address bar.
3. Run `lv24-seo auth login --provider gsc --code "<code>" --port 8123`.

The PKCE verifier from step 1 is kept in the secret store (`gsc_oauth_code_verifier`) and
used by step 3, then deleted. If the authorization URL was produced with a different
redirect URI, pin it in `[auth.gsc].redirect_uri` so the exchange can repeat it exactly.

## 5. What is stored

| Secret | Content |
|---|---|
| `gsc_access_token` | Bearer token, refreshed automatically. |
| `gsc_refresh_token` | Long-lived grant from the first consent. |
| `gsc_token_expires_at` | ISO-8601 UTC expiry of the access token. |
| `gsc_token_scopes` | Granted scopes, one space-separated string. |
| `gsc_oauth_client_secret` | Desktop client secret (set by `auth set`). |
| `gsc_oauth_code_verifier` | Short-lived PKCE verifier for `--code`; deleted after use. |

`lv24-seo auth logout --provider gsc` deletes the access token, refresh token, expiry,
recorded scopes and verifier and keeps the client secret, so a later login does not need the
secret again.

`lv24-seo auth status` and `lv24-seo doctor` classify the grant as `oauth_ready`,
`oauth_token_expired`, `oauth_scope_missing`, `oauth_scope_unknown` and so on; see
[OAuth connectors](auth.md#readiness-diagnostics) for every code and its fix. Google's
`[auth.gsc].redirect_uri`, when set, must be a loopback `http://127.0.0.1:<port>/...` URI;
an empty value is the default and is valid.

## 6. Credential precedence

For every Search Console call the service picks the first usable source:

1. **OAuth refresh token** — a complete grant (`gsc_refresh_token` +
   `gsc_oauth_client_secret` + `[auth.gsc].client_id`). The access token is refreshed when
   it is missing, unreadable or within 60 seconds of expiry, and the new grant is written
   back to the secret store.
2. **Service account** — `[providers.gsc].service_account_file` or
   `LV24_SEO_GSC_SERVICE_ACCOUNT_FILE`.
3. **Static token** — the legacy `gsc_token` secret (a plain bearer token or a
   service-account JSON document), set with `auth set --provider gsc --stdin`.

`lv24-seo properties` reports the source as `oauth_refresh_token`, `service_account_file`,
`secret` or `missing`. An incomplete OAuth grant (for example a refresh token without the
client secret) is ignored instead of failing the command.

## 7. Testing-mode caveat: 7-day refresh tokens

While the OAuth consent screen is in **Testing**, Google expires refresh tokens after
**7 days** for external apps. A daily cron job therefore stops working after a week with
`invalid_grant` even though the login succeeded.

Options:

- Publish the app (**OAuth consent screen → Publish app**). An unverified app still works
  for its test users, but the 7-day limit no longer applies; Google may show an
  "unverified app" warning until verification. No paid service is involved.
- Use a service account for unattended runs — it has no refresh-token expiry.
- Re-run `lv24-seo auth login --provider gsc` weekly.

## 8. Service-account fallback (unattended)

1. Create a service account and a JSON key in the same Cloud project.
2. Add the service-account email to the Search Console property as **Full** or **Owner**.
3. Point the config at the key's absolute path:

```toml
[providers.gsc]
property = "sc-domain:example.com"
service_account_file = "C:/secrets/gsc-service-account.json"
```

The file is read once per process and the signed JWT is exchanged for a short-lived access
token, which is refreshed before expiry. `lv24-seo doctor` reports a missing, relative or
unreadable path.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `AUTH_REQUIRED ... client secret is missing` | Run `lv24-seo auth set --provider gsc_oauth_secret --stdin`. |
| `AUTH_REQUIRED ... client_id is not configured` | Set `[auth.gsc].client_id` or `LV24_SEO_GSC_CLIENT_ID`. |
| `redirect_uri_mismatch` on the Google page | The client is not a **Desktop app**, or `[auth.gsc].redirect_uri` was changed between the authorization request and `--code`. |
| `invalid_grant` after a week | Testing-mode refresh-token expiry; publish the app, re-login, or switch to a service account. |
| `access_denied` | The Google account is not a test user, or the consent screen was cancelled. |
| `403` on a property | The account has no access to that Search Console property; add it as Full/Owner. |
| Callback times out | The browser ran on another machine, or a local firewall blocks `127.0.0.1`; use the `--code` flow. |
| `invalid_client` | The client secret was rotated in Google Cloud; store the new one with `auth set`. |

## 10. Security notes

- The callback server binds `127.0.0.1` only, accepts one request, verifies `state` and
  never logs the request path (it carries the code).
- PKCE (S256) is always used for the browser flow; the verifier is stored only until the
  exchange completes.
- The client secret and every token live in the secret store; `--json` output, envelopes,
  errors and `repr()` of the token provider never contain a token value.
- No Search Console write scope is requested by default.
