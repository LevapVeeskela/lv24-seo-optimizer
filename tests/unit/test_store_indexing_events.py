"""Indexing-event storage (0.7.0): migration, idempotent upsert, filters and bounds."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lv24_seo.store import Store
from lv24_seo.store.db import SCHEMA_VERSION, migrate

INDEXING_EVENTS_MIGRATION_VERSION = 4


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(tmp_path / "seo.db")
    try:
        yield instance
    finally:
        instance.close()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _versions(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [int(row["version"]) for row in rows]


def _add(store: Store, **overrides: object) -> str:
    fields: dict[str, object] = {
        "provider": "yandex_webmaster",
        "property_id": "p1",
        "url": "https://example.com/a",
        "event": "noindex_added",
        "event_date": "2026-11-05",
    }
    fields.update(overrides)
    return store.upsert_indexing_event(**fields)  # type: ignore[arg-type]


def test_schema_version_covers_the_indexing_events_migration() -> None:
    assert SCHEMA_VERSION >= INDEXING_EVENTS_MIGRATION_VERSION


def test_fresh_database_has_indexing_events_and_the_full_ledger(store: Store) -> None:
    assert "indexing_events" in _table_names(store.conn)
    assert _versions(store.conn) == list(range(1, SCHEMA_VERSION + 1))
    indexes = {
        row["name"]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {"idx_indexing_events_property_date", "idx_indexing_events_property_url"} <= indexes


def test_existing_database_gains_indexing_events_without_losing_data(store: Store) -> None:
    store.upsert_property("gsc", "p", "P")
    store.conn.execute("DROP TABLE indexing_events")
    store.conn.execute(
        "DELETE FROM schema_migrations WHERE version = ?", (INDEXING_EVENTS_MIGRATION_VERSION,)
    )
    store.conn.commit()

    # migrate() alone must rebuild the table: initialize() would also run schema.sql.
    assert migrate(store.conn) == [INDEXING_EVENTS_MIGRATION_VERSION]
    assert "indexing_events" in _table_names(store.conn)
    assert store.list_properties()[0]["property_id"] == "p"
    assert _versions(store.conn) == list(range(1, SCHEMA_VERSION + 1))
    assert migrate(store.conn) == []


def test_schema_enforces_the_unique_identity(store: Store) -> None:
    _add(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            """
            INSERT INTO indexing_events (
                provider, property_id, url, event, event_date, first_seen, last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "yandex_webmaster",
                "p1",
                "https://example.com/a",
                "noindex_added",
                "2026-11-05",
                "2026-11-06T00:00:00+00:00",
                "2026-11-06T00:00:00+00:00",
            ),
        )
    store.conn.rollback()


def test_upsert_is_idempotent_and_keeps_first_seen(store: Store) -> None:
    first = _add(store, detail={"status": "excluded"}, seen_at="2026-11-05T00:00:00+00:00")
    assert first == "new"
    [event] = store.list_indexing_events(property_id="p1")
    assert event["first_seen"] == "2026-11-05T00:00:00+00:00"
    assert event["last_seen"] == "2026-11-05T00:00:00+00:00"
    assert event["detail"] == {"status": "excluded"}

    assert _add(store, seen_at="2026-11-07T00:00:00+00:00") == "existing"
    [event] = store.list_indexing_events(property_id="p1")
    assert event["first_seen"] == "2026-11-05T00:00:00+00:00"
    assert event["last_seen"] == "2026-11-07T00:00:00+00:00"
    # A repeat without detail keeps the stored evidence instead of erasing it.
    assert event["detail"] == {"status": "excluded"}

    assert (
        _add(store, detail={"status": "indexed"}, seen_at="2026-11-08T00:00:00+00:00") == "existing"
    )
    [event] = store.list_indexing_events(property_id="p1")
    assert event["detail"] == {"status": "indexed"}
    assert event["last_seen"] == "2026-11-08T00:00:00+00:00"
    assert store.count_indexing_events() == 1


def test_last_seen_never_moves_backwards(store: Store) -> None:
    _add(store, seen_at="2026-11-09T00:00:00+00:00")
    _add(store, seen_at="2026-11-06T00:00:00+00:00")

    [event] = store.list_indexing_events(property_id="p1")

    assert event["first_seen"] == "2026-11-09T00:00:00+00:00"
    assert event["last_seen"] == "2026-11-09T00:00:00+00:00"


def test_identity_separates_provider_property_url_event_and_day(store: Store) -> None:
    _add(store)
    _add(store, provider="bing")
    _add(store, property_id="p2")
    _add(store, url="https://example.com/b")
    _add(store, event="indexed")
    _add(store, event_date="2026-11-06")

    assert store.count_indexing_events() == 6


def test_accepts_dates_and_aware_datetimes(store: Store) -> None:
    _add(
        store,
        event_date=date(2026, 11, 5),
        seen_at=datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
    )

    [event] = store.list_indexing_events(property_id="p1")

    assert event["event_date"] == "2026-11-05"
    assert event["first_seen"] == "2026-11-05T12:00:00+00:00"


@pytest.mark.parametrize("field", ["provider", "property_id", "url", "event"])
def test_empty_identity_fields_are_rejected(store: Store, field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be a non-empty string"):
        _add(store, **{field: "   "})


def test_naive_seen_at_is_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _add(store, seen_at=datetime(2026, 11, 5, 12, 0))


def test_list_filters_and_bounds(store: Store) -> None:
    _add(store, url="https://example.com/a", event="noindex_added", event_date="2026-11-01")
    _add(store, url="https://example.com/b", event="indexed", event_date="2026-11-03")
    _add(
        store,
        provider="bing",
        url="https://example.com/c",
        event="noindex_added",
        event_date="2026-11-05",
    )
    _add(
        store,
        property_id="p2",
        url="https://example.com/d",
        event="indexed",
        event_date="2026-11-07",
    )

    assert [row["url"] for row in store.list_indexing_events(property_id="p1")] == [
        "https://example.com/c",
        "https://example.com/b",
        "https://example.com/a",
    ]
    assert [row["url"] for row in store.list_indexing_events(provider="bing")] == [
        "https://example.com/c"
    ]
    assert [row["url"] for row in store.list_indexing_events(url="https://example.com/b")] == [
        "https://example.com/b"
    ]
    assert [
        row["url"] for row in store.list_indexing_events(property_id="p1", event="indexed")
    ] == ["https://example.com/b"]
    assert [
        row["url"]
        for row in store.list_indexing_events(
            property_id="p1", start="2026-11-03", end="2026-11-05"
        )
    ] == ["https://example.com/c", "https://example.com/b"]
    assert store.list_indexing_events(property_id="p1", limit=1)[0]["url"].endswith("/c")
    assert store.list_indexing_events(limit=0) == []
    assert store.list_indexing_events(limit=-1) == []

    assert store.count_indexing_events() == 4
    assert store.count_indexing_events(property_id="p1") == 3
    assert store.count_indexing_events(provider="bing") == 1
    assert store.count_indexing_events(event="indexed") == 2
    assert store.count_indexing_events(start="2026-11-03", end="2026-11-05") == 2
    assert store.count_indexing_events(start=date(2026, 11, 6)) == 1


def test_detail_defaults_to_an_empty_object(store: Store) -> None:
    _add(store)

    [event] = store.list_indexing_events(property_id="p1")

    assert event["detail"] == {}
    assert "detail_json" not in event


def test_concurrent_upsert_of_the_same_event_creates_one_row(tmp_path: Path) -> None:
    path = tmp_path / "seo.db"
    workers = 4
    barrier = threading.Barrier(workers)

    def worker(_index: int) -> str:
        with Store(path) as instance:
            barrier.wait(timeout=30)
            return instance.upsert_indexing_event(
                "yandex_webmaster",
                "p1",
                "https://example.com/a",
                "noindex_added",
                "2026-11-05",
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(worker, range(workers)))

    assert sorted(outcomes) == ["existing", "existing", "existing", "new"]
    with Store(path) as instance:
        assert instance.count_indexing_events() == 1
        assert instance.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
