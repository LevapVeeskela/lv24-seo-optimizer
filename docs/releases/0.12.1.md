# 0.12.1 - Fix the indexing client lifecycle

Status: **released** (2026-11-17) on PyPI, npm and the MCP Registry. Desktop `0.9.0` bundles engine
`0.11.0`; desktop `0.9.1` follows.

Patch release. `indexing run` reported `internal_error` for Bing and Yandex because the indexing
engine closed a provider client between the quota read and the checks/submit phase, while
`SeoService` kept that closed client in its cache. The next phase reused it and failed with
`Cannot send a request, as the client has been closed.` `indexing status --live` was unaffected.

## Fixed

* `SeoService` no longer hands the indexing engine a `close_client` callback. The service owns and
  caches provider clients for the whole run, so the engine drops its local reference instead of
  closing a shared client. `indexing run` now plans and applies for Bing and Yandex.
* Added a regression test with a closable fake Yandex client that reproduces the cached-client
  failure.

## Verification

| Check | Result |
|---|---|
| `indexing run --from-sitemap --max-submit 20 --max-inspect 5` (lv24.by) | Bing planned 20, Yandex planned 20, GSC checked 5, IndexNow planned 20; no `internal_error` |
| `indexing status --live` (lv24.by) | Bing 99 daily / 1399 monthly remaining, Yandex 150, GSC 0/2000 used, IndexNow 0/1000, `write_secret_present: true` |
| Local gate | `ruff check`, `ruff format --check`, `mypy src`, MCP registry `--check`, full `pytest -q` green |

No API, CLI, schema or config changes. MCP stays at 12 tools; schema stays at v5.
