"""Offline CLI tests for ``indexing status|run|history`` and the indexing schedule."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from lv24_seo import indexing as indexing_module
from lv24_seo import services as services_module
from lv24_seo.cli import main as cli_main
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.base import ProviderResult

RUNNER = CliRunner()
SECRET = "unit-test-write-secret"
SITE = "https://example.com"
HOST_ID = "https:example.com:443"
PROPERTY = "sc-domain:example.com"
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"


def _result(source: str, rows: list[dict[str, Any]], **kwargs: Any) -> ProviderResult:
    return ProviderResult(
        source=source,
        property_id=kwargs.pop("property_id", SITE),
        fetched_at=utc_now(),
        data_age_days=0,
        rows=rows,
        **kwargs,
    )


class FakeBingClient:
    instances: ClassVar[list[FakeBingClient]] = []

    def __init__(self, *, api_key: str, site_url: str, **_: object) -> None:
        self.api_key = api_key
        self.site_url = site_url
        self.submit_calls: list[list[str]] = []
        self.url_info_calls: list[str] = []
        FakeBingClient.instances.append(self)

    def submission_quota(self) -> ProviderResult:
        return _result(
            "bing",
            [{"daily_remaining": 10, "monthly_remaining": 100, "raw": {}}],
            limitations=("quota_monthly_can_bind_first",),
        )

    def url_info(self, url: str) -> ProviderResult:
        self.url_info_calls.append(url)
        return _result(
            "bing",
            [{"url": url, "http_status": 200, "last_crawled_date": "2026-11-15T00:00:00+00:00"}],
        )

    def submit_url_batches(self, urls: list[str], *, batch_size: int = 500) -> ProviderResult:
        self.submit_calls.append(list(urls))
        return _result(
            "bing",
            [{"accepted": len(urls), "submitted": len(urls), "batches": 1}],
        )


class FakeGscClient:
    def __init__(self, *, token_provider: object, property_id: str) -> None:
        self.property_id = property_id

    def inspect_url(self, url: str, *, language_code: str = "en-US") -> ProviderResult:
        return _result(
            "gsc",
            [
                {
                    "url": url,
                    "verdict": "PASS",
                    "coverage_state": "Submitted and indexed",
                    "last_crawl_time": "2026-11-15T00:00:00+00:00",
                }
            ],
        )

    def list_sitemaps(self) -> ProviderResult:
        return _result(
            "gsc",
            [{"contents": [{"type": "web", "submitted": 10, "indexed": 7}]}],
            limitations=("sitemap_indexed_count_deprecated",),
        )


class FakeYandexClient:
    def __init__(self, *, token: str) -> None:
        self.token = token
        self.enqueue_calls: list[str] = []

    def recrawl_quota(self, host_id: str) -> ProviderResult:
        return _result(
            "yandex_webmaster",
            [{"daily_quota": 10, "quota_remainder": 5}],
            property_id=host_id,
        )

    def recrawl_enqueue(self, host_id: str, url: str) -> ProviderResult:
        self.enqueue_calls.append(url)
        return _result(
            "yandex_webmaster",
            [{"url": url, "status": "queued", "task_id": "task-1"}],
            property_id=host_id,
        )


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("LV24_SEO_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fake_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeBingClient.instances.clear()
    monkeypatch.setattr(services_module, "BingClient", FakeBingClient)
    monkeypatch.setattr(services_module, "GscClient", FakeGscClient)
    monkeypatch.setattr(services_module, "YandexWebmasterClient", FakeYandexClient)
    monkeypatch.setattr(
        indexing_module,
        "indexnow_submit",
        lambda **kwargs: _result(
            "indexnow", [{"submitted": len(kwargs["urls"]), "status": "accepted"}]
        ),
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Example"\n'
        f'base_url = "{SITE}"\n'
        "\n"
        "[providers.gsc]\n"
        f'property = "{PROPERTY}"\n'
        "\n"
        "[providers.yandex_webmaster]\n"
        f'host_id = "{HOST_ID}"\n'
        "\n"
        "[providers.bing]\n"
        f'site = "{SITE}"\n'
        'indexnow_key = "abc12345"\n'
        'indexnow_key_location = "https://example.com/abc12345.txt"\n'
        "\n"
        "[indexing]\n"
        "enabled = true\n"
        'providers = ["bing", "yandex", "gsc", "indexnow"]\n',
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    return path


@pytest.fixture()
def tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LV24_SEO_GSC_TOKEN", "gsc-token")
    monkeypatch.setenv("LV24_SEO_YANDEX_WEBMASTER_TOKEN", "yandex-token")
    monkeypatch.setenv("LV24_SEO_BING_API_KEY", "bing-key")
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(getattr(result, "stdout", None) or result.output)


def test_status_is_read_only_and_reports_write_secret(
    workspace: Path, tokens: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LV24_SEO_WRITE_SECRET")

    result = _invoke("indexing", "status", "--config", str(workspace), "--json")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["write_secret_present"] is False
    assert "write_secret_missing" in payload["meta"]["limitations"]
    assert set(payload["data"]["providers"]) == {"bing", "yandex", "gsc", "indexnow"}
    assert payload["data"]["providers"]["bing"]["indexed_total"] is None
    assert "no_per_url_index_count" in payload["data"]["providers"]["bing"]["limitations"]
    assert payload["data"]["providers"]["gsc"]["indexed_total"] == 7
    assert "gsc_indexed_count_deprecated" in payload["meta"]["limitations"]


def test_status_live_reads_quotas_and_gsc_aggregate(workspace: Path, tokens: None) -> None:
    result = _invoke(
        "indexing",
        "status",
        "--provider",
        "gsc",
        "--live",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["write_secret_present"] is True
    entry = payload["data"]["providers"]["gsc"]
    assert entry["indexed_total"] == 7
    assert entry["indexed_total_source"] == "gsc_sitemaps_contents"
    assert "gsc_indexed_count_deprecated" in payload["meta"]["limitations"]
    assert entry["quota"]["reset"]


def test_run_without_allow_write_only_plans(workspace: Path, tokens: None) -> None:
    result = _invoke(
        "indexing",
        "run",
        "--provider",
        "bing",
        "--url",
        URL_A,
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["meta"]["dry_run"] is True
    assert payload["data"]["providers"]["bing"]["planned"] == 1
    assert payload["data"]["providers"]["bing"]["submitted"] == 0
    assert FakeBingClient.instances[0].submit_calls == []


def test_run_with_allow_write_submits_and_records_history(workspace: Path, tokens: None) -> None:
    result = _invoke(
        "indexing",
        "run",
        "--provider",
        "bing",
        "--url",
        URL_A,
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["meta"]["dry_run"] is False
    assert payload["data"]["providers"]["bing"]["submitted"] == 1
    assert FakeBingClient.instances[0].submit_calls == [[URL_A]]

    history = _invoke("indexing", "history", "--config", str(workspace), "--json")
    assert history.exit_code == 0, history.output
    runs = _payload(history)["data"]["runs"]
    assert runs and runs[0]["provider"] == "bing"
    assert runs[0]["status"] == "applied"


def test_run_with_allow_write_requires_enabled_config(workspace: Path, tokens: None) -> None:
    workspace.write_text(
        workspace.read_text(encoding="utf-8").replace("enabled = true", "enabled = false"),
        encoding="utf-8",
    )

    result = _invoke(
        "indexing",
        "run",
        "--provider",
        "bing",
        "--url",
        URL_A,
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "WRITE_DISABLED"


def test_run_with_allow_write_requires_write_secret(
    workspace: Path, tokens: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LV24_SEO_WRITE_SECRET")

    result = _invoke(
        "indexing",
        "run",
        "--provider",
        "bing",
        "--url",
        URL_A,
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["error"]["code"] == "WRITE_DISABLED"
    assert FakeBingClient.instances == []


def test_indexing_schedule_renders_allow_write_and_absolute_config(
    workspace: Path, tokens: None
) -> None:
    spec, name = cli_main._schedule_spec("indexing", None, workspace, "cron")

    assert name == "indexing"
    assert spec.name == "lv24-seo-indexing"
    assert "indexing run --allow-write" in spec.command
    assert "--config" in spec.command
    assert str(workspace) in spec.command
    assert SECRET not in spec.command


def test_schedule_print_kind_indexing_renders_all_targets(workspace: Path, tokens: None) -> None:
    for target, expected in (
        ("cron", "lv24-seo-indexing.cron"),
        ("systemd", "lv24-seo-indexing.timer"),
        ("windows", "lv24-seo-indexing.xml"),
    ):
        result = _invoke(
            "schedule",
            "print",
            "--kind",
            "indexing",
            "--target",
            target,
            "--config",
            str(workspace),
            "--json",
        )
        assert result.exit_code == 0, result.output
        payload = _payload(result)
        assert expected in payload["data"]["files"]
        rendered = "\n".join(payload["data"]["files"].values())
        assert "indexing run --allow-write" in rendered


class _FakeCrontab:
    def __init__(self) -> None:
        self.text = "# user job\n0 1 * * * /usr/bin/true\n"
        self.writes: list[str] = []

    def read(self, crontab: str) -> str:
        del crontab
        return self.text

    def run(self, args: list[str], **kwargs: Any) -> None:
        del args
        input_text = kwargs.get("input_text")
        if input_text is not None:
            self.text = input_text
            self.writes.append(input_text)


def test_schedule_install_status_uninstall_for_indexing(
    workspace: Path, tokens: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lv24_seo.automation import scheduler

    fake = _FakeCrontab()
    monkeypatch.setattr(scheduler, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(scheduler, "_read_crontab", fake.read)
    monkeypatch.setattr(scheduler, "_run", fake.run)

    installed = _invoke(
        "schedule",
        "install",
        "--kind",
        "indexing",
        "--target",
        "cron",
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )
    assert installed.exit_code == 0, installed.output
    assert "# >>> lv24-seo:lv24-seo-indexing >>>" in fake.text
    assert "indexing run --allow-write" in fake.text
    assert "0 4 * * *" in fake.text

    status = _invoke(
        "schedule",
        "status",
        "--kind",
        "indexing",
        "--target",
        "cron",
        "--config",
        str(workspace),
        "--json",
    )
    assert status.exit_code == 0, status.output
    assert _payload(status)["data"]["state"] == "installed"

    removed = _invoke(
        "schedule",
        "uninstall",
        "--kind",
        "indexing",
        "--target",
        "cron",
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )
    assert removed.exit_code == 0, removed.output
    assert _payload(removed)["data"]["removed"] is True
    assert "lv24-seo-indexing" not in fake.text
    assert "0 1 * * * /usr/bin/true" in fake.text


def test_schedule_systemd_lifecycle_for_indexing(
    workspace: Path, tokens: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lv24_seo.automation import scheduler

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(scheduler, "_which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []
    monkeypatch.setattr(scheduler, "_run", lambda args, **kwargs: calls.append(args))

    installed = _invoke(
        "schedule",
        "install",
        "--kind",
        "indexing",
        "--target",
        "systemd",
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )
    assert installed.exit_code == 0, installed.output
    assert _payload(installed)["data"]["installed"] is True
    service = tmp_path / "config" / "systemd" / "user" / "lv24-seo-indexing.service"
    assert "indexing run --allow-write" in service.read_text(encoding="utf-8")

    status = _invoke(
        "schedule",
        "status",
        "--kind",
        "indexing",
        "--target",
        "systemd",
        "--config",
        str(workspace),
        "--json",
    )
    assert status.exit_code == 0, status.output
    assert _payload(status)["data"]["state"] == "installed"

    removed = _invoke(
        "schedule",
        "uninstall",
        "--kind",
        "indexing",
        "--target",
        "systemd",
        "--allow-write",
        "--config",
        str(workspace),
        "--json",
    )
    assert removed.exit_code == 0, removed.output
    assert _payload(removed)["data"]["removed"] is True
    assert not service.exists()
    assert any("enable" in call for call in calls)
