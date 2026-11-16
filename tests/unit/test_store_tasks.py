"""Task lifecycle storage (F2/F7): migration, idempotent upsert, filters, transitions."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lv24_seo.store import TASK_STATES, Store
from lv24_seo.store.db import SCHEMA_VERSION, migrate

TASKS_MIGRATION_VERSION = 3


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


def _add(store: Store, url: str = "https://example.com/a", **kwargs: object) -> str:
    return store.upsert_task("decay", "p1", url, "content-decay", **kwargs)


def test_fresh_database_has_tasks_and_the_full_migration_ledger(store: Store) -> None:
    assert "tasks" in _table_names(store.conn)
    assert _versions(store.conn) == list(range(1, SCHEMA_VERSION + 1))


def test_existing_database_gains_tasks_without_losing_data(store: Store) -> None:
    store.upsert_property("gsc", "p", "P")
    store.conn.execute("DROP TABLE tasks")
    store.conn.execute(
        "DELETE FROM schema_migrations WHERE version = ?", (TASKS_MIGRATION_VERSION,)
    )
    store.conn.commit()

    # migrate() alone must rebuild the table: initialize() would also run schema.sql.
    assert migrate(store.conn) == [TASKS_MIGRATION_VERSION]
    assert "tasks" in _table_names(store.conn)
    assert store.list_properties()[0]["property_id"] == "p"
    assert _versions(store.conn) == list(range(1, SCHEMA_VERSION + 1))
    assert migrate(store.conn) == []


def test_schema_rejects_a_state_outside_the_closed_set(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            """
            INSERT INTO tasks (kind, property_id, url, rule_id, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("decay", "p1", "https://example.com/a", "r", "done", "2026-11-01", "2026-11-01"),
        )
    store.conn.rollback()


def test_upsert_is_idempotent_and_keeps_identity_and_created_at(store: Store) -> None:
    first = _add(store, payload={"months_stale": 16}, seen_at="2026-11-01T00:00:00+00:00")
    assert first == "created"
    [task] = store.list_tasks(property_id="p1")
    original_id = task["id"]
    assert task["state"] == "open"
    assert task["payload"] == {"months_stale": 16}
    assert task["created_at"] == "2026-11-01T00:00:00+00:00"

    again = _add(store, seen_at="2026-11-05T00:00:00+00:00")
    assert again == "existing"
    [task] = store.list_tasks(property_id="p1")
    assert task["id"] == original_id
    assert task["created_at"] == "2026-11-01T00:00:00+00:00"
    # No payload on the repeat: a true no-op, not a freshness bump.
    assert task["updated_at"] == "2026-11-01T00:00:00+00:00"

    refreshed = _add(store, payload={"months_stale": 17}, seen_at="2026-11-06T00:00:00+00:00")
    assert refreshed == "existing"
    [task] = store.list_tasks(property_id="p1")
    assert task["payload"] == {"months_stale": 17}
    assert task["updated_at"] == "2026-11-06T00:00:00+00:00"


def test_identity_is_kind_property_url_and_rule(store: Store) -> None:
    _add(store, url="https://example.com/a")
    store.upsert_task("decay", "p1", "https://example.com/a", "other-rule")
    store.upsert_task("finding", "p1", "https://example.com/a", "content-decay")
    store.upsert_task("decay", "p2", "https://example.com/a", "content-decay")

    assert len(store.list_tasks()) == 4


def test_upsert_rejects_unknown_state_and_empty_identity(store: Store) -> None:
    with pytest.raises(ValueError, match="unknown task state"):
        _add(store, state="done")
    with pytest.raises(ValueError, match="url must be a non-empty string"):
        store.upsert_task("decay", "p1", "   ", "content-decay")


def test_list_filters_and_bounds(store: Store) -> None:
    store.upsert_task("decay", "p1", "u1", "r1", seen_at="2026-11-01T00:00:00+00:00")
    store.upsert_task("finding", "p1", "u2", "r2", seen_at="2026-11-02T00:00:00+00:00")
    store.upsert_task("decay", "p2", "u3", "r1", seen_at="2026-11-03T00:00:00+00:00")

    assert [task["url"] for task in store.list_tasks()] == ["u3", "u2", "u1"]
    assert [task["url"] for task in store.list_tasks(property_id="p1")] == ["u2", "u1"]
    assert [task["url"] for task in store.list_tasks(kind="decay")] == ["u3", "u1"]
    assert [task["url"] for task in store.list_tasks(rule_id="r1")] == ["u3", "u1"]
    assert [task["url"] for task in store.list_tasks(url="u2")] == ["u2"]
    assert store.list_tasks(limit=0) == []
    assert store.list_tasks(limit=-1) == []

    assert store.count_tasks() == 3
    assert store.count_tasks(property_id="p1") == 2
    assert store.count_tasks(kind="decay") == 2
    assert store.count_tasks(rule_id="r1") == 2
    assert store.count_tasks(url="u2") == 1

    [task] = store.list_tasks(url="u2")
    store.update_task_state(task["id"], "in_progress")
    assert [row["url"] for row in store.list_tasks(state="in_progress")] == ["u2"]
    assert store.count_tasks(state="in_progress") == 1
    assert len(store.list_tasks(state=None)) == 3

    with pytest.raises(ValueError, match="unknown task state"):
        store.list_tasks(state="done")
    with pytest.raises(ValueError, match="unknown task state"):
        store.count_tasks(state="done")


def test_transitions_are_validated_and_same_state_is_a_noop(store: Store) -> None:
    _add(store)
    [task] = store.list_tasks()
    task_id = task["id"]
    store.conn.execute(
        "UPDATE tasks SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (task_id,)
    )
    store.conn.commit()

    unchanged = store.update_task_state(task_id, "open")
    assert unchanged["state"] == "open"
    assert unchanged["updated_at"] == "2000-01-01T00:00:00+00:00"

    moved = store.update_task_state(task_id, "in_progress")
    assert moved["state"] == "in_progress"
    assert moved["updated_at"] != "2000-01-01T00:00:00+00:00"

    assert store.update_task_state(task_id, "verified")["state"] == "verified"
    with pytest.raises(ValueError, match="verified -> in_progress"):
        store.update_task_state(task_id, "in_progress")

    assert store.update_task_state(task_id, "open")["state"] == "open"
    assert store.update_task_state(task_id, "dismissed")["state"] == "dismissed"
    with pytest.raises(ValueError, match="dismissed -> verified"):
        store.update_task_state(task_id, "verified")

    with pytest.raises(ValueError, match="unknown task state"):
        store.update_task_state(task_id, "done")
    with pytest.raises(KeyError):
        store.update_task_state(999, "open")
    assert set(TASK_STATES) == {"open", "in_progress", "verified", "dismissed"}


def test_a_verified_task_reopens_on_recurrence_but_a_dismissed_one_does_not(
    store: Store,
) -> None:
    _add(store, seen_at="2026-11-01T00:00:00+00:00")
    [task] = store.list_tasks()
    task_id = task["id"]
    created_at = task["created_at"]

    store.update_task_state(task_id, "verified")
    assert _add(store, seen_at="2026-11-08T00:00:00+00:00") == "reopened"
    [task] = store.list_tasks()
    assert task["state"] == "open"
    assert task["created_at"] == created_at
    assert task["updated_at"] == "2026-11-08T00:00:00+00:00"

    store.update_task_state(task_id, "dismissed")
    assert _add(store, seen_at="2026-11-15T00:00:00+00:00") == "existing"
    assert store.list_tasks()[0]["state"] == "dismissed"


def test_concurrent_add_of_the_same_identity_creates_one_row(tmp_path: Path) -> None:
    path = tmp_path / "seo.db"
    workers = 4
    barrier = threading.Barrier(workers)

    def worker(_index: int) -> str:
        with Store(path) as instance:
            barrier.wait(timeout=30)
            return instance.upsert_task("decay", "p1", "https://example.com/a", "content-decay")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(worker, range(workers)))

    assert sorted(outcomes) == ["created", "existing", "existing", "existing"]
    with Store(path) as instance:
        assert len(instance.list_tasks()) == 1
        assert instance.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_open_and_writes_keep_the_database_consistent(tmp_path: Path) -> None:
    path = tmp_path / "seo.db"
    workers = 3
    barrier = threading.Barrier(workers)

    def worker(index: int) -> tuple[str, str]:
        with Store(path) as instance:
            barrier.wait(timeout=30)
            created = instance.upsert_task(
                "decay", "p1", f"https://example.com/{index}", "content-decay"
            )
            repeated = instance.upsert_task(
                "decay", "p1", f"https://example.com/{index}", "content-decay"
            )
            return created, repeated

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(worker, range(workers)))

    assert [created for created, _ in outcomes] == ["created"] * workers
    assert [repeated for _, repeated in outcomes] == ["existing"] * workers

    with Store(path) as instance:
        assert len(instance.list_tasks()) == workers
        assert instance.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
