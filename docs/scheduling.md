# Scheduling and delivery

## Model

There is **no daemon**. Scheduling is delegated to the host scheduler; the tool provides generators
and a **plan-only default**, so nothing is written without an explicit `--allow-write`.

```bash
lv24-seo schedule install --kind weekly --cron "0 6 * * 1" --config ./seo.toml
lv24-seo schedule install --kind weekly --cron "0 6 * * 1" --config ./seo.toml --allow-write
lv24-seo schedule status  --kind weekly --config ./seo.toml
lv24-seo schedule uninstall --kind weekly --config ./seo.toml --allow-write
```

`install` without `--allow-write` prints the artefact and the command it would run
(`dry_run: true`, `installed: false`); `--print` is an alias for that default. The generated command
embeds the **absolute `--config` path**, so a custom filename, a path with spaces, or several configs
in one folder keep working. The job also uses the config file's parent as its working directory, so a
scheduler that starts elsewhere still reads the same config and data directory.

`status` is read-only and reports `state` (`installed`, `outdated`, `partial`, `missing` or
`unknown`), the expected `paths`, and per-artefact `exists`/`matches`. A systemd timer without its
service is `partial`, not "installed"; when `crontab`/`schtasks` cannot be inspected the state is
`unknown` and `matches` is `null`. `uninstall --allow-write` removes exactly the generated managed
cron block, the two user units or the named task, reports `removed_items` / `removed_paths`, and is
idempotent (`removed: false` on a second run). It never uses wildcards and never touches unrelated cron lines
or tasks. `schedule print --kind weekly|daily|indexing --target …` remains available for rendering
one artefact without building the report job spec.

## Indexing automation job

Installing the indexing job is the explicit automation opt-in. It renders a job named
`lv24-seo-indexing` at `0 4 * * *` (daily, in the configured timezone):

```bash
lv24-seo schedule install --kind indexing --config ./seo.toml              # plan only
lv24-seo schedule install --kind indexing --config ./seo.toml --allow-write
lv24-seo schedule status  --kind indexing --config ./seo.toml
lv24-seo schedule uninstall --kind indexing --config ./seo.toml --allow-write
lv24-seo schedule print --kind indexing --target systemd --config ./seo.toml
```

The generated command is `lv24-seo indexing run --allow-write --config <absolute path> --json`.
`--allow-write` is embedded because installing the job is the opt-in; the engine still re-reads
provider quotas, verifies the plan hash, skips URLs whose `last_submitted_at` is newer than their
`lastmod`, and records one `indexing_runs` row per provider. `[indexing] enabled = true` is still
required, so a job installed before the config is enabled only plans.

**The write secret is not in the command line.** `LV24_SEO_WRITE_SECRET` must be present in the
environment the scheduler starts the job in. Keep it out of `seo.toml`, shell history and the
generated crontab/unit/XML:

* **systemd user unit**: use a drop-in that survives reinstall:
  `%h/.config/systemd/user/lv24-seo-indexing.service.d/secret.conf` containing
  `[Service]` and `EnvironmentFile=%h/.config/lv24-seo/write.env`, with a `0600` env file
  containing `LV24_SEO_WRITE_SECRET=<value>`; then run `systemctl --user daemon-reload`.
  Adding `Environment="LV24_SEO_WRITE_SECRET=<value>"` directly to the generated unit also
  works, but a hand-edited unit is reported as `outdated` and replaced on reinstall, so the
  drop-in is safer. The XML/cron/unit files never contain the value.
* **Windows Task Scheduler**: set the variable once in the user environment
  (`setx LV24_SEO_WRITE_SECRET "<value>"`) and start a new session so the task inherits it. The XML
  carries no secret.
* **cron**: export the variable in the crontab environment (for example
  `LV24_SEO_WRITE_SECRET=<value>` above the managed block) or source a `0600` env file from a wrapper
  script; do not put the value on the `lv24-seo` command line.

`indexing status` reports `write_secret_present: false` and `write_secret_missing` when the
variable is absent, so a missing secret is visible before the next scheduled apply fails with
`WRITE_DISABLED`.

Timezone honesty: the configured zone is applied where the backend supports it
and reported as a warning where it does not.

| Target | Artefact | Timezone behaviour |
|---|---|---|
| `cron` | one crontab line + `CRON_TZ=<zone>` assignment | confirm that the installed cron daemon supports `CRON_TZ`; otherwise it uses host time |
| `systemd` | `<name>.service` + `<name>.timer` (user units) | `OnCalendar` includes the configured IANA zone explicitly |
| `windows` | Task Scheduler XML + hidden `<name>.vbs` launcher via `wscript.exe //B //Nologo` | aware `StartBoundary` carries the resolved UTC offset; seasonal IANA zones produce a DST warning |

The Windows action starts the generated WScript launcher, not the console executable, so a scheduled
run never opens a console window in the signed-in session. The launcher runs the real command hidden
(`WScript.Shell.Run(command, 0, True)`), waits for it and forwards its exit code with
`WScript.Quit`. `schedule status` lists the task, the XML and the launcher; a missing or changed
launcher is reported as `outdated`/`partial`. Tasks installed by engine `0.14.0` or older keep the
console action until they are reinstalled once.

Only simple daily/weekly cron shapes convert to systemd/Windows; anything else
is rejected instead of silently rescheduled. An unknown/unavailable timezone is rejected
before installation. Windows needs `tzdata` (included on Windows); fixed-offset XML does not
encode an IANA zone's future DST rules, so review such tasks after seasonal transitions.

The install result carries a `warnings` list with the backend limits above. Re-installing replaces
every managed block for that job. A damaged block with a missing end marker is repaired
conservatively: only lines that look like lv24-seo entries are removed, unrelated lines are kept,
and the envelope warns you to review `crontab -l`.

## Recommended cadence

| Job | When | Why |
|---|---|---|
| `report weekly --send` (performance + opportunities) | Monday 06:00 | GSC data has 2–3 days of lag, Monday is stable |
| `crawl --start URL` | weekly, after the report | keep crawl close to the reporting window |
| `indexing run --allow-write` | daily 04:00 | check due URLs and submit within fresh quotas; needs `[indexing] enabled=true` and the write secret in the scheduler environment |
| `auth status` check | weekly | presence + source of every provider token |
| CrUX field data (in the weekly report) | weekly | 28-day rolling window, daily polling adds nothing |

## Email

* Default transport: SMTP (`587 STARTTLS`) with a verifying TLS context — the
  server certificate is checked and the login never runs after a TLS failure.
* The message is `multipart/alternative`: the product weekly template (HTML) plus the same content
  as 78-column plain text, and a localized subject (single line, at most 78 characters). Subject,
  HTML and plain text are rendered in `report.locale` (`ru`, `en`, `zh`); JSON snapshots and MCP
  payloads stay canonical English.
* Template: a 600px card with inline CSS only, a dark header and a text wordmark. The order is
  preheader → brand → property/period/verdict → four KPI cards (clicks, impressions, CTR and
  average position, each with current value, signed delta and previous value) → top three
  opportunities → issues by severity → merged sources → data notes → warnings → footer. Critical
  issues move ahead of opportunities. There are no external images, stylesheets, scripts, tracking
  pixels or remote fonts.
* Honesty in the template: unknown KPI values render as the localized "No data" text
  (`Нет данных` / `无数据`), never `0` and never an em dash; Yandex Metrica is shown as sessions,
  not clicks; an empty issues section says no issue data was provided instead of claiming the site
  is clean; provider limitations and warnings are kept and deduplicated (provider prose stays in
  its original language and is labelled as original when the report is localized); the footer
  carries the exact property id, the UTC generation time and "generated by lv24-seo". Raw machine
  fields (`metric`, `current`, `evidence`, booleans, rule ids) are not printed.
* Only email and Telegram channels exist. There are no Resend/Brevo adapters and no
  `--channel github` — provider-sent GitHub issues are not implemented.

`report.locale` is the single source of truth for the email and Telegram language. The lower-level
delivery API mirrors it: `send_email` and `send_telegram` take no `locale` argument and render/hash
with `EmailConfig.locale` / `TelegramConfig.locale` (default `en`). `deliver(locale=...)` is only a
fallback for a channel config that carries no locale; CLI and scheduled runs pass
`report.locale` into both channel configs.

## Telegram

The plain-text report (the same 78-column, localized email text) goes through the Bot API
`sendMessage` method. HTTP 4xx/5xx, transport timeouts and malformed Bot API answers are reported as
`failed` and never recorded as sent.

## Idempotency and retries

* `sends` ledger unique on `(kind, property, period, content_hash)`; the hash
  covers headline + sections and the normalized render locale, while `generated_at` and
  diagnostics are excluded. English omits the locale from the hash so the digest stays identical
  to the pre-localization ledger rows: upgrading does not re-send an already delivered English
  period. Rebuilding the same period does not resend; switching `report.locale` to `ru` or `zh`
  legitimately re-sends that period once in the new language, and switching back to `en` matches
  the legacy English row.
* `only_on_change` skips the send when the content hash was already delivered
  (`already_sent`), and the material gate skips it when no change field reaches
  the configured `report.thresholds` (`no_material_change`). The material gate runs before the
  locale hash is consulted, so a pure locale switch can still be suppressed when the period has
  no material change; it is delivered once the gate passes or the gate is disabled.
* Delivery is **at-least-once per channel**: an ambiguous transport result
  (accepted remotely, failed locally) is reported `failed` and never recorded,
  so the next run may deliver a duplicate. Concurrent runs de-duplicate
  through the SQLite transaction around check/send/journal; SMTP and the Bot API offer no
  exactly-once primitive after a process crash. Keep one schedule per period; no host lock
  file is installed. The database serializes delivery attempts and may block other writers
  up to the transport timeout.

## Other channels

* **Static HTML/JSON** — `report weekly --out reports/` writes `report.json`
  (canonical English) and `report.html` (rendered in `report.locale`) for hosting or other
  agents. There is no `reports/latest/` symlink and no `--channel github`.

## Failure handling

* A failed provider does not fail the report: the email says which source is missing and why.
  The envelope reports `sources_failed` and `complete: false`; only a total
  outage still returns an explicit no-data report instead of an exception.
