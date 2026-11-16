"""Tests for the read-only multi-site portfolio scan (F11).

Pinned contracts:

* discovery is bounded by depth 3, skips hidden/tooling directories and counts every config;
* the scan inspects at most ``--limit`` sites and says so with ``truncated``/``limitations``;
* a broken config or database becomes a per-site ``error`` instead of failing the portfolio;
* an ambient ``LV24_SEO_DATA_DIR`` never redirects every site to one data directory;
* the scan writes nothing: no SQLite ``-wal``/``-shm`` sidecars, no changed files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from lv24_seo.cli.portfolio_cmd import register
from lv24_seo.composition import DB_FILENAME as COMPOSITION_DB_FILENAME
from lv24_seo.core import portfolio
from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.core.portfolio import DB_FILENAME, MAX_SITES, scan_portfolio
from lv24_seo.store.db import connect, initialize
from lv24_seo.store.errors import STORE_CORRUPT, STORE_LOCKED, STORE_UNAVAILABLE

RUNNER = CliRunner()

PROPERTY_A = "sc-domain:a.example"
PROPERTY_B = "sc-domain:b.example"


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the scan away from the repository and the real user config directory."""
    monkeypatch.chdir(tmp_path)
    for name in ("LV24_SEO_DATA_DIR", "LV24_SEO_GSC_PROPERTY", "LV24_SEO_REPORT_LOCALE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "lv24_seo.core.config.platformdirs.user_config_dir",
        lambda *_args, **_kwargs: str(tmp_path / "user-config"),
    )


def _site(
    root: Path,
    rel: str,
    *,
    name: str = "Example",
    default_property: str = PROPERTY_A,
    gsc_property: str | None = None,
    data: bool = True,
) -> tuple[Path, Path | None]:
    """Create a project folder and return ``(config_path, db_path)``."""
    site = root / rel if rel else root
    site.mkdir(parents=True, exist_ok=True)
    body = f'[project]\nname = "{name}"\nbase_url = "https://example.com"\n'
    if default_property:
        body += f'default_property = "{default_property}"\n'
    if gsc_property:
        body += f'[providers.gsc]\nproperty = "{gsc_property}"\n'
    body += '[storage]\ndata_dir = "./data"\n'
    config = site / "seo.toml"
    config.write_text(body, encoding="utf-8")

    db_path: Path | None = None
    if data:
        data_dir = site / "data"
        data_dir.mkdir(exist_ok=True)
        db_path = data_dir / DB_FILENAME
    return config, db_path


def _seed(
    db_path: Path,
    *,
    runs: tuple[tuple[str, str | None, str | None, str | None, str], ...] = (),
    snapshots: tuple[tuple[str, str, str, str], ...] = (),
    issues: tuple[tuple[str, str, str, str, str, str], ...] = (),
) -> None:
    """Initialise a history database and insert explicit rows.

    ``runs``: ``(kind, property_id, started_at, finished_at, status)``.
    ``snapshots``: ``(kind, property_id, period, created_at)``.
    ``issues``: ``(rule_id, severity, property_id, url, state, last_seen)``.
    """
    conn = connect(db_path)
    try:
        initialize(conn, path=db_path)
        for kind, property_id, started_at, finished_at, status in runs:
            conn.execute(
                "INSERT INTO runs (kind, property_id, started_at, finished_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, property_id, started_at, finished_at, status),
            )
        for kind, property_id, period, created_at in snapshots:
            conn.execute(
                "INSERT INTO snapshots "
                "(kind, property_id, period, content_hash, payload_gz, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, property_id, period, "hash", b"x", created_at),
            )
        for rule_id, severity, property_id, url, state, last_seen in issues:
            conn.execute(
                "INSERT INTO issues "
                "(rule_id, severity, property_id, url, first_seen, last_seen, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule_id, severity, property_id, url, last_seen, last_seen, state),
            )
        conn.commit()
    finally:
        conn.close()


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def _cli() -> typer.Typer:
    app = typer.Typer()
    register(app)
    return app


def test_scan_finds_root_and_nested_configs_up_to_depth_three(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _site(root, "", data=False)
    _site(root, "one", data=False)
    _site(root, "one/two", data=False)
    _site(root, "one/two/three", data=False)
    _site(root, "one/two/three/four", data=False)
    _site(root, ".hidden", data=False)
    _site(root, "node_modules/pkg", data=False)
    (root / "notes.txt").write_text("not a config", encoding="utf-8")

    scan = scan_portfolio(root)

    found = {Path(site.config_path).relative_to(root).as_posix() for site in scan.sites}
    assert found == {"seo.toml", "one/seo.toml", "one/two/seo.toml", "one/two/three/seo.toml"}
    assert scan.total == 4
    assert scan.truncated is False
    assert scan.limitations() == []


def test_scan_reports_counts_timestamps_and_sorts_by_last_activity(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_a = _site(root, "a", default_property=PROPERTY_A)
    _, db_b = _site(root, "b", name="Bee", default_property="", gsc_property=PROPERTY_B)
    assert db_a is not None and db_b is not None

    _seed(
        db_a,
        runs=(
            (
                "crawl",
                PROPERTY_A,
                "2026-11-09T08:00:00+00:00",
                "2026-11-09T08:05:00+00:00",
                "ok",
            ),
            (
                "crawl",
                PROPERTY_A,
                "2026-11-10T08:00:00+00:00",
                "2026-11-10T08:05:00+00:00",
                "ok",
            ),
        ),
        snapshots=(
            ("weekly", PROPERTY_A, "2026-W45", "2026-11-10T09:00:00+00:00"),
            ("weekly", PROPERTY_A, "2026-W46", "2026-11-14T09:00:00+00:00"),
        ),
        issues=(
            (
                "canonical-missing",
                "critical",
                PROPERTY_A,
                "https://a.example/1",
                "open",
                "2026-11-15T10:00:00+00:00",
            ),
            (
                "h1-missing",
                "high",
                PROPERTY_A,
                "https://a.example/2",
                "open",
                "2026-11-12T10:00:00+00:00",
            ),
            (
                "h1-missing",
                "high",
                PROPERTY_A,
                "https://a.example/3",
                "fixed",
                "2026-11-11T10:00:00+00:00",
            ),
        ),
    )
    _seed(
        db_b,
        runs=(
            (
                "crawl",
                PROPERTY_B,
                "2026-11-16T07:00:00+00:00",
                "2026-11-16T07:03:00+00:00",
                "ok",
            ),
        ),
    )

    scan = scan_portfolio(root)

    assert [Path(site.config_path).parent.name for site in scan.sites] == ["b", "a"]
    by_name = {Path(site.config_path).parent.name: site for site in scan.sites}

    a = by_name["a"]
    assert a.property == PROPERTY_A
    assert Path(a.data_dir or "") == root / "a" / "data"
    assert a.db_path == str(root / "a" / "data" / DB_FILENAME)
    assert a.db_present is True
    assert a.crawl_runs == 2
    assert a.last_crawl_at == "2026-11-10T08:05:00+00:00"
    assert a.report_snapshots == 2
    assert a.last_report_at == "2026-11-14T09:00:00+00:00"
    assert a.last_report_period == "2026-W46"
    assert a.issues_total == 3
    assert a.issues_open == 2
    assert a.issues_critical == 1
    assert a.last_issue_at == "2026-11-15T10:00:00+00:00"
    assert a.last_activity_at == "2026-11-15T10:00:00+00:00"
    assert a.error is None

    b = by_name["b"]
    assert b.name == "Bee"
    assert b.property == PROPERTY_B  # falls back to providers.gsc.property
    assert b.crawl_runs == 1
    assert b.last_activity_at == "2026-11-16T07:03:00+00:00"
    assert b.report_snapshots == 0
    assert b.issues_total == 0


def test_missing_database_is_not_an_error(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_path = _site(root, "site", data=False)
    assert db_path is None

    scan = scan_portfolio(root)

    site = scan.sites[0]
    assert site.db_present is False
    assert site.db_path == str(root / "site" / "data" / DB_FILENAME)
    assert site.data_dir == str(root / "site" / "data")
    assert site.error is None
    assert site.last_activity_at is None


def test_corrupt_config_is_a_per_site_error(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _site(root, "good", data=False)
    bad = root / "bad"
    bad.mkdir()
    (bad / "seo.toml").write_text("[project\nname = broken", encoding="utf-8")

    scan = scan_portfolio(root)

    assert len(scan.sites) == 2
    by_dir = {Path(site.config_path).parent.name: site for site in scan.sites}
    assert by_dir["good"].error is None
    broken = by_dir["bad"]
    assert broken.error is not None
    assert broken.error["code"] == CONFIG_INVALID
    assert "not valid TOML" in broken.error["message"]
    assert broken.data_dir is None
    assert broken.db_present is False


def test_corrupt_database_is_a_per_site_error(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_path = _site(root, "site")
    assert db_path is not None
    db_path.write_bytes(b"this is not a sqlite database")

    scan = scan_portfolio(root)

    site = scan.sites[0]
    assert site.db_present is True
    assert site.error is not None
    assert site.error["code"] == STORE_CORRUPT
    assert "could not be read" in site.error["message"]
    assert site.crawl_runs == 0


def test_limit_caps_inspection_and_reports_the_full_total(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    for name in ("a", "b", "c"):
        _site(root, name, data=False)

    scan = scan_portfolio(root, limit=2)

    assert scan.total == 3
    assert len(scan.sites) == 2
    assert scan.truncated is True
    meta = scan.meta()
    assert meta["count"] == 2
    assert meta["total"] == 3
    assert meta["truncated"] is True
    assert meta["read_only"] is True
    assert meta["data_age_days"] == 0
    assert len(scan.limitations()) == 1
    assert "first 2 of 3" in scan.limitations()[0]


@pytest.mark.parametrize("limit", [0, -1, MAX_SITES + 1])
def test_limit_outside_one_to_fifty_is_rejected(tmp_path: Path, limit: int) -> None:
    root = tmp_path / "sites"
    root.mkdir()

    with pytest.raises(SeoError) as excinfo:
        scan_portfolio(root, limit=limit)

    assert excinfo.value.code == INVALID_INPUT


def test_root_must_be_an_absolute_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    for root in (Path("relative/sites"), missing, file_path):
        with pytest.raises(SeoError) as excinfo:
            scan_portfolio(root)
        assert excinfo.value.code == INVALID_INPUT


def test_ambient_data_dir_does_not_redirect_the_portfolio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_path = _site(root, "site")
    assert db_path is not None
    _seed(db_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("LV24_SEO_DATA_DIR", str(elsewhere))

    scan = scan_portfolio(root)

    site = scan.sites[0]
    assert site.data_dir == str(root / "site" / "data")
    assert site.db_present is True


def test_live_wal_database_is_read_without_creating_sidecars(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_path = _site(root, "site")
    assert db_path is not None
    writer = connect(db_path)
    try:
        initialize(writer, path=db_path)
        writer.execute(
            "INSERT INTO runs (kind, property_id, started_at, status) VALUES ('crawl', ?, ?, 'ok')",
            (PROPERTY_A, "2026-11-10T08:00:00+00:00"),
        )
        writer.commit()
        before = _tree_state(root)

        scan = scan_portfolio(root)

        assert _tree_state(root) == before
    finally:
        writer.close()
    site = scan.sites[0]
    assert site.error is None
    assert site.crawl_runs == 1


def test_wal_without_shared_memory_is_refused_instead_of_writing(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_path = _site(root, "site")
    assert db_path is not None
    _seed(db_path)
    Path(f"{db_path}-wal").write_bytes(b"")
    assert not Path(f"{db_path}-shm").exists()

    scan = scan_portfolio(root)

    site = scan.sites[0]
    assert site.error is not None
    assert site.error["code"] == STORE_UNAVAILABLE
    assert "uncheckpointed WAL" in site.error["message"]
    assert not Path(f"{db_path}-shm").exists()


def test_scan_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_path = _site(root, "site")
    assert db_path is not None
    _seed(
        db_path,
        runs=(("crawl", PROPERTY_A, "2026-11-10T08:00:00+00:00", None, "ok"),),
    )
    before = _tree_state(root)

    scan = scan_portfolio(root)

    assert _tree_state(root) == before
    assert not list(root.rglob("*-wal"))
    assert not list(root.rglob("*-shm"))
    assert scan.sites[0].db_present is True


def test_cli_emits_the_standard_envelope_and_human_summary(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    _, db_path = _site(root, "site")
    assert db_path is not None
    _seed(
        db_path,
        runs=(
            (
                "crawl",
                PROPERTY_A,
                "2026-11-10T08:00:00+00:00",
                "2026-11-10T08:05:00+00:00",
                "ok",
            ),
        ),
        issues=(
            (
                "h1-missing",
                "high",
                PROPERTY_A,
                "https://a.example/1",
                "open",
                "2026-11-11T10:00:00+00:00",
            ),
        ),
    )

    result = RUNNER.invoke(_cli(), ["portfolio", "list", "--root", str(root), "--json"])

    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["ok"] is True
    assert set(payload) == {"ok", "data", "meta", "warnings"}
    assert payload["meta"]["root"] == str(root)
    assert payload["meta"]["count"] == 1
    assert payload["meta"]["total"] == 1
    assert payload["meta"]["truncated"] is False
    assert payload["meta"]["read_only"] is True
    assert payload["meta"]["max_depth"] == 3
    assert payload["meta"]["limitations"] == []
    assert payload["warnings"] == []
    site = payload["data"]["sites"][0]
    assert site["property"] == PROPERTY_A
    assert site["crawl_runs"] == 1
    assert site["issues_open"] == 1
    assert site["error"] is None

    human = RUNNER.invoke(_cli(), ["portfolio", "list", "--root", str(root)])
    assert human.exit_code == 0
    assert "portfolio: 1 of 1 sites" in human.output
    assert PROPERTY_A in human.output
    assert "issues=1/1" in human.output
    assert not human.output.lstrip().startswith("{")


def test_cli_rejects_a_bad_root_and_limit_with_exit_code_two(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()

    bad_root = RUNNER.invoke(
        _cli(), ["portfolio", "list", "--root", str(tmp_path / "missing"), "--json"]
    )
    assert bad_root.exit_code == 2
    payload = _json(bad_root)
    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT

    bad_limit = RUNNER.invoke(
        _cli(), ["portfolio", "list", "--root", str(root), "--limit", "0", "--json"]
    )
    assert bad_limit.exit_code == 2
    assert _json(bad_limit)["error"]["code"] == INVALID_INPUT


def test_cli_reports_truncation_and_limitations(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()
    for name in ("a", "b"):
        _site(root, name, data=False)

    result = RUNNER.invoke(
        _cli(), ["portfolio", "list", "--root", str(root), "--limit", "1", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["meta"]["truncated"] is True
    assert payload["meta"]["count"] == 1
    assert payload["meta"]["total"] == 2
    assert payload["meta"]["limitations"]
    assert "first 1 of 2" in payload["meta"]["limitations"][0]


def test_empty_root_is_an_empty_portfolio(tmp_path: Path) -> None:
    root = tmp_path / "sites"
    root.mkdir()

    scan = scan_portfolio(root)

    assert scan.sites == []
    assert scan.total == 0
    assert scan.truncated is False
    assert scan.to_dict() == {"sites": []}


def test_implicit_data_dir_is_anchored_to_the_config_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``[storage]`` block: the default must not follow the shell's working directory."""
    root = tmp_path / "sites"
    site = root / "site"
    site.mkdir(parents=True)
    (site / "seo.toml").write_text('[project]\nname = "NoStorage"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    scan = scan_portfolio(root)

    assert Path(scan.sites[0].data_dir or "") == site / "data"
    assert scan.sites[0].db_present is False


def test_db_filename_and_store_codes_stay_aligned() -> None:
    """Core cannot import store/composition; a parity test prevents silent drift."""
    assert DB_FILENAME == COMPOSITION_DB_FILENAME
    assert portfolio._DB_CORRUPT == STORE_CORRUPT
    assert portfolio._DB_LOCKED == STORE_LOCKED
    assert portfolio._DB_UNAVAILABLE == STORE_UNAVAILABLE


def _tree_state(root: Path) -> dict[str, tuple[str, int | None, int | None]]:
    state: dict[str, tuple[str, int | None, int | None]] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_dir():
            state[key] = ("dir", None, None)
        else:
            stat = path.stat()
            state[key] = ("file", stat.st_size, stat.st_mtime_ns)
    return state
