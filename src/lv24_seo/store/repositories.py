"""Repositories for every Wave 2 table.

Design notes
------------
* One :class:`Store` owns one connection. Methods are short, explicit SQL; there is no ORM and
  no global state, so a CLI command, an MCP tool and a test can share the class safely.
* Writes run inside ``with self.conn:`` blocks: a failing multi-statement operation rolls back
  instead of leaving a half-updated issue or snapshot.
* JSON columns are stored **canonicalised** (``sort_keys=True, separators=(",", ":")``) so the
  same payload always yields the same SHA-256 -- that is what makes ``only_on_change`` delivery
  reliable. ``list_*`` methods decode JSON columns back into Python values (``config``,
  ``detail``) instead of leaking ``config_json`` / ``detail_json`` names to callers.
* Timestamps are ISO-8601 UTC. Passing a timezone-aware ``datetime`` stores it converted to UTC;
  passing a string stores it verbatim (the caller is expected to use ISO-8601 UTC, as providers
  and report builders do). Naive datetimes are rejected: the project forbids ambiguous times.
* :meth:`Store.prune_raw` is deliberately a no-op in Wave 2. Raw gzip archives on disk and their
  retention policy land in WP2.3; implementing deletion here would silently break that work.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from lv24_seo.core.timeutil import utc_now
from lv24_seo.store.db import connect, initialize

__all__ = ["Store", "canonical_json", "content_hash"]

JsonObject = dict[str, Any]


def canonical_json(payload: object) -> str:
    """Serialise ``payload`` as compact, key-sorted, strict JSON.

    ``allow_nan=False`` keeps the output valid JSON (NaN/Infinity are not) and guarantees that
    two equal payloads produce byte-identical text, which the snapshot hash depends on.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(payload: object) -> str:
    """SHA-256 hex digest of the canonical JSON form of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def _timestamp(value: datetime | str | None, *, name: str) -> str:
    if value is None:
        return _now_iso()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware, got a naive datetime")
        return value.astimezone(UTC).isoformat(timespec="seconds")
    if isinstance(value, str) and value:
        return value
    raise TypeError(f"{name} must be an ISO-8601 string or an aware datetime")


def _day(value: date | str, *, name: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    raise TypeError(f"{name} must be a YYYY-MM-DD string or a date")


def _row_to_dict(row: sqlite3.Row) -> JsonObject:
    # `.keys()` is required: iterating a Row yields values, not column names (SIM118 is wrong here).
    return {column: row[column] for column in row.keys()}  # noqa: SIM118


def _decode_json(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


class Store:
    """SQLite-backed persistence for providers, metrics, crawl results and delivery state.

    The constructor opens the database (creating parent directories), applies the PRAGMAs and
    runs the idempotent schema bootstrap, so a valid ``Store`` is always ready to use::

        with Store("data/seo.db") as store:
            store.upsert_property("gsc", "sc-domain:lv24.by", "LV24")

    Use it as a context manager, or call :meth:`close` explicitly. The connection is not
    thread-safe; give each thread (or worker thread pool slot) its own ``Store``.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._closed = False
        conn = connect(path)
        try:
            initialize(conn)
        except BaseException:
            conn.close()
            raise
        self.conn: sqlite3.Connection = conn

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection; repeated calls are no-ops."""
        if self._closed:
            return
        self.conn.close()
        self._closed = True

    # -- properties ---------------------------------------------------------------------------

    def upsert_property(
        self,
        provider: str,
        property_id: str,
        display_name: str,
        *,
        host_id: str | None = None,
        counter_id: str | None = None,
        verified: bool = True,
        config: JsonObject | None = None,
    ) -> None:
        """Insert or update one property; ``config`` replaces the stored JSON (``None`` clears)."""
        config_json = None if config is None else canonical_json(config)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO properties (
                    provider, property_id, display_name, host_id, counter_id, verified, config_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, property_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    host_id      = excluded.host_id,
                    counter_id   = excluded.counter_id,
                    verified     = excluded.verified,
                    config_json  = excluded.config_json
                """,
                (
                    provider,
                    property_id,
                    display_name,
                    host_id,
                    counter_id,
                    int(verified),
                    config_json,
                ),
            )

    def list_properties(self) -> list[JsonObject]:
        """All properties, ordered by ``(provider, property_id)``.

        ``verified`` is returned as ``bool`` and ``config`` as the decoded object (or ``None``).
        """
        rows = self.conn.execute(
            """
            SELECT provider, property_id, display_name, host_id, counter_id, verified, config_json
            FROM properties
            ORDER BY provider, property_id
            """
        ).fetchall()
        properties: list[JsonObject] = []
        for row in rows:
            item = _row_to_dict(row)
            item["verified"] = bool(item["verified"])
            item["config"] = _decode_json(cast(str | None, item.pop("config_json")))
            properties.append(item)
        return properties

    # -- daily metrics ------------------------------------------------------------------------

    def upsert_metric_daily(
        self,
        property_id: str,
        source: str,
        date: date | str,
        *,
        scope: str = "total",
        key: str = "",
        metric: str,
        value: float,
        accuracy: str | None = None,
    ) -> None:
        """Insert or overwrite one daily metric value.

        The natural key is ``(property_id, source, date, scope, key, metric)``; re-syncing the
        same day replaces the value instead of duplicating the row.
        """
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO metric_daily (
                    property_id, source, "date", "scope", "key", metric, value, accuracy
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (property_id, source, "date", "scope", "key", metric) DO UPDATE SET
                    value    = excluded.value,
                    accuracy = excluded.accuracy
                """,
                (
                    property_id,
                    source,
                    _day(date, name="date"),
                    scope,
                    key,
                    metric,
                    float(value),
                    accuracy,
                ),
            )

    def get_metric_daily(
        self,
        property_id: str,
        *,
        source: str | None = None,
        scope: str | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[JsonObject]:
        """Metrics for a property, optionally filtered by source, scope and an inclusive range."""
        params: dict[str, object] = {
            "property_id": property_id,
            "source": source,
            "scope": scope,
            "start": None if start is None else _day(start, name="start"),
            "end": None if end is None else _day(end, name="end"),
        }
        rows = self.conn.execute(
            """
            SELECT property_id, source, "date", "scope", "key", metric, value, accuracy
            FROM metric_daily
            WHERE property_id = :property_id
              AND (:source IS NULL OR source = :source)
              AND (:scope  IS NULL OR "scope" = :scope)
              AND (:start  IS NULL OR "date" >= :start)
              AND (:end    IS NULL OR "date" <= :end)
            ORDER BY "date", source, "scope", "key", metric
            """,
            params,
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # -- crawled pages ------------------------------------------------------------------------

    def upsert_page(
        self,
        property_id: str,
        url: str,
        *,
        status: int,
        index_state: str | None = None,
        canonical: str | None = None,
        title_hash: str | None = None,
        seen_at: datetime | str | None = None,
    ) -> None:
        """Record one crawl observation; the first observation keeps ``first_seen`` forever."""
        stamp = _timestamp(seen_at, name="seen_at")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO pages (
                    property_id, url, status, index_state, canonical, title_hash,
                    first_seen, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (property_id, url) DO UPDATE SET
                    status      = excluded.status,
                    index_state = excluded.index_state,
                    canonical   = excluded.canonical,
                    title_hash  = excluded.title_hash,
                    last_seen   = excluded.last_seen
                """,
                (
                    property_id,
                    url,
                    status,
                    index_state,
                    canonical,
                    title_hash,
                    stamp,
                    stamp,
                ),
            )

    def list_pages(self, property_id: str, *, limit: int = 1000) -> list[JsonObject]:
        """Crawled pages for a property, ordered by URL."""
        rows = self.conn.execute(
            """
            SELECT property_id, url, status, index_state, canonical, title_hash,
                   first_seen, last_seen
            FROM pages
            WHERE property_id = ?
            ORDER BY url
            LIMIT ?
            """,
            (property_id, limit),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # -- issues -------------------------------------------------------------------------------

    def upsert_issue(
        self,
        rule_id: str,
        severity: str,
        property_id: str,
        url: str,
        detail: JsonObject | None = None,
        *,
        seen_at: datetime | str | None = None,
    ) -> str:
        """Record a rule violation and report what happened.

        Returns ``"new"`` on first sight, ``"reopened"`` when the issue was ``fixed`` and is
        still present, and ``"existing"`` otherwise. ``first_seen`` never moves; ``last_seen``,
        severity and ``detail`` follow the latest observation. A state such as ``ignored`` or
        ``accepted`` is preserved: only a confirmed fix is undone by a recurrence.
        """
        stamp = _timestamp(seen_at, name="seen_at")
        detail_json = None if detail is None else canonical_json(detail)
        with self.conn:
            existing = self.conn.execute(
                """
                SELECT first_seen, state
                FROM issues
                WHERE rule_id = ? AND property_id = ? AND url = ?
                """,
                (rule_id, property_id, url),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO issues (
                        rule_id, severity, property_id, url, detail_json,
                        first_seen, last_seen, state
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
                    """,
                    (rule_id, severity, property_id, url, detail_json, stamp, stamp),
                )
                return "new"
            self.conn.execute(
                """
                UPDATE issues
                SET severity = ?,
                    detail_json = ?,
                    last_seen = ?,
                    state = CASE WHEN state = 'fixed' THEN 'open' ELSE state END
                WHERE rule_id = ? AND property_id = ? AND url = ?
                """,
                (severity, detail_json, stamp, rule_id, property_id, url),
            )
            reopened = existing["state"] == "fixed"
        return "reopened" if reopened else "existing"

    def list_issues(
        self,
        property_id: str,
        *,
        state: str | None = "open",
        severity: str | None = None,
        limit: int = 1000,
    ) -> list[JsonObject]:
        """Issues for a property, newest first. ``state=None`` returns every state."""
        params: dict[str, object] = {
            "property_id": property_id,
            "state": state,
            "severity": severity,
            "limit": limit,
        }
        rows = self.conn.execute(
            """
            SELECT id, rule_id, severity, property_id, url, detail_json,
                   first_seen, last_seen, state
            FROM issues
            WHERE property_id = :property_id
              AND (:state    IS NULL OR state = :state)
              AND (:severity IS NULL OR severity = :severity)
            ORDER BY last_seen DESC, id
            LIMIT :limit
            """,
            params,
        ).fetchall()
        issues: list[JsonObject] = []
        for row in rows:
            item = _row_to_dict(row)
            raw_detail = cast(str | None, item.pop("detail_json"))
            item["detail"] = {} if raw_detail is None else _decode_json(raw_detail)
            issues.append(item)
        return issues

    def set_issue_state(self, issue_id: int, state: str) -> None:
        """Move an issue to ``state`` (``open``, ``fixed``, ``ignored``, ...).

        Raises :class:`KeyError` when the id does not exist: silently ignoring a typo would let
        a report claim that an issue was triaged when it was not.
        """
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE issues SET state = ? WHERE id = ?", (state, issue_id)
            )
        if cursor.rowcount == 0:
            raise KeyError(f"issue {issue_id} not found")

    # -- snapshots ----------------------------------------------------------------------------

    def save_snapshot(self, kind: str, property_id: str, period: str, payload: JsonObject) -> str:
        """Store a gzipped report payload and return its canonical SHA-256.

        The hash covers the **uncompressed canonical JSON**, so identical payloads compare equal
        regardless of dict insertion order or gzip metadata. Re-saving the same period replaces
        the row, which keeps one snapshot per ``(kind, property_id, period)``.
        """
        if not isinstance(payload, dict):
            raise TypeError("payload must be a JSON object")
        canonical = canonical_json(payload)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # mtime=0 removes a timestamp from the gzip header: the blob is reproducible too.
        blob = gzip.compress(canonical.encode("utf-8"), mtime=0)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO snapshots (
                    kind, property_id, period, content_hash, payload_gz, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (kind, property_id, period) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    payload_gz   = excluded.payload_gz,
                    created_at   = excluded.created_at
                """,
                (kind, property_id, period, digest, blob, _now_iso()),
            )
        return digest

    def load_snapshot(self, kind: str, property_id: str, period: str) -> JsonObject | None:
        """Decode a stored snapshot, or return ``None`` when the period has none."""
        row = self.conn.execute(
            """
            SELECT payload_gz
            FROM snapshots
            WHERE kind = ? AND property_id = ? AND period = ?
            """,
            (kind, property_id, period),
        ).fetchone()
        if row is None:
            return None
        decoded = json.loads(gzip.decompress(bytes(row["payload_gz"])))
        if not isinstance(decoded, dict):
            raise ValueError("stored snapshot payload is not a JSON object")
        return cast(JsonObject, decoded)

    # -- delivery de-duplication --------------------------------------------------------------

    def record_send(
        self,
        kind: str,
        property_id: str,
        period: str,
        content_hash: str,
        channel: str,
        *,
        status: str = "sent",
        sent_at: datetime | str | None = None,
    ) -> bool:
        """Claim a delivery slot. Returns ``False`` when this exact send already happened.

        The primary key includes ``content_hash``: unchanged content is never re-sent, while a
        changed report legitimately opens a new slot. A duplicate never overwrites the original
        row (its timestamp and status stay the audit trail of the first attempt).
        """
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO sends (
                    kind, property_id, period, content_hash, channel, sent_at, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (kind, property_id, period, channel, content_hash) DO NOTHING
                """,
                (
                    kind,
                    property_id,
                    period,
                    content_hash,
                    channel,
                    _timestamp(sent_at, name="sent_at"),
                    status,
                ),
            )
        return cursor.rowcount == 1

    def was_sent(
        self, kind: str, property_id: str, period: str, content_hash: str, channel: str
    ) -> bool:
        """``True`` when this exact ``(kind, property, period, channel, hash)`` was recorded."""
        row = self.conn.execute(
            """
            SELECT 1
            FROM sends
            WHERE kind = ? AND property_id = ? AND period = ?
              AND channel = ? AND content_hash = ?
            LIMIT 1
            """,
            (kind, property_id, period, channel, content_hash),
        ).fetchone()
        return row is not None

    # -- run journal --------------------------------------------------------------------------

    def record_run(
        self,
        kind: str,
        property_id: str | None,
        status: str,
        *,
        started_at: datetime | str | None = None,
        finished_at: datetime | str | None = None,
        stats: JsonObject | None = None,
    ) -> int:
        """Append a run row and return its id. ``stats`` is stored as canonical JSON."""
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO runs (
                    kind, property_id, started_at, finished_at, status, stats_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    property_id,
                    _timestamp(started_at, name="started_at"),
                    None if finished_at is None else _timestamp(finished_at, name="finished_at"),
                    status,
                    None if stats is None else canonical_json(stats),
                ),
            )
        run_id = cursor.lastrowid
        if run_id is None:
            raise RuntimeError("SQLite did not return an id for the recorded run")
        return int(run_id)

    # -- retention ----------------------------------------------------------------------------

    def prune_raw(self, *, before: datetime) -> int:
        """Reserved for WP2.3 raw-archive retention; intentionally deletes nothing today.

        Raw provider responses live on disk under ``data/raw/<source>/<date>/`` and are pruned by
        the retention job that owns those files. Snapshots are report history and must survive:
        deleting them here would break the "what changed since last send" comparison. The method
        exists so callers can wire the retention schedule now and get a real implementation later.
        """
        return 0
