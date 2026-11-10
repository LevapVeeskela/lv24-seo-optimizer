"""Automation helpers: host-scheduler generators (no daemon)."""

from lv24_seo.automation.scheduler import (
    ScheduleSpec,
    install,
    render_cron,
    render_systemd,
    render_windows_xml,
)

__all__ = [
    "ScheduleSpec",
    "install",
    "render_cron",
    "render_systemd",
    "render_windows_xml",
]
