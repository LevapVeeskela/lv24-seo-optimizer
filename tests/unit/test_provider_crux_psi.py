"""Offline tests for the CrUX and PSI adapters (``httpx.MockTransport``, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, QUOTA_EXCEEDED, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import LOW_TRAFFIC
from lv24_seo.providers.crux import CruxClient
from lv24_seo.providers.psi import PsiClient

Handler = Callable[[httpx.Request], httpx.Response]

CRUX_RECORD = {
    "record": {
        "key": {"origin": "https://example.com"},
        "metrics": {
            "first_contentful_paint": {
                "percentiles": {"p75": 1800.0},
                "distributions": [
                    {"min": 0, "max": 1800, "proportion": 0.6},
                    {"min": 1800, "max": 3000, "proportion": 0.3},
                    {"min": 3000, "proportion": 0.1},
                ],
            },
            "cumulative_layout_shift": {
                "percentiles": {"p75": 0.06},
                "distributions": [
                    {"min": 0, "max": 0.1, "proportion": 0.82},
                    {"min": 0.1, "max": 0.25, "proportion": 0.12},
                    {"min": 0.25, "proportion": 0.06},
                ],
            },
            "largest_contentful_paint": {
                "percentiles": {"p75": 2410.5},
                "distributions": [
                    {"min": 0, "max": 2500, "proportion": 0.78},
                    {"min": 2500, "max": 4000, "proportion": 0.15},
                    {"min": 4000, "proportion": 0.07},
                ],
            },
        },
        "collectionPeriod": {
            "firstDate": {"year": 2026, "month": 10, "day": 13},
            "lastDate": {"year": 2026, "month": 11, "day": 9},
        },
    }
}


def _http(handler: Handler, *, max_retries: int = 0) -> HttpClient:
    return HttpClient(
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        sleep=lambda _seconds: None,
    )


# --- CrUX -------------------------------------------------------------------------------


def test_crux_404_means_low_traffic_not_an_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"code": 404, "message": "chrome ux report data not found"}}
        )

    with _http(handler) as http:
        result = CruxClient(api_key="secret-key", http=http).origin("https://example.com")

    assert result.empty is True
    assert result.rows == []
    assert result.limitations == (LOW_TRAFFIC,)
    assert result.warnings == ("no field data",)
    assert result.source == "crux"
    assert result.property_id == "https://example.com"
    assert result.data_age_days == 2


def test_crux_200_without_metrics_is_low_traffic_too() -> None:
    with _http(lambda _request: httpx.Response(200, json={"record": {}})) as http:
        result = CruxClient(api_key="secret-key", http=http).url("https://example.com/page")

    assert result.empty is True
    assert result.limitations == (LOW_TRAFFIC,)
    assert result.warnings == ("no field data",)


def test_crux_maps_metrics_to_p75_rows() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CRUX_RECORD)

    with _http(handler) as http:
        result = CruxClient(api_key="secret-key", http=http).origin("https://example.com")

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/records:queryRecord"
    assert request.url.params["key"] == "secret-key"
    assert json.loads(request.content) == {"origin": "https://example.com"}

    assert result.source == "crux"
    assert result.property_id == "https://example.com"
    assert result.limitations == ()
    assert result.warnings == ()
    # Core Web Vitals first, then supporting timings; unknown metrics sort alphabetically.
    assert result.rows == [
        {
            "metric": "largest_contentful_paint",
            "p75": 2410.5,
            "good": 0.78,
            "needs_improvement": 0.15,
            "poor": 0.07,
            "unit": "ms",
        },
        {
            "metric": "cumulative_layout_shift",
            "p75": 0.06,
            "good": 0.82,
            "needs_improvement": 0.12,
            "poor": 0.06,
            "unit": "score",
        },
        {
            "metric": "first_contentful_paint",
            "p75": 1800.0,
            "good": 0.6,
            "needs_improvement": 0.3,
            "poor": 0.1,
            "unit": "ms",
        },
    ]


def test_crux_url_query_sends_url_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CRUX_RECORD)

    with _http(handler) as http:
        result = CruxClient(api_key="secret-key", http=http).url("https://example.com/page")

    assert json.loads(seen[0].content) == {"url": "https://example.com/page"}
    assert result.property_id == "https://example.com/page"


def test_crux_400_maps_to_invalid_input_without_leaking_the_key() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"code": 400, "message": "invalid argument"}})

    with pytest.raises(SeoError) as excinfo, _http(handler) as http:
        CruxClient(api_key="secret-key", http=http).origin("not a url")

    error = excinfo.value
    assert error.code == INVALID_INPUT
    assert error.retryable is False
    assert calls == 1
    rendered = f"{error.message} {error.detail!r}"
    assert "secret-key" not in rendered
    assert "?" not in error.message


def test_crux_requires_an_api_key_without_calling_the_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=CRUX_RECORD)

    with pytest.raises(SeoError) as excinfo, _http(handler) as http:
        CruxClient(api_key="  ", http=http).origin("https://example.com")

    assert excinfo.value.code == AUTH_REQUIRED
    assert "crux" in (excinfo.value.next_action or "")
    assert calls == 0


@pytest.mark.parametrize("target", ["", "   "])
def test_crux_empty_origin_or_url_raises_invalid_input(target: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=CRUX_RECORD)

    with _http(handler) as http:
        client = CruxClient(api_key="secret-key", http=http)
        with pytest.raises(SeoError) as origin_error:
            client.origin(target)
        with pytest.raises(SeoError) as url_error:
            client.url(target)

    assert origin_error.value.code == INVALID_INPUT
    assert url_error.value.code == INVALID_INPUT
    assert calls == 0


# --- PSI --------------------------------------------------------------------------------

PSI_RUN = {
    "id": "https://example.com/page",
    "lighthouseResult": {
        "categories": {
            "performance": {"id": "performance", "score": 0.87},
            "seo": {"id": "seo", "score": 1.0},
            "accessibility": {"id": "accessibility", "score": 0.95},
        }
    },
    "loadingExperience": {
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2400, "category": "FAST"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5, "category": "FAST"},
        }
    },
}


def test_psi_maps_requested_categories_and_field_metrics() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PSI_RUN)

    with _http(handler) as http:
        result = PsiClient(api_key="psi-key", http=http).run(
            "https://example.com/page", strategy="mobile", categories=("performance", "seo")
        )

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/pagespeedonline/v5/runPagespeed"
    assert request.url.params["url"] == "https://example.com/page"
    assert request.url.params["strategy"] == "mobile"
    assert request.url.params.get_list("category") == ["performance", "seo"]
    assert request.url.params["key"] == "psi-key"

    assert result.source == "psi"
    assert result.property_id == "https://example.com/page"
    assert result.data_age_days == 0
    assert result.rows == [
        {"category": "performance", "score": 0.87},
        {"category": "seo", "score": 1.0},
        {"category": "field", "metric": "LCP", "p75": 2400.0},
        {"category": "field", "metric": "CLS", "p75": 0.05},
    ]


def test_psi_without_key_omits_the_key_param() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "lighthouseResult": {"categories": {"performance": {"score": 0.5}}},
            },
        )

    with _http(handler) as http:
        result = PsiClient(http=http).run("https://example.com/", categories=("performance",))

    assert "key" not in seen[0].url.params
    assert seen[0].url.params.get_list("category") == ["performance"]
    assert result.rows == [{"category": "performance", "score": 0.5}]


def test_psi_400_maps_to_invalid_input() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"code": 400, "message": "bad url"}})

    with pytest.raises(SeoError) as excinfo, _http(handler) as http:
        PsiClient(http=http).run("https://example.com/")

    assert excinfo.value.code == INVALID_INPUT
    assert "public and reachable" in (excinfo.value.next_action or "")
    assert calls == 1


def test_psi_429_maps_to_quota_exceeded() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    with pytest.raises(SeoError) as excinfo, _http(handler) as http:
        PsiClient(http=http).run("https://example.com/")

    assert excinfo.value.code == QUOTA_EXCEEDED


def test_psi_falls_back_to_origin_field_data_with_a_warning() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "lighthouseResult": {"categories": {"performance": {"score": 0.5}}},
                "originLoadingExperience": {
                    "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3000}}
                },
            },
        )

    with _http(handler) as http:
        result = PsiClient(http=http).run("https://example.com/", categories=("performance",))

    assert result.rows == [
        {"category": "performance", "score": 0.5},
        {"category": "field", "metric": "LCP", "p75": 3000.0},
    ]
    assert result.warnings == ("field data is origin-level, not URL-level",)


def test_psi_empty_url_raises_invalid_input() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=PSI_RUN)

    with pytest.raises(SeoError) as excinfo, _http(handler) as http:
        PsiClient(http=http).run("   ")

    assert excinfo.value.code == INVALID_INPUT
    assert calls == 0
