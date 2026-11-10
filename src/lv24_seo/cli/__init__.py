"""Shared plumbing for the ``lv24-seo`` command line.

The package stays import-light on purpose: importing :mod:`lv24_seo.cli` must not pull the
MCP SDK, open a database or read a token. Commands live in :mod:`lv24_seo.cli.main` and
:mod:`lv24_seo.cli.auth`; this module only owns output, exit codes and the version lookup.

Every command funnels its result through the standard ``{ok, data, meta, warnings}``
envelope. With ``--json`` exactly one JSON document is printed; without it the caller gets a
compact plain-text summary. Neither path emits ANSI escapes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Any

import typer

from lv24_seo.core.envelope import envelope_exit_code, fail
from lv24_seo.core.errors import INTERNAL, SeoError

__all__ = ["emit", "run_envelope", "tool_version"]

logger = logging.getLogger(__name__)


def tool_version() -> str:
    """Return the installed distribution version, or the source-tree version."""
    try:
        return pkg_version("lv24-seo")
    except PackageNotFoundError:  # running from a source tree without an install
        from lv24_seo import __version__

        return __version__


def run_envelope(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run *action* and degrade an unexpected crash to an ``INTERNAL`` envelope.

    Providers and the service layer already return typed failures; this guard only covers
    command wiring (config discovery, local files, storage). The exception text is never
    copied into the payload because it may echo a path or a request header.
    """
    try:
        return action()
    except SeoError as exc:
        return fail(exc)
    except Exception:
        logger.debug("unexpected CLI failure", exc_info=True)
        return fail(
            SeoError(
                code=INTERNAL,
                message="unexpected internal error",
                next_action="retry; if it persists run `lv24-seo doctor` and report the bug",
            )
        )


def emit(
    payload: dict[str, Any],
    *,
    as_json: bool,
    lines: Sequence[str] = (),
    exit_code: int | None = None,
) -> None:
    """Print exactly one envelope (or a plain summary) and exit with its code."""
    meta = payload.setdefault("meta", {})
    if isinstance(meta, dict):
        meta.setdefault("version", tool_version())

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        for line in lines:
            typer.echo(line)
        for warning in payload.get("warnings") or []:
            typer.echo(f"warning: {warning}")
        if not payload.get("ok"):
            error = payload.get("error") or {}
            code = error.get("code") or INTERNAL
            message = error.get("message") or "unknown error"
            typer.echo(f"error: {code}: {message}")
            next_action = error.get("next_action")
            if next_action:
                typer.echo(f"next: {next_action}")

    raise typer.Exit(code=envelope_exit_code(payload) if exit_code is None else exit_code)
