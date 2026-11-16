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

import contextlib
import gzip
import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Final, cast

from lv24_seo.core.timeutil import utc_now
from lv24_seo.store.db import connect, initialize
from lv24_seo.store.errors import translate_sqlite_error

__all__ = ["TASK_STATES", "TASK_TRANSITIONS", "Store", "canonical_json", "content_hash"]

JsonObject = dict[str, Any]

#: Closed task-state set (F2/F7). ``verified`` means a human/agent checked the fix; a later
#: observation of the same finding reopens it, which is how a regression is proven.
TASK_STATES: Final[tuple[str, ...]] = ("open", "in_progress", "verified", "dismissed")

#: Allowed moves. Same-state updates are accepted as a no-op; anything else raises ValueError.
#: ``verified`` can only leave to ``open`` (regressed) or ``dismissed`` (accepted as-is).
TASK_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "open": frozenset({"in_progress", "verified", "dismissed"}),
        "in_progress": frozenset({"open", "verified", "dismissed"}),
        "verified": frozenset({"open", "dismissed"}),
        "dismissed": frozenset({"open", "in_progress"}),
    }
)


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


def _task_row(row: sqlite3.Row) -> JsonObject:
    item = _row_to_dict(row)
    raw_payload = cast(str | None, item.pop("payload_json"))
    item["payload"] = {} if raw_payload is None else _decode_json(raw_payload)
    return item


def _indexing_event_row(row: sqlite3.Row) -> JsonObject:
    item = _row_to_dict(row)
    raw_detail = cast(str | None, item.pop("detail_json"))
    item["detail"] = {} if raw_detail is None else _decode_json(raw_detail)
    return item


def _task_identity(
    kind: str, property_id: str, url: str, rule_id: str
) -> tuple[str, str, str, str]:
    for name, value in (
        ("kind", kind),
        ("property_id", property_id),
        ("url", url),
        ("rule_id", rule_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    return (kind, property_id, url, rule_id)


def _indexing_event_identity(
    provider: str, property_id: str, url: str, event: str, event_date: date | str
) -> tuple[str, str, str, str, str]:
    for name, value in (
        ("provider", provider),
        ("property_id", property_id),
        ("url", url),
        ("event", event),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    return (provider, property_id, url, event, _day(event_date, name="event_date"))


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
        self._batch_depth = 0
        try:
            conn = connect(path)
            try:
                initialize(conn, path=self.path)
            except BaseException:
                conn.close()
                raise
        except sqlite3.Error as exc:
            # ``connect``/``initialize`` translate their own failures; this guard covers a
            # raw error raised between them (or by a future schema step) with the real path.
            translated = translate_sqlite_error(exc, self.path)
            if translated is None:
                raise
            raise translated from exc
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

    @contextlib.contextmanager
    def atomic(self) -> Iterator[None]:
        """Run a block of writes as one transaction (``BEGIN IMMEDIATE``).

        Related multi-write operations (a provider window sync, a crawl persist)
        must commit or roll back together: a crash mid-window must not leave a
        half-synced period behind. Nested ``atomic()`` blocks join the outer
        transaction; single writes outside ``atomic()`` keep their own commit.
        """
        if self._batch_depth > 0:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            translated = translate_sqlite_error(exc, self.path)
            if translated is None:
                raise
            raise translated from exc
        self._batch_depth += 1
        try:
            try:
                yield
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()
        except sqlite3.Error as exc:
            translated = translate_sqlite_error(exc, self.path)
            if translated is None:
                raise
            raise translated from exc
        finally:
            self._batch_depth -= 1

    @contextlib.contextmanager
    def _write_tx(self) -> Iterator[None]:
        try:
            if self._batch_depth > 0:
                yield
            else:
                with self.conn:
                    yield
        except sqlite3.Error as exc:
            translated = translate_sqlite_error(exc, self.path)
            if translated is None:
                raise
            raise translated from exc

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
        with self._write_tx():
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
        with self._write_tx():
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
        include_legacy: bool = False,
    ) -> list[JsonObject]:
        """True daily rows; opt in to legacy rows whose granularity is unknown.

        Older releases dated whole-window GSC aggregates at the window end.
        Migration 2 preserves those rows with an explicit accuracy marker; they
        must not enter a daily series unless a caller requests legacy data.
        """
        params: dict[str, object] = {
            "property_id": property_id,
            "source": source,
            "scope": scope,
            "start": None if start is None else _day(start, name="start"),
            "end": None if end is None else _day(end, name="end"),
            "include_legacy": include_legacy,
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
              AND (:include_legacy OR accuracy IS NULL OR accuracy != 'legacy_unknown_granularity')
            ORDER BY "date", source, "scope", "key", metric
            """,
            params,
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    # -- window aggregates --------------------------------------------------------------------

    def upsert_metric_window(
        self,
        property_id: str,
        source: str,
        start: date | str,
        end: date | str,
        *,
        scope: str = "total",
        key: str = "",
        metric: str,
        value: float,
        accuracy: str | None = None,
    ) -> None:
        """Insert or overwrite one window-aggregate metric value.

        Window aggregates (a weekly GSC query total, a Metrika visit count) carry
        their real ``[start, end]`` window in the key, so overlapping windows never
        overwrite each other and a window can never masquerade as a daily row.
        """
        with self._write_tx():
            self.conn.execute(
                """
                INSERT INTO metric_windows (
                    property_id, source, start, "end", "scope", "key", metric, value, accuracy
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (property_id, source, start, "end", "scope", "key", metric)
                DO UPDATE SET
                    value    = excluded.value,
                    accuracy = excluded.accuracy
                """,
                (
                    property_id,
                    source,
                    _day(start, name="start"),
                    _day(end, name="end"),
                    scope,
                    key,
                    metric,
                    float(value),
                    accuracy,
                ),
            )

    def get_metric_windows(
        self,
        property_id: str,
        *,
        source: str | None = None,
        scope: str | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> list[JsonObject]:
        """Window aggregates for a property, optionally filtered by the window bounds."""
        params: dict[str, object] = {
            "property_id": property_id,
            "source": source,
            "scope": scope,
            "start": None if start is None else _day(start, name="start"),
            "end": None if end is None else _day(end, name="end"),
        }
        rows = self.conn.execute(
            """
            SELECT property_id, source, start, "end", "scope", "key", metric, value, accuracy
            FROM metric_windows
            WHERE property_id = :property_id
              AND (:source IS NULL OR source = :source)
              AND (:scope  IS NULL OR "scope" = :scope)
              AND (:start  IS NULL OR "end" >= :start)
              AND (:end    IS NULL OR start <= :end)
            ORDER BY start, "end", source, "scope", "key", metric
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
        with self._write_tx():
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
            (property_id, max(0, int(limit))),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def record_gsc_sighting(
        self,
        property_id: str,
        url: str,
        *,
        seen_at: datetime | str | None = None,
    ) -> None:
        """Record that GSC search analytics mentioned ``url`` without touching crawl truth.

        A read-only analytics row must never overwrite crawl observations: an existing
        page keeps its ``status``/``index_state``/``canonical``/``title_hash`` and only
        ``last_seen`` advances. A never-crawled URL gains a ``gsc_search_analytics``
        placeholder row so the sighting is not lost.
        """
        stamp = _timestamp(seen_at, name="seen_at")
        with self._write_tx():
            self.conn.execute(
                """
                INSERT INTO pages (
                    property_id, url, status, index_state, canonical, title_hash,
                    first_seen, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (property_id, url) DO UPDATE SET
                    last_seen = excluded.last_seen
                """,
                (
                    property_id,
                    url,
                    0,
                    "gsc_search_analytics",
                    None,
                    None,
                    stamp,
                    stamp,
                ),
            )

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
        with self._write_tx():
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
            "limit": max(0, int(limit)),
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
        with self._write_tx():
            cursor = self.conn.execute(
                "UPDATE issues SET state = ? WHERE id = ?", (state, issue_id)
            )
        if cursor.rowcount == 0:
            raise KeyError(f"issue {issue_id} not found")

    # -- indexing events ------------------------------------------------------------------------

    def upsert_indexing_event(
        self,
        provider: str,
        property_id: str,
        url: str,
        event: str,
        event_date: date | str,
        *,
        detail: JsonObject | None = None,
        seen_at: datetime | str | None = None,
    ) -> str:
        """Record one provider-reported indexing event; return ``"new"`` or ``"existing"``.

        ``(provider, property_id, url, event, event_date)`` is the identity, so replaying a
        provider sync never duplicates an event. ``first_seen`` is the first import and never
        moves; ``last_seen`` only advances. ``detail`` refreshes the stored payload when
        supplied, and a repeat without detail keeps the original evidence instead of erasing it.
        """
        identity = _indexing_event_identity(provider, property_id, url, event, event_date)
        stamp = _timestamp(seen_at, name="seen_at")
        detail_json = None if detail is None else canonical_json(detail)
        # BEGIN IMMEDIATE: the SELECT-then-write must not race a concurrent sync of the same
        # event (unique-key violation instead of idempotency). Nested callers join a batch.
        with self.atomic():
            existing = self.conn.execute(
                """
                SELECT 1
                FROM indexing_events
                WHERE provider = ? AND property_id = ? AND url = ? AND event = ?
                  AND event_date = ?
                """,
                identity,
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO indexing_events (
                        provider, property_id, url, event, event_date, detail_json,
                        first_seen, last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*identity, detail_json, stamp, stamp),
                )
                return "new"
            self.conn.execute(
                """
                UPDATE indexing_events
                SET last_seen = MAX(last_seen, ?),
                    detail_json = COALESCE(?, detail_json)
                WHERE provider = ? AND property_id = ? AND url = ? AND event = ?
                  AND event_date = ?
                """,
                (stamp, detail_json, *identity),
            )
        return "existing"

    def list_indexing_events(
        self,
        *,
        property_id: str | None = None,
        provider: str | None = None,
        url: str | None = None,
        event: str | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
        limit: int = 1000,
    ) -> list[JsonObject]:
        """Indexing events newest first, optionally filtered and always bounded.

        ``start``/``end`` bound ``event_date`` inclusively. ``detail`` is decoded back into an
        object (``{}`` when the provider sent none), so callers never see the ``detail_json``
        column name.
        """
        params: dict[str, object] = {
            "property_id": property_id,
            "provider": provider,
            "url": url,
            "event": event,
            "start": None if start is None else _day(start, name="start"),
            "end": None if end is None else _day(end, name="end"),
            "limit": max(0, int(limit)),
        }
        rows = self.conn.execute(
            """
            SELECT provider, property_id, url, event, event_date, detail_json,
                   first_seen, last_seen
            FROM indexing_events
            WHERE (:property_id IS NULL OR property_id = :property_id)
              AND (:provider IS NULL OR provider = :provider)
              AND (:url      IS NULL OR url = :url)
              AND (:event    IS NULL OR event = :event)
              AND (:start    IS NULL OR event_date >= :start)
              AND (:end      IS NULL OR event_date <= :end)
            ORDER BY event_date DESC, url, event, provider
            LIMIT :limit
            """,
            params,
        ).fetchall()
        return [_indexing_event_row(row) for row in rows]

    def count_indexing_events(
        self,
        *,
        property_id: str | None = None,
        provider: str | None = None,
        url: str | None = None,
        event: str | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> int:
        """Number of indexing events matching the same filters as :meth:`list_indexing_events`.

        A bounded list cannot tell "exactly the cap" from "truncated"; the digest asks this
        once when it hits the cap so the limitation it prints is true.
        """
        params: dict[str, object] = {
            "property_id": property_id,
            "provider": provider,
            "url": url,
            "event": event,
            "start": None if start is None else _day(start, name="start"),
            "end": None if end is None else _day(end, name="end"),
        }
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM indexing_events
            WHERE (:property_id IS NULL OR property_id = :property_id)
              AND (:provider IS NULL OR provider = :provider)
              AND (:url      IS NULL OR url = :url)
              AND (:event    IS NULL OR event = :event)
              AND (:start    IS NULL OR event_date >= :start)
              AND (:end      IS NULL OR event_date <= :end)
            """,
            params,
        ).fetchone()
        return 0 if row is None else int(row["total"])

    # -- tasks (finding lifecycle) --------------------------------------------------------------

    def upsert_task(
        self,
        kind: str,
        property_id: str,
        url: str,
        rule_id: str,
        *,
        payload: JsonObject | None = None,
        state: str = "open",
        seen_at: datetime | str | None = None,
    ) -> str:
        """Record one observation of a finding as a task; return what happened.

        Returns ``"created"`` on first sight, ``"existing"`` when the task is already tracked,
        and ``"reopened"`` when a ``verified`` task was observed again -- the recurrence is a
        proven regression. ``dismissed`` tasks stay dismissed: only an explicit
        :meth:`update_task_state` brings them back.

        A repeated observation never resets triage: the original ``state`` and ``created_at``
        are preserved. Supplying ``payload`` refreshes the evidence and advances ``updated_at``;
        calling again without a payload is a true no-op.
        """
        identity = _task_identity(kind, property_id, url, rule_id)
        if state not in TASK_STATES:
            raise ValueError(
                f"unknown task state {state!r}; expected one of {', '.join(TASK_STATES)}"
            )
        stamp = _timestamp(seen_at, name="seen_at")
        payload_json = None if payload is None else canonical_json(payload)
        # BEGIN IMMEDIATE, not a deferred transaction: the SELECT-then-INSERT must not race a
        # concurrent crawl that observes the same finding (unique-key violation instead of
        # idempotency). Nested callers join their outer batch via ``atomic()``.
        with self.atomic():
            existing = self.conn.execute(
                """
                SELECT id, state
                FROM tasks
                WHERE kind = ? AND property_id = ? AND url = ? AND rule_id = ?
                """,
                identity,
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO tasks (
                        kind, property_id, url, rule_id, state, payload_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*identity, state, payload_json, stamp, stamp),
                )
                return "created"
            if payload_json is not None:
                self.conn.execute(
                    "UPDATE tasks SET payload_json = ?, updated_at = ? WHERE id = ?",
                    (payload_json, stamp, existing["id"]),
                )
            reopened = existing["state"] == "verified"
            if reopened:
                self.conn.execute(
                    "UPDATE tasks SET state = 'open', updated_at = ? WHERE id = ?",
                    (stamp, existing["id"]),
                )
        return "reopened" if reopened else "existing"

    def list_tasks(
        self,
        *,
        property_id: str | None = None,
        kind: str | None = None,
        state: str | None = None,
        url: str | None = None,
        rule_id: str | None = None,
        limit: int = 1000,
    ) -> list[JsonObject]:
        """Tasks newest first, optionally filtered; ``state=None`` returns every state.

        An unknown ``state`` raises :class:`ValueError` instead of silently returning an empty
        list, so a typo in a filter cannot be mistaken for "no tasks".
        """
        if state is not None and state not in TASK_STATES:
            raise ValueError(
                f"unknown task state {state!r}; expected one of {', '.join(TASK_STATES)}"
            )
        params: dict[str, object] = {
            "property_id": property_id,
            "kind": kind,
            "state": state,
            "url": url,
            "rule_id": rule_id,
            "limit": max(0, int(limit)),
        }
        rows = self.conn.execute(
            """
            SELECT id, kind, property_id, url, rule_id, state, payload_json,
                   created_at, updated_at
            FROM tasks
            WHERE (:property_id IS NULL OR property_id = :property_id)
              AND (:kind   IS NULL OR kind = :kind)
              AND (:state  IS NULL OR state = :state)
              AND (:url    IS NULL OR url = :url)
              AND (:rule_id IS NULL OR rule_id = :rule_id)
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit
            """,
            params,
        ).fetchall()
        return [_task_row(row) for row in rows]

    def count_tasks(
        self,
        *,
        property_id: str | None = None,
        kind: str | None = None,
        state: str | None = None,
        url: str | None = None,
        rule_id: str | None = None,
    ) -> int:
        """Number of tasks matching the same filters as :meth:`list_tasks`.

        The WHERE clause is kept in sync with :meth:`list_tasks` deliberately: two call sites
        are not worth a shared SQL builder, and an f-string query would weaken the audit trail.
        """
        if state is not None and state not in TASK_STATES:
            raise ValueError(
                f"unknown task state {state!r}; expected one of {', '.join(TASK_STATES)}"
            )
        params: dict[str, object] = {
            "property_id": property_id,
            "kind": kind,
            "state": state,
            "url": url,
            "rule_id": rule_id,
        }
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM tasks
            WHERE (:property_id IS NULL OR property_id = :property_id)
              AND (:kind   IS NULL OR kind = :kind)
              AND (:state  IS NULL OR state = :state)
              AND (:url    IS NULL OR url = :url)
              AND (:rule_id IS NULL OR rule_id = :rule_id)
            """,
            params,
        ).fetchone()
        return 0 if row is None else int(row["total"])

    def update_task_state(self, task_id: int, state: str) -> JsonObject:
        """Move one task to ``state`` and return the updated row.

        Only :data:`TASK_TRANSITIONS` moves are accepted; re-applying the current state is a
        no-op so a retried CLI call stays idempotent. An unknown id raises :class:`KeyError`
        and an invalid move raises :class:`ValueError`: silently ignoring either would let a
        report claim a fix that never happened.
        """
        if state not in TASK_STATES:
            raise ValueError(
                f"unknown task state {state!r}; expected one of {', '.join(TASK_STATES)}"
            )
        # Read and write in one IMMEDIATE transaction: two processes triaging the same task
        # must not both validate against a stale ``current`` and leave an impossible state.
        with self.atomic():
            row = self.conn.execute(
                """
                SELECT id, kind, property_id, url, rule_id, state, payload_json,
                       created_at, updated_at
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task {task_id} not found")
            current = str(row["state"])
            if current == state:
                return _task_row(row)
            allowed = TASK_TRANSITIONS[current]
            if state not in allowed:
                raise ValueError(
                    f"invalid task transition {current} -> {state}; "
                    f"allowed from {current}: {', '.join(sorted(allowed))}"
                )
            self.conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (state, _now_iso(), task_id),
            )
            updated = self.conn.execute(
                """
                SELECT id, kind, property_id, url, rule_id, state, payload_json,
                       created_at, updated_at
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if updated is None:  # pragma: no cover - the row cannot vanish inside the transaction
                raise KeyError(f"task {task_id} not found")
        return _task_row(updated)

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
        with self._write_tx():
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

    def list_snapshot_periods(self, kind: str, property_id: str, *, limit: int = 20) -> list[str]:
        """Stored snapshot periods for ``kind``/``property_id``, newest period first."""
        rows = self.conn.execute(
            """
            SELECT period
            FROM snapshots
            WHERE kind = ? AND property_id = ?
            ORDER BY period DESC
            LIMIT ?
            """,
            (kind, property_id, max(0, int(limit))),
        ).fetchall()
        return [str(row["period"]) for row in rows]

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
        with self._write_tx():
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

    def mark_send_sent(
        self,
        kind: str,
        property_id: str,
        period: str,
        content_hash: str,
        channel: str,
        *,
        sent_at: datetime | str | None = None,
    ) -> bool:
        """Promote a ``pending`` claim to ``sent``; returns ``False`` when nothing was pending.

        Only a pending row is updated, so a completed send is never rewritten by a late retry.
        """
        with self._write_tx():
            cursor = self.conn.execute(
                """
                UPDATE sends
                SET status = 'sent', sent_at = ?
                WHERE kind = ? AND property_id = ? AND period = ?
                  AND channel = ? AND content_hash = ? AND status = 'pending'
                """,
                (
                    _timestamp(sent_at, name="sent_at"),
                    kind,
                    property_id,
                    period,
                    channel,
                    content_hash,
                ),
            )
        return cursor.rowcount == 1

    def release_send(
        self, kind: str, property_id: str, period: str, content_hash: str, channel: str
    ) -> bool:
        """Drop a ``pending`` claim after a failed write; a ``sent`` row is never deleted.

        Returns ``True`` when a pending claim was released.
        """
        with self._write_tx():
            cursor = self.conn.execute(
                """
                DELETE FROM sends
                WHERE kind = ? AND property_id = ? AND period = ?
                  AND channel = ? AND content_hash = ? AND status = 'pending'
                """,
                (kind, property_id, period, channel, content_hash),
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
        with self._write_tx():
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
        """Retention entry point for on-disk raw provider archives (not yet implemented).

        Raw provider responses were never written to disk by any shipped command, so
        there is nothing to delete: this method returns ``0`` and logs that the
        ``raw_retention_days`` setting currently has no effect. Keeping the method
        (instead of removing the setting) preserves the call sites that will own
        the real implementation; claiming deletions here would be dishonest.
        """
        logger = logging.getLogger(__name__)
        logger.warning(
            "raw retention is not implemented: no raw archives are written, "
            "raw_retention_days is ignored"
        )
        return 0
