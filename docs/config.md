# Configuration

## Precedence

`CLI flags` → `LV24_SEO_*` environment → `./seo.toml` → user config
(`~/.config/lv24-seo/config.toml`, `%APPDATA%\lv24-seo\config.toml`) → defaults.

`.env` files are **not** auto-loaded. Use `--env-file .env` explicitly, or export variables.

## `seo.toml`

```toml
[project]
name = "example.com"
base_url = "https://example.com"
default_property = "sc-domain:example.com"

[providers.gsc]
property = "sc-domain:example.com"
# Optional: absolute path to a service-account JSON key added to the property as Full/Owner.
# The path is configuration, not a secret; the JSON itself never belongs in the repository.
# service_account_file = "/etc/lv24-seo/gsc-service-account.json"
# Reserved auth label; credentials resolve from env, optional keyring, then opt-in file fallback.
auth = "keychain"

[providers.yandex_webmaster]
host_id = "https:example.com:443"

[providers.yandex_metrika]
counter = "123456"
goals = [1, 2]

[providers.bing]
site = "https://example.com"
# Public IndexNow identifiers, safe in this file: the key is served openly at
# key_location (default https://<host>/<key>.txt). The Bing API key stays a secret.
indexnow_key = "abc12345"
indexnow_key_location = "https://example.com/abc12345.txt"

[providers.crux]
origin = "https://example.com"

[providers.psi]
strategy = "mobile"
urls = ["https://example.com/", "https://example.com/pricing"]

[report]
locale = "ru"           # ru | en | zh; rendered text only
timezone = "Europe/Minsk"
week_start = "monday"
thresholds = { clicks_pct = 10, impressions_pct = 15, position_abs = 1.5 }

[report.email]
enabled = true
to = ["owner@example.com"]
from = "seo@example.com"
only_on_change = true
# SMTP transport (password comes from the keychain, not from this file)
smtp_host = "smtp.example.com"
smtp_port = 587
smtp_user = "seo@example.com"

[report.telegram]
enabled = true
chat_id = "123456789"
severity = "critical"   # critical | all

[crawl]
max_urls = 500
concurrency = 4   # reserved for a future bounded worker pool; the crawler is serial today
respect_robots = true
user_agent = "lv24-seo/0.1 (+https://example.com/bot)"

[storage]
data_dir = "./data"      # relative to this file; absolute values and LV24_SEO_DATA_DIR are used as-is
raw_retention_days = 90   # reserved: no raw archives are written yet, pruning is a logged no-op

[indexing]
enabled = false          # explicit opt-in for provider submissions
providers = ["bing", "yandex", "gsc", "indexnow"]
max_submit_per_run = 500
max_inspect_per_run = 20
stale_days = 7
check_interval_days = 7
gsc_inspect_daily_limit = 2000
indexnow_daily_limit = 1000

[auth.gsc]
client_id = ""           # public OAuth client id; empty = manual token flow
scopes = ["https://www.googleapis.com/auth/webmasters.readonly"]
redirect_uri = ""        # default: http://127.0.0.1:<port>/callback

[auth.yandex]
client_id = ""           # public OAuth client id; empty = manual token flow
scopes = ["webmaster:hostinfo", "metrika:read"]
redirect_uri = ""        # default: https://oauth.yandex.ru/verification_code
```

`providers.bing.indexnow_key` and `providers.bing.indexnow_key_location` are **public**
identifiers, not secrets: the key file is served openly at the location (default
`https://<host>/<key>.txt`), and the same key is sent in every IndexNow request. They are safe in
`seo.toml` and are not redacted by `config show`. The Bing Webmaster API key stays a secret:
store it with `lv24-seo auth set --provider bing` or set `LV24_SEO_BING_API_KEY`.

`providers.yandex_metrika.counter` selects the counter read by `stats` and every `metrika`
command. `goals` lists numeric goal ids used by `lv24-seo metrika goals`; an empty list is valid and
produces a warning instead of a provider call. At most five goal ids are queried per request
(Metrika's 20-metric cap), and a longer list is reported as truncated.

## `[indexing]`

Quota-aware submission and index-check automation is disabled by default. `enabled = true` is
required before `lv24-seo indexing run --allow-write` will apply anything; without it the run is a
read-only plan. `providers` selects the engines that participate; a provider that is not configured
is reported as `not_configured`, never guessed.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | explicit opt-in for provider submissions |
| `providers` | `["bing", "yandex", "gsc", "indexnow"]` | engines included in `--provider all` |
| `max_submit_per_run` | `500` | global submission cap for one run |
| `max_inspect_per_run` | `20` | GSC URL Inspection / Bing `GetUrlInfo` checks per run |
| `stale_days` | `7` | `indexing status` marks state older than this as `stale` |
| `check_interval_days` | `7` | re-check cadence; also sets `next_check_at` |
| `gsc_inspect_daily_limit` | `2000` | local UTC-day cap (GSC exposes no remaining count) |
| `indexnow_daily_limit` | `1000` | local UTC-day cap for IndexNow submissions |

The engine still enforces provider caps at run time: Bing's remaining daily/monthly quota,
Yandex's `quota_remainder`, 500 URLs per Bing batch and one URL per Yandex request. A scheduled
`lv24-seo indexing run --allow-write` also needs `LV24_SEO_WRITE_SECRET` in the host scheduler's
environment; see [scheduling](scheduling.md).

## `[auth.gsc]` and `[auth.yandex]`

OAuth client registrations for `lv24-seo auth login`. `client_id` is public; the client secret is
**not** part of this model and is stored with
`lv24-seo auth set --provider gsc_oauth_secret|yandex_oauth_secret --stdin`. An empty `client_id`
keeps the manual token flow.

| Key | Default | Meaning |
|---|---|---|
| `client_id` | `""` | public OAuth client id |
| `scopes` | provider default | requested scopes; Google defaults to `webmasters.readonly`, Yandex to `webmaster:hostinfo` + `metrika:read` |
| `redirect_uri` | provider default | Google loopback `http://127.0.0.1:<port>/callback`; Yandex `https://oauth.yandex.ru/verification_code` |

A configured `redirect_uri` is validated by `auth status` and `doctor`: Google must use
`http://127.0.0.1:<port>/...` (not `localhost`, not HTTPS, not another host) and Yandex must
be exactly `https://oauth.yandex.ru/verification_code`. An empty value means the provider
default and is always valid. The granted scopes are recorded in the secret store
(`gsc_token_scopes`, `yandex_token_scopes`, `yandex_metrika_token_scopes`) so the readiness
check compares real data; see [OAuth connectors](auth.md#readiness-diagnostics).

Environment overrides: `LV24_SEO_GSC_CLIENT_ID`, `LV24_SEO_GSC_REDIRECT_URI`,
`LV24_SEO_YANDEX_CLIENT_ID`, `LV24_SEO_YANDEX_REDIRECT_URI`. See
[auth-google.md](auth-google.md) and [auth-yandex.md](auth-yandex.md).

> Telemetry is not configurable: the tool sends nothing anywhere. There is no `[privacy]` section and
> One `seo.toml` describes one project; a multi-site block is not implemented.

## Data directory

`storage.data_dir` has three scopes:

| Value | Resolved against |
|---|---|
| Relative value written in `seo.toml` (`data_dir = "./data"`) | the config file's parent |
| Built-in default (no `[storage]` block) | the process working directory |
| `LV24_SEO_DATA_DIR` | used as given; a relative value resolves against the process working directory. The environment always wins over the file |

A config that pins a relative directory is therefore self-contained: `--config /srv/site/seo.toml`
uses `/srv/site/data` from any working directory. `lv24-seo init` creates `<config dir>/data` and
writes `data_dir = "./data"`. Paths are made absolute with `os.path.abspath` (`~` expanded, `..`
normalised); symlinks are deliberately not resolved, so a config reached through a link anchors its
data directory next to the link. The SQLite store is `<data_dir>/seo.sqlite3`.

`lv24-seo doctor --json` makes the scope visible: `meta.data_dir` is the effective directory,
`meta.data_dir_source` is `config` when it is absolute (pinned by the file or an absolute
`LV24_SEO_DATA_DIR`) and `working directory` when it is relative, and a relative directory with a
config file present adds a warning that names the config's folder.
A relative directory still works, but running the same config from two directories reads two
different databases. Set an absolute path to pin it. `lv24-seo config show --json` reports the same
effective path.

The opt-in `<data_dir>/secrets.json` fallback follows the same directory; the desktop app and the
CLI share it only when they resolve the same data directory.

## Locale

`report.locale` accepts `en`, `ru` and `zh`; the default is `ru`. It selects the language of
human-readable output: the weekly email subject, HTML and plain text, the CLI human summary, the
HTML written by `report weekly --out`, and Telegram messages. It also selects number, percent,
position, date and plural formatting for those surfaces.

Locale is **presentation-only**. JSON envelopes (`--json`), MCP payloads, stored snapshots,
`report diff` and the `report.json` written by `--out` stay canonical English, and machine codes
(`metric`, `severity`, `engine`, `kind`, `status`, `reason`) are never translated. An unsupported
locale fails config validation with `CONFIG_INVALID` instead of falling back silently.

`report.locale` is the single source of truth for the rendered language of email, Telegram, the
CLI human summary and the HTML written by `report weekly --out`. The lower-level delivery API
mirrors it per channel: `EmailConfig.locale` and `TelegramConfig.locale` default to `en` and are
what `send_email` / `send_telegram` use (neither function accepts a `locale` argument).
`deliver(locale=...)` is only a fallback for a channel config that carries no locale; when the
channel configs are present, their own `locale` wins. There are no `report.email.locale` or
`report.telegram.locale` keys in `seo.toml` — set `report.locale` once.

## Timezone

`report.timezone` is the IANA zone used for report and `stats` windows, MCP default windows
(`gsc_search_analytics`, `seo_opportunities`, `seo_weekly_report`) and local day boundaries. It must
be a valid IANA name; an invalid zone fails config validation with `CONFIG_INVALID`. Provider data
stays date-based and is not converted. Scheduler generators apply the same zone where the backend
supports it — see [scheduling](scheduling.md).

## Google Search Console authentication

GSC accepts two credential forms. Resolution order for every GSC command (`properties`, `stats`,
`gsc query`, `report weekly`, the MCP GSC tools) is:

1. the service-account file: `providers.gsc.service_account_file`, or
   `LV24_SEO_GSC_SERVICE_ACCOUNT_FILE` when set (the global environment-over-file rule applies);
2. the `gsc_token` secret — `LV24_SEO_GSC_TOKEN`, the OS keychain entry written by
   `lv24-seo auth set --provider gsc`, or the opt-in `0600` secrets file.

A service-account file must be an **absolute** path; `~` is expanded. A relative, missing or
unreadable file fails with `CONFIG_INVALID` and a next action instead of silently falling back to a
token. The engine reads and validates the JSON once per process, signs an RS256 JWT (`iss`, `scope`,
`aud`, `exp`), exchanges it at the key's `token_uri` with
`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, and caches the resulting access token. The
token is refreshed 60 seconds before expiry, so a long-lived MCP server survives token rotation. The
JSON and private key are never printed, logged or put in an error message; only the file path is
reported.

`gsc_token` keeps the historical behaviour: a plain string is sent as
`Authorization: Bearer <token>` and is **not** refreshed. A value that is a service-account JSON
object (detected by `"type": "service_account"` plus `private_key`) is accepted as well, so a
single-secret CI setup works without a file.

`lv24-seo auth status` is keychain/env/file-secret only; it does not inspect
`service_account_file`. Use `lv24-seo properties --json` or `lv24-seo doctor --json` (the `gsc_auth`
check) for readiness; neither prints the key.

## Secrets

| Source | How |
|---|---|
| OS keychain | `lv24-seo auth set --provider … --stdin` stores the token through the optional `secrets` extra; `auth login` prints instructions only |
| Optional `secrets` extra | `pip install "lv24-seo[secrets]"` — without it, keychain calls fail and env/file fallbacks apply |
| Environment | `LV24_SEO_YANDEX_WEBMASTER_TOKEN=…` |
| Headless fallback | `LV24_SEO_ALLOW_FILE_SECRETS=1` + `data/secrets.json` (0600, warning on every run) |

Credentials are either manually supplied access tokens/API keys or, for GSC, a service-account JSON
key. The service-account flow signs and refreshes its own short-lived access token; plain tokens and
API keys have no refresh flow. Expiry depends on the provider and grant; presence does not prove
validity.

`lv24-seo auth status` reports provider, secret presence and storage source, never the token.
It does not verify permissions or perform an API call.

## Environment variables

See [`.env.example`](../.env.example). All are prefixed `LV24_SEO_`; the mapping is
`report.email.to` ↔ `LV24_SEO_MAIL_TO`. `LV24_SEO_REPORT_LOCALE` sets `report.locale` and accepts
`ru`, `en` or `zh`. `LV24_SEO_INDEXNOW_KEY` and `LV24_SEO_INDEXNOW_KEY_LOCATION` override the
public `[providers.bing]` IndexNow fields.

`LV24_SEO_DATA_DIR` overrides `storage.data_dir` and is **process-scoped**: a relative value resolves
against the working directory, not the config file. Use it for container/CI runs where the data
directory is mounted elsewhere; run `lv24-seo doctor --json` to confirm which directory won.

`LV24_SEO_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, …) opts the process into redacting logging. The CLI
and `lv24-seo mcp serve` install the handler; library imports and `create_server()` never reconfigure
logging. Unset means "leave logging alone". Output is one JSON line per record when stderr is not a
terminal (a pipe, scheduler log or MCP host) and a plain line otherwise.

## Multi-property

Not supported yet (planned in WP8.4). Today one `seo.toml` describes exactly one project and one set of
provider targets; run several configs (`--config path/to/seo.toml`) for several sites.
