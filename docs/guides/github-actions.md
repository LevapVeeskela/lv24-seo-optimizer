# Weekly reports on GitHub Actions

Run `lv24-seo report weekly` on GitHub's scheduler instead of a server. The report is built on a
fresh `ubuntu-latest` runner, delivered over SMTP/Telegram, and the SQLite state (sends ledger and
snapshots) is carried between runs with `actions/cache`. On a **public repository with standard
runners this costs $0**.

The complete workflow lives in [`examples/github-actions/seo-weekly.yml`](../../examples/github-actions/seo-weekly.yml);
the copy below is identical.

## When to use Actions vs cron on a VPS

| Need | GitHub Actions | VPS cron / systemd timer |
|---|---|---|
| No server to patch or monitor | yes | no |
| $0 | public repo + standard runner | VPS is always paid |
| Exact start time / SLA | best effort: delayed, occasionally dropped | yes, the host clock |
| Stable outbound IP for provider allowlists | no, shared pool | yes |
| Read local nginx logs, private network, VPN | no | yes |
| Keep history longer than 7 days | no (cache/artifact limits) | yes |
| Long crawl next to the report | 15-minute job here; 6-hour platform limit | limited only by the host |
| Secret storage | GitHub encrypted secrets | `EnvironmentFile` mode `0600` |
| Setup effort | low | medium |

Choose **Actions** when the site is public, all providers are reachable over the internet, weekly
cadence is enough, and you do not need local logs. Choose a **VPS** when timing is contractual, a
provider allowlists your IP, you analyse nginx logs (`lv24-seo audit logs`), or you need months of
SQLite history. A common split: Actions sends the weekly report, the VPS runs nightly crawls and log
ingestion. See [vps-cron.md](vps-cron.md) for the self-hosted variant.

## The workflow

Copy this file to `.github/workflows/seo-weekly.yml` in **your site's repository**, not in the
`lv24-seo` repository.

```yaml
# Weekly lv24-seo report on GitHub Actions.
#
# Copy this file to .github/workflows/seo-weekly.yml in *your site's* repository
# (not the lv24-seo repository), then follow the guide in the lv24-seo repository:
# docs/guides/github-actions.md.
#
# Zero-spend policy for this recipe:
#   * public repository + standard ubuntu-latest runner = $0;
#   * no matrix, no larger runner, no paid service;
#   * artifacts are kept for 7 days (short retention, no long-term storage);
#   * a private repository consumes included/billed Actions minutes and storage.
name: seo-weekly

on:
  # GitHub cron is always UTC. 06:00 UTC = 09:00 Europe/Minsk (UTC+3, no DST).
  # Monday morning is stable because GSC data lags 2-3 days (docs/scheduling.md).
  schedule:
    - cron: "0 6 * * 1"
  # Manual run from the Actions tab; useful for the first setup and for token rotation.
  workflow_dispatch:

# Least privilege: the job only reads the checked-out repository.
# No write scopes, so a compromised dependency cannot push or open PRs.
permissions:
  contents: read

# Never let a manual run and a scheduled run overlap: two senders would race on the
# SQLite state and could send the same report twice. Cancelling the older run is safe
# because the report is idempotent (sends ledger + content hash).
concurrency:
  group: seo-weekly-${{ github.ref }}
  cancel-in-progress: true

jobs:
  report:
    name: weekly report
    # Standard GitHub-hosted runner only. Larger runners are always billed.
    runs-on: ubuntu-latest
    # Hard cap so a hung provider call cannot burn minutes indefinitely.
    timeout-minutes: 15
    env:
      # Relative to the checkout. The directory must exist before the report; see below.
      LV24_SEO_DATA_DIR: ./data
      LV24_SEO_LOG_LEVEL: INFO

    steps:
      - name: Check out the repository
        uses: actions/checkout@v7
        with:
          # The report job never pushes; do not leave the token in .git/config.
          persist-credentials: false

      - name: Install uv and Python
        # Pinned to the same action version as this project's own CI.
        uses: astral-sh/setup-uv@v10.3.3
        with:
          # Sets UV_PYTHON; uvx downloads this interpreter on first use if absent.
          python-version: "3.13"

      - name: Create the data directory
        # `lv24-seo init` creates ./data, but this workflow never runs init. The CLI opens
        # SQLite only when the directory exists, so without this step no baseline is saved.
        run: mkdir -p "$LV24_SEO_DATA_DIR"

      - name: Restore the SQLite state
        # A cache is not a backup. It preserves the sends ledger (idempotency) and snapshot
        # history across ephemeral runners; report deltas come from the provider APIs.
        # GitHub deletes cache entries that are not accessed for 7 days.
        uses: actions/cache/restore@v6
        with:
          path: |
            data/seo.sqlite3
            data/seo.sqlite3-wal
            data/seo.sqlite3-shm
          # run_id makes the key unique; restore-keys picks the newest previous entry.
          key: seo-db-${{ github.ref_name }}-${{ github.run_id }}
          restore-keys: |
            seo-db-${{ github.ref_name }}-

      - name: Materialise the Google service account
        # Service account JSON cannot be pasted into seo.toml (it is a secret), and it
        # must be a file for the Google auth library. Decode it into the runner temp dir.
        # Delete this step if you do not use Search Console.
        env:
          GSC_SERVICE_ACCOUNT_B64: ${{ secrets.LV24_SEO_GSC_SERVICE_ACCOUNT_B64 }}
        run: |
          set -euo pipefail
          umask 077
          if [ -z "${GSC_SERVICE_ACCOUNT_B64:-}" ]; then
            echo "::error::repository secret LV24_SEO_GSC_SERVICE_ACCOUNT_B64 is not set"
            exit 1
          fi
          mkdir -p "$RUNNER_TEMP/secrets"
          printf '%s' "$GSC_SERVICE_ACCOUNT_B64" | base64 --decode > "$RUNNER_TEMP/secrets/gsc.json"
          # Export the path to later steps without writing the JSON anywhere persistent.
          printf 'LV24_SEO_GSC_SERVICE_ACCOUNT_FILE=%s\n' "$RUNNER_TEMP/secrets/gsc.json" >> "$GITHUB_ENV"

      - name: Build and send the weekly report
        # Only this step sees the provider and delivery credentials. GitHub masks secret
        # values in logs; the CLI also redacts tokens from every error and JSON envelope.
        # Non-secret identifiers (property, host_id, counter, mail addresses, chat_id)
        # live in the committed seo.toml, not here.
        env:
          LV24_SEO_YANDEX_WEBMASTER_TOKEN: ${{ secrets.LV24_SEO_YANDEX_WEBMASTER_TOKEN }}
          LV24_SEO_YANDEX_METRIKA_TOKEN: ${{ secrets.LV24_SEO_YANDEX_METRIKA_TOKEN }}
          LV24_SEO_SMTP_PASSWORD: ${{ secrets.LV24_SEO_SMTP_PASSWORD }}
          LV24_SEO_TELEGRAM_BOT_TOKEN: ${{ secrets.LV24_SEO_TELEGRAM_BOT_TOKEN }}
        run: |
          # `bash -e` (the GitHub default) would abort before we can inspect the exit code.
          set +e
          # Pin for reproducibility if you prefer: uvx --from "lv24-seo==0.1.0" ...
          # Write the JSON to a file instead of the log: it can contain query/page data and
          # logs of a public repository are public.
          uvx --from lv24-seo lv24-seo report weekly --send --json > "$RUNNER_TEMP/weekly-report.json"
          status=$?
          # Exit code 5 is documented as partial success (truncated data in strict mode).
          # Keep the run green but surface it; a skipped provider is only a warning.
          if [ "$status" -eq 5 ]; then
            echo "::warning title=Partial report::Exit code 5; check the JSON artifact for limitations."
            status=0
          fi
          exit "$status"

      - name: Save the SQLite state
        # Runs even after a partial or failed report: the snapshot and sends ledger are
        # still valid and must not be thrown away.
        if: always()
        uses: actions/cache/save@v6
        with:
          path: |
            data/seo.sqlite3
            data/seo.sqlite3-wal
            data/seo.sqlite3-shm
          key: seo-db-${{ github.ref_name }}-${{ github.run_id }}

      - name: Upload the JSON envelope
        # Small debug artefact. retention-days: 7 satisfies the zero-spend rule: no
        # repository artifact may outlive seven days. On a private repository artifact
        # storage counts against the included quota; delete this step for strict $0.
        if: always()
        uses: actions/upload-artifact@v7
        with:
          name: weekly-report-json
          path: ${{ runner.temp }}/weekly-report.json
          retention-days: 7
          if-no-files-found: ignore
```

## One-time setup

### 1. Commit a `seo.toml` (no secrets)

Start from [`examples/config/seo.toml`](../../examples/config/seo.toml) and keep it in the site
repository. It carries only identifiers and preferences. At minimum the workflow needs:

* `[project]` — `name`, `base_url`;
* `[providers.gsc] property` — the exact Search Console property string;
* `[providers.yandex_webmaster] host_id` and `[providers.yandex_metrika] counter` if used;
* `[report.email]` — `enabled = true`, `to`, `from`, `smtp_host` (plus `smtp_port`/`smtp_user` if
  your server needs them);
* `[report.telegram]` — `enabled = true`, `chat_id`.

`--send` asks for delivery, but it does not invent a channel: email is sent only when
`report.email.enabled`, `to` and `smtp_host` are all set, and Telegram only when
`report.telegram.enabled` and `chat_id` are set. Missing both produces the warning
`delivery: no channel configured` and the report is still built.

### 2. Add repository secrets

Settings → Secrets and variables → Actions → **New repository secret**. Names are case-sensitive.

| Secret | Value | Required for |
|---|---|---|
| `LV24_SEO_GSC_SERVICE_ACCOUNT_B64` | base64 of the Google service-account JSON key | Search Console |
| `LV24_SEO_YANDEX_WEBMASTER_TOKEN` | Yandex OAuth token, scope `webmaster:hostinfo` | Yandex Webmaster |
| `LV24_SEO_YANDEX_METRIKA_TOKEN` | Yandex OAuth token, scope `metrika:read` | Metrika |
| `LV24_SEO_SMTP_PASSWORD` | SMTP password or app password | email delivery |
| `LV24_SEO_TELEGRAM_BOT_TOKEN` | BotFather token | Telegram delivery |

Non-secret values (`property`, `host_id`, `counter`, `to`, `from`, `smtp_host`, `smtp_user`,
`chat_id`) stay in `seo.toml`. If you consider them private, move the identifiers to repository
**variables** (`vars.*`) and map them to the matching `LV24_SEO_*` variable in `env:`; keep
`report.email.enabled` and `report.telegram.enabled` in `seo.toml`, because they are config flags,
not environment overrides.

### 3. Google Search Console (service account)

1. In Google Cloud, create or pick a project and enable the **Search Console API**.
2. Create a service account, then **Keys → Add key → JSON**. Download the file once.
3. In Search Console → **Settings → Users and permissions**, add the service account address
   (`name@project.iam.gserviceaccount.com`) as **Full** or **Owner**. A service account cannot
   verify a new domain property; a human must have verified it already.
4. Base64-encode the JSON and paste the single-line output as `LV24_SEO_GSC_SERVICE_ACCOUNT_B64`:

   ```bash
   # Linux / macOS
   base64 < gsc-service-account.json | tr -d '\n'
   ```

   ```powershell
   # Windows PowerShell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("gsc-service-account.json"))
   ```

5. Delete the downloaded key after pasting. The base64 form is **not encryption**; GitHub encrypts
   the secret at rest, and the workflow writes it only to `$RUNNER_TEMP` for the job.

After the first run, `lv24-seo properties --json` under the same environment should list Search
Console as ready. If it still reports `gsc: token missing`, the build does not read the
service-account file yet and GSC rows are skipped (the rest of the report is unaffected).

An interactive OAuth setup (`LV24_SEO_GSC_CLIENT_ID`/`SECRET`) needs a browser loopback and a refresh
token, so it is a local flow; for unattended CI the service account is the intended path.

### 4. Yandex Webmaster and Metrika

Create OAuth tokens with the scopes in the table above in Yandex OAuth and paste each one directly
into the matching repository secret. Optionally store the Webmaster token in the local keychain:

```bash
lv24-seo auth login --provider yandex          # prints the instructions
lv24-seo auth set --provider yandex --stdin    # stores yandex_webmaster_token
```

There is no `auth set --provider metrika` in v0.1; use `LV24_SEO_YANDEX_METRIKA_TOKEN` (or the
0600 secrets file) for Metrika. Webmaster tokens live ~6 months and Metrika tokens ~1 year; put a
rotation reminder in the calendar. `lv24-seo auth status` reports only whether a token is present and
where it came from — never the value.

### 5. SMTP

Put the host, port, user, sender and recipient in `seo.toml`; only the password is a secret.

* Use port **587 with STARTTLS** (the default). The SMTP transport opens a plain connection and
  upgrades it with STARTTLS, so implicit-TLS port 465 is not supported by this build. Port 25 is
  blocked on GitHub-hosted runners.
* Align **SPF + DKIM + DMARC** on the sending domain before relying on delivery.
* A Gmail/Workspace account needs an app password; normal account passwords and 2FA will fail.

### 6. Telegram (optional)

1. Create a bot with **@BotFather**, copy the token into `LV24_SEO_TELEGRAM_BOT_TOKEN`.
2. Send the bot a message, then read the chat id from
   `https://api.telegram.org/bot<token>/getUpdates` (or use a chat-id helper bot).
3. Put the id in `[report.telegram] chat_id` and keep `severity = "critical"` unless you want every
   weekly digest.

## First run

Actions → **seo-weekly** → **Run workflow**. The first run is normally a cache miss and builds a new
baseline. Confirm that:

* the job is green, with a `Partial report` annotation in the summary if exit code 5 occurred (partial
  success);
* the email/Telegram message arrived;
* the log contains no token, password or `Authorization` header;
* a second manual run with unchanged data sends nothing and reports `meta.sent: false`; the sends
  ledger deduplicates the same digest (set `only_on_change = false` to force a resend).

## Cost and limits

* **Public repository + standard `ubuntu-latest` runner = $0.** GitHub documents standard runners as
  free and unlimited for public repositories.
* **A private repository is billed.** Its standard runners consume the plan's included minutes and
  then charge per minute; cache/artifact storage beyond the included quota is also billed. Treat this
  recipe as $0 only for public repositories.
* **Larger runners are always billed**, on public and private repositories. This workflow never uses
  them: one job, `ubuntu-latest`, no matrix.
* **No matrix.** A single job keeps the run to a few minutes; `timeout-minutes: 15` caps the worst
  case.
* **Artifacts 7 days.** `retention-days: 7` is the maximum used here. Delete the upload step for
  strict $0 on private repositories, or lower retention to 1 day.
* **Cache 7 days, 10 GB.** GitHub deletes cache entries not accessed for 7 days and allows 10 GB per
  repository for free; more is billed. The SQLite file is small, so the default is far below 10 GB.
* **No paid service is required.** No DataForSEO, SerpApi, captcha solver or proxy pool is used.

### Data in the cache and artifacts

`data/seo.sqlite3` holds the report payload (which can include top queries and pages) and the
delivery ledger — not tokens — but it is still business data. On a **public** repository, cache
entries can be read by anyone who can open a pull request, and artifacts are downloadable by anyone
who can read the repository. If that data is sensitive, pick one:

* use a private repository (billed, as above);
* encrypt the SQLite file with a passphrase secret before saving the cache;
* delete both the cache and artifact steps and accept that every run loses the sends ledger (a repeat
  run may resend) and the snapshot history.

Secrets are never written to the cache or the artifact; the service-account JSON exists only in
`$RUNNER_TEMP` for the life of the job.

## Operational notes

* Schedules run only from the **default branch** and only if the workflow file exists there.
* Cron is **UTC**. `0 6 * * 1` is Monday 09:00 in Minsk. GitHub may delay scheduled runs during high
  load (especially at the top of the hour) and can drop queued runs; use `17 6 * * 1` if an exact
  minute matters.
* In a **public** repository, scheduled workflows are disabled automatically after **60 days without
  repository activity**. Re-enable the workflow in the Actions tab (or `gh workflow enable`), or let
  normal commits keep the repository active.
* The runner is ephemeral. `data/` exists only for the duration of the job; the cache is the only
  continuity. If the cache is evicted (more than 7 days between runs), the sends ledger and snapshot
  history restart; the report still compares the provider windows, but a repeated run may deliver the
  same digest again.
* Secrets are not passed to workflows triggered from forks. This workflow has no `pull_request`
  trigger, so that is not a concern here.
* Pin `lv24-seo==X.Y.Z` in the `uvx` command if you want reproducible reports; `uvx --from lv24-seo`
  follows the latest release.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Exit `3`, `AUTH_REQUIRED`, or HTTP `401` | Token expired/revoked, secret missing or misnamed, service-account key deleted | Re-authenticate with `lv24-seo auth set --provider <name> --stdin` and update the secret; for GSC create a new JSON key and add the new service account to the property |
| One provider is missing from the report | Its token is absent/empty, so the aggregate report skips that engine and warns `<engine>: token missing` | Compare `lv24-seo properties --json` under the same environment; set the matching secret. A single-engine command fails with `AUTH_REQUIRED` instead of skipping |
| HTTP `403` | GSC service account not added to the property, wrong property string (`sc-domain:` vs `https://`), API not enabled; Yandex host not verified for the account | Grant Full/Owner in Search Console; copy the exact property/host id; enable the API. Do not retry a 403 in a loop |
| `429`, `QUOTA_EXCEEDED`, exit `4` | GSC 1,200 QPM / 50,000 rows per day per site, Metrika 5,000 req/day, Yandex recrawl quota | Wait for `Retry-After`, reduce `--limit` (query commands) or `--days` (report), split the work. Scheduled runs must not hammer the API |
| Report is empty or all zeros | GSC lags 2–3 days, Yandex ~4 days; wrong property; a too-short window; or no provider token is configured | Check the warnings and `data_age`/`limitations`, run `report weekly --json` (no `--send`), confirm the property with `lv24-seo properties`; for a delivery test set `only_on_change = false` for one run |
| No email, `meta.sent: false` | This exact report digest was already delivered (`already_sent`), or no channel is fully configured | Expected for a repeat run with unchanged data; set `only_on_change = false` for one run or complete the channel config |
| `delivery: no channel configured` | `report.email.enabled`/`to`/`smtp_host` or `report.telegram.enabled`/`chat_id` missing | Fill `seo.toml`; the warning is non-fatal and the report is still built |
| SMTP timeout / connection refused | Outbound SMTP is filtered: port 25 is blocked on GitHub-hosted runners, and some clouds also filter 587 | Use submission port 587 with STARTTLS if your provider allows it; otherwise deliver through an HTTPS email API on 443 (the optional Resend/Brevo adapters in [scheduling.md](../scheduling.md)) or switch to Telegram. An SMTP-only build cannot deliver from a network that blocks every SMTP port |
| Workflow never starts | Workflow not on the default branch, Actions disabled, cron typo, or 60-day inactivity in a public repo | Merge to the default branch, enable the workflow, validate the cron, run `workflow_dispatch` manually |
| `uvx: command not found` | The `setup-uv` step was removed or failed | Keep `astral-sh/setup-uv` before the report step |
| Cache restore misses | First run, or more than 7 days since the last run | Harmless: the sends ledger starts empty (a repeat run may resend); report deltas still come from provider data |

Exit codes are documented in [docs/cli.md](../cli.md): `0` ok, `2` usage, `3` auth, `4` quota,
`5` partial success, `1` unexpected.

## What this workflow does NOT do

* **No rank scraping.** It does not scrape Google/Yandex SERPs, bypass CAPTCHA, or track positions
  outside the official GSC/Yandex APIs.
* **No paid APIs by default.** DataForSEO/SerpApi adapters are BYO-key and disabled unless you
  configure them; this workflow configures none.
* **No provider writes.** It never submits sitemaps, queues recrawls or calls IndexNow: those need
  `--allow-write` (and a human decision), which the workflow does not pass.
* **No Google Indexing API submissions.** The Indexing API only accepts `JobPosting`/
  `BroadcastEvent`; this tool does not pretend otherwise.
* **No browser OAuth.** Loopback/device flows need a human; CI uses a service account or a token you
  created interactively.
* **No long-term data store.** History beyond the 7-day cache/artifact window is not preserved; use
  a VPS or your own database backup if you need it.
* **No exact-time guarantee.** GitHub's scheduler is best effort and can delay or drop runs.
* **No telemetry.** Property data goes only to the providers you configured and to your SMTP/Telegram
  recipients; nothing is sent to LV24.

## Verify the YAML locally

```bash
python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('seo-weekly.yml').read_text(encoding='utf-8')); print('ok')"
```

PyYAML follows YAML 1.1, so it reads the `on:` key as boolean `True`; that is a parser quirk, not an
invalid workflow. GitHub reads YAML 1.2 and treats it as the trigger key. If PyYAML is unavailable,
check indentation manually: two spaces per level, no tabs, no duplicate keys.

After the first successful run, delete the downloaded service-account JSON and keep only the GitHub
secret. If a token leaks, revoke it at the provider first, then replace the secret; deleting the
Actions log is not enough.
