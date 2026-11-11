# Security policy

## Supported versions

Use the latest 0.2.x release. Earlier releases contain known correctness and
transport-safety defects; see [CHANGELOG](CHANGELOG.md) before upgrading.

## Reporting a vulnerability

Use a private GitHub security advisory or email `paulalexeev10@gmail.com`.
Do not publish credentials, customer queries or exploit details in public issues.

## Credentials and local data

* Secrets resolve from `LV24_SEO_*` environment variables, then the optional OS
  keyring, then `data/secrets.json` only when `LV24_SEO_ALLOW_FILE_SECRETS=1` is set.
  There are no token command-line flags; `auth set` reads stdin or a hidden prompt.
* Install `lv24-seo[secrets]` for OS keychain support. On Windows, file fallback
  relies on directory/file ACLs; POSIX mode 0600 cannot enforce Windows permissions.
* No secrets belong in `seo.toml`, source control, reports or test fixtures.
  Logging applies redaction, but callers must never log raw credentials.
* Queries, URLs, traffic, snapshots and reports stay in the configured local
  storage. Requested provider calls, site crawling and opt-in SMTP/Telegram
  delivery transmit data to those destinations. No LV24 telemetry endpoint is used.
* Back up SQLite before upgrading. Legacy rows of unknown temporal granularity
  are retained and labelled, not silently converted or deleted.

## Network and delivery boundaries

* The default crawler refuses private, loopback and link-local destinations,
  validates each redirect and pins the connection to a validated address while
  preserving Host/SNI. Local crawling and HTTPS downgrade need explicit opt-in.
  Injected transports are trusted embedding/test boundaries.
* Crawl orchestration checks robots and origin scope before following a
  destination. Robots network/5xx errors defer crawling; robots is not access control.
* SMTP STARTTLS validates certificates before login. Telegram sends plain text
  and treats HTTP/Bot API errors as failures.
* Delivery uses semantic deduplication and serializes attempts through SQLite.
  A crash after remote acceptance but before the ledger write can still duplicate
  a message on retry: the contract is at-least-once, not exactly-once.
* HTTP rate limiting is process-local. It cannot guarantee persistent daily or
  account-wide quotas across separate CLI/MCP processes.

## Write operations

IndexNow submission requires explicit write opt-in, a reviewed confirmation
token and a local idempotency ledger. Sitemap submission and Yandex recrawl
currently return `WRITE_DISABLED`. Write calls are not automatically retried.
MCP annotations describe intent; they are not an authorization mechanism.

## Supply chain

CI runs offline tests, type/style checks, generated-schema drift checks and
gitleaks. Actions are SHA-pinned and downloaded release tools have checksums.
The release verification job checks the same commit and installs its wheel in
an isolated environment. Repository protection and account billing settings
must be checked separately; this file does not establish their current state.
