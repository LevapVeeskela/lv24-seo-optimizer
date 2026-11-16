"""``lv24-seo backup``: portable local workspace archives (F9).

Two commands, one read-only source: :func:`lv24_seo.core.backup.create_backup` builds the
``.zip``; :func:`lv24_seo.core.backup.plan_restore` / ``apply_restore`` validate and restore
it. This module only owns the Typer surface and the two output forms (the standard envelope
for ``--json`` and a compact summary for humans). No provider is called and no secret is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from lv24_seo.cli import emit, run_envelope
from lv24_seo.core.backup import (
    BACKUP_FORMAT,
    apply_restore,
    create_backup,
    plan_restore,
    restore_to_directory,
)
from lv24_seo.core.envelope import ok
from lv24_seo.core.errors import INVALID_INPUT, SeoError

backup_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Create or restore a portable local backup (config + SQLite history + snapshots).",
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


@backup_app.command("restore")
def backup_restore(
    archive_file: Annotated[
        Path,
        typer.Option("--file", help="Absolute path to the backup .zip to restore."),
    ],
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Absolute seo.toml to replace in place (needs --allow-write --confirm TOKEN).",
        ),
    ] = None,
    to_directory: Annotated[
        Path | None,
        typer.Option(
            "--to",
            help="New absolute directory for a non-destructive restore (must not exist).",
        ),
    ] = None,
    allow_write: Annotated[
        bool,
        typer.Option(
            "--allow-write",
            help="Opt in to the in-place restore; without it the command only plans and validates.",
        ),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="confirm_token returned by the in-place plan."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable envelope output.")
    ] = False,
) -> None:
    """Restore into a new directory (--to) or replace a workspace (--config, gated).

    --to and --config are mutually exclusive. A restore to a new directory never modifies an
    existing file, so it needs no confirm token; an in-place restore stays two-phase and needs
    both --allow-write and the plan's token.
    """

    def action() -> dict[str, Any]:
        if to_directory is not None:
            if config_path is not None:
                raise SeoError(
                    code=INVALID_INPUT,
                    message="--to and --config are mutually exclusive",
                    next_action=(
                        "use --to DIR for a non-destructive restore into a new directory, or "
                        "--config ABS for the gated in-place restore"
                    ),
                    retryable=False,
                )
            if allow_write or confirm is not None:
                raise SeoError(
                    code=INVALID_INPUT,
                    message="--allow-write and --confirm only apply to the in-place restore",
                    next_action=(
                        "drop them: --to writes only a new directory and never modifies "
                        "existing files"
                    ),
                    retryable=False,
                )
            restored = restore_to_directory(archive_path=archive_file, directory=to_directory)
            return ok(
                restored.to_dict(),
                meta={
                    "dry_run": False,
                    "destructive": False,
                    "writes_new_directory": True,
                    "format": BACKUP_FORMAT,
                    "limitations": list(restored.limitations),
                },
                warnings=list(restored.warnings),
            )
        if config_path is None:
            raise SeoError(
                code=INVALID_INPUT,
                message="backup restore needs --to DIR or --config ABS",
                next_action=(
                    "pass --to DIR for a non-destructive restore into a new directory, or "
                    "--config ABS for the gated in-place restore"
                ),
                retryable=False,
            )
        plan = plan_restore(archive_path=archive_file, config_path=config_path)
        if confirm is None:
            warnings = list(plan.warnings)
            if allow_write:
                warnings.append("no --confirm token was passed; nothing was written")
            return ok(
                plan.to_dict(),
                meta={
                    "dry_run": True,
                    "destructive": True,
                    "format": BACKUP_FORMAT,
                    "limitations": list(plan.limitations),
                },
                warnings=warnings,
            )
        result = apply_restore(plan, confirm, allow_write=allow_write)
        return ok(
            result.to_dict(),
            meta={
                "dry_run": False,
                "destructive": True,
                "format": BACKUP_FORMAT,
                "limitations": list(plan.limitations),
            },
            warnings=list(result.warnings),
        )

    payload = run_envelope(action)
    emit(payload, as_json=as_json, lines=_restore_lines(payload))


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


def _restore_lines(payload: Mapping[str, Any]) -> list[str]:
    if not payload.get("ok"):
        return []
    data = payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    if data.get("mode") == "to_directory":
        return _restore_to_lines(data)
    archive = data.get("archive") or {}
    target = data.get("target") or {}
    database = data.get("database") or {}
    config = data.get("config") or {}
    if not isinstance(archive, Mapping) or not isinstance(target, Mapping):
        return []
    if not isinstance(database, Mapping) or not isinstance(config, Mapping):
        return []
    lines = [
        f"restore: {archive.get('path', '')}",
        f"status: {data.get('status', '')}",
        f"archive sha256: {archive.get('sha256', '')}",
        f"config: {target.get('config_restore_path', '')} ({target.get('config_action', '-')})",
        f"data: {target.get('data_dir', '')}",
    ]
    if database.get("in_archive") is False:
        lines.append("database: not in the archive; the existing database is left untouched")
    else:
        lines.append(
            f"database: {int(database.get('size') or 0)} bytes, sha256 {database.get('sha256', '')}"
        )
    if database.get("restored"):
        lines.append(f"restored database: {database.get('path', '')}")
        lines.append(
            f"properties: {database.get('properties')}  "
            f"snapshots: {database.get('snapshots')}  "
            f"schema: {database.get('schema_version')}"
        )
    pre_database = database.get("pre_restore_path")
    if pre_database:
        lines.append(f"current database backup: {pre_database}")
    pre_config = config.get("pre_restore_path")
    if pre_config:
        lines.append(f"current config backup: {pre_config}")
    token = data.get("confirm_token")
    if token:
        lines.extend(
            [
                "",
                f"confirm token: {token}",
                "apply: rerun with --allow-write --confirm <token> within 5 minutes",
            ]
        )
    return lines


def _restore_to_lines(data: Mapping[str, Any]) -> list[str]:
    archive = data.get("archive") or {}
    target = data.get("target") or {}
    database = data.get("database") or {}
    config = data.get("config") or {}
    if not all(isinstance(item, Mapping) for item in (archive, target, database, config)):
        return []
    lines = [
        f"restore: {archive.get('path', '')}",
        "mode: new directory (existing files are not modified)",
        f"status: {data.get('status', '')}",
        f"archive sha256: {archive.get('sha256', '')}",
        f"directory: {target.get('directory', '')}",
        f"config: {config.get('path', '')} "
        f"({'valid' if config.get('valid') else 'does not validate'})",
        f"data: {target.get('data_dir', '')}",
    ]
    if database.get("in_archive") is False:
        lines.append("database: not in the archive; no database was created")
        lines.append("snapshots: unknown (no database in the archive)")
        return lines
    lines.append(
        f"database: {database.get('path', '')} "
        f"({int(database.get('size') or 0)} bytes, sha256 {database.get('sha256', '')})"
    )
    lines.append(f"quick_check: {database.get('quick_check')}")
    lines.append(
        f"properties: {database.get('properties')}  "
        f"snapshots: {database.get('snapshots')}  "
        f"schema: {database.get('schema_version')}"
    )
    return lines
