"""Scheduler generators: cron, systemd user units and Windows Task Scheduler XML.

There is no daemon. Every renderer is pure, and :func:`install` defaults to a dry run, so
``lv24-seo schedule install`` shows the artefact before anything is written; the CLI only
passes ``dry_run=False`` after an explicit ``--allow-write``. :func:`status` inspects the
host read-only and :func:`uninstall` removes exactly the generated job, never a wildcard.

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
import xml.etree.ElementTree as ET
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
_MANAGED_BEGIN_PREFIX = "# >>> lv24-seo:"

_TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
_TASK_NOT_FOUND = ("cannot find", "not exist", "no such", "не удается найти", "не найдено")

# A status state is one of: installed, outdated, partial, missing, unknown.
_STATE_INSTALLED = "installed"
_STATE_OUTDATED = "outdated"
_STATE_PARTIAL = "partial"
_STATE_MISSING = "missing"
_STATE_UNKNOWN = "unknown"


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
        if not timezone_available(self.timezone):
            raise SeoError(
                INVALID_INPUT,
                f"unknown or unavailable timezone: {self.timezone!r}",
                next_action="install tzdata or choose an installed IANA timezone",
            )
        for label, value in (("description", self.description), ("workdir", self.workdir or "")):
            if "\n" in value or "\r" in value or "\0" in value:
                raise SeoError(
                    INVALID_INPUT,
                    f"{label} must be a single line without NUL characters",
                    next_action="remove control characters from the schedule metadata",
                )
        _cron_fields(self.cron)


def render_cron(spec: ScheduleSpec) -> str:
    """Render a newline-terminated crontab block.

    ``CRON_TZ`` is an environment assignment (honoured by Vixie/ISC cron;
    other cron daemons run on host time — see the install warnings), followed
    by the schedule line. ``workdir`` is applied with ``cd`` because crontab
    has no working-directory field.
    """

    command = spec.command.strip()
    if spec.workdir:
        command = f"cd {shlex.quote(spec.workdir)} && {command}"
    lines = [
        f"# {spec.description}",
        f"CRON_TZ={spec.timezone}",
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
        f"OnCalendar={_on_calendar(minute, hour, weekday)} {spec.timezone}",
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
    the requested weekday and time in ``spec.timezone``. Unknown zones are rejected.
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

    ``dry_run=True`` (the default) writes nothing and returns the artefacts;
    ``target="auto"`` resolves from :data:`sys.platform` (Windows, Linux and macOS are
    supported). The CLI only passes ``dry_run=False`` after ``--allow-write``.
    """

    resolved = _resolve_target(target)
    files = _render_files(spec, resolved)
    command = _install_command(spec, resolved)
    result: dict[str, Any] = {
        "target": resolved,
        "name": spec.name,
        "dry_run": dry_run,
        "installed": False,
        "files": files,
        "command": command,
        "paths": {},
        "warnings": _schedule_warnings(spec, resolved),
    }
    if dry_run:
        return result
    repair_warnings: list[str] = []
    result["paths"] = _apply(spec, resolved, files, warnings=repair_warnings)
    result["warnings"] = [*result["warnings"], *repair_warnings]
    result["installed"] = True
    return result


def status(spec: ScheduleSpec, *, target: str = "auto") -> dict[str, Any]:
    """Report whether the generated job exists and matches *spec*, without changing anything.

    ``state`` is ``installed`` (present and identical), ``outdated`` (present but the host
    copy differs from the current spec), ``partial`` (some generated artefacts exist, e.g. a
    systemd timer without its service), ``missing`` or ``unknown`` (the host scheduler cannot
    be inspected). ``matches`` is ``None`` when the state is unknown.
    """

    resolved = _resolve_target(target)
    if resolved == "cron":
        return _cron_status(spec)
    if resolved == "systemd":
        return _systemd_status(spec)
    return _windows_status(spec)


def uninstall(spec: ScheduleSpec, *, target: str = "auto", dry_run: bool = True) -> dict[str, Any]:
    """Remove exactly the generated job for *spec*; idempotent and never wildcard-based.

    ``dry_run=True`` (the default) only reports ``plan``. With ``dry_run=False`` the exact
    managed cron block, the two systemd user units or the named Task Scheduler task are
    removed. ``removed`` is False when nothing was installed, so a second run is a no-op.
    """

    resolved = _resolve_target(target)
    if resolved == "cron":
        return _cron_uninstall(spec, dry_run=dry_run)
    if resolved == "systemd":
        return _systemd_uninstall(spec, dry_run=dry_run)
    return _windows_uninstall(spec, dry_run=dry_run)


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
    if candidate.tzinfo is not None:
        # An aware boundary carries the UTC offset so Task Scheduler does not
        # silently reinterpret the time in another zone.
        return candidate.isoformat(timespec="seconds")
    return candidate.strftime("%Y-%m-%dT%H:%M:%S")


def timezone_available(timezone: str) -> bool:
    """Return True when the host can resolve the IANA ``timezone``."""
    if any(character.isspace() or character == "\0" for character in timezone):
        return False
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone))


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


def _schedule_warnings(spec: ScheduleSpec, target: str) -> list[str]:
    """Honest per-backend timezone limits (F23).

    Cron support for ``CRON_TZ`` varies. Systemd receives the IANA zone in
    ``OnCalendar``. Windows calendar boundaries carry an offset, not an IANA
    zone: schedules in zones with seasonal changes need operator review.
    """
    warnings: list[str] = []
    if target == "cron":
        warnings.append("confirm your cron daemon supports CRON_TZ; otherwise it uses host time")
    elif target == "windows":
        now = _now(spec.timezone)
        offsets = {now.replace(month=month, day=1).utcoffset() for month in range(1, 13)}
        if len(offsets) > 1:
            warnings.append(
                "Windows calendar XML has a fixed UTC offset, not the configured IANA zone; "
                "review the task after daylight-saving transitions"
            )
    return warnings


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


def _apply(
    spec: ScheduleSpec,
    target: str,
    files: dict[str, str],
    *,
    warnings: list[str] | None = None,
) -> dict[str, str]:
    if target == "cron":
        _apply_cron(spec, warnings=warnings)
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


def _apply_cron(spec: ScheduleSpec, *, warnings: list[str] | None = None) -> None:
    crontab = _resolve_binary("crontab")
    current = _read_crontab(crontab)
    updated = _replace_block(current, _cron_block(spec), spec.name, warnings=warnings)
    if updated == current:
        return  # already installed: keep the crontab untouched
    _run(
        [crontab, "-"],
        next_action="run 'crontab -l' and fix the entry manually",
        input_text=updated,
    )


# -- lifecycle: status and uninstall ---------------------------------------------------------


def _job_report(
    spec: ScheduleSpec,
    target: str,
    artifacts: list[dict[str, Any]],
    *,
    state: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Common read-only status payload; ``matches`` is null when the host is unreadable."""

    return {
        "target": target,
        "name": spec.name,
        "state": state,
        "present": any(bool(artifact["exists"]) for artifact in artifacts),
        "matches": None if state == _STATE_UNKNOWN else state == _STATE_INSTALLED,
        "paths": {str(artifact["kind"]): str(artifact["path"]) for artifact in artifacts},
        "artifacts": artifacts,
        "warnings": list(warnings or []),
    }


def _uninstall_report(
    spec: ScheduleSpec,
    target: str,
    *,
    dry_run: bool,
    state: str,
    plan: list[str],
    removed_paths: list[str] | None = None,
    removed_items: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Common uninstall payload: ``removed`` is false on a dry run and on a second run."""

    paths = list(removed_paths or [])
    items = list(removed_items) if removed_items is not None else list(paths)
    return {
        "target": target,
        "name": spec.name,
        "dry_run": dry_run,
        "state": state,
        "removed": bool(items),
        "removed_items": items,
        "removed_paths": paths,
        "plan": list(plan),
        "warnings": list(warnings or []),
    }


def _cron_status(spec: ScheduleSpec) -> dict[str, Any]:
    text, warning = _read_crontab_optional()
    artifact: dict[str, Any] = {
        "kind": "cron_block",
        "path": "user crontab",
        "exists": False,
        "matches": False,
    }
    if text is None:
        return _job_report(
            spec,
            "cron",
            [artifact],
            state=_STATE_UNKNOWN,
            warnings=[warning] if warning else [],
        )
    state, detail = _managed_block_state(text, spec)
    artifact["exists"] = state != _STATE_MISSING
    artifact["matches"] = state == _STATE_INSTALLED
    return _job_report(spec, "cron", [artifact], state=state, warnings=[detail] if detail else [])


def _managed_block_state(text: str, spec: ScheduleSpec) -> tuple[str, str | None]:
    """Classify the managed cron block: installed, outdated, partial or missing."""

    begin = _BLOCK_BEGIN.format(name=spec.name)
    end = _BLOCK_END.format(name=spec.name)
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == begin]
    if not starts:
        return _STATE_MISSING, None
    if len(starts) > 1:
        return (
            _STATE_OUTDATED,
            f"crontab has {len(starts)} managed blocks for {spec.name}; "
            "reinstall to collapse them into one",
        )
    start = starts[0]
    terminator = _block_terminator(lines, start + 1, begin, end)
    if terminator is None:
        return (
            _STATE_PARTIAL,
            "the managed cron block has no end marker; reinstall repairs it conservatively",
        )
    block = "\n".join(lines[start : terminator + 1]) + "\n"
    if block == _cron_block(spec):
        return _STATE_INSTALLED, None
    return _STATE_OUTDATED, None


def _read_crontab_optional() -> tuple[str | None, str | None]:
    """Read the crontab for status; an unreadable host is reported, never raised."""

    crontab = _which("crontab")
    if crontab is None:
        return None, "crontab is not available; the user crontab cannot be inspected"
    try:
        return _read_crontab(crontab), None
    except SeoError as exc:
        return None, f"cannot read the user crontab: {exc.message}"


def _systemd_status(spec: ScheduleSpec) -> dict[str, Any]:
    rendered = render_systemd(spec)
    directory = _systemd_user_dir()
    artifacts: list[dict[str, Any]] = []
    for kind in ("service", "timer"):
        path = directory / f"{spec.name}.{kind}"
        content = _read_text(path)
        artifacts.append(
            {
                "kind": kind,
                "path": str(path),
                "exists": content is not None,
                "matches": content is not None and content == rendered[kind],
            }
        )
    present = [artifact for artifact in artifacts if artifact["exists"]]
    warnings: list[str] = []
    if not present:
        state = _STATE_MISSING
    elif len(present) < len(artifacts):
        state = _STATE_PARTIAL
        missing = next(artifact["kind"] for artifact in artifacts if not artifact["exists"])
        warnings.append(
            f"{missing} unit is missing while the other unit exists; "
            "reinstall with --allow-write to repair the pair"
        )
    elif all(artifact["matches"] for artifact in artifacts):
        state = _STATE_INSTALLED
    else:
        state = _STATE_OUTDATED
        warnings.append("unit content differs from the current spec; reinstall to replace it")
    return _job_report(spec, "systemd", artifacts, state=state, warnings=warnings)


def _windows_status(spec: ScheduleSpec) -> dict[str, Any]:
    xml_path = _windows_xml_path(spec)
    content = _read_text(xml_path, encoding="utf-16")
    xml_artifact: dict[str, Any] = {
        "kind": "xml",
        "path": str(xml_path),
        "exists": content is not None,
        "matches": content is not None and content == render_windows_xml(spec),
    }
    task_artifact: dict[str, Any] = {
        "kind": "task",
        "path": f"Task Scheduler\\{spec.name}",
        "exists": False,
        "matches": False,
    }
    schtasks = _which("schtasks")
    if schtasks is None:
        return _job_report(
            spec,
            "windows",
            [task_artifact, xml_artifact],
            state=_STATE_UNKNOWN,
            warnings=["schtasks is not available; the registered task state is unknown"],
        )
    try:
        registered = _query_windows_task(schtasks, spec.name)
    except SeoError as exc:
        return _job_report(
            spec,
            "windows",
            [task_artifact, xml_artifact],
            state=_STATE_UNKNOWN,
            warnings=[f"cannot query Task Scheduler: {exc.message}"],
        )
    task_artifact["exists"] = registered is not None
    task_artifact["matches"] = registered is not None and _windows_xml_matches(registered, spec)
    warnings = []
    if registered is not None:
        state = _STATE_INSTALLED if task_artifact["matches"] else _STATE_OUTDATED
    elif xml_artifact["exists"]:
        state = _STATE_PARTIAL
        warnings.append("the generated XML exists but the task is not registered")
    else:
        state = _STATE_MISSING
    return _job_report(
        spec, "windows", [task_artifact, xml_artifact], state=state, warnings=warnings
    )


def _cron_uninstall(spec: ScheduleSpec, *, dry_run: bool) -> dict[str, Any]:
    crontab = _which("crontab")
    if crontab is None:
        if dry_run:
            return _uninstall_report(
                spec,
                "cron",
                dry_run=True,
                state=_STATE_UNKNOWN,
                plan=[],
                warnings=["crontab is not available; the user crontab cannot be inspected"],
            )
        crontab = _resolve_binary("crontab")
    try:
        current = _read_crontab(crontab)
    except SeoError as exc:
        if not dry_run:
            raise
        return _uninstall_report(
            spec,
            "cron",
            dry_run=True,
            state=_STATE_UNKNOWN,
            plan=[],
            warnings=[f"cannot read the user crontab: {exc.message}"],
        )
    begin = _BLOCK_BEGIN.format(name=spec.name)
    if not any(line.strip() == begin for line in current.splitlines()):
        return _uninstall_report(spec, "cron", dry_run=dry_run, state=_STATE_MISSING, plan=[])
    warnings: list[str] = []
    updated = _remove_block(current, spec.name, warnings=warnings)
    plan = [f"managed crontab block for {spec.name}"]
    if dry_run:
        return _uninstall_report(
            spec, "cron", dry_run=True, state="would_remove", plan=plan, warnings=warnings
        )
    _run(
        [crontab, "-"],
        next_action="run 'crontab -l' and remove the lv24-seo block manually",
        input_text=updated,
    )
    return _uninstall_report(
        spec,
        "cron",
        dry_run=False,
        state="removed",
        plan=plan,
        removed_items=plan,
        warnings=warnings,
    )


def _systemd_uninstall(spec: ScheduleSpec, *, dry_run: bool) -> dict[str, Any]:
    directory = _systemd_user_dir()
    plan: list[str] = []
    warnings: list[str] = []
    for kind in ("service", "timer"):
        path = directory / f"{spec.name}.{kind}"
        content = _read_text(path)
        if content is None:
            continue
        if _looks_like_generated_unit(content, kind, spec):
            plan.append(str(path))
        else:
            warnings.append(
                f"left {path} in place: its content does not look like the generated unit"
            )
    if not plan:
        state = "skipped" if warnings else _STATE_MISSING
        return _uninstall_report(
            spec, "systemd", dry_run=dry_run, state=state, plan=[], warnings=warnings
        )
    if dry_run:
        return _uninstall_report(
            spec, "systemd", dry_run=True, state="would_remove", plan=plan, warnings=warnings
        )
    systemctl = _which("systemctl")
    if systemctl is not None:
        failure = _run_optional([systemctl, "--user", "disable", "--now", f"{spec.name}.timer"])
        if failure:
            warnings.append(f"systemctl --user disable --now failed: {failure}")
    removed_paths: list[str] = []
    for target in plan:
        _unlink(Path(target))
        removed_paths.append(target)
    if systemctl is not None:
        failure = _run_optional([systemctl, "--user", "daemon-reload"])
        if failure:
            warnings.append(f"systemctl --user daemon-reload failed: {failure}")
    return _uninstall_report(
        spec,
        "systemd",
        dry_run=False,
        state="removed",
        plan=plan,
        removed_paths=removed_paths,
        warnings=warnings,
    )


def _windows_uninstall(spec: ScheduleSpec, *, dry_run: bool) -> dict[str, Any]:
    xml_path = _windows_xml_path(spec)
    xml_exists = _read_text(xml_path, encoding="utf-16") is not None
    schtasks = _which("schtasks")
    warnings: list[str] = []
    plan: list[str] = []
    task_registered = False
    if schtasks is None:
        warnings.append("schtasks is not available; a registered task, if any, is left in place")
    else:
        task_registered = _query_windows_task(schtasks, spec.name) is not None
        if task_registered:
            plan.append(f"Task Scheduler task {spec.name}")
    if xml_exists:
        plan.append(str(xml_path))
    if not plan:
        state = _STATE_UNKNOWN if schtasks is None else _STATE_MISSING
        return _uninstall_report(
            spec, "windows", dry_run=dry_run, state=state, plan=[], warnings=warnings
        )
    if dry_run:
        return _uninstall_report(
            spec, "windows", dry_run=True, state="would_remove", plan=plan, warnings=warnings
        )
    removed_paths: list[str] = []
    removed_items: list[str] = []
    if task_registered and schtasks is not None:
        _run(
            [schtasks, "/Delete", "/TN", spec.name, "/F"],
            next_action=f"open Task Scheduler and delete the task {spec.name!r} manually",
        )
        removed_items.append(f"Task Scheduler\\{spec.name}")
    if xml_exists:
        _unlink(xml_path)
        removed_paths.append(str(xml_path))
        removed_items.append(str(xml_path))
    return _uninstall_report(
        spec,
        "windows",
        dry_run=False,
        state="removed",
        plan=plan,
        removed_paths=removed_paths,
        removed_items=removed_items,
        warnings=warnings,
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


def _query_windows_task(schtasks: str, name: str) -> str | None:
    """Return the registered task XML, or ``None`` when the task does not exist."""

    completed = subprocess.run(  # noqa: S603 - absolute executable resolved from PATH
        [schtasks, "/Query", "/TN", name, "/XML"],
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0:
        return _decode_scheduler_output(completed.stdout)
    combined = _decode_scheduler_output(completed.stdout + completed.stderr).lower()
    if any(marker in combined for marker in _TASK_NOT_FOUND):
        return None
    raise SeoError(
        INTERNAL,
        "schtasks /Query failed",
        next_action=f"open Task Scheduler and check the task {name!r} manually",
        detail={"exit_code": completed.returncode},
    )


def _decode_scheduler_output(data: bytes) -> str:
    """Decode ``schtasks`` output, which is UTF-16 when redirected on Windows."""

    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if len(data) > 1 and data[1] == 0:
        return data.decode("utf-16-le", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if sys.platform == "win32":
            # Console output uses the OEM code page; the ANSI page is a fallback.
            for encoding in ("oem", "mbcs"):
                try:
                    return data.decode(encoding)
                except (UnicodeDecodeError, LookupError):
                    continue
        return data.decode("latin-1", errors="replace")


def _windows_xml_matches(registered_xml: str, spec: ScheduleSpec) -> bool:
    """Compare the action and the daily/weekly trigger of a registered task."""

    try:
        # Local output of `schtasks /Query`; the file is not fetched from the network.
        root = ET.fromstring(registered_xml)  # noqa: S314
    except (ET.ParseError, ValueError):
        return False
    action = root.find("t:Actions/t:Exec", _TASK_NS)
    trigger = root.find("t:Triggers/t:CalendarTrigger", _TASK_NS)
    if action is None or trigger is None:
        return False
    executable, arguments = _split_command(spec.command)
    command = (action.findtext("t:Command", namespaces=_TASK_NS) or "").strip()
    registered_arguments = (action.findtext("t:Arguments", namespaces=_TASK_NS) or "").strip()
    if command != executable or registered_arguments != arguments:
        return False
    if spec.workdir:
        workdir = (action.findtext("t:WorkingDirectory", namespaces=_TASK_NS) or "").strip()
        if workdir != spec.workdir:
            return False
    minute, hour, weekday = _simple_schedule(spec.cron, next_action=_WINDOWS_HINT)
    boundary_text = (trigger.findtext("t:StartBoundary", namespaces=_TASK_NS) or "").strip()
    try:
        boundary = datetime.fromisoformat(boundary_text)
    except ValueError:
        return False
    if (boundary.hour, boundary.minute) != (hour, minute):
        return False
    if weekday is None:
        return trigger.find("t:ScheduleByDay/t:DaysInterval", _TASK_NS) is not None
    day = _TASK_DAYS[weekday % 7]
    if trigger.find(f"t:ScheduleByWeek/t:DaysOfWeek/t:{day}", _TASK_NS) is None:
        return False
    return boundary.weekday() == (weekday - 1) % 7


def _looks_like_generated_unit(content: str, kind: str, spec: ScheduleSpec) -> bool:
    """True when a unit is the generated one, even if the spec changed after installation."""

    if content == render_systemd(spec)[kind]:
        return True
    lines = [line.strip() for line in content.splitlines()]
    if kind == "service":
        return any(line.startswith("ExecStart=") and "lv24-seo" in line for line in lines)
    return (
        any(line.startswith("Description=") and "lv24-seo" in line for line in lines)
        or f"Unit={spec.name}.service" in lines
    )


def _cron_block(spec: ScheduleSpec) -> str:
    begin = _BLOCK_BEGIN.format(name=spec.name)
    end = _BLOCK_END.format(name=spec.name)
    return f"{begin}\n{render_cron(spec)}{end}\n"


def _replace_block(
    text: str,
    block: str,
    name: str,
    *,
    warnings: list[str] | None = None,
) -> str:
    begin = _BLOCK_BEGIN.format(name=name)
    end = _BLOCK_END.format(name=name)
    lines = _strip_managed_blocks(text.splitlines(), begin, end, warnings=warnings)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.extend(block.rstrip("\n").splitlines())
    return "\n".join(lines) + "\n"


def _remove_block(text: str, name: str, *, warnings: list[str] | None = None) -> str:
    """Drop only the managed block for ``name`` and leave every other line in place."""

    begin = _BLOCK_BEGIN.format(name=name)
    end = _BLOCK_END.format(name=name)
    lines = _strip_managed_blocks(text.splitlines(), begin, end, warnings=warnings)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def _strip_managed_blocks(
    lines: list[str],
    begin: str,
    end: str,
    *,
    warnings: list[str] | None = None,
) -> list[str]:
    """Drop every managed block for ``begin``/``end``, repairing a truncated one safely.

    A missing end marker used to make the block extend to the next managed marker (or EOF),
    which silently deleted every user cron entry that followed the damaged block. Repair is
    now conservative: when no end marker exists, only lines that look like lv24-seo schedule
    entries (``CRON_TZ=``, a ``# lv24-seo`` comment, or a cron line whose command invokes
    ``lv24-seo``) are removed; everything else is kept and a warning tells the operator to
    review ``crontab -l``. A terminated block is still removed as a whole.
    """
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != begin:
            kept.append(lines[index])
            index += 1
            continue
        start = index
        terminator = _block_terminator(lines, index + 1, begin, end)
        if terminator is not None:
            index = terminator + 1
            continue
        if warnings is not None:
            warnings.append(
                f"crontab: repaired an unterminated managed block ({begin}); "
                "lines that did not look like lv24-seo entries were kept — review `crontab -l`"
            )
        index += 1
        while index < len(lines):
            marker = lines[index].strip()
            if marker == end or _is_managed_begin(marker):
                break
            if not _looks_managed_line(lines[index], position=index - start):
                kept.append(lines[index])
            index += 1
        if index < len(lines) and lines[index].strip() == end:
            index += 1  # stray end marker of the damaged block
    return kept


def _is_managed_begin(marker: str) -> bool:
    """True for any ``# >>> lv24-seo:<job> >>>`` marker, including another job's block."""
    return marker.startswith(_MANAGED_BEGIN_PREFIX) and marker.endswith(">>>")


def _block_terminator(lines: list[str], start: int, begin: str, end: str) -> int | None:
    """Index of the closing marker, or ``None`` when the block is unterminated.

    Another job's begin marker is a boundary as well: an unterminated block ends there, so a
    later install of that other job still finds and replaces its own intact block.
    """
    index = start
    while index < len(lines):
        marker = lines[index].strip()
        if marker == end:
            return index
        if marker == begin or _is_managed_begin(marker):
            return None
        index += 1
    return None


def _looks_managed_line(line: str, *, position: int) -> bool:
    """Recognise a line this module writes inside a managed cron block.

    Used only when a block has no end marker: anything that is not clearly ours is preserved,
    because deleting a user's cron job is unrecoverable while leaving a stale lv24-seo line is
    visible and harmless. ``position`` is the line offset from the begin marker; ``CRON_TZ=``
    is only ours near the top of the block, so a user's own zone assignment further down (it
    applies to their later entries) survives the repair.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("CRON_TZ="):
        return position <= 2
    if stripped.startswith("#") and "lv24-seo" in stripped:
        return True
    fields = stripped.split()
    return bool(len(fields) >= 6 and any("lv24-seo" in field for field in fields[5:]))


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


def _run_optional(args: list[str]) -> str | None:
    """Best-effort host command for teardown; return a short failure text instead of raising."""

    try:
        completed = subprocess.run(  # noqa: S603 - absolute executable resolved from PATH
            args,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return str(exc)
    if completed.returncode == 0:
        return None
    return (completed.stderr or completed.stdout).strip() or f"exit code {completed.returncode}"


def _which(name: str) -> str | None:
    """Resolve a host binary; tests monkeypatch this instead of touching the real PATH."""

    return shutil.which(name)


def _resolve_binary(name: str) -> str:
    executable = _which(name)
    if executable is None:
        raise SeoError(
            INVALID_INPUT,
            f"{name} was not found in PATH",
            next_action=f"install {name} or use dry_run=True",
        )
    return executable


def _read_text(path: Path, *, encoding: str = "utf-8") -> str | None:
    """Read a generated file for status; a missing or unreadable file is ``None``."""

    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeError):
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        raise SeoError(
            INTERNAL,
            f"cannot remove {path}",
            next_action="check the file permissions and remove the file manually",
        ) from exc


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _systemd_user_dir() -> Path:
    return _config_home() / "systemd" / "user"


def _windows_xml_path(spec: ScheduleSpec) -> Path:
    return _config_home() / "lv24-seo" / "schedule" / f"{spec.name}.xml"
