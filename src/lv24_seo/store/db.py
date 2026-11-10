"""SQLite connection factory and one-shot schema bootstrap.

Why this module is separate from :mod:`lv24_seo.store.repositories`
------------------------------------------------------------------
Connection concerns are shared by every repository, report and test fixture: the same PRAGMAs
must hold for a CLI run, an MCP tool call and a migration. Keeping them in one function makes
"works on my machine" impossible -- there is exactly one way to open the database.

Rules
-----
* **WAL** is enabled for every file-backed database: readers never block the writer, which
  matters when a scheduled report reads while a provider sync writes.
* **``foreign_keys = ON``** is set even though the current schema declares no foreign keys; the
  pragma is per connection and a future migration may add them.
* **``row_factory = sqlite3.Row``** lets repositories address columns by name and keeps SQL
  tolerant to column order.
* ``initialize`` is idempotent: the script uses ``IF NOT EXISTS`` only, so running it at every
  process start is cheap and safe.
* The schema file lives next to this module (``store/schema.sql``) and is shipped inside the
  wheel, so no path discovery or packaging step is needed at runtime.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

__all__ = ["MEMORY_PATH", "connect", "initialize", "schema_path"]

#: SQLite's in-memory database name. WAL is meaningless there (the journal is memory-backed).
MEMORY_PATH = ":memory:"
SCHEMA_FILENAME = "schema.sql"
_BUSY_TIMEOUT_SECONDS = 30.0


def schema_path() -> Path:
    """Absolute path of the packaged schema file."""
    return Path(__file__).with_name(SCHEMA_FILENAME)


@lru_cache(maxsize=1)
def _schema_sql() -> str:
    # Read once per process: a schema edit requires a restart anyway, and tests get the same
    # bytes as production instead of re-reading the file for every Store.
    return schema_path().read_text(encoding="utf-8")


def _database_target(path: Path | str) -> str:
    """Return a sqlite3 target string, creating the parent directory when needed."""
    raw = str(path)
    if raw == MEMORY_PATH:
        return raw
    candidate = Path(raw).expanduser()
    parent = candidate.parent
    if parent != Path() and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    return str(candidate)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open ``path`` with the project's PRAGMAs and row factory.

    Parent directories are created automatically. ``:memory:`` is supported for tests and
    one-off tools (WAL is skipped because an in-memory journal cannot use it).
    """
    target = _database_target(path)
    conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != MEMORY_PATH:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create every table and index; safe to call on an already-initialised database."""
    conn.executescript(_schema_sql())
    conn.commit()
