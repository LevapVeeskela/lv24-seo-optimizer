"""Scheduler generators: cron, systemd user units and Windows Task Scheduler XML.

There is no daemon. Every renderer is pure, and :func:`install` defaults to a dry run, so
``lv24-seo schedule install --print`` can show the artefact before anything is written.

Only the simple monthly-free shapes are convertible across schedulers::

    0 6 * * *    daily at 06:00
    0 6 * * 1    weekly on Monday at 06:00

Cron accepts any 5-field expression; systemd and Task Scheduler reject everything that is
not one of the shapes above instead of silently changing the intended cadence.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lv24_seo.core.errors import INTERNAL, INVALID_INPUT, SeoError

_TARGETS = frozenset({"cron", "systemd", "windows"})
_PLATFORM_TARGETS = {"win32": "windows", "linux": "systemd", "darwin": "cron"}

# cron day-of-week 0/7 is Sunday; Python's weekday() starts on Monday.
_SYSTEMD_DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_TASK_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

_SYSTEMD_HINT = "use a simple cron or a systemd OnCalendar value"
_WINDOWS_HINT = "use a simple daily/weekly cron like '0 6 * * 1'"

_BLOCK_BEGIN = "# >>> lv24-seo:{name} >>>"
_BLOCK_END = "# <<< lv24-seo:{name} <<<"


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """One scheduled job, independent of the host scheduler."""

    name: str
    cron: str
    command: str
    workdir: str | None = None
    timezone: str = "Europe/Minsk"
    description: str = "lv24-seo scheduled job"

    def __post_init__(self) -> None:
        if not self.name or not all(char.isalnum() or char in "-_" for char in self.name):
            raise SeoError(
                INVALID_INPUT,
                f"invalid job name {self.name!r}",
                next_action="use letters, digits, '-' or '_' for the job name",
            )
        if not self.command.strip():
            raise SeoError(
                INVALID_INPUT,
                "command must not be empty",
                next_action=(
                    "pass an absolute command line such as "
                    "'/usr/local/bin/lv24-seo report weekly --json'"
                ),
            )
        if "\n" in self.command or "\r" in self.command:
            raise SeoError(
                INVALID_INPUT,
                "command must be a single line",
                next_action="render a wrapper script and schedule that instead",
            )
        if not self.timezone.strip():
            raise SeoError(
                INVALID_INPUT,
                "timezone must not be empty",
                next_action="pass an IANA timezone such as 'Europe/Minsk'",
            )
        _cron_fields(self.cron)


def render_cron(spec: ScheduleSpec) -> str:
    """Render a newline-terminated crontab block.

    The block carries the description and ``CRON_TZ`` as comments, followed by the schedule
    line. ``workdir`` is applied with ``cd`` because crontab has no working-directory field.
    """

    command = spec.command.strip()
    if spec.workdir:
        command = f"cd {shlex.quote(spec.workdir)} && {command}"
    lines = [
        f"# {spec.description}",
        f"# CRON_TZ={spec.timezone}",
        f"{spec.cron.strip()} {command}",
    ]
    return "\n".join(lines) + "\n"


def render_systemd(spec: ScheduleSpec) -> dict[str, str]:
    """Render ``{"service": ..., "timer": ...}`` for systemd *user* units."""

    minute, hour, weekday = _simple_schedule(spec.cron, next_action=_SYSTEMD_HINT)
    service_lines = [
        "[Unit]",
        f"Description={spec.description}",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart={spec.command.strip()}",
    ]
    if spec.workdir:
        service_lines.append(f"WorkingDirectory={spec.workdir}")
    timer_lines = [
        "[Unit]",
        f"Description={spec.description} (timer)",
        "",
        "[Timer]",
        f"OnCalendar={_on_calendar(minute, hour, weekday)}",
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
    ]
    return {
        "service": "\n".join(service_lines) + "\n",
        "timer": "\n".join(timer_lines) + "\n",
    }


def render_windows_xml(spec: ScheduleSpec) -> str:
    """Render Task Scheduler XML (``schtasks /Create /XML``) for daily/weekly jobs.

    The declaration is UTF-16 because Task Scheduler expects a Unicode file; write the
    returned text with ``encoding="utf-16"``. ``StartBoundary`` is the next occurrence of
    the requested weekday and time in ``spec.timezone`` (falls back to host local time when
    the platform has no tz database).
    """

    minute, hour, weekday = _simple_schedule(spec.cron, next_action=_WINDOWS_HINT)
    start = _next_start(hour, minute, weekday, spec.timezone)
    executable, arguments = _split_command(spec.command)
    if weekday is None:
        schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    else:
        day = _TASK_DAYS[weekday % 7]
        schedule = (
            "<ScheduleByWeek><DaysOfWeek>"
            f"<{day}/>"
            "</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
        )

    lines = [
        '<?xml version="1.0" encoding="UTF-16"?>',
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
        "  <RegistrationInfo>",
        f"    <URI>\\{escape(spec.name)}</URI>",
        f"    <Description>{escape(spec.description)}</Description>",
        "  </RegistrationInfo>",
        "  <Triggers>",
        "    <CalendarTrigger>",
        f"      <StartBoundary>{start}</StartBoundary>",
        "      <Enabled>true</Enabled>",
        f"      {schedule}",
        "    </CalendarTrigger>",
        "  </Triggers>",
        "  <Principals>",
        '    <Principal id="Author">',
        "      <LogonType>InteractiveToken</LogonType>",
        "      <RunLevel>LeastPrivilege</RunLevel>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <StartWhenAvailable>true</StartWhenAvailable>",
        "    <Enabled>true</Enabled>",
        "  </Settings>",
        '  <Actions Context="Author">',
        "    <Exec>",
        f"      <Command>{escape(executable)}</Command>",
    ]
    if arguments:
        lines.append(f"      <Arguments>{escape(arguments)}</Arguments>")
    if spec.workdir:
        lines.append(f"      <WorkingDirectory>{escape(spec.workdir)}</WorkingDirectory>")
    lines.extend(
        [
            "    </Exec>",
            "  </Actions>",
            "</Task>",
            "",
        ]
    )
    return "\n".join(lines)


def install(spec: ScheduleSpec, *, target: str = "auto", dry_run: bool = True) -> dict[str, Any]:
    """Render or install the schedule for one target.

    ``dry_run=True`` writes nothing and returns the artefacts; ``target="auto"`` resolves
    from :data:`sys.platform` (Windows, Linux and macOS are supported).
    """

    resolved = _resolve_target(target)
    files = _render_files(spec, resolved)
    command = _install_command(spec, resolved)
    result: dict[str, Any] = {
        "target": resolved,
        "dry_run": dry_run,
        "files": files,
        "command": command,
    }
    if dry_run:
        return result
    result["paths"] = _apply(spec, resolved, files)
    result["installed"] = True
    return result


def _cron_fields(cron: str) -> list[str]:
    fields = cron.split()
    if len(fields) != 5:
        raise SeoError(
            INVALID_INPUT,
            f"cron must have 5 fields, got {len(fields)}",
            next_action="pass a 5-field cron such as '0 6 * * 1'",
        )
    return fields


def _simple_schedule(cron: str, *, next_action: str) -> tuple[int, int, int | None]:
    """Return ``(minute, hour, cron_weekday | None)`` for daily/weekly shapes only."""

    minute_raw, hour_raw, day_of_month, month, weekday_raw = _cron_fields(cron)
    if not minute_raw.isdigit() or not hour_raw.isdigit():
        raise _simple_error(cron, next_action)
    minute, hour = int(minute_raw), int(hour_raw)
    if not 0 <= minute <= 59 or not 0 <= hour <= 23:
        raise _simple_error(cron, next_action)
    if day_of_month != "*" or month != "*":
        raise _simple_error(cron, next_action)
    weekday = None
    if weekday_raw != "*":
        weekday = _parse_weekday(weekday_raw)
        if weekday is None:
            raise _simple_error(cron, next_action)
    return minute, hour, weekday


def _simple_error(cron: str, next_action: str) -> SeoError:
    return SeoError(
        INVALID_INPUT,
        f"cron {cron!r} cannot be expressed as a simple daily/weekly calendar schedule",
        next_action=next_action,
    )


def _parse_weekday(token: str) -> int | None:
    lowered = token.lower()
    if lowered in _DOW_NAMES:
        return _DOW_NAMES[lowered]
    if token.isdigit() and 0 <= int(token) <= 7:
        return int(token) % 7
    return None


def _on_calendar(minute: int, hour: int, weekday: int | None) -> str:
    clock = f"{hour:02d}:{minute:02d}:00"
    if weekday is None:
        return f"*-*-* {clock}"
    return f"{_SYSTEMD_DAYS[weekday % 7]} *-*-* {clock}"


def _next_start(hour: int, minute: int, weekday: int | None, timezone: str) -> str:
    now = _now(timezone)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        if candidate <= now:
            candidate += timedelta(days=1)
    else:
        py_weekday = (weekday - 1) % 7  # cron: 1 = Monday, Python: 0 = Monday
        candidate += timedelta(days=(py_weekday - candidate.weekday()) % 7)
        if candidate <= now:
            candidate += timedelta(days=7)
    return candidate.strftime("%Y-%m-%dT%H:%M:%S")


def _now(timezone: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        # Windows and slim containers often ship without tzdata; the date is all we need.
        return datetime.now()


def _split_command(command: str) -> tuple[str, str]:
    try:
        parts = shlex.split(command.strip(), posix=False)
    except ValueError as exc:
        raise SeoError(
            INVALID_INPUT,
            f"cannot split the command into executable and arguments: {exc}",
            next_action="fix the quoting in the command line",
        ) from exc
    if not parts:
        raise SeoError(
            INVALID_INPUT,
            "command must not be empty",
            next_action="pass an absolute command line",
        )
    executable = parts[0].strip('"').strip("'")
    return executable, " ".join(parts[1:])


def _resolve_target(target: str) -> str:
    if target == "auto":
        resolved = _PLATFORM_TARGETS.get(sys.platform)
        if resolved is None:
            raise SeoError(
                INVALID_INPUT,
                f"cannot auto-detect a scheduler for platform {sys.platform!r}",
                next_action="pass an explicit target: cron, systemd or windows",
            )
        return resolved
    if target in _TARGETS:
        return target
    raise SeoError(
        INVALID_INPUT,
        f"unsupported scheduler target {target!r}",
        next_action="pass target 'cron', 'systemd', 'windows' or 'auto'",
    )


def _render_files(spec: ScheduleSpec, target: str) -> dict[str, str]:
    if target == "cron":
        return {f"{spec.name}.cron": render_cron(spec)}
    if target == "systemd":
        units = render_systemd(spec)
        return {
            f"{spec.name}.service": units["service"],
            f"{spec.name}.timer": units["timer"],
        }
    return {f"{spec.name}.xml": render_windows_xml(spec)}


def _install_command(spec: ScheduleSpec, target: str) -> str:
    if target == "cron":
        return "crontab -"
    if target == "systemd":
        return f"systemctl --user daemon-reload && systemctl --user enable --now {spec.name}.timer"
    return f"schtasks /Create /XML {_windows_xml_path(spec)} /TN {spec.name} /F"


def _apply(spec: ScheduleSpec, target: str, files: dict[str, str]) -> dict[str, str]:
    if target == "cron":
        _apply_cron(spec)
        return {}
    if target == "systemd":
        directory = _systemd_user_dir()
        directory.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}
        for name, content in files.items():
            path = directory / name
            path.write_text(content, encoding="utf-8")
            paths[name] = str(path)
        systemctl = _resolve_binary("systemctl")
        _run(
            [systemctl, "--user", "daemon-reload"],
            next_action="check 'systemctl --user status' and the unit files",
        )
        _run(
            [systemctl, "--user", "enable", "--now", f"{spec.name}.timer"],
            next_action=f"enable {spec.name}.timer manually",
        )
        return paths
    path = _windows_xml_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(files[f"{spec.name}.xml"], encoding="utf-16")
    schtasks = _resolve_binary("schtasks")
    _run(
        [schtasks, "/Create", "/XML", str(path), "/TN", spec.name, "/F"],
        next_action="open Task Scheduler and import the XML manually",
    )
    return {f"{spec.name}.xml": str(path)}


def _apply_cron(spec: ScheduleSpec) -> None:
    crontab = _resolve_binary("crontab")
    current = _read_crontab(crontab)
    updated = _replace_block(current, _cron_block(spec), spec.name)
    if updated == current:
        return  # already installed: keep the crontab untouched
    _run(
        [crontab, "-"],
        next_action="run 'crontab -l' and fix the entry manually",
        input_text=updated,
    )


def _read_crontab(crontab: str) -> str:
    completed = subprocess.run(  # noqa: S603 - absolute executable resolved from PATH
        [crontab, "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    if completed.returncode == 1 and "no crontab" in completed.stderr.lower():
        return ""
    raise SeoError(
        INTERNAL,
        "crontab -l failed",
        next_action="run 'crontab -l' manually to inspect the current crontab",
        detail={"exit_code": completed.returncode},
    )


def _cron_block(spec: ScheduleSpec) -> str:
    begin = _BLOCK_BEGIN.format(name=spec.name)
    end = _BLOCK_END.format(name=spec.name)
    return f"{begin}\n{render_cron(spec)}{end}\n"


def _replace_block(text: str, block: str, name: str) -> str:
    begin = _BLOCK_BEGIN.format(name=name)
    end = _BLOCK_END.format(name=name)
    lines = text.splitlines()
    if begin in lines and end in lines:
        start, stop = lines.index(begin), lines.index(end)
        if start < stop:
            lines = lines[:start] + lines[stop + 1 :]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.extend(block.rstrip("\n").splitlines())
    return "\n".join(lines) + "\n"


def _run(
    args: list[str],
    *,
    next_action: str,
    input_text: str | None = None,
) -> None:
    try:
        subprocess.run(  # noqa: S603 - callers pass an absolute executable resolved from PATH
            args,
            input=input_text,
            text=True,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise SeoError(
            INVALID_INPUT,
            f"{args[0]} is not available",
            next_action=next_action,
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SeoError(
            INTERNAL,
            f"{Path(args[0]).name} failed with exit code {exc.returncode}",
            next_action=next_action,
            detail={"command": Path(args[0]).name, "exit_code": exc.returncode},
        ) from exc


def _resolve_binary(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SeoError(
            INVALID_INPUT,
            f"{name} was not found in PATH",
            next_action=f"install {name} or use dry_run=True",
        )
    return executable


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _systemd_user_dir() -> Path:
    return _config_home() / "systemd" / "user"


def _windows_xml_path(spec: ScheduleSpec) -> Path:
    return _config_home() / "lv24-seo" / "schedule" / f"{spec.name}.xml"
