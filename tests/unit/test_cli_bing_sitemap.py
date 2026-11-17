"""Offline CLI tests for ``sitemap submit --provider bing``.

The Bing client is replaced by a fake, so a dry run proves it never contacts the provider and
an apply proves the same ``SeoService.submit_sitemap`` path the MCP tool uses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from lv24_seo import services as services_module
from lv24_seo.cli import main as cli_main
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.base import ProviderResult

RUNNER = CliRunner()
SECRET = "unit-test-write-secret"
BING_KEY = "unit-test-bing-key"
SITE = "https://example.com"
FEED = "https://example.com/sitemap.xml"


class FakeBingClient:
    instances: ClassVar[list[FakeBingClient]] = []

    def __init__(self, *, api_key: str, site_url: str, **_: object) -> None:
        self.api_key = api_key
        self.site_url = site_url
        self.feeds: list[str] = []
        self.closed = False
        FakeBingClient.instances.append(self)

    def submit_feed(self, sitemap_url: str) -> ProviderResult[dict[str, object]]:
        self.feeds.append(sitemap_url)
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"submitted": True, "site_url": self.site_url, "sitemap_url": sitemap_url}],
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("LV24_SEO_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeBingClient.instances.clear()
    monkeypatch.setattr(services_module, "BingClient", FakeBingClient)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Example"\n'
        'base_url = "https://example.com"\n'
        'default_property = "sc-domain:example.com"\n'
        "\n"
        "[providers.bing]\n"
        'site = "https://example.com"\n',
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    return path


@pytest.fixture()
def write_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)
    monkeypatch.setenv("LV24_SEO_BING_API_KEY", BING_KEY)


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(getattr(result, "stdout", None) or result.output)


def _args(workspace: Path, *extra: str) -> tuple[str, ...]:
    return (
        "sitemap",
        "submit",
        "--provider",
        "bing",
        "--config",
        str(workspace),
        *extra,
        "--json",
    )


def _token(workspace: Path, *extra: str) -> str:
    result = _invoke(*_args(workspace, "--allow-write", *extra))
    assert result.exit_code == 0, result.output
    token = _payload(result)["data"]["confirm_token"]
    assert isinstance(token, str) and token
    return token


def test_bing_dry_run_uses_the_config_site_and_default_sitemap(
    workspace: Path, write_env: None
) -> None:
    result = _invoke(*_args(workspace, "--allow-write"))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["meta"]["dry_run"] is True
    assert payload["data"]["plan"] == {
        "action": "sitemap_submit",
        "target": SITE,
        "summary": f"submit sitemap {FEED} to {SITE} via bing",
        "payload": {"property": SITE, "url": FEED, "action": "submit", "provider": "bing"},
    }
    assert FakeBingClient.instances == [], "a dry run must not construct a provider client"


def test_bing_apply_calls_submit_feed_once(workspace: Path, write_env: None) -> None:
    token = _token(workspace)

    result = _invoke(*_args(workspace, "--allow-write", "--confirm", token))

    assert result.exit_code == 0, result.output
    assert len(FakeBingClient.instances) == 1
    client = FakeBingClient.instances[0]
    assert client.api_key == BING_KEY
    assert client.site_url == SITE
    assert client.feeds == [FEED]
    assert client.closed is True
    payload = _payload(result)
    assert payload["data"]["status"] == "applied"
    assert payload["data"]["result"] == {
        "provider": "bing",
        "status": "submitted",
        "site_url": SITE,
        "sitemap_url": FEED,
    }
    assert payload["meta"]["source"] == "bing"


def test_bing_apply_replay_is_skipped_by_the_ledger(workspace: Path, write_env: None) -> None:
    token = _token(workspace)
    arguments = _args(workspace, "--allow-write", "--confirm", token)

    first = _invoke(*arguments)
    second = _invoke(*arguments)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert len(FakeBingClient.instances) == 1
    assert FakeBingClient.instances[0].feeds == [FEED]
    assert _payload(second)["data"]["status"] == "skipped"
    assert _payload(second)["data"]["reason"] == "duplicate"


def test_bing_missing_key_requires_auth(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)

    result = _invoke(*_args(workspace, "--allow-write"))

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert "LV24_SEO_BING_API_KEY" in payload["error"]["next_action"]


def test_bing_missing_site_is_config_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "seo.toml"
    path.write_text(
        '[project]\nname = "Example"\nbase_url = "https://example.com"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)
    monkeypatch.setenv("LV24_SEO_BING_API_KEY", BING_KEY)

    result = _invoke(*_args(path, "--allow-write"))

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert "providers.bing.site" in payload["error"]["next_action"]


def test_bing_explicit_property_and_url_override_the_config(
    workspace: Path, write_env: None
) -> None:
    result = _invoke(
        *_args(
            workspace,
            "--allow-write",
            "--property",
            "https://other.example",
            "--url",
            "https://other.example/feed.xml",
        )
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["plan"]["target"] == "https://other.example"
    assert payload["data"]["plan"]["payload"]["url"] == "https://other.example/feed.xml"
