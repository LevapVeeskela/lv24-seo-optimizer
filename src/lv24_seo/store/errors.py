"""Typed storage failures (ENG-10).

SQLite raises a handful of very different problems through the same ``sqlite3`` exception
classes: a *transient* write lock, a *permanent* corrupt file and an unusable path all used to
reach the CLI as a generic ``INTERNAL`` "report the bug" envelope. Callers cannot act on that:
a lock should be retried, a corrupt file needs a recovery path, and neither is a program bug.

Rules
-----
* ``STORE_LOCKED`` is retryable: another process holds the write lock past the busy timeout.
* ``STORE_CORRUPT`` carries ``detail.backup_path`` — a byte copy of the unusable file made
  before anything else happens. The original is never moved or deleted.
* ``STORE_UNAVAILABLE`` covers everything else SQLite reports (permissions, missing parent,
  disk errors) with a concrete next action.
* The exception message and detail never contain row data, tokens or SQL parameters.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from lv24_seo.core.errors import SeoError

__all__ = [
    "STORE_CORRUPT",
    "STORE_LOCKED",
    "STORE_UNAVAILABLE",
    "StoreError",
    "corrupt_error",
    "locked_error",
    "preserve_corrupt_copy",
    "translate_sqlite_error",
    "unavailable_error",
]

STORE_LOCKED = "STORE_LOCKED"
STORE_CORRUPT = "STORE_CORRUPT"
STORE_UNAVAILABLE = "STORE_UNAVAILABLE"

_MEMORY_PATH = ":memory:"
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
_UNAVAILABLE_MARKERS = (
    "unable to open database file",
    "attempt to write a readonly database",
    "database or disk is full",
    "disk i/o error",
    "permission denied",
)


class StoreError(SeoError):
    """A storage failure with a stable code (see the module constants)."""


def locked_error(path: str | Path) -> StoreError:
    """Another connection holds the write lock past the busy timeout."""
    return StoreError(
        code=STORE_LOCKED,
        message=f"the database at {path} is locked by another process",
        next_action=(
            "wait for the other lv24-seo process (crawl, report, MCP server) to finish, "
            "then retry; nothing was written"
        ),
        retryable=True,
        detail={"path": str(path)},
    )


def corrupt_error(path: str | Path, *, backup_path: Path | None = None) -> StoreError:
    """The file is not a usable SQLite database; *backup_path* preserves its bytes."""
    detail: dict[str, str] = {"path": str(path)}
    if backup_path is not None:
        detail["backup_path"] = str(backup_path)
        next_action = (
            f"the original bytes are preserved at {backup_path}; inspect or archive that copy, "
            f"then move or delete {path} and rerun — a new empty database is created"
        )
    else:
        next_action = (
            f"move or delete {path} and rerun — a new empty database is created; "
            "the old file was left untouched because a copy could not be made"
        )
    return StoreError(
        code=STORE_CORRUPT,
        message=f"the database at {path} is not a valid SQLite file",
        next_action=next_action,
        retryable=False,
        detail=detail,
    )


def unavailable_error(path: str | Path, *, reason: str = "") -> StoreError:
    """SQLite could not use the path for a non-corruption reason (permissions, disk)."""
    detail: dict[str, str] = {"path": str(path)}
    if reason:
        detail["reason"] = reason
    return StoreError(
        code=STORE_UNAVAILABLE,
        message=f"the database at {path} could not be opened or written",
        next_action=(
            "check that the data directory exists, is writable and has free space; "
            "run `lv24-seo doctor`"
        ),
        retryable=False,
        detail=detail,
    )


def preserve_corrupt_copy(path: str | Path) -> Path | None:
    """Copy an unusable database next to itself and return the copy, or ``None``.

    The copy is additive on purpose: recovery must never destroy the only evidence of what
    went wrong, and a failed copy must not hide the original :class:`StoreError`.
    """
    raw = str(path)
    if raw == _MEMORY_PATH:
        return None
    candidate = Path(raw)
    if not candidate.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = candidate.with_name(f"{candidate.name}.corrupt-{stamp}")
    try:
        shutil.copy2(candidate, backup)
    except OSError:
        return None
    return backup


def translate_sqlite_error(exc: sqlite3.Error, path: str | Path) -> StoreError | None:
    """Map a raw SQLite failure onto a typed, actionable :class:`StoreError`.

    ``None`` means "not a storage condition this layer understands" (for example a broken
    migration statement): the caller re-raises the original error so a real programming bug
    keeps its message and traceback instead of being disguised as a disk problem.
    """
    text = str(exc).lower()
    if any(marker in text for marker in _LOCKED_MARKERS):
        return locked_error(path)
    if any(marker in text for marker in _CORRUPT_MARKERS):
        return corrupt_error(path, backup_path=preserve_corrupt_copy(path))
    if any(marker in text for marker in _UNAVAILABLE_MARKERS):
        return unavailable_error(path, reason=str(exc).strip()[:200])
    return None
