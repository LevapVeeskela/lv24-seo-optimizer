"""CLI contract tests for ``lv24-seo tasks`` (envelopes, exit codes, --config)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from lv24_seo.cli.tasks_cmd import register
from lv24_seo.store import Store

DEFAULT_PROPERTY = "sc-domain:example.com"
CONFIG = (
    '[project]\nname = "example"\ndefault_property = "sc-domain:example.com"\n\n'
    '[storage]\ndata_dir = "data"\n'
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("LV24_SEO_DATA_DIR", raising=False)
    (tmp_path / "data").mkdir()
    config = tmp_path / "seo.toml"
    config.write_text(CONFIG, encoding="utf-8")
    return config


def _invoke(*args: str) -> Any:
    app = typer.Typer(add_completion=False, no_args_is_help=True)
    register(app)
    return CliRunner().invoke(app, ["tasks", *args])


def _json(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


def _add(
    workspace: Path, *, url: str = "https://example.com/a", extra: tuple[str, ...] = ()
) -> int:
    result = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        url,
        "--rule",
        "content-decay",
        "--config",
        str(workspace),
        "--json",
        *extra,
    )
    assert result.exit_code == 0, result.stdout
    payload = _json(result)
    assert payload["ok"] is True
    return int(payload["data"]["task"]["id"])


def test_add_is_idempotent_and_returns_the_envelope(workspace: Path) -> None:
    first = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--payload",
        '{"months_stale":16}',
        "--config",
        str(workspace),
        "--json",
    )
    assert first.exit_code == 0
    payload = _json(first)
    assert payload["ok"] is True
    assert payload["data"]["outcome"] == "created"
    task = payload["data"]["task"]
    assert task["state"] == "open"
    assert task["property_id"] == DEFAULT_PROPERTY
    assert task["payload"] == {"months_stale": 16}
    assert payload["meta"]["version"]
    assert payload["warnings"] == []

    second = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--config",
        str(workspace),
        "--json",
    )
    assert second.exit_code == 0
    assert _json(second)["data"]["outcome"] == "existing"
    assert _json(second)["data"]["task"]["id"] == task["id"]

    # --config is honoured: the row lives in that workspace's data directory.
    with Store(workspace.parent / "data" / "seo.sqlite3") as store:
        assert len(store.list_tasks()) == 1


def test_list_filters_and_reports_truncation(workspace: Path) -> None:
    _add(workspace, url="https://example.com/1")
    _add(workspace, url="https://example.com/2")
    result = _invoke(
        "add",
        "--kind",
        "finding",
        "--url",
        "https://example.com/3",
        "--rule",
        "thin-content",
        "--config",
        str(workspace),
        "--json",
    )
    assert result.exit_code == 0

    filtered = _invoke("list", "--kind", "decay", "--config", str(workspace), "--json")
    assert filtered.exit_code == 0
    payload = _json(filtered)
    assert len(payload["data"]["tasks"]) == 2
    assert payload["meta"]["kind"] == "decay"
    assert payload["meta"]["property_id"] == DEFAULT_PROPERTY
    assert payload["meta"]["truncated"] is False
    assert payload["meta"]["count"] == 2
    assert payload["meta"]["total"] == 2
    assert payload["meta"]["data_age_days"] == 0

    capped = _invoke(
        "list", "--kind", "decay", "--limit", "1", "--config", str(workspace), "--json"
    )
    assert capped.exit_code == 0
    capped_payload = _json(capped)
    assert len(capped_payload["data"]["tasks"]) == 1
    assert capped_payload["meta"]["count"] == 1
    assert capped_payload["meta"]["total"] == 2
    assert capped_payload["meta"]["truncated"] is True
    assert capped_payload["meta"]["limitations"]

    exact = _invoke(
        "list", "--kind", "finding", "--limit", "1", "--config", str(workspace), "--json"
    )
    assert exact.exit_code == 0
    exact_payload = _json(exact)
    assert exact_payload["meta"]["count"] == exact_payload["meta"]["total"] == 1
    assert exact_payload["meta"]["truncated"] is False


def test_list_rejects_an_unknown_state(workspace: Path) -> None:
    _add(workspace)
    result = _invoke("list", "--state", "done", "--config", str(workspace), "--json")
    assert result.exit_code == 2
    payload = _json(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_INPUT"


def test_update_validates_transitions_and_returns_the_row(workspace: Path) -> None:
    task_id = _add(workspace)

    moved = _invoke(
        "update", str(task_id), "--state", "in_progress", "--config", str(workspace), "--json"
    )
    assert moved.exit_code == 0
    moved_payload = _json(moved)
    assert moved_payload["data"]["task"]["state"] == "in_progress"
    assert moved_payload["meta"]["task_id"] == task_id

    verified = _invoke(
        "update", str(task_id), "--state", "verified", "--config", str(workspace), "--json"
    )
    assert verified.exit_code == 0

    rejected = _invoke(
        "update", str(task_id), "--state", "in_progress", "--config", str(workspace), "--json"
    )
    assert rejected.exit_code == 2
    rejected_payload = _json(rejected)
    assert rejected_payload["ok"] is False
    assert rejected_payload["error"]["code"] == "INVALID_INPUT"
    assert "verified -> in_progress" in rejected_payload["error"]["message"]

    missing = _invoke("update", "999", "--state", "open", "--config", str(workspace), "--json")
    assert missing.exit_code == 2
    assert _json(missing)["error"]["code"] == "INVALID_INPUT"


def test_a_verified_task_reopens_when_the_finding_is_observed_again(workspace: Path) -> None:
    task_id = _add(workspace)
    assert (
        _invoke(
            "update", str(task_id), "--state", "verified", "--config", str(workspace), "--json"
        ).exit_code
        == 0
    )

    result = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--config",
        str(workspace),
        "--json",
    )
    assert result.exit_code == 0
    payload = _json(result)
    assert payload["data"]["outcome"] == "reopened"
    assert payload["data"]["task"]["state"] == "open"


@pytest.mark.parametrize(
    "payload",
    ['{"gsc_token":"super-secret-value"}', '{"evidence":{"api_key":"super-secret-value"}}'],
)
def test_add_refuses_credential_shaped_payload_keys(workspace: Path, payload: str) -> None:
    result = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--payload",
        payload,
        "--config",
        str(workspace),
        "--json",
    )
    assert result.exit_code == 2
    assert "super-secret-value" not in result.stdout
    assert _json(result)["error"]["code"] == "INVALID_INPUT"


def test_add_rejects_invalid_payload_json(workspace: Path) -> None:
    result = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--payload",
        "not-json",
        "--config",
        str(workspace),
        "--json",
    )
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "INVALID_INPUT"


def test_add_allows_a_token_count_field(workspace: Path) -> None:
    result = _invoke(
        "add",
        "--kind",
        "finding",
        "--url",
        "https://example.com/a",
        "--rule",
        "agent-review",
        "--payload",
        '{"tokens":12,"token_count":3}',
        "--config",
        str(workspace),
        "--json",
    )
    assert result.exit_code == 0
    assert _json(result)["data"]["task"]["payload"] == {"tokens": 12, "token_count": 3}


def test_missing_data_directory_is_config_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LV24_SEO_DATA_DIR", raising=False)
    config = tmp_path / "seo.toml"
    config.write_text(
        '[project]\ndefault_property = "sc-domain:example.com"\n\n[storage]\ndata_dir = "absent"\n',
        encoding="utf-8",
    )
    result = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--config",
        str(config),
        "--json",
    )
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "CONFIG_INVALID"


def test_property_falls_back_to_the_config_and_can_be_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LV24_SEO_DATA_DIR", raising=False)
    (tmp_path / "data").mkdir()
    config = tmp_path / "seo.toml"
    config.write_text('[storage]\ndata_dir = "data"\n', encoding="utf-8")

    missing = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--config",
        str(config),
        "--json",
    )
    assert missing.exit_code == 2
    assert _json(missing)["error"]["code"] == "INVALID_INPUT"

    explicit = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--property",
        "p2",
        "--config",
        str(config),
        "--json",
    )
    assert explicit.exit_code == 0
    assert _json(explicit)["data"]["task"]["property_id"] == "p2"


def test_human_output_is_compact(workspace: Path) -> None:
    added = _invoke(
        "add",
        "--kind",
        "decay",
        "--url",
        "https://example.com/a",
        "--rule",
        "content-decay",
        "--config",
        str(workspace),
    )
    assert added.exit_code == 0
    assert "added task" in added.stdout
    assert "open" in added.stdout

    listed = _invoke("list", "--config", str(workspace))
    assert listed.exit_code == 0
    assert "https://example.com/a" in listed.stdout
