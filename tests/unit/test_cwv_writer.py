"""Offline tests for the CrUX/PSI URL+device CWV writer and the weekly read-back.

Provider calls are faked or served by ``httpx.MockTransport``; storage is a real
temporary SQLite database. Nothing here touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lv24_seo.core.config import (
    CrawlConfig,
    CruxConfig,
    ProjectConfig,
    ProvidersConfig,
    PsiConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import local_today, utc_now
from lv24_seo.providers.base import LOW_TRAFFIC, ProviderResult
from lv24_seo.providers.crux import CruxClient
from lv24_seo.services import SeoService
from lv24_seo.store import Store

PROPERTY = "sc-domain:example.com"
URL = "https://example.com/blog/42"

Handler = Callable[[httpx.Request], httpx.Response]

CRUX_RECORD = {
    "record": {
        "key": {"url": URL},
        "metrics": {
            "largest_contentful_paint": {"percentiles": {"p75": 2500.0}},
            "interaction_to_next_paint": {"percentiles": {"p75": 180.0}},
            "cumulative_layout_shift": {"percentiles": {"p75": 0.1}},
            "first_contentful_paint": {"percentiles": {"p75": 1700.0}},
            "experimental_time_to_first_byte": {"percentiles": {"p75": 600.0}},
            "first_input_delay": {"percentiles": {"p75": 90.0}},
        },
    }
}


def _http(handler: Handler) -> HttpClient:
    return HttpClient(
        transport=httpx.MockTransport(handler),
        max_retries=0,
        sleep=lambda _seconds: None,
    )


def _config(*, crux: bool = True, psi: bool = False) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example", base_url="https://example.com/", default_property=PROPERTY
        ),
        providers=ProvidersConfig(
            crux=CruxConfig(origin="https://example.com") if crux else None,
            psi=PsiConfig(urls=[URL]) if psi else None,
        ),
        crawl=CrawlConfig(max_urls=10),
        storage=StorageConfig(data_dir="data"),
    )


def _secrets() -> SecretStore:
    return SecretStore(env={"LV24_SEO_CRUX_API_KEY": "key"})


class _NoSecrets(SecretStore):
    """A store with no secret at all: keyring/file backends must not make tests flaky."""

    def __init__(self) -> None:
        super().__init__(env={})

    def get(self, name: str) -> str | None:
        del name
        return None


def _result(
    source: str,
    rows: list[dict[str, Any]],
    *,
    data_age_days: int = 0,
    limitations: tuple[str, ...] = (),
) -> ProviderResult[dict[str, Any]]:
    return ProviderResult(
        source=source,
        property_id=URL,
        fetched_at=utc_now(),
        data_age_days=data_age_days,
        rows=rows,
        total=len(rows),
        limitations=limitations,
    )


class _FakeCrux:
    def __init__(self) -> None:
        self.url_calls: list[tuple[str, str | None]] = []
        self.origin_calls = 0

    def url(self, url: str, form_factor: str | None = None) -> ProviderResult[dict[str, Any]]:
        self.url_calls.append((url, form_factor))
        rows = [
            {
                "metric": "largest_contentful_paint",
                "p75": 2500.0,
                "good": 0.8,
                "needs_improvement": 0.1,
                "poor": 0.1,
                "unit": "ms",
                "url": url,
                "device": form_factor,
            },
            {
                "metric": "cumulative_layout_shift",
                "p75": 0.1,
                "good": 0.9,
                "needs_improvement": 0.05,
                "poor": 0.05,
                "unit": "score",
                "url": url,
                "device": form_factor,
            },
        ]
        return _result("crux", rows, data_age_days=2)

    def origin(self, _origin: str) -> ProviderResult[dict[str, Any]]:
        self.origin_calls += 1
        return _result("crux", [], data_age_days=2, limitations=(LOW_TRAFFIC,))


class _FakePsi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run(
        self, url: str, *, strategy: str = "mobile", categories: tuple[str, ...] = ()
    ) -> ProviderResult[dict[str, Any]]:
        del categories
        self.calls.append((url, strategy))
        return _result(
            "psi",
            [
                {"category": "performance", "score": 0.9},
                {"category": "field", "metric": "LCP", "p75": 3000.0},
            ],
        )


# --- CrUX client: formFactor ---------------------------------------------------------------


def test_crux_url_form_factor_is_sent_and_rows_carry_url_and_device() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CRUX_RECORD)

    with _http(handler) as http:
        result = CruxClient(api_key="secret-key", http=http).url(URL, form_factor="mobile")

    assert json.loads(seen[0].content) == {"url": URL, "formFactor": "PHONE"}
    assert {row["device"] for row in result.rows} == {"phone"}
    assert {row["url"] for row in result.rows} == {URL}
    by_metric = {row["metric"]: row for row in result.rows}
    assert by_metric["largest_contentful_paint"]["p75"] == 2500.0
    assert by_metric["largest_contentful_paint"]["unit"] == "ms"


@pytest.mark.parametrize(
    ("alias", "enum"),
    [
        ("phone", "PHONE"),
        ("mobile", "PHONE"),
        ("smartphone", "PHONE"),
        ("desktop", "DESKTOP"),
        ("tablet", "TABLET"),
        ("TABLET", "TABLET"),
    ],
)
def test_crux_device_aliases(alias: str, enum: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CRUX_RECORD)

    with _http(handler) as http:
        CruxClient(api_key="secret-key", http=http).url(URL, form_factor=alias)

    assert json.loads(seen[0].content)["formFactor"] == enum


def test_crux_without_form_factor_keeps_the_historical_body_and_rows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CRUX_RECORD)

    with _http(handler) as http:
        result = CruxClient(api_key="secret-key", http=http).url(URL)

    assert json.loads(seen[0].content) == {"url": URL}
    assert all("device" not in row and "url" not in row for row in result.rows)


def test_crux_unknown_form_factor_is_invalid_input_without_a_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=CRUX_RECORD)

    with _http(handler) as http, pytest.raises(SeoError) as excinfo:
        CruxClient(api_key="secret-key", http=http).url(URL, form_factor="watch")

    assert excinfo.value.code == INVALID_INPUT
    assert "phone, desktop or tablet" in (excinfo.value.next_action or "")
    assert calls == 0


# --- service: persistence ------------------------------------------------------------------


def test_cwv_collect_persists_normalized_crux_field_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(
            _config(), store=store, secrets=_secrets(), clients={"crux": _FakeCrux()}
        )
        response = service.cwv_collect(urls=[URL], device="phone", source="crux", limit=5)
        rows = store.get_metric_daily(PROPERTY, source="crux")

    assert response["ok"] is True
    assert response["meta"]["written"] == 2
    assert response["meta"]["truncated"] is False
    assert response["meta"]["data_age_days"] == 2
    assert response["meta"]["total"] == 1
    assert {row["metric"] for row in rows} == {"LCP", "CLS"}
    assert {row["scope"] for row in rows} == {"phone"}
    assert {row["key"] for row in rows} == {URL}
    assert {row["accuracy"] for row in rows} == {"crux_field_28d"}
    assert {row["date"] for row in rows} == {local_today("Europe/Minsk").isoformat()}
    by_metric = {row["metric"]: row["value"] for row in rows}
    assert by_metric == {"LCP": 2500.0, "CLS": 0.1}


def test_cwv_collect_truncates_urls_and_reports_honestly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    crux = _FakeCrux()
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(_config(), store=store, secrets=_secrets(), clients={"crux": crux})
        response = service.cwv_collect(
            urls=[URL, "https://example.com/a", "https://example.com/b"],
            device="phone",
            source="crux",
            limit=2,
        )

    assert response["ok"] is True
    assert response["meta"]["urls_total"] == 3
    assert response["meta"]["urls_selected"] == 2
    assert response["meta"]["total"] == 3
    assert response["meta"]["processed"] == 2
    assert response["meta"]["pairs_total"] == 3
    assert response["meta"]["pairs_processed"] == 2
    assert response["meta"]["truncated"] is True
    assert "cwv_url_cap" in response["meta"]["limitations"]
    assert response["data"]["skipped_urls"] == ["https://example.com/b"]
    assert [call[0] for call in crux.url_calls] == [URL, "https://example.com/a"]


def test_cwv_collect_reports_low_traffic_without_writing_zeroes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _EmptyCrux:
        def url(self, url: str, form_factor: str | None = None) -> ProviderResult[dict[str, Any]]:
            del url, form_factor
            return _result("crux", [], data_age_days=2, limitations=(LOW_TRAFFIC,))

    with Store(tmp_path / "seo.db") as store:
        service = SeoService(
            _config(), store=store, secrets=_secrets(), clients={"crux": _EmptyCrux()}
        )
        response = service.cwv_collect(urls=[URL], device="phone", source="crux", limit=5)
        rows = store.get_metric_daily(PROPERTY, source="crux")

    assert response["ok"] is True
    assert response["data"]["rows"] == []
    assert response["meta"]["written"] == 0
    assert "low_traffic" in response["meta"]["limitations"]
    assert "cwv_no_field_rows" in response["meta"]["limitations"]
    assert rows == []


def test_cwv_collect_never_persists_psi_lab_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    psi = _FakePsi()
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(
            _config(psi=True), store=store, secrets=_secrets(), clients={"psi": psi}
        )
        response = service.cwv_collect(urls=[URL], device="phone", source="psi", limit=5)
        rows = store.get_metric_daily(PROPERTY, source="psi")

    assert response["ok"] is True
    assert rows == []
    assert response["data"]["written"] == {"crux": 0, "psi": 0}
    assert response["data"]["lab"]
    assert {row["kind"] for row in response["data"]["lab"]} == {"lab"}
    assert "cwv_lab_ignored" in response["meta"]["limitations"]
    assert psi.calls == [(URL, "mobile")]


def test_cwv_collect_skips_psi_tablet_instead_of_mapping_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    psi = _FakePsi()
    crux = _FakeCrux()
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(
            _config(psi=True),
            store=store,
            secrets=_secrets(),
            clients={"crux": crux, "psi": psi},
        )
        response = service.cwv_collect(urls=[URL], device="tablet", source="both", limit=5)

    assert response["ok"] is True
    assert psi.calls == []
    assert crux.url_calls == [(URL, "tablet")]
    assert "psi_tablet_unsupported" in response["meta"]["limitations"]
    assert "cwv_lab_ignored" in response["meta"]["limitations"]
    assert response["data"]["lab"] == []


def test_cwv_collect_without_crux_key_is_auth_required(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(_config(), store=store, secrets=_NoSecrets())
        response = service.cwv_collect(urls=[URL], device="phone", source="crux", limit=5)

    assert response["ok"] is False
    assert response["error"]["code"] == AUTH_REQUIRED
    assert "crux" in (response["error"].get("next_action") or "")


def test_cwv_collect_rejects_unknown_device_and_source(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(_config(), store=store, secrets=_secrets())
        bad_device = service.cwv_collect(urls=[URL], device="watch", source="crux", limit=5)
        bad_source = service.cwv_collect(urls=[URL], device="phone", source="lighthouse", limit=5)

    assert bad_device["error"]["code"] == INVALID_INPUT
    assert bad_source["error"]["code"] == INVALID_INPUT


# --- weekly read-back ----------------------------------------------------------------------


def test_mcp_weekly_tool_reads_rows_written_by_cwv_collect(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from lv24_seo.mcp.server import create_server

    crux = _FakeCrux()
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(_config(), store=store, secrets=_secrets(), clients={"crux": crux})
        service.cwv_collect(urls=[URL], device="phone", source="crux", limit=5)
        server = create_server(service)
        result = asyncio.run(server.call_tool("seo_weekly_report", {"days": 7, "lag_days": 2}))

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    sections = {section["key"]: section for section in payload["data"]["sections"]}
    assert "cwv" in sections
    assert sections["cwv"]["rows"][0]["template"] == "/blog/{id}"
    assert sections["cwv"]["rows"][0]["device"] == "phone"


def test_weekly_report_reads_rows_written_by_cwv_collect(tmp_path) -> None:  # type: ignore[no-untyped-def]
    crux = _FakeCrux()
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(_config(), store=store, secrets=_secrets(), clients={"crux": crux})
        collected = service.cwv_collect(urls=[URL], device="phone", source="crux", limit=5)
        response = service.weekly_report(days=7, lag_days=2, send=False)

    assert collected["meta"]["written"] == 2
    assert response["ok"] is True
    sections = {section["key"]: section for section in response["data"]["sections"]}
    assert "cwv" in sections
    templates = sections["cwv"]["rows"]
    assert len(templates) == 1
    assert templates[0]["template"] == "/blog/{id}"
    assert templates[0]["device"] == "phone"
    metrics = {metric["metric"]: metric for metric in templates[0]["metrics"]}
    assert metrics["lcp"]["p75"] == 2500.0
    assert metrics["lcp"]["unit"] == "ms"
    assert metrics["cls"]["p75"] == 0.1
    assert metrics["cls"]["unit"] == "score"
    assert "cwv_p75_worst_page" in response["data"]["limitations"]
