# 0.5.2 — data-dir anchoring, replay-safe writes and honest caps

This is a bugfix release for the 0.5.x engine. It closes blockers found after 0.5.1: a relative
`storage.data_dir` in `seo.toml` followed the shell's working directory, the CLI built its write
gate without the idempotency ledger, sitemap fetch errors echoed URLs that can carry query tokens,
and GSC claimed complete data when the final page exactly filled the row cap.

No schema migration and no new CLI command or MCP tool. The JSON envelope keeps its existing keys;
`doctor` adds the additive `meta.data_dir` / `meta.data_dir_source` fields and may carry a warning.
Existing stores open unchanged, and configs that already use an absolute `data_dir` behave exactly
as before.

## Fixed

### A relative `data_dir` is anchored to the config file

**Problem.** `[storage].data_dir = "./data"` resolved against the process working directory, so
`lv24-seo --config /srv/site/seo.toml` run from another folder read and wrote a stray `./data`.
Two shells could see two different databases for the same config, and a scheduler job only worked
when its working directory happened to match.

**Fix.** `load_config` resolves a relative `data_dir` written in the file against that file's
parent before environment overrides are merged. `lv24-seo init` creates `<config dir>/data` and
writes `data_dir = "./data"`, so an initialised workspace is self-contained. Paths are made
absolute with `os.path.abspath`: `~` is expanded and `..` is normalised, but symlinks are not
resolved.

Two cases stay process-scoped on purpose, because they describe the process rather than a file:

* the built-in default (no `[storage]` block at all) stays `data` relative to the working directory;
* `LV24_SEO_DATA_DIR` keeps overriding the file and stays relative to the working directory.

`lv24-seo doctor --json` makes the scope visible instead of guessing: `meta.data_dir` is the
effective directory, `meta.data_dir_source` is `config` when it is absolute (pinned by the file or
an absolute `LV24_SEO_DATA_DIR`) and `working directory` when it is relative, and a relative
directory with a config file present adds a warning that names the config's folder.
`config show --json` reports the effective path too.
`doctor` does not fail on a relative directory: it is the historical default, not a broken setup.

**Limits.** A config reached through a symlink anchors its data directory next to the link, not the
target, because symlinks are deliberately kept. `LV24_SEO_DATA_DIR` is not anchored to the config
file; it remains an explicit process-scoped override.

### `resolve_config_path` returns an absolute path

**Problem.** The resolved config path could be relative, so a scheduler artifact or a relative
setting derived from it depended on the process working directory.

**Fix.** `resolve_config_path` always returns an absolute, normalised path (`~` expanded, `..`
normalised, symlinks kept). `schedule install` and `schedule print` embed that absolute path in the
generated `--config` argument and use the config's parent as the working directory. A job therefore
runs the same config from any scheduler working directory; the cron/systemd/Task Scheduler formats
themselves are unchanged.

### CLI writes use the idempotency ledger

**Problem.** The CLI constructed `WriteGate` without a store. A `confirm_token` is an HMAC valid
for five minutes and is not consumed by verification, so replaying the same token could call the
provider a second time.

**Fix.** A write command with `--confirm` opens the workspace SQLite ledger (the same database
`init` creates) and passes it to `WriteGate`. A replayed token now returns `status: skipped` with
`reason: duplicate` and the provider is called exactly once. A dry run opens no ledger and creates
no database as a side effect.

Without an initialised workspace the command still works — sitemap and IndexNow submissions need no
config — but the envelope carries an explicit warning that replay protection is off for the
token's five-minute lifetime. A config that cannot be loaded produces its own warning instead of
failing the write.

**Limits.** No data directory means no replay protection: the token remains valid for its full
five-minute window. Deleting `data/seo.sqlite3` resets the ledger. A `pending` claim left by a
crash is still treated as in-flight (no TTL, unchanged from 0.5.1).

### Sitemap fetch errors no longer echo the URL

**Problem.** A failed sitemap fetch returned `str(exc)`, and a fetch exception routinely embeds the
full URL including its query string. Those strings reach CLI warnings, report `limitations[]` and
MCP payloads, so a sitemap URL carrying `?key=…` could leak a token into a report or a log.

**Fix.** The reader reports only the exception type name (`HTTPError`, `TimeoutError`, …). The
failure stays visible and machine-readable; the URL, query string and credentials do not appear.

### GSC reports a full final page at the cap as truncated

**Problem.** After 0.5.1, a final page that exactly filled the 50,000-row cap was treated as
complete. The provider may still hold rows behind the cap, so a report could claim full coverage
from an "exactly 50,000 rows" answer.

**Fix.** A final page that fills the cap now sets `truncated: true`. With the default cap the
result carries the `rows_cap_50000` limitation; a smaller caller cap produces a warning that names
the caller's row limit. A zero cap is also reported as a cap, not as an empty result set.

**Limits.** A site with exactly 50,000 rows and nothing behind the cap is also reported truncated.
That is deliberate: a full page is not proof that no further row exists. Consumers must keep
treating `truncated: true` as "coverage is not proven", never as "there is definitely more data".

### Redacting logging is opt-in at process entry points

**Problem.** The redacting formatter existed, but no process entry point installed it, so
`LV24_SEO_LOG_LEVEL` had no effect and the docs had to say logging was ignored.

**Fix.** `configure_logging_from_env()` installs the redacting handler only when
`LV24_SEO_LOG_LEVEL` is set. Exactly two process entry points call it: the CLI callback and
`lv24-seo mcp serve`. A library import, `create_server()`, `SeoService` or a provider client never
replaces the root handlers, so an embedding application keeps its own logging configuration.
Output is one JSON line per record when the stream is not a terminal (a pipe, scheduler log or MCP
host) and a plain line otherwise. The MCP transport logs to stderr only; stdout stays the protocol.

**Limits.** Logging remains opt-in; the default is no installed handler, and the `--json` envelope
is still the stable diagnostic interface. The redaction rules are the existing ones (sensitive keys,
known token prefixes and opaque long values), not a guarantee that every provider-specific secret
shape is recognised.

## Verified

- Offline suite: 1141 passed, 1 skipped.
- `ruff check`, `ruff format --check` and `mypy src` are clean.
- `python -m lv24_seo.mcp.registry --check` is clean (no MCP surface change).
- New regression tests cover data-dir anchoring from another working directory, the `doctor` scope
  note and `meta.data_dir_source`, absolute config paths and scheduler artifacts, CLI ledger replay
  and the store-less warning, the dry run not creating a database, sitemap exception redaction,
  GSC exact-cap truncation and the opt-in logging wiring.

## Upgrade notes

- Configs with a relative `[storage].data_dir` now use `<config dir>/data`. If you relied on the
  working directory, set an absolute `data_dir` or keep using `LV24_SEO_DATA_DIR`; run
  `lv24-seo doctor --json` and read `meta.data_dir_source` before upgrading a scheduled host.
- Existing `data/seo.sqlite3` files need no migration.
- The matching desktop `0.2.2` release bundles engine `0.5.2`; `0.2.1` users receive it through the
  signed in-app updater.
