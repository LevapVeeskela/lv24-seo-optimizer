"""Schema v5: indexing state, run history and local quota counters."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lv24_seo.store import Store
from lv24_seo.store.db import SCHEMA_VERSION, migrate

INDEXING_AUTOMATION_MIGRATION = 5


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(tmp_path / "seo.db")
    try:
        yield instance
    finally:
        instance.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _versions(conn: sqlite3.Connection) -> list[int]:
    return [
        int(row["version"])
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]


def test_schema_version_is_five_and_fresh_database_has_the_tables(store: Store) -> None:
    assert SCHEMA_VERSION == INDEXING_AUTOMATION_MIGRATION
    assert {"indexing_state", "indexing_runs", "quota_usage"} <= _tables(store.conn)
    assert _versions(store.conn) == list(range(1, SCHEMA_VERSION + 1))
    indexes = {
        str(row["name"])
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert {
        "idx_indexing_state_provider_check",
        "idx_indexing_state_provider_status",
        "idx_indexing_runs_provider_started",
        "idx_indexing_runs_started",
    } <= indexes


def test_v5_migration_is_idempotent_and_keeps_existing_data(store: Store) -> None:
    store.upsert_property("gsc", "sc-domain:example.com", "Example")
    store.conn.execute("DROP TABLE indexing_state")
    store.conn.execute("DROP TABLE indexing_runs")
    store.conn.execute("DROP TABLE quota_usage")
    store.conn.execute("DELETE FROM schema_migrations WHERE version = ?", (5,))
    store.conn.commit()

    assert migrate(store.conn) == [5]
    assert {"indexing_state", "indexing_runs", "quota_usage"} <= _tables(store.conn)
    assert store.list_properties()[0]["property_id"] == "sc-domain:example.com"
    assert _versions(store.conn) == list(range(1, SCHEMA_VERSION + 1))
    assert migrate(store.conn) == []


def test_state_upsert_preserves_fields_and_first_seen(store: Store) -> None:
    first = datetime(2026, 11, 1, tzinfo=UTC)
    store.ensure_indexing_state(
        "bing", "https://example.com", "https://example.com/a", first_seen=first
    )
    store.upsert_indexing_state(
        "bing",
        "https://example.com",
        "https://example.com/a",
        last_submitted_at=datetime(2026, 11, 2, tzinfo=UTC),
        submit_status="accepted",
        index_status="crawled",
        http_status=200,
        detail={"task": "one"},
    )
    store.upsert_indexing_state(
        "bing",
        "https://example.com",
        "https://example.com/a",
        last_checked_at=datetime(2026, 11, 3, tzinfo=UTC),
        coverage_state="Crawled",
        detail={"verdict": "PASS"},
    )

    row = store.get_indexing_state("bing", "https://example.com", "https://example.com/a")
    assert row is not None
    assert row["first_seen"] == "2026-11-01T00:00:00+00:00"
    assert row["last_submitted_at"] == "2026-11-02T00:00:00+00:00"
    assert row["last_checked_at"] == "2026-11-03T00:00:00+00:00"
    assert row["submit_status"] == "accepted"
    assert row["index_status"] == "crawled"
    assert row["http_status"] == 200
    assert row["coverage_state"] == "Crawled"
    assert row["detail"] == {"verdict": "PASS"}


def test_ensure_does_not_overwrite_existing_state(store: Store) -> None:
    store.upsert_indexing_state(
        "gsc", "sc-domain:example.com", "https://example.com/a", submit_status="accepted"
    )
    store.ensure_indexing_state(
        "gsc",
        "sc-domain:example.com",
        "https://example.com/a",
        first_seen="2020-01-01T00:00:00+00:00",
    )

    row = store.get_indexing_state("gsc", "sc-domain:example.com", "https://example.com/a")
    assert row is not None
    assert row["submit_status"] == "accepted"


def test_state_counts_and_filters(store: Store) -> None:
    for index, status in enumerate(("indexed", "not_indexed", "unknown", "crawled")):
        store.upsert_indexing_state(
            "gsc",
            "p",
            f"https://example.com/{index}",
            last_checked_at="2026-11-10T00:00:00+00:00",
            index_status=status,
        )
    counts = store.indexing_state_counts("gsc", "p")
    assert counts == {
        "urls": 4,
        "submitted": 0,
        "inspected": 4,
        "indexed": 1,
        "not_indexed": 1,
        "crawled": 1,
        "unknown": 1,
        "queued": 0,
        "stale": 4,
        "pending": 2,
    }
    assert store.count_indexing_state(provider="gsc", property_id="p") == 4
    assert store.count_indexing_state(provider="gsc", index_status="indexed") == 1
    assert len(store.list_indexing_state(provider="gsc", limit=2)) == 2


def test_run_history_round_trip(store: Store) -> None:
    run_id = store.insert_indexing_run(
        "bing",
        started_at="2026-11-17T04:00:00+00:00",
        planned=2,
        quota_before={"daily_remaining": 10},
        status="running",
    )
    store.finish_indexing_run(
        run_id,
        status="applied",
        finished_at="2026-11-17T04:00:05+00:00",
        submitted=2,
        checked=1,
        quota_after={"daily_remaining": 8},
        detail={"not_attempted": 0},
    )

    runs = store.list_indexing_runs(provider="bing")
    assert len(runs) == 1
    assert runs[0]["status"] == "applied"
    assert runs[0]["planned"] == 2
    assert runs[0]["submitted"] == 2
    assert runs[0]["checked"] == 1
    assert runs[0]["quota_before"] == {"daily_remaining": 10}
    assert runs[0]["quota_after"] == {"daily_remaining": 8}
    assert runs[0]["detail"] == {"not_attempted": 0}


def test_quota_usage_increments_per_day_and_kind(store: Store) -> None:
    assert store.get_quota_usage("gsc", "p", "2026-11-17", "inspect") == 0
    assert store.increment_quota_usage("gsc", "p", "2026-11-17", "inspect") == 1
    assert store.increment_quota_usage("gsc", "p", "2026-11-17", "inspect", 2) == 3
    assert store.increment_quota_usage("gsc", "p", "2026-11-17", "submit") == 1
    assert store.increment_quota_usage("gsc", "p", "2026-11-18", "inspect") == 1
    assert store.get_quota_usage("gsc", "p", "2026-11-17", "inspect") == 3
