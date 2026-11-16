"""Contract tests for ``backup restore`` (engine 0.7.0, package A).

The tests drive the public core functions and the Typer sub-app only: the archive is built by
``create_backup`` (or crafted by hand for hostile cases), and every assertion checks the
observable result — envelope shape, exit codes, files on disk, hashes and SQLite contents.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import stat
import tomllib
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli.backup_cmd import backup_app
from lv24_seo.core.backup import (
    BACKUP_FORMAT,
    CONFIG_MEMBER,
    DATABASE_MEMBER,
    MANIFEST_MEMBER,
    RESTORE_TOKEN_TTL_SECONDS,
    apply_restore,
    create_backup,
    plan_restore,
    restore_to_directory,
)
from lv24_seo.core.errors import (
    CONFIRM_REQUIRED,
    INVALID_INPUT,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.store import connect, initialize
from lv24_seo.store.db import SCHEMA_VERSION

RUNNER = CliRunner()
NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
STAMP = "20261116T120000Z"
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


def _init_database(path: Path, *, property_id: str = "sc-domain:example.com") -> None:
    conn = connect(path)
    try:
        initialize(conn, path=path)
        conn.execute(
            "INSERT INTO properties (provider, property_id, display_name, verified) "
            "VALUES (?, ?, ?, ?)",
            ("gsc", property_id, property_id, 1),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(kind, property_id, period, content_hash, payload_gz, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "weekly",
                property_id,
                "2026-W45",
                "abc123",
                gzip.compress(b'{"ok":true}'),
                "2026-11-10T06:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _workspace(root: Path, name: str = "src") -> tuple[Path, Path, Path]:
    base = root / name
    data_dir = base / "data"
    data_dir.mkdir(parents=True)
    config = base / "seo.toml"
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


def _target(root: Path, name: str = "target") -> Path:
    base = root / name
    base.mkdir()
    return base / "seo.toml"


def _archive(tmp_path: Path, config: Path) -> Path:
    out = tmp_path / "backups"
    out.mkdir()
    result = create_backup(out_dir=out, config_path=config, now=NOW)
    return result.archive_path


def _manifest_document(files: dict[str, bytes], **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "format_version": 1,
        "engine_version": "0.6.0",
        "schema_version": SCHEMA_VERSION,
        "engine_schema_version": SCHEMA_VERSION,
        "created_at": "2026-11-16T12:00:00+00:00",
        "files": [
            {
                "path": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(files.items())
        ],
        "summary": {},
        "limitations": [],
    }
    document.update(overrides)
    return document


def _build_archive(
    path: Path,
    *,
    files: dict[str, bytes],
    manifest: dict[str, Any] | None = None,
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    raw_manifest = json.dumps(manifest or _manifest_document(files)).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, raw_manifest)
        for name, payload in files.items():
            archive.writestr(name, payload)
        for name, payload in (extra_members or {}).items():
            archive.writestr(name, payload)
    return path


def _valid_config_bytes() -> bytes:
    return b'[project]\nname = "Example"\n[storage]\ndata_dir = "./data"\n'


def _minimal_archive(tmp_path: Path, *, name: str = "minimal.zip") -> Path:
    config = _valid_config_bytes()
    files = {CONFIG_MEMBER: config}
    return _build_archive(tmp_path / name, files=files)


def _rewrite_member(path: Path, member: str, payload: bytes) -> None:
    with zipfile.ZipFile(path) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w") as archive:
        for info, original in entries:
            archive.writestr(info, payload if info.filename == member else original)


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(result.output)


def _apply(plan: Any, token: str | None = None) -> Any:
    return apply_restore(plan, token or plan.confirm_token, allow_write=True, now=NOW)


# -- core: plan and apply --------------------------------------------------------------------


def test_plan_restore_is_read_only_and_returns_a_token(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)

    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert plan.archive_path == archive
    assert plan.archive_size == archive.stat().st_size
    assert plan.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert plan.config_action == "create"
    assert plan.config_restore_path == target
    assert plan.data_dir == target.parent / "data"
    assert plan.database_path == plan.data_dir / "seo.sqlite3"
    assert plan.database_member is True
    assert plan.existing_database is False
    assert plan.existing_config is False
    assert plan.config_valid is True
    assert plan.manifest.summary_dict()["properties"] == 1
    assert plan.manifest.summary_dict()["snapshots"] == 1
    assert len(plan.plan_hash) == 64
    expiry, _, digest = plan.confirm_token.partition(":")
    assert expiry.isdigit()
    assert int(expiry) == int(NOW.timestamp()) + RESTORE_TOKEN_TTL_SECONDS
    assert len(digest) == 64
    assert plan.expires_at == "2026-11-16T12:05:00+00:00"

    # The plan never writes to the target workspace.
    assert not target.exists()
    assert not (target.parent / "data").exists()
    assert [item.name for item in target.parent.iterdir()] == []


def test_apply_restore_replaces_database_and_config(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)

    result = _apply(plan)

    assert result.status == "applied"
    assert result.database_restored is True
    assert result.config_restored is True
    assert result.database_sha256 == plan.database_sha256
    assert result.config_sha256 == plan.config_sha256
    assert result.properties == 1
    assert result.snapshots == 1
    assert result.schema_version == SCHEMA_VERSION
    assert result.latest_snapshot_at == "2026-11-10T06:00:00+00:00"
    assert result.pre_restore_database is None
    assert result.pre_restore_config is None

    restored_config = tomllib.loads(target.read_text(encoding="utf-8"))
    assert restored_config["project"]["name"] == "Example"
    assert restored_config["storage"]["data_dir"] == "./data"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == plan.config_sha256

    db_path = target.parent / "data" / "seo.sqlite3"
    assert db_path.is_file()
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == plan.database_sha256
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    finally:
        conn.close()
    # The extraction directory is removed; only the database remains.
    assert [item.name for item in (target.parent / "data").iterdir()] == ["seo.sqlite3"]


def test_apply_without_allow_write_is_refused(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)

    with pytest.raises(SeoError) as exc:
        apply_restore(plan, plan.confirm_token, allow_write=False, now=NOW)

    assert exc.value.code == WRITE_DISABLED
    assert not target.exists()


def test_wrong_confirm_token_is_refused(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)

    with pytest.raises(SeoError) as exc:
        apply_restore(plan, "bogus-token", allow_write=True, now=NOW)

    assert exc.value.code == CONFIRM_REQUIRED
    assert not target.exists()
    assert not (target.parent / "data").exists()


def test_confirm_token_cannot_be_replayed(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)
    _apply(plan)
    db_path = target.parent / "data" / "seo.sqlite3"
    restored_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()

    with pytest.raises(SeoError) as exc:
        _apply(plan)

    assert exc.value.code == CONFIRM_REQUIRED
    assert "changed after the plan" in exc.value.message
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == restored_hash


def test_expired_confirm_token_is_refused(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)

    with pytest.raises(SeoError) as exc:
        apply_restore(
            plan,
            plan.confirm_token,
            allow_write=True,
            now=NOW + timedelta(seconds=RESTORE_TOKEN_TTL_SECONDS + 1),
        )

    assert exc.value.code == CONFIRM_REQUIRED
    assert "expired" in exc.value.message
    assert not target.exists()


def test_token_from_another_plan_is_refused(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    first_target = _target(tmp_path, "first")
    second_target = _target(tmp_path, "second")
    first = plan_restore(archive_path=archive, config_path=first_target, now=NOW)
    second = plan_restore(archive_path=archive, config_path=second_target, now=NOW)
    assert first.confirm_token != second.confirm_token

    with pytest.raises(SeoError) as exc:
        apply_restore(second, first.confirm_token, allow_write=True, now=NOW)

    assert exc.value.code == CONFIRM_REQUIRED
    assert not second_target.exists()


def test_a_fresh_plan_can_restore_again(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    _apply(plan_restore(archive_path=archive, config_path=target, now=NOW))

    second = plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert second.existing_database is True
    assert second.existing_config is True
    assert second.pre_restore_database is not None
    result = _apply(second)
    assert result.status == "applied"
    assert result.pre_restore_database is not None
    assert result.pre_restore_database.is_file()


def test_changed_archive_is_refused(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)

    # Rebuild the same members with a different manifest (created_at changes the bytes).
    with zipfile.ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist() if name != MANIFEST_MEMBER}
    document = _manifest_document(members, created_at="2026-11-16T13:00:00+00:00")
    _build_archive(archive, files=members, manifest=document)

    with pytest.raises(SeoError) as exc:
        _apply(plan)

    assert exc.value.code == CONFIRM_REQUIRED
    assert "manifest changed" in exc.value.message
    assert not target.exists()


# -- core: hostile archives ------------------------------------------------------------------


def test_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    _rewrite_member(archive, DATABASE_MEMBER, b"tampered database bytes")
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT
    assert "does not match the manifest" in exc.value.message


@pytest.mark.parametrize(
    "member",
    ["../evil.txt", "/evil.txt", "data/../../evil.txt", "C:\\evil.txt", "data//evil.txt"],
)
def test_zip_slip_member_is_rejected(tmp_path: Path, member: str) -> None:
    payload = b"evil"
    files = {CONFIG_MEMBER: _valid_config_bytes(), member: payload}
    archive = _build_archive(tmp_path / "slip.zip", files=files)
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT
    assert "unsafe member path" in exc.value.message
    assert not target.exists()


def test_symlink_member_is_rejected(tmp_path: Path) -> None:
    payload = _valid_config_bytes()
    archive_path = tmp_path / "symlink.zip"
    manifest = _manifest_document({CONFIG_MEMBER: payload})
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(MANIFEST_MEMBER, json.dumps(manifest))
        info = zipfile.ZipInfo(CONFIG_MEMBER)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, payload)
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=archive_path, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT
    assert "not a regular file" in exc.value.message


def test_extra_member_is_rejected(tmp_path: Path) -> None:
    files = {CONFIG_MEMBER: _valid_config_bytes()}
    archive = _build_archive(
        tmp_path / "extra.zip",
        files=files,
        extra_members={"data/extra.sqlite3": b"x"},
    )
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT
    assert "not allowed" in exc.value.message


def test_wrong_format_version_is_rejected(tmp_path: Path) -> None:
    files = {CONFIG_MEMBER: _valid_config_bytes()}
    manifest = _manifest_document(files, format_version=2)
    archive = _build_archive(tmp_path / "v2.zip", files=files, manifest=manifest)
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT
    assert "unsupported backup format version 2" in exc.value.message


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    files = {CONFIG_MEMBER: _valid_config_bytes()}
    manifest = _manifest_document(files, schema_version=SCHEMA_VERSION + 1)
    archive = _build_archive(tmp_path / "newer.zip", files=files, manifest=manifest)
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT
    assert "newer than this engine" in exc.value.message


def test_archive_size_cap_is_enforced(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(
            archive_path=archive,
            config_path=target,
            now=NOW,
            max_database_bytes=1,
        )

    assert exc.value.code == INVALID_INPUT
    assert exc.value.detail["limit"] == 1


def test_corrupt_archived_database_is_rejected_at_apply(tmp_path: Path) -> None:
    database = b"this is not a sqlite database" * 10
    files = {CONFIG_MEMBER: _valid_config_bytes(), DATABASE_MEMBER: database}
    archive = _build_archive(tmp_path / "corrupt.zip", files=files)
    target = _target(tmp_path)
    plan = plan_restore(archive_path=archive, config_path=target, now=NOW)

    with pytest.raises(SeoError) as exc:
        _apply(plan)

    assert exc.value.code == INVALID_INPUT
    assert "not a valid SQLite file" in exc.value.message
    assert not target.exists()
    assert not (target.parent / "data" / "seo.sqlite3").exists()


# -- core: existing workspace and config handling --------------------------------------------


def test_existing_database_and_config_are_backed_up(tmp_path: Path) -> None:
    source_config, _, _ = _workspace(tmp_path, "source")
    archive = _archive(tmp_path, source_config)
    target_config, target_data, target_db = _workspace(tmp_path, "target")
    old_config_bytes = target_config.read_bytes()
    conn = sqlite3.connect(target_db)
    try:
        conn.execute(
            "INSERT INTO properties (provider, property_id, display_name, verified) "
            "VALUES (?, ?, ?, ?)",
            ("bing", "old-property", "Old", 1),
        )
        conn.commit()
    finally:
        conn.close()

    plan = plan_restore(archive_path=archive, config_path=target_config, now=NOW)

    assert plan.existing_database is True
    assert plan.existing_config is True
    assert plan.config_action == "replace"
    assert plan.pre_restore_database == target_data / f"seo.sqlite3.pre-restore-{STAMP}"
    assert plan.pre_restore_config == target_config.parent / f"seo.toml.pre-restore-{STAMP}"

    result = _apply(plan)

    assert result.pre_restore_database is not None
    assert result.pre_restore_database.is_file()
    assert result.pre_restore_config is not None
    assert result.pre_restore_config.read_bytes() == old_config_bytes
    conn = sqlite3.connect(result.pre_restore_database)
    try:
        names = {row[0] for row in conn.execute("SELECT property_id FROM properties")}
    finally:
        conn.close()
    assert names == {"sc-domain:example.com", "old-property"}

    conn = sqlite3.connect(target_db)
    try:
        names = {row[0] for row in conn.execute("SELECT property_id FROM properties")}
    finally:
        conn.close()
    assert names == {"sc-domain:example.com"}
    assert result.properties == 1


def test_config_only_archive_leaves_database_untouched(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_config = source / "seo.toml"
    source_config.write_text(
        '[project]\nname = "Config only"\n[storage]\ndata_dir = "./data"\n',
        encoding="utf-8",
    )
    out = tmp_path / "backups"
    out.mkdir()
    archive = create_backup(out_dir=out, config_path=source_config, now=NOW).archive_path
    target_config, _, target_db = _workspace(tmp_path, "target")
    before = hashlib.sha256(target_db.read_bytes()).hexdigest()

    plan = plan_restore(archive_path=archive, config_path=target_config, now=NOW)
    assert plan.database_member is False
    assert plan.pre_restore_database is None
    assert any("no database" in warning for warning in plan.warnings)

    result = _apply(plan)

    assert result.database_restored is False
    assert result.database_sha256 is None
    assert result.properties is None
    assert hashlib.sha256(target_db.read_bytes()).hexdigest() == before
    assert tomllib.loads(target_config.read_text(encoding="utf-8"))["project"]["name"] == (
        "Config only"
    )


def test_invalid_archived_config_is_written_alongside(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").mkdir()
    _init_database(source / "data" / "seo.sqlite3")
    source_config = source / "seo.toml"
    source_config.write_text(
        "[project]\n"
        'name = "Example"\n'
        "[storage]\n"
        'data_dir = "./data"\n'
        "[providers.gsc]\n"
        'token = "super-secret-token"\n',
        encoding="utf-8",
    )
    out = tmp_path / "backups"
    out.mkdir()
    archive = create_backup(out_dir=out, config_path=source_config, now=NOW).archive_path
    target_config, _, _ = _workspace(tmp_path, "target")
    original = target_config.read_bytes()

    plan = plan_restore(archive_path=archive, config_path=target_config, now=NOW)

    assert plan.config_valid is False
    assert plan.config_action == "write_alongside"
    assert plan.config_restore_path.name == f"seo.toml.restored-{STAMP}"
    assert any("does not validate" in warning for warning in plan.warnings)

    result = _apply(plan)

    assert result.config_restored is True
    assert target_config.read_bytes() == original
    alongside = plan.config_restore_path
    assert alongside.is_file()
    text = alongside.read_text(encoding="utf-8")
    assert "super-secret-token" not in text
    assert 'token = "***"' in text
    assert result.config_sha256 == hashlib.sha256(alongside.read_bytes()).hexdigest()


@pytest.mark.parametrize("kind", ["relative", "missing", "directory"])
def test_archive_path_must_be_absolute_and_regular(tmp_path: Path, kind: str) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    if kind == "relative":
        candidate = Path(archive.name)
    elif kind == "missing":
        candidate = tmp_path / "missing.zip"
    else:
        candidate = tmp_path
    target = _target(tmp_path)

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=candidate, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT


def test_config_parent_must_exist(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = tmp_path / "missing" / "seo.toml"

    with pytest.raises(SeoError) as exc:
        plan_restore(archive_path=archive, config_path=target, now=NOW)

    assert exc.value.code == INVALID_INPUT
    assert "config directory does not exist" in exc.value.message


# -- CLI contract ----------------------------------------------------------------------------


def test_cli_restore_plan_json_envelope(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--config", str(target), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["meta"]["dry_run"] is True
    assert payload["meta"]["destructive"] is True
    assert payload["meta"]["format"] == BACKUP_FORMAT
    data = payload["data"]
    assert data["status"] == "planned"
    assert data["confirm_token"]
    assert data["plan_hash"]
    assert data["target"]["config_path"] == str(target)
    assert data["database"]["in_archive"] is True
    assert data["summary"]["snapshots"] == 1
    assert not target.exists()


def test_cli_restore_apply_json_envelope(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    dry = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--config", str(target), "--json"],
    )
    assert dry.exit_code == 0, dry.output
    token = _payload(dry)["data"]["confirm_token"]

    result = RUNNER.invoke(
        backup_app,
        [
            "restore",
            "--file",
            str(archive),
            "--config",
            str(target),
            "--allow-write",
            "--confirm",
            token,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["meta"]["dry_run"] is False
    data = payload["data"]
    assert data["status"] == "applied"
    assert data["database"]["restored"] is True
    assert data["database"]["properties"] == 1
    assert data["database"]["snapshots"] == 1
    assert data["config"]["restored"] is True
    assert target.is_file()


def test_cli_restore_wrong_token_exit_code(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)

    result = RUNNER.invoke(
        backup_app,
        [
            "restore",
            "--file",
            str(archive),
            "--config",
            str(target),
            "--allow-write",
            "--confirm",
            "not-a-token",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == CONFIRM_REQUIRED
    assert not target.exists()


def test_cli_restore_apply_without_allow_write_is_refused(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)
    dry = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--config", str(target), "--json"],
    )
    token = _payload(dry)["data"]["confirm_token"]

    result = RUNNER.invoke(
        backup_app,
        [
            "restore",
            "--file",
            str(archive),
            "--config",
            str(target),
            "--confirm",
            token,
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = _payload(result)
    assert payload["error"]["code"] == WRITE_DISABLED
    assert not target.exists()


def test_cli_restore_relative_file_exit_code(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", archive.name, "--config", str(target), "--json"],
    )

    assert result.exit_code == 2, result.output
    payload = _payload(result)
    assert payload["error"]["code"] == INVALID_INPUT
    assert "must be absolute" in payload["error"]["message"]


def test_cli_restore_human_output(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--config", str(target)],
    )

    assert result.exit_code == 0, result.output
    assert "restore:" in result.output
    assert "status: planned" in result.output
    assert "archive sha256:" in result.output
    assert "confirm token:" in result.output
    assert "--allow-write --confirm" in result.output


def test_cli_restore_allow_write_without_confirm_writes_nothing(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = _target(tmp_path)

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--config", str(target), "--allow-write", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["meta"]["dry_run"] is True
    assert any("nothing was written" in item for item in payload["warnings"])
    assert not target.exists()


# -- non-destructive restore to a new directory ----------------------------------------------


def test_restore_to_directory_extracts_config_database_and_snapshots(tmp_path: Path) -> None:
    config, _, db_path = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    source_config = config.read_bytes()
    source_db = hashlib.sha256(db_path.read_bytes()).hexdigest()
    target = tmp_path / "restored"

    result = restore_to_directory(archive_path=archive, directory=target)

    assert result.directory == target
    assert result.config_path == target / "seo.toml"
    assert result.database_path == target / "data" / "seo.sqlite3"
    assert result.database_in_archive is True
    assert result.database_sha256 is not None
    assert result.quick_check == "ok"
    assert result.properties == 1
    assert result.snapshots == 1
    assert result.schema_version == SCHEMA_VERSION
    assert result.warnings == ()
    assert result.config_path.is_file()
    assert result.database_path is not None and result.database_path.is_file()
    assert sorted(item.name for item in target.iterdir()) == ["data", "seo.toml"]

    restored = tomllib.loads(result.config_path.read_text(encoding="utf-8"))
    assert restored["storage"]["data_dir"] == "./data"
    conn = sqlite3.connect(result.database_path)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
    finally:
        conn.close()

    # The current workspace is byte-for-byte untouched.
    assert config.read_bytes() == source_config
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == source_db


@pytest.mark.parametrize("non_empty", [False, True])
def test_restore_to_directory_refuses_an_existing_directory(
    tmp_path: Path, non_empty: bool
) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = tmp_path / "restored"
    target.mkdir()
    if non_empty:
        (target / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(SeoError) as exc:
        restore_to_directory(archive_path=archive, directory=target)

    assert exc.value.code == INVALID_INPUT
    assert "already exists" in exc.value.message
    assert not (target / "seo.toml").exists()
    if non_empty:
        assert (target / "keep.txt").read_text(encoding="utf-8") == "user data"


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt", "C:/evil.txt"])
def test_restore_to_directory_rejects_zip_slip_members(tmp_path: Path, member: str) -> None:
    archive = _build_archive(
        tmp_path / "hostile.zip",
        files={CONFIG_MEMBER: _valid_config_bytes()},
        extra_members={member: b"payload"},
    )
    target = tmp_path / "restored"

    with pytest.raises(SeoError) as exc:
        restore_to_directory(archive_path=archive, directory=target)

    assert exc.value.code == INVALID_INPUT
    assert "unsafe member" in exc.value.message
    assert not target.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_restore_to_directory_verifies_member_hashes(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    _rewrite_member(archive, DATABASE_MEMBER, b"not a database")
    target = tmp_path / "restored"

    with pytest.raises(SeoError) as exc:
        restore_to_directory(archive_path=archive, directory=target)

    assert exc.value.code == INVALID_INPUT
    assert "does not match the manifest" in exc.value.message
    assert not target.exists()


def test_restore_to_directory_rejects_a_corrupt_database(tmp_path: Path) -> None:
    archive = _build_archive(
        tmp_path / "corrupt.zip",
        files={CONFIG_MEMBER: _valid_config_bytes(), DATABASE_MEMBER: b"not a database"},
    )
    target = tmp_path / "restored"

    with pytest.raises(SeoError) as exc:
        restore_to_directory(archive_path=archive, directory=target)

    assert exc.value.code == INVALID_INPUT
    assert "not a valid SQLite file" in exc.value.message
    assert not target.exists()


def test_restore_to_directory_without_database_reports_honestly(tmp_path: Path) -> None:
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "restored"

    result = restore_to_directory(archive_path=archive, directory=target)

    assert result.database_path is None
    assert result.database_in_archive is False
    assert result.quick_check is None
    assert result.snapshots is None
    assert any("contains no database" in item for item in result.warnings)
    assert (target / "seo.toml").is_file()
    assert not (target / "data").exists()


def test_restore_to_directory_requires_an_existing_parent(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)

    with pytest.raises(SeoError) as exc:
        restore_to_directory(archive_path=archive, directory=tmp_path / "missing" / "restored")

    assert exc.value.code == INVALID_INPUT
    assert "parent does not exist" in exc.value.message


def test_cli_restore_to_directory_json_envelope(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = tmp_path / "restored"

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--to", str(target), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["meta"]["dry_run"] is False
    assert payload["meta"]["destructive"] is False
    assert payload["meta"]["writes_new_directory"] is True
    assert payload["meta"]["format"] == BACKUP_FORMAT
    data = payload["data"]
    assert data["status"] == "restored"
    assert data["mode"] == "to_directory"
    assert data["target"]["directory"] == str(target)
    assert data["target"]["config_path"] == str(target / "seo.toml")
    assert data["target"]["database_path"] == str(target / "data" / "seo.sqlite3")
    assert data["database"]["in_archive"] is True
    assert data["database"]["restored"] is True
    assert data["database"]["quick_check"] == "ok"
    assert data["database"]["snapshots"] == 1
    assert data["config"]["valid"] is True
    assert "confirm_token" not in data
    assert payload["warnings"] == []
    assert target.is_dir()


def test_cli_restore_to_directory_human_output(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = tmp_path / "restored"

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--to", str(target)],
    )

    assert result.exit_code == 0, result.output
    assert "restore:" in result.output
    assert "mode: new directory" in result.output
    assert "status: restored" in result.output
    assert f"directory: {target}" in result.output
    assert "quick_check: ok" in result.output
    assert "snapshots: 1" in result.output
    assert "confirm token:" not in result.output


def test_cli_restore_to_and_config_are_mutually_exclusive(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = tmp_path / "restored"

    result = RUNNER.invoke(
        backup_app,
        [
            "restore",
            "--file",
            str(archive),
            "--to",
            str(target),
            "--config",
            str(tmp_path / "seo.toml"),
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT
    assert "mutually exclusive" in payload["error"]["message"]
    assert not target.exists()


def test_cli_restore_requires_to_or_config(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)

    result = RUNNER.invoke(backup_app, ["restore", "--file", str(archive), "--json"])

    assert result.exit_code == 2, result.output
    payload = _payload(result)
    assert payload["error"]["code"] == INVALID_INPUT
    assert "needs --to DIR or --config ABS" in payload["error"]["message"]


def test_cli_restore_to_rejects_write_flags(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)
    target = tmp_path / "restored"

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--to", str(target), "--allow-write", "--json"],
    )

    assert result.exit_code == 2, result.output
    payload = _payload(result)
    assert payload["error"]["code"] == INVALID_INPUT
    assert "only apply to the in-place restore" in payload["error"]["message"]
    assert not target.exists()


def test_cli_restore_to_requires_an_absolute_directory(tmp_path: Path) -> None:
    config, _, _ = _workspace(tmp_path)
    archive = _archive(tmp_path, config)

    result = RUNNER.invoke(
        backup_app,
        ["restore", "--file", str(archive), "--to", "restored", "--json"],
    )

    assert result.exit_code == 2, result.output
    payload = _payload(result)
    assert payload["error"]["code"] == INVALID_INPUT
    assert "must be absolute" in payload["error"]["message"]
    assert not (tmp_path / "restored").exists()
