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
concurrency = 4   # reserved for a future bounded worker pool; the crawler is serial today
respect_robots = true
user_agent = "lv24-seo/0.1 (+https://example.com/bot)"

[storage]
data_dir = "./data"
raw_retention_days = 90   # reserved: no raw archives are written yet, pruning is a logged no-op
```

> Telemetry is not configurable: the tool sends nothing anywhere. There is no `[privacy]` section and
> One `seo.toml` describes one project; a multi-site block is not implemented.

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
`report.email.to` ↔ `LV24_SEO_MAIL_TO`.

## Multi-property

Not supported yet (planned in WP8.4). Today one `seo.toml` describes exactly one project and one set of
provider targets; run several configs (`--config path/to/seo.toml`) for several sites.
