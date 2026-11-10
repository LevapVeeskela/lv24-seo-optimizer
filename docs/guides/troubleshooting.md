# Troubleshooting

Start with `error.code`, not the exit code: several different failures share an exit status, and
`PROPERTY_NOT_FOUND` has no dedicated one. Every CLI command accepts `--json` and then prints one
envelope:

```json
{"ok": false, "error": {"code": "AUTH_REQUIRED", "message": "Google Search Console token is missing", "retryable": false, "next_action": "lv24-seo auth login --provider gsc"}, "meta": {"version": "0.0.1"}, "warnings": ["gsc: token missing"]}
```

MCP tools return the same envelope (as `structuredContent` when the SDK supports it) and set
`isError: true` on failure.

## Error and exit code reference

| Code | CLI exit | Retryable | Meaning | First move |
|---|---|---|---|---|
| `AUTH_REQUIRED` | 3 | no | No token for the requested engine. Nothing was sent to the provider. | `lv24-seo auth status`; store the token |
| `AUTH_INVALID` | 3 | no | Provider rejected the credential: HTTP 401, or HTTP 403 (permission/host verification) | 401: replace the token. 403: check property/host access |
| `PROPERTY_NOT_FOUND` | 1 | no | HTTP 404: the property id, host id or site URL does not exist for this account | Use the exact id from the provider UI |
| `QUOTA_EXCEEDED` | 4 | usually | HTTP 429, or the local rate limiter cannot fit the wait in its budget | Wait for the reset; lower `--limit`; split the window |
| `UPSTREAM_UNAVAILABLE` | 4 | often | Network/timeout, HTTP 5xx, invalid JSON, or a capability the client does not implement | Retry later; check egress and provider status |
| `WRITE_DISABLED` | 2 | no | A write was attempted without `--allow-write`/`allow_write`, the write secret is missing, the adapter is not wired, or a secret could not be stored | See [Write tools](#write_disabled--confirm_required) |
| `CONFIRM_REQUIRED` | 1 | no | A gated CLI write received a missing, expired or plan-mismatched `--confirm` token | Rerun the dry run and pass the fresh token |
| `INVALID_INPUT` | 2 | no | Bad flag, date, limit, URL, provider name or client name | Fix the argument; read `next_action` |
| `CONFIG_INVALID` | 2 | no | Bad `seo.toml`, bad host JSON, BOM, or a missing explicit config path | Fix the file named in the message |
| `TRUNCATED` | 5 | n/a | Reserved as an error code. **Not emitted:** truncation is `meta.truncated` / `limitations` and exits `0` | Raise the limit within provider caps |
| `INTERNAL` | 1 | no | Unexpected failure; the message deliberately omits the traceback | Run `doctor --json`; report with the envelope |

Exit `5` is only produced for a successful payload with `meta.truncated` **and** `meta.strict`;
no current command sets `strict`. Treat `meta.truncated` as the signal, not the exit code.

## AUTH_REQUIRED and AUTH_INVALID

### No token

`AUTH_REQUIRED` is local: `SecretStore` found no value for that engine. Single-engine commands fail;
aggregated surfaces continue and add a warning such as `gsc: token missing`.

```bash
lv24-seo auth status
lv24-seo auth set --provider gsc            # hidden prompt
printf '%s' "$TOKEN" | lv24-seo auth set --provider gsc --stdin
lv24-seo properties
```

Providers accepted by `auth set` are `gsc`, `yandex`, `bing`, `crux`, `psi`, `smtp`, `telegram`.
Yandex Webmaster and Metrika use different secrets:

| Engine | Secret | How to store it |
|---|---|---|
| Yandex Webmaster | `yandex_webmaster_token` | `auth set --provider yandex` |
| Yandex Metrika | `yandex_metrika_token` | `LV24_SEO_YANDEX_METRIKA_TOKEN` only (no `auth set --provider metrika`) |

`auth login --provider gsc|yandex` does **not** open a browser and does not write anything; it
prints the provider-specific manual steps. Follow them, then use `auth set`.

### 401 versus 403

The shared HTTP layer maps both to `AUTH_INVALID`, but the message tells them apart:

| Status | Message | Meaning | Action |
|---|---|---|---|
| 401 | `authentication failed (HTTP 401) for <host><path>` with `next_action: lv24-seo auth login` | Token expired, revoked, malformed, or from the wrong account | Replace the token with `auth set` |
| 403 | `access denied (HTTP 403) for <host><path>` with no `next_action` | Credential is valid but lacks permission | **Do not re-authenticate.** For Yandex, verify the host in Webmaster under the same account; for GSC, add the account to the property as Full/Owner |

A Yandex 403 on a host-scoped call usually means the host is not verified for the token's account
(or the app lacks the `webmaster:hostinfo` scope). Check the host list in Yandex Webmaster, then
compare `providers.yandex_webmaster.host_id` with the exact id (`https:example.com:443`, not a URL).
Re-authenticating a valid token cannot fix a 403.

### Token lifetimes

Provider-side lifetimes:

| Provider | Lifetime | Note |
|---|---|---|
| Yandex Webmaster | ~6 months | OAuth token |
| Yandex Metrika | ~1 year | OAuth token |
| Google OAuth refresh token | until revoked | The OAuth app must be in production (published). In testing mode, refresh tokens expire after 7 days |
| Google service account | no expiry | Must be added to the property as Full/Owner |

**Current build caveat:** there is no OAuth refresh flow and no service-account auth. `auth set
--provider gsc` stores a single string and sends it as `Authorization: Bearer`. That is a Google
**access token**, which normally lives about an hour. Store a fresh one per run; the long
refresh-token lifetimes above apply once the refresh flow lands. Yandex tokens are static and work
for their full lifetime.

`auth status` reports `present` and the source (`env`, `keyring`, `file`) but never a value. An
environment token is read-only: `auth set` and `auth logout` return `WRITE_DISABLED` while it is
set.

## PROPERTY_NOT_FOUND

A 404 from the provider. The id must match the provider's own string exactly.

### Google Search Console

| Property type | Id form | Example |
|---|---|---|
| Domain property | `sc-domain:` + domain | `sc-domain:example.com` |
| URL-prefix property | exact URL, including scheme and trailing slash | `https://example.com/` |
| URL-prefix with `www` | separate property | `https://www.example.com/` |
| `http` prefix | separate property | `http://example.com/` |

`lv24-seo init --property https://example.com` writes `sc-domain:example.com` to both
`project.default_property` and `providers.gsc.property`. If the account only has a URL-prefix
property, edit `seo.toml`:

```toml
[providers.gsc]
property = "https://example.com/"
```

`properties` is a **local readiness check**: it shows the configured target and whether a token is
present. It does not ask Google whether the property exists, so a wrong id still looks configured.

### Yandex Webmaster

The host id is `scheme:host:port`, never a URL:

```text
https:example.com:443
http:example.com:80
```

`init` derives it from `--property`. A 404 on a host-scoped Yandex read is deliberately treated as
a **degraded host**, not `PROPERTY_NOT_FOUND`: the result has empty rows, warning
`host_not_loaded`, and limitation `degraded_host`. A typo in `host_id` therefore looks like “no
data” or maintenance. Compare the value with the host id in Yandex Webmaster.

Do not run `lv24-seo yandex hosts`: that command does not exist in this build (`lv24-seo yandex
--help` lists only `queries` and `indexing`), even though one internal error hint mentions it.

### Bing and CrUX

`providers.bing.site` and `providers.crux.origin` are full origins such as
`https://example.com`, not `sc-domain:` ids. A trailing path is not part of either.

## QUOTA_EXCEEDED

| Provider | Limit | Applies to |
|---|---|---|
| GSC search analytics | 50,000 rows/day/site/type | `gsc query`; pagination stops at 50,000 and sets `truncated: true` |
| GSC URL Inspection | 2,000/day/site, 600/min | One URL per call, no batch |
| GSC project | 40,000 QPM / 30M QPD | All projects on one Cloud project |
| Yandex Webmaster | no published global RPD/RPS | Query analytics (popular queries) is fragile above ~10k req/hour (429) |
| Yandex recrawl | hard daily quota | The API exposes `GET /recrawl/quota`; the adapter is not wired, so the CLI only plans the write and consumes no quota |
| Metrika | `/stat/v1/data` 200 requests / 5 min; 5,000/day/user; 30 rps/IP; 3 parallel | Report and stat calls |
| CrUX | 150 queries/min/project | Origin-level field data |
| PSI | ~25,000/day | Lab audits |

What the tool does instead of retrying blindly:

* A token bucket per provider smooths request rate. Defaults: GSC 20 rps / 1,200 burst, URL
  Inspection 10 rps / 600, Yandex Webmaster 5 rps / 100, Metrika 0.7 rps / 200, Bing 5 rps / 100,
  CrUX 2.5 rps / 150, PSI 25 rps / 1,000.
* HTTP 429 and 5xx are retried **only for GET/HEAD**, at most three attempts total, honouring
  `Retry-After` up to 30 seconds, with jittered exponential backoff.
* POST is never retried. GSC search analytics, URL Inspection, CrUX, Metrika `/stat/v1/data` and
  provider writes fail fast with the mapped error.
* Exhausted quota becomes `QUOTA_EXCEEDED` with `retryable: true` and exit 4. Nothing is silently
  rescheduled or looped.
* Row caps are surfaced, not hidden: GSC `rows_cap_50000` / `truncated`; Yandex top 3,000 queries
  and 500 rows per page; weekly report uses 5,000 GSC rows.

What to do: wait for the provider's reset, lower `--limit`, split the date range, or run the report
with `--lag-days 3`. Do not shorten the schedule or add a retry loop around exit 4. For recrawl and
sitemap actions, use the provider UI until those adapters are wired. Recrawl quota is not consumed
today because the CLI never reaches the queue call.

## UPSTREAM_UNAVAILABLE

| Cause | Typical message | Retryable | Handling |
|---|---|---|---|
| No route / DNS / TCP failure | `network error (ConnectError) for <host><path>` | yes | Check egress, proxy and DNS; retry later |
| Timeout | `request timed out after 30s for <host><path>` | yes | Retry; narrow the request |
| HTTP 5xx | `upstream unavailable (HTTP 503) for <host><path>` | yes | GET/HEAD were already retried; wait and retry |
| Maintenance HTML instead of JSON | `invalid JSON response from <host><path>` | no | Retry manually after the provider recovers |
| Missing client capability | `the service does not implement <method>` | no | Upgrade; use the CLI |

### IPv4 pinning

The HTTP client binds to the IPv4 any-address (`httpx.HTTPTransport(local_address="0.0.0.0")`).
This prevents an unreachable `AAAA` record from winning on hosts that have no working IPv6 route.
The trade-off: a provider endpoint reachable **only** over IPv6 cannot be used. Test the route
outside the tool:

```bash
curl -4 -I https://api.webmaster.yandex.net/v4/
curl -6 -I https://api.webmaster.yandex.net/v4/
```

If only `curl -6` succeeds, move the run to a host with IPv4 egress.

### Yandex `HOST_NOT_LOADED`

A new host, a host in maintenance, or a host-scoped 404 produces a **degraded result**, not an
error:

* empty `rows`;
* `warnings: ["host_not_loaded"]`;
* `limitations: ["degraded_host"]`;
* `data_age_days: 0`.

The command still exits 0. Retry later; Yandex query reports also lag roughly four days. Check the
host in Webmaster before assuming maintenance — a wrong `host_id` looks identical.

GET-based Yandex reads are retried on 429/5xx up to three attempts; the degraded result above is
what remains after a host-scoped 404. POST-based reads (GSC search analytics, URL Inspection, CrUX,
Metrika `/stat/v1/data`) are not retried at all.

## WRITE_DISABLED / CONFIRM_REQUIRED

CLI writes use the ADR-0003 flow: a dry run returns a plan plus a short-lived confirm token, and
applying the plan requires the same plan plus that token.

| Command | Dry run | Apply |
|---|---|---|
| `indexnow submit` | `--allow-write` + `LV24_SEO_WRITE_SECRET` → plan + token | Sends the URLs to IndexNow |
| `sitemap submit` | same → plan + token | `WRITE_DISABLED`: `gsc sitemap submit is not wired yet` (no provider call) |
| `yandex recrawl` | same → plan + token | `WRITE_DISABLED`: `yandex recrawl is not wired yet` (no provider call) |

`yandex recrawl` reads `providers.yandex_webmaster.host_id` from `seo.toml`; if it is blank, the
command returns `INVALID_INPUT` and exit 2 before the write gate.

```bash
# Long random value; environment only, never on the command line or in an agent config.
export LV24_SEO_WRITE_SECRET="REPLACE_WITH_A_LONG_RANDOM_VALUE"

lv24-seo indexnow submit --host example.com --key YOUR_INDEXNOW_KEY \
  --url https://example.com/a --url https://example.com/b \
  --allow-write --json
# -> data.status=dry_run, data.plan, data.confirm_token (valid 300 seconds)

lv24-seo indexnow submit --host example.com --key YOUR_INDEXNOW_KEY \
  --url https://example.com/a --url https://example.com/b \
  --allow-write --confirm YOUR_CONFIRM_TOKEN --json
# -> data.status=applied
```

IndexNow also requires the key file to be live at `https://<host>/<key>.txt` before the apply step.
The dry run never contacts IndexNow; only the apply with a valid token does.

The token is an HMAC over the action, target, full plan hash and expiry. Changing the URLs, host,
key, property or even the summary invalidates it:

```json
{"ok": false, "error": {"code": "CONFIRM_REQUIRED", "message": "confirm token is missing, expired or does not match this plan", "retryable": false, "next_action": "rerun the dry run, review the plan and pass the fresh confirm_token within 5 minutes"}, "meta": {"version": "0.0.1"}, "warnings": []}
```

Tokens live 300 seconds. Treat one as single-use: the CLI does not attach the idempotency ledger, so
a replay inside the window can apply again.

`WRITE_DISABLED` has several distinct causes; the message identifies the one you hit:

| Message | Cause | Fix |
|---|---|---|
| `write operations are disabled by default` | `--allow-write` was not passed | Review the dry run, then opt in per call |
| `write secret is empty: set LV24_SEO_WRITE_SECRET` | `--allow-write` without the environment secret | Set a long random value in the same shell or service |
| `gsc sitemap submit is not wired yet` | GSC sitemap adapter absent | Submit in Search Console until it lands |
| `yandex recrawl is not wired yet` | Recrawl adapter absent | Queue in Yandex Webmaster until it lands |
| `<tool> is a write tool and is disabled by default` | MCP write tool called without `allow_write=true` | Pass `allow_write=true` only after reviewing the plan |
| `recrawl writes are not available in this build` / `sitemap writes are not available in this build` | MCP call with `allow_write=true`, but the MCP write layer is not wired | Use the CLI write surface; MCP writes remain disabled |

`WRITE_DISABLED` also comes from the secret store, with a different message:

| Message | Cause | Fix |
|---|---|---|
| `LV24_SEO_<NAME> is set in the environment, which is read-only and always wins` | A token env var shadows the keychain | Unset the variable, then `auth set` |
| `the OS keyring rejected the write and file storage is disabled` | Locked/unavailable keyring | Unlock it, or set `LV24_SEO_ALLOW_FILE_SECRETS=1` for the 0600 file fallback |
| `no writable secret backend` | No keyring and no file fallback | Install the `secrets` extra or enable the file fallback |

MCP has no confirm-token path yet: both write tools return `WRITE_DISABLED` even with
`allow_write=true`. Only the CLI can perform a write in this build, and only IndexNow reaches a
provider. Keep `--allow-write` per call; never enable writes globally in an agent config, and never
log or commit `LV24_SEO_WRITE_SECRET`.

## Empty or partial reports

Check these fields before concluding that SEO collapsed:

| Field | What it tells you |
|---|---|
| `meta.sources` | Which engines actually returned data (`[]` means none) |
| `meta.data_age_days` | Maximum provider lag, **0 when no source data exists** — 0 is not proof of freshness |
| `meta.truncated` | A row/URL cap was reached |
| `warnings` | Missing tokens, missing property/host/counter, degraded hosts |
| `limitations` | Provider capabilities that cannot be fixed by retrying |

Expected lag:

| Source | Lag |
|---|---|
| GSC search analytics | 2–3 days (`--lag-days 2` is the report default) |
| Yandex Webmaster queries | ~4 days |
| Yandex Metrika | Near real time for the requested date window |
| CrUX | 28-day rolling p75 |
| PSI | Lab run, current request |

Common warning strings and their fixes:

| Warning | Fix |
|---|---|
| `gsc: token missing`, `yandex_webmaster: token missing`, `yandex_metrika: token missing` | `auth set` / `LV24_SEO_YANDEX_METRIKA_TOKEN` |
| `gsc: property missing` | Set `providers.gsc.property` or `LV24_SEO_GSC_PROPERTY` |
| `yandex_webmaster: host_id missing` | Set `providers.yandex_webmaster.host_id` |
| `yandex_metrika: counter missing` | Set `providers.yandex_metrika.counter` |
| `host_not_loaded` + `degraded_host` | Wait, or fix the host id |
| `no provider rows; traffic and opportunities are empty` | Configure at least one engine and property |
| `store: not configured; snapshot was not saved` | Run `init` so the data directory exists |

A weekly report with no usable source is still a successful command. Trimmed envelope:

```json
{"ok": true, "data": {"kind": "weekly", "sections": [{"key": "sources", "summary": "No engine returned rows"}]}, "meta": {"sources": [], "data_age_days": 0, "truncated": false}, "warnings": ["no provider rows; traffic and opportunities are empty", "gsc: token missing", "yandex_webmaster: token missing", "yandex_metrika: token missing"]}
```

That is a configuration result, not an outage. By contrast, a single-engine command with no token
fails with `AUTH_REQUIRED` and exit 3.

If rows are genuinely empty while the same dates show data in the provider UI:

1. Re-check the property/host id (`properties` only echoes config).
2. Re-run with an explicit window: `lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 --json`.
3. Check `meta.truncated` and `limitations`; a cap is not an absence of data.
4. Remember that GSC needs 2–3 days, and a new site may have no impressions yet.

## Diagnostics for an issue

Collect the envelope, not a screenshot:

```bash
lv24-seo version --json
lv24-seo doctor --json
lv24-seo auth status --json
lv24-seo config show --json
lv24-seo properties --json
# repeat the failing command with --json added
```

| Source | What it contains | What it never contains |
|---|---|---|
| `doctor --json` | Python version, platform, config path, cwd/data-dir writability, token presence | Tokens, network results |
| `auth status --json` | Provider, secret name, `present`, `source`, `expires_at` | Secret values |
| `config show --json` | Effective config with secret-looking keys masked as `***` | Tokens, passwords, API keys |
| Error envelope | `code`, `message`, `next_action`, `retryable`, optional `detail` | Request bodies, headers, token fields; input-validation details can echo the value you passed (including a URL query) |
| HTTP `detail.target` | Host and path only | Query string (may carry `?key=`), userinfo, port |

The logging layer (`core/logging.py`) masks `Authorization`, `token`, `secret`, `password`,
`cookie`, `api_key`, known token prefixes (`y0_`, `eyJ`, `ghp_`, `sk-`) and opaque 32+ character
runs. However, the current CLI does not install a log handler, and `LV24_SEO_LOG_LEVEL` is ignored
by the config layer. The stable diagnostic interface is the `--json` envelope; do not rely on log
format.

Before posting anywhere:

* never paste tokens, cookies, `Authorization` headers, `LV24_SEO_WRITE_SECRET`, `.env` contents,
  `data/secrets.json`, service-account JSON, or a populated client config;
* review `detail.value` and `detail.host` as well: CLI input validation can echo the value you
  passed, and a URL query may contain a key;
* rewrite URLs containing `key=`, `token=`, `signature=` or `code=`;
* keep `code`, `message`, `next_action`, `retryable`, `meta`, and the command plus its flags.

Issue report template:

```text
lv24-seo version:
OS / Python:
command:
expected:
actual (redacted --json envelope):
```

Report security problems through the private advisory described in
[SECURITY.md](../../SECURITY.md), not a public issue.

## MCP client or install failures

The first three rows describe the `0.0.1` alpha; re-check `lv24-seo --help` after an upgrade.

| Symptom | Cause | Fix |
|---|---|---|
| `install` says the file “is not valid JSON” but it looks valid | UTF-8 BOM at the start of the file (common with Windows PowerShell 5.1 `Set-Content -Encoding utf8`) | Rewrite as UTF-8 without BOM, or delete the file and rerun `install` |
| `No such command 'mcp'` | The `mcp serve` subcommand is not wired in this build | Use the checkout launcher in [MCP client setup](mcp-clients.md#launcher-status-in-the-current-alpha) |
| `uvx` says `lv24-seo was not found in the package registry` | The package is not published yet | Use the checkout launcher |
| Server fails to start from a GUI client | The app does not inherit the shell `PATH`, so `uvx` is not found | Put the absolute path (`where uvx` / `which uvx`) in `command` |
| VS Code lists nothing after `install --client vscode` | `install` writes `mcpServers`; VS Code's `.vscode/mcp.json` uses `servers` | Rename the key or use the VS Code block in [MCP client setup](mcp-clients.md#vs-code-copilot-chat) |
| OpenCode keeps the old tools after editing `opencode.json` | MCP servers start with the session | Run `opencode reload`, or restart the session |
| Tools are missing after an upgrade | Client-side tool cache | Restart the client; in VS Code run **MCP: Reset Cached Tools** |
