"""Contract tests for the canonical MCP tool registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lv24_seo.cli.main import app
from lv24_seo.mcp import registry
from lv24_seo.mcp.registry import TOOLS, tools_as_json


def test_tool_names_are_unique_and_snake_case() -> None:
    names = [tool.name for tool in TOOLS]
    assert len(names) == len(set(names))
    for name in names:
        assert name == name.lower()
        assert name.startswith("seo_") or name.startswith(("gsc_", "yandex_"))


def test_write_tools_are_annotated_as_destructive() -> None:
    payload = tools_as_json()
    by_name = {tool["name"]: tool for tool in payload["tools"]}  # type: ignore[index]
    for spec in TOOLS:
        annotations = by_name[spec.name]["annotations"]
        if spec.kind == "write":
            assert annotations["destructiveHint"] is True
            assert annotations["readOnlyHint"] is False
        else:
            assert annotations["destructiveHint"] is False
            assert annotations["readOnlyHint"] is (not spec.writes_local_state)
            assert annotations["idempotentHint"] is (not spec.writes_local_state)


def test_local_state_writers_are_not_advertised_as_read_only() -> None:
    payload = tools_as_json()
    by_name = {tool["name"]: tool for tool in payload["tools"]}  # type: ignore[index]

    for name in ("seo_crawl_site", "seo_weekly_report"):
        tool = by_name[name]
        assert tool["kind"] == "read"
        assert tool["writes_local_state"] is True
        assert tool["annotations"] == {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
        }


def test_every_tool_declares_providers_and_returns() -> None:
    for tool in TOOLS:
        assert tool.providers, f"{tool.name} has no providers"
        assert tool.summary.endswith(".") or tool.summary
        if tool.kind == "read":
            assert tool.returns or tool.providers == ("crawl",)


def test_registry_is_json_serialisable() -> None:
    payload = tools_as_json()
    text = json.dumps(payload, ensure_ascii=False)
    assert json.loads(text)["schema_version"] == 1
    assert len(payload["tools"]) == len(TOOLS)  # type: ignore[arg-type]


def test_checked_in_schema_matches_the_registry() -> None:
    schema = Path(__file__).resolve().parents[2] / "schemas" / "tools.json"

    assert json.loads(schema.read_text(encoding="utf-8")) == tools_as_json()


def test_generator_writes_and_checks_the_schema(tmp_path: Path) -> None:
    destination = tmp_path / "schemas" / "tools.json"

    assert registry.main(["--write", "--out", str(destination)]) == 0
    assert json.loads(destination.read_text(encoding="utf-8")) == tools_as_json()
    assert registry.main(["--check", "--out", str(destination)]) == 0

    destination.write_text("{}\n", encoding="utf-8")
    assert registry.main(["--check", "--out", str(destination)]) == 1


def test_generator_check_fails_when_the_schema_is_missing(tmp_path: Path) -> None:
    assert registry.main(["--check", "--out", str(tmp_path / "missing.json")]) == 1


@pytest.mark.parametrize("command", [["version", "--json"], ["doctor", "--json"]])
def test_cli_returns_envelope(command: list[str]) -> None:
    result = CliRunner().invoke(app, command)
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "data", "meta", "warnings"}
