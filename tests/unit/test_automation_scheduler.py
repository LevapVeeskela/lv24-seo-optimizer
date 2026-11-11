"""Tests for the scheduler generators (WP5.6)."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest

from lv24_seo.automation import (
    ScheduleSpec,
    install,
    render_cron,
    render_systemd,
    render_windows_xml,
)
from lv24_seo.automation.scheduler import _replace_block
from lv24_seo.core.errors import INVALID_INPUT, SeoError

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
WEEKLY = ScheduleSpec(
    name="lv24-seo-weekly",
    cron="0 6 * * 1",
    command="/usr/local/bin/lv24-seo report weekly --json",
)


def _exec_action(xml: str) -> ET.Element:
    root = ET.fromstring(xml)
    assert root.tag == f"{{{NS['t']}}}Task"
    action = root.find("t:Actions/t:Exec", NS)
    assert action is not None
    return action


def _trigger(xml: str) -> ET.Element:
    root = ET.fromstring(xml)
    trigger = root.find("t:Triggers/t:CalendarTrigger", NS)
    assert trigger is not None
    return trigger


def test_render_cron_block() -> None:
    text = render_cron(WEEKLY)

    assert text == (
        "# lv24-seo scheduled job\n"
        "CRON_TZ=Europe/Minsk\n"
        "0 6 * * 1 /usr/local/bin/lv24-seo report weekly --json\n"
    )


def test_render_cron_applies_workdir_and_custom_metadata() -> None:
    spec = ScheduleSpec(
        name="crawl",
        cron="30 4 * * *",
        command="/opt/lv24-seo/bin/lv24-seo crawl audit --json",
        workdir="/srv/lv24",
        timezone="UTC",
        description="nightly crawl audit",
    )

    lines = render_cron(spec).splitlines()

    assert lines[0] == "# nightly crawl audit"
    assert lines[1] == "CRON_TZ=UTC"
    assert lines[2] == ("30 4 * * * cd /srv/lv24 && /opt/lv24-seo/bin/lv24-seo crawl audit --json")


def test_render_cron_keeps_complex_expressions() -> None:
    spec = ScheduleSpec(
        name="poll",
        cron="*/5 * * * *",
        command="/usr/local/bin/lv24-seo report daily --json",
    )

    assert render_cron(spec).splitlines()[-1].startswith("*/5 * * * * /usr/local/bin/lv24-seo")


def test_render_systemd_service_and_timer() -> None:
    units = render_systemd(WEEKLY)

    assert set(units) == {"service", "timer"}
    service = units["service"]
    assert "[Service]" in service
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/local/bin/lv24-seo report weekly --json" in service
    assert "WorkingDirectory=" not in service

    timer = units["timer"]
    assert "OnCalendar=Mon *-*-* 06:00:00" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_render_systemd_includes_workdir() -> None:
    spec = ScheduleSpec(
        name="crawl",
        cron="0 3 * * *",
        command="/opt/lv24-seo/bin/lv24-seo crawl audit --json",
        workdir="/srv/lv24",
    )

    service = render_systemd(spec)["service"]

    assert "WorkingDirectory=/srv/lv24" in service
    assert "OnCalendar=*-*-* 03:00:00" in render_systemd(spec)["timer"]


@pytest.mark.parametrize("cron", ["*/5 * * * *", "15 3 * * 1-5", "0 6 1 * *", "0 6 * * 1,3"])
def test_render_systemd_rejects_complex_cron(cron: str) -> None:
    spec = ScheduleSpec(name="complex", cron=cron, command="/usr/local/bin/lv24-seo report --json")

    with pytest.raises(SeoError) as excinfo:
        render_systemd(spec)

    assert excinfo.value.code == INVALID_INPUT
    assert excinfo.value.next_action == "use a simple cron or a systemd OnCalendar value"


def test_render_windows_xml_weekly() -> None:
    spec = ScheduleSpec(
        name="lv24-seo-weekly",
        cron="0 6 * * 1",
        command='"C:\\Program Files\\lv24-seo\\lv24-seo.exe" report weekly --json',
        workdir="C:\\srv\\lv24",
        description="SEO & reports",
    )

    xml = render_windows_xml(spec)
    action = _exec_action(xml)

    assert (
        action.findtext("t:Command", namespaces=NS) == "C:\\Program Files\\lv24-seo\\lv24-seo.exe"
    )
    assert action.findtext("t:Arguments", namespaces=NS) == "report weekly --json"
    assert action.findtext("t:WorkingDirectory", namespaces=NS) == "C:\\srv\\lv24"

    trigger = _trigger(xml)
    start = trigger.findtext("t:StartBoundary", namespaces=NS)
    assert start is not None
    boundary = datetime.fromisoformat(start)
    assert boundary.weekday() == 0  # Monday
    assert (boundary.hour, boundary.minute, boundary.second) == (6, 0, 0)
    assert trigger.find("t:ScheduleByWeek/t:DaysOfWeek/t:Monday", NS) is not None
    assert trigger.find("t:ScheduleByWeek/t:WeeksInterval", NS) is not None

    root = ET.fromstring(xml)
    assert root.findtext("t:RegistrationInfo/t:Description", namespaces=NS) == "SEO & reports"


def test_render_windows_xml_daily() -> None:
    spec = ScheduleSpec(
        name="lv24-seo-daily",
        cron="30 7 * * *",
        command="lv24-seo report daily --json",
    )

    xml = render_windows_xml(spec)
    action = _exec_action(xml)
    trigger = _trigger(xml)

    assert action.findtext("t:Command", namespaces=NS) == "lv24-seo"
    assert action.findtext("t:Arguments", namespaces=NS) == "report daily --json"
    assert trigger.findtext("t:ScheduleByDay/t:DaysInterval", namespaces=NS) == "1"
    assert trigger.find("t:ScheduleByWeek", NS) is None


@pytest.mark.parametrize("cron", ["*/15 * * * *", "0 6 * * 1-5", "0 6 15 * *"])
def test_render_windows_xml_rejects_complex_cron(cron: str) -> None:
    spec = ScheduleSpec(name="complex", cron=cron, command="lv24-seo report daily --json")

    with pytest.raises(SeoError) as excinfo:
        render_windows_xml(spec)

    assert excinfo.value.code == INVALID_INPUT


@pytest.mark.parametrize(
    ("target", "expected_files", "command_marker"),
    [
        ("cron", {"lv24-seo-weekly.cron"}, "crontab -"),
        (
            "systemd",
            {"lv24-seo-weekly.service", "lv24-seo-weekly.timer"},
            "systemctl --user",
        ),
        ("windows", {"lv24-seo-weekly.xml"}, "schtasks /Create /XML"),
    ],
)
def test_install_dry_run_returns_files_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    expected_files: set[str],
    command_marker: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    result = install(WEEKLY, target=target)

    assert result["target"] == target
    assert result["dry_run"] is True
    assert set(result["files"]) == expected_files
    assert all(isinstance(content, str) and content for content in result["files"].values())
    assert command_marker in result["command"]
    assert list(tmp_path.rglob("*")) == []


def test_install_dry_run_matches_renderers() -> None:
    result = install(WEEKLY, target="cron")

    assert result["files"]["lv24-seo-weekly.cron"] == render_cron(WEEKLY)


def test_install_rejects_unsupported_target() -> None:
    with pytest.raises(SeoError) as excinfo:
        install(WEEKLY, target="launchd")

    assert excinfo.value.code == INVALID_INPUT
    assert excinfo.value.next_action is not None


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("win32", "windows"), ("linux", "systemd"), ("darwin", "cron")],
)
def test_install_resolves_auto_target_from_platform(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    result = install(WEEKLY)

    assert result["target"] == expected
    assert result["dry_run"] is True


def test_install_rejects_unknown_platform_for_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "aix")

    with pytest.raises(SeoError) as excinfo:
        install(WEEKLY)

    assert excinfo.value.code == INVALID_INPUT


@pytest.mark.parametrize(
    ("name", "cron", "command"),
    [
        ("bad/name", "0 6 * * 1", "lv24-seo report weekly --json"),
        ("job", "0 6 * *", "lv24-seo report weekly --json"),
        ("job", "0 6 * * 1", ""),
        ("job", "0 6 * * 1", "lv24-seo report\nweekly --json"),
    ],
)
def test_spec_validation(name: str, cron: str, command: str) -> None:
    with pytest.raises(SeoError) as excinfo:
        ScheduleSpec(name=name, cron=cron, command=command)

    assert excinfo.value.code == INVALID_INPUT


def test_managed_cron_block_is_replaced_idempotently() -> None:
    block = render_cron(WEEKLY)
    current = "# other job\n0 1 * * * /usr/bin/true\n"
    wrapped = f"# >>> lv24-seo:lv24-seo-weekly >>>\n{block}# <<< lv24-seo:lv24-seo-weekly <<<\n"

    first = _replace_block(current, wrapped, "lv24-seo-weekly")
    second = _replace_block(first, wrapped, "lv24-seo-weekly")

    assert first == second
    assert first.count("# >>> lv24-seo:lv24-seo-weekly >>>") == 1
    assert "0 1 * * * /usr/bin/true" in first
    assert first.endswith("# <<< lv24-seo:lv24-seo-weekly <<<\n")
