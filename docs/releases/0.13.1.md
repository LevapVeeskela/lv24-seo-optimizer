# 0.13.1 - Yandex connector URL-only mode

Status: **released** (2026-11-18) on PyPI, npm and the MCP Registry. Desktop `0.9.1` bundles engine
`0.13.1` and adds the Connect buttons.

Patch release. The desktop connector needs the Yandex authorization URL without a blocking
verification-code prompt, so `auth login` gains `--url-only`.

## Added

* `lv24-seo auth login --provider yandex|metrika --url-only --json` returns
  `{provider, flow, authorization_url, scopes, target}` and does not open a browser, print to
  stderr, exchange a code or write a token. The desktop shows the URL, the user approves access,
  and a second call with `--code <code>` completes the grant.
* `--url-only` is Yandex-only; `--provider gsc` keeps the loopback flow.

## Not changed

* No API, CLI count, MCP, schema or config change. `auth login --code` and the interactive flow are
  unchanged.

## Verification

| Check | Result |
|---|---|
| Unit test | `login_yandex(..., url_only=True)` returns the authorization URL and stores no token |
| Local gate | `ruff check`, `ruff format --check`, `mypy src`, MCP registry `--check`, full `pytest -q` green |
| Live consent | still pending a registered Yandex OAuth app/client ID |
