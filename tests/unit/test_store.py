"""Contract tests for the Wave 2 SQLite storage layer."""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lv24_seo.store import Store, connect, initialize
from lv24_seo.store.db import schema_path
from lv24_seo.store.repositories import canonical_json

EXPECTED_TABLES = {
    "bot_daily",
    "crawl_runs",
    "issues",
    "metric_daily",
    "pages",
    "properties",
    "runs",
    "sends",
    "snapshots",
}


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


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "seo.db"
    first = Store(db_path)
    assert _table_names(first.conn) >= EXPECTED_TABLES
    first.close()

    conn = connect(db_path)
    try:
        initialize(conn)
        initialize(conn)
        assert _table_names(conn) >= EXPECTED_TABLES
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
        ).fetchall()
        assert indexes
    finally:
        conn.close()


def test_connect_sets_wal_foreign_keys_and_row_factory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "seo.db"
    conn = connect(db_path)
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db_path.parent.is_dir()
    finally:
        conn.close()

    reopened = connect(db_path)
    try:
        # WAL is a persistent database property, not only a per-connection pragma.
        assert reopened.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        reopened.close()


def test_schema_file_is_packaged_next_to_module() -> None:
    assert schema_path().is_file()
    assert schema_path().name == "schema.sql"


def test_property_round_trip(store: Store) -> None:
    store.upsert_property(
        "gsc",
        "sc-domain:lv24.by",
        "LV24",
        host_id="https:lv24.by:443",
        counter_id="12345",
        config={"site": "https://lv24.by", "lag_days": 3},
    )

    assert store.list_properties() == [
        {
            "provider": "gsc",
            "property_id": "sc-domain:lv24.by",
            "display_name": "LV24",
            "host_id": "https:lv24.by:443",
            "counter_id": "12345",
            "verified": True,
            "config": {"lag_days": 3, "site": "https://lv24.by"},
        }
    ]


def test_property_upsert_replaces_fields_for_same_key(store: Store) -> None:
    store.upsert_property("gsc", "p1", "Old", host_id="host-1", config={"a": 1})
    store.upsert_property("bing", "p2", "Bing")
    store.upsert_property("gsc", "p1", "New", verified=False)

    properties = store.list_properties()
    assert [item["property_id"] for item in properties] == ["p2", "p1"]
    updated = properties[1]
    assert updated["display_name"] == "New"
    assert updated["host_id"] is None
    assert updated["verified"] is False
    assert updated["config"] is None


def test_metric_daily_range_filter_and_upsert(store: Store) -> None:
    store.upsert_metric_daily("p1", "gsc", "2026-11-01", metric="clicks", value=10)
    store.upsert_metric_daily("p1", "gsc", date(2026, 11, 2), metric="clicks", value=12)
    store.upsert_metric_daily("p1", "gsc", "2026-11-03", metric="clicks", value=9)
    store.upsert_metric_daily("p1", "yandex", "2026-11-02", metric="clicks", value=7)
    store.upsert_metric_daily("p1", "gsc", "2026-11-02", scope="desktop", metric="clicks", value=4)
    store.upsert_metric_daily("p1", "gsc", "2026-11-02", metric="clicks", value=15)

    rows = store.get_metric_daily(
        "p1", source="gsc", scope="total", start="2026-11-02", end=date(2026, 11, 3)
    )
    assert [(row["date"], row["value"]) for row in rows] == [
        ("2026-11-02", 15.0),
        ("2026-11-03", 9.0),
    ]

    assert [row["value"] for row in store.get_metric_daily("p1", source="yandex")] == [7.0]
    assert [row["date"] for row in store.get_metric_daily("p1", scope="desktop")] == ["2026-11-02"]
    assert len(store.get_metric_daily("p1")) == 5


def test_metric_daily_accuracy_is_replaced(store: Store) -> None:
    store.upsert_metric_daily("p1", "gsc", "2026-11-02", metric="clicks", value=1)
    store.upsert_metric_daily("p1", "gsc", "2026-11-02", metric="clicks", value=2, accuracy="final")

    [row] = store.get_metric_daily("p1")
    assert row["value"] == 2.0
    assert row["accuracy"] == "final"


def test_page_first_seen_is_preserved(store: Store) -> None:
    store.upsert_page(
        "p1",
        "https://lv24.by/a",
        status=200,
        index_state="indexed",
        seen_at="2026-11-01T00:00:00+00:00",
    )
    store.upsert_page(
        "p1",
        "https://lv24.by/a",
        status=301,
        index_state="redirect",
        title_hash="abc",
        seen_at=datetime(2026, 11, 2, 12, 30, tzinfo=UTC),
    )
    store.upsert_page("p1", "https://lv24.by/b", status=200)

    [page] = [item for item in store.list_pages("p1") if item["url"].endswith("/a")]
    assert page["first_seen"] == "2026-11-01T00:00:00+00:00"
    assert page["last_seen"] == "2026-11-02T12:30:00+00:00"
    assert page["status"] == 301
    assert page["index_state"] == "redirect"
    assert page["title_hash"] == "abc"
    assert len(store.list_pages("p1", limit=1)) == 1


def test_issue_dedupe_keeps_first_seen_and_reopens_after_fix(store: Store) -> None:
    first = store.upsert_issue(
        "missing-title",
        "high",
        "p1",
        "https://lv24.by/a",
        {"selector": "h1"},
        seen_at="2026-11-01T10:00:00+00:00",
    )
    assert first == "new"

    [issue] = store.list_issues("p1")
    issue_id = issue["id"]
    assert issue["first_seen"] == "2026-11-01T10:00:00+00:00"
    assert issue["last_seen"] == "2026-11-01T10:00:00+00:00"
    assert issue["detail"] == {"selector": "h1"}
    assert issue["state"] == "open"

    second = store.upsert_issue(
        "missing-title",
        "medium",
        "p1",
        "https://lv24.by/a",
        {"selector": "h2", "count": 2},
        seen_at="2026-11-02T10:00:00+00:00",
    )
    assert second == "existing"

    [issue] = store.list_issues("p1")
    assert issue["id"] == issue_id
    assert issue["first_seen"] == "2026-11-01T10:00:00+00:00"
    assert issue["last_seen"] == "2026-11-02T10:00:00+00:00"
    assert issue["severity"] == "medium"
    assert issue["detail"] == {"count": 2, "selector": "h2"}

    store.set_issue_state(issue_id, "fixed")
    assert store.list_issues("p1") == []
    assert [row["id"] for row in store.list_issues("p1", state="fixed")] == [issue_id]

    third = store.upsert_issue(
        "missing-title",
        "high",
        "p1",
        "https://lv24.by/a",
        seen_at="2026-11-03T10:00:00+00:00",
    )
    assert third == "reopened"

    [issue] = store.list_issues("p1")
    assert issue["state"] == "open"
    assert issue["first_seen"] == "2026-11-01T10:00:00+00:00"
    assert issue["last_seen"] == "2026-11-03T10:00:00+00:00"
    assert issue["detail"] == {}


def test_issue_severity_and_state_filters(store: Store) -> None:
    store.upsert_issue("r1", "high", "p1", "https://lv24.by/a")
    store.upsert_issue("r2", "low", "p1", "https://lv24.by/b")
    store.upsert_issue("r3", "medium", "p2", "https://lv24.by/c")

    assert [row["rule_id"] for row in store.list_issues("p1", severity="high")] == ["r1"]
    assert len(store.list_issues("p1", state=None)) == 2
    assert store.list_issues("p2")[0]["property_id"] == "p2"


def test_set_issue_state_rejects_unknown_id(store: Store) -> None:
    with pytest.raises(KeyError):
        store.set_issue_state(999, "fixed")


def test_snapshot_round_trip_and_stable_hash(store: Store) -> None:
    payload = {"headline": {"clicks": 10}, "issues": [{"rule": "missing-title"}]}
    reordered = {"issues": [{"rule": "missing-title"}], "headline": {"clicks": 10}}

    digest = store.save_snapshot("weekly", "p1", "2026-W45", payload)
    assert digest == hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert store.save_snapshot("weekly", "p1", "2026-W45", reordered) == digest
    assert store.load_snapshot("weekly", "p1", "2026-W45") == payload

    row = store.conn.execute(
        """
        SELECT content_hash, payload_gz
        FROM snapshots
        WHERE kind = 'weekly' AND property_id = 'p1' AND period = '2026-W45'
        """
    ).fetchone()
    assert row["content_hash"] == digest
    assert gzip.decompress(row["payload_gz"]).decode("utf-8") == canonical_json(payload)

    changed = store.save_snapshot("weekly", "p1", "2026-W45", {"headline": {"clicks": 11}})
    assert changed != digest
    assert store.load_snapshot("weekly", "p1", "2026-W45") == {"headline": {"clicks": 11}}
    assert store.load_snapshot("weekly", "p1", "2026-W44") is None
    assert store.load_snapshot("daily", "p1", "2026-W45") is None


def test_snapshot_hash_ignores_dict_order() -> None:
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 3}}) == canonical_json(
        {"a": {"x": 3, "y": 2}, "b": 1}
    )


def test_duplicate_send_returns_false_and_keeps_original_row(store: Store) -> None:
    digest = store.save_snapshot("weekly", "p1", "2026-W45", {"clicks": 10})

    assert (
        store.record_send(
            "weekly",
            "p1",
            "2026-W45",
            digest,
            "email",
            sent_at="2026-11-10T08:00:00+00:00",
        )
        is True
    )
    assert store.was_sent("weekly", "p1", "2026-W45", digest, "email") is True

    assert (
        store.record_send(
            "weekly",
            "p1",
            "2026-W45",
            digest,
            "email",
            status="failed",
            sent_at="2026-11-10T09:00:00+00:00",
        )
        is False
    )
    row = store.conn.execute(
        """
        SELECT sent_at, status FROM sends
        WHERE kind = 'weekly' AND property_id = 'p1' AND period = '2026-W45'
          AND content_hash = ? AND channel = 'email'
        """,
        (digest,),
    ).fetchone()
    assert row["sent_at"] == "2026-11-10T08:00:00+00:00"
    assert row["status"] == "sent"

    assert store.record_send("weekly", "p1", "2026-W45", digest, "telegram") is True
    changed = store.save_snapshot("weekly", "p1", "2026-W45", {"clicks": 11})
    assert store.record_send("weekly", "p1", "2026-W45", changed, "email") is True
    assert store.was_sent("weekly", "p1", "2026-W45", changed, "telegram") is False


def test_record_run_returns_incrementing_ids_and_stores_stats(store: Store) -> None:
    first = store.record_run(
        "weekly",
        "p1",
        "ok",
        started_at="2026-11-10T08:00:00+00:00",
        finished_at="2026-11-10T08:00:05+00:00",
        stats={"urls": 12, "errors": 0},
    )
    second = store.record_run("crawl", None, "failed")

    assert first > 0
    assert second > first

    row = store.conn.execute("SELECT * FROM runs WHERE id = ?", (first,)).fetchone()
    assert row["kind"] == "weekly"
    assert row["property_id"] == "p1"
    assert row["status"] == "ok"
    assert row["started_at"] == "2026-11-10T08:00:00+00:00"
    assert row["finished_at"] == "2026-11-10T08:00:05+00:00"
    assert row["stats_json"] == canonical_json({"errors": 0, "urls": 12})

    other = store.conn.execute("SELECT * FROM runs WHERE id = ?", (second,)).fetchone()
    assert other["property_id"] is None
    assert other["stats_json"] is None
    assert other["started_at"]


def test_prune_raw_is_a_documented_noop(store: Store) -> None:
    store.save_snapshot("weekly", "p1", "2026-W45", {"clicks": 10})

    assert store.prune_raw(before=datetime(2020, 1, 1, tzinfo=UTC)) == 0
    assert store.load_snapshot("weekly", "p1", "2026-W45") == {"clicks": 10}


def test_naive_datetime_is_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        store.record_run("weekly", "p1", "ok", started_at=datetime(2026, 11, 10, 8, 0))


def test_store_context_manager_closes_connection(tmp_path: Path) -> None:
    with Store(tmp_path / "seo.db") as instance:
        instance.upsert_property("gsc", "p1", "LV24")
        assert instance.list_properties()
    assert instance._closed is True
