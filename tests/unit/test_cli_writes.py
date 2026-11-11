"""Offline CLI tests for the gated write commands (WP8.2).

The tests assert the public envelope only: ``WRITE_DISABLED`` without ``--allow-write`` or
without ``LV24_SEO_WRITE_SECRET``, a dry-run token with ``--allow-write``, ``CONFIRM_REQUIRED``
for a stale/changed token, and one real IndexNow apply through a monkeypatched submit.
No provider is contacted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main
from lv24_seo.providers.base import ProviderResult, now_utc

RUNNER = CliRunner()

SECRET = "unit-test-write-secret"
PROPERTY = "sc-domain:example.com"
SITEMAP_URL = "https://example.com/sitemap.xml"
HOST_ID = "https:example.com:443"
INDEXNOW_HOST = "example.com"
INDEXNOW_KEY = "abc123def456"  # gitleaks:allow -- synthetic IndexNow fixture
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every command out of the repository checkout."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic environment: no ambient ``LV24_SEO_*`` values, including the write secret."""
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
        'default_property = "sc-domain:example.com"\n'
        "\n"
        "[providers.yandex_webmaster]\n"
        f'host_id = "{HOST_ID}"\n',
        encoding="utf-8",
    )
    return path


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _stdout(result: Any) -> str:
    return getattr(result, "stdout", None) or result.output


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(_stdout(result))


def _sitemap_args(workspace: Path, *extra: str) -> tuple[str, ...]:
    return (
        "sitemap",
        "submit",
        "--property",
        PROPERTY,
        "--url",
        SITEMAP_URL,
        "--config",
        str(workspace),
        *extra,
        "--json",
    )


def _dry_run_token(args: tuple[str, ...]) -> str:
    result = _invoke(*args, "--allow-write")
    assert result.exit_code == 0, _stdout(result)
    payload = _payload(result)
    token = payload["data"]["confirm_token"]
    assert isinstance(token, str) and token
    return token


# -- opt-in gate -----------------------------------------------------------------------------


def test_sitemap_submit_without_allow_write_is_refused(workspace: Path) -> None:
    result = _invoke(*_sitemap_args(workspace))

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "WRITE_DISABLED"
    assert "--allow-write" in payload["error"]["next_action"]


def test_allow_write_without_the_secret_is_refused(workspace: Path) -> None:
    result = _invoke(*_sitemap_args(workspace, "--allow-write"))

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["error"]["code"] == "WRITE_DISABLED"
    assert "LV24_SEO_WRITE_SECRET" in payload["error"]["message"]
    assert "LV24_SEO_WRITE_SECRET" in payload["error"]["next_action"]


def test_invalid_url_is_rejected_before_the_write_gate(workspace: Path) -> None:
    result = _invoke(
        "sitemap",
        "submit",
        "--property",
        PROPERTY,
        "--url",
        "not-a-url",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "INVALID_INPUT"


# -- dry run ---------------------------------------------------------------------------------


def test_sitemap_submit_allow_write_returns_plan_and_token(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)

    result = _invoke(*_sitemap_args(workspace, "--allow-write"))

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["meta"]["dry_run"] is True
    assert payload["meta"]["action"] == "sitemap_submit"
    data = payload["data"]
    assert data["status"] == "dry_run"
    assert data["confirm_token"]
    assert data["plan_hash"]
    assert data["plan"] == {
        "action": "sitemap_submit",
        "target": PROPERTY,
        "summary": f"submit sitemap {SITEMAP_URL} to {PROPERTY}",
        "payload": {"property": PROPERTY, "url": SITEMAP_URL, "action": "submit"},
    }
    assert SECRET not in _stdout(result)


def test_yandex_recrawl_dry_run_uses_the_configured_host(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)

    result = _invoke(
        "yandex",
        "recrawl",
        "--url",
        URL_A,
        "--url",
        URL_B,
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["meta"]["dry_run"] is True
    plan = payload["data"]["plan"]
    assert plan["action"] == "recrawl"
    assert plan["target"] == HOST_ID
    assert plan["payload"]["host_id"] == HOST_ID
    assert plan["payload"]["urls"] == [URL_A, URL_B]


# -- confirmation ----------------------------------------------------------------------------


def test_wrong_confirm_token_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)

    result = _invoke(*_sitemap_args(workspace, "--allow-write", "--confirm", "bogus-token"))

    assert result.exit_code == 2  # CONFIRM_REQUIRED maps to usage
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIRM_REQUIRED"
    assert "bogus-token" not in _stdout(result)


def test_a_token_does_not_confirm_a_changed_plan(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)
    token = _dry_run_token(_sitemap_args(workspace))

    result = _invoke(
        "sitemap",
        "submit",
        "--property",
        PROPERTY,
        "--url",
        "https://example.com/other.xml",
        "--confirm",
        token,
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "CONFIRM_REQUIRED"


def test_sitemap_apply_reports_that_the_write_is_not_wired(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)
    token = _dry_run_token(_sitemap_args(workspace))

    result = _invoke(*_sitemap_args(workspace, "--allow-write", "--confirm", token))

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "WRITE_DISABLED"
    assert "not wired yet" in payload["error"]["message"]


def test_recrawl_apply_reports_that_the_write_is_not_wired(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)
    args = (
        "yandex",
        "recrawl",
        "--url",
        URL_A,
        "--config",
        str(workspace),
        "--json",
    )
    token = _dry_run_token(args)

    result = _invoke(*args, "--allow-write", "--confirm", token)

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["error"]["code"] == "WRITE_DISABLED"
    assert "not wired yet" in payload["error"]["message"]


# -- IndexNow: the only wired write ----------------------------------------------------------


def test_indexnow_is_not_called_during_the_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_indexnow(*, host: str, key: str, urls: list[str]) -> dict[str, Any]:
        calls.append({"host": host, "key": key, "urls": list(urls)})
        return {"submitted": len(urls), "host": host}

    monkeypatch.setattr(cli_main, "indexnow_submit", fake_indexnow)
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)

    result = _invoke(
        "indexnow",
        "submit",
        "--host",
        INDEXNOW_HOST,
        "--key",
        INDEXNOW_KEY,
        "--url",
        URL_A,
        "--allow-write",
        "--json",
    )

    assert result.exit_code == 0
    assert _payload(result)["data"]["status"] == "dry_run"
    assert calls == []


def test_indexnow_apply_calls_submit_with_the_confirmed_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_indexnow(*, host: str, key: str, urls: list[str]) -> ProviderResult[dict[str, Any]]:
        calls.append({"host": host, "key": key, "urls": list(urls)})
        return ProviderResult(
            source="indexnow",
            property_id=host,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=[{"submitted": len(urls), "host": host}],
        )

    monkeypatch.setattr(cli_main, "indexnow_submit", fake_indexnow)
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)
    args = (
        "indexnow",
        "submit",
        "--host",
        INDEXNOW_HOST,
        "--key",
        INDEXNOW_KEY,
        "--url",
        URL_A,
        "--url",
        URL_B,
        "--json",
    )
    token = _dry_run_token(args)

    result = _invoke(*args, "--allow-write", "--confirm", token)

    assert result.exit_code == 0, _stdout(result)
    assert calls == [{"host": INDEXNOW_HOST, "key": INDEXNOW_KEY, "urls": [URL_A, URL_B]}]
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["meta"]["dry_run"] is False
    assert payload["data"]["status"] == "applied"
    assert payload["data"]["result"]["rows"] == [{"submitted": 2, "host": INDEXNOW_HOST}]
    assert payload["data"]["result"]["meta"]["source"] == "indexnow"


def test_indexnow_with_a_wrong_token_never_calls_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_indexnow(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(cli_main, "indexnow_submit", fake_indexnow)
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)

    result = _invoke(
        "indexnow",
        "submit",
        "--host",
        INDEXNOW_HOST,
        "--key",
        INDEXNOW_KEY,
        "--url",
        URL_A,
        "--allow-write",
        "--confirm",
        "bogus-token",
        "--json",
    )

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "CONFIRM_REQUIRED"
    assert calls == []
