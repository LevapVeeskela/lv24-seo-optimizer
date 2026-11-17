"""Offline CLI tests for ``bing submit`` and the read-only Bing commands.

The Bing client is replaced by a fake in the service module, so a plan only performs the
read-only quota call and an apply goes through the same ``SeoService`` path the desktop uses.
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
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"
URL_C = "https://example.com/c"


class FakeBingClient:
    instances: ClassVar[list[FakeBingClient]] = []

    def __init__(self, *, api_key: str, site_url: str, **_: object) -> None:
        self.api_key = api_key
        self.site_url = site_url
        self.submit_calls: list[tuple[list[str], int]] = []
        self.details: list[str] = []
        self.closed = False
        FakeBingClient.instances.append(self)

    def submission_quota(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"daily_remaining": 100, "monthly_remaining": 1400, "raw": {}}],
            total=1,
            limitations=(
                "quota_monthly_can_bind_first",
                "quota_monthly_reset_undocumented",
            ),
        )

    def feeds(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=2,
            rows=[
                {
                    "url": FEED,
                    "type": "Sitemap",
                    "status": "Success",
                    "submitted": None,
                    "last_crawled": None,
                    "url_count": 10,
                    "file_size": 100,
                    "compressed": False,
                    "raw": {},
                }
            ],
            total=1,
            limitations=(
                "no_per_url_sitemap_errors",
                "sitemap_status_is_not_crawl_status",
            ),
        )

    def feed_details(self, feed_url: str) -> ProviderResult[dict[str, object]]:
        self.details.append(feed_url)
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"url": "https://example.com/child.xml", "status": "Success"}],
            total=1,
        )

    def crawl_issues(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[
                {
                    "url": URL_A,
                    "http_code": 404,
                    "issues": ["4xx"],
                    "issues_bits": 4,
                    "in_links": 2,
                    "raw": {},
                }
            ],
            total=1,
        )

    def submit_url_batches(
        self, urls: list[str], *, batch_size: int = 500
    ) -> ProviderResult[dict[str, object]]:
        self.submit_calls.append((list(urls), batch_size))
        batches = (len(urls) + batch_size - 1) // batch_size
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[
                {
                    "accepted": len(urls),
                    "submitted": len(urls),
                    "batches": batches,
                    "batch_size": batch_size,
                    "duplicates_removed": 0,
                    "status": "accepted",
                    "site_url": self.site_url,
                }
            ],
            limitations=("submit_idempotency_undocumented",),
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
        "\n"
        "[providers.bing]\n"
        f'site = "{SITE}"\n',
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
    return ("bing", "submit", "--config", str(workspace), *extra, "--json")


def _token(workspace: Path, *extra: str) -> str:
    result = _invoke(*_args(workspace, "--allow-write", *extra))
    assert result.exit_code == 0, result.output
    token = _payload(result)["data"]["confirm_token"]
    assert isinstance(token, str) and token
    return token


def _submissions() -> list[tuple[list[str], int]]:
    return [call for instance in FakeBingClient.instances for call in instance.submit_calls]


def _inventory(
    urls: list[str], *, errors: list[str] | None = None, truncated: bool = False
) -> cli_main._SitemapInventory:
    return cli_main._SitemapInventory(
        urls=list(urls), errors=list(errors or []), truncated=truncated
    )


def test_plan_reports_urls_batches_quota_and_no_submission(
    workspace: Path, write_env: None
) -> None:
    result = _invoke(*_args(workspace, "--url", URL_A, "--url", URL_B, "--allow-write"))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["plan"] == {
        "action": "bing_submit",
        "target": SITE,
        "summary": f"submit 2 URL(s) to Bing for {SITE} in 1 batch(es)",
        "payload": {
            "site": SITE,
            "urls": [URL_A, URL_B],
            "batch_size": 500,
            "batches": 1,
        },
    }
    assert payload["meta"]["quota"]["daily_remaining"] == 100
    assert payload["meta"]["quota"]["monthly_remaining"] == 1400
    assert payload["meta"]["total"] == 2
    assert payload["meta"]["truncated"] is False
    assert _submissions() == []
    assert FakeBingClient.instances[0].closed is True


def test_apply_submits_once_and_reports_accepted_and_quota_after(
    workspace: Path, write_env: None
) -> None:
    token = _token(workspace, "--url", URL_A, "--url", URL_B)

    result = _invoke(
        *_args(workspace, "--url", URL_A, "--url", URL_B, "--allow-write", "--confirm", token)
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["status"] == "applied"
    assert payload["data"]["result"]["status"] == "accepted"
    assert payload["data"]["result"]["accepted"] == 2
    assert payload["data"]["result"]["batches"] == 1
    assert payload["data"]["result"]["quota_after"]["daily_remaining"] == 100
    assert payload["meta"]["truncated"] is False
    assert "submit_idempotency_undocumented" in payload["meta"]["limitations"]
    assert _submissions() == [([URL_A, URL_B], 500)]


def test_plan_splits_into_batches_at_the_configured_size(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "_BING_SUBMIT_BATCH", 2)
    urls = [URL_A, URL_B, URL_C]

    plan = _payload(
        _invoke(
            *_args(
                workspace,
                "--url",
                urls[0],
                "--url",
                urls[1],
                "--url",
                urls[2],
                "--allow-write",
            )
        )
    )
    assert plan["data"]["plan"]["payload"]["batch_size"] == 2
    assert plan["data"]["plan"]["payload"]["batches"] == 2

    token = plan["data"]["confirm_token"]
    result = _invoke(
        *_args(
            workspace,
            "--url",
            urls[0],
            "--url",
            urls[1],
            "--url",
            urls[2],
            "--allow-write",
            "--confirm",
            token,
        )
    )

    assert result.exit_code == 0, result.output
    assert _submissions() == [(urls, 2)]
    assert _payload(result)["data"]["result"]["batches"] == 2


def test_from_sitemap_merges_dedupes_and_skips_off_host_urls(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_main,
        "_read_sitemap_inventory",
        lambda _url, _host: _inventory([URL_A, URL_A, URL_B, "https://evil.example/x"]),
    )

    result = _invoke(*_args(workspace, "--from-sitemap", "--url", URL_C, "--allow-write"))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    plan = payload["data"]["plan"]
    assert plan["payload"]["urls"] == [URL_C, URL_A, URL_B]
    assert plan["payload"]["sitemap_url"] == FEED
    assert payload["meta"]["total"] == 3
    assert any("outside example.com" in note for note in payload["meta"]["limitations"])
    assert _submissions() == []


def test_explicit_off_host_urls_are_skipped(workspace: Path, write_env: None) -> None:
    result = _invoke(
        *_args(workspace, "--url", URL_A, "--url", "https://evil.example/x", "--allow-write")
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["plan"]["payload"]["urls"] == [URL_A]
    assert payload["meta"]["total"] == 1
    assert any("outside example.com" in note for note in payload["meta"]["limitations"])


def test_a_sitemap_changed_between_plan_and_apply_invalidates_the_token(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventories = [
        _inventory([URL_A]),  # dry-run plan
        _inventory([URL_A]),  # apply plan: still matches the token
        _inventory([URL_B]),  # apply execute: the sitemap moved
    ]
    monkeypatch.setattr(cli_main, "_read_sitemap_inventory", lambda _url, _host: inventories.pop(0))
    token = _token(workspace, "--from-sitemap")

    result = _invoke(*_args(workspace, "--from-sitemap", "--allow-write", "--confirm", token))

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "CONFIRM_REQUIRED"
    assert _submissions() == []


def test_missing_key_is_auth_required(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)

    result = _invoke(*_args(workspace, "--url", URL_A, "--allow-write"))

    assert result.exit_code == 3
    payload = _payload(result)
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert "LV24_SEO_BING_API_KEY" in payload["error"]["next_action"]
    assert _submissions() == []


def test_missing_site_is_config_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "seo.toml"
    path.write_text(
        '[project]\nname = "Example"\nbase_url = "https://example.com"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)
    monkeypatch.setenv("LV24_SEO_BING_API_KEY", BING_KEY)

    result = _invoke(*_args(path, "--url", URL_A, "--allow-write"))

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert "providers.bing.site" in payload["error"]["next_action"]


def test_quota_read_is_read_only(workspace: Path, write_env: None) -> None:
    result = _invoke("bing", "quota", "--config", str(workspace), "--json")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["quota"]["daily_remaining"] == 100
    assert payload["meta"]["truncated"] is False
    assert "quota_monthly_can_bind_first" in payload["meta"]["limitations"]
    assert _submissions() == []


def test_sitemaps_read_is_read_only_and_reports_limits(workspace: Path, write_env: None) -> None:
    result = _invoke("bing", "sitemaps", "--config", str(workspace), "--json")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["rows"][0]["url"] == FEED
    assert "no_per_url_sitemap_errors" in payload["meta"]["limitations"]
    assert "sitemap_status_is_not_crawl_status" in payload["meta"]["limitations"]
    assert _submissions() == []


def test_sitemaps_details_lists_a_feed_index(workspace: Path, write_env: None) -> None:
    result = _invoke("bing", "sitemaps", "--details", FEED, "--config", str(workspace), "--json")

    assert result.exit_code == 0, result.output
    assert _payload(result)["data"]["rows"][0]["url"] == "https://example.com/child.xml"
    assert FakeBingClient.instances[0].details == [FEED]


def test_issues_read_is_read_only(workspace: Path, write_env: None) -> None:
    result = _invoke("bing", "issues", "--config", str(workspace), "--json")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["rows"][0]["issues"] == ["4xx"]
    assert payload["data"]["rows"][0]["issues_bits"] == 4
    assert _submissions() == []
