"""``lv24-seo portfolio``: read-only roll-up over several local projects (F11).

One ``seo.toml`` describes one project; agencies keep those projects as sibling folders.
This sub-app scans a root directory and reports each site's storage state and last activity
without opening a provider, reading a token or writing to disk. The scan itself lives in
:mod:`lv24_seo.core.portfolio`, so a future MCP tool or the desktop can reuse the same
contract.

The sub-app is attached by :func:`register`; ``main.py`` stays the only place that composes
the root command tree.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from lv24_seo.cli import emit, run_envelope
from lv24_seo.core.envelope import ok, partial
from lv24_seo.core.portfolio import MAX_SITES, scan_portfolio

__all__ = ["portfolio_app", "register"]

portfolio_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Roll up several local projects from one directory (read-only).",
)


@portfolio_app.command("list")
def portfolio_list(
    root: Annotated[
        Path,
        typer.Option("--root", help="Absolute directory that contains project folders."),
    ],
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help=f"Maximum sites to inspect (1..{MAX_SITES}); discovery still counts all.",
        ),
    ] = MAX_SITES,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """List local projects, newest activity first, with crawl/report/issue counts."""

    def action() -> dict[str, Any]:
        scan = scan_portfolio(root, limit=limit)
        meta = scan.meta()
        if scan.truncated:
            return partial(
                scan.to_dict(),
                limitations=scan.limitations(),
                meta=meta,
                warnings=scan.warnings,
            )
        return ok(scan.to_dict(), meta=meta, warnings=scan.warnings)

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_list_lines(payload))


def register(app: typer.Typer) -> None:
    """Attach the ``portfolio`` sub-app to the root command tree."""
    app.add_typer(
        portfolio_app,
        name="portfolio",
        help="Roll up several local projects from one directory (read-only).",
    )


def _list_lines(payload: Mapping[str, Any]) -> list[str]:
    """Compact human summary; ``--json`` remains the machine contract."""
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    sites = data.get("sites") or []
    lines = [
        f"portfolio: {meta.get('count', 0)} of {meta.get('total', 0)} sites "
        f"under {meta.get('root', '')}"
    ]
    if not sites:
        lines.append("no projects found (looked for seo.toml up to depth 3)")
        return lines

    for site in sites:
        label = site.get("property") or site.get("name") or site.get("config_path") or "-"
        lines.append(
            f"  {label}  db={'yes' if site.get('db_present') else 'no'}"
            f"  crawl={site.get('last_crawl_at') or '-'} ({site.get('crawl_runs', 0)})"
            f"  report={site.get('last_report_at') or '-'}"
            f" ({site.get('report_snapshots', 0)})"
            f"  issues={site.get('issues_open', 0)}/{site.get('issues_total', 0)}"
        )
        error = site.get("error")
        if isinstance(error, Mapping):
            lines.append(f"    error: {error.get('code')}: {error.get('message')}")
    if meta.get("truncated"):
        hint = (
            "raise --limit for more"
            if int(meta.get("limit") or 0) < MAX_SITES
            else "scan a narrower root"
        )
        lines.append(
            f"note: showing {meta.get('count', 0)} of {meta.get('total', 0)} sites; {hint}"
        )
    return lines
