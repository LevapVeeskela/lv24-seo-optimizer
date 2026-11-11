"""Tests for the full CLI surface (WP6.1-WP6.4).

Everything here is offline: provider access goes through a fake ``SeoService`` and secrets
through a fake ``SecretStore``. The tests assert the public contract only вЂ” envelope shape,
exit codes, redaction, host-config JSON and the files written by ``init``/``export``/``report``.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import auth as cli_auth
from lv24_seo.cli import main as cli_main
from lv24_seo.core.config import load_config
from lv24_seo.core.envelope import fail, ok
from lv24_seo.core.errors import AUTH_REQUIRED, SeoError
from lv24_seo.core.secrets import SECRET_NAMES, SecretStatus

RUNNER = CliRunner()

REPORT_DATA: dict[str, Any] = {
    "kind": "weekly",
    "property_id": "sc-domain:example.com",
    "period": {"start": "2026-11-01", "end": "2026-11-07"},
    "generated_at": "2026-11-10T06:00:00",
    "headline": "clicks +12%",
    "sections": [
        {
            "key": "gsc",
            "title": "Google Search Console",
            "summary": "clicks +12%",
            "rows": [{"metric": "clicks", "current": 10.0, "previous": 8.0}],
            "notes": [],
        }
    ],
    "limitations": ["data lag 2-3 days"],
    "warnings": [],
}


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every command out of the repository checkout."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic config: no ambient ``LV24_SEO_*`` overrides."""
    for name in list(os.environ):
        if name.startswith("LV24_SEO_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Example"\n'
        'base_url = "https://example.com"\n'
        'default_property = "sc-domain:example.com"\n',
        encoding="utf-8",
    )
    return path


def _invoke(*args: str, input: str | None = None) -> Any:
    return RUNNER.invoke(cli_main.app, list(args), input=input)


def _stdout(result: Any) -> str:
    return getattr(result, "stdout", None) or result.output


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, _stdout(result)
    return json.loads(_stdout(result))


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(_stdout(result))


def test_version_json_envelope() -> None:
    payload = _json(_invoke("version", "--json"))

    assert payload["ok"] is True
    assert payload["data"]["name"] == "lv24-seo"
    assert payload["data"]["version"]
    assert payload["meta"]["version"] == payload["data"]["version"]
    assert payload["warnings"] == []


def test_version_human_output_has_no_ansi() -> None:
    result = _invoke("version")

    assert result.exit_code == 0
    assert "lv24-seo" in _stdout(result)
    assert "\x1b[" not in _stdout(result)


def test_init_writes_template_and_data_dir(tmp_path: Path) -> None:
    target = tmp_path / "seo.toml"

    payload = _json(
        _invoke(
            "init",
            "--config",
            str(target),
            "--property",
            "https://example.com",
            "--json",
        )
    )

    assert target.is_file()
    assert (tmp_path / "data").is_dir()
    assert payload["data"]["overwritten"] is False
    config = load_config(target, env={})
    assert config.project.base_url == "https://example.com"
    assert config.project.default_property == "sc-domain:example.com"
    assert config.providers.gsc is not None
    parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    assert parsed["project"]["name"] == "example.com"
    assert parsed["providers"]["yandex_webmaster"]["host_id"] == "https:example.com:443"


def test_init_refuses_existing_config_without_force(tmp_path: Path) -> None:
    target = tmp_path / "seo.toml"
    target.write_text("sentinel = 1\n", encoding="utf-8")

    result = _invoke("init", "--config", str(target), "--json")

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert target.read_text(encoding="utf-8") == "sentinel = 1\n"


def test_init_force_overwrites_existing_config(tmp_path: Path) -> None:
    target = tmp_path / "seo.toml"
    target.write_text("sentinel = 1\n", encoding="utf-8")

    payload = _json(_invoke("init", "--config", str(target), "--force", "--json"))

    assert payload["data"]["overwritten"] is True
    text = target.read_text(encoding="utf-8")
    assert "sentinel" not in text
    assert "[project]" in text


def test_config_show_json_uses_redacted_dump(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    seen: dict[str, Any] = {}

    def fake_dump(config: Any) -> dict[str, Any]:
        seen["config"] = config
        return {
            "project": {"name": config.project.name},
            "providers": {"gsc": {"property": "sc-domain:example.com", "token": "***"}},
        }

    monkeypatch.setattr(cli_main, "redacted_dump", fake_dump)

    payload = _json(_invoke("config", "show", "--config", str(workspace), "--json"))

    assert payload["meta"]["redacted"] is True
    assert payload["data"]["config"]["providers"]["gsc"]["token"] == "***"
    assert seen["config"].project.name == "Example"
    assert "super-secret" not in json.dumps(payload)


class FakeSecretStore:
    """In-memory SecretStore with the real read/write/status surface."""

    values: ClassVar[dict[str, str]] = {}
    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, *, data_dir: Path | None = None, **_: Any) -> None:
        self.data_dir = data_dir

    def set(self, name: str, value: str) -> None:
        self.values[name] = value
        self.calls.append((name, value))

    def delete(self, name: str) -> None:
        self.values.pop(name, None)

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def status(self, names: Sequence[str] | None = None) -> list[SecretStatus]:
        selected = list(names) if names is not None else list(SECRET_NAMES)
        return [
            SecretStatus(
                name=name,
                present=name in self.values,
                source="file" if name in self.values else "missing",
            )
            for name in selected
        ]


@pytest.fixture()
def fake_secrets(monkeypatch: pytest.MonkeyPatch) -> type[FakeSecretStore]:
    FakeSecretStore.values = {}
    FakeSecretStore.calls = []
    monkeypatch.setattr(cli_auth, "SecretStore", FakeSecretStore)
    return FakeSecretStore


def test_auth_set_then_status_never_leaks_the_secret(
    fake_secrets: type[FakeSecretStore],
    workspace: Path,
) -> None:
    result = _invoke(
        "auth",
        "set",
        "--provider",
        "gsc",
        "--stdin",
        "--config",
        str(workspace),
        input="token-value\n",
    )

    assert result.exit_code == 0
    assert "token-value" not in _stdout(result)
    assert json.loads(_stdout(result)) == {
        "provider": "gsc",
        "stored": True,
        "source": "file",
    }
    assert fake_secrets.values["gsc_token"] == "token-value"

    payload = _json(_invoke("auth", "status", "--config", str(workspace), "--json"))
    providers = {entry["provider"]: entry for entry in payload["data"]["providers"]}
    assert providers["gsc"]["present"] is True
    assert providers["gsc"]["source"] == "file"
    assert providers["yandex"]["present"] is False
    assert payload["data"]["present"] == ["gsc"]
    assert "token-value" not in json.dumps(payload)


def test_auth_set_without_a_secret_is_a_usage_error(
    fake_secrets: type[FakeSecretStore],
    workspace: Path,
) -> None:
    result = _invoke(
        "auth",
        "set",
        "--provider",
        "gsc",
        "--stdin",
        "--config",
        str(workspace),
        "--json",
        input="\n",
    )

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert fake_secrets.values == {}


def test_auth_login_prints_manual_instructions(workspace: Path) -> None:
    result = _invoke("auth", "login", "--provider", "gsc", "--config", str(workspace))

    assert result.exit_code == 0
    output = _stdout(result)
    assert "auth set --provider gsc --stdin" in output
    assert "manual" in output


class FakeService:
    """Fake SeoService; records calls and returns prepared envelopes."""

    calls: ClassVar[list[dict[str, Any]]] = []
    response: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        config: Any,
        *,
        store: Any = None,
        secrets: Any = None,
        clients: Any = None,
    ) -> None:
        self.config = config
        self.store = store
        self.secrets = secrets
        self.clients = clients

    def properties(self) -> dict[str, Any]:
        return ok(
            {
                "project": {
                    "name": self.config.project.name,
                    "base_url": self.config.project.base_url,
                    "default_property": self.config.project.default_property,
                },
                "providers": [],
            },
            meta={"configured": []},
        )

    def gsc_query(
        self,
        *,
        start: Any,
        end: Any,
        dimensions: Sequence[str] = ("query",),
        limit: int = 100,
    ) -> dict[str, Any]:
        type(self).calls.append(
            {"start": start, "end": end, "dimensions": tuple(dimensions), "limit": limit}
        )
        return type(self).response

    def weekly_report(
        self,
        *,
        days: int = 7,
        lag_days: int = 2,
        send: bool = False,
    ) -> dict[str, Any]:
        type(self).calls.append({"days": days, "lag_days": lag_days, "send": send})
        return ok(REPORT_DATA, meta={"period": REPORT_DATA["period"]})


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch) -> type[FakeService]:
    FakeService.calls = []
    FakeService.response = ok(
        {"rows": [{"query": "seo", "clicks": 3.0}]},
        meta={"source": "gsc", "rows": 1, "data_age_days": 2},
    )
    monkeypatch.setattr(cli_main, "SeoService", FakeService)
    return FakeService


def test_gsc_query_envelope(fake_service: type[FakeService], workspace: Path) -> None:
    payload = _json(
        _invoke(
            "gsc",
            "query",
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-07",
            "--dimensions",
            "query,page",
            "--limit",
            "5",
            "--config",
            str(workspace),
            "--json",
        )
    )

    assert payload["ok"] is True
    assert payload["data"]["rows"] == [{"query": "seo", "clicks": 3.0}]
    assert payload["meta"]["source"] == "gsc"
    call = fake_service.calls[-1]
    assert call["dimensions"] == ("query", "page")
    assert call["limit"] == 5
    assert call["start"].isoformat() == "2026-11-01"
    assert call["end"].isoformat() == "2026-11-07"


def test_failed_service_result_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    class FailingService(FakeService):
        def gsc_query(self, **_: Any) -> dict[str, Any]:
            return fail(
                SeoError(
                    code=AUTH_REQUIRED,
                    message="Google Search Console token is missing",
                    next_action="lv24-seo auth set --provider gsc --stdin",
                )
            )

    monkeypatch.setattr(cli_main, "SeoService", FailingService)

    result = _invoke(
        "gsc",
        "query",
        "--start",
        "2026-11-01",
        "--end",
        "2026-11-07",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 3
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("client", ["claude", "cursor"])
def test_install_print_for_claude_style_clients(client: str) -> None:
    result = _invoke("install", "--client", client, "--print")

    assert result.exit_code == 0
    document = json.loads(_stdout(result))
    server = document["mcpServers"]["lv24-seo"]
    assert server == {
        "command": "uvx",
        "args": ["--from", "lv24-seo", "lv24-seo", "mcp", "serve"],
    }


def test_install_print_for_opencode() -> None:
    result = _invoke("install", "--client", "opencode", "--print")

    assert result.exit_code == 0
    document = json.loads(_stdout(result))
    server = document["mcp"]["lv24-seo"]
    assert server["type"] == "local"
    assert server["command"] == ["uvx", "--from", "lv24-seo", "lv24-seo", "mcp", "serve"]


def test_install_writes_and_merges_existing_servers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other-server"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_main, "_client_config_path", lambda client: target)

    payload = _json(_invoke("install", "--client", "claude", "--json"))

    assert payload["data"]["written"] is True
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["mcpServers"]["other"]["command"] == "other-server"
    assert document["mcpServers"]["lv24-seo"]["command"] == "uvx"


def test_export_tools_writes_valid_json(tmp_path: Path) -> None:
    destination = tmp_path / "schemas" / "tools.json"

    payload = _json(_invoke("export", "tools", "--out", str(destination), "--json"))

    document = json.loads(destination.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert len(document["tools"]) == payload["data"]["tools"] == 11
    names = {tool["name"] for tool in document["tools"]}
    assert "seo_properties" in names
    assert "seo_weekly_report" in names


def test_schedule_print_cron(workspace: Path) -> None:
    result = _invoke(
        "schedule",
        "print",
        "--cron",
        "0 6 * * 1",
        "--target",
        "cron",
        "--config",
        str(workspace),
    )

    assert result.exit_code == 0
    output = _stdout(result)
    assert "0 6 * * 1" in output
    assert "CRON_TZ=" in output
    assert "report weekly" in output


def test_schedule_print_json_envelope(workspace: Path) -> None:
    payload = _json(
        _invoke(
            "schedule",
            "print",
            "--cron",
            "0 6 * * 1",
            "--target",
            "cron",
            "--config",
            str(workspace),
            "--json",
        )
    )

    files = payload["data"]["files"]
    assert "lv24-seo-weekly.cron" in files
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["target"] == "cron"


def test_report_weekly_writes_json_and_html(
    fake_service: type[FakeService],
    workspace: Path,
    tmp_path: Path,
) -> None:
    out = tmp_path / "reports"

    payload = _json(
        _invoke(
            "report",
            "weekly",
            "--out",
            str(out),
            "--config",
            str(workspace),
            "--json",
        )
    )

    report_json = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report_json["headline"] == "clicks +12%"
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "clicks +12%" in html
    assert "<!DOCTYPE html>" in html
    assert payload["meta"]["artifacts"]["json"].endswith("report.json")


def test_report_out_falls_back_for_unstructured_data(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    class DictService(FakeService):
        def weekly_report(self, **_: Any) -> dict[str, Any]:
            return ok({"foo": "bar"})

    monkeypatch.setattr(cli_main, "SeoService", DictService)
    out = tmp_path / "reports"

    _json(
        _invoke(
            "report",
            "weekly",
            "--out",
            str(out),
            "--config",
            str(workspace),
            "--json",
        )
    )

    assert json.loads((out / "report.json").read_text(encoding="utf-8")) == {"foo": "bar"}
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "SEO report" in html
    assert "foo" in html


def test_doctor_json_reports_checks() -> None:
    payload = _json(_invoke("doctor", "--json"))

    ids = {check["id"] for check in payload["data"]["checks"]}
    assert {"python", "platform", "config", "data_dir", "tokens"} <= ids
    assert payload["data"]["healthy"] is True


def test_unknown_command_exits_two() -> None:
    result = _invoke("does-not-exist")

    assert result.exit_code == 2


def test_install_print_for_vscode_uses_servers_key() -> None:
    result = _invoke("install", "--client", "vscode", "--print")

    assert result.exit_code == 0
    document = json.loads(_stdout(result))
    server = document["servers"]["lv24-seo"]
    assert server["type"] == "stdio"
    assert server["command"] == "uvx"
