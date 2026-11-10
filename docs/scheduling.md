# Scheduling and delivery

## Model

There is **no daemon**. Scheduling is delegated to the host scheduler; the tool provides generators and
a `--print` mode so nothing is written without consent.

```bash
lv24-seo schedule install --kind weekly --cron "0 6 * * 1" --print
lv24-seo schedule install --kind daily  --cron "0 6 * * *"
lv24-seo schedule status
```

Three targets, detected from the OS (override with `--target`):

| Target | Artefact | Notes |
|---|---|---|
| `cron` | one crontab line | Linux/macOS; logs to `data/logs/schedule.log` |
| `systemd` | `<name>.service` + `<name>.timer` (user units) | `Persistent=true` catches missed runs after reboot |
| `windows` | Task Scheduler XML via `schtasks /Create /XML` | runs whether or not the user is logged in (`--run-as-service`) |

Generated commands always use absolute paths, `--json`, and a lock file so an overlapping manual run is
skipped instead of double-sending.

## Recommended cadence

| Job | When | Why |
|---|---|---|
| `report daily` (indexing + errors) | 06:00 local | catch index regressions within a day |
| `report weekly` (performance + opportunities) | Monday 06:00 | GSC data has 2–3 days of lag, Monday is stable |
| `crawl audit` | weekly, after the report | keep crawl close to the reporting window |
| `auth status` check | weekly | warn 14 days before a Yandex token expires |
| `crux origin` | weekly | 28-day rolling window, daily polling adds nothing |

## Email

* Default transport: SMTP (`587 STARTTLS`), because it works on a VPS and needs no vendor.
* Optional: Resend (3k emails/month free) for delivery events and idempotency; Brevo (300/day) as a
  second option. Adapters implement the same `deliver()` interface.
* Message is `multipart/alternative` with HTML + plain text; no remote pixels, no tracking.
* SPF + DKIM + DMARC must be aligned on the sending domain; the tool checks the DNS records in
  `doctor` and warns.
* Attachments only under 1 MB; larger artefacts are written to `reports/` and linked from the email if
  `report.public_url` is configured.
* Idempotency: `sends` ledger unique on `(kind, property, period, content_hash)`; `only_on_change`
  skips the send when the diff is below thresholds and prints `"skipped": "no_material_change"`.

## Telegram

Critical-only by default (indexing drop > 20%, sitemap errors, TLS expiry < 14 days, 5xx to bots).
Same `sends` ledger; message text is a shortened report with a link/command to get the full one.

## Other channels

* **GitHub issue** — `--channel github` opens an issue with the crawl defects (useful for a dev backlog
  even when the SEO owner is not a developer). Requires `gh` in the PATH and a repo slug.
* **Static HTML** — `reports/latest/index.html`, deployable to GitHub Pages or any static host.
* **JSON artefact** — `reports/latest/report.json`, for other agents or dashboards.

## Failure handling

* A failed provider does not fail the report: the email says which source is missing and why.
* Delivery failures retry with backoff (3 attempts) and then mark `failed` in `sends`; the next run
  reports `last_delivery_failed`.
* If the scheduler did not run for N days, the next report shows a `gap` banner instead of silently
  comparing non-adjacent periods.
