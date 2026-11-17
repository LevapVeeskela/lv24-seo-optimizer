"""Offline tests for the read-only dashboard ``stats`` command.

Every provider is a fake returning ``ProviderResult`` values, so no network, clock or
credential is involved. The tests assert the contract in
``lv24-seo-optimizer-desktop/contracts/stats-v1.md``: service order, per-engine metrics,
previous windows only where comparable, coverage and isolated failures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lv24_seo.cli import main as cli_main
from lv24_seo.core.config import (
    BingConfig,
    CrawlConfig,
    CruxConfig,
    GscConfig,
    ProjectConfig,
    ProvidersConfig,
    PsiConfig,
    ReportConfig,
    SeoConfig,
    StorageConfig,
    YandexMetrikaConfig,
    YandexWebmasterConfig,
)
from lv24_seo.core.envelope import ok
from lv24_seo.core.errors import INVALID_INPUT, QUOTA_EXCEEDED, SeoError
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import compare_period, local_today, utc_now
from lv24_seo.providers.base import NO_QUERY_PAGE_JOIN, ProviderResult
from lv24_seo.services import SeoService

PROPERTY = "sc-domain:example.com"
HOST_ID = "https:example.com:443"
COUNTER = "12345"
BASE_URL = "https://example.com/"
ORDER = ["gsc", "yandex_webmaster", "yandex_metrika", "bing", "crux", "psi"]


def _result(
    source: str,
    rows: Sequence[dict[str, Any]],
    *,
    property_id: str = PROPERTY,
    data_age_days: int = 2,
    truncated: bool = False,
    warnings: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> ProviderResult[dict[str, Any]]:
    return ProviderResult(
        source=source,
        property_id=property_id,
        fetched_at=utc_now(),
        data_age_days=data_age_days,
        rows=list(rows),
        total=len(rows),
        truncated=truncated,
        limitations=tuple(limitations),
        warnings=tuple(warnings),
    )


def _config(
    *,
    gsc: bool = True,
    webmaster: bool = True,
    metrika: bool = True,
    bing: bool = True,
    crux: bool = True,
    psi: bool = True,
    psi_urls: Sequence[str] | None = None,
) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(name="Example", base_url=BASE_URL, default_property=PROPERTY),
        providers=ProvidersConfig(
            gsc=GscConfig(property=PROPERTY) if gsc else None,
            yandex_webmaster=YandexWebmasterConfig(host_id=HOST_ID) if webmaster else None,
            yandex_metrika=YandexMetrikaConfig(counter=COUNTER) if metrika else None,
            bing=BingConfig(site=BASE_URL) if bing else None,
            crux=CruxConfig(origin=BASE_URL) if crux else None,
            psi=PsiConfig(urls=list(psi_urls or [BASE_URL])) if psi else None,
        ),
        report=ReportConfig(locale="en", timezone="UTC"),
        crawl=CrawlConfig(),
        storage=StorageConfig(data_dir=Path("data")),
    )


def _secrets(
    *,
    gsc: bool = True,
    webmaster: bool = True,
    metrika: bool = True,
    bing: bool = True,
    crux: bool = True,
) -> SecretStore:
    env: dict[str, str] = {}
    if gsc:
        env["LV24_SEO_GSC_TOKEN"] = "gsc-token"
    if webmaster:
        env["LV24_SEO_YANDEX_WEBMASTER_TOKEN"] = "webmaster-token"
    if metrika:
        env["LV24_SEO_YANDEX_METRIKA_TOKEN"] = "metrika-token"
    if bing:
        env["LV24_SEO_BING_API_KEY"] = "bing-key"
    if crux:
        env["LV24_SEO_CRUX_API_KEY"] = "crux-key"
    return SecretStore(env=env)


class FakeGsc:
    def __init__(self, current: Sequence[dict[str, Any]], previous: Sequence[dict[str, Any]]):
        self.current = list(current)
        self.previous = list(previous)
        self.calls: list[dict[str, Any]] = []
        self.error: SeoError | None = None

    def search_analytics(
        self,
        *,
        start_date: date,
        end_date: date,
        dimensions: Sequence[str] = ("query",),
        search_type: str = "web",
        row_limit: int = 25_000,
        max_rows: int = 50_000,
        filters: Sequence[dict[str, Any]] | None = None,
    ) -> ProviderResult[dict[str, Any]]:
        self.calls.append(
            {
                "start_date": start_date,
                "end_date": end_date,
                "dimensions": tuple(dimensions),
                "search_type": search_type,
                "row_limit": row_limit,
                "max_rows": max_rows,
                "filters": filters,
            }
        )
        if self.error is not None:
            raise self.error
        rows = self.current if len(self.calls) == 1 else self.previous
        return _result("gsc", rows, data_age_days=2)


class FakeWebmaster:
    def __init__(self, current: Sequence[dict[str, Any]], previous: Sequence[dict[str, Any]]):
        self.current = list(current)
        self.previous = list(previous)
        self.calls: list[dict[str, Any]] = []
        self.error: SeoError | None = None

    def popular_queries(
        self,
        host_id: str,
        *,
        limit: int = 500,
        order_by: str = "TOTAL_SHOWS",
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ProviderResult[dict[str, Any]]:
        self.calls.append(
            {
                "host_id": host_id,
                "limit": limit,
                "order_by": order_by,
                "date_from": date_from,
                "date_to": date_to,
            }
        )
        if self.error is not None:
            raise self.error
        rows = self.current if len(self.calls) == 1 else self.previous
        return _result(
            "yandex_webmaster",
            rows,
            property_id=host_id,
            data_age_days=4,
            limitations=(NO_QUERY_PAGE_JOIN,),
        )


class FakeMetrika:
    def __init__(self, current: Sequence[dict[str, Any]], previous: Sequence[dict[str, Any]]):
        self.current = list(current)
        self.previous = list(previous)
        self.calls: list[dict[str, Any]] = []
        self.error: SeoError | None = None

    def stat_data(
        self,
        *,
        counter: int | str,
        metrics: Sequence[str],
        date1: date,
        date2: date,
        dimensions: Sequence[str] = (),
        filters: str | None = None,
        sort: str | None = None,
        accuracy: str = "medium",
        limit: int = 100,
    ) -> ProviderResult[dict[str, Any]]:
        self.calls.append(
            {
                "counter": counter,
                "metrics": tuple(metrics),
                "date1": date1,
                "date2": date2,
                "dimensions": tuple(dimensions),
                "filters": filters,
                "sort": sort,
                "limit": limit,
            }
        )
        if self.error is not None:
            raise self.error
        current = len(self.calls) % 2 == 1
        rows = self.current if current else self.previous
        return _result(
            "yandex_metrika",
            rows,
            property_id=str(counter),
            data_age_days=1 if current else 8,
        )


class FakeBing:
    def __init__(self) -> None:
        self.calls = 0
        self.error: SeoError | None = None
        self.warnings: tuple[str, ...] = ()

    def query_stats(self) -> ProviderResult[dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _result(
            "bing",
            [
                {"query": "seo", "clicks": 10, "impressions": 100, "position": 2.0},
                {"query": "audit", "clicks": 5, "impressions": 50, "position": 5.0},
            ],
            property_id=BASE_URL,
            data_age_days=0,
            warnings=self.warnings,
        )


class FakeCrux:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: SeoError | None = None

    def origin(self, origin: str) -> ProviderResult[dict[str, Any]]:
        self.calls.append(origin)
        if self.error is not None:
            raise self.error
        return _result(
            "crux",
            [
                {
                    "metric": "largest_contentful_paint",
                    "p75": 2100.0,
                    "good": 0.8,
                    "needs_improvement": 0.1,
                    "poor": 0.1,
                    "unit": "ms",
                },
                {
                    "metric": "cumulative_layout_shift",
                    "p75": 0.05,
                    "good": 0.9,
                    "needs_improvement": 0.05,
                    "poor": 0.05,
                    "unit": "score",
                },
            ],
            property_id=origin,
            data_age_days=2,
        )


class FakePsi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: SeoError | None = None

    def run(
        self,
        url: str,
        *,
        strategy: str = "mobile",
        categories: Sequence[str] = ("performance", "seo"),
    ) -> ProviderResult[dict[str, Any]]:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        performance = 0.9 if len(self.calls) == 1 else 0.4
        return _result(
            "psi",
            [
                {"category": "performance", "score": performance},
                {"category": "seo", "score": 0.8},
                {"category": "field", "metric": "LCP", "p75": 1800.0},
            ],
            property_id=url,
            data_age_days=0,
        )


def _clients(
    *,
    gsc: FakeGsc | None = None,
    webmaster: FakeWebmaster | None = None,
    metrika: FakeMetrika | None = None,
    bing: FakeBing | None = None,
    crux: FakeCrux | None = None,
    psi: FakePsi | None = None,
) -> dict[str, object]:
    return {
        "gsc": gsc
        or FakeGsc(
            [
                {"date": "2026-11-10", "clicks": 10.0, "impressions": 100.0, "position": 3.0},
                {"date": "2026-11-11", "clicks": 5.0, "impressions": 50.0, "position": 5.0},
            ],
            [
                {"date": "2026-11-03", "clicks": 8.0, "impressions": 150.0, "position": 6.0},
                {"date": "2026-11-04", "clicks": 2.0, "impressions": 50.0, "position": 7.0},
            ],
        ),
        "yandex_webmaster": webmaster
        or FakeWebmaster(
            [{"query": "seo", "shows": 300.0, "clicks": 30.0, "position": 4.0}],
            [{"query": "seo", "shows": 200.0, "clicks": 20.0, "position": 6.0}],
        ),
        "yandex_metrika": metrika
        or FakeMetrika(
            [{"visits": 120.0, "users": 100.0}],
            [{"visits": 90.0, "users": 80.0}],
        ),
        "bing": bing or FakeBing(),
        "crux": crux or FakeCrux(),
        "psi": psi or FakePsi(),
    }


def _service(
    *,
    config: SeoConfig | None = None,
    secrets: SecretStore | None = None,
    clients: Mapping[str, object] | None = None,
) -> SeoService:
    return SeoService(
        config or _config(),
        secrets=secrets or _secrets(),
        clients=dict(clients or _clients()),
    )


def _by_provider(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    data = payload["data"]
    assert isinstance(data, Mapping)
    services = data["services"]
    assert isinstance(services, list)
    return {str(service["provider"]): service for service in services}


def test_stats_happy_path_all_six_providers() -> None:
    gsc = FakeGsc(
        [
            {"date": "2026-11-10", "clicks": 10.0, "impressions": 100.0, "position": 3.0},
            {"date": "2026-11-11", "clicks": 5.0, "impressions": 50.0, "position": 5.0},
        ],
        [
            {"date": "2026-11-03", "clicks": 8.0, "impressions": 150.0, "position": 6.0},
            {"date": "2026-11-04", "clicks": 2.0, "impressions": 50.0, "position": 7.0},
        ],
    )
    webmaster = FakeWebmaster(
        [{"query": "seo", "shows": 300.0, "clicks": 30.0, "position": 4.0}],
        [{"query": "seo", "shows": 200.0, "clicks": 20.0, "position": 6.0}],
    )
    metrika = FakeMetrika(
        [{"visits": 120.0, "users": 100.0}],
        [{"visits": 90.0, "users": 80.0}],
    )
    payload = _service(clients=_clients(gsc=gsc, webmaster=webmaster, metrika=metrika)).stats(
        days=7, lag_days=2
    )

    assert payload["ok"] is True
    data = payload["data"]
    assert [service["provider"] for service in data["services"]] == ORDER
    assert all(service["status"] == "ok" for service in data["services"])
    assert all(service["configured"] and service["ready"] for service in data["services"])

    today = local_today("UTC")
    (start, end), (previous_start, previous_end) = compare_period(7, end=today, lag_days=2)
    assert data["period"] == {"start": start.isoformat(), "end": end.isoformat()}
    assert data["previous_period"] == {
        "start": previous_start.isoformat(),
        "end": previous_end.isoformat(),
    }
    assert data["generated_at"].endswith("+00:00")

    services = _by_provider(payload)
    gsc_entry = services["gsc"]
    assert gsc_entry["rows"] == 2
    assert gsc_entry["metrics"] == {
        "clicks": 15.0,
        "impressions": 150.0,
        "ctr": pytest.approx(0.1),
        "position": pytest.approx(550.0 / 150.0),
    }
    assert gsc_entry["previous"] == {
        "clicks": 10.0,
        "impressions": 200.0,
        "ctr": pytest.approx(0.05),
        "position": pytest.approx(1250.0 / 200.0),
    }
    assert gsc_entry["data_age_days"] == 2
    assert gsc_entry["error"] is None
    assert [call["dimensions"] for call in gsc.calls] == [("date",), ("date",)]
    assert [call["start_date"] for call in gsc.calls] == [start, previous_start]
    assert [call["end_date"] for call in gsc.calls] == [end, previous_end]

    yandex_entry = services["yandex_webmaster"]
    assert yandex_entry["rows"] == 1
    assert yandex_entry["metrics"] == {
        "shows": 300.0,
        "clicks": 30.0,
        "position": 4.0,
    }
    assert yandex_entry["previous"] == {
        "shows": 200.0,
        "clicks": 20.0,
        "position": 6.0,
    }
    assert yandex_entry["limitations"] == [NO_QUERY_PAGE_JOIN]
    assert yandex_entry["data_age_days"] == 4
    assert [call["date_from"] for call in webmaster.calls] == [start, previous_start]
    assert [call["date_to"] for call in webmaster.calls] == [end, previous_end]

    metrika_entry = services["yandex_metrika"]
    assert metrika_entry["rows"] == 1
    assert metrika_entry["metrics"] == {
        "organic_visits": 120.0,
        "organic_users": 100.0,
        "all_visits": 120.0,
        "all_users": 100.0,
    }
    assert metrika_entry["previous"] == {
        "organic_visits": 90.0,
        "organic_users": 80.0,
        "all_visits": 90.0,
        "all_users": 80.0,
    }
    # The current window is one day old; the previous window's 8-day age must not win.
    assert metrika_entry["data_age_days"] == 1
    assert [call["filters"] for call in metrika.calls] == [
        "ym:s:lastTrafficSource=='organic'",
        "ym:s:lastTrafficSource=='organic'",
        None,
        None,
    ]

    bing_entry = services["bing"]
    assert bing_entry["rows"] == 2
    assert bing_entry["metrics"] == {
        "clicks": 15.0,
        "impressions": 150.0,
        "position": pytest.approx(450.0 / 150.0),
    }
    assert bing_entry["previous"] is None

    crux_entry = services["crux"]
    assert crux_entry["rows"] == 2
    assert crux_entry["metrics"] == {
        "largest_contentful_paint": 2100.0,
        "cumulative_layout_shift": 0.05,
    }
    assert crux_entry["units"] == {
        "largest_contentful_paint": "ms",
        "cumulative_layout_shift": "score",
    }
    assert crux_entry["previous"] is None

    psi_entry = services["psi"]
    assert psi_entry["rows"] == 3
    assert psi_entry["metrics"] == {"performance": 0.9, "seo": 0.8, "LCP": 1800.0}
    assert psi_entry["previous"] is None

    assert data["coverage"] == {
        "configured": 6,
        "returned": 6,
        "failed": 0,
        "missing": 0,
        "complete": True,
    }
    assert NO_QUERY_PAGE_JOIN in data["limitations"]
    assert any("organic_*" in item for item in data["limitations"])
    assert payload["meta"]["complete"] is True
    assert payload["meta"]["truncated"] is False
    assert payload["meta"]["days"] == 7
    assert payload["meta"]["lag_days"] == 2


def test_stats_missing_token_and_keyless_psi() -> None:
    clients = _clients()
    payload = _service(
        secrets=_secrets(gsc=True, webmaster=False, metrika=False, bing=False, crux=False),
        clients=clients,
    ).stats()
    services = _by_provider(payload)

    assert services["gsc"]["status"] == "ok"
    assert services["psi"]["status"] == "ok"
    assert services["psi"]["ready"] is True  # PSI works keyless
    for provider in ("yandex_webmaster", "yandex_metrika", "bing", "crux"):
        assert services[provider]["status"] == "missing_token"
        assert services[provider]["ready"] is False
        assert services[provider]["metrics"] == {}
        assert services[provider]["rows"] == 0
        assert services[provider]["error"] is None

    assert clients["yandex_webmaster"].calls == []  # type: ignore[attr-defined]
    assert clients["yandex_metrika"].calls == []  # type: ignore[attr-defined]
    assert clients["bing"].calls == 0  # type: ignore[attr-defined]
    assert clients["crux"].calls == []  # type: ignore[attr-defined]

    assert payload["data"]["coverage"] == {
        "configured": 6,
        "returned": 2,
        "failed": 0,
        "missing": 4,
        "complete": False,
    }


def test_stats_not_configured_is_excluded_from_coverage() -> None:
    payload = _service(
        config=_config(gsc=True, webmaster=False, metrika=False, bing=False, crux=False, psi=False)
    ).stats()
    services = _by_provider(payload)

    assert services["gsc"]["status"] == "ok"
    for provider in ORDER[1:]:
        assert services[provider]["status"] == "not_configured"
        assert services[provider]["configured"] is False
        assert services[provider]["ready"] is False

    assert payload["data"]["coverage"] == {
        "configured": 1,
        "returned": 1,
        "failed": 0,
        "missing": 0,
        "complete": True,
    }


def test_stats_no_configured_provider_is_incomplete() -> None:
    payload = _service(
        config=_config(
            gsc=False,
            webmaster=False,
            metrika=False,
            bing=False,
            crux=False,
            psi=False,
        )
    ).stats()

    assert payload["ok"] is True
    assert all(service["status"] == "not_configured" for service in payload["data"]["services"])
    assert payload["data"]["coverage"] == {
        "configured": 0,
        "returned": 0,
        "failed": 0,
        "missing": 0,
        "complete": False,
    }


def test_stats_no_data_keeps_metrics_empty() -> None:
    payload = _service(clients=_clients(gsc=FakeGsc([], []))).stats()
    gsc_entry = _by_provider(payload)["gsc"]

    assert gsc_entry["status"] == "no_data"
    assert gsc_entry["metrics"] == {}
    assert gsc_entry["previous"] is None
    assert gsc_entry["rows"] == 0
    assert payload["data"]["coverage"]["complete"] is False


def test_stats_aggregates_provider_warnings() -> None:
    bing = FakeBing()
    bing.warnings = ("sampled",)
    payload = _service(clients=_clients(bing=bing)).stats()

    assert _by_provider(payload)["bing"]["warnings"] == ["sampled"]
    assert payload["data"]["warnings"] == ["sampled"]
    assert payload["warnings"] == ["sampled"]


def test_stats_provider_error_is_isolated() -> None:
    gsc = FakeGsc([], [])
    gsc.error = SeoError(code=QUOTA_EXCEEDED, message="quota exceeded for today")
    payload = _service(clients=_clients(gsc=gsc)).stats()
    services = _by_provider(payload)

    assert payload["ok"] is True
    assert services["gsc"]["status"] == "error"
    assert services["gsc"]["error"] == {
        "code": QUOTA_EXCEEDED,
        "message": "quota exceeded for today",
    }
    assert services["gsc"]["metrics"] == {}
    assert services["gsc"]["rows"] == 0
    assert services["bing"]["status"] == "ok"
    assert payload["data"]["coverage"] == {
        "configured": 6,
        "returned": 5,
        "failed": 1,
        "missing": 0,
        "complete": False,
    }


def test_stats_unexpected_provider_exception_is_isolated() -> None:
    crux = FakeCrux()

    def explode(_origin: str) -> ProviderResult[dict[str, Any]]:
        raise RuntimeError("boom")

    crux.origin = explode  # type: ignore[method-assign]
    payload = _service(clients=_clients(crux=crux)).stats()
    crux_entry = _by_provider(payload)["crux"]

    assert payload["ok"] is True
    assert crux_entry["status"] == "error"
    assert crux_entry["error"] == {
        "code": "INTERNAL",
        "message": "unexpected error while collecting crux",
    }
    assert _by_provider(payload)["psi"]["status"] == "ok"


def test_stats_psi_multiple_urls_uses_first_for_metrics() -> None:
    psi = FakePsi()
    payload = _service(
        config=_config(psi_urls=[BASE_URL, f"{BASE_URL}page"]),
        clients=_clients(psi=psi),
    ).stats()
    psi_entry = _by_provider(payload)["psi"]

    assert psi.calls == [BASE_URL, f"{BASE_URL}page"]
    assert psi_entry["metrics"] == {"performance": 0.9, "seo": 0.8, "LCP": 1800.0}
    assert psi_entry["rows"] == 6
    assert "lab data, first URL only" in psi_entry["limitations"]
    assert psi_entry["truncated"] is False
    assert payload["data"]["coverage"]["complete"] is True


def test_stats_psi_url_cap_marks_truncated() -> None:
    urls = [f"{BASE_URL}page-{index}" for index in range(6)]
    psi = FakePsi()
    payload = _service(config=_config(psi_urls=urls), clients=_clients(psi=psi)).stats()
    psi_entry = _by_provider(payload)["psi"]

    assert len(psi.calls) == 5
    assert psi_entry["truncated"] is True
    assert "lab data, first URL only" in psi_entry["limitations"]
    assert any("first 5" in item for item in psi_entry["limitations"])
    assert payload["data"]["coverage"]["complete"] is False


def test_stats_is_read_only_without_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    service = SeoService(
        _config(),
        secrets=_secrets(),
        clients=_clients(),
    )

    payload = service.stats()

    assert payload["ok"] is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("days", "lag_days"),
    [(0, 2), (-1, 2), (7, -1), (True, 2), (1_000_000, 2), (3661, 2)],
)
def test_stats_rejects_invalid_windows(days: int, lag_days: int) -> None:
    payload = _service().stats(days=days, lag_days=lag_days)

    assert payload["ok"] is False
    assert payload["error"]["code"] == INVALID_INPUT


def test_stats_cli_json_uses_service_without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    clients = _clients()
    captured: dict[str, Any] = {}

    def fake_load_config(_path: Path | None) -> SeoConfig:
        return config

    def fake_service(service_config: SeoConfig, *, store: Any = None) -> SeoService:
        captured["store"] = store
        return SeoService(service_config, secrets=_secrets(), clients=clients)

    monkeypatch.setattr(cli_main, "load_config", fake_load_config)
    monkeypatch.setattr(cli_main, "SeoService", fake_service)

    result = CliRunner().invoke(cli_main.app, ["stats", "--days", "3", "--lag-days", "1", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["meta"]["days"] == 3
    assert payload["meta"]["lag_days"] == 1
    assert [service["provider"] for service in payload["data"]["services"]] == ORDER
    assert captured["store"] is None


def test_stats_cli_human_output() -> None:
    payload = ok(
        {
            "period": {"start": "2026-11-07", "end": "2026-11-13"},
            "previous_period": {"start": "2026-10-31", "end": "2026-11-06"},
            "generated_at": "2026-11-14T12:00:00+00:00",
            "services": [
                {
                    "provider": "gsc",
                    "label": "Google Search Console",
                    "configured": True,
                    "ready": True,
                    "status": "ok",
                    "metrics": {
                        "clicks": 1284.0,
                        "impressions": 48213.0,
                        "ctr": 0.0266,
                        "position": 8.4,
                    },
                    "previous": None,
                    "rows": 7,
                    "data_age_days": 2,
                    "truncated": False,
                    "limitations": [],
                    "warnings": [],
                    "error": None,
                }
            ],
            "coverage": {
                "configured": 1,
                "returned": 1,
                "failed": 0,
                "missing": 0,
                "complete": True,
            },
            "limitations": [],
            "warnings": [],
        }
    )

    lines = cli_main._stats_lines(payload)
    text = "\n".join(lines)

    assert "period: 2026-11-07..2026-11-13" in text
    assert "coverage: 1/1 ok" in text
    assert "gsc" in text
    assert "ctr=2.66%" in text
    assert "\x1b[" not in text
