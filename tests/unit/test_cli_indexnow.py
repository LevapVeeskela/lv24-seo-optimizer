"""Offline CLI tests for one-click IndexNow (``--from-sitemap`` + config defaults).

No network is involved: the sitemap inventory and the IndexNow POST are replaced by fakes.
The reader itself is tested against a fake ``Fetcher`` so urlset, sitemapindex bounds and the
10k URL cap are covered without leaving the process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main
from lv24_seo.core.config import BingConfig, load_config, redacted_dump
from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.core.timeutil import utc_now
from lv24_seo.crawl.models import FetchResult
from lv24_seo.providers.base import ProviderResult

RUNNER = CliRunner()
SECRET = "unit-test-write-secret"
KEY = "abc12345"
HOST = "example.com"
SITEMAP = "https://example.com/sitemap.xml"
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"
URL_C = "https://example.com/c"

_URLSET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
)
_INDEX = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'
)


def _urlset(urls: list[str]) -> bytes:
    items = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return _URLSET.format(items=items).encode()


def _index(children: list[str]) -> bytes:
    items = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in children)
    return _INDEX.format(items=items).encode()


class FakeFetcher:
    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.calls: list[str] = []
        self.closed = False

    def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
        self.calls.append(url)
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url=url, status=404, final_url=url)
        return FetchResult(url=url, status=200, final_url=url, body=body)

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


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[project]\n"
        'name = "Example"\n'
        'base_url = "https://example.com"\n'
        "\n"
        "[providers.bing]\n"
        'site = "https://example.com"\n'
        f'indexnow_key = "{KEY}"\n',
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    return path


@pytest.fixture()
def write_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", SECRET)


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(getattr(result, "stdout", None) or result.output)


def _args(workspace: Path, *extra: str) -> tuple[str, ...]:
    return ("indexnow", "submit", "--config", str(workspace), *extra, "--json")


def _token(workspace: Path, *extra: str) -> str:
    result = _invoke(*_args(workspace, "--allow-write", *extra))
    assert result.exit_code == 0, result.output
    token = _payload(result)["data"]["confirm_token"]
    assert isinstance(token, str) and token
    return token


def _inventory(
    urls: list[str], *, errors: list[str] | None = None, truncated: bool = False
) -> cli_main._SitemapInventory:
    return cli_main._SitemapInventory(
        urls=list(urls), errors=list(errors or []), truncated=truncated
    )


def test_bing_config_indexnow_fields_are_public_and_default_empty() -> None:
    assert BingConfig().indexnow_key == ""
    assert BingConfig().indexnow_key_location == ""


def test_indexnow_config_fields_load_and_are_not_redacted(tmp_path: Path) -> None:
    path = tmp_path / "seo.toml"
    path.write_text(
        "[providers.bing]\n"
        'site = "https://example.com"\n'
        f'indexnow_key = "{KEY}"\n'
        'indexnow_key_location = "https://example.com/key.txt"\n',
        encoding="utf-8",
    )

    config = load_config(path, env={})
    assert config.providers.bing is not None
    assert config.providers.bing.indexnow_key == KEY
    assert config.providers.bing.indexnow_key_location == "https://example.com/key.txt"
    dumped = redacted_dump(config)["providers"]["bing"]
    assert dumped["indexnow_key"] == KEY
    assert dumped["indexnow_key_location"] == "https://example.com/key.txt"


def test_indexnow_env_overrides_the_public_config_fields() -> None:
    config = load_config(
        None,
        env={
            "LV24_SEO_INDEXNOW_KEY": KEY,
            "LV24_SEO_INDEXNOW_KEY_LOCATION": "https://example.com/key.txt",
        },
    )

    assert config.providers.bing is not None
    assert config.providers.bing.indexnow_key == KEY
    assert config.providers.bing.indexnow_key_location == "https://example.com/key.txt"


def test_one_click_uses_config_host_key_and_default_sitemap(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_main, "_read_sitemap_inventory", lambda _url, _host: _inventory([URL_A, URL_B])
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli_main, "indexnow_submit", lambda **kwargs: calls.append(kwargs))

    result = _invoke(*_args(workspace, "--from-sitemap", "--allow-write"))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    plan = payload["data"]["plan"]
    assert plan["target"] == HOST
    assert plan["payload"] == {
        "host": HOST,
        "key": KEY,
        "key_location": f"https://{HOST}/{KEY}.txt",
        "urls": [URL_A, URL_B],
        "sitemap_url": SITEMAP,
    }
    assert payload["meta"]["truncated"] is False
    assert payload["meta"]["total"] == 2
    assert payload["meta"]["limitations"] == []
    assert calls == []


def test_explicit_urls_merge_with_the_sitemap_and_dedupe(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_main,
        "_read_sitemap_inventory",
        lambda _url, _host: _inventory([URL_A, URL_A, URL_B]),
    )

    result = _invoke(
        *_args(workspace, "--from-sitemap", "--url", URL_C, "--url", URL_A, "--allow-write")
    )

    assert result.exit_code == 0, result.output
    assert _payload(result)["data"]["plan"]["payload"]["urls"] == [URL_C, URL_A, URL_B]


def test_missing_key_requires_auth_and_names_the_config_field(
    tmp_path: Path, write_env: None
) -> None:
    path = tmp_path / "seo.toml"
    path.write_text(
        '[project]\nbase_url = "https://example.com"\n'
        '[providers.bing]\nsite = "https://example.com"\n',
        encoding="utf-8",
    )

    result = _invoke(*_args(path, "--from-sitemap", "--allow-write"))

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert "providers.bing.indexnow_key" in payload["error"]["next_action"]
    assert f"https://{HOST}/<key>.txt" in payload["error"]["next_action"]


def test_neither_url_nor_sitemap_is_invalid(workspace: Path, write_env: None) -> None:
    result = _invoke(*_args(workspace, "--allow-write"))

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "INVALID_INPUT"


def test_the_url_cap_sets_truncated_and_a_limitation(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "_INDEXNOW_URL_LIMIT", 3)
    monkeypatch.setattr(
        cli_main,
        "_read_sitemap_inventory",
        lambda _url, _host: _inventory(
            [
                "https://example.com/1",
                "https://example.com/2",
                "https://example.com/3",
                "https://example.com/4",
                "https://example.com/5",
            ]
        ),
    )

    result = _invoke(*_args(workspace, "--from-sitemap", "--allow-write"))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["plan"]["payload"]["urls"] == [
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/3",
    ]
    assert payload["meta"]["truncated"] is True
    assert payload["meta"]["total"] == 5
    assert any("capped at 3" in note for note in payload["meta"]["limitations"])


def test_off_host_sitemap_urls_are_skipped(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_main,
        "_read_sitemap_inventory",
        lambda _url, _host: _inventory([URL_A, "https://evil.example/x"]),
    )

    result = _invoke(*_args(workspace, "--from-sitemap", "--allow-write"))

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["data"]["plan"]["payload"]["urls"] == [URL_A]
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
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli_main, "indexnow_submit", lambda **kwargs: calls.append(kwargs))
    token = _token(workspace, "--from-sitemap")

    result = _invoke(*_args(workspace, "--from-sitemap", "--allow-write", "--confirm", token))

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "CONFIRM_REQUIRED"
    assert calls == []


def test_apply_submits_the_refreshed_merged_list(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_main, "_read_sitemap_inventory", lambda _url, _host: _inventory([URL_A, URL_B])
    )
    calls: list[dict[str, Any]] = []

    def fake_indexnow(
        *, host: str, key: str, urls: list[str], key_location: str | None = None
    ) -> ProviderResult[dict[str, Any]]:
        calls.append({"host": host, "key": key, "urls": list(urls), "key_location": key_location})
        return ProviderResult(
            source="indexnow",
            property_id=host,
            fetched_at=utc_now(),
            data_age_days=0,
            rows=[{"submitted": len(urls), "host": host, "status_code": 200}],
        )

    monkeypatch.setattr(cli_main, "indexnow_submit", fake_indexnow)
    token = _token(workspace, "--from-sitemap", "--url", URL_C)

    result = _invoke(
        *_args(workspace, "--from-sitemap", "--url", URL_C, "--allow-write", "--confirm", token)
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "host": HOST,
            "key": KEY,
            "urls": [URL_C, URL_A, URL_B],
            "key_location": f"https://{HOST}/{KEY}.txt",
        }
    ]
    payload = _payload(result)
    assert payload["data"]["status"] == "applied"
    assert payload["data"]["result"]["rows"] == [{"submitted": 3, "host": HOST, "status_code": 200}]


def test_provider_422_surfaces_the_key_file_next_action(
    workspace: Path, write_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_main, "_read_sitemap_inventory", lambda _url, _host: _inventory([URL_A])
    )

    def rejected(**_: Any) -> None:
        raise SeoError(
            code=INVALID_INPUT,
            message="IndexNow rejected the key (HTTP 422): key file missing or mismatched",
            next_action=(
                f"publish the key at https://{HOST}/{KEY}.txt with exactly the key as the body"
            ),
            detail={"status_code": 422},
        )

    monkeypatch.setattr(cli_main, "indexnow_submit", rejected)
    token = _token(workspace, "--from-sitemap")

    result = _invoke(*_args(workspace, "--from-sitemap", "--allow-write", "--confirm", token))

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert f"https://{HOST}/{KEY}.txt" in payload["error"]["next_action"]


# -- the real bounded sitemap reader ---------------------------------------------------------


def _patch_fetcher(monkeypatch: pytest.MonkeyPatch, pages: dict[str, bytes]) -> FakeFetcher:
    fetcher = FakeFetcher(pages)
    monkeypatch.setattr(cli_main, "Fetcher", lambda: fetcher)
    return fetcher


def test_reader_parses_a_urlset(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = _patch_fetcher(monkeypatch, {SITEMAP: _urlset([URL_A, URL_B, URL_A])})

    inventory = cli_main._read_sitemap_inventory(SITEMAP, HOST)

    assert inventory.urls == [URL_A, URL_B]
    assert inventory.errors == []
    assert inventory.truncated is False
    assert fetcher.calls == [SITEMAP]
    assert fetcher.closed is True


def test_reader_keeps_the_indexnow_host_in_scope_from_a_cdn_sitemap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cdn = "https://cdn.example.net/sitemap.xml"
    child = "https://cdn.example.net/pages.xml"
    _patch_fetcher(monkeypatch, {cdn: _index([child]), child: _urlset([URL_A, URL_B])})

    inventory = cli_main._read_sitemap_inventory(cdn, HOST)

    assert inventory.urls == [URL_A, URL_B]
    assert "off_origin_loc" not in inventory.errors


def test_reader_bounds_a_sitemapindex_to_five_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = [f"https://example.com/{index}.xml" for index in range(7)]
    pages = {SITEMAP: _index(children)}
    for index, child in enumerate(children):
        pages[child] = _urlset([f"https://example.com/page-{index}"])
    fetcher = _patch_fetcher(monkeypatch, pages)

    inventory = cli_main._read_sitemap_inventory(SITEMAP, HOST)

    assert len(fetcher.calls) == 6, "one root plus at most five child sitemaps"
    assert inventory.urls == [
        "https://example.com/page-0",
        "https://example.com/page-1",
        "https://example.com/page-2",
        "https://example.com/page-3",
        "https://example.com/page-4",
    ]
    assert inventory.truncated is True
    assert "sitemap_docs_cap" in inventory.errors


def test_reader_applies_the_url_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "_INDEXNOW_URL_LIMIT", 3)
    urls = [f"https://example.com/page-{index}" for index in range(5)]
    _patch_fetcher(monkeypatch, {SITEMAP: _urlset(urls)})

    inventory = cli_main._read_sitemap_inventory(SITEMAP, HOST)

    assert inventory.urls == urls[:3]
    assert inventory.truncated is True
