"""Weekly integration regressions (F01-F03, F25) with the real report builder.

Providers are fakes, storage is a real SQLite database, but
``report.builders.build_weekly`` runs unpatched: the test asserts on the
actual sections, sources and headline of the built report.
"""

from __future__ import annotations

from typing import Any

import pytest

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
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.providers.base import ProviderResult
from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import render_email_text
from lv24_seo.services import SeoService
from lv24_seo.store import Store

PROPERTY = "sc-domain:example.com"

GSC_CURRENT = [
    {
        "query": "seo",
        "page": "https://example.com/seo",
        "clicks": 10.0,
        "impressions": 100.0,
        "ctr": 0.1,
        "position": 3.0,
    },
    {
        "query": "audit",
        "page": "https://example.com/audit",
        "clicks": 5.0,
        "impressions": 50.0,
        "ctr": 0.1,
        "position": 5.0,
    },
]
GSC_PREVIOUS = [
    {
        "query": "seo",
        "page": "https://example.com/seo",
        "clicks": 8.0,
        "impressions": 100.0,
        "ctr": 0.08,
        "position": 4.0,
    },
    {
        "query": "audit",
        "page": "https://example.com/audit",
        "clicks": 5.0,
        "impressions": 50.0,
        "ctr": 0.1,
        "position": 5.0,
    },
]


def _config(**providers: Any) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Ex", base_url="https://example.com/", default_property=PROPERTY
        ),
        providers=ProvidersConfig(
            gsc=GscConfig(property=PROPERTY),
            yandex_webmaster=YandexWebmasterConfig(host_id="https:example.com:443"),
            yandex_metrika=YandexMetrikaConfig(counter="12345"),
            **providers,
        ),
        crawl=CrawlConfig(max_urls=50),
        storage=StorageConfig(data_dir="data"),
    )


def _result(
    source: str, rows: list[dict[str, Any]], **kwargs: Any
) -> ProviderResult[dict[str, Any]]:
    return ProviderResult(
        source=source,
        property_id=PROPERTY,
        fetched_at=utc_now(),
        data_age_days=kwargs.get("data_age_days", 1),
        rows=rows,
        total=len(rows),
        truncated=kwargs.get("truncated", False),
        limitations=tuple(kwargs.get("limitations", ())),
        warnings=tuple(kwargs.get("warnings", ())),
    )


class _Gsc:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.calls = 0

    def search_analytics(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return _result("gsc", self.responses[index])


class _Webmaster:
    def popular_queries(self, host_id: str, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        return _result(
            "yandex_webmaster",
            [{"query": "seo", "shows": 300.0, "clicks": 30.0, "position": 4.0}],
            data_age_days=4,
        )


class _Metrika:
    def __init__(self, visits: float) -> None:
        self.visits = visits
        self.calls = 0

    def stat_data(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        self.calls += 1
        visits = self.visits if self.calls == 1 else self.visits - 30.0
        return _result("yandex_metrika", [{"visits": visits, "users": visits - 20.0}])


class _Failing:
    def __init__(self, code: str = "AUTH_INVALID") -> None:
        from lv24_seo.core.errors import SeoError

        self._error = SeoError(code=code, message="denied", next_action="login")
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        def _raise(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            raise self._error

        return _raise


def _service(store: Store, clients: dict[str, Any]) -> SeoService:
    return SeoService(
        _config(),
        store=store,
        secrets=SecretStore(
            env={
                "LV24_SEO_GSC_TOKEN": "g",
                "LV24_SEO_YANDEX_WEBMASTER_TOKEN": "y",
                "LV24_SEO_YANDEX_METRIKA_TOKEN": "m",
            }
        ),
        clients=clients,
    )


def test_no_phantom_growth_when_yandex_only_in_current(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F01: identical GSC windows + Yandex snapshot must not report +30% growth."""
    with Store(tmp_path / "seo.db") as store:
        service = _service(
            store,
            {
                "gsc": _Gsc([GSC_CURRENT, GSC_CURRENT, GSC_CURRENT, GSC_CURRENT]),
                "yandex_webmaster": _Webmaster(),
                "yandex_metrika": _Metrika(120.0),
            },
        )
        response = service.weekly_report(send=False)

    assert response["ok"] is True
    sections = {section["key"]: section for section in response["data"]["sections"]}
    headline_rows = {row["metric"]: row for row in sections["headline"]["rows"]}
    # GSC is flat: the headline delta must be ~zero despite 30 Yandex clicks.
    assert headline_rows["clicks"]["abs_delta"] == 0.0
    assert "yandex" in sections["sources"]["summary"] or any(
        row["source"] == "yandex" for row in sections["sources"]["rows"]
    )
    assert response["meta"]["sources_failed"] == []
    limitations = " ".join(response["data"]["limitations"])
    assert "GSC" in limitations


def test_provider_failure_isolates_and_keeps_available_sources(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F03: a 401 in GSC must not cancel Yandex/Metrika results."""
    with Store(tmp_path / "seo.db") as store:
        service = _service(
            store,
            {
                "gsc": _Failing("AUTH_INVALID"),
                "yandex_webmaster": _Webmaster(),
                "yandex_metrika": _Metrika(120.0),
            },
        )
        response = service.weekly_report(send=False)

    assert response["ok"] is True
    assert response["meta"]["sources_failed"] == ["gsc"]
    assert response["meta"]["complete"] is False
    assert "yandex_metrika" in response["meta"]["sources"]
    sections = {section["key"]: section for section in response["data"]["sections"]}
    assert sections["sources"]["summary"] != "No engine returned rows"


def test_all_sources_failing_yields_explicit_no_data_report(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F03: total outage is an explicit no-data report, not an exception."""
    with Store(tmp_path / "seo.db") as store:
        service = _service(
            store,
            {
                "gsc": _Failing(),
                "yandex_webmaster": _Failing(),
                "yandex_metrika": _Failing(),
            },
        )
        response = service.weekly_report(send=False)

    assert response["ok"] is True
    assert response["meta"]["complete"] is False
    assert sorted(response["meta"]["sources_failed"]) == [
        "gsc",
        "yandex_metrika",
        "yandex_webmaster",
    ]
    assert "No provider data" in response["data"]["headline"]


def test_stored_crawl_issues_reach_the_builder(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F25: issues persisted by the crawler appear in the weekly issues section."""
    with Store(tmp_path / "seo.db") as store:
        store.upsert_issue(
            "broken-internal-link",
            "high",
            PROPERTY,
            "https://example.com/a",
            {"target": "https://example.com/gone"},
        )
        service = _service(
            store, {"gsc": _Gsc([GSC_CURRENT, GSC_PREVIOUS, GSC_CURRENT, GSC_PREVIOUS])}
        )
        response = service.weekly_report(send=False)

    sections = {section["key"]: section for section in response["data"]["sections"]}
    groups = {group["severity"]: group for group in sections["issues"]["rows"]}
    assert groups["high"]["count"] == 1
    assert groups["high"]["rule_ids"] == ["broken-internal-link"]


def test_query_page_rows_feed_opportunities_and_decay(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F25: vanished rows stay unknown (not zero) while GSC coverage is partial."""
    vanished_current = [dict(GSC_CURRENT[0])]
    vanished_previous = [
        dict(GSC_PREVIOUS[0]),
        {
            "query": "gone",
            "page": "https://example.com/gone",
            "clicks": 50.0,
            "impressions": 500.0,
            "ctr": 0.1,
            "position": 6.0,
        },
    ]
    with Store(tmp_path / "seo.db") as store:
        service = _service(
            store, {"gsc": _Gsc([GSC_CURRENT, GSC_PREVIOUS, vanished_current, vanished_previous])}
        )
        response = service.weekly_report(send=False)

    sections = {section["key"]: section for section in response["data"]["sections"]}
    decays = [row for row in sections["opportunities"]["rows"] if row["kind"] == "decay"]
    # GSC top-rows coverage is explicitly partial (truncated/anonymous rows are
    # omitted by the API), so an absent row is unknown, never modelled as zero.
    assert [row["query"] for row in decays] == []
    assert "unknown, not zero" in " ".join(response["data"]["limitations"])


class _ScriptedGsc:
    """Returns (rows, truncated) per call: date-current, date-previous, query x2."""

    def __init__(self, script: list[tuple[list[dict[str, Any]], bool]]) -> None:
        self.script = script
        self.calls = 0

    def search_analytics(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        rows, truncated = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return _result("gsc", rows, truncated=truncated)


class _EmptyMetrika:
    def stat_data(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        return _result("yandex_metrika", [])


def _complete_case(store: Store, clients: dict[str, Any]) -> dict[str, Any]:
    return _service(store, clients).weekly_report(send=False)


def test_complete_is_false_for_truncated_gsc(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        response = _complete_case(
            store,
            {
                "gsc": _ScriptedGsc([(GSC_CURRENT, True), (GSC_PREVIOUS, False)]),
                "yandex_webmaster": _Webmaster(),
                "yandex_metrika": _Metrika(120.0),
            },
        )
    assert response["meta"]["complete"] is False
    assert "gsc" in response["meta"]["sources_incomplete"]
    assert response["meta"]["truncated"] is True


def test_complete_is_false_when_gsc_previous_window_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        response = _complete_case(
            store,
            {
                "gsc": _ScriptedGsc([(GSC_CURRENT, False), ([], False)]),
                "yandex_webmaster": _Webmaster(),
                "yandex_metrika": _Metrika(120.0),
            },
        )
    assert response["meta"]["complete"] is False
    assert "gsc" in response["meta"]["sources_incomplete"]
    assert "headline delta unavailable" in " ".join(response["meta"]["limitations"])


def test_current_only_gsc_keeps_totals_without_inventing_a_baseline(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A real current window with no previous one is values + no comparison."""
    with Store(tmp_path / "seo.db") as store:
        response = _complete_case(
            store,
            {
                "gsc": _ScriptedGsc([(GSC_CURRENT, False), ([], False)]),
                "yandex_webmaster": _Webmaster(),
                "yandex_metrika": _Metrika(120.0),
            },
        )

    assert response["ok"] is True
    assert response["meta"]["complete"] is False
    assert "headline delta unavailable" in " ".join(response["meta"]["limitations"])

    sections = {section["key"]: section for section in response["data"]["sections"]}
    rows = {row["metric"]: row for row in sections["headline"]["rows"]}
    assert set(rows) == {"clicks", "impressions", "ctr", "position"}
    assert rows["clicks"]["current"] == 15.0
    assert rows["impressions"]["current"] == 150.0
    assert rows["ctr"]["current"] == 0.1
    assert rows["position"]["current"] == pytest.approx(550.0 / 150.0)
    for row in rows.values():
        assert row["previous"] is None
        assert row["abs_delta"] is None
        assert row["pct_delta"] is None
        assert row["significant"] is False
        assert row["direction"] == "flat"
        assert row["good"] is None

    # The email presenter reads previous=None as "no comparison": values render,
    # but no zero baseline, false delta or "stable" verdict is produced.
    text = render_email_text(_report_from(response["data"]))
    assert "Clicks: 15" in text
    assert "Clicks: No data" not in text
    assert "No comparison" in text
    assert "Previous 0" not in text
    assert "Worse" not in text
    assert "Performance is stable" not in text


def _report_from(payload: dict[str, Any]) -> Report:
    return Report(
        kind=payload["kind"],
        property_id=payload["property_id"],
        period_start=payload["period"]["start"],
        period_end=payload["period"]["end"],
        headline=payload["headline"],
        sections=[ReportSection(**section) for section in payload["sections"]],
        generated_at=payload["generated_at"],
        limitations=list(payload["limitations"]),
        warnings=list(payload["warnings"]),
    )


def test_complete_is_false_for_an_empty_source(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        response = _complete_case(
            store,
            {
                "gsc": _Gsc([GSC_CURRENT, GSC_PREVIOUS, GSC_CURRENT, GSC_PREVIOUS]),
                "yandex_webmaster": _Webmaster(),
                "yandex_metrika": _EmptyMetrika(),
            },
        )
    assert response["meta"]["complete"] is False
    assert response["meta"]["sources_incomplete"] == ["yandex_metrika"]


def test_complete_is_false_when_a_configured_source_is_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        service = SeoService(
            _config(),
            store=store,
            secrets=SecretStore(env={"LV24_SEO_GSC_TOKEN": "g"}),
            clients={"gsc": _Gsc([GSC_CURRENT, GSC_PREVIOUS, GSC_CURRENT, GSC_PREVIOUS])},
        )
        response = service.weekly_report(send=False)
    assert response["meta"]["complete"] is False
    assert response["meta"]["sources_missing"] == ["yandex_metrika", "yandex_webmaster"]


def test_complete_is_true_only_when_every_source_is_whole(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with Store(tmp_path / "seo.db") as store:
        response = _complete_case(
            store,
            {
                "gsc": _Gsc([GSC_CURRENT, GSC_PREVIOUS, GSC_CURRENT, GSC_PREVIOUS]),
                "yandex_webmaster": _Webmaster(),
                "yandex_metrika": _Metrika(120.0),
            },
        )
    assert response["meta"]["complete"] is True
    assert response["meta"]["sources_incomplete"] == []
