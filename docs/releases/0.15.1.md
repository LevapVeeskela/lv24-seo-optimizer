# 0.15.1 - Reject placeholder OAuth client IDs

Status: **released** (2026-11-18) on PyPI, npm and the MCP Registry. Desktop `0.9.5` bundles this
engine.

Patch release: a client id copied from the documentation is rejected before a consent URL is built,
instead of opening a provider page that answers HTTP 400.

## Fixed

### Placeholder OAuth client ids

The docs use example values such as `0123456789abcdef0123456789abcdef` (Yandex) and
`1234567890-abc.apps.googleusercontent.com` (Google). Those values are not registered provider apps,
so authorizing with them returned a 400 page ("unknown application"). The placeholder list now
includes the documented examples and `your-google-client-id` / `your-yandex-client-id`.

`yandex_oauth.authorization_url()` also checks the value itself, not only the CLI wrapper, so every
caller gets `CONFIG_INVALID` with the registration link and the exact config key instead of a URL.
Real client ids are unaffected; `auth status` and `doctor` already report a placeholder as
`oauth_client_missing`.

### Desktop

Desktop `0.9.5` (bundling this engine) disables **Connect** while the provider readiness is
`oauth_client_missing`, `oauth_client_secret_missing` or `oauth_redirect_invalid`, and ignores a
second **Open** click while the first one is still in flight, so a misconfigured setup cannot spawn
repeated consent tabs.

## Not changed

* MCP stays at **12 tools**, SQLite schema at **version 5**, CLI at **25 top-level commands**.
* No new dependency; no provider call; no change to the write gates.
* Real client ids and the OAuth token/refresh flows are unchanged.

## Verification

Offline gate on the release commit:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python -m lv24_seo.mcp.registry --check
uv run python -m pytest -q
```

New tests cover the documented placeholders, the `authorization_url()` guard and the unchanged real
client-id path. The full offline suite is green.

## Limitations

* A syntactically valid but unregistered 32-hex Yandex client id cannot be detected locally; the
  provider still answers 400. Register the app and use the ClientID it shows.
* `oauth_client_secret_missing` still requires `auth set --provider yandex_oauth_secret --stdin`
  (or the Google equivalent) before Connect can work.
