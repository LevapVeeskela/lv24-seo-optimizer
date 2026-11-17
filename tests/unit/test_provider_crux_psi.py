"""Offline tests for the CrUX and PSI adapters (``httpx.MockTransport``, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, QUOTA_EXCEEDED, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import LOW_TRAFFIC
from lv24_seo.providers.crux import CruxClient
from lv24_seo.providers.crux.client import (
    CRUX_HISTORY_GAPS,
    CRUX_HISTORY_METRICS_FILTERED,
    CRUX_HISTORY_SHORT,
    CRUX_PERIOD_MISSING,
    CRUX_URL_NORMALIZED,
)
from lv24_seo.providers.psi import PsiClient
from lv24_seo.providers.psi.client import (
    PSI_AUDITS_CAP,
    PSI_AUDITS_TRUNCATED,
    PSI_CAPTCHA,
    PSI_FINAL_URL_DIFFERS,
    PSI_RUNTIME_ERROR,
)

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
    assert result.limitations == (LOW_TRAFFIC, CRUX_PERIOD_MISSING)
    assert result.warnings == ("no field data",)
    assert result.source == "crux"
    assert result.property_id == "https://example.com"
    assert result.data_age_days == 2


def test_crux_200_without_metrics_is_low_traffic_too() -> None:
    with _http(lambda _request: httpx.Response(200, json={"record": {}})) as http:
        result = CruxClient(api_key="secret-key", http=http).url("https://example.com/page")

    assert result.empty is True
    assert result.limitations == (LOW_TRAFFIC, CRUX_PERIOD_MISSING)
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
    assert result.data_age_days == max(0, (result.fetched_at.date() - date(2026, 11, 9)).days)
    assert result.to_meta()["collection_period"] == {
        "first_date": "2026-10-13",
        "last_date": "2026-11-09",
    }
    # Core Web Vitals first, then supporting timings; unknown metrics sort alphabetically.
    assert result.rows == [
        {
            "metric": "largest_contentful_paint",
            "p75": 2410.5,
            "good": 0.78,
            "needs_improvement": 0.15,
            "poor": 0.07,
            "unit": "ms",
            "first_date": "2026-10-13",
            "last_date": "2026-11-09",
        },
        {
            "metric": "cumulative_layout_shift",
            "p75": 0.06,
            "good": 0.82,
            "needs_improvement": 0.12,
            "poor": 0.06,
            "unit": "score",
            "first_date": "2026-10-13",
            "last_date": "2026-11-09",
        },
        {
            "metric": "first_contentful_paint",
            "p75": 1800.0,
            "good": 0.6,
            "needs_improvement": 0.3,
            "poor": 0.1,
            "unit": "ms",
            "first_date": "2026-10-13",
            "last_date": "2026-11-09",
        },
    ]


def test_crux_url_normalization_is_a_warning_and_limitation() -> None:
    payload = {
        **CRUX_RECORD,
        "urlNormalizationDetails": {
            "originalUrl": "https://example.com/page",
            "normalizedUrl": "https://example.com/page/",
        },
    }

    with _http(lambda _request: httpx.Response(200, json=payload)) as http:
        result = CruxClient(api_key="secret-key", http=http).url("https://example.com/page")

    assert result.limitations == (CRUX_URL_NORMALIZED,)
    assert result.warnings == (
        "crux normalized url: https://example.com/page -> https://example.com/page/",
    )
    assert len(result.rows) == 3


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


PSI_AUDITS_RUN = {
    "captchaResult": "CAPTCHA_NOT_NEEDED",
    "analysisUTCTimestamp": "2026-11-17T10:00:00Z",
    "lighthouseResult": {
        "finalUrl": "https://example.com/page",
        "categories": {
            "performance": {"score": 0.5},
            "seo": {"score": 0.9},
        },
        "audits": {
            "unused-javascript": {
                "title": "Reduce unused JavaScript",
                "score": 0.4,
                "displayValue": "1.2 s",
                "description": (
                    'Remove <a href="https://example.com">unused</a> code &amp; retry.'
                ),
                "details": {
                    "type": "opportunity",
                    "overallSavingsMs": 1200.5,
                    "overallSavingsBytes": 50_000,
                    "items": [{"url": "a"}, {"url": "b"}],
                },
            },
            "is-on-https": {
                "title": "Uses HTTPS",
                "score": 1,
                "details": {"type": "binary"},
            },
        },
    },
}


def test_psi_audits_are_bounded_and_plain_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PSI_AUDITS_RUN)

    with _http(handler) as http:
        report = PsiClient(http=http).audits(
            "https://example.com/page",
            strategy="mobile",
            categories=("performance", "seo"),
        )

    assert seen[0].url.params.get_list("category") == ["performance", "seo"]
    assert report.source == "psi"
    assert report.property_id == "https://example.com/page"
    assert report.strategy == "mobile"
    assert report.analysis_utc_timestamp == "2026-11-17T10:00:00Z"
    assert report.final_url == "https://example.com/page"
    assert report.total == 2
    assert report.truncated is False
    assert report.limitations == ()
    assert report.warnings == ()
    assert report.categories == [
        {"category": "performance", "score": 0.5},
        {"category": "seo", "score": 0.9},
    ]
    assert [row["id"] for row in report.rows] == ["is-on-https", "unused-javascript"]
    assert report.rows[0] == {
        "id": "is-on-https",
        "title": "Uses HTTPS",
        "score": 1.0,
        "display_value": "",
        "description": "",
        "details_type": "binary",
        "overall_savings_ms": None,
        "overall_savings_bytes": None,
        "items_count": None,
    }
    assert report.rows[1]["title"] == "Reduce unused JavaScript"
    assert report.rows[1]["display_value"] == "1.2 s"
    assert report.rows[1]["description"] == "Remove unused code & retry."
    assert "<a" not in str(report.rows[1]["description"])
    assert report.rows[1]["details_type"] == "opportunity"
    assert report.rows[1]["overall_savings_ms"] == 1200.5
    assert report.rows[1]["overall_savings_bytes"] == 50_000
    assert report.rows[1]["items_count"] == 2


def test_psi_audits_cap_the_audit_list() -> None:
    audits = {f"audit-{index:03d}": {"title": "t"} for index in range(PSI_AUDITS_CAP + 5)}
    payload = {"lighthouseResult": {"audits": audits}}

    with _http(lambda _request: httpx.Response(200, json=payload)) as http:
        report = PsiClient(http=http).audits("https://example.com/")

    assert len(report.rows) == PSI_AUDITS_CAP
    assert report.total == PSI_AUDITS_CAP + 5
    assert report.truncated is True
    assert report.limitations == (PSI_AUDITS_TRUNCATED,)


def test_psi_run_reports_runtime_error_final_url_and_captcha() -> None:
    payload = {
        "captchaResult": "CAPTCHA_NEEDED",
        "lighthouseResult": {
            "finalUrl": "https://example.com/redirected",
            "runtimeError": {
                "code": "ERRORED_DOCUMENT_REQUEST",
                "message": "Failed to load <html>",
            },
            "categories": {"performance": {"score": 0.2}},
        },
    }

    with _http(lambda _request: httpx.Response(200, json=payload)) as http:
        result = PsiClient(http=http).run("https://example.com/page", categories=("performance",))

    assert result.rows == [{"category": "performance", "score": 0.2}]
    assert result.limitations == (
        PSI_RUNTIME_ERROR,
        PSI_CAPTCHA,
        PSI_FINAL_URL_DIFFERS,
    )
    assert result.warnings == (
        "psi runtime error: ERRORED_DOCUMENT_REQUEST: Failed to load <html>",
        "psi captcha: CAPTCHA_NEEDED",
        "psi final url differs from requested: https://example.com/redirected",
    )


def test_psi_clean_run_has_no_diagnostic_limitations() -> None:
    payload = {
        "captchaResult": "CAPTCHA_NOT_NEEDED",
        "lighthouseResult": {
            "finalUrl": "https://example.com/page/",
            "categories": {"performance": {"score": 0.9}},
        },
    }

    with _http(lambda _request: httpx.Response(200, json=payload)) as http:
        result = PsiClient(http=http).run("https://example.com/page", categories=("performance",))

    assert result.limitations == ()
    assert result.warnings == ()


# --- CrUX history -----------------------------------------------------------------------

CRUX_HISTORY = {
    "record": {
        "metrics": {
            "largest_contentful_paint": {"percentilesTimeseries": {"p75s": [2500, 2400, None]}},
            "cumulative_layout_shift": {
                "percentilesTimeseries": {"p75s": ["0.10", "0.08", "0.09"]}
            },
            "round_trip_time": {"percentilesTimeseries": {"p75s": [100, 110, 120]}},
        },
        "collectionPeriods": [
            {
                "firstDate": {"year": 2026, "month": 9, "day": 7},
                "lastDate": {"year": 2026, "month": 10, "day": 4},
            },
            {
                "firstDate": {"year": 2026, "month": 9, "day": 14},
                "lastDate": {"year": 2026, "month": 10, "day": 11},
            },
            {
                "firstDate": {"year": 2026, "month": 9, "day": 21},
                "lastDate": {"year": 2026, "month": 10, "day": 18},
            },
        ],
    }
}


def test_crux_history_returns_bounded_weekly_p75_periods() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CRUX_HISTORY)

    with _http(handler) as http:
        result = CruxClient(api_key="secret-key", http=http).history(
            origin="https://example.com", form_factor="phone", weeks=2
        )

    assert seen[0].url.path == "/v1/records:queryHistoryRecord"
    assert json.loads(seen[0].content) == {
        "origin": "https://example.com",
        "collectionPeriodCount": 2,
        "formFactor": "PHONE",
    }
    assert result.total == 3
    assert result.truncated is True
    assert result.rows == [
        {
            "first_date": "2026-09-14",
            "last_date": "2026-10-11",
            "device": "phone",
            "lcp": 2400.0,
            "cls": 0.08,
        },
        {
            "first_date": "2026-09-21",
            "last_date": "2026-10-18",
            "device": "phone",
            "cls": 0.09,
        },
    ]
    assert CRUX_HISTORY_GAPS in result.limitations
    assert CRUX_HISTORY_METRICS_FILTERED in result.limitations
    assert result.data_age_days == max(0, (result.fetched_at.date() - date(2026, 10, 18)).days)
    assert result.to_meta()["collection_period"] == {
        "first_date": "2026-09-21",
        "last_date": "2026-10-18",
    }


def test_crux_history_reports_short_history_and_404_as_low_traffic() -> None:
    with _http(lambda _request: httpx.Response(200, json=CRUX_HISTORY)) as http:
        short = CruxClient(api_key="secret-key", http=http).history(
            origin="https://example.com", weeks=40
        )

    assert CRUX_HISTORY_SHORT in short.limitations
    assert short.truncated is False

    with _http(lambda _request: httpx.Response(404, json={"error": {"code": 404}})) as http:
        empty = CruxClient(api_key="secret-key", http=http).history(
            origin="https://example.com", weeks=4
        )

    assert empty.empty is True
    assert empty.limitations == (LOW_TRAFFIC, CRUX_PERIOD_MISSING)
    assert empty.warnings == ("no field data",)


def test_crux_history_rejects_bad_target_and_weeks_before_the_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=CRUX_HISTORY)

    with _http(handler) as http:
        client = CruxClient(api_key="secret-key", http=http)
        with pytest.raises(SeoError) as both:
            client.history(origin="https://example.com", url="https://example.com/page")
        with pytest.raises(SeoError) as neither:
            client.history()
        with pytest.raises(SeoError) as low:
            client.history(origin="https://example.com", weeks=0)
        with pytest.raises(SeoError) as high:
            client.history(origin="https://example.com", weeks=41)

    for error in (both, neither, low, high):
        assert error.value.code == INVALID_INPUT
    assert calls == 0
