"""Regression tests for the F14/F24 store fixes.

Covers: daily vs window granularity separation, GSC sightings that preserve
crawl truth, atomic multi-write batches and the schema-migrations ledger.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from lv24_seo.store.db import migrate
from lv24_seo.store.repositories import Store


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(tmp_path / "seo.db")
    try:
        yield instance
    finally:
        instance.close()


def test_weekly_and_daily_gsc_share_end_date_without_clobbering(store: Store) -> None:
    store.upsert_metric_daily(
        "p1", "gsc", "2026-10-07", scope="query", key="q", metric="clicks", value=10
    )
    store.upsert_metric_window(
        "p1", "gsc", "2026-10-01", "2026-10-07", scope="query", key="q", metric="clicks", value=100
    )

    daily = store.get_metric_daily("p1", source="gsc")
    windows = store.get_metric_windows("p1", source="gsc")

    assert [(row["date"], row["value"]) for row in daily] == [("2026-10-07", 10.0)]
    assert [(row["start"], row["end"], row["value"]) for row in windows] == [
        ("2026-10-01", "2026-10-07", 100.0)
    ]


def test_overlapping_windows_do_not_overwrite_each_other(store: Store) -> None:
    store.upsert_metric_window(
        "p1", "gsc", "2026-10-01", "2026-10-07", scope="query", key="q", metric="clicks", value=100
    )
    store.upsert_metric_window(
        "p1", "gsc", "2026-10-02", "2026-10-08", scope="query", key="q", metric="clicks", value=110
    )

    windows = store.get_metric_windows("p1", source="gsc")
    assert sorted(row["value"] for row in windows) == [100.0, 110.0]


def test_gsc_sighting_preserves_crawl_truth(store: Store) -> None:
    store.upsert_page(
        "p1",
        "https://example.com/a",
        status=200,
        index_state="indexable",
        canonical="https://example.com/a",
        title_hash="abc",
    )

    store.record_gsc_sighting("p1", "https://example.com/a")
    store.record_gsc_sighting("p1", "https://example.com/unseen")

    pages = {row["url"]: row for row in store.list_pages("p1")}
    assert pages["https://example.com/a"]["status"] == 200
    assert pages["https://example.com/a"]["index_state"] == "indexable"
    assert pages["https://example.com/a"]["canonical"] == "https://example.com/a"
    assert pages["https://example.com/a"]["title_hash"] == "abc"
    assert pages["https://example.com/unseen"]["index_state"] == "gsc_search_analytics"


def test_atomic_batch_rolls_back_together(store: Store) -> None:
    class _Boom(Exception):
        pass

    try:
        with store.atomic():
            store.upsert_metric_daily("p1", "gsc", "2026-11-01", metric="clicks", value=1)
            raise _Boom()
    except _Boom:
        pass

    assert store.get_metric_daily("p1") == []


def test_atomic_batch_commits_together(store: Store) -> None:
    with store.atomic():
        store.upsert_metric_daily("p1", "gsc", "2026-11-01", metric="clicks", value=1)
        store.upsert_metric_daily("p1", "gsc", "2026-11-02", metric="clicks", value=2)

    assert len(store.get_metric_daily("p1")) == 2


def test_migrations_ledger_records_schema_version(store: Store) -> None:
    rows = store.conn.execute(
        "SELECT version, description FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert rows, "fresh databases must record the applied migrations"
    assert rows[0][0] == 1


def test_migrate_upgrades_pre_windows_database_without_losing_data(tmp_path: Path) -> None:
    """A database created before ``metric_windows`` gains it with data intact."""
    from lv24_seo.store import Store as _Store

    path = tmp_path / "legacy.db"
    legacy = _Store(path)
    legacy.upsert_property("gsc", "p", "P")
    legacy.conn.execute("DROP TABLE metric_windows")
    legacy.conn.execute("DROP TABLE schema_migrations")
    legacy.conn.commit()
    legacy.close()

    store = Store(path)
    try:
        assert (
            store.conn.execute(
                "SELECT name FROM sqlite_master WHERE name='metric_windows'"
            ).fetchone()
            is not None
        )
        assert store.list_properties()[0]["property_id"] == "p"
        # Re-running migrations on an upgraded database applies nothing.
        assert migrate(store.conn) == []
    finally:
        store.close()


def _legacy_db_with_gsc_daily(path: Path) -> None:
    """Simulate a pre-0.2.0 database: GSC rows in metric_daily, no migration 2."""
    legacy = Store(path)
    legacy.upsert_metric_daily("p", "gsc", "2026-11-01", metric="clicks", value=70.0)
    legacy.upsert_metric_daily("p", "bing", "2026-11-01", metric="clicks", value=3.0)
    legacy.conn.execute("DELETE FROM schema_migrations WHERE version = 2")
    legacy.conn.execute("UPDATE metric_daily SET accuracy = NULL")
    legacy.conn.commit()
    legacy.close()


def test_migration_2_marks_only_legacy_gsc_daily_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db_with_gsc_daily(path)

    store = Store(path)
    try:
        daily = store.get_metric_daily("p")
        assert [row["source"] for row in daily] == ["bing"], "legacy GSC rows leak into daily"
        with_legacy = store.get_metric_daily("p", include_legacy=True)
        gsc = [row for row in with_legacy if row["source"] == "gsc"]
        assert gsc and gsc[0]["accuracy"] == "legacy_unknown_granularity"
        assert gsc[0]["value"] == 70.0, "history must be retained, not deleted"

        # A true daily write after the migration is a normal daily row again.
        store.upsert_metric_daily("p", "gsc", "2026-11-02", metric="clicks", value=9.0)
        assert [r["date"] for r in store.get_metric_daily("p", source="gsc")] == ["2026-11-02"]
        assert migrate(store.conn) == []
    finally:
        store.close()


def test_failed_migration_rolls_back_data_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lv24_seo.store import db

    path = tmp_path / "seo.db"
    Store(path).close()
    broken = (
        99,
        "deliberately broken",
        "CREATE TABLE half_applied (id INTEGER); INSERT INTO no_such_table VALUES (1);",
    )
    monkeypatch.setattr(db, "_MIGRATIONS", (*db._MIGRATIONS, broken))

    conn = db.connect(path)
    try:
        with pytest.raises(Exception, match="no_such_table"):
            db.migrate(conn)
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='half_applied'").fetchone()
            is None
        ), "DDL from a failed migration must roll back"
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=99").fetchone() is None

        fixed = (99, "fixed", "CREATE TABLE half_applied (id INTEGER);")
        monkeypatch.setattr(db, "_MIGRATIONS", (*db._MIGRATIONS[:-1], fixed))
        assert db.migrate(conn) == [99]
        assert db.migrate(conn) == []
    finally:
        conn.close()
