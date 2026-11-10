"""Offline contract tests for the integration service layer.

Every provider is replaced by a tiny object returning ``ProviderResult`` values, storage is a
real SQLite database in ``tmp_path``, and both the report builder and delivery seams are
monkeypatched. No network, clock-sensitive assertions or real credentials are involved.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from lv24_seo import services
from lv24_seo.core.config import (
    CrawlConfig,
    GscConfig,
    ProjectConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
    YandexMetrikaConfig,
    YandexWebmasterConfig,
)
from lv24_seo.core.errors import (
    AUTH_REQUIRED,
    INTERNAL,
    INVALID_INPUT,
    QUOTA_EXCEEDED,
    SeoError,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.crawl.fetcher import RobotsPolicy
from lv24_seo.crawl.models import FetchResult
from lv24_seo.providers.base import NO_QUERY_PAGE_JOIN, ProviderResult
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.services import SeoService
from lv24_seo.store import Store

PROPERTY = "sc-domain:example.com"
HOST_ID = "https:example.com:443"
COUNTER = "12345"
BASE_URL = "https://example.com/"

GSC_CURRENT = [
    {"query": "seo", "clicks": 10.0, "impressions": 100.0, "ctr": 0.1, "position": 3.0},
    {"query": "audit", "clicks": 5.0, "impressions": 50.0, "ctr": 0.1, "position": 5.0},
]
GSC_PREVIOUS = [
    {"query": "seo", "clicks": 8.0, "impressions": 150.0, "ctr": 0.053, "position": 6.0},
    {"query": "audit", "clicks": 2.0, "impressions": 50.0, "ctr": 0.04, "position": 7.0},
]


def _config(
    *,
    gsc_property: str = PROPERTY,
    host_id: str = HOST_ID,
    counter: str = COUNTER,
    base_url: str = BASE_URL,
    data_dir: Path | None = None,
) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example",
            base_url=base_url,
            default_property=PROPERTY,
        ),
        providers=ProvidersConfig(
            gsc=GscConfig(property=gsc_property) if gsc_property else None,
            yandex_webmaster=(YandexWebmasterConfig(host_id=host_id) if host_id else None),
            yandex_metrika=(YandexMetrikaConfig(counter=counter) if counter else None),
        ),
        crawl=CrawlConfig(max_urls=50),
        storage=StorageConfig(data_dir=Path(data_dir or "data")),
    )


def _secrets(
    *,
    gsc: str | None = None,
    webmaster: str | None = None,
    metrika: str | None = None,
) -> SecretStore:
    env: dict[str, str] = {}
    if gsc is not None:
        env["LV24_SEO_GSC_TOKEN"] = gsc
    if webmaster is not None:
        env["LV24_SEO_YANDEX_WEBMASTER_TOKEN"] = webmaster
    if metrika is not None:
        env["LV24_SEO_YANDEX_METRIKA_TOKEN"] = metrika
    return SecretStore(env=env)


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


class FakeGscClient:
    """Return one canned response per call, cycling to the last response."""

    def __init__(self, responses: Sequence[Sequence[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

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
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return _result("gsc", self.responses[index], property_id=PROPERTY)


class FakeWebmasterClient:
    def __init__(self) -> None:
        self.query_calls: list[dict[str, Any]] = []

    def popular_queries(
        self,
        host_id: str,
        *,
        limit: int = 500,
        order_by: str = "TOTAL_SHOWS",
    ) -> ProviderResult[dict[str, Any]]:
        self.query_calls.append({"host_id": host_id, "limit": limit, "order_by": order_by})
        return _result(
            "yandex_webmaster",
            [{"query": "seo", "shows": 300.0, "clicks": 30.0, "position": 4.0}],
            property_id=host_id,
            data_age_days=4,
            limitations=(NO_QUERY_PAGE_JOIN,),
        )

    def summary(self, host_id: str) -> ProviderResult[dict[str, Any]]:
        return _result(
            "yandex_webmaster",
            [{"sqi": 12, "pages_in_search": 120}],
            property_id=host_id,
        )

    def indexing_history(self, host_id: str) -> ProviderResult[dict[str, Any]]:
        return _result(
            "yandex_webmaster",
            [{"date": "2026-11-08", "http_2xx": 100}],
            property_id=host_id,
        )

    def search_urls_events(self, host_id: str) -> ProviderResult[dict[str, Any]]:
        return _result(
            "yandex_webmaster",
            [{"date": "2026-11-08", "event": "entered"}],
            property_id=host_id,
        )

    def diagnostics(self, host_id: str) -> ProviderResult[dict[str, Any]]:
        return _result(
            "yandex_webmaster",
            [{"severity": "WARNING", "code": "SLOW", "state": "ACTIVE"}],
            property_id=host_id,
        )


class FakeMetrikaClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def stat_data(
        self,
        *,
        counter: int | str,
        metrics: Sequence[str],
        date1: date,
        date2: date,
        dimensions: Sequence[str] = (),
        filters: str | None = None,
        accuracy: str = "medium",
        limit: int = 100,
    ) -> ProviderResult[dict[str, Any]]:
        self.calls.append(
            {
                "counter": counter,
                "metrics": tuple(metrics),
                "date1": date1,
                "date2": date2,
                "accuracy": accuracy,
                "limit": limit,
            }
        )
        index = len(self.calls) - 1
        visits = 120.0 if index == 0 else 90.0
        return _result(
            "yandex_metrika",
            [{"visits": visits, "users": visits - 20.0}],
            property_id=str(counter),
            data_age_days=1,
        )


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    instance = Store(tmp_path / "seo.db")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def builder_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_build_weekly(**kwargs: Any) -> Report:
        captured.update(kwargs)
        return Report(
            kind="weekly",
            property_id=str(kwargs["property_id"]),
            period_start=kwargs["period_start"].isoformat(),
            period_end=kwargs["period_end"].isoformat(),
            headline="built weekly",
            sections=[ReportSection(key="totals", title="Totals", summary="built", rows=[])],
            generated_at="2026-11-10T00:00:00+00:00",
        )

    monkeypatch.setattr(services, "_load_weekly_builder", lambda: fake_build_weekly)
    return captured


# -- discovery -------------------------------------------------------------------------------


def test_properties_reports_token_presence_but_never_the_value() -> None:
    service = SeoService(
        _config(),
        secrets=_secrets(gsc="super-secret-token"),
    )

    response = service.properties()

    assert response["ok"] is True
    entries = {entry["provider"]: entry for entry in response["data"]["providers"]}
    assert entries["gsc"]["token"] == "present"
    assert entries["gsc"]["token_present"] is True
    assert entries["gsc"]["ready"] is True
    assert entries["yandex_webmaster"]["token"] == "missing"
    assert entries["yandex_metrika"]["token"] == "missing"
    assert entries["yandex_metrika"]["ready"] is False
    assert entries["psi"]["requires_token"] is False
    assert response["meta"]["ready"] == ["gsc"]
    assert "yandex_webmaster: token missing" in response["warnings"]
    assert "yandex_metrika: token missing" in response["warnings"]
    assert "super-secret-token" not in json.dumps(response)


# -- GSC -------------------------------------------------------------------------------------


def test_gsc_query_returns_rows_meta_and_persists_metrics(store: Store) -> None:
    client = FakeGscClient([GSC_CURRENT])
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="token"),
        clients={"gsc": client},
    )

    response = service.gsc_query(start=date(2026, 11, 1), end=date(2026, 11, 7))

    assert response["ok"] is True
    assert response["data"]["rows"] == GSC_CURRENT
    assert response["meta"]["start"] == "2026-11-01"
    assert response["meta"]["end"] == "2026-11-07"
    assert response["meta"]["dimensions"] == ["query"]
    assert response["meta"]["persisted"] == {"metrics": 4, "pages": 0}
    assert client.calls == [
        {
            "start_date": date(2026, 11, 1),
            "end_date": date(2026, 11, 7),
            "dimensions": ("query",),
            "search_type": "web",
            "row_limit": 100,
            "max_rows": 100,
            "filters": None,
        }
    ]

    persisted = store.get_metric_daily(PROPERTY, source="gsc")
    assert len(persisted) == 4
    assert {row["metric"] for row in persisted} == {"clicks", "impressions"}
    assert {row["scope"] for row in persisted} == {"query"}
    assert {row["key"] for row in persisted} == {"seo", "audit"}
    clicks = sum(row["value"] for row in persisted if row["metric"] == "clicks")
    assert clicks == 15.0
    assert store.list_pages(PROPERTY) == []


def test_gsc_query_persists_page_rows_as_unknown_status(store: Store) -> None:
    rows = [
        {
            "page": "https://example.com/seo",
            "clicks": 7.0,
            "impressions": 70.0,
            "ctr": 0.1,
            "position": 2.0,
        }
    ]
    client = FakeGscClient([rows])
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="token"),
        clients={"gsc": client},
    )

    response = service.gsc_query(
        start=date(2026, 11, 1),
        end=date(2026, 11, 7),
        dimensions=("page",),
        limit=10,
    )

    assert response["ok"] is True
    assert response["meta"]["persisted"] == {"metrics": 2, "pages": 1}
    pages = store.list_pages(PROPERTY)
    assert len(pages) == 1
    assert pages[0]["url"] == "https://example.com/seo"
    assert pages[0]["status"] == 0
    assert pages[0]["index_state"] == "gsc_search_analytics"
    metrics = store.get_metric_daily(PROPERTY, source="gsc", scope="page")
    assert {row["key"] for row in metrics} == {"https://example.com/seo"}


def test_gsc_query_without_store_warns_but_returns_rows() -> None:
    service = SeoService(
        _config(),
        secrets=_secrets(gsc="token"),
        clients={"gsc": FakeGscClient([GSC_CURRENT])},
    )

    response = service.gsc_query(start=date(2026, 11, 1), end=date(2026, 11, 7))

    assert response["ok"] is True
    assert "store: not configured; rows were not stored" in response["warnings"]
    assert "persisted" not in response["meta"]


def test_gsc_query_missing_token_fails_with_warning() -> None:
    service = SeoService(_config(), secrets=_secrets())

    response = service.gsc_query(start=date(2026, 11, 1), end=date(2026, 11, 7))

    assert response["ok"] is False
    assert response["error"]["code"] == AUTH_REQUIRED
    assert "lv24-seo auth login --provider gsc" in response["error"]["next_action"]
    assert "gsc: token missing" in response["warnings"]


def test_gsc_query_client_override_needs_no_token() -> None:
    service = SeoService(
        _config(),
        secrets=_secrets(),
        clients={"gsc": FakeGscClient([GSC_CURRENT])},
    )

    response = service.gsc_query(start=date(2026, 11, 1), end=date(2026, 11, 7))

    assert response["ok"] is True
    assert response["data"]["rows"] == GSC_CURRENT


def test_gsc_query_rejects_reversed_dates_before_calling_the_client() -> None:
    client = FakeGscClient([GSC_CURRENT])
    service = SeoService(
        _config(),
        secrets=_secrets(gsc="token"),
        clients={"gsc": client},
    )

    response = service.gsc_query(start=date(2026, 11, 7), end=date(2026, 11, 1))

    assert response["ok"] is False
    assert response["error"]["code"] == INVALID_INPUT
    assert client.calls == []


class _QuotaClient:
    def search_analytics(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        raise SeoError(
            code=QUOTA_EXCEEDED,
            message="daily quota exceeded",
            retryable=True,
        )


def test_provider_seo_error_maps_to_fail() -> None:
    service = SeoService(
        _config(),
        secrets=_secrets(),
        clients={"gsc": _QuotaClient()},
    )

    response = service.gsc_query(start=date(2026, 11, 1), end=date(2026, 11, 7))

    assert response["ok"] is False
    assert response["error"]["code"] == QUOTA_EXCEEDED
    assert response["error"]["retryable"] is True


class _BoomClient:
    def search_analytics(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        raise RuntimeError("super-secret-token leaked")


def test_unexpected_exception_maps_to_internal_without_payload_details() -> None:
    service = SeoService(
        _config(),
        secrets=_secrets(),
        clients={"gsc": _BoomClient()},
    )

    response = service.gsc_query(start=date(2026, 11, 1), end=date(2026, 11, 7))

    assert response["ok"] is False
    assert response["error"]["code"] == INTERNAL
    assert "gsc_query" in response["error"]["message"]
    assert "super-secret-token leaked" not in json.dumps(response)


# -- Yandex ----------------------------------------------------------------------------------


def test_yandex_queries_passes_host_and_limit() -> None:
    client = FakeWebmasterClient()
    service = SeoService(
        _config(),
        secrets=_secrets(webmaster="token"),
        clients={"yandex_webmaster": client},
    )

    response = service.yandex_queries(limit=7)

    assert response["ok"] is True
    assert response["data"]["host_id"] == HOST_ID
    assert response["data"]["rows"][0]["query"] == "seo"
    assert response["meta"]["host_id"] == HOST_ID
    assert response["meta"]["limit"] == 7
    assert client.query_calls == [{"host_id": HOST_ID, "limit": 7, "order_by": "TOTAL_SHOWS"}]


def test_yandex_indexing_combines_all_reads() -> None:
    service = SeoService(
        _config(),
        secrets=_secrets(webmaster="token"),
        clients={"yandex_webmaster": FakeWebmasterClient()},
    )

    response = service.yandex_indexing()

    assert response["ok"] is True
    assert response["data"]["summary"] == [{"sqi": 12, "pages_in_search": 120}]
    assert response["data"]["history"][0]["http_2xx"] == 100
    assert response["data"]["events"][0]["event"] == "entered"
    assert response["data"]["problems"][0]["code"] == "SLOW"
    assert set(response["meta"]["calls"]) == {"summary", "history", "events", "problems"}


def test_yandex_queries_missing_token_fails_with_warning() -> None:
    service = SeoService(_config(), secrets=_secrets())

    response = service.yandex_queries()

    assert response["ok"] is False
    assert response["error"]["code"] == AUTH_REQUIRED
    assert "yandex_webmaster: token missing" in response["warnings"]


# -- weekly report ---------------------------------------------------------------------------


def test_weekly_report_calls_builder_and_saves_snapshot(
    store: Store,
    builder_capture: dict[str, Any],
) -> None:
    metrika = FakeMetrikaClient()
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="gsc", webmaster="webmaster", metrika="metrika"),
        clients={
            "gsc": FakeGscClient([GSC_CURRENT, GSC_PREVIOUS]),
            "yandex_webmaster": FakeWebmasterClient(),
            "yandex_metrika": metrika,
        },
    )

    response = service.weekly_report(days=7, lag_days=2, send=False)

    assert response["ok"] is True
    assert response["data"]["kind"] == "weekly"
    assert response["data"]["headline"] == "built weekly"
    assert response["meta"]["sent"] is False
    assert response["meta"]["sources"] == [
        "gsc",
        "yandex_metrika",
        "yandex_webmaster",
    ]
    assert response["meta"]["data_age_days"] == 4
    assert "delivery:" not in " ".join(response["warnings"])

    assert builder_capture["current"]["clicks"] == 45.0
    assert builder_capture["current"]["impressions"] == 150.0
    assert builder_capture["previous"]["clicks"] > 0.0
    assert "yandex_rows" in builder_capture
    assert "metrika_rows" in builder_capture
    assert "gsc_rows" in builder_capture
    start = builder_capture["period_start"]
    end = builder_capture["period_end"]
    assert (end - start) == timedelta(days=6)
    assert len(metrika.calls) == 2

    period = response["meta"]["period"]
    stored = store.load_snapshot("weekly", PROPERTY, f"{period['start']}..{period['end']}")
    assert stored == response["data"]


def test_weekly_report_skips_engines_with_missing_tokens(
    store: Store,
    builder_capture: dict[str, Any],
) -> None:
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="gsc"),
        clients={"gsc": FakeGscClient([GSC_CURRENT, GSC_PREVIOUS])},
    )

    response = service.weekly_report()

    assert response["ok"] is True
    assert "yandex_webmaster: token missing" in response["warnings"]
    assert "yandex_metrika: token missing" in response["warnings"]
    assert response["meta"]["sources"] == ["gsc"]
    assert set(builder_capture["current"]) == {"clicks", "impressions", "ctr", "position"}
    assert builder_capture["previous"]["clicks"] == 10.0


def test_weekly_report_falls_back_when_builders_are_missing(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "_load_weekly_builder", lambda: None)
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="gsc"),
        clients={"gsc": FakeGscClient([GSC_CURRENT, GSC_PREVIOUS])},
    )

    response = service.weekly_report()

    assert response["ok"] is True
    assert any("report builders: unavailable" in warning for warning in response["warnings"])
    assert response["data"]["kind"] == "weekly"
    assert response["data"]["sections"]
    assert response["data"]["headline"]


def test_weekly_report_send_false_does_not_touch_delivery(
    store: Store,
    builder_capture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("delivery must not be called when send=False")

    monkeypatch.setattr(services, "_load_deliver", lambda: forbidden)
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="gsc"),
        clients={"gsc": FakeGscClient([GSC_CURRENT, GSC_PREVIOUS])},
    )

    response = service.weekly_report(send=False)

    assert response["ok"] is True
    assert response["meta"]["sent"] is False
    assert "delivery:" not in " ".join(response["warnings"])


def test_weekly_report_send_true_without_delivery_warns(
    store: Store,
    builder_capture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="gsc"),
        clients={"gsc": FakeGscClient([GSC_CURRENT, GSC_PREVIOUS])},
    )

    response = service.weekly_report(send=True)

    assert response["ok"] is True
    assert response["meta"]["sent"] is False
    assert "delivery: no channel configured" in response["warnings"]


def test_weekly_report_send_true_delivers_when_available(
    store: Store,
    builder_capture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[object] = []

    def fake_deliver_report(self: SeoService, report: object, warnings: list[str]) -> bool:
        delivered.append(report)
        return True

    monkeypatch.setattr(SeoService, "_deliver_report", fake_deliver_report)
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(gsc="gsc"),
        clients={"gsc": FakeGscClient([GSC_CURRENT, GSC_PREVIOUS])},
    )

    response = service.weekly_report(send=True)

    assert response["ok"] is True
    assert response["meta"]["sent"] is True
    assert len(delivered) == 1
    assert "delivery:" not in " ".join(response["warnings"])


def test_weekly_report_without_store_warns_and_returns_report(
    builder_capture: dict[str, Any],
) -> None:
    service = SeoService(
        _config(),
        secrets=_secrets(gsc="gsc"),
        clients={"gsc": FakeGscClient([GSC_CURRENT, GSC_PREVIOUS])},
    )

    response = service.weekly_report()

    assert response["ok"] is True
    assert "store: not configured; snapshot was not saved" in response["warnings"]
    assert "snapshot_hash" not in response["meta"]


# -- crawl -----------------------------------------------------------------------------------

ROOT_HTML = b"""\
<html><head>
<title>Home</title>
<link rel="canonical" href="https://example.com/">
<meta name="robots" content="noindex">
</head><body><a href="/about">About</a></body></html>
"""

ABOUT_HTML = b"""\
<html><head>
<title>About</title>
<link rel="canonical" href="https://example.com/about">
</head><body>About us</body></html>
"""


class FakeFetcher:
    instances: ClassVar[list[FakeFetcher]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        FakeFetcher.instances.append(self)

    def fetch_robots(self, base_url: str) -> RobotsPolicy:
        return RobotsPolicy(disallow=[], allow=[], sitemaps=[])

    def fetch(self, url: str) -> FetchResult:
        if url == BASE_URL:
            return FetchResult(
                url=url,
                status=200,
                final_url=url,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=ROOT_HTML,
            )
        if url == "https://example.com/about":
            return FetchResult(
                url=url,
                status=200,
                final_url=url,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=ABOUT_HTML,
            )
        return FetchResult(url=url, status=404, final_url=url, error=None)

    def close(self) -> None:
        self.closed = True


def test_crawl_and_store_persists_pages_issues_and_run(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFetcher.instances.clear()
    monkeypatch.setattr(services, "Fetcher", FakeFetcher)
    service = SeoService(
        _config(),
        store=store,
        secrets=_secrets(),
    )

    response = service.crawl_and_store(start_url=BASE_URL, max_urls=5)

    assert response["ok"] is True
    assert response["data"]["summary"]["urls"] == 2
    assert response["data"]["summary"]["fetched"] == 2
    assert response["data"]["graph"]["nodes"] == 2
    assert response["data"]["graph"]["edges"] == 1
    rule_ids = {issue["rule_id"] for issue in response["data"]["issues"]}
    assert "noindex-on-200" in rule_ids
    assert response["meta"]["pages_stored"] == 2
    assert response["meta"]["issues_stored"] == len(response["data"]["issues"])

    pages = store.list_pages(PROPERTY)
    assert {page["url"] for page in pages} == {
        "https://example.com/",
        "https://example.com/about",
    }
    stored_issues = store.list_issues(PROPERTY)
    assert any(issue["rule_id"] == "noindex-on-200" for issue in stored_issues)
    runs = store.conn.execute("SELECT kind, status FROM runs").fetchall()
    assert [(row["kind"], row["status"]) for row in runs] == [("crawl", "ok")]
    assert FakeFetcher.instances[0].closed is True


def test_crawl_and_store_requires_a_start_url() -> None:
    service = SeoService(
        _config(base_url=""),
        secrets=_secrets(),
    )

    response = service.crawl_and_store()

    assert response["ok"] is False
    assert response["error"]["code"] == INVALID_INPUT


def test_crawl_and_store_without_store_still_returns_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "Fetcher", FakeFetcher)
    service = SeoService(_config(), secrets=_secrets())

    response = service.crawl_and_store(start_url=BASE_URL, max_urls=2)

    assert response["ok"] is True
    assert response["meta"]["pages_stored"] == 0
    assert "store: not configured; crawl results were not persisted" in response["warnings"]



