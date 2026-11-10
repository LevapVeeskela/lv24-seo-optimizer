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
# auth: keychain by default; alternatives: env var name, or a 0600 file outside the repo
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
locale = "ru"           # ru | en
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
concurrency = 4
respect_robots = true
user_agent = "lv24-seo/0.1 (+https://example.com/bot)"

[storage]
data_dir = "./data"
raw_retention_days = 90
```

> Telemetry is not configurable: the tool sends nothing anywhere. There is no `[privacy]` section and
> no multi-site block yet — one `seo.toml` describes one project (multi-site lands in WP8.4).

## Secrets

| Source | How |
|---|---|
| OS keychain (default) | `lv24-seo auth login` stores the token; read back with `keyring` |
| Environment | `LV24_SEO_YANDEX_WEBMASTER_TOKEN=…` |
| File | `--secrets-file /run/secrets/seo.json` (`0600`, outside the repo) |
| Headless fallback | `LV24_SEO_ALLOW_FILE_SECRETS=1` + `data/secrets.json` (0600, warning on every run) |

Token lifetimes: Yandex Webmaster ~6 months, Metrika ~1 year, GSC refresh token until revoked (OAuth app
must be in production mode, otherwise refresh tokens expire in 7 days). Google service accounts do not
expire but must be added to the property as Full/Owner.

`lv24-seo auth status` prints provider, scope, expiry and `verified: true|false` — never the token.

## Environment variables

See [`.env.example`](../.env.example). All are prefixed `LV24_SEO_`; the mapping is
`report.email.to` ↔ `LV24_SEO_MAIL_TO`.

## Multi-property

Not supported yet (planned in WP8.4). Today one `seo.toml` describes exactly one project and one set of
provider targets; run several configs (`--config path/to/seo.toml`) for several sites.
