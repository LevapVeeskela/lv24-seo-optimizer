# ADR-0001 — Stack: Python 3.11+, uv, Typer, httpx, SQLite

**Status:** accepted (2026-11-10)

## Context

The tool must ship an MCP server, a CLI, an HTTP crawler, an HTML/JSON renderer and a scheduler
generator, and run on Windows, macOS and a small Linux VPS with zero infrastructure spend.

## Decision

* Language: **Python 3.11+** (3.12 recommended).
* Packaging: **hatchling** + **uv** (`uv sync` for dev, `uvx --from lv24-seo` for users).
* CLI: **Typer** on Click.
* HTTP: **httpx** (sync for CLI, async for crawl/providers).
* Parsing: **lxml** for HTML/XML, `json` for JSON-LD.
* Storage: **SQLite (WAL)** + gzipped JSONL for raw payloads.
* MCP: official Python SDK, stdio by default, stateless Streamable HTTP optional.

## Consequences

* One language for every surface; provider models shared between CLI and MCP.
* No Node toolchain, no bundler, no duplicated crawler.
* Python packaging friction (uv solves it) and slower cold start than Go — acceptable for a CLI.

## Rejected

* TypeScript: would need a second implementation of the crawler and report renderers.
* Go: excellent runtime, but MCP/HTML/templating ecosystem is weaker and iteration is slower.
