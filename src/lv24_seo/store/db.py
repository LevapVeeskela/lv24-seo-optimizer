"""SQLite connection factory and one-shot schema bootstrap.

Why this module is separate from :mod:`lv24_seo.store.repositories`
------------------------------------------------------------------
Connection concerns are shared by every repository, report and test fixture: the same PRAGMAs
must hold for a CLI run, an MCP tool call and a migration. Keeping them in one function makes
"works on my machine" impossible -- there is exactly one way to open the database.

Schema versions
--------------
The base ``schema.sql`` creates every table for a fresh database, including the
``schema_migrations`` ledger and ``metric_windows``. ``migrate()`` then applies any
pending numbered migrations in order and records them, so an existing database
created before a table existed gains it without losing user data. Migrations are
append-only: never edit an applied migration, add a new version instead.

Rules
-----
* **WAL** is enabled for every file-backed database: readers never block the writer, which
  matters when a scheduled report reads while a provider sync writes. A concurrent first open
  can race on the mode switch; :func:`_enable_wal` retries and verifies instead of failing.
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
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from lv24_seo.store.errors import translate_sqlite_error

__all__ = ["MEMORY_PATH", "SCHEMA_VERSION", "connect", "initialize", "migrate", "schema_path"]

#: SQLite's in-memory database name. WAL is meaningless there (the journal is memory-backed).
MEMORY_PATH = ":memory:"
SCHEMA_FILENAME = "schema.sql"
_BUSY_TIMEOUT_SECONDS = 30.0
_WAL_ATTEMPTS = 5
_WAL_RETRY_SECONDS = 0.05

#: Current schema version. Bump when appending to ``_MIGRATIONS``.
SCHEMA_VERSION = 5

_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    # v1 backfills databases created before ``schema_migrations`` /
    # ``metric_windows`` existed (the base script is idempotent, so re-running
    # it is the migration). New tables arrive here as separate DDL later.
    (
        1,
        "metric_windows window-aggregate table and migrations ledger",
        """
        CREATE TABLE IF NOT EXISTS metric_windows (
            property_id TEXT NOT NULL,
            source      TEXT NOT NULL,
            start       TEXT NOT NULL,
            "end"       TEXT NOT NULL,
            "scope"     TEXT NOT NULL,
            "key"       TEXT NOT NULL,
            metric      TEXT NOT NULL,
            value       REAL NOT NULL,
            accuracy    TEXT,
            PRIMARY KEY (property_id, source, start, "end", "scope", "key", metric)
        );
        CREATE INDEX IF NOT EXISTS idx_metric_windows_end
            ON metric_windows (property_id, "end");
        """,
    ),
    (
        2,
        "mark legacy GSC daily rows with unknown source granularity",
        """
        UPDATE metric_daily
        SET accuracy = 'legacy_unknown_granularity'
        WHERE source = 'gsc';
        """,
    ),
    (
        3,
        "tasks finding lifecycle table",
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,
            property_id  TEXT NOT NULL,
            url          TEXT NOT NULL,
            rule_id      TEXT NOT NULL,
            state        TEXT NOT NULL DEFAULT 'open'
                         CHECK (state IN ('open', 'in_progress', 'verified', 'dismissed')),
            payload_json TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            UNIQUE (kind, property_id, url, rule_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_property_state
            ON tasks (property_id, state, updated_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_kind_state
            ON tasks (kind, state, updated_at);
        """,
    ),
    (
        4,
        "indexing_events provider-reported index status table",
        """
        CREATE TABLE IF NOT EXISTS indexing_events (
            provider    TEXT NOT NULL,
            property_id TEXT NOT NULL,
            url         TEXT NOT NULL,
            event       TEXT NOT NULL,
            event_date  TEXT NOT NULL,
            detail_json TEXT,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            UNIQUE (provider, property_id, url, event, event_date)
        );
        CREATE INDEX IF NOT EXISTS idx_indexing_events_property_date
            ON indexing_events (property_id, event_date);
        CREATE INDEX IF NOT EXISTS idx_indexing_events_property_url
            ON indexing_events (property_id, url, event_date);
        """,
    ),
    (
        5,
        "quota-aware indexing state, run history and local quota usage",
        """
        CREATE TABLE IF NOT EXISTS indexing_state (
            provider          TEXT NOT NULL,
            property_id       TEXT NOT NULL,
            url               TEXT NOT NULL,
            first_seen        TEXT NOT NULL,
            last_submitted_at TEXT,
            last_checked_at   TEXT,
            submit_status     TEXT NOT NULL DEFAULT 'not_submitted'
                              CHECK (submit_status IN
                                     ('not_submitted', 'accepted', 'already_queued',
                                      'skipped', 'error')),
            index_status      TEXT NOT NULL DEFAULT 'unknown'
                              CHECK (index_status IN ('indexed', 'not_indexed', 'crawled',
                                                      'unknown')),
            coverage_state    TEXT,
            http_status       INTEGER,
            last_crawled_at   TEXT,
            next_check_at     TEXT,
            detail_json       TEXT,
            UNIQUE (provider, property_id, url)
        );
        CREATE INDEX IF NOT EXISTS idx_indexing_state_provider_check
            ON indexing_state (provider, property_id, next_check_at);
        CREATE INDEX IF NOT EXISTS idx_indexing_state_provider_status
            ON indexing_state (provider, property_id, index_status);
        CREATE TABLE IF NOT EXISTS indexing_runs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at        TEXT NOT NULL,
            finished_at       TEXT,
            provider          TEXT NOT NULL,
            planned           INTEGER NOT NULL DEFAULT 0,
            submitted         INTEGER NOT NULL DEFAULT 0,
            checked           INTEGER NOT NULL DEFAULT 0,
            skipped           INTEGER NOT NULL DEFAULT 0,
            quota_before_json TEXT,
            quota_after_json  TEXT,
            status            TEXT NOT NULL,
            detail_json       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_indexing_runs_provider_started
            ON indexing_runs (provider, started_at);
        CREATE INDEX IF NOT EXISTS idx_indexing_runs_started
            ON indexing_runs (started_at);
        CREATE TABLE IF NOT EXISTS quota_usage (
            provider    TEXT NOT NULL,
            property_id TEXT NOT NULL,
            day         TEXT NOT NULL,
            kind        TEXT NOT NULL,
            used        INTEGER NOT NULL DEFAULT 0,
            UNIQUE (provider, property_id, day, kind)
        );
        """,
    ),
)


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


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Enable WAL, tolerating a concurrent opener that is switching it at the same time.

    Changing the journal mode needs a brief exclusive lock, and SQLite on Windows can answer
    ``SQLITE_BUSY`` immediately instead of running the busy handler. WAL is a persistent
    database property, so a failed switch is harmless when another process already enabled it.
    The switch gets a short busy timeout and a few retries: only another opener's schema
    bootstrap can legitimately hold the lock this long, and a real long-running writer must
    still fail fast instead of stalling the CLI.
    """
    conn.execute("PRAGMA busy_timeout = 1000")
    try:
        for attempt in range(_WAL_ATTEMPTS):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError:
                row = conn.execute("PRAGMA journal_mode").fetchone()
                if row is not None and str(row[0]).lower() == "wal":
                    return
                if attempt + 1 == _WAL_ATTEMPTS:
                    raise
                time.sleep(_WAL_RETRY_SECONDS * (attempt + 1))
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_SECONDS * 1000)}")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open ``path`` with the project's PRAGMAs and row factory.

    Parent directories are created automatically. ``:memory:`` is supported for tests and
    one-off tools (WAL is skipped because an in-memory journal cannot use it). A lock or a
    corrupt file becomes a typed :class:`~lv24_seo.store.errors.StoreError` instead of a raw
    ``sqlite3`` exception, so every surface can act on it.
    """
    target = _database_target(path)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(target, timeout=_BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if target != MEMORY_PATH:
            _enable_wal(conn)
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        translated = translate_sqlite_error(exc, path)
        if translated is None:
            raise
        raise translated from exc
    return conn


def initialize(conn: sqlite3.Connection, *, path: Path | str | None = None) -> None:
    """Create every table and index; safe to call on an already-initialised database."""
    try:
        conn.executescript(_schema_sql())
        conn.commit()
    except sqlite3.Error as exc:
        translated = translate_sqlite_error(
            exc, path if path is not None else _connection_path(conn)
        )
        if translated is None:
            raise
        raise translated from exc
    migrate(conn)


def _connection_path(conn: sqlite3.Connection) -> str:
    """Best-effort file path of an open connection (``:memory:`` for in-memory databases)."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return MEMORY_PATH
    if row is None or len(row) < 3 or not row[2]:
        return MEMORY_PATH
    return str(row[2])


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending numbered migrations in order; return the versions applied."""
    try:
        return _migrate(conn)
    except sqlite3.Error as exc:
        translated = translate_sqlite_error(exc, _connection_path(conn))
        if translated is None:
            raise
        raise translated from exc


def _migrate(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER NOT NULL PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()
    done: list[int] = []
    for version, description, sql in _MIGRATIONS:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone():
                continue
            # executescript commits an existing transaction, so execute complete
            # statements individually to keep DDL/data and the ledger atomic.
            statement = ""
            for character in sql:
                statement += character
                if character == ";" and sqlite3.complete_statement(statement):
                    conn.execute(statement)
                    statement = ""
            if statement.strip():
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                (version, description, datetime.now(UTC).isoformat(timespec="seconds")),
            )
            done.append(version)
    return done
