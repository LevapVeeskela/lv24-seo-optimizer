# Documentation map

Entry points, in reading order:

1. [`../README.md`](../README.md) — product, install, quick start, status.
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) — layers, provider contract, storage, report/delivery,
   deployment, invariants, extension points.
3. [`../AGENTS.md`](../AGENTS.md) — canonical contract for coding agents.
4. This file — where every other document lives.

## Reference

| Document | What it covers |
|---|---|
| [`cli.md`](cli.md) | 25 top-level commands, `--json` envelope, exit codes, `--strict` |
| [`mcp.md`](mcp.md) | 12 tools, 3 resources, 3 prompts, annotations and write gates |
| [`config.md`](config.md) | `seo.toml` keys, defaults, env overrides, secret storage |
| [`data-sources.md`](data-sources.md) | per-provider capability, quota and honesty limits |
| [`scheduling.md`](scheduling.md) | cron / systemd / Task Scheduler generators, SMTP, Telegram |
| [`auth.md`](auth.md) | OAuth connectors, readiness codes, keychain slots |
| [`auth-google.md`](auth-google.md) · [`auth-yandex.md`](auth-yandex.md) | provider-specific OAuth setup |
| [`skills.md`](skills.md) | skill format, export and editor mirrors |

## Architecture diagrams

[`architecture/README.md`](architecture/README.md) indexes the four committed diagrams. Edit the
`.mmd` source and re-render with `lv24-diagram`; `.svg`/`.png` are generated artifacts.

| Diagram | Question it answers |
|---|---|
| `01-system-context` | Who calls the engine, which external systems it reaches, what stays local |
| `02-provider-envelope-and-honesty` | How rows, caps, totals and failures are normalized |
| `03-weekly-report-and-delivery` | How a report is built, snapshotted and sent once |
| `04-release-pipeline` | Which gates sit between a PR and PyPI/npm/MCP Registry |

## Guides

| Guide | Audience |
|---|---|
| [`guides/quickstart.md`](guides/quickstart.md) | first run without reading the whole reference |
| [`guides/mcp-clients.md`](guides/mcp-clients.md) | Claude Desktop, OpenCode, VS Code, Cursor setup |
| [`guides/github-actions.md`](guides/github-actions.md) | scheduled runs in CI with stored secrets |
| [`guides/vps-cron.md`](guides/vps-cron.md) | a long-lived host scheduler deployment |
| [`guides/troubleshooting.md`](guides/troubleshooting.md) | typed errors, auth, quota and truncation issues |

## Decisions, plans and history

| Path | Contents |
|---|---|
| [`decisions/`](decisions/) | ADR-0001…ADR-0005; append, never rewrite accepted ADRs |
| [`plans/2026-11-10-implementation-plan.md`](plans/2026-11-10-implementation-plan.md) | waves, work packages and DoD |
| [`plans/2026-11-14-seo-desktop.md`](plans/2026-11-14-seo-desktop.md) | desktop shell scope |
| [`plans/2026-11-15-post-0.2.2-roadmap.md`](plans/2026-11-15-post-0.2.2-roadmap.md) | post-release roadmap |
| [`releases/`](releases/) | per-version release notes (`0.15.4` is current) |
| [`research/2026-11-landscape.md`](research/2026-11-landscape.md) | API limits and existing MCP servers |

## Repository-level files

| File | Purpose |
|---|---|
| [`../CHANGELOG.md`](../CHANGELOG.md) | Keep a Changelog history |
| [`../SECURITY.md`](../SECURITY.md) | token handling, write gating, vulnerability reporting |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | development and review workflow |
| [`../llms.txt`](../llms.txt) | curated doc map for LLM crawlers |
| [`../schemas/tools.json`](../schemas/tools.json) | generated MCP tool schemas |
| [`../skills/manifest.json`](../skills/manifest.json) | skill index with schema hash |

## Conventions

- `ARCHITECTURE.md` is the only system-design document; release notes and plans are historical and
  may describe older behavior. Code wins over prose; record a divergence here or in an ADR.
- Generated files (`schemas/tools.json`, `docs/architecture/*.svg|png`) are never hand-edited.
- Public docs portal: <https://seo.lv24.by/> (en at `/`, ru at `/ru/`, zh at `/zh/`).
