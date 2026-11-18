"""Schedule lifecycle: gated install, read-only status and exact uninstall.

Every test runs against a temp ``XDG_CONFIG_HOME`` and a fake ``crontab``/``schtasks``;
no real host scheduler is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.automation import (
    ScheduleSpec,
    install,
    render_systemd,
    render_windows_launcher,
    render_windows_xml,
    scheduler,
    status,
    uninstall,
)
from lv24_seo.cli import main as cli_main

RUNNER = CliRunner()
WEEKLY = ScheduleSpec(
    name="lv24-seo-weekly",
    cron="0 6 * * 1",
    command="/usr/local/bin/lv24-seo report weekly --json",
)
DAILY = ScheduleSpec(
    name="lv24-seo-daily",
    cron="0 7 * * *",
    command="/usr/local/bin/lv24-seo report daily --json",
)


class FakeCrontab:
    """In-memory crontab; records writes instead of calling the host binary."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.writes: list[str] = []

    def read(self, crontab: str) -> str:
        del crontab
        return self.text

    def run(self, args: list[str], **kwargs: Any) -> None:
        del args
        input_text = kwargs.get("input_text")
        if input_text is not None:
            self.text = input_text
            self.writes.append(input_text)


def _workspace(tmp_path: Path) -> Path:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Example"\n'
        'base_url = "https://example.com"\n'
        'default_property = "sc-domain:example.com"\n',
        encoding="utf-8",
    )
    return path


def _use_fake_crontab(monkeypatch: pytest.MonkeyPatch, fake: FakeCrontab) -> None:
    monkeypatch.setattr(scheduler, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(scheduler, "_read_crontab", fake.read)
    monkeypatch.setattr(scheduler, "_run", fake.run)


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _block(spec: ScheduleSpec) -> str:
    return scheduler._cron_block(spec)


def test_install_dry_run_reports_not_installed() -> None:
    result = install(WEEKLY, target="cron")

    assert result["dry_run"] is True
    assert result["installed"] is False
    assert result["paths"] == {}


def test_cli_install_without_allow_write_only_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _workspace(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    applied: list[Any] = []
    monkeypatch.setattr(scheduler, "_apply", lambda *args, **kwargs: applied.append(args) or {})

    payload = _json(
        _invoke(
            "schedule",
            "install",
            "--kind",
            "weekly",
            "--target",
            "cron",
            "--config",
            str(config),
            "--json",
        )
    )

    assert payload["data"]["dry_run"] is True
    assert payload["data"]["installed"] is False
    assert payload["meta"]["allow_write"] is False
    assert applied == []
    assert not (tmp_path / "config").exists()


def test_cli_install_with_allow_write_writes_the_cron_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _workspace(tmp_path)
    fake = FakeCrontab("# user job\n0 1 * * * /usr/bin/true\n")
    _use_fake_crontab(monkeypatch, fake)

    payload = _json(
        _invoke(
            "schedule",
            "install",
            "--kind",
            "weekly",
            "--target",
            "cron",
            "--allow-write",
            "--config",
            str(config),
            "--json",
        )
    )

    assert payload["data"]["dry_run"] is False
    assert payload["data"]["installed"] is True
    assert payload["meta"]["allow_write"] is True
    assert "# >>> lv24-seo:lv24-seo-weekly >>>" in fake.text
    assert "0 1 * * * /usr/bin/true" in fake.text
    assert len(fake.writes) == 1


def test_install_systemd_writes_user_units_into_temp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(scheduler, "_which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []
    monkeypatch.setattr(scheduler, "_run", lambda args, **kwargs: calls.append(args))

    result = install(WEEKLY, target="systemd", dry_run=False)

    directory = tmp_path / "config" / "systemd" / "user"
    units = render_systemd(WEEKLY)
    assert result["installed"] is True
    assert (directory / f"{WEEKLY.name}.service").read_text(encoding="utf-8") == units["service"]
    assert (directory / f"{WEEKLY.name}.timer").read_text(encoding="utf-8") == units["timer"]
    assert result["paths"][f"{WEEKLY.name}.timer"] == str(directory / f"{WEEKLY.name}.timer")
    assert any("daemon-reload" in call for call in calls)
    assert any("enable" in call for call in calls)


def test_install_windows_writes_xml_and_registers_via_fake_schtasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(scheduler, "_which", lambda name: f"C:\\Windows\\{name}.exe")
    calls: list[list[str]] = []
    monkeypatch.setattr(scheduler, "_run", lambda args, **kwargs: calls.append(args))

    result = install(WEEKLY, target="windows", dry_run=False)

    xml_path = tmp_path / "config" / "lv24-seo" / "schedule" / f"{WEEKLY.name}.xml"
    vbs_path = tmp_path / "config" / "lv24-seo" / "schedule" / f"{WEEKLY.name}.vbs"
    assert result["installed"] is True
    assert xml_path.read_text(encoding="utf-16") == render_windows_xml(WEEKLY)
    assert vbs_path.read_text(encoding="utf-16") == render_windows_launcher(WEEKLY)
    assert calls and calls[0][1:4] == ["/Create", "/XML", str(xml_path)]
    assert result["paths"][f"{WEEKLY.name}.xml"] == str(xml_path)
    assert result["paths"][f"{WEEKLY.name}.vbs"] == str(vbs_path)


def test_status_detects_installed_cron_and_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCrontab("# user job\n0 1 * * * /usr/bin/true\n" + _block(WEEKLY))
    _use_fake_crontab(monkeypatch, fake)

    result = status(WEEKLY, target="cron")

    assert result["state"] == "installed"
    assert result["present"] is True
    assert result["matches"] is True
    assert result["paths"] == {"cron_block": "user crontab"}
    assert result["warnings"] == []
    assert fake.writes == []


def test_status_reports_an_outdated_cron_block(monkeypatch: pytest.MonkeyPatch) -> None:
    installed = ScheduleSpec(
        name=WEEKLY.name,
        cron="0 8 * * 1",
        command=WEEKLY.command,
    )
    fake = FakeCrontab(_block(installed))
    _use_fake_crontab(monkeypatch, fake)

    result = status(WEEKLY, target="cron")

    assert result["state"] == "outdated"
    assert result["present"] is True
    assert result["matches"] is False
    assert fake.writes == []


def test_status_reports_missing_and_unknown_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCrontab("# user job\n0 1 * * * /usr/bin/true\n")
    _use_fake_crontab(monkeypatch, fake)

    missing = status(WEEKLY, target="cron")

    assert missing["state"] == "missing"
    assert missing["present"] is False
    assert missing["matches"] is False

    monkeypatch.setattr(scheduler, "_which", lambda name: None)
    unknown = status(WEEKLY, target="cron")

    assert unknown["state"] == "unknown"
    assert unknown["matches"] is None
    assert any("crontab" in warning for warning in unknown["warnings"])


def test_status_reports_a_partial_systemd_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    directory = tmp_path / "config" / "systemd" / "user"
    directory.mkdir(parents=True)
    (directory / f"{WEEKLY.name}.timer").write_text(
        render_systemd(WEEKLY)["timer"], encoding="utf-8"
    )

    result = status(WEEKLY, target="systemd")

    assert result["state"] == "partial"
    assert result["present"] is True
    assert result["matches"] is False
    assert [artifact["kind"] for artifact in result["artifacts"]] == ["service", "timer"]
    assert result["artifacts"][0]["exists"] is False
    assert result["artifacts"][1]["exists"] is True
    assert any("service unit is missing" in warning for warning in result["warnings"])
    assert (directory / f"{WEEKLY.name}.timer").exists()


def test_uninstall_removes_one_cron_block_and_keeps_unrelated_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = (
        "# user job\n"
        "0 1 * * * /usr/bin/true\n"
        f"{_block(WEEKLY)}"
        f"{_block(DAILY)}"
        "15 3 * * * /usr/local/bin/rotate.sh\n"
    )
    fake = FakeCrontab(current)
    _use_fake_crontab(monkeypatch, fake)

    result = uninstall(WEEKLY, target="cron", dry_run=False)

    assert result["state"] == "removed"
    assert result["removed"] is True
    assert result["removed_items"] == [f"managed crontab block for {WEEKLY.name}"]
    assert result["removed_paths"] == []
    assert "# user job" in fake.text
    assert "0 1 * * * /usr/bin/true" in fake.text
    assert "15 3 * * * /usr/local/bin/rotate.sh" in fake.text
    assert "# >>> lv24-seo:lv24-seo-daily >>>" in fake.text
    assert "# >>> lv24-seo:lv24-seo-weekly >>>" not in fake.text
    assert "0 6 * * 1" not in fake.text

    before = fake.text
    again = uninstall(WEEKLY, target="cron", dry_run=False)

    assert again["state"] == "missing"
    assert again["removed"] is False
    assert fake.text == before
    assert len(fake.writes) == 1


def test_cli_uninstall_without_allow_write_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _workspace(tmp_path)
    fake = FakeCrontab(f"{_block(WEEKLY)}")
    _use_fake_crontab(monkeypatch, fake)

    payload = _json(
        _invoke(
            "schedule",
            "uninstall",
            "--kind",
            "weekly",
            "--target",
            "cron",
            "--config",
            str(config),
            "--json",
        )
    )

    assert payload["data"]["dry_run"] is True
    assert payload["data"]["removed"] is False
    assert payload["data"]["state"] == "would_remove"
    assert payload["data"]["plan"]
    assert fake.writes == []
    assert any("--allow-write" in warning for warning in payload["warnings"])


def test_uninstall_dry_run_without_crontab_reports_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler, "_which", lambda name: None)

    result = uninstall(WEEKLY, target="cron")

    assert result["state"] == "unknown"
    assert result["removed"] is False
    assert any("crontab" in warning for warning in result["warnings"])


def test_uninstall_systemd_units_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    directory = tmp_path / "config" / "systemd" / "user"
    directory.mkdir(parents=True)
    units = render_systemd(WEEKLY)
    for suffix in ("service", "timer"):
        (directory / f"{WEEKLY.name}.{suffix}").write_text(units[suffix], encoding="utf-8")
    monkeypatch.setattr(
        scheduler, "_which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    optional_calls: list[list[str]] = []
    monkeypatch.setattr(scheduler, "_run_optional", lambda args: optional_calls.append(args))

    first = uninstall(WEEKLY, target="systemd", dry_run=False)

    assert first["removed"] is True
    assert first["removed_items"] == first["removed_paths"]
    assert len(first["removed_paths"]) == 2
    assert not (directory / f"{WEEKLY.name}.service").exists()
    assert not (directory / f"{WEEKLY.name}.timer").exists()
    assert optional_calls[0][-1] == f"{WEEKLY.name}.timer"

    calls_after_first = len(optional_calls)
    second = uninstall(WEEKLY, target="systemd", dry_run=False)

    assert second["state"] == "missing"
    assert second["removed"] is False
    assert len(optional_calls) == calls_after_first


def test_windows_status_and_uninstall_never_touch_the_real_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    xml_path = scheduler._windows_xml_path(WEEKLY)
    vbs_path = scheduler._windows_launcher_path(WEEKLY)
    xml_path.parent.mkdir(parents=True)
    xml_path.write_text(render_windows_xml(WEEKLY), encoding="utf-16")
    vbs_path.write_text(render_windows_launcher(WEEKLY), encoding="utf-16")
    state: dict[str, str | None] = {"task": render_windows_xml(WEEKLY)}
    monkeypatch.setattr(
        scheduler, "_which", lambda name: "schtasks.exe" if name == "schtasks" else None
    )
    monkeypatch.setattr(scheduler, "_query_windows_task", lambda schtasks, name: state["task"])
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> None:
        del kwargs
        calls.append(args)
        state["task"] = None

    monkeypatch.setattr(scheduler, "_run", fake_run)

    found = status(WEEKLY, target="windows")

    assert found["state"] == "installed"
    assert found["artifacts"][0]["kind"] == "task"
    assert found["artifacts"][0]["exists"] is True
    assert found["artifacts"][0]["matches"] is True
    assert [artifact["kind"] for artifact in found["artifacts"]] == ["task", "xml", "vbs"]
    assert calls == []

    removed = uninstall(WEEKLY, target="windows", dry_run=False)

    assert removed["removed"] is True
    assert removed["removed_items"][0] == f"Task Scheduler\\{WEEKLY.name}"
    assert removed["removed_paths"] == [str(xml_path), str(vbs_path)]
    assert calls == [["schtasks.exe", "/Delete", "/TN", WEEKLY.name, "/F"]]
    assert not xml_path.exists()
    assert not vbs_path.exists()

    again = uninstall(WEEKLY, target="windows", dry_run=False)

    assert again["state"] == "missing"
    assert again["removed"] is False
    assert len(calls) == 1


def test_windows_status_reports_an_outdated_registered_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        scheduler, "_which", lambda name: "schtasks.exe" if name == "schtasks" else None
    )
    other = ScheduleSpec(name=WEEKLY.name, cron=WEEKLY.cron, command="C:\\lv24\\other.exe report")
    monkeypatch.setattr(
        scheduler, "_query_windows_task", lambda schtasks, name: render_windows_xml(other)
    )

    result = status(WEEKLY, target="windows")

    assert result["state"] == "outdated"
    assert result["artifacts"][1]["exists"] is False
    assert result["artifacts"][2]["exists"] is False


def test_windows_status_detects_a_changed_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        scheduler, "_which", lambda name: "schtasks.exe" if name == "schtasks" else None
    )
    rescheduled = ScheduleSpec(name=WEEKLY.name, cron="0 8 * * 1", command=WEEKLY.command)
    monkeypatch.setattr(
        scheduler, "_query_windows_task", lambda schtasks, name: render_windows_xml(rescheduled)
    )

    result = status(WEEKLY, target="windows")

    assert result["state"] == "outdated"
    assert result["artifacts"][0]["matches"] is False


def test_schtasks_output_is_decoded_from_utf16() -> None:
    xml = render_windows_xml(WEEKLY)

    assert scheduler._decode_scheduler_output(xml.encode("utf-16")) == xml
    assert scheduler._decode_scheduler_output(xml.encode("utf-8")) == xml
    assert scheduler._decode_scheduler_output(b"") == ""


@pytest.mark.skipif(sys.platform != "win32", reason="the OEM code page is Windows-specific")
def test_schtasks_russian_not_found_is_decoded_from_oem(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "ОШИБКА: Не удается найти указанный файл.".encode("cp866")  # noqa: RUF001
    monkeypatch.setattr(scheduler, "_windows_oem_encoding", lambda: "cp866")

    assert "не удается найти" in scheduler._decode_scheduler_output(message).lower()
