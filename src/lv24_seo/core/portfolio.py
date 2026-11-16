"""Read-only multi-site portfolio scan (F11).

One ``seo.toml`` describes one project, so an agency workspace is a directory tree of
independent project folders. This module walks that tree (at most :data:`MAX_DEPTH` levels
below the root), loads each config without the caller's ``LV24_SEO_*`` environment, and reads
the local SQLite history of every site. It never writes, never calls a provider and never
opens a secret store: the output is paths, timestamps and counts only.

Design rules
------------
* **Bounded.** At most :data:`MAX_SITES` configs are inspected per scan. Discovery still
  counts every config in the tree, so a capped result reports ``truncated`` and an honest
  ``total`` instead of silently showing a partial portfolio as complete.
* **Per-site failure is data, not a crash.** A broken TOML file or an unreadable database
  yields a site row with an ``error`` object; the rest of the portfolio still scans.
* **No filesystem writes.** SQLite runs in ``immutable=1`` mode when the database has no
  ``-wal``/``-shm``/``-journal`` sidecar, which avoids creating a shared-memory index just to
  read a quiescent file. A live WAL database is opened with ``mode=ro``; its sidecars already
  exist, so the scan does not change the database content.
* **Hermetic config.** :func:`~lv24_seo.core.config.load_config` is called with ``env={}``:
  an ambient ``LV24_SEO_DATA_DIR`` must not redirect every site in the portfolio to one
  directory. A relative or default ``data_dir`` is anchored to the config file's directory,
  because a multi-site scan has no meaningful single working directory.

Timestamps are the ISO-8601 UTC strings stored by the history layer; they are passed through
unchanged so output diffs stay stable. Sorting is by last activity, then by config path.
"""

from __future__ import annotations

import os
import sqlite3
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lv24_seo.core.config import CONFIG_FILENAME, SeoConfig, data_dir_path, load_config
from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, SeoError

__all__ = [
    "DB_FILENAME",
    "MAX_DEPTH",
    "MAX_SITES",
    "PortfolioScan",
    "SiteSummary",
    "scan_portfolio",
]

#: Directory levels below the root that may contain a project config.
MAX_DEPTH = 3
#: Hard cap on inspected sites; keeps a portfolio scan cheap on a large agency tree.
MAX_SITES = 50
#: Same file name as :data:`lv24_seo.composition.DB_FILENAME` (pinned by a test; core must
#: not import the composition root, which depends on this layer).
DB_FILENAME = "seo.sqlite3"

_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv"})
_MAX_DIR_WARNINGS = 20
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# SQLite error markers, aligned with :mod:`lv24_seo.store.errors` so the desktop can reuse
# its existing STORE_* handling. The codes are duplicated as strings because ``core`` must
# not depend on ``store``; a test pins the values.
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
_DB_LOCKED = "STORE_LOCKED"
_DB_CORRUPT = "STORE_CORRUPT"
_DB_UNAVAILABLE = "STORE_UNAVAILABLE"


@dataclass(slots=True)
class SiteSummary:
    """One project row: identity, local storage state and last activity."""

    config_path: str
    name: str = ""
    property: str = ""
    base_url: str = ""
    data_dir: str | None = None
    db_path: str | None = None
    db_present: bool = False
    crawl_runs: int = 0
    last_crawl_at: str | None = None
    report_snapshots: int = 0
    last_report_at: str | None = None
    last_report_period: str | None = None
    issues_open: int = 0
    issues_total: int = 0
    issues_critical: int = 0
    last_issue_at: str | None = None
    last_activity_at: str | None = None
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON shape (keys never disappear; unknown values are ``null``)."""
        return {
            "config_path": self.config_path,
            "name": self.name,
            "property": self.property,
            "base_url": self.base_url,
            "data_dir": self.data_dir,
            "db_path": self.db_path,
            "db_present": self.db_present,
            "crawl_runs": self.crawl_runs,
            "last_crawl_at": self.last_crawl_at,
            "report_snapshots": self.report_snapshots,
            "last_report_at": self.last_report_at,
            "last_report_period": self.last_report_period,
            "issues_open": self.issues_open,
            "issues_total": self.issues_total,
            "issues_critical": self.issues_critical,
            "last_issue_at": self.last_issue_at,
            "last_activity_at": self.last_activity_at,
            "error": None if self.error is None else dict(self.error),
        }


@dataclass(slots=True)
class PortfolioScan:
    """Result of one bounded portfolio scan."""

    root: Path
    sites: list[SiteSummary]
    total: int
    limit: int
    warnings: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        """True when configs were discovered but not inspected because of the cap."""
        return self.total > len(self.sites)

    def to_dict(self) -> dict[str, Any]:
        """The ``data`` payload of the CLI envelope."""
        return {"sites": [site.to_dict() for site in self.sites]}

    def meta(self) -> dict[str, Any]:
        """Completeness flags and input echo for the envelope ``meta`` block."""
        return {
            "root": str(self.root),
            "limit": self.limit,
            "count": len(self.sites),
            "total": self.total,
            "truncated": self.truncated,
            "data_age_days": 0,
            "read_only": True,
            "max_depth": MAX_DEPTH,
            "limitations": self.limitations(),
        }

    def limitations(self) -> list[str]:
        """Human-readable coverage caveat; empty when the scan covered every config."""
        if not self.truncated:
            return []
        if self.limit < MAX_SITES:
            next_step = f"raise --limit (max {MAX_SITES}) or scan a narrower root"
        else:
            next_step = f"the scan is capped at {MAX_SITES} sites; scan a narrower root"
        return [
            f"inspected the first {len(self.sites)} of {self.total} project configs "
            f"(limit {self.limit}); {next_step}"
        ]


@dataclass(slots=True)
class _DbStats:
    """Counts and timestamps read from one history database."""

    crawl_runs: int = 0
    last_crawl_at: str | None = None
    report_snapshots: int = 0
    last_report_at: str | None = None
    last_report_period: str | None = None
    issues_open: int = 0
    issues_total: int = 0
    issues_critical: int = 0
    last_issue_at: str | None = None


def scan_portfolio(root: str | Path, *, limit: int = MAX_SITES) -> PortfolioScan:
    """Scan *root* for project configs and their local history (read-only).

    ``root`` must be an absolute, existing directory. ``limit`` is validated against
    ``1..MAX_SITES`` rather than silently clamped, so a caller never gets fewer sites than
    it asked for without a ``truncated`` flag.
    """
    if not 1 <= limit <= MAX_SITES:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"limit must be between 1 and {MAX_SITES}, got {limit}",
            next_action=f"pass --limit between 1 and {MAX_SITES}",
        )

    candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        raise SeoError(
            code=INVALID_INPUT,
            message="portfolio root must be an absolute path",
            next_action="pass --root as an absolute directory, for example /srv/sites",
        )
    if not candidate.exists():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"portfolio root does not exist: {candidate}",
            next_action="check the path or create the directory",
        )
    if not candidate.is_dir():
        raise SeoError(
            code=INVALID_INPUT,
            message=f"portfolio root is not a directory: {candidate}",
            next_action="pass a directory that contains project folders",
        )

    root_path = Path(os.path.abspath(candidate))
    warnings: list[str] = []
    try:
        with os.scandir(root_path):
            pass
    except OSError as exc:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"cannot read the portfolio root at {root_path}: {exc.strerror or 'I/O error'}",
            next_action="check directory permissions and retry",
        ) from None

    configs = list(_iter_config_paths(root_path, warnings))
    selected = configs[:limit]
    sites = [_summarize(path) for path in selected]
    _sort_by_activity(sites)
    return PortfolioScan(
        root=root_path,
        sites=sites,
        total=len(configs),
        limit=limit,
        warnings=warnings,
    )


def _iter_config_paths(root: Path, warnings: list[str]) -> Iterator[Path]:
    """Yield ``seo.toml`` files breadth-first, pruned to hidden/heavy directories.

    Symlinked directories are skipped (cycle safety); a symlinked config *file* is allowed
    because it is read once and never followed into another tree. Unreadable directories are
    skipped with a bounded warning instead of aborting the whole portfolio.
    """
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        directory, depth = queue.popleft()
        try:
            entries = sorted(
                directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
            )
        except OSError as exc:
            if len(warnings) < _MAX_DIR_WARNINGS:
                reason = exc.strerror or "I/O error"
                warnings.append(f"skipped unreadable directory {directory}: {reason}")
            continue

        for entry in entries:
            if entry.is_dir():
                if entry.is_symlink() or _skip_dir(entry.name) or depth >= MAX_DEPTH:
                    continue
                queue.append((entry, depth + 1))
            elif entry.name == CONFIG_FILENAME and entry.is_file():
                yield entry


def _skip_dir(name: str) -> bool:
    """Hidden and tooling directories never contain a user project config."""
    return name.startswith(".") or name in _SKIP_DIRS


def _summarize(config_path: Path) -> SiteSummary:
    """Load one config and its database; convert every failure into a per-site error."""
    summary = SiteSummary(config_path=str(config_path))
    try:
        config = load_config(config_path, env={})
    except SeoError as exc:
        summary.error = {"code": exc.code, "message": exc.message}
        return summary

    summary.name = config.project.name
    summary.base_url = config.project.base_url
    summary.property = (
        config.project.default_property
        or (config.providers.gsc.property if config.providers.gsc else "")
        or config.project.base_url
    )
    data_dir = _anchor_data_dir(config, config_path)
    db_path = data_dir / DB_FILENAME
    summary.data_dir = str(data_dir)
    summary.db_path = str(db_path)

    if data_dir.exists() and not data_dir.is_dir():
        summary.error = {
            "code": CONFIG_INVALID,
            "message": f"the data directory at {data_dir} is not a directory",
        }
        return summary

    summary.db_present = db_path.is_file()
    if not summary.db_present:
        return summary

    stats, error = _read_database(db_path, property_id=summary.property)
    summary.error = error
    summary.crawl_runs = stats.crawl_runs
    summary.last_crawl_at = stats.last_crawl_at
    summary.report_snapshots = stats.report_snapshots
    summary.last_report_at = stats.last_report_at
    summary.last_report_period = stats.last_report_period
    summary.issues_open = stats.issues_open
    summary.issues_total = stats.issues_total
    summary.issues_critical = stats.issues_critical
    summary.last_issue_at = stats.last_issue_at
    summary.last_activity_at = _latest(
        stats.last_crawl_at, stats.last_report_at, stats.last_issue_at
    )
    return summary


def _anchor_data_dir(config: SeoConfig, config_path: Path) -> Path:
    """Resolve a relative/default data directory against the config file.

    ``load_config`` anchors a *declared* relative path; the built-in default stays
    process-scoped for normal CLI runs. A portfolio scan has no single working directory, so
    the implicit default is anchored to the project folder here as well.
    """
    candidate = data_dir_path(config)
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return Path(os.path.abspath(candidate))


def _read_database(db_path: Path, *, property_id: str) -> tuple[_DbStats, dict[str, str] | None]:
    """Read counts from one history database without writing anything."""
    blocked = _blocked_sidecar_reason(db_path)
    if blocked is not None:
        return _DbStats(), {"code": _DB_UNAVAILABLE, "message": blocked}

    try:
        conn = _open_read_only(db_path)
    except sqlite3.Error as exc:
        return _DbStats(), _sqlite_error(exc)

    try:
        tables = _tables(conn)
        params = {"property": property_id or None}
        stats = _DbStats()
        _read_crawl_stats(conn, tables, params, stats)
        _read_report_stats(conn, tables, params, stats)
        _read_issue_stats(conn, tables, params, stats)
        return stats, None
    except sqlite3.Error as exc:
        return _DbStats(), _sqlite_error(exc)
    finally:
        conn.close()


def _blocked_sidecar_reason(db_path: Path) -> str | None:
    """Refuse states where a read-only open would need to create or roll back files.

    A cleanly closed WAL database has no sidecars and is read with ``immutable=1``. A live
    writer leaves ``-wal`` and ``-shm``; reading with ``mode=ro`` then creates nothing because
    the shared-memory file already exists. A ``-wal`` without ``-shm`` or a pending rollback
    journal would force SQLite to write, so the site is reported as unavailable instead of
    silently violating the read-only contract.
    """
    wal = Path(f"{db_path}-wal")
    shm = Path(f"{db_path}-shm")
    journal = Path(f"{db_path}-journal")
    if journal.exists():
        return (
            "the database has a pending rollback journal; open the project once with "
            "`lv24-seo` so SQLite can recover it, then rescan"
        )
    if wal.exists() and not shm.exists():
        return (
            "the database has an uncheckpointed WAL without its shared-memory file; "
            "open the project once with `lv24-seo` so SQLite can checkpoint it, then rescan"
        )
    return None


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    """Open SQLite read-only; ``immutable`` avoids creating WAL sidecars for a quiet file."""
    live = Path(f"{db_path}-wal").exists() or Path(f"{db_path}-shm").exists()
    query = "?mode=ro" if live else "?immutable=1"
    conn = sqlite3.connect(f"{db_path.as_uri()}{query}", uri=True, timeout=1.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    """Table names present in this database (older databases may lack newer tables)."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def _read_crawl_stats(
    conn: sqlite3.Connection, tables: set[str], params: dict[str, str | None], stats: _DbStats
) -> None:
    """Prefer the run journal; fall back to the legacy ``crawl_runs`` table."""
    if "runs" in tables:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total, MAX(COALESCE(finished_at, started_at)) AS last
            FROM runs
            WHERE kind = 'crawl'
              AND (:property IS NULL OR property_id = :property OR property_id IS NULL)
            """,
            params,
        ).fetchone()
        if row is not None:
            stats.crawl_runs = int(row["total"] or 0)
            stats.last_crawl_at = _text(row["last"])
    elif "crawl_runs" in tables:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total, MAX(COALESCE(finished_at, started_at)) AS last
            FROM crawl_runs
            """
        ).fetchone()
        if row is not None:
            stats.crawl_runs = int(row["total"] or 0)
            stats.last_crawl_at = _text(row["last"])


def _read_report_stats(
    conn: sqlite3.Connection, tables: set[str], params: dict[str, str | None], stats: _DbStats
) -> None:
    """Report activity is the ``snapshots`` table (one row per kind/property/period)."""
    if "snapshots" not in tables:
        return
    row = conn.execute(
        """
        SELECT COUNT(*) AS total, MAX(created_at) AS last
        FROM snapshots
        WHERE (:property IS NULL OR property_id = :property)
        """,
        params,
    ).fetchone()
    if row is not None:
        stats.report_snapshots = int(row["total"] or 0)
        stats.last_report_at = _text(row["last"])
    period_row = conn.execute(
        """
        SELECT period
        FROM snapshots
        WHERE (:property IS NULL OR property_id = :property)
        ORDER BY created_at DESC, period DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if period_row is not None:
        stats.last_report_period = _text(period_row["period"])


def _read_issue_stats(
    conn: sqlite3.Connection, tables: set[str], params: dict[str, str | None], stats: _DbStats
) -> None:
    """Open/total/critical issue counts plus the latest observation timestamp."""
    if "issues" not in tables:
        return
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN state = 'open' THEN 1 ELSE 0 END), 0) AS open,
               COALESCE(
                   SUM(CASE WHEN state = 'open' AND severity = 'critical' THEN 1 ELSE 0 END), 0
               ) AS critical,
               MAX(last_seen) AS last
        FROM issues
        WHERE (:property IS NULL OR property_id = :property)
        """,
        params,
    ).fetchone()
    if row is not None:
        stats.issues_total = int(row["total"] or 0)
        stats.issues_open = int(row["open"] or 0)
        stats.issues_critical = int(row["critical"] or 0)
        stats.last_issue_at = _text(row["last"])


def _sqlite_error(exc: sqlite3.Error) -> dict[str, str]:
    """Map a SQLite failure onto the stable STORE_* codes without leaking row data."""
    text = str(exc).lower()
    if any(marker in text for marker in _LOCKED_MARKERS):
        code = _DB_LOCKED
    elif any(marker in text for marker in _CORRUPT_MARKERS):
        code = _DB_CORRUPT
    else:
        code = _DB_UNAVAILABLE
    reason = str(exc).strip()[:200]
    message = "the database could not be read" + (f": {reason}" if reason else "")
    return {"code": code, "message": message}


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _latest(*values: str | None) -> str | None:
    """Return the newest parseable ISO timestamp, preserving its stored spelling."""
    best: tuple[datetime, str] | None = None
    for value in values:
        if not value:
            continue
        parsed = _parse_iso(value)
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, value)
    return None if best is None else best[1]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _sort_by_activity(sites: list[SiteSummary]) -> None:
    """Newest activity first; sites without activity keep a deterministic path order."""
    sites.sort(key=lambda site: site.config_path)
    sites.sort(
        key=lambda site: _parse_iso(site.last_activity_at) or _EPOCH,
        reverse=True,
    )
