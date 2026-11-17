"""Offline CLI tests for the read-only Bing diagnostics.

The provider client is a fake, so every command is proven to stay on the read path and to
keep the ``{ok, data, meta, warnings}`` envelope, including the empty-stats honesty note.
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
BING_KEY = "unit-test-bing-key"
SITE = "https://example.com"
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"


class FakeBingClient:
    instances: ClassVar[list[FakeBingClient]] = []

    def __init__(self, *, api_key: str, site_url: str, **_: object) -> None:
        self.api_key = api_key
        self.site_url = site_url
        self.children_calls: list[tuple[str, int]] = []
        self.join_calls: list[tuple[str, str, str]] = []
        self.url_info_calls: list[str] = []
        self.closed = False
        FakeBingClient.instances.append(self)

    def page_stats(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=1,
            rows=[
                {
                    "page": URL_A,
                    "clicks": 3,
                    "impressions": 40,
                    "click_position": 2.0,
                    "impression_position": 3.0,
                    "date": "2026-11-14T00:00:00+00:00",
                }
            ],
            total=1,
        )

    def rank_and_traffic(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=1,
            rows=[{"date": "2026-11-14T00:00:00+00:00", "clicks": 3, "impressions": 40}],
            total=1,
        )

    def crawl_stats(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=1,
            rows=[{"date": "2026-11-14T00:00:00+00:00", "code_2xx": 10}],
            total=1,
            limitations=("crawl_stats_six_month_window",),
        )

    def url_info(self, url: str) -> ProviderResult[dict[str, object]]:
        self.url_info_calls.append(url)
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=2,
            rows=[
                {
                    "url": url,
                    "http_status": 200,
                    "is_page": True,
                    "last_crawled_date": "2026-11-13T00:00:00+00:00",
                    "total_child_url_count": 0,
                }
            ],
            total=1,
        )

    def children_url_info(self, url: str, page: int = 0) -> ProviderResult[dict[str, object]]:
        self.children_calls.append((url, page))
        pages = {
            0: [{"url": URL_A, "http_status": 200, "is_page": True}],
            1: [{"url": URL_B, "http_status": 200, "is_page": True}],
        }
        rows = pages.get(page, [])
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=1 if rows else 0,
            rows=rows,
            total=len(rows),
        )

    def site_roles(self, **_: object) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"email": "user@example.com", "role": "ReadWrite", "role_code": 2}],
            total=1,
        )

    def user_sites(self) -> ProviderResult[dict[str, object]]:
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"url": "https://example.com/", "is_verified": True}],
            total=1,
        )

    def query_page_stats(self, query: str) -> ProviderResult[dict[str, object]]:
        self.join_calls.append(("query_pages", query, ""))
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"query": query, "page": URL_A, "clicks": 1, "impressions": 2}],
            total=1,
        )

    def page_query_stats(self, page: str) -> ProviderResult[dict[str, object]]:
        self.join_calls.append(("page_queries", "", page))
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"query": "seo tools", "page": page, "clicks": 1, "impressions": 2}],
            total=1,
        )

    def query_page_detail_stats(self, query: str, page: str) -> ProviderResult[dict[str, object]]:
        self.join_calls.append(("query_page_detail", query, page))
        return ProviderResult(
            source="bing",
            property_id=self.site_url,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[
                {
                    "query": query,
                    "page": page,
                    "clicks": 1,
                    "impressions": 2,
                    "position": 3.5,
                }
            ],
            total=1,
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
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LV24_SEO_BING_API_KEY", BING_KEY)
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Example"\n'
        'base_url = "https://example.com"\n'
        "\n"
        "[providers.bing]\n"
        'site = "https://example.com"\n',
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    return path


@pytest.fixture()
def workspace_without_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LV24_SEO_BING_API_KEY", BING_KEY)
    path = tmp_path / "seo.toml"
    path.write_text(
        '[project]\nname = "Example"\nbase_url = "https://example.com"\n',
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    return path


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(getattr(result, "stdout", None) or result.output)


def test_bing_pages_traffic_and_crawl_json(workspace: Path) -> None:
    pages = _invoke("bing", "pages", "--config", str(workspace), "--json")
    assert pages.exit_code == 0, pages.output
    assert _payload(pages)["data"]["rows"][0]["page"] == URL_A

    traffic = _invoke("bing", "traffic", "--config", str(workspace), "--json")
    assert traffic.exit_code == 0, traffic.output
    assert _payload(traffic)["data"]["rows"][0]["impressions"] == 40

    crawl = _invoke("bing", "crawl", "--config", str(workspace), "--json")
    assert crawl.exit_code == 0, crawl.output
    payload = _payload(crawl)
    assert payload["data"]["rows"][0]["code_2xx"] == 10
    assert "crawl_stats_six_month_window" in payload["meta"]["limitations"]


def test_bing_urlinfo_and_children_pass_the_target_and_page(workspace: Path) -> None:
    info = _invoke(
        "bing", "urlinfo", "--url", "domain:example.com", "--config", str(workspace), "--json"
    )
    assert info.exit_code == 0, info.output
    assert _payload(info)["data"]["url"] == "domain:example.com"
    assert FakeBingClient.instances[0].url_info_calls == ["domain:example.com"]

    children = _invoke(
        "bing",
        "children",
        "--url",
        "domain:example.com",
        "--page",
        "1",
        "--pages",
        "2",
        "--config",
        str(workspace),
        "--json",
    )
    assert children.exit_code == 0, children.output
    payload = _payload(children)
    assert payload["data"]["page"] == 1
    assert payload["data"]["pages_read"] == 2
    assert [row["url"] for row in payload["data"]["rows"]] == [URL_B]
    assert FakeBingClient.instances[-1].children_calls == [
        ("domain:example.com", 1),
        ("domain:example.com", 2),
    ]


def test_bing_users_and_sites_json(workspace: Path) -> None:
    users = _invoke("bing", "users", "--config", str(workspace), "--json")
    assert users.exit_code == 0, users.output
    assert _payload(users)["data"]["rows"][0]["role"] == "ReadWrite"

    sites = _invoke("bing", "sites", "--config", str(workspace), "--json")
    assert sites.exit_code == 0, sites.output
    assert _payload(sites)["data"]["rows"] == [{"url": "https://example.com/", "is_verified": True}]


def test_bing_sites_works_without_a_configured_site(workspace_without_site: Path) -> None:
    result = _invoke("bing", "sites", "--config", str(workspace_without_site), "--json")

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["rows"][0]["is_verified"] is True
    assert "site_url" not in payload["data"]
    assert FakeBingClient.instances[0].site_url == ""


def test_bing_joins_selects_the_mode(workspace: Path) -> None:
    query_pages = _invoke(
        "bing", "joins", "--query", "seo tools", "--config", str(workspace), "--json"
    )
    assert query_pages.exit_code == 0, query_pages.output
    assert _payload(query_pages)["data"]["mode"] == "query_pages"

    page_queries = _invoke("bing", "joins", "--page", URL_A, "--config", str(workspace), "--json")
    assert page_queries.exit_code == 0, page_queries.output
    assert _payload(page_queries)["data"]["mode"] == "page_queries"

    detail = _invoke(
        "bing",
        "joins",
        "--query",
        "seo tools",
        "--page",
        URL_A,
        "--config",
        str(workspace),
        "--json",
    )
    assert detail.exit_code == 0, detail.output
    payload = _payload(detail)
    assert payload["data"]["mode"] == "query_page_detail"
    assert payload["data"]["rows"][0]["position"] == 3.5


def test_bing_joins_without_a_dimension_is_invalid_input(workspace: Path) -> None:
    result = _invoke("bing", "joins", "--config", str(workspace), "--json")

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "INVALID_INPUT"


def test_bing_reads_missing_key_requires_auth(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LV24_SEO_BING_API_KEY", raising=False)

    result = _invoke("bing", "pages", "--config", str(workspace), "--json")

    assert result.exit_code == 3
    assert _payload(result)["error"]["code"] == "AUTH_REQUIRED"
    assert FakeBingClient.instances == []


def test_bing_reads_missing_site_is_config_invalid(workspace_without_site: Path) -> None:
    result = _invoke("bing", "pages", "--config", str(workspace_without_site), "--json")

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "CONFIG_INVALID"
    assert FakeBingClient.instances == []


def test_bing_pages_empty_window_is_not_rendered_as_zero(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class EmptyPages(FakeBingClient):
        def page_stats(self) -> ProviderResult[dict[str, object]]:
            return ProviderResult(
                source="bing",
                property_id=self.site_url,
                fetched_at=utc_now(),
                data_age_days=0,
                rows=[],
                total=0,
                limitations=("no_rows_for_window",),
            )

    monkeypatch.setattr(services_module, "BingClient", EmptyPages)

    result = _invoke("bing", "pages", "--config", str(workspace))

    assert result.exit_code == 0, result.output
    assert "rows: 0" in result.output
    assert "not a zero-impression result" in result.output
