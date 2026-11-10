# CLI reference

Every command supports `--json` (machine output), `--verbose`, `--dry-run` where a write is involved,
and `--config PATH`. Output envelope:

```json
{ "ok": true, "data": {}, "meta": { "version": "0.1.0", "duration_ms": 812 }, "warnings": [] }
```

Exit codes: `0` ok · `2` usage · `3` auth · `4` quota/upstream · `5` partial success · `1` unexpected.

## Commands

```
lv24-seo init [--property https://example.com] [--non-interactive]
lv24-seo doctor [--json]
lv24-seo auth login --provider gsc|yandex|bing|crux [--no-browser]
lv24-seo auth status [--json]
lv24-seo auth logout --provider …
lv24-seo config show|set|path
lv24-seo properties list|add|remove
```

### Data

```
lv24-seo gsc query   --property sc-domain:example.com --start 2026-11-01 --end 2026-11-07 \
                     --dimensions query --limit 100 [--json]
lv24-seo gsc pages   --property … --days 28 --limit 100
lv24-seo gsc inspect --property … --url https://example.com/page     # URL Inspection
lv24-seo gsc sitemaps list|submit|delete                            # write, needs --allow-write

lv24-seo yandex queries   --host https:example.com:443 --limit 100
lv24-seo yandex indexing  --host …
lv24-seo yandex diagnostics --host …
lv24-seo yandex recrawl quota|queue|status --host …                 # write, needs --allow-write

lv24-seo metrika report --counter 123456 --period 7d [--goals 1,2]
lv24-seo crux origin --origin https://example.com
lv24-seo psi url --url https://example.com --strategy mobile
lv24-seo bing stats --site https://example.com
lv24-seo indexnow submit --key … --urls urls.txt                    # write
```

### Crawl and audit

```
lv24-seo crawl --start https://example.com --max-urls 500 --out data/crawl.json
lv24-seo audit --start https://example.com [--rules all|core|tech] [--fail-on critical]
lv24-seo audit logs --path /var/log/nginx/access.json.log --since 7d
```

### Reports

```
lv24-seo report weekly  --property … [--no-send] [--always] [--out reports/]
lv24-seo report indexing --property … 
lv24-seo report opportunities --property …
lv24-seo report send --file reports/weekly-2026-11-10.html --to owner@example.com
lv24-seo report history [--limit 20]
```

### Automation

```
lv24-seo schedule install --kind weekly --cron "0 6 * * 1" [--print]
lv24-seo schedule install --kind daily  --cron "0 6 * * *"
lv24-seo schedule status|remove
lv24-seo mcp serve [--transport stdio|http] [--port 8765]
lv24-seo install --client claude|opencode|vscode|cursor|copilot
lv24-seo export tools [--out schemas/tools.json]
```

## Conventions

* `--json` is always available and always the same envelope; humans get tables on a TTY.
* Dates are ISO-8601 (`2026-11-10`), durations are `7d`, `28d`, `90d`.
* `--allow-write` is required for every provider write; without it the command prints the plan and exits 0
  with `"dry_run": true`.
* `--idempotency-key` is accepted by write commands and stored in the audit table.
* No command ever prints a token, cookie or full `Authorization` header.

## Windows notes

* `lv24-seo schedule install` on Windows writes a Task Scheduler XML and calls `schtasks /Create /XML`.
* Paths in docs are given for both PowerShell and bash where they differ.
