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
# Reserved auth label; credentials resolve from env, optional keyring, then opt-in file fallback.
auth = "keychain"

[providers.yandex_webmaster]
host_id = "https:example.com:443"

[providers.yandex_metrika]
counter = "123456"
goals = [1, 2]

[providers.bing]
site = "https://example.com"

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
```

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

## Secrets

| Source | How |
|---|---|
| OS keychain | `lv24-seo auth set --provider … --stdin` stores the token through the optional `secrets` extra; `auth login` prints instructions only |
| Optional `secrets` extra | `pip install "lv24-seo[secrets]"` — without it, keychain calls fail and env/file fallbacks apply |
| Environment | `LV24_SEO_YANDEX_WEBMASTER_TOKEN=…` |
| Headless fallback | `LV24_SEO_ALLOW_FILE_SECRETS=1` + `data/secrets.json` (0600, warning on every run) |

Credentials currently use manually supplied access tokens/API keys. There is no built-in refresh-token
or service-account flow. Expiry depends on the provider and grant; presence does not prove validity.

`lv24-seo auth status` reports provider, secret presence and storage source, never the token.
It does not verify permissions or perform an API call.

## Environment variables

See [`.env.example`](../.env.example). All are prefixed `LV24_SEO_`; the mapping is
`report.email.to` ↔ `LV24_SEO_MAIL_TO`. `LV24_SEO_REPORT_LOCALE` sets `report.locale` and accepts
`ru`, `en` or `zh`.

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
