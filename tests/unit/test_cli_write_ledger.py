"""Write-gate replay protection through the CLI (ADR-0003).

A ``confirm_token`` is valid for five minutes and is not consumed by verification, so the
only thing that stops the *same* token from writing twice is the idempotency ledger. The CLI
used to build ``WriteGate`` without a store, which meant one token could submit the same
IndexNow payload repeatedly. These tests pin the ledger wiring and the honest warning when no
workspace exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main

RUNNER = CliRunner()
_SECRET = "write-secret-for-tests"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("LV24_SEO_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An initialised workspace: ``seo.toml`` plus the data directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "seo.toml").write_text('[project]\nname = "Example"\n', encoding="utf-8")
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture()
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, list[str]]]:
    """Replace the real IndexNow POST with a recorder."""
    calls: list[tuple[str, str, list[str]]] = []

    def fake_submit(
        *, host: str, key: str, urls: list[str], key_location: str | None = None
    ) -> dict[str, Any]:
        calls.append((host, key, list(urls)))
        return {"status": 200, "submitted": len(urls)}

    monkeypatch.setattr(cli_main, "indexnow_submit", fake_submit)
    return calls


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _payload(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _plan(monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str, list[str]]]) -> str:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", _SECRET)
    payload = _payload(
        _invoke(
            "indexnow",
            "submit",
            "--host",
            "example.com",
            "--key",
            "abc123",
            "--url",
            "https://example.com/a",
            "--allow-write",
            "--json",
        )
    )
    assert sent == [], "a dry run must never reach the provider"
    token = payload["data"]["confirm_token"]
    assert token
    assert payload["warnings"] == []
    return str(token)


def _apply(token: str) -> dict[str, Any]:
    return _payload(
        _invoke(
            "indexnow",
            "submit",
            "--host",
            "example.com",
            "--key",
            "abc123",
            "--url",
            "https://example.com/a",
            "--allow-write",
            "--confirm",
            token,
            "--json",
        )
    )


def test_replayed_token_is_skipped_when_the_workspace_has_a_ledger(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str, list[str]]]
) -> None:
    assert workspace  # the fixture initialised seo.toml and data/
    token = _plan(monkeypatch, sent)

    first = _apply(token)
    second = _apply(token)

    assert first["data"]["status"] == "applied"
    assert second["data"]["status"] == "skipped"
    assert second["data"]["reason"] == "duplicate"
    assert len(sent) == 1, "the provider must be called exactly once"


def test_ledger_warning_when_no_workspace_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str, list[str]]]
) -> None:
    monkeypatch.chdir(tmp_path)
    token = _plan(monkeypatch, sent)

    payload = _apply(token)

    assert payload["data"]["status"] == "applied"
    assert len(sent) == 1
    assert any("replay" in warning for warning in payload["warnings"])


def test_dry_run_does_not_create_a_database(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str, list[str]]]
) -> None:
    _plan(monkeypatch, sent)

    assert not (workspace / "data" / "seo.sqlite3").exists()


def test_config_path_selects_the_ledger_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str, list[str]]]
) -> None:
    """ENG-07: replay protection must follow ``--config``, not the shell's working directory."""
    site = tmp_path / "srv" / "site"
    site.mkdir(parents=True)
    config = site / "seo.toml"
    config.write_text(
        '[project]\nname = "Example"\n[storage]\ndata_dir = "./data"\n', encoding="utf-8"
    )
    (site / "data").mkdir()
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", _SECRET)

    args = (
        "indexnow",
        "submit",
        "--host",
        "example.com",
        "--key",
        "abc123",
        "--url",
        "https://example.com/a",
        "--config",
        str(config),
        "--json",
    )
    dry = _payload(_invoke(*args, "--allow-write"))
    token = dry["data"]["confirm_token"]

    first = _payload(_invoke(*args, "--allow-write", "--confirm", token))
    second = _payload(_invoke(*args, "--allow-write", "--confirm", token))

    assert first["data"]["status"] == "applied"
    assert first["warnings"] == [], "the selected workspace must provide the ledger"
    assert second["data"]["status"] == "skipped"
    assert len(sent) == 1
    assert (site / "data" / "seo.sqlite3").is_file()
    assert not (foreign / "data").exists()
