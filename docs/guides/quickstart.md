# Quickstart

Verified against the 0.12.2 CLI (`lv24-seo <command> --help` is the authority when in doubt); the
list has 25 top-level commands (18 groups + 7 commands). `0.12.2` is the latest published engine,
and desktop `0.9.0` bundles engine `0.11.0`.

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

## 7. Local insights: logs, tasks, portfolio and backup

```bash
lv24-seo logs analyze --file /var/log/nginx/access.log --json   # bot rollups + crawl-waste share
lv24-seo tasks list --json                                      # tracked findings
lv24-seo portfolio list --root /srv/sites --json                # read-only multi-site roll-up
mkdir -p backups
lv24-seo backup create --out "$PWD/backups" --json               # config + SQLite history zip
lv24-seo backup restore --file "$PWD/backups/lv24-seo-backup-<stamp>.zip" \
  --to "$PWD/restored" --json                                    # new dir; nothing overwritten
lv24-seo backup restore --file "$PWD/backups/lv24-seo-backup-<stamp>.zip" \
  --config "$PWD/seo.toml" --json                                # plan; add --allow-write --confirm TOKEN
```

`logs analyze` reads exactly one local nginx JSON access log (absolute path, max 100 MiB) and never
stores or uploads it. Bot classes are user-agent only, so treat them as triage, not verified crawler
identity. `tasks list` shows tasks recorded with `tasks add`; an add is idempotent per
`(kind, property, url, rule)` and `tasks update` moves a task through
`open → in_progress → verified`. `portfolio list` scans sibling project folders (absolute `--root`,
up to three levels deep, at most 50 sites) and reports local crawl/report/issue activity without
calling a provider. `backup create` writes a portable zip with a redacted `seo.toml`, a consistent
SQLite copy and a SHA-256 manifest. `backup restore --to DIR` validates the archive and extracts it
into a new directory (no token, existing directories refused); the restored database passes
`PRAGMA quick_check`. The in-place `--config` mode first returns a five-minute `confirm_token`;
apply with `--allow-write --confirm TOKEN`. It replaces files (never merges rows), copies the current
database/config to `.pre-restore-<utc>` first, and neither mode includes provider tokens — re-run
`auth set` afterwards.

## 8. Schedule it

```bash
lv24-seo schedule install --kind weekly --config ./seo.toml
lv24-seo schedule install --kind weekly --config ./seo.toml --allow-write
lv24-seo schedule status  --kind weekly --config ./seo.toml
```

`install` is plan-only by default: it prints the cron block / systemd units / task XML and the
command it would run (`dry_run: true`, `installed: false`). Nothing touches the host until
`--allow-write`; `--print` is an alias for the default. The generated command embeds the
**absolute config path**, so custom names and multiple configs in one folder keep working.

`status` is read-only and reports `installed` / `outdated` / `partial` / `missing` / `unknown`, the
expected paths and whether each artefact matches the current spec. `schedule uninstall --allow-write`
removes exactly the generated block/units/task and is idempotent; without the flag it only prints
the plan.

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
