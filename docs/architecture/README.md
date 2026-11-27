# Architecture diagrams

Mermaid sources live next to the generated files. Edit the `.mmd`, re-render with
`lv24-diagram render <file.mmd> --saved "<title>"`, and commit the `.svg`/`.png` with it. Do not
hand-edit generated files.

| # | Diagram | Source | Rendered | What it shows |
|---|---|---|---|---|
| 01 | System context | [.mmd](01-system-context.mmd) | [.svg](01-system-context.svg) · [.png](01-system-context.png) | Actors, provider APIs, crawl target, local history and opt-in delivery. |
| 02 | Provider envelope and honesty | [.mmd](02-provider-envelope-and-honesty.mmd) | [.svg](02-provider-envelope-and-honesty.svg) · [.png](02-provider-envelope-and-honesty.png) | How an adapter normalizes rows, caps, totals and limitations, and how a failure stays named. |
| 03 | Weekly report and delivery | [.mmd](03-weekly-report-and-delivery.mmd) | [.svg](03-weekly-report-and-delivery.svg) · [.png](03-weekly-report-and-delivery.png) | Report build, snapshot, content-hash send gate and duplicate skip. |
| 04 | Release pipeline | [.mmd](04-release-pipeline.mmd) | [.svg](04-release-pipeline.svg) · [.png](04-release-pipeline.png) | PR gates, version tag, publishing to PyPI/npm/MCP Registry and the desktop pin. |

Updated 2026-11-27.
