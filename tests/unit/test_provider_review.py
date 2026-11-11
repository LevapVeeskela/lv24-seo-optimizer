"""Regressions found while reviewing the provider contract repair."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from lv24_seo.core import http as http_module
from lv24_seo.core.errors import SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.core.ratelimit import Limiter
from lv24_seo.providers.gsc import GscClient
from lv24_seo.providers.yandex_webmaster import YandexWebmasterClient


class RecordingLimiter(Limiter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def acquire(self, key: str, cost: float = 1.0, timeout: float | None = None) -> float:
        self.calls.append(key)
        return 0.0


def test_gsc_inspection_retries_read_and_counts_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = RecordingLimiter()
    monkeypatch.setattr(http_module, "_DEFAULT_LIMITER", limiter)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"inspectionResult": {}})

    with HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _: None) as http:
        GscClient(
            access_token="synthetic", property_id="sc-domain:example.com", http=http
        ).inspect_url("https://example.com/")

    assert calls == 2
    assert limiter.calls == ["gsc.url_inspection", "gsc.url_inspection"]


def test_yandex_pagination_preserves_window_and_bounds_rows() -> None:
    offsets: list[int] = []
    limits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/"):
            return httpx.Response(200, json={"user_id": 7})
        params = request.url.params
        assert params["date_from"] == "2026-10-01"
        assert params["date_to"] == "2026-10-07"
        offset = int(params["offset"])
        limit = int(params["limit"])
        offsets.append(offset)
        limits.append(limit)
        return httpx.Response(
            200,
            json={
                "queries": [
                    {"query_text": f"q{i}", "indicators": {"TOTAL_SHOWS": 1}}
                    for i in range(offset, offset + limit)
                ],
                "count": 3500,
                "date_to": "2026-10-07",
            },
        )

    with HttpClient(transport=httpx.MockTransport(handler)) as http:
        result = YandexWebmasterClient(token="synthetic", http=http).popular_queries(
            "https:example.com:443",
            limit=499,
            date_from=date(2026, 10, 1),
            date_to=date(2026, 10, 7),
        )

    assert len(result.rows) == 3000
    assert offsets == [0, 499, 998, 1497, 1996, 2495, 2994]
    assert limits[-1] == 6
    assert result.total == 3500
    assert result.truncated


@pytest.mark.parametrize("limit", [0, 501, True])
def test_yandex_rejects_invalid_page_limit_without_network(limit: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid page limit must fail before the network")

    with HttpClient(transport=httpx.MockTransport(handler)) as http, pytest.raises(SeoError):
        YandexWebmasterClient(token="synthetic", http=http).popular_queries("host", limit=limit)


@pytest.mark.parametrize("payload", [{}, {"problems": None}, {"problems": {"NO_SITEMAPS": 1}}])
def test_yandex_malformed_diagnostics_is_not_a_clean_report(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/"):
            return httpx.Response(200, json={"user_id": 7})
        return httpx.Response(200, json=payload)

    with HttpClient(transport=httpx.MockTransport(handler)) as http, pytest.raises(SeoError):
        YandexWebmasterClient(token="synthetic", http=http).diagnostics("host")
