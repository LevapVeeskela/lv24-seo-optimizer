# Quickstart

This page will be complete after W6; the commands below describe the intended v0.1 flow.

## 1. Install

```bash
# run without installing
uvx --from lv24-seo lv24-seo doctor

# or install into the current environment
uv tool install lv24-seo
```

## 2. Check the machine

```bash
lv24-seo doctor
```

It verifies Python, config, writable data dir, DNS (SPF/DKIM/DMARC), TLS expiry, `robots.txt` and
sitemap reachability, and prints which providers still need a token.

## 3. Configure a site

```bash
lv24-seo init --property https://example.com
# writes ./seo.toml
```

## 4. Authorize providers

```bash
lv24-seo auth login --provider gsc       # opens a loopback OAuth flow in the browser
lv24-seo auth login --provider yandex    # prints a URL, paste the token back
lv24-seo auth status
```

Yandex Webmaster tokens live ~6 months; the CLI warns 14 days before expiry.

## 5. First data

```bash
lv24-seo gsc query --property sc-domain:example.com --days 7 --dimensions query --limit 20
lv24-seo yandex queries --host https:example.com:443 --limit 20
```

## 6. First report

```bash
lv24-seo report weekly --no-send --out reports/
open reports/weekly-*.html          # macOS
start reports\weekly-*.html         # Windows
```

## 7. Schedule it

```bash
lv24-seo schedule install --kind weekly --cron "0 6 * * 1"
lv24-seo schedule status
```

## 8. Use it from an agent

```bash
lv24-seo install --client claude     # writes the MCP server entry
```

Then in the agent: "проверь SEO за неделю и скажи, что чинить" — the `seo-weekly` skill takes over.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AUTH_REQUIRED` | no token for the provider | `lv24-seo auth login --provider …` |
| `403` from Yandex | host is not verified under that account | add the host in Webmaster UI |
| empty GSC rows | data lag 2–3 days, or the property id is wrong | try `--days 7` and `sc-domain:` prefix |
| `QUOTA_EXCEEDED` on recrawl | daily Yandex quota is tiny | wait for 00:00 MSK or split the batch |
