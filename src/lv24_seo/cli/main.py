"""Minimal CLI entry point.

Only `version` and `doctor` are implemented in the scaffold; the full command tree lands in W6
(see docs/plans/2026-11-10-implementation-plan.md).
"""

from __future__ import annotations

import json
import platform
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import typer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Free-first SEO control plane for humans and AI agents.",
)


def _tool_version() -> str:
    try:
        return pkg_version("lv24-seo")
    except PackageNotFoundError:  # running from a source tree
        from lv24_seo import __version__

        return __version__


@app.command()
def version(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """Print the tool version."""
    payload = {"name": "lv24-seo", "version": _tool_version()}
    if as_json:
        typer.echo(json.dumps({"ok": True, "data": payload, "meta": {}, "warnings": []}))
    else:
        typer.echo(f"lv24-seo {payload['version']}")


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """Check the local environment: python, config file, data directory, tokens."""
    config_path = Path.cwd() / "seo.toml"
    checks = [
        {"id": "python", "ok": sys.version_info >= (3, 11), "detail": platform.python_version()},
        {"id": "platform", "ok": True, "detail": f"{platform.system()} {platform.release()}"},
        {"id": "config", "ok": config_path.exists(), "detail": str(config_path)},
        {"id": "cwd_writable", "ok": _is_writable(Path.cwd()), "detail": str(Path.cwd())},
        {
            "id": "tokens",
            "ok": False,
            "detail": "not configured yet (W1: gsc / yandex / metrika / bing / crux)",
        },
    ]
    ok = all(item["ok"] for item in checks if item["id"] in {"python", "cwd_writable"})
    payload = {
        "ok": ok,
        "data": {"checks": checks},
        "meta": {"version": _tool_version()},
        "warnings": [],
    }
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        for item in checks:
            mark = "OK  " if item["ok"] else "FAIL"
            typer.echo(f"[{mark}] {item['id']}: {item['detail']}")
        typer.echo("")
        typer.echo("Next: lv24-seo init  (writes seo.toml)")
    if not ok:
        raise typer.Exit(code=1)


def _is_writable(path: Path) -> bool:
    try:
        probe = path / ".lv24-seo-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":  # pragma: no cover
    app()
