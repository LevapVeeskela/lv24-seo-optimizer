"""F22/F23 scheduler regressions: --config embedding and honest timezones.

``schedule install/print`` must embed the absolute config path (custom names
and spaces included); cron carries CRON_TZ as a real assignment; the Windows
boundary carries a UTC offset when the zone resolves; every backend reports
its timezone limits instead of drifting silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lv24_seo.automation import (
    ScheduleSpec,
    install,
    render_cron,
    render_systemd,
    render_windows_xml,
)
from lv24_seo.automation.scheduler import timezone_available
from lv24_seo.core.errors import SeoError

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _boundary(spec: ScheduleSpec) -> str:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(render_windows_xml(spec))
    trigger = root.find("t:Triggers/t:CalendarTrigger", NS)
    assert trigger is not None
    text = trigger.findtext("t:StartBoundary", namespaces=NS)
    assert text is not None
    return text


def test_cron_block_carries_env_assignment_not_comment() -> None:
    lines = render_cron(
        ScheduleSpec(
            name="lv24-seo-weekly",
            cron="0 6 * * 1",
            command="/usr/local/bin/lv24-seo report weekly --json",
            timezone="Europe/Minsk",
        )
    ).splitlines()

    assert lines[1] == "CRON_TZ=Europe/Minsk"
    assert not lines[1].startswith("#")


def test_windows_boundary_carries_utc_offset_when_zone_resolves() -> None:
    spec = ScheduleSpec(
        name="lv24-seo-weekly",
        cron="0 6 * * 1",
        command="/usr/local/bin/lv24-seo report weekly --json",
        timezone="UTC",
    )
    assert _boundary(spec).endswith("+00:00")


def test_timezone_availability_is_explicit() -> None:
    assert timezone_available("UTC") is True
    assert timezone_available("No/Such_Zone") is False


def test_install_reports_backend_timezone_limits() -> None:
    weekly = ScheduleSpec(
        name="lv24-seo-weekly",
        cron="0 6 * * 1",
        command="/usr/local/bin/lv24-seo report weekly --json",
    )
    assert any("CRON_TZ" in warning for warning in install(weekly, target="cron")["warnings"])
    assert install(weekly, target="systemd")["warnings"] == []
    assert "OnCalendar=Mon *-*-* 06:00:00 Europe/Minsk\n" in render_systemd(weekly)["timer"]


def test_unresolvable_zone_is_rejected_before_installation() -> None:
    with pytest.raises(SeoError, match="unknown or unavailable timezone"):
        ScheduleSpec(
            name="lv24-seo-weekly",
            cron="0 6 * * 1",
            command="/usr/local/bin/lv24-seo report weekly --json",
            timezone="No/Such_Zone",
        )


@pytest.mark.parametrize("field", ["description", "workdir", "timezone"])
def test_schedule_metadata_cannot_inject_another_directive(field: str) -> None:
    with pytest.raises(SeoError):
        ScheduleSpec(name="weekly", cron="0 6 * * 1", command="report", **{field: "x\n* * * * * x"})


def test_windows_dst_zone_explains_fixed_offset_limit() -> None:
    spec = ScheduleSpec(name="weekly", cron="0 6 * * 1", command="report", timezone="Europe/Berlin")
    assert any(
        "daylight-saving" in warning for warning in install(spec, target="windows")["warnings"]
    )


def test_report_command_embeds_absolute_config(tmp_path: Path) -> None:
    from lv24_seo.cli.main import _report_command

    config = tmp_path / "my dir" / "client.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[project]\n", encoding="utf-8")

    command = _report_command("weekly", config)
    assert "--config" in command
    assert str(config) in command

    daily = _report_command("daily", config)
    assert "--days 1" in daily
    assert str(config) in daily


def test_windows_config_path_uses_windows_quoting_on_any_host(tmp_path: Path) -> None:
    from lv24_seo.cli.main import _report_command

    config = tmp_path / "my dir" / "client.toml"
    config.parent.mkdir()
    config.write_text("[project]\n", encoding="utf-8")
    command = _report_command("weekly", config, target="windows")
    assert f'--config "{config}"' in command
    assert f"'{config}'" not in command
