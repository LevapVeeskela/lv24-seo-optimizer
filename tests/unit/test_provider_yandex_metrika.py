"""Offline tests for the Yandex Metrika adapter (``httpx.MockTransport``, no network)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from lv24_seo.core.errors import AUTH_INVALID, AUTH_REQUIRED, INVALID_INPUT, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import SAMPLED
from lv24_seo.providers.yandex_metrika import YandexMetrikaClient
from lv24_seo.providers.yandex_metrika import client as metrika_client_module

Handler = Callable[[httpx.Request], httpx.Response]
TOKEN = "test-oauth-token"


def _metrika(handler: Handler, *, token: str = TOKEN) -> YandexMetrikaClient:
    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    return YandexMetrikaClient(token=token, http=http)


def _stat_table(
    *,
    dimensions: list[dict[str, str]] | None = None,
    metrics: list[float] | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "data": [
            {
                "dimensions": dimensions if dimensions is not None else [],
                "metrics": metrics if metrics is not None else [1],
            }
        ],
        "sampled": False,
    }
    payload.update(extra)
    return payload


def test_counters_sends_oauth_header_and_maps_rows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "counters": [
                    {
                        "id": 123456,
                        "name": "LV24",
                        "site": "lv24.by",
                        "status": "Active",
                        "permission": "own",
                    },
                    {
                        "id": 987654,
                        "name": "Staging",
                        "site": "staging.lv24.by",
                        "status": "Active",
                    },
                ],
                "rows": 2,
            },
        )

    result = _metrika(handler).counters()

    assert result.source == "yandex_metrika"
    assert result.property_id == "counters"
    assert result.rows == [
        {"id": 123456, "name": "LV24", "site": "lv24.by", "status": "Active"},
        {"id": 987654, "name": "Staging", "site": "staging.lv24.by", "status": "Active"},
    ]
    assert result.total == 2
    assert result.truncated is False
    assert result.data_age_days == 0
    assert result.empty is False

    request = seen[0]
    assert request.method == "GET"
    assert request.url == httpx.URL("https://api-metrika.yandex.net/management/v1/counters")
    assert request.headers["authorization"] == f"OAuth {TOKEN}"


def test_goals_maps_rows_and_uses_counter_in_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/management/v1/counter/123456/goals"
        return httpx.Response(
            200,
            json={
                "goals": [
                    {
                        "id": 42,
                        "name": "Order",
                        "type": "url",
                        "is_retargeting": True,
                        "conditions": [{"type": "contain", "url": "order"}],
                    },
                    {"id": 43, "name": "Call", "type": "phone", "is_retargeting": False},
                ],
                "rows": 2,
            },
        )

    result = _metrika(handler).goals(123456)

    assert result.rows == [
        {"id": 42, "name": "Order", "type": "url", "is_retargeting": True},
        {"id": 43, "name": "Call", "type": "phone", "is_retargeting": False},
    ]
    assert result.property_id == "123456"
    assert result.total == 2


def test_stat_data_maps_rows_and_reports_sampling() -> None:
    seen: list[httpx.Request] = []
    metrics = [
        "ym:s:visits",
        "ym:s:users",
        "ym:s:pageviews",
        "ym:s:bounceRate",
        "ym:s:avgVisitDurationSeconds",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "dimensions": [{"name": "/catalog", "id": "1"}],
                        "metrics": [10, 8, 20, 25.5, 61.2],
                    },
                    {
                        "dimensions": [{"name": "/contacts"}],
                        "metrics": [3, 2, 4, 50, 12],
                    },
                ],
                "total_rows": 250,
                "sampled": True,
                "sample_share": 0.5,
            },
        )

    result = _metrika(handler).stat_data(
        counter="123456",
        metrics=metrics,
        dimensions=["ym:s:startURLPath"],
        date1=date(2026, 11, 1),
        date2=date(2026, 11, 7),
        filters="ym:s:lastTrafficSource=='organic'",
        limit=100,
    )

    assert result.rows[0] == {
        "startURLPath": "/catalog",
        "visits": 10.0,
        "users": 8.0,
        "pageviews": 20.0,
        "bounceRate": 25.5,
        "avgVisitDurationSeconds": 61.2,
    }
    assert result.rows[1]["startURLPath"] == "/contacts"
    assert all(
        isinstance(value, float) for key, value in result.rows[0].items() if key != "startURLPath"
    )
    assert result.total == 250
    assert result.truncated is True
    assert result.limitations == (SAMPLED,)
    assert result.warnings == ("sampled",)
    assert result.property_id == "123456"

    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/stat/v1/data"
    assert request.headers["authorization"] == f"OAuth {TOKEN}"
    assert request.content == b""

    form = parse_qs(request.url.query.decode())
    assert form["ids"] == ["123456"]
    assert form["metrics"] == [",".join(metrics)]
    assert form["dimensions"] == ["ym:s:startURLPath"]
    assert form["date1"] == ["2026-11-01"]
    assert form["date2"] == ["2026-11-07"]
    assert form["accuracy"] == ["medium"]
    assert form["limit"] == ["100"]
    assert form["filters"] == ["ym:s:lastTrafficSource=='organic'"]


def test_stat_data_without_total_rows_is_not_truncated() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"dimensions": [], "metrics": [5]}],
                "sampled": False,
            },
        )

    result = _metrika(handler).stat_data(
        counter=123456,
        metrics=["ym:s:visits"],
        date1=date(2026, 11, 1),
        date2=date(2026, 11, 7),
    )

    assert result.rows == [{"visits": 5.0}]
    assert result.total is None
    assert result.truncated is False
    assert result.limitations == ()
    assert result.warnings == ()


def test_data_age_is_measured_from_the_end_of_date2(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(metrika_client_module, "now_utc", lambda: fixed)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_stat_table(metrics=[1]))

    result = _metrika(handler).stat_data(
        counter=123456,
        metrics=["ym:s:visits"],
        date1=date(2026, 11, 4),
        date2=date(2026, 11, 10),
    )

    assert result.fetched_at == fixed
    # 2026-11-10 23:59 UTC -> 2026-11-13 12:00 UTC is two whole days.
    assert result.data_age_days == 2


def test_data_age_is_never_negative_for_a_future_date2(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(metrika_client_module, "now_utc", lambda: fixed)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_stat_table(metrics=[1]))

    result = _metrika(handler).stat_data(
        counter=123456,
        metrics=["ym:s:visits"],
        date1=date(2026, 11, 10),
        date2=date(2026, 11, 10),
    )

    assert result.data_age_days == 0


def test_forbidden_maps_to_auth_invalid() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": [{"error_type": "oauth_token_invalid"}]})

    with pytest.raises(SeoError) as excinfo:
        _metrika(handler).counters()

    error = excinfo.value
    assert error.code == AUTH_INVALID
    assert error.retryable is False


def test_empty_token_raises_auth_required() -> None:
    with pytest.raises(SeoError) as excinfo:
        YandexMetrikaClient(token="   ")

    error = excinfo.value
    assert error.code == AUTH_REQUIRED
    assert error.next_action == "lv24-seo auth login --provider yandex"


def test_empty_metrics_raises_invalid_input_without_a_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_stat_table())

    client = _metrika(handler)
    with pytest.raises(SeoError) as excinfo:
        client.stat_data(
            counter=123456,
            metrics=[],
            date1=date(2026, 11, 1),
            date2=date(2026, 11, 7),
        )

    assert excinfo.value.code == INVALID_INPUT
    assert calls == 0


def test_missing_dates_raise_invalid_input() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for invalid input")

    client = _metrika(handler)
    with pytest.raises(SeoError) as excinfo:
        client.stat_data(
            counter=123456,
            metrics=["ym:s:visits"],
            date1=None,  # type: ignore[arg-type]
            date2=date(2026, 11, 7),
        )

    assert excinfo.value.code == INVALID_INPUT
