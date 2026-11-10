# Contributing

Thanks for helping. This project is meant to be usable by humans and by AI agents, so clear,
boring, well-tested changes win over clever ones.

## Quick start

```bash
git clone https://github.com/LevapVeeskela/lv24-seo-optimizer
cd lv24-seo-optimizer
uv sync --extra dev
uv run pytest -q
uv run lv24-seo doctor
```

## What we accept

* New **free-tier providers** with a documented quota and a recorded fixture test.
* New **crawl rules**: one rule = one file, a `rule_id`, a severity, a false-positive note and a test
  with two fixtures (violation + clean).
* **Report improvements** that keep output diffable.
* **Docs** fixes, especially where a provider limit is described imprecisely.
* **Skills** that encode a repeatable workflow (audit → plan → fix → verify), with the tool calls
  spelled out.

## What we do not accept

* Paid-only providers in the default path, or any integration that needs a card on file.
* SERP scraping of Google/Yandex.
* Code that silently swallows truncation or quota errors.
* Committing tokens, real customer data, or crawled pages.
* Rewrites that drop Windows support.

## Workflow

1. Open an issue describing the provider/rule/report and the evidence you have.
2. Branch from `main`: `feat/<area>-<short-name>`.
3. Keep the change to one area (see `AGENTS.md` §5 for commit format).
4. Add tests: unit for logic, contract for provider payload parsing, a rule fixture for crawl rules.
5. Run `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q`.
6. Open a PR with: problem, solution, verification commands, provider limits touched, screenshots for
   report changes.

## Commit format

```
[SEO](<area>) - <what changed>
```

Areas: `cli`, `mcp`, `providers`, `crawl`, `report`, `store`, `docs`, `infra`, `tests`, `skills`.

## Review checklist (maintainers)

- [ ] No new required paid dependency, no card required.
- [ ] `limitations[]` / truncation still honest.
- [ ] Write path gated and idempotent.
- [ ] Docs updated (tool table, `docs/data-sources.md`, command help).
- [ ] Tests cover the failure mode, not only the happy path.
- [ ] Works on Windows (paths, encodings, line endings).

## License

By contributing you agree your work is licensed under Apache-2.0 (inbound = outbound).
