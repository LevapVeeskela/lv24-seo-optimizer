"""Offline tests for the shared HTTP client (``httpx.MockTransport``, no network)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from email.utils import formatdate

import httpx
import pytest

from lv24_seo.core.errors import (
    AUTH_INVALID,
    INVALID_INPUT,
    PROPERTY_NOT_FOUND,
    QUOTA_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.http import DEFAULT_USER_AGENT, HttpClient

Handler = Callable[[httpx.Request], httpx.Response]


def _client(
    handler: Handler,
    *,
    sleep: Callable[[float], None] | None = None,
    max_retries: int = 3,
    base_url: str = "",
) -> HttpClient:
    return HttpClient(
        base_url=base_url,
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
        sleep=sleep if sleep is not None else (lambda _seconds: None),
    )


def test_get_json_parses_body_and_sends_default_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": [1, 2]})

    with _client(handler) as client:
        payload = client.get_json("https://api.example.com/v1/rows", params={"q": "cafe"})

    assert payload == {"rows": [1, 2]}
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].headers["user-agent"] == DEFAULT_USER_AGENT
    assert seen[0].url.params["q"] == "cafe"


def test_post_json_sends_body_and_custom_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"accepted": True})

    with _client(handler) as client:
        payload = client.post_json(
            "https://api.example.com/v1/submit",
            json={"url": "https://site.example/"},
            headers={"X-Custom": "1"},
        )

    assert payload == {"accepted": True}
    assert seen[0].method == "POST"
    assert seen[0].headers["x-custom"] == "1"
    assert json.loads(seen[0].content) == {"url": "https://site.example/"}


def test_base_url_is_applied_to_relative_paths() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with _client(handler, base_url="https://api.example.com") as client:
        client.get_json("/v1/sites")

    assert seen[0].url == httpx.URL("https://api.example.com/v1/sites")


def test_401_maps_to_auth_invalid_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "invalid_token"})

    with pytest.raises(SeoError) as excinfo, _client(handler) as client:
        client.get_json("https://api.example.com/v1/sites")

    error = excinfo.value
    assert error.code == AUTH_INVALID
    assert error.retryable is False
    assert error.next_action == "lv24-seo auth login"
    assert "api.example.com/v1/sites" in error.message
    assert calls == 1


def test_403_maps_to_access_denied_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    with pytest.raises(SeoError) as excinfo, _client(handler) as client:
        client.get_json("https://api.example.com/v1/sites")

    error = excinfo.value
    assert error.code == AUTH_INVALID
    assert "access denied" in error.message
    assert error.retryable is False
    assert calls == 1


def test_404_maps_to_property_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(SeoError) as excinfo, _client(handler) as client:
        client.get_json("https://api.example.com/v1/sites/123")

    assert excinfo.value.code == PROPERTY_NOT_FOUND
    assert excinfo.value.retryable is False


def test_429_retry_after_seconds_is_honoured_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    with _client(handler, sleep=sleeps.append) as client:
        assert client.get_json("https://api.example.com/v1/data") == {"ok": True}

    assert calls == 2
    assert sleeps == [1.0]


def test_retry_after_http_date_is_capped_at_30_seconds() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            retry_at = formatdate(time.time() + 120, usegmt=True)
            return httpx.Response(429, headers={"Retry-After": retry_at})
        return httpx.Response(200, json={"ok": True})

    with _client(handler, sleep=sleeps.append) as client:
        assert client.get_json("https://api.example.com/v1/data") == {"ok": True}

    assert calls == 2
    assert sleeps == [30.0]


def test_429_exhaustion_maps_to_quota_exceeded() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    with (
        pytest.raises(SeoError) as excinfo,
        _client(handler, max_retries=1, sleep=sleeps.append) as client,
    ):
        client.get_json("https://api.example.com/v1/data")

    assert excinfo.value.code == QUOTA_EXCEEDED
    assert excinfo.value.retryable is True
    assert calls == 2
    assert sleeps == [1.0]


def test_500_is_retried_max_retries_times_then_raises() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with (
        pytest.raises(SeoError) as excinfo,
        _client(handler, max_retries=2, sleep=sleeps.append) as client,
    ):
        client.get_json("https://api.example.com/v1/data")

    assert calls == 3
    assert len(sleeps) == 2
    assert 0.4 <= sleeps[0] <= 0.6
    assert 0.8 <= sleeps[1] <= 1.2
    assert excinfo.value.code == UPSTREAM_UNAVAILABLE
    assert excinfo.value.retryable is True


def test_attempts_are_capped_at_three_even_with_larger_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(SeoError) as excinfo, _client(handler, max_retries=10) as client:
        client.get_json("https://api.example.com/v1/data")

    assert calls == 3
    assert excinfo.value.code == UPSTREAM_UNAVAILABLE


def test_400_is_not_retried() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "bad request"})

    with pytest.raises(SeoError) as excinfo, _client(handler, sleep=sleeps.append) as client:
        client.post_json("https://api.example.com/v1/submit", json={"url": "https://site.example"})

    assert calls == 1
    assert sleeps == []
    assert excinfo.value.code == INVALID_INPUT
    assert excinfo.value.retryable is False


def test_timeout_maps_to_upstream_unavailable() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(SeoError) as excinfo, _client(handler) as client:
        client.post_json("https://api.example.com/v1/submit", json={})

    # POST is not idempotent: no retry, but the failure is reported as retryable upstream trouble.
    assert calls == 1
    assert excinfo.value.code == UPSTREAM_UNAVAILABLE
    assert excinfo.value.retryable is True


def test_get_connect_error_is_retried_then_mapped() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("no route to host")

    with (
        pytest.raises(SeoError) as excinfo,
        _client(handler, max_retries=1, sleep=sleeps.append) as client,
    ):
        client.get_json("https://api.example.com/v1/data")

    assert calls == 2
    assert len(sleeps) == 1
    assert excinfo.value.code == UPSTREAM_UNAVAILABLE
    assert excinfo.value.retryable is True


def test_invalid_json_maps_to_upstream_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(SeoError) as excinfo, _client(handler) as client:
        client.get_json("https://api.example.com/v1/data")

    assert excinfo.value.code == UPSTREAM_UNAVAILABLE
    assert "api.example.com/v1/data" in excinfo.value.message


def test_error_never_leaks_query_values_or_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(SeoError) as excinfo, _client(handler, max_retries=0) as client:
        client.get_json(
            "https://api.example.com/v1/search?token=QUERY-SECRET&q=private",
            headers={
                "Authorization": "Bearer HEADER-SECRET",
                "X-Api-Key": "KEY-SECRET",
            },
        )

    error = excinfo.value
    rendered = f"{error.message} {error.detail!r}"
    assert "QUERY-SECRET" not in rendered
    assert "HEADER-SECRET" not in rendered
    assert "KEY-SECRET" not in rendered
    assert "?" not in error.message
    assert "api.example.com/v1/search" in error.message


def test_default_transport_pins_ipv4() -> None:
    client = HttpClient()
    try:
        transport = client._client._transport
        assert isinstance(transport, httpx.HTTPTransport)
        assert transport._pool._local_address == "0.0.0.0"
    finally:
        client.close()


def test_injected_transport_is_used() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    with HttpClient(transport=transport) as client:
        assert client._client._transport is transport
        assert client.get_json("https://api.example.com/") == {}


def test_context_manager_closes_client() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    with HttpClient(transport=transport) as client:
        assert client._client.is_closed is False
    assert client._client.is_closed is True
