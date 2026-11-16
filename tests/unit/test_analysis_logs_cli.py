"""Offline CLI tests for ``lv24-seo logs analyze`` (F3).

The sub-app is registered on a throwaway Typer app, so these tests do not depend on the root
command tree wiring (the integrator adds ``register(app)`` to ``cli.main``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from lv24_seo.cli.logs_cmd import logs_app, register

GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

RUNNER = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register(app)
    return app


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(_app(), ["logs", "analyze", *args])


def _stdout(result: Any) -> str:
    return getattr(result, "stdout", None) or result.output


def _log(tmp_path: Path) -> Path:
    lines = [
        {
            "time_iso8601": "2026-11-10T09:00:00+00:00",
            "request": "GET / HTTP/1.1",
            "status": 200,
            "body_bytes_sent": 100,
            "http_user_agent": GOOGLEBOT,
        },
        {
            "time_iso8601": "2026-11-10T10:00:00+00:00",
            "request": "GET /missing HTTP/1.1",
            "status": 404,
            "body_bytes_sent": 10,
            "http_user_agent": GOOGLEBOT,
        },
        {
            "time_iso8601": "2026-11-10T11:00:00+00:00",
            "request": "GET /assets/app.js HTTP/1.1",
            "status": 200,
            "body_bytes_sent": 7,
            "http_user_agent": BROWSER,
        },
        "",
        "not json",
    ]
    path = tmp_path / "access.log"
    path.write_text(
        "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def test_logs_analyze_returns_the_standard_envelope(tmp_path: Path) -> None:
    path = _log(tmp_path)

    result = _invoke("--file", str(path), "--json")

    assert result.exit_code == 0, _stdout(result)
    payload = json.loads(_stdout(result))
    assert set(payload) == {"ok", "data", "meta", "warnings"}
    assert payload["ok"] is True
    assert payload["data"]["total_lines"] == 5
    assert payload["data"]["parsed_lines"] == 3
    assert payload["data"]["skipped_lines"] == 2
    assert payload["data"]["crawl_waste"]["hits"] == 2
    assert payload["data"]["crawl_waste"]["share"] == 0.667
    assert payload["meta"]["source"] == "log_file"
    assert payload["meta"]["file"] == str(path)
    assert payload["meta"]["truncated"] is False
    assert payload["warnings"] == ["skipped 2 of 5 lines: blank or not nginx JSON"]


def test_logs_analyze_bot_filter_scopes_the_rollup(tmp_path: Path) -> None:
    path = _log(tmp_path)

    payload = json.loads(_stdout(_invoke("--file", str(path), "--bot", "googlebot", "--json")))

    assert payload["meta"]["bot"] == "googlebot"
    assert {row["bot"] for row in payload["data"]["bot_daily"]} == {"googlebot"}
    assert payload["data"]["crawl_waste"]["total_hits"] == 2
    assert payload["data"]["crawl_waste"]["share"] == 0.5


def test_logs_analyze_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    result = _invoke("--file", str(tmp_path / "missing.log"), "--json")

    assert result.exit_code == 2, _stdout(result)
    payload = json.loads(_stdout(result))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert payload["error"]["next_action"]


@pytest.mark.parametrize(
    "raw",
    ["access.log", "*.log", "//server/share/access.log"],
)
def test_logs_analyze_rejects_relative_glob_and_unc_paths(raw: str) -> None:
    result = _invoke("--file", raw, "--json")

    assert result.exit_code == 2, _stdout(result)
    assert json.loads(_stdout(result))["error"]["code"] == "INVALID_INPUT"


def test_logs_analyze_plain_text_summary(tmp_path: Path) -> None:
    path = _log(tmp_path)

    result = _invoke("--file", str(path))

    assert result.exit_code == 0, _stdout(result)
    output = _stdout(result)
    assert f"file: {path}" in output
    assert "lines: 5 total, 3 parsed, 2 skipped" in output
    assert "crawl waste: 2 of 3 requests" in output
    assert "{" not in output


def test_register_attaches_the_logs_sub_app() -> None:
    app = _app()

    attached = {group.name: group.typer_instance for group in app.registered_groups}
    assert attached.get("logs") is logs_app
    assert "analyze" in {command.name for command in logs_app.registered_commands}
