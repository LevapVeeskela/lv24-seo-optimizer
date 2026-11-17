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
| `CONFIRM_REQUIRED` | 2 | no | A gated write received a missing, expired or plan-mismatched confirm token | Rerun the dry run and pass the fresh token |
| `INVALID_INPUT` | 2 | no | Bad flag, date, limit, URL, path, task transition, provider name or client name | Fix the argument; read `next_action` |
| `CONFIG_INVALID` | 2 | no | Bad `seo.toml`, bad host JSON, BOM, a missing explicit config path, or `tasks` without an initialised workspace | Fix the file named in the message, or run `lv24-seo init` |
| `STORE_LOCKED` | 1 | yes | Another process holds the SQLite write lock past the busy timeout | Wait for the other run (crawl, report, MCP server), then retry; nothing was written |
| `STORE_CORRUPT` | 1 | no | The database file is not valid SQLite; a byte copy is kept as `<db>.corrupt-<utc>` | Inspect `detail.backup_path`, move the original aside, rerun |
| `STORE_UNAVAILABLE` | 1 | no | SQLite could not open or write the path (permissions, missing directory, disk) | Fix the path/permissions; run `doctor --json` |
| `TRUNCATED` | 5 | n/a | Reserved as an error code. **Not emitted:** truncation is `meta.truncated` / `limitations` and exits `0` unless `--strict` is set | Raise the limit within provider caps |
| `INTERNAL` | 1 | no | Unexpected failure; the message deliberately omits the traceback | Run `doctor --json`; report with the envelope |

Exit `5` is produced only for a successful payload with `meta.truncated` **and** `meta.strict`.
`--strict` exists on the read commands (`stats`, `gsc query`, `yandex queries`, `yandex indexing`,
`crawl`, `report weekly`, `report diff`) and always sets `meta.strict`; without it, truncation is
still visible in `meta.truncated` but the command exits `0`. `report diff --markdown --strict`
prints the Markdown and then exits `5` for truncated data.

`STORE_*` codes are typed storage failures, not program bugs. They currently map to exit `1`
(unexpected) because they have no dedicated exit status; use `error.code` and `retryable`, not the
exit number. `STORE_LOCKED` is the only one safe to retry as-is.

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

Providers accepted by `auth set` are `gsc`, `yandex`, `metrika`, `bing`, `crux`, `psi`, `smtp`,
`telegram`. Yandex Webmaster and Metrika use different secrets:

| Engine | Secret | How to store it |
|---|---|---|
| Yandex Webmaster | `yandex_webmaster_token` | `auth set --provider yandex` |
| Yandex Metrika | `yandex_metrika_token` | `auth set --provider metrika` or `LV24_SEO_YANDEX_METRIKA_TOKEN` |

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

**GSC service accounts:** set `providers.gsc.service_account_file` or
`LV24_SEO_GSC_SERVICE_ACCOUNT_FILE` to an **absolute** path to the JSON key. The engine reads the
key once per process, signs an RS256 JWT, exchanges it at the Google token endpoint, caches the
access token and refreshes it 60 seconds before expiry. A relative, missing or unreadable file is
`CONFIG_INVALID`; an invalid JSON/key or a rejected exchange is `CONFIG_INVALID`/`AUTH_INVALID` with
a next action. `auth set --provider gsc` still stores a plain OAuth access token, which normally
lives about an hour and is not refreshed. `auth status` reports only stored secrets; a configured
service-account file is reported by `properties` and by the `gsc_auth` check in `doctor`. Yandex
tokens are static and work for their full lifetime.

### Service-account file errors

| Message | Cause | Fix |
|---|---|---|
| `Google service account file path must be absolute` | A relative `service_account_file` | Use an absolute path (or `~`-expanded) in `seo.toml`/the environment |
| `Google service account file not found` / `is not a file` | Wrong path or a directory | Point at the downloaded JSON key; keep it outside the repository |
| `cannot read the Google service account file` | Permissions or a locked volume | Fix permissions or move the key to a readable location |
| `... is not valid JSON` / `is not a service-account key` | Truncated or wrong JSON | Download a new key from Google Cloud → IAM → Service accounts → Keys |
| `Google rejected the service-account token request` | Key revoked, clock skew, or API disabled | Create a new key, check the system clock, enable the Search Console API |

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
| Yandex recrawl | hard daily quota | `yandex recrawl` reads `GET /recrawl/quota` first and caps the list to the remainder; one POST per URL, `429` stops the loop with `not_attempted` |
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

An **empty** history is not a degraded host. When the host answered but has no indexing buckets or
search-URL events for the window, the result is `rows: []`, `total: 0`,
`limitations: ["empty_history"]`, no `degraded_host` warning, and exit 0. `HOST_NOT_LOADED` /
`degraded_host` is reserved for a host-scoped 404 or an explicit not-loaded payload.

GET-based Yandex reads are retried on 429/5xx up to three attempts; the degraded result above is
what remains after a host-scoped 404. POST-based reads (GSC search analytics, URL Inspection, CrUX,
Metrika `/stat/v1/data`) are not retried at all.

## WRITE_DISABLED / CONFIRM_REQUIRED

CLI writes use the ADR-0003 flow: a plan (`--allow-write` plus `LV24_SEO_WRITE_SECRET`) returns the
payload and a short-lived confirm token, and applying the plan requires the same plan plus that
token.

| Command | Plan | Apply |
|---|---|---|
| `indexnow submit` | `--allow-write` + `LV24_SEO_WRITE_SECRET` → plan + token | Sends the URLs to IndexNow |
| `sitemap submit` | same → plan + token | `--provider bing`: JSON `SubmitFeed`; GSC: `WRITE_DISABLED` (`gsc sitemap submit is not wired yet`, no provider call) |
| `yandex recrawl` | same → plan + token | One `POST /recrawl/queue` per URL, capped to the remaining daily quota; `202` rows carry `task_id`/`quota_remainder`, `409` is `already_queued`, `429` stops with `not_attempted` |

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
The plan never contacts IndexNow; only the apply with a valid token does.

The token is an HMAC over the action, target, full plan hash and expiry. Changing the URLs, host,
key, property or even the summary invalidates it:

```json
{"ok": false, "error": {"code": "CONFIRM_REQUIRED", "message": "confirm token is missing, expired or does not match this plan", "retryable": false, "next_action": "rerun the dry run, review the plan and pass the fresh confirm_token within 5 minutes"}, "meta": {"version": "0.0.1"}, "warnings": []}
```

Tokens live 300 seconds. With an initialised workspace the CLI attaches the SQLite idempotency
ledger, so replaying the same token returns `status: skipped` / `reason: duplicate` and the provider
is called once. Without a data directory the apply still runs, but the envelope warns that replay
protection is off for the token's five-minute lifetime — run `lv24-seo init` to enable it. The plan
opens no ledger and creates no database.

An apply claims the slot as `pending`, promotes it to `sent` only after the provider confirms, and
releases it when the provider raises. A retry after a provider failure is therefore allowed; a
completed or still in-flight claim is `skipped`. A crash between the provider success and the
promotion leaves `pending`, which is treated as in-flight until an operator releases it.

`WRITE_DISABLED` has several distinct causes; the message identifies the one you hit:

| Message | Cause | Fix |
|---|---|---|
| `write operations are disabled by default` | `--allow-write` was not passed | Pass `--allow-write` plus `LV24_SEO_WRITE_SECRET` to plan; apply with `--allow-write --confirm TOKEN` |
| `write secret is empty: set LV24_SEO_WRITE_SECRET` | `--allow-write` without the environment secret | Set a long random value in the same shell or service |
| `gsc sitemap submit is not wired yet` | GSC sitemap adapter absent | Submit in Search Console until it lands |
| `Yandex recrawl quota exceeded for <url>` | Daily recrawl quota exhausted (429) | Wait for the quota to reset; earlier `task_id`s stay reported and the rest is in `not_attempted` |
| `<tool> is a write tool and is disabled by default` | MCP write tool called without `allow_write=true` | Pass `allow_write=true` only after reviewing the plan |
| `recrawl writes are not available in this build` / `sitemap writes are not available in this build` | MCP call with `allow_write=true`, but the engine service exposes no write handler | Use the CLI write surface; these MCP writes remain disabled |

`WRITE_DISABLED` also comes from the secret store, with a different message:

| Message | Cause | Fix |
|---|---|---|
| `LV24_SEO_<NAME> is set in the environment, which is read-only and always wins` | A token env var shadows the keychain | Unset the variable, then `auth set` |
| `the OS keyring rejected the write and file storage is disabled` | Locked/unavailable keyring | Unlock it, or set `LV24_SEO_ALLOW_FILE_SECRETS=1` for the 0600 file fallback |
| `no writable secret backend` | No keyring and no file fallback | Install the `secrets` extra or enable the file fallback |

MCP write tools implement the same two-phase flow: `allow_write=true` plus `plan_hash` and
`confirm_token` from the dry run, with `LV24_SEO_WRITE_SECRET` in the server environment. Bing
sitemap submit and Yandex recrawl are wired; GSC submit and Bing delete return `WRITE_DISABLED`
and nothing is sent. IndexNow stays CLI/desktop-only. Keep
`allow_write` per call; never enable writes globally in an agent config, and never log or commit
`LV24_SEO_WRITE_SECRET`.

## STORE_LOCKED / STORE_CORRUPT / STORE_UNAVAILABLE

The store translates raw `sqlite3` failures into typed errors instead of a generic `INTERNAL`
"report the bug" envelope:

| Code | Meaning | What to do |
|---|---|---|
| `STORE_LOCKED` | Another connection held the write lock past the busy timeout. `retryable: true`; nothing was written. | Let the other lv24-seo process (crawl, report, MCP server) finish, then rerun. |
| `STORE_CORRUPT` | The file is not a usable SQLite database. A byte copy is made first and named `<db>.corrupt-<utc>` (for example `seo.sqlite3.corrupt-20261115T124500Z`); `detail.backup_path` names it. The original is never moved or deleted. | Inspect or archive the backup, move the original aside, rerun to create a new database. |
| `STORE_UNAVAILABLE` | SQLite could not open or write the path: permissions, missing parent directory, read-only file, disk full or I/O error. | Check the directory and free space; `lv24-seo doctor --json` now fails when a declared data directory is unusable. |

The message and `detail` never contain row data, tokens or SQL parameters.

`backup create` is the one `STORE_CORRUPT` path that does **not** write `<db>.corrupt-<utc>`: it
promises to leave the source workspace untouched, so it reports the corrupt file and writes no
archive. Inspect or restore that file from another copy.

## logs, tasks and backup

These commands add no new error codes; they reuse `INVALID_INPUT` (exit `2`), `CONFIG_INVALID`
(exit `2`) and `STORE_CORRUPT` (exit `1`).

| Symptom | Cause | Fix |
|---|---|---|
| `logs analyze` says the path “must be absolute”, “must name exactly one file, not a glob”, or “UNC, network and device paths are not accepted” | The log path contract is deliberately narrow | Pass one absolute local regular file: no `*`, `?`, `[`, no `\\server\share`, no `NUL`/`COM1` |
| `logs analyze` says the path “is a directory” or “is not a regular file” | A directory or special file was passed | Pass the access log itself |
| `logs analyze` says the file “is larger than the … byte limit” | The log exceeds **100 MiB** | Rotate or split the log and analyse one part at a time |
| `warnings: skipped N of M lines: blank or not nginx JSON` | Blank, truncated or non-JSON lines | Expected for a rotated or partially written log; the parsed share is reported |
| Bot counts look wrong, or a fake Googlebot appears | Bot classes are **user-agent only** | Treat the rollup as triage; there is no reverse-DNS or published-IP verification |
| `tasks …` returns `CONFIG_INVALID`: “tasks need the local history store” | No initialised data directory | Run `lv24-seo init`, or set `storage.data_dir` and retry |
| `tasks update` returns `INVALID_INPUT`: “invalid task transition” | The requested move is not in the lifecycle | Allowed: `open → in_progress\|verified\|dismissed`, `in_progress → open\|verified\|dismissed`, `verified → open\|dismissed`, `dismissed → open\|in_progress` |
| `tasks add --payload` rejects a field as a credential | A key ends with `token`, `secret`, `password`, `api_key` or `authorization` | Store evidence only; remove the field |
| `backup create` says the output directory must be absolute, does not exist or is not a directory | `--out` must be an existing absolute directory | Create it first, then pass the absolute path |
| `backup create` says “the archive already exists” | The timestamped name already exists (a rerun in the same second) | Pass `--force` to replace it, or wait a second |
| `backup create` returns `STORE_CORRUPT` | The source database is not valid SQLite | The source is left untouched and no archive is written; inspect or restore it from another copy |
| `backup create` warns “the database was not found” | A config-only workspace | The archive contains `seo.toml` only; run `init` if you expected history |
| `backup restore --to` says “the restore directory already exists” | `--to` never merges or overwrites, including an empty directory | Choose a path that does not exist yet (the parent must exist) |
| `backup restore --to` says “the archive contains no database” | A config-only archive | No database is invented; the restored directory holds only `seo.toml` |
| `backup restore` says “unsafe member path” | The archive was edited or made by another tool | Use an archive from `backup create`; absolute paths, `..`, backslashes and symlink members are rejected |
| `backup restore --config` returns `CONFIRM_REQUIRED` | The token expired, the archive changed, or the workspace changed after the plan | Re-run without `--confirm`, review the fresh plan, then pass the new token within 5 minutes |

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
| GSC search analytics | 2–3 days (`--lag-days 2` is the report default); `data_age_days` is floored at 3 |
| Yandex Webmaster queries | ~4 days |
| Yandex Metrika | Near real time for the requested date window |
| Bing Webmaster | Age of the newest dated row in the feed |
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
| `empty_history` (limitation) | The host answered but has no buckets/events yet; not an outage |
| `off-origin sitemap loc excluded` | A sitemap listed a URL outside the site; it was dropped, not fetched |
| `no provider rows; traffic and opportunities are empty` | Configure at least one engine and property |
| `store: not configured; snapshot was not saved` | Run `init` so the data directory exists |
| `write ledger: no data directory` … | Run `init`; a store-less apply has no replay protection |
| `data is relative, so it follows the working directory` … | Set an absolute `[storage].data_dir` to pin the database to the config file |

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
| `doctor --json` | Python version, platform, config path, effective data directory and its scope, `meta.data_dir_declared`, cwd/data-dir writability, token presence, an ambiguity warning for a relative data directory. A **declared** data directory that is missing or unwritable is fatal (`ok: false`, `meta.healthy: false`) | Tokens, network results |
| `auth status --json` | Provider, secret name, `present`, `source`, `expires_at` | Secret values |
| `config show --json` | Effective config with secret-looking keys masked as `***` | Tokens, passwords, API keys |
| Error envelope | `code`, `message`, `next_action`, `retryable`, optional `detail` | Request bodies, headers, token fields; input-validation details can echo the value you passed (including a URL query) |
| HTTP `detail.target` | Host and path only | Query string (may carry `?key=`), userinfo, port |

The logging layer (`core/logging.py`) masks `Authorization`, `token`, `secret`, `password`,
`cookie`, `api_key`, known token prefixes (`y0_`, `eyJ`, `ghp_`, `sk-`) and opaque 32+ character
runs. Logging is **opt-in**: `LV24_SEO_LOG_LEVEL=INFO` (or `DEBUG`, `WARNING`, …) makes the CLI and
`lv24-seo mcp serve` install the redacting handler. An unset variable leaves the caller's logging
configuration untouched, and importing the library or calling `create_server()` never replaces root
handlers. Output is one JSON line per record when stderr is not a terminal. The stable diagnostic
interface is still the `--json` envelope; do not rely on log format.

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
| `No such command 'mcp'` | The `mcp serve` subcommand is not wired in this build | Use the checkout launcher in [MCP client setup](mcp-clients.md#launcher-status) |
| `uvx` says `lv24-seo was not found in the package registry` | The package is not published yet | Use the checkout launcher |
| Server fails to start from a GUI client | The app does not inherit the shell `PATH`, so `uvx` is not found | Put the absolute path (`where uvx` / `which uvx`) in `command` |
| VS Code lists nothing after `install --client vscode` | `install` writes `mcpServers`; VS Code's `.vscode/mcp.json` uses `servers` | Rename the key or use the VS Code block in [MCP client setup](mcp-clients.md#vs-code-copilot-chat) |
| OpenCode keeps the old tools after editing `opencode.json` | MCP servers start with the session | Run `opencode reload`, or restart the session |
| Tools are missing after an upgrade | Client-side tool cache | Restart the client; in VS Code run **MCP: Reset Cached Tools** |
