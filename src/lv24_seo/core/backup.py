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
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import tomllib
import zipfile
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
from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, SeoError
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
    "BackupFile",
    "BackupManifest",
    "BackupResult",
    "create_backup",
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

_CHUNK = 1024 * 1024
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
