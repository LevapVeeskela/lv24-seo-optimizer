"""``lv24-seo backup``: portable local workspace archives (F9).

One command, one read-only source: :func:`lv24_seo.core.backup.create_backup` builds the
``.zip``; this module only owns the Typer surface and the two output forms (the standard
envelope for ``--json`` and a compact summary for humans). No provider is called and no
secret is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from lv24_seo.cli import emit, run_envelope
from lv24_seo.core.backup import BACKUP_FORMAT, create_backup
from lv24_seo.core.envelope import ok

backup_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Create a portable local backup (config + SQLite history + report snapshots).",
)


@backup_app.callback()
def _backup_root() -> None:
    """Portable local backups; run ``backup create --out <absolute-dir>``."""


@backup_app.command("create")
def backup_create(
    out: Annotated[
        Path,
        typer.Option("--out", help="Existing absolute directory for the .zip archive."),
    ],
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Config file to use.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an archive with the same timestamped name."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Write ``manifest.json``, a redacted ``seo.toml`` and a consistent SQLite copy."""

    def action() -> dict[str, Any]:
        result = create_backup(out_dir=out, config_path=config_path, force=force)
        return ok(
            result.to_dict(),
            meta={"read_only_source": True, "format": BACKUP_FORMAT},
            warnings=list(result.warnings),
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_create_lines(payload))


def _create_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    manifest = data.get("manifest") or {}
    summary = data.get("summary") or {}
    lines = [
        f"backup: {data.get('archive_path', '')}",
        f"size: {int(data.get('archive_size') or 0)} bytes",
        f"sha256: {data.get('archive_sha256', '')}",
    ]
    for item in manifest.get("files") or []:
        if isinstance(item, Mapping):
            lines.append(f"  {item.get('path', '')}  {int(item.get('size') or 0)} bytes")
    schema = summary.get("schema_version")
    lines.append(f"schema: {schema if schema is not None else 'none'}")
    snapshots = summary.get("snapshots")
    if snapshots is not None:
        lines.append(f"snapshots: {snapshots}")
    lines.append("manifest: manifest.json (inside the archive)")
    return lines
