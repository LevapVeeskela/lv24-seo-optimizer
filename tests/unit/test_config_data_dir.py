"""Regression tests: a relative ``storage.data_dir`` belongs to the config file.

Before the fix a relative ``data_dir`` was resolved against the process working directory.
``lv24-seo init --config /srv/site/seo.toml`` created ``/srv/site/data``, but running
``lv24-seo --config /srv/site/seo.toml crawl`` from ``/home/me`` looked for ``/home/me/data``
instead: the store silently never opened, so crawl results and report snapshots were dropped
without an error. These tests pin the anchored behaviour and the two intentional exceptions
(built-in default and ``LV24_SEO_DATA_DIR``).
"""

from __future__ import annotations

from pathlib import Path

import platformdirs
import pytest

from lv24_seo.composition import open_store, open_workspace
from lv24_seo.core.config import data_dir_path, load_config, resolve_config_path


@pytest.fixture(autouse=True)
def _user_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never read the real per-user config file from these tests."""
    root = tmp_path / "user-config"
    monkeypatch.setattr(platformdirs, "user_config_dir", lambda *args, **kwargs: str(root))
    return root


def _write_config(directory: Path, body: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "seo.toml"
    path.write_text(
        '[project]\nname = "example.com"\n[storage]\ndata_dir = "./data"\n' + body,
        encoding="utf-8",
    )
    return path


def test_relative_data_dir_is_anchored_to_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "srv" / "site"
    config_path = _write_config(site)
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)

    config = load_config(config_path)

    assert config.storage.data_dir == site / "data"
    assert config.storage.data_dir.is_absolute()


def test_absolute_config_from_foreign_cwd_opens_the_store_next_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end symptom: persistence must not depend on the working directory."""
    site = tmp_path / "srv" / "site"
    config_path = _write_config(site)
    (site / "data").mkdir()
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)

    workspace = open_workspace(config_path)
    try:
        assert workspace.store is not None
        assert Path(workspace.store.path) == site / "data" / "seo.sqlite3"
        workspace.store.save_snapshot("weekly", "p", "2026-W46", {"headline": "anchored"})
    finally:
        workspace.close()

    assert not (foreign_cwd / "data").exists()

    # A second run from yet another directory sees the same history.
    other = tmp_path / "third"
    other.mkdir()
    monkeypatch.chdir(other)
    reopened = open_workspace(config_path)
    try:
        assert reopened.store is not None
        assert reopened.store.load_snapshot("weekly", "p", "2026-W46") == {"headline": "anchored"}
    finally:
        reopened.close()


def test_open_store_follows_the_anchored_directory(tmp_path: Path) -> None:
    site = tmp_path / "site"
    config_path = _write_config(site)
    (site / "data").mkdir()

    store = open_store(load_config(config_path))
    assert store is not None
    try:
        assert Path(store.path) == site / "data" / "seo.sqlite3"
    finally:
        store.close()


def test_absolute_data_dir_is_left_alone(tmp_path: Path) -> None:
    custom = tmp_path / "custom-data"
    config_path = tmp_path / "site" / "seo.toml"
    config_path.parent.mkdir()
    config_path.write_text(f'[storage]\ndata_dir = "{custom.as_posix()}"\n', encoding="utf-8")

    assert load_config(config_path).storage.data_dir == custom


def test_builtin_default_stays_relative_to_the_working_directory(tmp_path: Path) -> None:
    """Compatibility: a config without ``[storage]`` keeps the historical scope."""
    config_path = tmp_path / "site" / "seo.toml"
    config_path.parent.mkdir()
    config_path.write_text('[project]\nname = "x"\n', encoding="utf-8")

    config = load_config(config_path)

    assert config.storage.data_dir == Path("data")
    assert not config.storage.data_dir.is_absolute()


def test_env_override_stays_process_scoped(tmp_path: Path) -> None:
    """``LV24_SEO_DATA_DIR`` is a shell variable: it means "here", not "next to the config"."""
    config_path = _write_config(tmp_path / "site")

    config = load_config(config_path, env={"LV24_SEO_DATA_DIR": "./env-data"})

    assert config.storage.data_dir == Path("./env-data")
    assert not config.storage.data_dir.is_absolute()


def test_env_override_wins_over_the_anchored_file_value(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path / "site")

    config = load_config(config_path, env={"LV24_SEO_DATA_DIR": "/srv/shared/seo"})

    assert config.storage.data_dir == Path("/srv/shared/seo")


def test_tilde_in_a_file_value_is_expanded(tmp_path: Path) -> None:
    config_path = tmp_path / "site" / "seo.toml"
    config_path.parent.mkdir()
    config_path.write_text('[storage]\ndata_dir = "~/.lv24-seo"\n', encoding="utf-8")

    resolved = load_config(config_path).storage.data_dir

    assert resolved.is_absolute()
    assert resolved == Path.home() / ".lv24-seo"


def test_data_dir_path_expands_tilde_for_the_default(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)

    assert data_dir_path(config) == Path("data")


def test_resolve_config_path_returns_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``--config`` must not leak into a scheduler command or a path anchor."""
    config_path = _write_config(tmp_path / "site")
    monkeypatch.chdir(tmp_path / "site")

    assert resolve_config_path(Path("seo.toml")) == config_path
    assert resolve_config_path(None) == config_path

    nested = tmp_path / "site" / "sub"
    nested.mkdir()
    monkeypatch.chdir(nested)
    # Explicit relative path with a parent segment is normalised, not left dangling.
    assert resolve_config_path(Path("../seo.toml")) == config_path
