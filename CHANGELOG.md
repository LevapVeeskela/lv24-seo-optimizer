# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is [SemVer](https://semver.org/).

## [Unreleased]

### Added

- Repository scaffold: Apache-2.0 license, agent contract (`AGENTS.md`), `llms.txt`, CI, issue/PR templates.
- `lv24_seo.mcp.registry` — canonical tool registry (10 tools) and generated `schemas/tools.json`.
- Minimal CLI (`version`, `doctor --json`).
- Docs: architecture, data sources and quotas, MCP surface, CLI, config, scheduling, skills.
- Research: 2026 landscape (Google/Yandex APIs, existing SEO MCP servers, paid tooling, audit signals).
- Skills: `seo-weekly`, `seo-indexing-triage`, `seo-technical-audit`.

### Notes

- Pre-alpha. No provider adapter is wired yet; see `docs/plans/2026-11-10-implementation-plan.md`.
