# 0.12.2 - Map Yandex recrawl 5xx honestly

Status: **released** (2026-11-17) on PyPI, npm and the MCP Registry. Desktop `0.9.0` bundles engine
`0.11.0`; desktop `0.9.1` follows.

Patch release. `yandex recrawl` and `indexing run --provider yandex` reported
`UPSTREAM_UNAVAILABLE: invalid JSON response from recrawl/queue` when Yandex returned a 5xx gateway
timeout with an empty body. The adapter parsed the body before checking the status, so a transient
provider failure looked like a malformed payload.

## Fixed

* `YandexWebmasterClient.recrawl_enqueue` checks `status >= 500` first and raises
  `UPSTREAM_UNAVAILABLE` with `message: "recrawl/queue upstream HTTP 504"`,
  `detail.status_code: 504` and `retryable: true`. `202`, `409 URL_ALREADY_ADDED` and
  `429 QUOTA_EXCEEDED` behavior is unchanged.
* Added an offline test with a 504 and an empty body; the message must not say "invalid JSON".

## Verification

| Check | Result |
|---|---|
| lv24.by `recrawl/queue` POST | Yandex returned HTTP 504 with an empty body repeatedly while `recrawl/quota` GET returned 150/150; the provider-side timeout is now reported honestly |
| Unit tests | Yandex adapter and indexing automation suites green |
| Local gate | `ruff check`, `ruff format --check`, `mypy src`, MCP registry `--check`, full `pytest -q` green |

No API, CLI, schema or config changes. The daily `lv24-seo-indexing` job retries the Yandex queue on
the next run; Bing, IndexNow and GSC are unaffected.
