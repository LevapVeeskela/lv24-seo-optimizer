"""Portable, local-first workspace backup (F9).

The archive is a plain ``.zip`` that can be copied to another machine without a cloud
account:

``manifest.json``
    format/version, engine and schema versions, creation time, every payload file with
    its size and SHA-256, and a bounded summary (properties, report snapshots, warnings).
``seo.toml``
    the validated configuration, with ``storage.data_dir`` reset to ``./data`` so the
    bundle restores next to its own config. A config that does not validate is kept as a
    redacted raw copy instead of blocking the backup.
``data/seo.sqlite3``
    a consistent copy made through the SQLite backup API. Report snapshots live in the
    ``snapshots`` table, so they travel inside this file.

Guarantees
----------
* The source workspace is never modified: the config is only read and the database is
  opened with ``mode=ro`` and ``PRAGMA query_only``.
* Secrets are never read. ``secrets.json`` is not part of the archive, and every
  secret-looking key in the config is replaced by ``***``.
* The archive is written to a temporary file in the output directory and renamed
  atomically, so a failed run never leaves a half-written ``.zip``.
* Every payload member is hashed before it is written; a restore can verify the exact
  bytes recorded in the manifest.

Restore (``backup restore``)
----------------------------
:func:`plan_restore` validates an archive against a target config and returns a
:class:`RestorePlan` with a short-lived ``confirm_token``; it never writes to the target.
:func:`apply_restore` re-verifies every member, snapshots the current database to
``<db>.pre-restore-<utc>``, then atomically replaces the database and the config. Extraction
always happens in a temporary directory inside the target data directory, and only the exact
members named by the manifest are accepted (no absolute paths, ``..`` segments, symlinks or
extra files). The token is bound to the archive bytes and the target state, so a changed
archive, a changed workspace or a replay is refused with ``CONFIRM_REQUIRED``.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import tomllib
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, cast

from lv24_seo.core.config import (
    SeoConfig,
    data_dir_path,
    load_config,
    redacted_dump,
    resolve_config_path,
)
from lv24_seo.core.errors import (
    CONFIG_INVALID,
    CONFIRM_REQUIRED,
    INVALID_INPUT,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.core.timeutil import utc_now

__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_FORMAT_VERSION",
    "CONFIG_MEMBER",
    "DATABASE_FILENAME",
    "DATABASE_MEMBER",
    "MANIFEST_MEMBER",
    "MAX_CONFIG_BYTES",
    "MAX_DATABASE_BYTES",
    "MAX_MANIFEST_BYTES",
    "RESTORE_TOKEN_TTL_SECONDS",
    "BackupFile",
    "BackupManifest",
    "BackupResult",
    "RestorePlan",
    "RestoreResult",
    "apply_restore",
    "create_backup",
    "plan_restore",
    "redact_config_document",
]

BACKUP_FORMAT = "lv24-seo-backup"
BACKUP_FORMAT_VERSION = 1

MANIFEST_MEMBER = "manifest.json"
CONFIG_MEMBER = "seo.toml"
DATABASE_MEMBER = "data/seo.sqlite3"

#: Keep in sync with :data:`lv24_seo.composition.DB_FILENAME`; duplicated so ``core`` does
#: not import the composition root (and the store) just to name one file.
DATABASE_FILENAME = "seo.sqlite3"

ARCHIVE_PREFIX = "lv24-seo-backup"

#: A TOML config larger than this is not a real workspace config.
MAX_CONFIG_BYTES = 4 * 1024 * 1024
#: The database plus its WAL must fit; a larger source is archived manually instead.
MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
#: The manifest is a bounded JSON summary; anything larger is not a real manifest.
MAX_MANIFEST_BYTES = 1024 * 1024
#: Restore confirm tokens live as long as provider write tokens (ADR-0003).
RESTORE_TOKEN_TTL_SECONDS = 300

_RESTORE_SCOPE = "lv24-seo-restore"
#: An archive has exactly three members; a larger count is never produced by this engine.
_MAX_ARCHIVE_MEMBERS = 16
_ALLOWED_MEMBERS = frozenset({CONFIG_MEMBER, DATABASE_MEMBER})
_DATABASE_SIDECARS = ("-wal", "-shm")

_CHUNK = 1024 * 1024
_HEX_DIGITS = frozenset("0123456789abcdef")
_SECRET_KEY_RE = re.compile(
    r"token|secret|password|passwd|api[_-]?key|authorization|credential|private[_-]?key"
    r"|access[_-]?key",
    re.IGNORECASE,
)
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Mirrors lv24_seo.store.errors. The backup path must not call ``translate_sqlite_error``:
# that helper preserves a ``.corrupt-*`` copy next to the source, which would write to a
# workspace this command promises to leave untouched.
_LOCKED_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
)
_CORRUPT_MARKERS = (
    "file is not a database",
    "file is encrypted",
    "malformed",
    "not a database",
)

_COUNT_SQL: dict[str, str] = {
    "properties": "SELECT COUNT(*) FROM properties",
    "snapshots": "SELECT COUNT(*) FROM snapshots",
}


@dataclass(frozen=True, slots=True)
class BackupFile:
    """One payload member of the archive."""

    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """The machine-readable contract stored as ``manifest.json``."""

    engine_version: str
    schema_version: int | None
    engine_schema_version: int
    created_at: str
    files: tuple[BackupFile, ...]
    summary: Mapping[str, Any]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "engine_version": self.engine_version,
            "schema_version": self.schema_version,
            "engine_schema_version": self.engine_schema_version,
            "created_at": self.created_at,
            "files": [item.to_dict() for item in self.files],
            "totals": {
                "files": len(self.files),
                "size": sum(item.size for item in self.files),
            },
            "summary": dict(self.summary),
            "limitations": list(self.limitations),
        }

    def summary_dict(self) -> dict[str, Any]:
        """Flat summary for CLI output; the full manifest stays inside the archive."""
        return {
            "created_at": self.created_at,
            "engine_version": self.engine_version,
            "schema_version": self.schema_version,
            "engine_schema_version": self.engine_schema_version,
            "files": [item.path for item in self.files],
            "total_size": sum(item.size for item in self.files),
            **self.summary,
        }


@dataclass(frozen=True, slots=True)
class BackupResult:
    """A finished archive plus the manifest that describes it."""

    archive_path: Path
    size: int
    sha256: str
    manifest: BackupManifest
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_path": str(self.archive_path),
            "archive_size": self.size,
            "archive_sha256": self.sha256,
            "manifest": self.manifest.to_dict(),
            "summary": self.manifest.summary_dict(),
        }


@dataclass(frozen=True, slots=True)
class _ConfigSnapshot:
    member: bytes
    data_dir: Path
    source: str
    warnings: tuple[str, ...]


def redact_config_document(value: Any) -> Any:
    """Return a copy of *value* with secret-looking keys replaced by ``***``.

    The validated config model has no secret fields, so this is a second line of defence
    for raw configs that contain a stray token or password. The key name is kept so a
    restore can see that a value existed without learning it.
    """
    if isinstance(value, Mapping):
        return {
            key: "***"
            if isinstance(key, str) and _SECRET_KEY_RE.search(key)
            else redact_config_document(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_config_document(item) for item in value]
    return value


def create_backup(
    *,
    out_dir: Path,
    config_path: Path | None = None,
    force: bool = False,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    max_config_bytes: int = MAX_CONFIG_BYTES,
    max_database_bytes: int = MAX_DATABASE_BYTES,
) -> BackupResult:
    """Build a portable archive in *out_dir* and return its path, size and manifest.

    ``out_dir`` must be an absolute, existing directory. ``cwd`` emulates the process
    working directory for config discovery and a relative default data directory; ``env``
    is injectable for hermetic tests. ``now`` fixes the timestamp (and therefore the
    archive name) in tests.
    """
    destination = _validate_out_dir(out_dir)
    created = _utc_moment(now)
    created_at = created.isoformat(timespec="seconds")
    archive_path = destination / _archive_name(created)
    _guard_destination(archive_path, force)

    resolved = resolve_config_path(config_path, cwd)
    if resolved is not None:
        _guard_config_size(resolved, max_config_bytes)
    snapshot = _config_snapshot(resolved, cwd=cwd, env=env)
    database_path = snapshot.data_dir / DATABASE_FILENAME
    database_present = database_path.is_file()
    if database_path.exists() and not database_present:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the database path is not a file: {database_path}",
            next_action="move the directory away or point [storage].data_dir at the data directory",
        )
    if resolved is None and not database_present:
        raise SeoError(
            code=INVALID_INPUT,
            message="nothing to back up: no seo.toml and no database were found",
            next_action="run `lv24-seo init` first, or pass --config to an existing workspace",
        )

    warnings = list(snapshot.warnings)
    if not database_present:
        warnings.append("the database was not found; the archive contains configuration only")

    files = [
        BackupFile(
            path=CONFIG_MEMBER,
            size=len(snapshot.member),
            sha256=_sha256_bytes(snapshot.member),
        )
    ]
    summary: dict[str, Any] = {
        "config_source": snapshot.source,
        "database_present": database_present,
        "secrets_included": False,
        "schema_version": None,
        "properties": None,
        "snapshots": None,
        "latest_snapshot_at": None,
    }

    with tempfile.TemporaryDirectory(prefix="lv24-seo-backup-") as temp_dir:
        database_copy: Path | None = None
        if database_present:
            _guard_database_size(database_path, max_database_bytes)
            database_copy = Path(temp_dir) / DATABASE_FILENAME
            _copy_database(database_path, database_copy)
            copied_size = database_copy.stat().st_size
            if copied_size > max_database_bytes:
                raise SeoError(
                    code=INVALID_INPUT,
                    message=(
                        "the database copy is too large to back up "
                        f"({copied_size} bytes; limit {max_database_bytes})"
                    ),
                    next_action=(
                        "archive the data directory manually, or move old data out of the workspace"
                    ),
                    detail={
                        "path": str(database_path),
                        "size": copied_size,
                        "limit": max_database_bytes,
                    },
                )
            files.append(
                BackupFile(
                    path=DATABASE_MEMBER,
                    size=copied_size,
                    sha256=_sha256_file(database_copy),
                )
            )
            summary.update(_read_database_summary(database_copy))

        files.sort(key=lambda item: item.path)
        engine_schema = _engine_schema_version()
        schema_version = cast(int | None, summary.get("schema_version"))
        if database_present:
            _append_schema_warnings(warnings, schema_version, engine_schema)

        manifest = BackupManifest(
            engine_version=_engine_version(),
            schema_version=schema_version,
            engine_schema_version=engine_schema,
            created_at=created_at,
            files=tuple(files),
            summary=summary,
            limitations=_limitations(
                warnings,
                database_present=database_present,
                snapshots=cast(int | None, summary.get("snapshots")),
            ),
        )
        size, digest = _write_archive(
            archive_path=archive_path,
            manifest_bytes=_manifest_bytes(manifest),
            config_bytes=snapshot.member,
            database_path=database_copy,
            created=created,
            force=force,
        )

    return BackupResult(
        archive_path=archive_path,
        size=size,
        sha256=digest,
        manifest=manifest,
        warnings=tuple(warnings),
    )


def _config_snapshot(
    resolved: Path | None,
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> _ConfigSnapshot:
    if resolved is None:
        config = load_config(None, env=env, cwd=cwd)
        return _ConfigSnapshot(
            member=_config_bytes_from_model(config),
            data_dir=_resolve_data_dir(config, cwd),
            source="defaults",
            warnings=("no config file was found; the archive contains built-in defaults",),
        )

    try:
        config = load_config(resolved, env=env)
    except SeoError as exc:
        if exc.code != CONFIG_INVALID:
            raise
        raw = _read_raw_document(resolved)
        redacted = cast(Mapping[str, Any], redact_config_document(raw))
        return _ConfigSnapshot(
            member=_config_bytes_from_document(redacted),
            data_dir=_raw_data_dir(raw, resolved, cwd=cwd, env=env),
            source="raw",
            warnings=(
                "seo.toml is not valid for this engine; the archive contains a redacted "
                "raw copy. Fix seo.toml and retry for a canonical config.",
            ),
        )

    return _ConfigSnapshot(
        member=_config_bytes_from_model(config),
        data_dir=_resolve_data_dir(config, cwd),
        source="file",
        warnings=(),
    )


def _config_bytes_from_model(config: SeoConfig) -> bytes:
    document = cast(Mapping[str, Any], redact_config_document(redacted_dump(config)))
    return _config_bytes_from_document(document)


def _config_bytes_from_document(document: Mapping[str, Any]) -> bytes:
    normalized = dict(document)
    storage = normalized.get("storage")
    storage_copy = dict(storage) if isinstance(storage, Mapping) else {}
    storage_copy["data_dir"] = "./data"
    normalized["storage"] = storage_copy
    try:
        return _toml_document(normalized).encode("utf-8")
    except TypeError:
        raise SeoError(
            code=CONFIG_INVALID,
            message="the config contains a value this backup cannot serialise",
            next_action="fix the config or remove the unsupported table, then retry",
        ) from None


def _read_raw_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise SeoError(
            code=CONFIG_INVALID,
            message=f"cannot read the config file at {path}",
            next_action="check the path and file permissions",
        ) from None
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SeoError(
            code=CONFIG_INVALID,
            message=f"the config file at {path} is not valid TOML: {exc}",
            next_action="fix the TOML syntax and try again",
        ) from None
    return parsed


def _resolve_data_dir(config: SeoConfig, cwd: Path | None) -> Path:
    data_dir = data_dir_path(config)
    if data_dir.is_absolute():
        return data_dir
    base = Path.cwd() if cwd is None else Path(cwd)
    return Path(os.path.abspath(base / data_dir))


def _raw_data_dir(
    document: Mapping[str, Any],
    config_path: Path,
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> Path:
    environment = os.environ if env is None else env
    base = Path.cwd() if cwd is None else Path(cwd)
    override = environment.get("LV24_SEO_DATA_DIR")
    if override is not None and override.strip():
        candidate = Path(override.strip()).expanduser()
        return candidate if candidate.is_absolute() else Path(os.path.abspath(base / candidate))
    storage = document.get("storage")
    if isinstance(storage, Mapping):
        raw = storage.get("data_dir")
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw.strip()).expanduser()
            if candidate.is_absolute():
                return candidate
            return Path(os.path.abspath(config_path.parent / candidate))
    return Path(os.path.abspath(base / "data"))


def _toml_document(document: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _emit_toml_table(lines, (), document)
    return "\n".join(lines).rstrip() + "\n"


def _emit_toml_table(
    lines: list[str],
    prefix: tuple[str, ...],
    table: Mapping[str, Any],
) -> None:
    scalars: list[tuple[str, Any]] = []
    children: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in table.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            children.append((key, cast(Mapping[str, Any], value)))
        else:
            scalars.append((key, value))

    if prefix and scalars:
        lines.append(f"[{'.'.join(_toml_key(part) for part in prefix)}]")
    for key, value in scalars:
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, child in children:
        _emit_toml_table(lines, (*prefix, key), child)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def _validate_out_dir(out_dir: Path) -> Path:
    candidate = Path(out_dir).expanduser()
    if not candidate.is_absolute():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the output directory must be absolute: {out_dir}",
            next_action=(
                "pass an absolute path (a leading / or a drive letter), not one relative "
                "to the working directory"
            ),
        )
    if not candidate.exists():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the output directory does not exist: {candidate}",
            next_action="create the directory first, then retry",
        )
    if not candidate.is_dir():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the output path is not a directory: {candidate}",
            next_action="pass a directory to --out",
        )
    return candidate


def _guard_destination(path: Path, force: bool) -> None:
    if not path.exists():
        return
    if path.is_dir():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the archive path is a directory: {path}",
            next_action="remove the directory or choose another --out",
        )
    if not force:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the archive already exists: {path}",
            next_action="pass --force to replace it, or wait a second for a new name",
        )


def _guard_config_size(path: Path, limit: int) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        raise SeoError(
            code=CONFIG_INVALID,
            message=f"cannot read the config file at {path}",
            next_action="check the path and file permissions",
        ) from None
    if size > limit:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the config file is too large to back up ({size} bytes; limit {limit})",
            next_action="reduce seo.toml to the settings you need, then retry",
            detail={"path": str(path), "size": size, "limit": limit},
        )


def _guard_database_size(path: Path, limit: int) -> None:
    try:
        size = _database_source_bytes(path)
    except OSError:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"cannot read the database at {path}",
            next_action="check the file permissions and free space",
        ) from None
    if size > limit:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"the database is too large to back up ({size} bytes; limit {limit})",
            next_action=(
                "archive the data directory manually, or move old data out of the workspace"
            ),
            detail={"path": str(path), "size": size, "limit": limit},
        )


def _database_source_bytes(path: Path) -> int:
    total = path.stat().st_size
    wal = path.with_name(path.name + "-wal")
    with contextlib.suppress(OSError):
        if wal.is_file():
            total += wal.stat().st_size
    return total


def _copy_database(source_path: Path, dest_path: Path) -> None:
    source: sqlite3.Connection | None = None
    dest: sqlite3.Connection | None = None
    try:
        source = _connect_readonly(source_path)
        dest = sqlite3.connect(str(dest_path), timeout=30.0)
        source.backup(dest, pages=1024, sleep=0.05)
        # The backup copies page 1, including the source journal mode. Force a single-file
        # DELETE journal so the restored database never depends on missing -wal/-shm files.
        dest.execute("PRAGMA journal_mode = DELETE")
        dest.commit()
        _check_copy(dest, source_path)
    except sqlite3.Error as exc:
        raise _storage_error(exc, source_path) from exc
    finally:
        if dest is not None:
            with contextlib.suppress(sqlite3.Error):
                dest.close()
        if source is not None:
            with contextlib.suppress(sqlite3.Error):
                source.close()
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                dest_path.with_name(dest_path.name + suffix).unlink(missing_ok=True)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        uri = f"{Path(os.path.abspath(path)).as_uri()}?mode=ro"
    except ValueError:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"cannot open the database at {path}",
            next_action="move the workspace to a local path and retry",
        ) from None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise _storage_error(exc, path) from exc
    return conn


def _check_copy(conn: sqlite3.Connection, source_path: Path) -> None:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if not rows or any(str(row[0]).lower() != "ok" for row in rows):
        raise _corrupt_source_error(source_path)


def _read_database_summary(path: Path) -> dict[str, Any]:
    conn = _connect_readonly(path)
    try:
        return {
            "schema_version": _schema_version(conn),
            "properties": _table_count(conn, "properties"),
            "snapshots": _table_count(conn, "snapshots"),
            "latest_snapshot_at": _latest_snapshot_at(conn),
        }
    except sqlite3.Error as exc:
        raise _storage_error(exc, path) from exc
    finally:
        conn.close()


def _schema_version(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, "schema_migrations"):
        return None
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    row = conn.execute(_COUNT_SQL[table]).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _latest_snapshot_at(conn: sqlite3.Connection) -> str | None:
    if not _table_exists(conn, "snapshots"):
        return None
    row = conn.execute("SELECT MAX(created_at) FROM snapshots").fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _storage_error(exc: sqlite3.Error, path: Path) -> SeoError:
    text = str(exc).lower()
    if any(marker in text for marker in _LOCKED_MARKERS):
        from lv24_seo.store.errors import locked_error

        return locked_error(path)
    if any(marker in text for marker in _CORRUPT_MARKERS):
        return _corrupt_source_error(path)
    from lv24_seo.store.errors import unavailable_error

    return unavailable_error(path, reason=str(exc).strip()[:200])


def _corrupt_source_error(path: Path) -> SeoError:
    """A corrupt source is reported without the store's "delete and rerun" advice.

    The store helper is right for a normal command that can rebuild an empty database;
    here the user asked for a backup, so the source must be preserved and inspected.
    """
    from lv24_seo.store.errors import STORE_CORRUPT

    return SeoError(
        code=STORE_CORRUPT,
        message=f"the database at {path} is not a valid SQLite file",
        next_action=(
            "inspect or restore the file from another copy; the backup was not written "
            "and the source was left untouched"
        ),
        retryable=False,
        detail={"path": str(path)},
    )


def _append_schema_warnings(
    warnings: list[str],
    schema_version: int | None,
    engine_schema: int,
) -> None:
    if schema_version is None:
        warnings.append("the database has no schema ledger; it will be initialised on first open")
    elif schema_version < engine_schema:
        warnings.append(
            f"the database schema is v{schema_version}; the engine schema is v{engine_schema} "
            "(restore migrates it)"
        )
    elif schema_version > engine_schema:
        warnings.append(
            f"the database schema is v{schema_version}, newer than this engine's v{engine_schema}; "
            "restore with a newer engine"
        )


def _limitations(
    warnings: Sequence[str],
    *,
    database_present: bool,
    snapshots: int | None,
) -> tuple[str, ...]:
    items = [
        "secrets.json, logs, raw provider responses and caches are not included",
        "provider tokens are not included; re-authenticate after restore with `lv24-seo auth set`",
        "absolute paths in provider settings are copied as-is; edit them after restore if needed",
        "contact settings (email recipients, SMTP user, Telegram chat id) are included; "
        "review before sharing",
    ]
    items.extend(warnings)
    if database_present and snapshots == 0:
        items.append("no report snapshots were present in the database")
    return tuple(items)


def _write_archive(
    *,
    archive_path: Path,
    manifest_bytes: bytes,
    config_bytes: bytes,
    database_path: Path | None,
    created: datetime,
    force: bool,
) -> tuple[int, str]:
    try:
        handle_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{archive_path.name}.",
            suffix=".tmp",
            dir=str(archive_path.parent),
        )
        os.close(handle_fd)
        tmp_path = Path(tmp_name)
    except OSError:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"cannot write to the output directory: {archive_path.parent}",
            next_action="pass a writable absolute --out directory",
        ) from None

    try:
        with zipfile.ZipFile(
            tmp_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr(_zip_info(MANIFEST_MEMBER, created), manifest_bytes)
            archive.writestr(_zip_info(CONFIG_MEMBER, created), config_bytes)
            if database_path is not None:
                info = _zip_info(DATABASE_MEMBER, created)
                with archive.open(info, "w") as target, database_path.open("rb") as source:
                    shutil.copyfileobj(source, target, length=_CHUNK)
        if os.name == "posix":
            os.chmod(tmp_path, 0o600)
        size = tmp_path.stat().st_size
        digest = _sha256_file(tmp_path)
        if archive_path.exists() and not force:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"the archive already exists: {archive_path}",
                next_action="pass --force to replace it, or wait a second for a new name",
            )
        os.replace(tmp_path, archive_path)
    except OSError:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"cannot write the backup archive to {archive_path.parent}",
            next_action="check free space and directory permissions, then retry",
        ) from None
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
    return size, digest


def _zip_info(name: str, created: datetime) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(
        name,
        date_time=(
            created.year,
            created.month,
            created.day,
            created.hour,
            created.minute,
            created.second,
        ),
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def _manifest_bytes(manifest: BackupManifest) -> bytes:
    text = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return (text + "\n").encode("utf-8")


def _archive_name(created: datetime) -> str:
    return f"{ARCHIVE_PREFIX}-{created.strftime('%Y%m%dT%H%M%SZ')}.zip"


def _utc_moment(now: datetime | None) -> datetime:
    moment = utc_now() if now is None else now
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).replace(microsecond=0)


def _engine_version() -> str:
    try:
        return package_version("lv24-seo")
    except PackageNotFoundError:
        from lv24_seo import __version__

        return __version__


def _engine_schema_version() -> int:
    from lv24_seo.store.db import SCHEMA_VERSION

    return SCHEMA_VERSION


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# -- restore ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """A validated restore, ready to apply with its ``confirm_token``.

    Every path is absolute. ``config_action`` is ``"create"`` (no target config existed),
    ``"replace"`` (a valid archived config replaces the target, which is copied first) or
    ``"write_alongside"`` (the archived config is a redacted raw copy that does not validate,
    so the existing config is preserved and the archive copy lands next to it).
    """

    archive_path: Path
    archive_size: int
    archive_sha256: str
    manifest_sha256: str
    manifest: BackupManifest
    config_path: Path
    data_dir: Path
    database_path: Path
    database_member: bool
    database_size: int | None
    database_sha256: str | None
    existing_database: bool
    existing_database_sha256: str | None
    config_valid: bool
    config_sha256: str
    config_action: str
    config_restore_path: Path
    existing_config: bool
    existing_config_sha256: str | None
    pre_restore_database: Path | None
    pre_restore_config: Path | None
    plan_hash: str
    confirm_token: str
    expires_at: str
    ttl_seconds: int
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe rendering consumed by the CLI envelope and the desktop app."""
        return {
            "status": "planned",
            "archive": {
                "path": str(self.archive_path),
                "size": self.archive_size,
                "sha256": self.archive_sha256,
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "engine_version": self.manifest.engine_version,
                "created_at": self.manifest.created_at,
            },
            "target": {
                "config_path": str(self.config_path),
                "data_dir": str(self.data_dir),
                "database_path": str(self.database_path),
                "config_action": self.config_action,
                "config_restore_path": str(self.config_restore_path),
            },
            "database": {
                "path": str(self.database_path),
                "in_archive": self.database_member,
                "size": self.database_size,
                "sha256": self.database_sha256,
                "existing": self.existing_database,
                "existing_sha256": self.existing_database_sha256,
                "pre_restore_path": _optional_str(self.pre_restore_database),
            },
            "config": {
                "valid": self.config_valid,
                "sha256": self.config_sha256,
                "existing": self.existing_config,
                "existing_sha256": self.existing_config_sha256,
                "pre_restore_path": _optional_str(self.pre_restore_config),
            },
            "plan_hash": self.plan_hash,
            "confirm_token": self.confirm_token,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "files": [item.to_dict() for item in self.manifest.files],
            "summary": self.manifest.summary_dict(),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """An applied restore plus the post-restore database counts and hashes."""

    status: str
    plan: RestorePlan
    database_restored: bool
    config_restored: bool
    database_sha256: str | None
    config_sha256: str
    pre_restore_database: Path | None
    pre_restore_config: Path | None
    properties: int | None
    snapshots: int | None
    schema_version: int | None
    latest_snapshot_at: str | None
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "archive_sha256": self.plan.archive_sha256,
            "target": {
                "config_path": str(self.plan.config_path),
                "data_dir": str(self.plan.data_dir),
                "database_path": str(self.plan.database_path),
                "config_action": self.plan.config_action,
                "config_restore_path": str(self.plan.config_restore_path),
            },
            "database": {
                "path": str(self.plan.database_path),
                "restored": self.database_restored,
                "sha256": self.database_sha256,
                "pre_restore_path": _optional_str(self.pre_restore_database),
                "properties": self.properties,
                "snapshots": self.snapshots,
                "schema_version": self.schema_version,
                "latest_snapshot_at": self.latest_snapshot_at,
            },
            "config": {
                "path": str(self.plan.config_restore_path),
                "restored": self.config_restored,
                "sha256": self.config_sha256,
                "valid": self.plan.config_valid,
                "pre_restore_path": _optional_str(self.pre_restore_config),
            },
            "plan_hash": self.plan.plan_hash,
            "limitations": list(self.plan.limitations),
        }


@dataclass(frozen=True, slots=True)
class _ArchiveContents:
    manifest: BackupManifest
    manifest_bytes: bytes
    config_bytes: bytes
    sha256: str
    size: int


def plan_restore(
    *,
    archive_path: Path,
    config_path: Path,
    now: datetime | None = None,
    ttl_seconds: int = RESTORE_TOKEN_TTL_SECONDS,
    max_config_bytes: int = MAX_CONFIG_BYTES,
    max_database_bytes: int = MAX_DATABASE_BYTES,
) -> RestorePlan:
    """Validate *archive_path* for *config_path* and return a plan plus a confirm token.

    Nothing outside a temporary directory is written. The archive must be an absolute path to
    a regular file; the config must be an absolute path whose directory exists. The database is
    restored to ``<config dir>/data/seo.sqlite3``, the only location a backup config describes.
    """
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise _invalid_input(
            "confirm token TTL must be a positive number of seconds",
            "use the default 300 seconds",
        )
    archive = _validate_archive_path(archive_path)
    target = _validate_config_target(config_path)
    moment = _utc_moment(now)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    contents = _read_archive(
        archive,
        max_config_bytes=max_config_bytes,
        max_database_bytes=max_database_bytes,
    )
    schema_warnings = _check_manifest_schema(contents.manifest)
    config_valid, config_warnings = _validate_restored_config(contents.config_bytes)
    data_dir = _restore_data_dir(contents.config_bytes, target)
    _guard_data_dir(data_dir)
    database_path = data_dir / DATABASE_FILENAME
    _guard_target_database(database_path)

    database_member = _member(contents.manifest, DATABASE_MEMBER)
    existing_database = database_path.exists()
    existing_database_sha256 = _file_sha256_or_none(database_path)
    existing_config = target.exists()
    existing_config_sha256 = _file_sha256_or_none(target)

    config_action, config_restore_path, pre_restore_config = _config_restore_plan(
        target,
        existing_config=existing_config,
        config_valid=config_valid,
        stamp=stamp,
    )
    pre_restore_database = (
        _unique_path(database_path.with_name(f"{database_path.name}.pre-restore-{stamp}"))
        if existing_database and database_member is not None
        else None
    )

    warnings = list(config_warnings)
    warnings.extend(schema_warnings)
    if database_member is None:
        warnings.append("the archive contains no database; the existing database is left untouched")
    elif existing_database and pre_restore_database is not None:
        warnings.append(
            f"the current database will be copied to {pre_restore_database} before it is replaced"
        )
    if config_action == "replace" and pre_restore_config is not None:
        warnings.append(
            f"the current config will be copied to {pre_restore_config} before it is replaced"
        )
    if config_action == "write_alongside":
        warnings.append(
            f"the archived config does not validate; it will be written to {config_restore_path} "
            "and the existing config will be left untouched"
        )
    if contents.manifest.summary.get("config_source") == "raw":
        warnings.append("the archived config is a redacted raw copy; re-check it before use")

    limitations = (
        "restore replaces files; it never merges rows or settings",
        "provider tokens are not included; re-authenticate with `lv24-seo auth set` after restore",
        "stop other lv24-seo or desktop processes before applying; a database held open "
        "cannot be replaced on Windows",
        "the confirm token is bound to the archive bytes and the target state, expires in "
        "5 minutes and cannot be replayed",
        "the confirm token prevents accidental or stale applies; it is not access control — "
        "anyone who can run the CLI can restore",
    )

    manifest_sha256 = _sha256_bytes(contents.manifest_bytes)
    document: dict[str, Any] = {
        "archive": {
            "path": str(archive),
            "size": contents.size,
            "sha256": contents.sha256,
            "manifest_sha256": manifest_sha256,
        },
        "target": {
            "config_path": str(target),
            "data_dir": str(data_dir),
            "database_path": str(database_path),
        },
        "database": {
            "in_archive": database_member is not None,
            "size": None if database_member is None else database_member.size,
            "sha256": None if database_member is None else database_member.sha256,
            "existing": existing_database,
            "existing_sha256": existing_database_sha256,
            "pre_restore": _optional_str(pre_restore_database),
        },
        "config": {
            "valid": config_valid,
            "action": config_action,
            "restore_path": str(config_restore_path),
            "existing": existing_config,
            "existing_sha256": existing_config_sha256,
            "pre_restore": _optional_str(pre_restore_config),
        },
        "files": [item.to_dict() for item in contents.manifest.files],
    }
    plan_hash = _sha256_bytes(_canonical_json(document).encode("utf-8"))
    expiry = int(moment.timestamp()) + ttl_seconds
    token = _restore_token(plan_hash, contents.sha256, expiry)

    return RestorePlan(
        archive_path=archive,
        archive_size=contents.size,
        archive_sha256=contents.sha256,
        manifest_sha256=manifest_sha256,
        manifest=contents.manifest,
        config_path=target,
        data_dir=data_dir,
        database_path=database_path,
        database_member=database_member is not None,
        database_size=None if database_member is None else database_member.size,
        database_sha256=None if database_member is None else database_member.sha256,
        existing_database=existing_database,
        existing_database_sha256=existing_database_sha256,
        config_valid=config_valid,
        config_sha256=_sha256_bytes(contents.config_bytes),
        config_action=config_action,
        config_restore_path=config_restore_path,
        existing_config=existing_config,
        existing_config_sha256=existing_config_sha256,
        pre_restore_database=pre_restore_database,
        pre_restore_config=pre_restore_config,
        plan_hash=plan_hash,
        confirm_token=token,
        expires_at=datetime.fromtimestamp(expiry, UTC).isoformat(timespec="seconds"),
        ttl_seconds=ttl_seconds,
        warnings=tuple(warnings),
        limitations=limitations,
    )


def apply_restore(
    plan: RestorePlan,
    confirm_token: str,
    *,
    allow_write: bool,
    now: datetime | None = None,
    max_config_bytes: int = MAX_CONFIG_BYTES,
    max_database_bytes: int = MAX_DATABASE_BYTES,
) -> RestoreResult:
    """Apply a :class:`RestorePlan` after re-verifying its token, archive and pre-state.

    ``allow_write=False`` refuses with ``WRITE_DISABLED``; a missing, expired, forged or
    replayed token refuses with ``CONFIRM_REQUIRED``. The archive is extracted into a temporary
    directory inside the target data directory, every member hash is verified again, the
    database passes ``PRAGMA quick_check``, and only then are the files replaced atomically.
    """
    if not allow_write:
        raise SeoError(
            code=WRITE_DISABLED,
            message="restore replaces the database and the config, so it is disabled by default",
            next_action="review the plan, then rerun with --allow-write --confirm <token>",
            retryable=False,
        )
    _verify_restore_token(plan, confirm_token, now=now)
    _recheck_pre_state(plan)
    data_dir = _ensure_data_dir(plan.data_dir)
    warnings = list(plan.warnings)
    temp_root = _make_temp_root(data_dir)
    staged_config: Path | None = None
    try:
        manifest, extracted, manifest_sha256 = _extract_archive(
            plan.archive_path,
            temp_root,
            max_config_bytes=max_config_bytes,
            max_database_bytes=max_database_bytes,
        )
        if not hmac.compare_digest(manifest_sha256, plan.manifest_sha256):
            raise _confirm_required("the archive manifest changed after the plan was made")
        config_bytes = extracted[CONFIG_MEMBER].read_bytes()
        database_temp = extracted.get(DATABASE_MEMBER)
        summary: Mapping[str, Any] | None = None
        if database_temp is not None:
            summary = _verify_archive_database(database_temp, manifest)

        staged_config = _stage_config(plan.config_restore_path, config_bytes)
        if plan.existing_database and plan.pre_restore_database is not None:
            _backup_current_database(plan.database_path, plan.pre_restore_database, warnings)
        if (
            plan.config_action == "replace"
            and plan.existing_config
            and plan.pre_restore_config is not None
        ):
            _copy_file(plan.config_path, plan.pre_restore_config, what="current config")

        database_restored = False
        if database_temp is not None:
            _remove_sidecars(plan.database_path)
            _replace_file(database_temp, plan.database_path)
            database_restored = True
        _replace_file(staged_config, plan.config_restore_path)
        staged_config = None

        if database_restored:
            summary = _read_database_summary(plan.database_path)
        counts = summary or {}
        return RestoreResult(
            status="applied",
            plan=plan,
            database_restored=database_restored,
            config_restored=True,
            database_sha256=plan.database_sha256 if database_restored else None,
            config_sha256=_sha256_bytes(config_bytes),
            pre_restore_database=plan.pre_restore_database if database_restored else None,
            pre_restore_config=(
                plan.pre_restore_config if plan.config_action == "replace" else None
            ),
            properties=_optional_int(counts.get("properties")),
            snapshots=_optional_int(counts.get("snapshots")),
            schema_version=_optional_int(counts.get("schema_version")),
            latest_snapshot_at=_optional_str_value(counts.get("latest_snapshot_at")),
            warnings=tuple(warnings),
        )
    finally:
        if staged_config is not None:
            with contextlib.suppress(OSError):
                staged_config.unlink(missing_ok=True)
        shutil.rmtree(temp_root, ignore_errors=True)


def _validate_archive_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise _invalid_input(
            f"the archive path must be absolute: {path}",
            "pass an absolute path to --file",
        )
    try:
        info = candidate.stat()
    except OSError:
        raise _invalid_input(
            f"the archive does not exist: {candidate}",
            "check the path and pass the .zip created by `lv24-seo backup create`",
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise _invalid_input(
            f"the archive path is not a regular file: {candidate}",
            "pass the .zip file itself, not a directory or a device",
        )
    return candidate


def _validate_config_target(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise _invalid_input(
            f"the config path must be absolute: {path}",
            "pass an absolute path to --config",
        )
    parent = candidate.parent
    if not parent.exists():
        raise _invalid_input(
            f"the config directory does not exist: {parent}",
            "create the workspace directory first, then retry",
        )
    if not parent.is_dir():
        raise _invalid_input(
            f"the config directory is not a directory: {parent}",
            "pass a config path inside a directory",
        )
    if candidate.is_symlink():
        raise _invalid_input(
            f"the config path is a symlink: {candidate}",
            "pass the real config file path so restore replaces the file, not the link",
        )
    if candidate.exists():
        try:
            info = candidate.stat()
        except OSError:
            raise _invalid_input(
                f"cannot read the config at {candidate}",
                "check the file permissions",
            ) from None
        if not stat.S_ISREG(info.st_mode):
            raise _invalid_input(
                f"the config path is not a regular file: {candidate}",
                "pass a path to seo.toml",
            )
    return candidate


def _guard_data_dir(data_dir: Path) -> None:
    if data_dir.exists() and not data_dir.is_dir():
        raise _invalid_input(
            f"the data directory path is not a directory: {data_dir}",
            "move the file away or restore to a different --config",
        )


def _guard_target_database(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise _invalid_input(
            f"the database path is a symlink: {path}",
            "replace the link with the real database file, then retry",
        )
    if not path.is_file():
        raise _invalid_input(
            f"the database path is not a regular file: {path}",
            "move the directory away or restore to a different --config",
        )


def _restore_data_dir(config_bytes: bytes, config_path: Path) -> Path:
    """The database location described by the archived config: ``<config dir>/data``.

    ``create_backup`` always writes ``data_dir = "./data"``. An archive whose config points
    somewhere else was edited by hand, and restore refuses to write outside the workspace
    directory that ``--config`` names.
    """
    expected = Path(os.path.abspath(config_path.parent / "data"))
    try:
        document = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return expected
    storage = document.get("storage")
    raw = storage.get("data_dir") if isinstance(storage, Mapping) else None
    if not isinstance(raw, str) or not raw.strip():
        return expected
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    candidate = Path(os.path.abspath(candidate))
    if os.path.normcase(str(candidate)) != os.path.normcase(str(expected)):
        raise SeoError(
            code=INVALID_INPUT,
            message=(
                "the archived config points its data directory outside the restore target: "
                f"{candidate}"
            ),
            next_action=(
                "restore next to a config whose [storage].data_dir is './data'; this command "
                "only writes the database to <config dir>/data"
            ),
            detail={"data_dir": str(candidate), "expected": str(expected)},
        )
    return expected


def _validate_restored_config(config_bytes: bytes) -> tuple[bool, tuple[str, ...]]:
    """Validate the archived config with the real loader, without env overrides."""
    try:
        with tempfile.TemporaryDirectory(prefix="lv24-seo-restore-config-") as temp_dir:
            path = Path(temp_dir) / CONFIG_MEMBER
            try:
                path.write_bytes(config_bytes)
            except OSError:
                raise _invalid_input(
                    "cannot stage the archived config for validation",
                    "check free space in the temporary directory",
                ) from None
            try:
                load_config(path, env={})
            except SeoError as exc:
                if exc.code != CONFIG_INVALID:
                    raise
                return False, (
                    f"the archived config does not validate for this engine: {exc.message}",
                )
    except OSError:
        raise _invalid_input(
            "cannot create a temporary directory to validate the archived config",
            "check free space in the temporary directory",
        ) from None
    return True, ()


def _config_restore_plan(
    target: Path,
    *,
    existing_config: bool,
    config_valid: bool,
    stamp: str,
) -> tuple[str, Path, Path | None]:
    """Decide where the config goes and which file (if any) is copied before replacement."""
    if config_valid:
        action = "replace" if existing_config else "create"
        pre_restore = (
            _unique_path(target.with_name(f"{target.name}.pre-restore-{stamp}"))
            if existing_config
            else None
        )
        return action, target, pre_restore
    if existing_config:
        alongside = _unique_path(target.with_name(f"{target.name}.restored-{stamp}"))
        return "write_alongside", alongside, None
    return "create", target, None


def _check_manifest_schema(manifest: BackupManifest) -> tuple[str, ...]:
    engine_schema = _engine_schema_version()
    if manifest.schema_version is not None and manifest.schema_version > engine_schema:
        raise SeoError(
            code=INVALID_INPUT,
            message=(
                f"the archive database schema is v{manifest.schema_version}, newer than this "
                f"engine's v{engine_schema}"
            ),
            next_action="upgrade lv24-seo (and the desktop app) before restoring this archive",
            detail={
                "schema_version": manifest.schema_version,
                "engine_schema_version": engine_schema,
            },
        )
    if manifest.schema_version is None:
        if _member(manifest, DATABASE_MEMBER) is None:
            return ()
        return ("the archived database has no schema ledger; it will be initialised on first open",)
    if manifest.schema_version < engine_schema:
        return (
            f"the archived database schema is v{manifest.schema_version}; it will be migrated to "
            f"v{engine_schema} on first open",
        )
    return ()


def _member(manifest: BackupManifest, name: str) -> BackupFile | None:
    for item in manifest.files:
        if item.path == name:
            return item
    return None


def _read_archive(
    path: Path,
    *,
    max_config_bytes: int,
    max_database_bytes: int,
) -> _ArchiveContents:
    try:
        size = path.stat().st_size
        digest = _sha256_file(path)
    except OSError:
        raise _invalid_input(
            f"cannot read the archive at {path}",
            "check the path and the file permissions",
        ) from None
    try:
        with zipfile.ZipFile(path) as archive:
            members = _archive_members(archive)
            manifest_info = members.get(MANIFEST_MEMBER)
            if manifest_info is None:
                raise _invalid_input(
                    f"the archive has no {MANIFEST_MEMBER}",
                    "use an archive created by `lv24-seo backup create`",
                )
            manifest_bytes = _read_member(archive, manifest_info, MAX_MANIFEST_BYTES)
            manifest = _parse_manifest(manifest_bytes)
            expected = {item.path: item for item in manifest.files}
            payload_names = set(members) - {MANIFEST_MEMBER}
            if payload_names != set(expected):
                raise _invalid_input(
                    "the archive members do not match the manifest",
                    "use an archive created by `lv24-seo backup create`",
                    detail={
                        "extra": sorted(payload_names - set(expected)),
                        "missing": sorted(set(expected) - payload_names),
                    },
                )
            config_bytes = b""
            for member in manifest.files:
                limit = max_config_bytes if member.path == CONFIG_MEMBER else max_database_bytes
                if member.size > limit:
                    raise _invalid_input(
                        f"the archive member {member.path} is too large to restore "
                        f"({member.size} bytes; limit {limit})",
                        "create a smaller backup, or archive the data directory manually",
                        detail={"member": member.path, "size": member.size, "limit": limit},
                    )
                info = members[member.path]
                if member.path == CONFIG_MEMBER:
                    config_bytes = _read_member(archive, info, limit)
                    _verify_bytes(config_bytes, member)
                else:
                    _verify_stream(archive, info, member)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError) as exc:
        raise _invalid_input(
            f"the archive at {path} is damaged or uses unsupported compression: {exc}",
            "create a new backup with `lv24-seo backup create`",
        ) from None
    except OSError:
        raise _invalid_input(
            f"cannot read the archive at {path}",
            "check the path and the file permissions",
        ) from None
    return _ArchiveContents(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        config_bytes=config_bytes,
        sha256=digest,
        size=size,
    )


def _archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise _invalid_input(
            f"the archive has too many members ({len(infos)}; limit {_MAX_ARCHIVE_MEMBERS})",
            "use an archive created by `lv24-seo backup create`",
        )
    members: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        _validate_member_name(info.filename)
        _validate_member_type(info)
        if info.filename in members:
            raise _invalid_input(
                f"the archive contains {info.filename!r} twice",
                "use an archive created by `lv24-seo backup create`",
            )
        members[info.filename] = info
    return members


def _validate_member_name(name: str) -> None:
    if not name:
        raise _invalid_input(
            "the archive contains a member without a name",
            "use an archive created by `lv24-seo backup create`",
        )
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise _unsafe_member(name)
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _unsafe_member(name)
    if name not in _ALLOWED_MEMBERS and name != MANIFEST_MEMBER:
        raise _invalid_input(
            f"the archive contains a member that is not allowed: {name!r}",
            "use an archive created by `lv24-seo backup create`",
            detail={"member": name},
        )


def _unsafe_member(name: str) -> SeoError:
    return _invalid_input(
        f"the archive contains an unsafe member path: {name!r}",
        "use an archive created by `lv24-seo backup create`; restore never extracts "
        "outside its temporary directory",
        detail={"member": name},
    )


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    if info.is_dir():
        raise _invalid_input(
            f"the archive member {info.filename!r} is a directory",
            "use an archive created by `lv24-seo backup create`",
        )
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = mode & 0o170000
    # ``create_backup`` stores permissions only (0o600, no S_IFREG); a zero type is accepted
    # for zips written by other tools, but a real symlink/FIFO/device is refused.
    if file_type and file_type != stat.S_IFREG:
        raise _invalid_input(
            f"the archive member {info.filename!r} is not a regular file",
            "use an archive created by `lv24-seo backup create`",
        )


def _parse_manifest(raw: bytes) -> BackupManifest:
    try:
        document: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid_input(
            "the archive manifest is not valid JSON",
            "use an archive created by `lv24-seo backup create`",
        ) from None
    if not isinstance(document, Mapping):
        raise _invalid_input(
            "the archive manifest is not a JSON object",
            "use an archive created by `lv24-seo backup create`",
        )
    manifest = cast(Mapping[str, Any], document)
    if manifest.get("format") != BACKUP_FORMAT:
        raise _invalid_input(
            "the archive is not an lv24-seo backup",
            "use an archive created by `lv24-seo backup create`",
        )
    version = manifest.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _invalid_input(
            "the archive manifest has no valid format_version",
            "use an archive created by `lv24-seo backup create`",
        )
    if version != BACKUP_FORMAT_VERSION:
        raise SeoError(
            code=INVALID_INPUT,
            message=(
                f"unsupported backup format version {version}; this engine supports "
                f"v{BACKUP_FORMAT_VERSION}"
            ),
            next_action=(
                "restore with the engine version that created the archive, or create a new "
                "backup with this engine"
            ),
            detail={"format_version": version, "supported": BACKUP_FORMAT_VERSION},
        )
    files_raw = manifest.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise _invalid_input(
            "the archive manifest lists no files",
            "use an archive created by `lv24-seo backup create`",
        )
    files: list[BackupFile] = []
    seen: set[str] = set()
    for item in files_raw:
        if not isinstance(item, Mapping):
            raise _invalid_input(
                "the archive manifest has a malformed file entry",
                "use an archive created by `lv24-seo backup create`",
            )
        entry = cast(Mapping[str, Any], item)
        member_path = entry.get("path")
        size = entry.get("size")
        sha256 = entry.get("sha256")
        if not isinstance(member_path, str):
            raise _invalid_input(
                "the archive manifest has a file entry without a path",
                "use an archive created by `lv24-seo backup create`",
            )
        _validate_member_name(member_path)
        if member_path not in _ALLOWED_MEMBERS:
            raise _invalid_input(
                f"the archive manifest lists a member that is not a payload file: {member_path!r}",
                "use an archive created by `lv24-seo backup create`",
                detail={"member": member_path},
            )
        if member_path in seen:
            raise _invalid_input(
                f"the archive manifest lists {member_path!r} twice",
                "use an archive created by `lv24-seo backup create`",
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _invalid_input(
                f"the archive manifest has an invalid size for {member_path!r}",
                "use an archive created by `lv24-seo backup create`",
            )
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in _HEX_DIGITS for char in sha256)
        ):
            raise _invalid_input(
                f"the archive manifest has an invalid SHA-256 for {member_path!r}",
                "use an archive created by `lv24-seo backup create`",
            )
        seen.add(member_path)
        files.append(BackupFile(path=member_path, size=size, sha256=sha256))
    if CONFIG_MEMBER not in seen:
        raise _invalid_input(
            f"the archive has no {CONFIG_MEMBER}",
            "use an archive created by `lv24-seo backup create`",
        )
    schema_version = manifest.get("schema_version")
    if schema_version is not None and (
        isinstance(schema_version, bool) or not isinstance(schema_version, int)
    ):
        raise _invalid_input(
            "the archive manifest has an invalid schema_version",
            "use an archive created by `lv24-seo backup create`",
        )
    engine_schema = manifest.get("engine_schema_version")
    if isinstance(engine_schema, bool) or not isinstance(engine_schema, int):
        raise _invalid_input(
            "the archive manifest has no valid engine_schema_version",
            "use an archive created by `lv24-seo backup create`",
        )
    engine_version = manifest.get("engine_version")
    created_at = manifest.get("created_at")
    if not isinstance(engine_version, str) or not isinstance(created_at, str):
        raise _invalid_input(
            "the archive manifest has no engine_version or created_at",
            "use an archive created by `lv24-seo backup create`",
        )
    summary_raw = manifest.get("summary")
    summary: Mapping[str, Any] = (
        cast(Mapping[str, Any], summary_raw) if isinstance(summary_raw, Mapping) else {}
    )
    limitations_raw = manifest.get("limitations")
    limitations = (
        tuple(str(item) for item in limitations_raw) if isinstance(limitations_raw, list) else ()
    )
    return BackupManifest(
        engine_version=engine_version,
        schema_version=schema_version,
        engine_schema_version=engine_schema,
        created_at=created_at,
        files=tuple(files),
        summary=dict(summary),
        limitations=limitations,
    )


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if info.file_size > limit:
        raise _invalid_input(
            f"the archive member {info.filename!r} is too large ({info.file_size} bytes; "
            f"limit {limit})",
            "use an archive created by `lv24-seo backup create`",
            detail={"member": info.filename, "size": info.file_size, "limit": limit},
        )
    with archive.open(info) as handle:
        payload = handle.read(limit + 1)
    if len(payload) != info.file_size:
        raise _invalid_input(
            f"the archive member {info.filename!r} is shorter than its header says",
            "create a new backup with `lv24-seo backup create`",
        )
    return payload


def _verify_bytes(payload: bytes, member: BackupFile) -> None:
    if len(payload) != member.size or _sha256_bytes(payload) != member.sha256:
        raise _member_mismatch(member)


def _verify_stream(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    member: BackupFile,
) -> None:
    digest = hashlib.sha256()
    total = 0
    with archive.open(info) as handle:
        while chunk := handle.read(_CHUNK):
            total += len(chunk)
            if total > member.size:
                raise _member_mismatch(member)
            digest.update(chunk)
    if total != member.size or digest.hexdigest() != member.sha256:
        raise _member_mismatch(member)


def _member_mismatch(member: BackupFile) -> SeoError:
    return _invalid_input(
        f"the archive member {member.path!r} does not match the manifest (size or SHA-256)",
        "create a new backup with `lv24-seo backup create`; the archive may be truncated or "
        "modified",
        detail={"member": member.path, "expected_sha256": member.sha256},
    )


def _extract_archive(
    path: Path,
    temp_root: Path,
    *,
    max_config_bytes: int,
    max_database_bytes: int,
) -> tuple[BackupManifest, dict[str, Path], str]:
    """Extract the manifest's members into *temp_root*, verifying every hash again."""
    extracted: dict[str, Path] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            members = _archive_members(archive)
            manifest_info = members.get(MANIFEST_MEMBER)
            if manifest_info is None:
                raise _invalid_input(
                    f"the archive has no {MANIFEST_MEMBER}",
                    "use an archive created by `lv24-seo backup create`",
                )
            manifest_bytes = _read_member(archive, manifest_info, MAX_MANIFEST_BYTES)
            manifest = _parse_manifest(manifest_bytes)
            expected = {item.path: item for item in manifest.files}
            payload_names = set(members) - {MANIFEST_MEMBER}
            if payload_names != set(expected):
                raise _invalid_input(
                    "the archive members do not match the manifest",
                    "use an archive created by `lv24-seo backup create`",
                )
            for member in manifest.files:
                limit = max_config_bytes if member.path == CONFIG_MEMBER else max_database_bytes
                if member.size > limit:
                    raise _invalid_input(
                        f"the archive member {member.path} is too large to restore "
                        f"({member.size} bytes; limit {limit})",
                        "create a smaller backup, or archive the data directory manually",
                        detail={"member": member.path, "size": member.size, "limit": limit},
                    )
                info = members[member.path]
                target = temp_root / Path(member.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                total = 0
                with archive.open(info) as source, target.open("wb") as sink:
                    while chunk := source.read(_CHUNK):
                        total += len(chunk)
                        if total > member.size:
                            raise _member_mismatch(member)
                        digest.update(chunk)
                        sink.write(chunk)
                if total != member.size or digest.hexdigest() != member.sha256:
                    raise _member_mismatch(member)
                extracted[member.path] = target
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError) as exc:
        raise _invalid_input(
            f"the archive at {path} is damaged or uses unsupported compression: {exc}",
            "create a new backup with `lv24-seo backup create`",
        ) from None
    except OSError:
        raise _invalid_input(
            f"cannot read the archive at {path} or write its temporary copy",
            "check free space and file permissions, then retry",
        ) from None
    return manifest, extracted, _sha256_bytes(manifest_bytes)


def _verify_archive_database(path: Path, manifest: BackupManifest) -> Mapping[str, Any]:
    """Run ``PRAGMA quick_check`` and the schema cross-check on an extracted database."""
    try:
        conn = _connect_readonly(path)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            if not rows or any(str(row[0]).lower() != "ok" for row in rows):
                raise _corrupt_archive_database(path)
            actual = _schema_version(conn)
        finally:
            conn.close()
        if actual != manifest.schema_version:
            raise _invalid_input(
                f"the archived database schema is v{actual}, but the manifest says "
                f"v{manifest.schema_version}",
                "create a new backup with `lv24-seo backup create`",
                detail={
                    "schema_version": actual,
                    "manifest_schema_version": manifest.schema_version,
                },
            )
        return _read_database_summary(path)
    except SeoError as exc:
        from lv24_seo.store.errors import STORE_CORRUPT

        if exc.code == STORE_CORRUPT:
            raise _corrupt_archive_database(path) from None
        raise
    except sqlite3.Error as exc:
        raise _corrupt_archive_database(path) from exc


def _corrupt_archive_database(path: Path) -> SeoError:
    return _invalid_input(
        f"the archived database is not a valid SQLite file: {path.name}",
        "create a new backup from a healthy workspace; the target was not changed",
    )


def _verify_restore_token(plan: RestorePlan, token: str, *, now: datetime | None) -> None:
    raw = token.strip()
    expiry_raw, separator, digest = raw.rpartition(":")
    if (
        not separator
        or not expiry_raw.isdigit()
        or len(digest) != 64
        or any(char not in _HEX_DIGITS for char in digest)
    ):
        raise _confirm_required("the confirm token is malformed")
    expiry = int(expiry_raw)
    current = int(_utc_moment(now).timestamp())
    if expiry <= current:
        raise _confirm_required("the confirm token expired")
    expected = _restore_token(plan.plan_hash, plan.archive_sha256, expiry)
    if not hmac.compare_digest(expected, raw):
        raise _confirm_required("the confirm token does not match this plan")


def _restore_token(plan_hash: str, archive_sha256: str, expiry: int) -> str:
    message = f"{_RESTORE_SCOPE}|{plan_hash}|{archive_sha256}|{expiry}"
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    return f"{expiry}:{digest}"


def _confirm_required(reason: str) -> SeoError:
    return SeoError(
        code=CONFIRM_REQUIRED,
        message=f"{reason}; nothing was written",
        next_action=(
            "rerun `lv24-seo backup restore` without --confirm, review the plan and pass the "
            "fresh confirm_token within 5 minutes"
        ),
        retryable=False,
    )


def _recheck_pre_state(plan: RestorePlan) -> None:
    current_config = _file_sha256_or_none(plan.config_path)
    if current_config != plan.existing_config_sha256:
        raise _confirm_required("the config changed after the plan was made")
    if plan.database_member:
        current_database = _file_sha256_or_none(plan.database_path)
        if current_database != plan.existing_database_sha256:
            raise _confirm_required("the database changed after the plan was made")
    if plan.config_action == "write_alongside" and plan.config_restore_path.exists():
        raise _confirm_required("the config restore target appeared after the plan was made")


def _ensure_data_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _invalid_input(
            f"cannot create the data directory at {path}",
            "check permissions and free space, then retry",
        ) from None
    if not path.is_dir():
        raise _invalid_input(
            f"the data directory path is not a directory: {path}",
            "move the file away and retry",
        )
    return path


def _make_temp_root(data_dir: Path) -> Path:
    try:
        return Path(tempfile.mkdtemp(prefix=".lv24-seo-restore-", dir=str(data_dir)))
    except OSError:
        raise _invalid_input(
            f"cannot create a temporary restore directory in {data_dir}",
            "check permissions and free space, then retry",
        ) from None


def _backup_current_database(source: Path, destination: Path, warnings: list[str]) -> None:
    """Snapshot the current database before it is replaced.

    The SQLite backup API gives a consistent single file. A corrupt or locked database must not
    block the restore, so a raw file copy (plus its sidecars) is the fallback, with a warning.
    """
    try:
        _copy_database(source, destination)
        return
    except SeoError as exc:
        reason = exc.code
    except OSError:
        reason = "io"
    try:
        shutil.copy2(source, destination)
        for suffix in _DATABASE_SIDECARS:
            side = source.with_name(source.name + suffix)
            if side.is_file():
                shutil.copy2(side, destination.with_name(destination.name + suffix))
    except OSError:
        raise _invalid_input(
            f"cannot back up the current database at {source} before restoring",
            "check free space and permissions, then retry",
        ) from None
    warnings.append(
        f"the current database could not be snapshotted through SQLite ({reason}); a raw file "
        f"copy was saved to {destination}"
    )


def _copy_file(source: Path, destination: Path, *, what: str) -> None:
    try:
        shutil.copy2(source, destination)
    except OSError:
        raise _invalid_input(
            f"cannot back up the {what} at {source} before restoring",
            "check free space and permissions, then retry",
        ) from None


def _remove_sidecars(path: Path) -> None:
    for suffix in _DATABASE_SIDECARS:
        side = path.with_name(path.name + suffix)
        try:
            side.unlink(missing_ok=True)
        except OSError:
            raise _invalid_input(
                f"cannot remove the stale SQLite sidecar at {side}",
                "stop any lv24-seo or desktop process that has the database open, then retry",
            ) from None


def _replace_file(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError:
        raise _invalid_input(
            f"cannot replace {destination}",
            "stop any lv24-seo or desktop process that has the file open, check permissions, "
            "then retry",
        ) from None


def _stage_config(destination: Path, payload: bytes) -> Path:
    try:
        handle_fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
    except OSError:
        raise _invalid_input(
            f"cannot write to the config directory: {destination.parent}",
            "check permissions and free space, then retry",
        ) from None
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temp_path, 0o600)
    except OSError:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise _invalid_input(
            f"cannot stage the config for {destination}",
            "check permissions and free space, then retry",
        ) from None
    return temp_path


def _file_sha256_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return _sha256_file(path)
    except OSError:
        raise _invalid_input(
            f"cannot read the file at {path}",
            "check the file permissions, then retry",
        ) from None


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.{index}")
        if not candidate.exists():
            return candidate
    raise _invalid_input(
        f"cannot find a free file name next to {path}",
        "remove old .pre-restore files, then retry",
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _invalid_input(
    message: str,
    next_action: str,
    *,
    detail: Mapping[str, Any] | None = None,
) -> SeoError:
    return SeoError(
        code=INVALID_INPUT,
        message=message,
        next_action=next_action,
        detail=dict(detail or {}),
    )


def _optional_str(path: Path | None) -> str | None:
    return None if path is None else str(path)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_str_value(value: Any) -> str | None:
    return value if isinstance(value, str) else None
