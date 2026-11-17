"""Offline tests for the read-only Metrika SEO surfaces (organic pages, goals, dimensions)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lv24_seo.core.config import (
    CrawlConfig,
    ProjectConfig,
    ProvidersConfig,
    ReportConfig,
    SeoConfig,
    StorageConfig,
    YandexMetrikaConfig,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.providers.base import SAMPLED, ProviderResult
from lv24_seo.providers.yandex_metrika import ORGANIC_FILTER
from lv24_seo.services import SeoService

COUNTER = "111384866"


class FakeMetrika:
    def __init__(self, rows: Sequence[dict[str, Any]] | None = None) -> None:
        self.rows = list(
            rows
            if rows is not None
            else [
                {
                    "startURLPath": "/catalog",
                    "visits": 4.0,
                    "users": 4.0,
                    "bounceRate": 25.0,
                    "avgVisitDurationSeconds": 61.0,
                    "pageDepth": 2.0,
                    "pageviews": 8.0,
                }
            ]
        )
        self.calls: list[dict[str, Any]] = []
        self.bytime_calls: list[dict[str, Any]] = []
        self.data_age_days = 1

    def stat_data(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        self.calls.append(dict(kwargs))
        return ProviderResult(
            source="yandex_metrika",
            property_id=str(kwargs["counter"]),
            fetched_at=datetime.now(UTC),
            data_age_days=self.data_age_days,
            rows=list(self.rows),
            total=len(self.rows),
            limitations=(SAMPLED,),
            warnings=("sampled",),
            sample_share=0.5,
            sample_size=100,
            sample_space=200,
            data_lag=90,
        )

    def stat_bytime(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        self.bytime_calls.append(dict(kwargs))
        return ProviderResult(
            source="yandex_metrika",
            property_id=str(kwargs["counter"]),
            fetched_at=datetime.now(UTC),
            data_age_days=1,
            rows=[
                {"date": "2026-11-09", "visits": 4.0, "users": 4.0},
                {"date": "2026-11-10", "visits": 0.0, "users": 0.0},
            ],
            total=1,
            limitations=(SAMPLED,),
            warnings=("sampled",),
            sample_share=0.5,
        )


def _config(*, goals: Sequence[int] = (111, 222), counter: str = COUNTER) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example", base_url="https://example.com", default_property="sc-domain:example.com"
        ),
        providers=ProvidersConfig(
            yandex_metrika=YandexMetrikaConfig(counter=counter, goals=list(goals)),
        ),
        report=ReportConfig(locale="en", timezone="UTC"),
        crawl=CrawlConfig(),
        storage=StorageConfig(data_dir=Path("data")),
    )


def _service(*, goals: Sequence[int] = (111, 222)) -> tuple[SeoService, FakeMetrika]:
    client = FakeMetrika()
    service = SeoService(
        _config(goals=goals),
        secrets=SecretStore(env={"LV24_SEO_YANDEX_METRIKA_TOKEN": "metrika-token"}),
        clients={"yandex_metrika": client},
    )
    return service, client


def test_pages_are_organic_and_bounded_with_engagement_metrics() -> None:
    service, client = _service()

    payload = service.metrika_pages(days=7, lag_days=2, limit=25)

    assert payload["ok"] is True
    call = client.calls[0]
    assert call["counter"] == COUNTER
    assert call["filters"] == ORGANIC_FILTER
    assert call["dimensions"] == ("ym:s:startURLPath",)
    assert call["metrics"] == (
        "ym:s:visits",
        "ym:s:users",
        "ym:s:bounceRate",
        "ym:s:avgVisitDurationSeconds",
        "ym:s:pageDepth",
        "ym:s:pageviews",
    )
    assert call["sort"] == "-ym:s:visits"
    assert call["limit"] == 25
    assert call["max_rows"] == 25
    assert isinstance(call["date1"], date)
    assert isinstance(call["date2"], date)
    meta = payload["meta"]
    assert meta["organic_only"] is True
    assert meta["sampled"] is True
    assert meta["sample_share"] == 0.5
    assert meta["sample_size"] == 100
    assert meta["sample_space"] == 200
    assert meta["data_lag"] == 90
    assert SAMPLED in meta["limitations"]
    assert payload["warnings"] == ["sampled"]


def test_goals_use_config_ids_and_organic_filter() -> None:
    service, client = _service(goals=(111, 222))

    payload = service.metrika_goals(days=7, lag_days=2, dimension="page", limit=50)

    assert payload["ok"] is True
    call = client.calls[0]
    assert call["filters"] == ORGANIC_FILTER
    assert call["dimensions"] == ("ym:s:startURLPath",)
    assert call["metrics"] == [
        "ym:s:goal111reaches",
        "ym:s:goal111visits",
        "ym:s:goal111users",
        "ym:s:goal111conversionRate",
        "ym:s:goal222reaches",
        "ym:s:goal222visits",
        "ym:s:goal222users",
        "ym:s:goal222conversionRate",
    ]
    assert call["sort"] == "-ym:s:goal111reaches"
    assert call["limit"] == 50
    assert payload["data"]["goals"] == [111, 222]
    assert payload["meta"]["goals"] == [111, 222]


def test_goals_are_capped_at_the_twenty_metric_request_limit() -> None:
    service, client = _service(goals=(1, 2, 3, 4, 5, 6))

    payload = service.metrika_goals()

    call = client.calls[0]
    assert len(call["metrics"]) == 20
    assert payload["data"]["goals"] == [1, 2, 3, 4, 5]
    assert payload["data"]["goals_configured"] == [1, 2, 3, 4, 5, 6]
    assert payload["meta"]["truncated"] is True
    assert any("first 5 goal ids" in item for item in payload["meta"]["limitations"])


def test_goals_without_config_ids_warn_and_make_no_call() -> None:
    service, client = _service(goals=())

    payload = service.metrika_goals()

    assert payload["ok"] is True
    assert client.calls == []
    assert payload["data"]["rows"] == []
    assert payload["data"]["goals"] == []
    assert payload["meta"]["data_age_days"] is None
    assert "goals is empty" in payload["warnings"][0]
    assert "no goal ids configured" in payload["meta"]["limitations"][0]


def test_queries_document_sensitive_and_google_gaps() -> None:
    service, client = _service()

    payload = service.metrika_queries(days=7, lag_days=2, limit=10)

    call = client.calls[0]
    assert call["dimensions"] == ("ym:s:lastSearchPhrase", "ym:s:lastSearchEngineRoot")
    assert call["filters"] == ORGANIC_FILTER
    limitations = " ".join(payload["meta"]["limitations"])
    assert "Google withholds" in limitations
    assert "fewer than 10 users" in limitations


def test_devices_countries_and_engagement_use_the_shared_helper() -> None:
    service, client = _service()

    devices = service.metrika_devices(limit=10)
    countries = service.metrika_countries(limit=10)
    engagement = service.metrika_engagement()

    assert client.calls[0]["dimensions"] == ("ym:s:deviceCategory",)
    assert client.calls[1]["dimensions"] == ("ym:s:regionCountry",)
    assert client.calls[2]["dimensions"] == ()
    assert client.calls[2]["limit"] == 1
    assert client.calls[2]["max_rows"] == 1
    assert all(call["filters"] == ORGANIC_FILTER for call in client.calls)
    for payload in (devices, countries, engagement):
        assert payload["meta"]["organic_only"] is True
        assert payload["meta"]["sampled"] is True


def test_bytime_uses_the_bytime_endpoint_and_group() -> None:
    service, client = _service()

    payload = service.metrika_bytime(days=7, lag_days=2, group="week")

    call = client.bytime_calls[0]
    assert call["group"] == "week"
    assert call["filters"] == ORGANIC_FILTER
    assert call["metrics"] == ("ym:s:visits", "ym:s:users")
    assert payload["ok"] is True
    assert payload["meta"]["group"] == "week"
    assert payload["meta"]["sampled"] is True
    assert payload["data"]["rows"][0] == {"date": "2026-11-09", "visits": 4.0, "users": 4.0}


def test_missing_counter_is_a_config_error() -> None:
    service = SeoService(
        _config(counter=""),
        secrets=SecretStore(env={"LV24_SEO_YANDEX_METRIKA_TOKEN": "metrika-token"}),
        clients={"yandex_metrika": FakeMetrika()},
    )

    payload = service.metrika_pages()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"
