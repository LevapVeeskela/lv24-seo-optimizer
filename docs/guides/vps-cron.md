# Self-hosted scheduling: systemd timer or cron on a VPS

Use this when you need exact timing, a stable outbound IP, local nginx logs, or SQLite history longer
than the 7-day cache window of [GitHub Actions](github-actions.md). The tool has **no daemon**: it
renders a unit file or a crontab line, and the host scheduler runs it.

Both examples assume:

* a dedicated Linux user `seo` with home `/home/seo`;
* the project checkout at `/home/seo/seo` (the directory that contains `seo.toml`), initialised once
  with `lv24-seo init` so `data/` exists — the CLI opens SQLite only when that directory is present;
* `lv24-seo` installed with `uv tool install lv24-seo`, which puts the binary in
  `/home/seo/.local/bin/lv24-seo`;
* Python 3.11+ available to that binary.

Confirm the absolute paths before writing a unit — schedulers do not inherit your interactive `PATH`:

```bash
command -v lv24-seo
# /home/seo/.local/bin/lv24-seo
```

## 1. Generate the artefacts

`schedule print` renders without writing anything; `schedule install` writes the host-default
artefact and enables it (`systemd` on Linux, `cron` on macOS, Task Scheduler on Windows). Start with
`print` and review the output.

```bash
# systemd user units (service + timer)
lv24-seo schedule print --cron "0 6 * * 1" --target systemd

# one crontab block
lv24-seo schedule print --cron "0 6 * * 1" --target cron
```

With the paths above, `--target cron` renders:

```cron
# lv24-seo weekly SEO report
# CRON_TZ=Europe/Minsk
0 6 * * 1 cd /home/seo/seo && /home/seo/.local/bin/lv24-seo report weekly --json
```

With `--target systemd` it renders:

```ini
# lv24-seo-weekly.service
[Unit]
Description=lv24-seo weekly SEO report

[Service]
Type=oneshot
ExecStart=/home/seo/.local/bin/lv24-seo report weekly --json
WorkingDirectory=/home/seo/seo
```

```ini
# lv24-seo-weekly.timer
[Unit]
Description=lv24-seo weekly SEO report (timer)

[Timer]
OnCalendar=Mon *-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

> **Add `--send` yourself.** The v0.1 generator renders the report *build* command only; scheduled
> runs would write the report but not deliver it. Both production examples below add `--send` (or
> call a wrapper that does). Do not copy the raw generated command if you expect email or Telegram.

The generators only accept simple daily/weekly cron shapes for systemd and Task Scheduler. A complex
cron expression is rejected instead of being silently changed; use `--target cron` for those.

## 2. Secrets: `EnvironmentFile` with mode `0600`

Never put tokens in the unit, the crontab or `seo.toml`. Create one environment file readable only by
the service user:

```bash
sudo install -d -m 0750 -o seo -g seo /etc/lv24-seo
sudo -u seo sh -c 'umask 077; cat > /etc/lv24-seo/secrets.env' <<'EOF'
LV24_SEO_YANDEX_WEBMASTER_TOKEN=...
LV24_SEO_YANDEX_METRIKA_TOKEN=...
LV24_SEO_SMTP_PASSWORD=...
LV24_SEO_TELEGRAM_BOT_TOKEN=...
EOF
```

Content is `KEY=value`, one per line, with no `export` and no trailing comments after a value. To
edit it later: `sudo -u seo vi /etc/lv24-seo/secrets.env` (the mode and owner stay `0600 seo:seo`).

Values are parsed twice: by systemd for the timer and by the shell in the cron wrapper. Keep them
simple; if a value contains `#` or spaces, wrap it in double quotes (`KEY="va#lue"`), which both
parsers accept.

Verify before enabling anything:

```bash
stat -c '%a %U:%G' /etc/lv24-seo/secrets.env
# 600 seo seo
sudo -u seo test -r /etc/lv24-seo/secrets.env && echo readable
```

Notes:

* Environment variables are visible to `root` and to the service user through `/proc/<pid>/environ`.
  On a headless VPS the OS keyring is usually unavailable, so `EnvironmentFile` is the practical
  option; do not set `LV24_SEO_ALLOW_FILE_SECRETS=1` unless you deliberately use the JSON file
  fallback (it warns on every run).
* Rotate a token by editing this file; the next run reads the new value. No restart is needed.
* Keep the file outside the repository and outside `data/`. `chmod 0600` is part of the contract, not
  a suggestion.

## 3. systemd user timer

### 3.1 Units

Write the generated service, then add `--send`, the environment file, a restrictive umask and two
cheap sandbox options:

```ini
# ~/.config/systemd/user/lv24-seo-weekly.service
[Unit]
Description=lv24-seo weekly SEO report

[Service]
Type=oneshot
EnvironmentFile=/etc/lv24-seo/secrets.env
ExecStart=/home/seo/.local/bin/lv24-seo report weekly --send --json
WorkingDirectory=/home/seo/seo
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
```

Write the timer. The generated `OnCalendar` uses the host timezone; append the IANA zone so the run
does not move if the VPS clock is changed to UTC:

```ini
# ~/.config/systemd/user/lv24-seo-weekly.timer
[Unit]
Description=lv24-seo weekly SEO report (timer)

[Timer]
OnCalendar=Mon *-*-* 06:00:00 Europe/Minsk
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
```

* `Persistent=true` runs a missed job after a reboot instead of skipping the week.
* `RandomizedDelaySec=5m` avoids hitting every provider at the exact same second; it does not move the
  run to another day.
* Check that your systemd accepts the timezone suffix before enabling:

  ```bash
  systemd-analyze calendar "Mon *-*-* 06:00:00 Europe/Minsk"
  ```

  If it rejects the suffix, drop it and set the host timezone instead:
  `sudo timedatectl set-timezone Europe/Minsk`. Belarus is UTC+3 all year, with no DST.

### 3.2 Enable and verify

```bash
systemctl --user daemon-reload
systemctl --user enable --now lv24-seo-weekly.timer
systemctl --user list-timers lv24-seo-weekly.timer

# Headless VPS: keep the user manager (and therefore the timer) alive without a login session.
sudo loginctl enable-linger seo
```

`systemctl --user` must run as `seo`. If you run it through `sudo`, use
`sudo -u seo XDG_RUNTIME_DIR=/run/user/$(id -u seo) systemctl --user ...` or simply log in as `seo`.

## 4. cron

### 4.1 Production block

Install the block with `crontab -e`. `lv24-seo schedule install --kind weekly --cron "0 6 * * 1"`
writes the **host-default** artefact instead (systemd on Linux) and has no `--target` option; use
`--print` to preview it. Because the generated command also lacks `--send`, the production line uses
the wrapper below, which builds, delivers, and redirects the log:

```cron
# lv24-seo weekly report — Monday 06:00 Europe/Minsk
CRON_TZ=Europe/Minsk
0 6 * * 1 /home/seo/bin/lv24-seo-weekly >> /var/log/lv24-seo/weekly.log 2>&1
```

* `CRON_TZ` is a **crontab-wide** setting and must appear as its own active line before the job. The
  generator emits it as a comment because support varies: cronie and Debian/Vixie cron honour it,
  while some minimal crons (for example BusyBox `crond`) do not. If yours ignores it, set the system
  timezone or use the systemd timer.
* Cron starts a minimal shell with a small `PATH`; always use absolute paths.
* Cron does not read the systemd `EnvironmentFile`, hence the wrapper.

### 4.2 Wrapper script

```bash
#!/bin/sh
# /home/seo/bin/lv24-seo-weekly
set -eu
set -a
. /etc/lv24-seo/secrets.env
set +a
cd /home/seo/seo
exec /home/seo/.local/bin/lv24-seo report weekly --send --json
```

```bash
install -d -m 0755 /home/seo/bin
umask 077
cat > /home/seo/bin/lv24-seo-weekly <<'EOF'
#!/bin/sh
set -eu
set -a
. /etc/lv24-seo/secrets.env
set +a
cd /home/seo/seo
exec /home/seo/.local/bin/lv24-seo report weekly --send --json
EOF
chmod 0700 /home/seo/bin/lv24-seo-weekly
```

The wrapper is `0700` because it can read the secrets file; the secrets file itself stays `0600`.

## 5. Logs and rotation

### systemd

The journal already rotates. Keep a bounded size and read the last run with:

```bash
journalctl --user -u lv24-seo-weekly.service --since "7 days ago" --no-pager
journalctl --user -u lv24-seo-weekly.service -f        # follow a manual run
```

Cap the journal in `/etc/systemd/journald.conf` (`SystemMaxUse=500M`), then
`sudo systemctl restart systemd-journald`. For a one-off cleanup: `sudo journalctl --vacuum-time=30d`.

### cron

Create the log directory and rotate the redirect target:

```bash
sudo install -d -m 0750 -o seo -g seo /var/log/lv24-seo
```

```conf
# /etc/logrotate.d/lv24-seo
/var/log/lv24-seo/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 0600 seo seo
}
```

Test the rule without rotating: `sudo logrotate -d /etc/logrotate.d/lv24-seo`. Add `MAILTO=""` at the
top of the crontab if you do not want cron's own mail for non-zero exits on top of the redirect.

## 6. Test a run manually

Do this before waiting a week, and again after rotating a token.

### systemd — real environment

```bash
systemctl --user start lv24-seo-weekly.service
systemctl --user status lv24-seo-weekly.service
journalctl --user -u lv24-seo-weekly.service --since "10 min ago" --no-pager
```

`systemctl start` is the only test that uses the exact `EnvironmentFile`, `WorkingDirectory` and
umask of the timer.

### Either scheduler — dry run without sending

```bash
sudo -u seo sh -lc '
  set -a; . /etc/lv24-seo/secrets.env; set +a
  cd /home/seo/seo
  /home/seo/.local/bin/lv24-seo report weekly --json
'
```

Without `--send`, the command builds and persists the report but does not use email/Telegram. When
that is green, run the same command with `--send` once by hand.

### cron — reproduce cron's empty environment

```bash
sudo -u seo env -i HOME=/home/seo SHELL=/bin/sh PATH=/usr/local/bin:/usr/bin:/bin \
  /home/seo/bin/lv24-seo-weekly
```

If this fails while the interactive run works, the cause is almost always `PATH`, `HOME` or a missing
secrets file — not the report code.

## 7. Operations and troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `status=203/EXEC` | `ExecStart` path is wrong or not executable | `command -v lv24-seo`; use the absolute path; check `namei -l /home/seo/.local/bin/lv24-seo` |
| `status=200/CHDIR` | `WorkingDirectory` does not exist | Point it at the directory containing `seo.toml` |
| `EnvironmentFile ... Permission denied` | File is root-only or owned by another user | `chown seo:seo`, `chmod 0600`; test with `sudo -u seo test -r …` |
| Run happens 3 hours early/late | Host is UTC, `OnCalendar` has no zone, or `CRON_TZ` is unsupported/absent | Add `Europe/Minsk` to `OnCalendar`; add `CRON_TZ=Europe/Minsk`; or set the host timezone |
| `AUTH_REQUIRED` / exit `3` | Secrets not loaded (missing `EnvironmentFile` or cron wrapper) or token expired | Run the manual dry run above; re-authenticate and update the secrets file |
| Exit `4`, `QUOTA_EXCEEDED` | Provider quota exhausted | Wait for `Retry-After`; do not shorten the interval or loop |
| Exit `5` | Partial success: the report is real but truncated (strict mode); a skipped provider is only a warning and exits `0` | Read the `limitations` in the JSON envelope; fix the data source, not the scheduler |
| Two reports in one week | A manual run raced the timer | The CLI's lock file skips an overlapping run instead of double-sending; still avoid starting a manual run at the scheduled minute |
| Timer not firing after logout | User manager stopped without lingering | `sudo loginctl enable-linger seo` |
| Nothing in `journalctl` | You are looking at the system journal, not the user journal | Add `--user`; check `systemctl --user status …` |

Check the whole install at any time with:

```bash
lv24-seo doctor
lv24-seo auth status
systemctl --user list-timers lv24-seo-weekly.timer   # systemd
crontab -l                                           # cron
```

`doctor` checks Python, the config file, the data directory and token presence (no network calls).
`auth status` reports which providers have a token and where it came from; it never prints the value.

Remove a systemd schedule with `systemctl --user disable --now lv24-seo-weekly.timer` and delete the
two unit files; remove a cron schedule with `crontab -e`. Removing the job does not delete `data/` or
`reports/`.
