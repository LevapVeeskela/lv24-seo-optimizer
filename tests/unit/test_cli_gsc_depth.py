"""CLI tests for the GSC read depth: query options, inspection and sitemaps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main
from lv24_seo.core.envelope import ok

RUNNER = CliRunner()


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _json(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, getattr(result, "stdout", None) or result.output
    return json.loads(getattr(result, "stdout", None) or result.output)


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


class RecordingService:
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []
    response: ClassVar[dict[str, Any]] = ok({})

    def __init__(self, config: Any, **_: Any) -> None:
        self.config = config

    def gsc_query(self, **kwargs: Any) -> dict[str, Any]:
        type(self).calls.append(("gsc_query", kwargs))
        return type(self).response

    def gsc_inspect(self, url: str, language: str = "en-US") -> dict[str, Any]:
        type(self).calls.append(("gsc_inspect", {"url": url, "language": language}))
        return type(self).response

    def gsc_sitemaps(self, *, sitemap_index: str = "") -> dict[str, Any]:
        type(self).calls.append(("gsc_sitemaps", {"sitemap_index": sitemap_index}))
        return type(self).response

    def gsc_sitemap(self, *, path: str) -> dict[str, Any]:
        type(self).calls.append(("gsc_sitemap", {"path": path}))
        return type(self).response


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch) -> type[RecordingService]:
    RecordingService.calls = []
    RecordingService.response = ok(
        {"rows": [{"query": "coffee", "clicks": 2.0}]},
        meta={"source": "gsc", "rows": 1},
    )
    monkeypatch.setattr(cli_main, "SeoService", RecordingService)
    return RecordingService


def test_gsc_query_forwards_every_filter_option(
    service: type[RecordingService], workspace: Path
) -> None:
    payload = _json(
        _invoke(
            "gsc",
            "query",
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-16",
            "--type",
            "image",
            "--filter",
            "country:equals:BLR",
            "--filter-list",
            "device:MOBILE,TABLET",
            "--data-state",
            "all",
            "--aggregation",
            "bypage",
            "--search-appearance",
            "AMP_BLUE_LINK",
            "--branded",
            "non_branded",
            "--config",
            str(workspace),
            "--json",
        )
    )

    assert payload["ok"] is True
    name, kwargs = service.calls[-1]
    assert name == "gsc_query"
    assert kwargs["search_type"] == "image"
    assert kwargs["filters"] == [
        {"dimension": "country", "operator": "equals", "expression": "BLR"},
        {
            "dimension": "searchAppearance",
            "operator": "equals",
            "expression": "AMP_BLUE_LINK",
        },
        {
            "dimension": "brandedClassification",
            "operator": "equals",
            "expression": "NON_BRANDED",
        },
    ]
    assert kwargs["filter_lists"] == [{"dimension": "device", "expressions": ["MOBILE", "TABLET"]}]
    assert kwargs["data_state"] == "all"
    assert kwargs["aggregation"] == "bypage"


def test_gsc_query_rejects_a_malformed_filter(
    service: type[RecordingService], workspace: Path
) -> None:
    result = _invoke(
        "gsc",
        "query",
        "--start",
        "2026-11-01",
        "--end",
        "2026-11-16",
        "--filter",
        "country:BLR",
        "--config",
        str(workspace),
        "--json",
    )

    assert result.exit_code == 2
    assert json.loads(getattr(result, "stdout", None) or result.output)["error"]["code"] == (
        "INVALID_INPUT"
    )
    assert service.calls == []


def test_gsc_inspect_calls_the_service(service: type[RecordingService], workspace: Path) -> None:
    service.response = ok(
        {
            "url": "https://example.com/places",
            "language": "ru-RU",
            "inspection": {
                "url": "https://example.com/places",
                "verdict": "NEUTRAL",
                "coverage_state": "URL is unknown to Google",
                "rich_results": {"verdict": None, "detected_items": []},
            },
        },
        meta={"source": "gsc", "limitations": ["one_url_per_call"]},
    )

    payload = _json(
        _invoke(
            "gsc",
            "inspect",
            "--url",
            "https://example.com/places",
            "--language",
            "ru-RU",
            "--config",
            str(workspace),
            "--json",
        )
    )

    assert payload["data"]["inspection"]["verdict"] == "NEUTRAL"
    assert service.calls[-1] == (
        "gsc_inspect",
        {"url": "https://example.com/places", "language": "ru-RU"},
    )


def test_gsc_inspect_human_output_names_the_verdict(
    service: type[RecordingService], workspace: Path
) -> None:
    service.response = ok(
        {
            "url": "https://example.com/places",
            "language": "en-US",
            "inspection": {
                "url": "https://example.com/places",
                "verdict": "NEUTRAL",
                "coverage_state": "URL is unknown to Google",
                "rich_results": {"verdict": None, "detected_items": []},
            },
        },
        meta={"limitations": ["one_url_per_call"]},
    )

    result = _invoke(
        "gsc",
        "inspect",
        "--url",
        "https://example.com/places",
        "--config",
        str(workspace),
    )

    assert result.exit_code == 0
    assert "verdict: NEUTRAL" in result.output
    assert "URL is unknown to Google" in result.output


def test_gsc_sitemaps_and_sitemap_call_the_service(
    service: type[RecordingService], workspace: Path
) -> None:
    service.response = ok(
        {
            "rows": [
                {
                    "path": "https://example.com/sitemap-seo.xml",
                    "type": "sitemap",
                    "is_sitemaps_index": True,
                    "is_pending": False,
                    "errors": 0,
                    "warnings": 0,
                    "contents": [
                        {
                            "type": "sitemap",
                            "submitted": 3,
                            "indexed": 0,
                            "indexed_deprecated": True,
                        }
                    ],
                }
            ],
            "sitemap_index": {
                "path": "https://example.com/sitemap-seo.xml",
                "is_sitemaps_index": True,
                "contents": [
                    {
                        "type": "sitemap",
                        "submitted": 3,
                        "indexed": 0,
                        "indexed_deprecated": True,
                    }
                ],
            },
        },
        meta={"source": "gsc", "limitations": ["sitemap_indexed_count_deprecated"]},
    )

    list_payload = _json(
        _invoke(
            "gsc",
            "sitemaps",
            "--sitemap-index",
            "https://example.com/sitemap-seo.xml",
            "--config",
            str(workspace),
            "--json",
        )
    )
    get_payload = _json(
        _invoke(
            "gsc",
            "sitemap",
            "--path",
            "https://example.com/sitemap.xml",
            "--config",
            str(workspace),
            "--json",
        )
    )

    assert list_payload["data"]["sitemap_index"]["is_sitemaps_index"] is True
    assert get_payload["ok"] is True
    assert service.calls[-2] == (
        "gsc_sitemaps",
        {"sitemap_index": "https://example.com/sitemap-seo.xml"},
    )
    assert service.calls[-1] == (
        "gsc_sitemap",
        {"path": "https://example.com/sitemap.xml"},
    )
