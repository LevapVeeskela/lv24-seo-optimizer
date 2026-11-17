"""Offline tests for the read-only CWV depth commands and service methods.

Provider payloads are faked; the CLI tests replace one service method so the flag
plumbing and envelope are checked without a network call or a store.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main
from lv24_seo.core.config import (
    CrawlConfig,
    CruxConfig,
    ProjectConfig,
    ProvidersConfig,
    PsiConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.envelope import ok
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.base import ProviderResult
from lv24_seo.providers.crux.client import CRUX_HISTORY_GAPS
from lv24_seo.providers.psi.client import PSI_RUNTIME_ERROR, PsiAudits
from lv24_seo.services import SeoService

RUNNER = CliRunner()
URL = "https://example.com/page"
ORIGIN = "https://example.com"


def _config() -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example",
            base_url="https://example.com/",
            default_property="sc-domain:example.com",
        ),
        providers=ProvidersConfig(
            crux=CruxConfig(origin=ORIGIN),
            psi=PsiConfig(urls=[URL]),
        ),
        crawl=CrawlConfig(max_urls=10),
        storage=StorageConfig(data_dir="data"),
    )


def _secrets() -> SecretStore:
    return SecretStore(env={"LV24_SEO_CRUX_API_KEY": "key"})


class _FakePsiAudits:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def audits(
        self,
        url: str,
        *,
        strategy: str = "mobile",
        categories: Sequence[str] = ("performance", "seo"),
    ) -> PsiAudits:
        self.calls.append({"url": url, "strategy": strategy, "categories": list(categories)})
        return PsiAudits(
            source="psi",
            property_id=url,
            fetched_at=utc_now(),
            data_age_days=0,
            strategy=strategy,
            rows=[
                {
                    "id": "unused-javascript",
                    "title": "Reduce unused JavaScript",
                    "score": 0.4,
                    "display_value": "1.2 s",
                    "description": "Remove unused code.",
                    "details_type": "opportunity",
                    "overall_savings_ms": 1200.0,
                    "overall_savings_bytes": 50_000,
                    "items_count": 2,
                }
            ],
            categories=[{"category": "performance", "score": 0.5}],
            total=1,
            truncated=False,
            analysis_utc_timestamp="2026-11-17T10:00:00Z",
            final_url=url,
            limitations=(PSI_RUNTIME_ERROR,),
            warnings=("psi runtime error: ERRORED_DOCUMENT_REQUEST",),
        )


class _FakeCruxHistory:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def history(
        self,
        *,
        origin: str = "",
        url: str = "",
        form_factor: str | None = None,
        weeks: int = 25,
    ) -> ProviderResult[dict[str, Any]]:
        self.calls.append(
            {
                "origin": origin,
                "url": url,
                "form_factor": form_factor,
                "weeks": weeks,
            }
        )
        return ProviderResult(
            source="crux",
            property_id=url or origin,
            fetched_at=utc_now(),
            data_age_days=7,
            rows=[
                {
                    "first_date": "2026-11-03",
                    "last_date": "2026-11-09",
                    "device": "phone",
                    "lcp": 2400.0,
                    "cls": 0.08,
                }
            ],
            total=1,
            truncated=False,
            limitations=(CRUX_HISTORY_GAPS,),
        )


def test_cwv_audits_service_returns_bounded_rows_and_meta() -> None:
    psi = _FakePsiAudits()
    service = SeoService(_config(), secrets=_secrets(), clients={"psi": psi})

    response = service.cwv_audits(
        url=URL, strategy="desktop", categories=["performance", "best-practices"]
    )

    assert response["ok"] is True
    assert psi.calls == [
        {
            "url": URL,
            "strategy": "desktop",
            "categories": ["performance", "best-practices"],
        }
    ]
    assert response["data"]["url"] == URL
    assert response["data"]["strategy"] == "desktop"
    assert response["data"]["categories"] == [{"category": "performance", "score": 0.5}]
    assert response["data"]["audits"][0]["id"] == "unused-javascript"
    assert response["meta"]["source"] == "psi"
    assert response["meta"]["total"] == 1
    assert response["meta"]["truncated"] is False
    assert response["meta"]["rows"] == 1
    assert response["meta"]["categories"] == 1
    assert response["meta"]["analysis_utc_timestamp"] == "2026-11-17T10:00:00Z"
    assert response["meta"]["final_url"] == URL
    assert PSI_RUNTIME_ERROR in response["meta"]["limitations"]
    assert "psi runtime error: ERRORED_DOCUMENT_REQUEST" in response["warnings"]


def test_cwv_history_service_returns_periods_and_meta() -> None:
    crux = _FakeCruxHistory()
    service = SeoService(_config(), secrets=_secrets(), clients={"crux": crux})

    response = service.cwv_history(origin=ORIGIN, device="mobile", weeks=1)

    assert response["ok"] is True
    assert crux.calls == [{"origin": ORIGIN, "url": "", "form_factor": "mobile", "weeks": 1}]
    assert response["data"] == {
        "target": ORIGIN,
        "scope": "origin",
        "device": "phone",
        "weeks_requested": 1,
        "periods": [
            {
                "first_date": "2026-11-03",
                "last_date": "2026-11-09",
                "device": "phone",
                "lcp": 2400.0,
                "cls": 0.08,
            }
        ],
        "metrics": ["lcp", "cls"],
    }
    assert response["meta"]["data_age_days"] == 7
    assert response["meta"]["weeks_returned"] == 1
    assert response["meta"]["weeks_requested"] == 1
    assert CRUX_HISTORY_GAPS in response["meta"]["limitations"]


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
        'default_property = "sc-domain:example.com"\n',
        encoding="utf-8",
    )
    return path


def _invoke(*args: str) -> Any:
    return RUNNER.invoke(cli_main.app, list(args))


def _payload(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_cli_cwv_audits_forwards_flags(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_audits(
        self: Any,
        *,
        url: str,
        strategy: str = "mobile",
        categories: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        del self
        captured.update(url=url, strategy=strategy, categories=list(categories or []))
        return ok(
            {"url": url, "strategy": strategy, "categories": [], "audits": []},
            meta={
                "source": "psi",
                "strategy": strategy,
                "total": 0,
                "truncated": False,
                "limitations": [],
            },
        )

    monkeypatch.setattr(cli_main.SeoService, "cwv_audits", fake_audits)
    payload = _payload(
        _invoke(
            "cwv",
            "audits",
            "--url",
            URL,
            "--strategy",
            "desktop",
            "--category",
            "performance",
            "--category",
            "best-practices",
            "--config",
            str(workspace),
            "--json",
        )
    )

    assert captured == {
        "url": URL,
        "strategy": "desktop",
        "categories": ["performance", "best-practices"],
    }
    assert payload["data"]["audits"] == []


def test_cli_cwv_history_forwards_flags(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_history(
        self: Any,
        *,
        url: str = "",
        origin: str = "",
        device: str = "",
        weeks: int = 25,
    ) -> dict[str, Any]:
        del self
        captured.update(url=url, origin=origin, device=device, weeks=weeks)
        return ok(
            {
                "target": origin,
                "scope": "origin",
                "device": "phone",
                "weeks_requested": weeks,
                "periods": [],
                "metrics": [],
            },
            meta={
                "source": "crux",
                "weeks_requested": weeks,
                "weeks_returned": 0,
                "truncated": False,
                "limitations": [],
            },
        )

    monkeypatch.setattr(cli_main.SeoService, "cwv_history", fake_history)
    payload = _payload(
        _invoke(
            "cwv",
            "history",
            "--origin",
            ORIGIN,
            "--device",
            "phone",
            "--weeks",
            "5",
            "--config",
            str(workspace),
            "--json",
        )
    )

    assert captured == {"url": "", "origin": ORIGIN, "device": "phone", "weeks": 5}
    assert payload["data"]["weeks_requested"] == 5


def test_cli_cwv_audits_help_lists_the_documented_options() -> None:
    result = RUNNER.invoke(cli_main.app, ["cwv", "audits", "--help"], env={"COLUMNS": "160"})

    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    compact = re.sub(r"\s+", "", plain)
    for flag in ("--url", "--strategy", "--category", "--config", "--json"):
        assert flag in compact


def test_cli_cwv_history_help_lists_the_documented_options() -> None:
    result = RUNNER.invoke(cli_main.app, ["cwv", "history", "--help"], env={"COLUMNS": "160"})

    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    compact = re.sub(r"\s+", "", plain)
    for flag in ("--url", "--origin", "--device", "--weeks", "--config", "--json"):
        assert flag in compact
