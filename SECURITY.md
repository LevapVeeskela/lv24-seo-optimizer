# Security policy

## Supported versions

Pre-alpha: only `main` is supported until the first tagged release.

## Reporting a vulnerability

Open a private security advisory on GitHub (Security → Advisories → New advisory) or email
`paulalexeev10@gmail.com`. Do not open a public issue for a vulnerability. Expect an answer within
7 days.

## Secret handling

* Tokens are read from, in order: CLI flags → `LV24_SEO_*` environment → OS keychain → `0600` file
  under the data directory. Nothing is written into `seo.toml` or any tracked file.
* `--allow-file-secrets` / `LV24_SEO_ALLOW_FILE_SECRETS=1` is only for headless hosts without a
  keyring; it prints a warning every run.
* Logs go through a redactor that strips `Authorization`, `access_token`, `refresh_token`, cookies
  and any value shorter than 8 chars that looks like a token. Provider request/response bodies are
  never logged at INFO.
* CI runs `gitleaks` on the full history; GitHub push protection is enabled for the repository.

## Data handling

* Provider data (queries, pages, traffic) belongs to the property owner and stays in the local
  database (`data/seo.sqlite3`) and report files (`reports/`). Neither directory is committed.
* The tool sends nothing to LV24 or to any third party. The only outbound calls are the provider
  APIs the user configured.
* Fixtures in `tests/fixtures` are synthetic or anonymised; never real customer queries.

## Defaults that protect users

* Read-only by default: `sitemap submit`, `recrawl queue`, `IndexNow submit` require
  `--allow-write` (CLI) or a confirmed write tool (MCP) with an idempotency key.
* Destructive provider calls are logged with a plan-first preview (`--dry-run` is the default for
  write commands until `--apply` is passed).
* Rate limiting is per provider and per method; the tool refuses to exceed documented quotas and
  surfaces `Retry-After` instead of retrying blindly.

## Dependencies

Renovate/Dependabot is enabled for security updates. New runtime dependencies must be pinned,
licensed permissively (Apache-2.0/MIT/BSD), and justified in the PR.
