## Problem

<!-- What SEO question or failure does this change address? -->

## Solution

<!-- What changed, and why this way. -->

## Provider limits touched

<!-- GSC / Yandex / Bing quotas, truncation, data lag. Write "none" if not applicable. -->

## Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

## Checklist

- [ ] No paid dependency added to the default path
- [ ] `limitations[]` / truncation honest
- [ ] Write path gated (`--allow-write`, idempotency key)
- [ ] Docs updated in the same change
- [ ] Works on Windows
- [ ] No secrets, tokens or real query data in tests/fixtures
