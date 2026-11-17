"""QA-03: engine-side contract tests for the ``lv24-seo --json`` envelope.

The desktop pins these commands through
``lv24-seo-optimizer-desktop/contracts/cli-v1/manifest.json``. This suite freezes the same
observable surface inside the engine repository, so an envelope or exit-code change fails
engine CI instead of the desktop after a PyPI release.

The tests drive the *installed* CLI as a subprocess and never import ``lv24_seo``: CI points
``LV24_SEO_CONTRACT_CLI`` at the console script of a freshly built wheel, which also proves
the shipped artefact behaves like the source tree. Every command runs in a temporary
workspace with a redirected home directory and every ``LV24_SEO_*`` variable removed, so the
fixtures are reproducible on Linux, macOS and Windows.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.contract

CONTRACT_FILE = Path(__file__).with_name("cli-v1-contract.json")
CONTRACT: dict[str, Any] = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
COMMANDS: list[dict[str, Any]] = CONTRACT["commands"]
ENVELOPE: dict[str, Any] = CONTRACT["envelope"]
ANSI_ESCAPE = re.compile(r"\x1b\[")
CLI_TIMEOUT_SECONDS = 180


def _cli_argv() -> list[str]:
    """Console script under test: CI override, then PATH, then this interpreter."""
    override = os.environ.get("LV24_SEO_CONTRACT_CLI")
    if override:
        return [override]
    found = shutil.which("lv24-seo")
    if found:
        return [found]
    return [sys.executable, "-c", "from lv24_seo.cli.main import app; app()"]


def _isolated_env(home: Path) -> dict[str, str]:
    """Environment without provider tokens or a user config that could leak into a fixture."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("LV24_SEO_")}
    env.update(
        {
            "APPDATA": str(home / "appdata"),
            "HOME": str(home),
            "LOCALAPPDATA": str(home / "localappdata"),
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TEMP": str(home / "tmp"),
            "TMP": str(home / "tmp"),
            "TMPDIR": str(home / "tmp"),
            "TZ": "UTC",
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / "xdg-config"),
        }
    )
    return env


def _run_cli(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    home = cwd / "home"
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [*_cli_argv(), *argv],
        cwd=cwd,
        env=_isolated_env(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CLI_TIMEOUT_SECONDS,
        check=False,
    )


def _single_json_document(stdout: str) -> dict[str, Any]:
    """``--json`` must print exactly one plain JSON object and nothing else."""
    text = stdout.strip()
    assert text, "the CLI printed nothing on stdout"
    assert not ANSI_ESCAPE.search(text), "stdout must not contain ANSI escapes"
    payload, end = json.JSONDecoder().raw_decode(text)
    trailing = text[end:].strip()
    assert not trailing, f"stdout must hold one JSON document, found trailing {trailing[:80]!r}"
    assert isinstance(payload, dict), "the envelope must be a JSON object"
    return payload


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload["data"]
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="session")
def engine_version(tmp_path_factory: pytest.TempPathFactory) -> str:
    cwd = tmp_path_factory.mktemp("version")
    result = _run_cli(["version", "--json"], cwd)
    assert result.returncode == 0, result.stderr
    return str(_data(_single_json_document(result.stdout))["version"])


@pytest.mark.parametrize("command", COMMANDS, ids=[str(item["id"]) for item in COMMANDS])
def test_cli_envelope_contract(
    command: dict[str, Any], tmp_path: Path, engine_version: str
) -> None:
    result = _run_cli(list(command["argv"]), tmp_path)
    payload = _single_json_document(result.stdout)

    assert result.returncode == command["exitCode"], (
        f"{command['id']} exited {result.returncode}, contract says {command['exitCode']}; "
        f"stderr: {result.stderr[-400:]}"
    )
    expected_keys = ENVELOPE["okKeys"] if command["ok"] else ENVELOPE["errorKeys"]
    assert sorted(payload) == sorted(expected_keys), f"{command['id']} envelope keys changed"
    assert payload["ok"] is command["ok"]
    assert isinstance(payload["warnings"], list)
    assert all(isinstance(item, str) for item in payload["warnings"])

    meta = payload["meta"]
    assert isinstance(meta, dict)
    for key in ENVELOPE["metaRequiredKeys"]:
        assert key in meta, f"{command['id']} meta lost {key!r}"
    assert meta["version"] == engine_version, f"{command['id']} meta.version drifted"

    if command["ok"]:
        data = _data(payload)
        for key in command["requiredDataKeys"]:
            assert key in data, f"{command['id']} data lost {key!r}"
        return

    error = payload["error"]
    assert isinstance(error, dict)
    for key in ENVELOPE["errorRequiredKeys"]:
        assert key in error, f"{command['id']} error lost {key!r}"
    allowed = {*ENVELOPE["errorRequiredKeys"], *ENVELOPE["errorOptionalKeys"]}
    assert set(error) <= allowed, f"{command['id']} error gained undocumented fields"
    assert error["code"] == command["errorCode"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["retryable"], bool)
    if "next_action" in error:
        assert isinstance(error["next_action"], str) and error["next_action"]
    assert error["code"] in ENVELOPE["exitCodes"], f"undocumented error code {error['code']}"
    assert result.returncode == ENVELOPE["exitCodes"][error["code"]], (
        f"{error['code']} must exit {ENVELOPE['exitCodes'][error['code']]}, got {result.returncode}"
    )


def test_version_matches_the_installed_distribution(engine_version: str) -> None:
    try:
        installed = distribution_version("lv24-seo")
    except PackageNotFoundError:
        pytest.skip("this interpreter has no lv24-seo distribution metadata")
    assert engine_version == installed


def test_doctor_checks_stay_renderable(tmp_path: Path) -> None:
    payload = _single_json_document(_run_cli(["doctor", "--json"], tmp_path).stdout)
    data = _data(payload)
    assert isinstance(data["healthy"], bool)
    checks = data["checks"]
    assert isinstance(checks, list) and checks
    for check in checks:
        required = {"detail", "id", "ok"}
        assert required <= set(check)
        assert set(check) <= required | {"code", "next_action"}
        assert isinstance(check["detail"], str)
        assert isinstance(check["ok"], bool)
        if "code" in check:
            assert isinstance(check["code"], str) and check["code"]
        if "next_action" in check:
            assert isinstance(check["next_action"], str)
    assert {"config", "platform", "python", "tokens"} <= {check["id"] for check in checks}


def test_properties_rows_keep_the_desktop_columns(tmp_path: Path) -> None:
    payload = _single_json_document(_run_cli(["properties", "--json"], tmp_path).stdout)
    data = _data(payload)
    assert set(data["project"]) >= {"base_url", "default_property", "name"}
    providers = data["providers"]
    assert isinstance(providers, list) and providers
    required = {"configured", "provider", "ready", "requires_token", "target", "token"}
    for row in providers:
        assert required <= set(row), f"provider row lost {sorted(required - set(row))}"
        assert isinstance(row["configured"], bool)
        assert isinstance(row["ready"], bool)
        assert isinstance(row["requires_token"], bool)
        assert isinstance(row["token"], str)


def test_auth_status_rows_keep_the_desktop_columns(tmp_path: Path) -> None:
    payload = _single_json_document(_run_cli(["auth", "status", "--json"], tmp_path).stdout)
    data = _data(payload)
    assert isinstance(data["missing"], list)
    assert isinstance(data["present"], list)
    providers = data["providers"]
    assert isinstance(providers, list) and providers
    required = {"expires_at", "present", "provider", "secret", "source"}
    for row in providers:
        assert required <= set(row), f"auth row lost {sorted(required - set(row))}"
        assert isinstance(row["present"], bool)
        assert isinstance(row["source"], str)
        assert row["expires_at"] is None or isinstance(row["expires_at"], str)


def test_stats_reports_coverage_honestly(tmp_path: Path) -> None:
    result = _run_cli(["stats", "--days", "7", "--lag-days", "2", "--json"], tmp_path)
    payload = _single_json_document(result.stdout)
    meta = payload["meta"]
    for key, kind in (("complete", bool), ("data_age_days", int), ("truncated", bool)):
        assert isinstance(meta[key], kind), f"stats meta.{key} must stay {kind.__name__}"
    assert isinstance(meta["limitations"], list)
    coverage = _data(payload)["coverage"]
    assert set(coverage) >= {"complete", "configured", "failed", "missing", "returned"}
    services = _data(payload)["services"]
    assert isinstance(services, list) and services
    required = {
        "configured",
        "data_age_days",
        "label",
        "limitations",
        "metrics",
        "provider",
        "ready",
        "rows",
        "status",
        "truncated",
        "warnings",
    }
    for service in services:
        assert required <= set(service), f"service row lost {sorted(required - set(service))}"
        assert isinstance(service["truncated"], bool)
        assert isinstance(service["limitations"], list)


def test_weekly_report_sections_stay_renderable(tmp_path: Path) -> None:
    argv = ["report", "weekly", "--days", "7", "--lag-days", "2", "--json"]
    payload = _single_json_document(_run_cli(argv, tmp_path).stdout)
    data = _data(payload)
    assert data["kind"] == "weekly"
    assert set(data["period"]) >= {"end", "start"}
    sections = data["sections"]
    assert isinstance(sections, list) and sections
    for section in sections:
        assert set(section) >= {"key", "notes", "rows", "summary", "title"}
        assert isinstance(section["rows"], list)
        assert isinstance(section["notes"], list)
    meta = payload["meta"]
    for key, kind in (("complete", bool), ("data_age_days", int), ("truncated", bool)):
        assert isinstance(meta[key], kind), f"weekly meta.{key} must stay {kind.__name__}"
    assert isinstance(meta["limitations"], list)


def test_contract_exit_codes_match_the_engine_mapping() -> None:
    """The frozen table must equal ``exit_code_for`` for every documented code."""
    errors = pytest.importorskip("lv24_seo.core.errors")
    documented = {
        name: value
        for name, value in vars(errors).items()
        if name.isupper() and isinstance(value, str) and value == name
    }
    assert set(documented) == set(ENVELOPE["exitCodes"]), (
        "error codes changed: update tests/contract/cli-v1-contract.json and the desktop "
        "contract together"
    )
    for code, expected in ENVELOPE["exitCodes"].items():
        actual = errors.exit_code_for(errors.SeoError(code=code, message="contract"))
        assert actual == expected, f"{code} exits {actual}, contract says {expected}"
