# Quickstart

Verified against the 0.6.0 CLI (`lv24-seo <command> --help` is the authority when
in doubt).

## 1. Install

```bash
# run without installing
uvx --from lv24-seo lv24-seo doctor

# or install into the current environment
uv tool install lv24-seo
```

Node users get the same CLI through a thin launcher (pinned to the npm package
version, with a recursion guard — see `packages/npm/README.md`):

```bash
npx @lv24/seo doctor --json
```

## 2. Check the machine

```bash
lv24-seo doctor
```

It verifies Python, the config file, a writable data dir and which providers
still need a token. It makes **no network calls** — no DNS, TLS-expiry or
robots/sitemap reachability checks.

## 3. Configure a site

```bash
lv24-seo init --property https://example.com
# writes ./seo.toml (+ ./data/ when missing)
```

## 4. Authorize providers

```bash
lv24-seo auth login --provider gsc       # prints manual token steps; no browser opens
lv24-seo auth login --provider yandex    # prints a URL, paste the token back
lv24-seo auth status
```

`auth set` stores the secret in the OS keychain. That path needs the optional
`secrets` extra:

```bash
pip install "lv24-seo[secrets]"           # OS keychain via keyring
# without it: LV24_SEO_* env vars or a 0600 secrets file outside the repo
```

## 5. First data

```bash
lv24-seo gsc query --start 2026-11-01 --end 2026-11-07 --dimensions query,page --limit 20
lv24-seo yandex queries --limit 20
lv24-seo yandex indexing
```

GSC needs explicit `--start/--end` (there is no `--days` shorthand and no
`--property` flag — the property comes from `seo.toml`). Yandex queries take
only `--limit`.

## 6. First crawl and report

```bash
lv24-seo crawl --start https://example.com/ --max-urls 50
lv24-seo report weekly --days 7 --lag-days 2 --out reports/
lv24-seo stats --days 7 --json
```

`report weekly` builds the digest, saves a snapshot to SQLite (when `init`
created the data dir) and writes `report.json`/`report.html` to `--out`.
Add `--send` to deliver through the configured email/Telegram channels — without
it nothing is sent.

## 7. Local insights: logs, tasks and backup

```bash
lv24-seo logs analyze --file /var/log/nginx/access.log --json   # bot rollups + crawl-waste share
lv24-seo tasks list --json                                      # tracked findings
mkdir -p backups
lv24-seo backup create --out "$PWD/backups" --json               # config + SQLite history zip
```

`logs analyze` reads exactly one local nginx JSON access log (absolute path, max 100 MiB) and never
stores or uploads it. Bot classes are user-agent only, so treat them as triage, not verified crawler
identity. `tasks list` shows tasks recorded with `tasks add`; an add is idempotent per
`(kind, property, url, rule)` and `tasks update` moves a task through
`open → in_progress → verified`. `backup create` writes a portable zip with a redacted `seo.toml`,
a consistent SQLite copy and a SHA-256 manifest. Restore is not implemented, so keep the archive as
your manual restore source.

## 8. Schedule it

```bash
lv24-seo schedule install --kind weekly --cron "0 6 * * 1" --config ./seo.toml --print
lv24-seo schedule install --kind weekly --config ./seo.toml
```

`--print` renders without touching the host. The generated command embeds the
**absolute config path**, so custom names and multiple configs in one folder
keep working. There is no `schedule status` command — inspect the host
scheduler (`crontab -l`, `systemctl --user list-timers`, Task Scheduler).

Timezone notes: `CRON_TZ` is a real assignment but only Vixie/ISC cron honours
it; systemd timers run on the system clock; Task Scheduler gets an aware
`StartBoundary` with a UTC offset when the zone resolves, host-local time
otherwise. Each backend prints its limits as warnings.

## 9. Use it from an agent

```bash
lv24-seo install --client claude     # writes the MCP server entry
lv24-seo export tools --out schemas/tools.json
lv24-seo export skills --out ./skills # copy packaged skills from the wheel/repo
```

Then in the agent: "проверь SEO за неделю и скажи, что чинить" — the `seo-weekly` skill takes over.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AUTH_REQUIRED` | no token for the provider | `lv24-seo auth login --provider …` |
| `403` from Yandex | host is not verified under that account | add the host in Webmaster UI |
| empty GSC rows | data lag 2–3 days, or the property id is wrong | widen `--start/--end`, check `sc-domain:` prefix |
| `QUOTA_EXCEEDED` on recrawl | daily Yandex quota is tiny | wait for 00:00 MSK or split the batch |
| `store: not configured` warnings | `data/` missing | run `lv24-seo init` first |
