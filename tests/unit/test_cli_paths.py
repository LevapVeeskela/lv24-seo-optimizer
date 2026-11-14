"""CLI regressions for config-relative paths.

Two user-visible bugs are pinned here:

* ``--config /abs/seo.toml`` from another directory used to resolve ``storage.data_dir``
  against the shell's working directory, so ``init`` created ``/abs/data`` while every later
  command looked at ``./data`` and silently ran store-less;
* ``schedule install/print --config seo.toml`` embedded a *relative* config path and a
  relative working directory in the generated cron/systemd/Task Scheduler artefact, so the
  job ran in the scheduler's own directory and fell back to defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main

RUNNER = CliRunner()

_FOREIGN_CWD = "elsewhere"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic config: no ambient ``LV24_SEO_*`` overrides."""
    import os

    for name in list(os.environ):
        if name.startswith("LV24_SEO_"):
            monkeypatch.delenv(name, raising=False)


def _site(tmp_path: Path, body: str = "") -> Path:
    site = tmp_path / "srv" / "site"
    site.mkdir(parents=True)
    path = site / "seo.toml"
    path.write_text(
        '[project]\nname = "Example"\nbase_url = "https://example.com"\n'
        '[storage]\ndata_dir = "./data"\n' + body,
        encoding="utf-8",
    )
    return path


def _foreign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / _FOREIGN_CWD
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _check(payload: dict[str, Any], check_id: str) -> dict[str, Any]:
    checks = payload["data"]["checks"]
    return next(check for check in checks if check["id"] == check_id)


def test_init_then_doctor_from_a_foreign_cwd_uses_the_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _foreign(tmp_path, monkeypatch)
    config_path = foreign / "site" / "seo.toml"

    created = _json(
        _invoke(
            "init",
            "--config",
            str(config_path),
            "--property",
            "https://example.com",
            "--json",
        )
    )
    data_dir = Path(created["data"]["data_dir"])
    assert data_dir == config_path.parent / "data"
    assert data_dir.is_dir()

    payload = _json(_invoke("doctor", "--config", str(config_path), "--json"))
    data_check = _check(payload, "data_dir")

    assert data_check["ok"] is True
    assert Path(data_check["detail"]) == data_dir
    assert payload["meta"]["data_dir"] == str(data_dir)
    assert payload["meta"]["data_dir_source"] == "config"
    assert payload["warnings"] == []
    assert not (foreign / "data").exists()


def test_doctor_flags_a_data_dir_that_still_follows_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compatibility: the built-in default stays CWD-relative but is reported honestly."""
    site = tmp_path / "srv" / "site"
    site.mkdir(parents=True)
    config_path = site / "seo.toml"
    config_path.write_text('[project]\nname = "Example"\n', encoding="utf-8")
    foreign = _foreign(tmp_path, monkeypatch)
    (foreign / "data").mkdir()

    payload = _json(_invoke("doctor", "--config", str(config_path), "--json"))

    assert payload["data"]["healthy"] is True
    assert payload["meta"]["data_dir_source"] == "working directory"
    assert payload["warnings"], "an ambiguous data dir must be reported"
    assert "relative" in payload["warnings"][0]
    assert str(site) in payload["warnings"][0]


def test_doctor_is_quiet_when_the_config_pins_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _site(tmp_path)
    (config_path.parent / "data").mkdir()
    _foreign(tmp_path, monkeypatch)

    payload = _json(_invoke("doctor", "--config", str(config_path), "--json"))

    assert payload["warnings"] == []
    assert payload["meta"]["data_dir_source"] == "config"


def test_config_show_reports_the_anchored_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _site(tmp_path)
    _foreign(tmp_path, monkeypatch)

    payload = _json(_invoke("config", "show", "--config", str(config_path), "--json"))

    assert payload["data"]["path"] == str(config_path)
    assert Path(payload["data"]["data_dir"]) == config_path.parent / "data"
    assert Path(payload["data"]["config"]["storage"]["data_dir"]) == config_path.parent / "data"


def test_schedule_print_embeds_absolute_config_and_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _site(tmp_path)
    site = config_path.parent
    monkeypatch.chdir(site)

    payload = _json(
        _invoke(
            "schedule",
            "print",
            "--cron",
            "0 6 * * 1",
            "--target",
            "cron",
            "--config",
            "seo.toml",
            "--json",
        )
    )

    cron = payload["data"]["files"]["lv24-seo-weekly.cron"]
    assert f"cd {site}" in cron or f"cd '{site}'" in cron
    assert f"--config {config_path}" in cron or f"--config '{config_path}'" in cron
    assert "seo.toml --send" not in cron


def test_schedule_print_systemd_workdir_is_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _site(tmp_path)
    site = config_path.parent
    monkeypatch.chdir(site)

    payload = _json(
        _invoke(
            "schedule",
            "print",
            "--cron",
            "0 6 * * 1",
            "--target",
            "systemd",
            "--config",
            "seo.toml",
            "--json",
        )
    )

    service = payload["data"]["files"]["lv24-seo-weekly.service"]
    workdir_line = next(
        line for line in service.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert Path(workdir_line.split("=", 1)[1]).is_absolute()


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))
