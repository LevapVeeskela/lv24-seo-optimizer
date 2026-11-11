"""F24: one shared composition root for CLI and MCP with a common SQLite history."""

from __future__ import annotations

from pathlib import Path

from lv24_seo.composition import open_store, open_workspace


def _toml(data_dir: Path) -> Path:
    path = data_dir / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Ex"\n'
        'base_url = "https://example.com/"\n'
        'default_property = "sc-domain:example.com"\n'
        "[storage]\n"
        f'data_dir = "{data_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    return path


def test_open_store_returns_none_without_init(tmp_path: Path) -> None:
    import lv24_seo.core.config as config_module

    config = config_module.SeoConfig.model_validate(
        {"storage": {"data_dir": str(tmp_path / "missing")}}
    )
    assert open_store(config) is None


def test_two_workspaces_share_one_history(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = _toml(data_dir)

    first = open_workspace(config_path)
    try:
        assert first.store is not None
        first.store.save_snapshot("weekly", "p", "2026-W45", {"headline": "one"})
    finally:
        first.close()

    second = open_workspace(config_path)
    try:
        assert second.store is not None
        assert second.store.load_snapshot("weekly", "p", "2026-W45") == {"headline": "one"}
    finally:
        second.close()


def test_mcp_default_server_opens_shared_store(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import lv24_seo.composition as composition
    from lv24_seo.mcp import server as server_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = _toml(data_dir)
    monkeypatch.chdir(tmp_path)

    import lv24_seo.core.config as config_module

    real_load = config_module.load_config
    monkeypatch.setattr(
        composition,
        "load_config",
        lambda path=None: real_load(config_path if path is None else path),
    )
    # server.py imports open_workspace lazily; patching composition.load_config is enough.
    server_module.create_server(None)
    try:
        assert server_module._OPEN_WORKSPACES, "default MCP server must own a workspace"
        workspace = server_module._OPEN_WORKSPACES[-1]
        assert workspace.store is not None
        workspace.store.save_snapshot("weekly", "p", "2026-W45", {"headline": "mcp"})
        assert workspace.store.load_snapshot("weekly", "p", "2026-W45") == {"headline": "mcp"}
        # CLI history is the same database file.
        cli_store = open_store(workspace.config)
        assert cli_store is not None
        try:
            assert cli_store.load_snapshot("weekly", "p", "2026-W45") == {"headline": "mcp"}
        finally:
            cli_store.close()
    finally:
        server_module.close_workspaces()
        assert not server_module._OPEN_WORKSPACES
