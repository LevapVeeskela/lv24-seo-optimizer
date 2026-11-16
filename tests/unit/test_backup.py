"""Contract tests for the portable workspace backup (F9)."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli.backup_cmd import backup_app
from lv24_seo.core.backup import (
    CONFIG_MEMBER,
    DATABASE_MEMBER,
    MANIFEST_MEMBER,
    create_backup,
    redact_config_document,
)
from lv24_seo.core.config import load_config
from lv24_seo.core.errors import CONFIG_INVALID, INVALID_INPUT, SeoError
from lv24_seo.store import connect, initialize
from lv24_seo.store.db import SCHEMA_VERSION
from lv24_seo.store.errors import STORE_CORRUPT

RUNNER = CliRunner()
NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
ARCHIVE_NAME = "lv24-seo-backup-20261116T120000Z.zip"


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test out of the repository and away from the real user config."""
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("LV24_SEO_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "lv24_seo.core.config.platformdirs.user_config_dir",
        lambda *_args, **_kwargs: str(tmp_path / "user-config"),
    )


def _init_database(path: Path) -> None:
    conn = connect(path)
    try:
        initialize(conn, path=path)
        conn.execute(
            "INSERT INTO properties (provider, property_id, display_name, verified) "
            "VALUES (?, ?, ?, ?)",
            ("gsc", "sc-domain:example.com", "Example", 1),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(kind, property_id, period, content_hash, payload_gz, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "weekly",
                "sc-domain:example.com",
                "2026-W45",
                "abc123",
                gzip.compress(b'{"ok":true}'),
                "2026-11-10T06:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = tmp_path / "seo.toml"
    config.write_text(
        "[project]\n"
        'name = "Example"\n'
        'base_url = "https://example.com"\n'
        'default_property = "sc-domain:example.com"\n'
        "[storage]\n"
        'data_dir = "./data"\n',
        encoding="utf-8",
    )
    db_path = data_dir / "seo.sqlite3"
    _init_database(db_path)
    return config, data_dir, db_path


def _out_dir(tmp_path: Path) -> Path:
    out = tmp_path / "backups"
    out.mkdir()
    return out


def _manifest(archive_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive_path) as archive:
        return json.loads(archive.read(MANIFEST_MEMBER))


def test_create_backup_manifest_hashes_and_consistent_copy(tmp_path: Path) -> None:
    config, data_dir, db_path = _workspace(tmp_path)
    out = _out_dir(tmp_path)
    source_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()

    result = create_backup(config_path=config, out_dir=out, now=NOW)

    assert result.archive_path == out / ARCHIVE_NAME
    assert result.archive_path.is_file()
    assert result.size == result.archive_path.stat().st_size
    assert result.sha256 == hashlib.sha256(result.archive_path.read_bytes()).hexdigest()
    assert [item.name for item in out.iterdir()] == [ARCHIVE_NAME]

    with zipfile.ZipFile(result.archive_path) as archive:
        assert archive.namelist() == [MANIFEST_MEMBER, CONFIG_MEMBER, DATABASE_MEMBER]
        for info in archive.infolist():
            assert (info.external_attr >> 16) & 0o777 == 0o600

        manifest = json.loads(archive.read(MANIFEST_MEMBER))
        assert manifest["format"] == "lv24-seo-backup"
        assert manifest["format_version"] == 1
        assert manifest["engine_version"]
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["engine_schema_version"] == SCHEMA_VERSION
        assert manifest["created_at"] == "2026-11-16T12:00:00+00:00"
        assert [item["path"] for item in manifest["files"]] == [
            DATABASE_MEMBER,
            CONFIG_MEMBER,
        ]
        for item in manifest["files"]:
            payload = archive.read(item["path"])
            assert len(payload) == item["size"]
            assert hashlib.sha256(payload).hexdigest() == item["sha256"]
        assert manifest["totals"] == {
            "files": 2,
            "size": sum(item["size"] for item in manifest["files"]),
        }
        assert manifest["summary"]["config_source"] == "file"
        assert manifest["summary"]["database_present"] is True
        assert manifest["summary"]["secrets_included"] is False
        assert manifest["summary"]["properties"] == 1
        assert manifest["summary"]["snapshots"] == 1
        assert manifest["summary"]["latest_snapshot_at"] == "2026-11-10T06:00:00+00:00"

        config_text = archive.read(CONFIG_MEMBER).decode("utf-8")
        parsed = tomllib.loads(config_text)
        assert parsed["project"]["name"] == "Example"
        assert parsed["storage"]["data_dir"] == "./data"
        assert "token" not in config_text.lower()

        copy_path = tmp_path / "copy.sqlite3"
        copy_path.write_bytes(archive.read(DATABASE_MEMBER))

    archived_config = tmp_path / "archived.toml"
    archived_config.write_text(config_text, encoding="utf-8")
    reloaded = load_config(archived_config)
    assert reloaded.project.name == "Example"
    assert reloaded.storage.data_dir == tmp_path / "data"

    copy = sqlite3.connect(copy_path)
    try:
        assert copy.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert copy.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert copy.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
        assert copy.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    finally:
        copy.close()

    # The source workspace was only read: bytes and rows are unchanged. SQLite may create
    # its standard -wal/-shm sidecars while a WAL database is open; they carry no new data.
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == source_hash
    assert hashlib.sha256(config.read_bytes()).hexdigest() == config_hash
    names = {item.name for item in data_dir.iterdir()}
    assert names <= {"seo.sqlite3", "seo.sqlite3-wal", "seo.sqlite3-shm"}
    assert "secrets.json" not in names


def test_redact_config_document_masks_secret_keys() -> None:
    document = {
        "providers": {
            "gsc": {
                "property": "sc-domain:example.com",
                "token": "super-secret-token",
                "api_key": "key",
                "password": "pw",
            }
        },
        "report": {"email": {"smtp_user": "user@example.com", "from": "a@example.com"}},
        "authorization": "Bearer secret",
        "private_key": "pem",
        "items": [{"secret": "s"}, {"ok": 1}],
    }

    redacted = redact_config_document(document)

    assert redacted["providers"]["gsc"]["token"] == "***"
    assert redacted["providers"]["gsc"]["api_key"] == "***"
    assert redacted["providers"]["gsc"]["password"] == "***"
    assert redacted["providers"]["gsc"]["property"] == "sc-domain:example.com"
    assert redacted["report"]["email"]["smtp_user"] == "user@example.com"
    assert redacted["authorization"] == "***"
    assert redacted["private_key"] == "***"
    assert redacted["items"][0]["secret"] == "***"
    assert redacted["items"][1]["ok"] == 1


def test_invalid_config_is_redacted_not_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _init_database(data_dir / "seo.sqlite3")
    config = tmp_path / "seo.toml"
    config.write_text(
        "[project]\n"
        'name = "Example"\n'
        "[storage]\n"
        'data_dir = "./data"\n'
        "[providers.gsc]\n"
        'token = "super-secret-token"\n',
        encoding="utf-8",
    )
    out = _out_dir(tmp_path)

    result = create_backup(config_path=config, out_dir=out, now=NOW)

    assert any("not valid" in warning for warning in result.warnings)
    with zipfile.ZipFile(result.archive_path) as archive:
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
        assert manifest["summary"]["config_source"] == "raw"
        config_text = archive.read(CONFIG_MEMBER).decode("utf-8")
        assert "super-secret-token" not in config_text
        assert 'token = "***"' in config_text
        for name in archive.namelist():
            assert b"super-secret-token" not in archive.read(name)


def test_archive_never_includes_secrets_file(tmp_path: Path) -> None:
    config, data_dir, _ = _workspace(tmp_path)
    (data_dir / "secrets.json").write_text(
        json.dumps({"gsc_token": {"value": "super-secret-token"}}),
        encoding="utf-8",
    )
    out = _out_dir(tmp_path)

    result = create_backup(config_path=config, out_dir=out, now=NOW)

    with zipfile.ZipFile(result.archive_path) as archive:
        assert "secrets.json" not in archive.namelist()
        for name in archive.namelist():
            assert b"super-secret-token" not in archive.read(name)


def test_backup_copies_a_consistent_snapshot_during_a_write(tmp_path: Path) -> None:
    config, _, db_path = _workspace(tmp_path)
    out = _out_dir(tmp_path)
    writer = connect(db_path)
    try:
        writer.execute(
            "INSERT INTO properties (provider, property_id, display_name, verified) "
            "VALUES (?, ?, ?, ?)",
            ("bing", "committed", "Committed", 1),
        )
        writer.commit()
        writer.execute("BEGIN")
        writer.execute(
            "INSERT INTO properties (provider, property_id, display_name, verified) "
            "VALUES (?, ?, ?, ?)",
            ("bing", "uncommitted", "Uncommitted", 1),
        )

        result = create_backup(config_path=config, out_dir=out, now=NOW)
        writer.rollback()
    finally:
        writer.close()

    copy_path = tmp_path / "copy.sqlite3"
    with zipfile.ZipFile(result.archive_path) as archive:
        copy_path.write_bytes(archive.read(DATABASE_MEMBER))
    copy = sqlite3.connect(copy_path)
    try:
        names = {row[0] for row in copy.execute("SELECT property_id FROM properties")}
    finally:
        copy.close()
    assert "committed" in names
    assert "uncommitted" not in names


def test_overwrite_guard_and_force(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    out = _out_dir(tmp_path)
    first = create_backup(config_path=config, out_dir=out, now=NOW)

    with pytest.raises(SeoError) as exc:
        create_backup(config_path=config, out_dir=out, now=NOW)
    assert exc.value.code == INVALID_INPUT
    assert "already exists" in exc.value.message

    second = create_backup(config_path=config, out_dir=out, now=NOW, force=True)
    assert second.archive_path == first.archive_path
    assert [item.name for item in out.iterdir()] == [ARCHIVE_NAME]


def test_missing_config_and_data_is_an_error(tmp_path: Path) -> None:
    out = _out_dir(tmp_path)

    with pytest.raises(SeoError) as exc:
        create_backup(out_dir=out, cwd=tmp_path)
    assert exc.value.code == INVALID_INPUT
    assert "nothing to back up" in exc.value.message
    assert list(out.iterdir()) == []


def test_config_only_backup_warns_about_missing_database(tmp_path: Path) -> None:
    config = tmp_path / "seo.toml"
    config.write_text(
        '[project]\nname = "Example"\n[storage]\ndata_dir = "./data"\n',
        encoding="utf-8",
    )
    out = _out_dir(tmp_path)

    result = create_backup(config_path=config, out_dir=out, now=NOW)

    assert any("database" in warning for warning in result.warnings)
    with zipfile.ZipFile(result.archive_path) as archive:
        assert archive.namelist() == [MANIFEST_MEMBER, CONFIG_MEMBER]
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
        assert manifest["schema_version"] is None
        assert manifest["summary"]["database_present"] is False
        assert [item["path"] for item in manifest["files"]] == [CONFIG_MEMBER]


def test_database_only_backup_uses_defaults(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _init_database(data_dir / "seo.sqlite3")
    out = _out_dir(tmp_path)

    result = create_backup(out_dir=out, cwd=tmp_path, now=NOW)

    assert any("config" in warning.lower() for warning in result.warnings)
    with zipfile.ZipFile(result.archive_path) as archive:
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
        assert manifest["summary"]["config_source"] == "defaults"
        parsed = tomllib.loads(archive.read(CONFIG_MEMBER).decode("utf-8"))
        assert parsed["storage"]["data_dir"] == "./data"
        assert parsed["project"]["name"] == ""


def test_corrupt_database_is_typed_and_leaves_no_archive(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = tmp_path / "seo.toml"
    config.write_text('[storage]\ndata_dir = "./data"\n', encoding="utf-8")
    (data_dir / "seo.sqlite3").write_bytes(b"not a sqlite database" * 10)
    out = _out_dir(tmp_path)

    with pytest.raises(SeoError) as exc:
        create_backup(config_path=config, out_dir=out, now=NOW)
    assert exc.value.code == STORE_CORRUPT
    assert list(out.iterdir()) == []
    names = {item.name for item in data_dir.iterdir()}
    assert "seo.sqlite3" in names
    # The backup path must not call the store helper that writes a .corrupt-* copy.
    assert not any(name.startswith("seo.sqlite3.corrupt-") for name in names)


def test_unparseable_config_is_rejected_before_writing(tmp_path: Path) -> None:
    config = tmp_path / "seo.toml"
    config.write_text("[project\n", encoding="utf-8")
    out = _out_dir(tmp_path)

    with pytest.raises(SeoError) as exc:
        create_backup(config_path=config, out_dir=out, now=NOW)
    assert exc.value.code == CONFIG_INVALID
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("kind", ["relative", "missing", "file"])
def test_out_dir_must_be_an_existing_absolute_directory(tmp_path: Path, kind: str) -> None:
    config, _, _ = _workspace(tmp_path)
    if kind == "relative":
        out = Path("backups")
    elif kind == "missing":
        out = tmp_path / "missing"
    else:
        out = tmp_path / "file"
        out.write_text("x", encoding="utf-8")

    with pytest.raises(SeoError) as exc:
        create_backup(config_path=config, out_dir=out, now=NOW)
    assert exc.value.code == INVALID_INPUT


def test_source_size_is_bounded(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    out = _out_dir(tmp_path)

    with pytest.raises(SeoError) as exc:
        create_backup(config_path=config, out_dir=out, now=NOW, max_database_bytes=1)
    assert exc.value.code == INVALID_INPUT
    assert exc.value.detail["limit"] == 1
    assert list(out.iterdir()) == []


def test_backup_subapp_registers_under_the_root_command(tmp_path: Path) -> None:
    import typer

    config, _, _ = _workspace(tmp_path)
    out = _out_dir(tmp_path)
    root = typer.Typer()
    root.add_typer(backup_app, name="backup")

    result = RUNNER.invoke(
        root,
        ["backup", "create", "--out", str(out), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True


def test_cli_backup_create_json_envelope(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    out = _out_dir(tmp_path)

    result = RUNNER.invoke(
        backup_app,
        ["create", "--out", str(out), "--config", str(config), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["meta"]["read_only_source"] is True
    assert payload["data"]["archive_path"].endswith(".zip")
    assert payload["data"]["archive_size"] > 0
    assert len(payload["data"]["archive_sha256"]) == 64
    assert payload["data"]["manifest"]["format"] == "lv24-seo-backup"
    assert payload["data"]["summary"]["snapshots"] == 1


def test_cli_backup_create_human_output(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    out = _out_dir(tmp_path)

    result = RUNNER.invoke(backup_app, ["create", "--out", str(out), "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert "backup:" in result.output
    assert "sha256:" in result.output
    assert "snapshots: 1" in result.output


def test_cli_backup_rejects_relative_out(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)

    result = RUNNER.invoke(
        backup_app,
        ["create", "--out", "backups", "--config", str(config), "--json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT
