"""``lv24-seo logs``: local access-log analysis (read-only, no provider calls).

The command reads exactly one user-supplied nginx JSON log. Path validation and the analysis
itself live in :mod:`lv24_seo.analysis.logs`, so the CLI and the ``seo_log_analysis`` MCP tool
share one contract and one set of security checks. Nothing here writes to disk or uploads the
log; the file is opened read-only and only aggregate rollups leave this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

import typer

from lv24_seo.analysis.logs import analyze_log_file
from lv24_seo.cli import emit, run_envelope
from lv24_seo.core.envelope import ok

__all__ = ["logs_app", "register"]

logs_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Analyze local nginx JSON access logs (read-only).",
)

_BOT_HELP = "Only this bot: googlebot, yandexbot, bingbot or other."


@logs_app.command("analyze")
def logs_analyze(
    file: Annotated[
        str,
        typer.Option("--file", help="Absolute path to one nginx JSON access log."),
    ],
    bot: Annotated[str | None, typer.Option("--bot", help=_BOT_HELP)] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Parse one nginx JSON log; report bot rollups and the wasted crawl share."""

    def action() -> dict[str, Any]:
        analysis = analyze_log_file(file, bot=bot)
        return ok(analysis.to_dict(), meta=analysis.meta(), warnings=analysis.warnings())

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_analyze_lines(payload))


def register(app: typer.Typer) -> None:
    """Attach the ``logs`` sub-app to the root command tree."""
    app.add_typer(logs_app, name="logs", help="Analyze local nginx JSON access logs (read-only).")


def _analyze_lines(payload: Mapping[str, Any]) -> list[str]:
    """Compact plain-text summary; ``--json`` remains the machine contract."""
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    waste = data.get("crawl_waste") or {}
    lines = [
        f"file: {meta.get('file') or '-'}",
        f"lines: {data.get('total_lines', 0)} total, "
        f"{data.get('parsed_lines', 0)} parsed, {data.get('skipped_lines', 0)} skipped",
    ]
    if meta.get("bot"):
        lines.append(f"bot: {meta['bot']}")
    lines.append(f"bot daily rows: {len(data.get('bot_daily') or [])}")
    lines.append(
        f"crawl waste: {waste.get('hits', 0)} of {waste.get('total_hits', 0)} requests, "
        f"{waste.get('bytes', 0)} bytes, share {waste.get('share', 0.0)}"
    )
    return lines
