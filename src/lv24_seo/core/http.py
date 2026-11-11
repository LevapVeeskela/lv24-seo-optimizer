"""Shared synchronous HTTP client used by every provider adapter.

Why this module exists
----------------------
Every provider talks to a different API but shares the same failure surface: auth errors,
quotas, transient 5xx and flaky networks. Centralising the retry and redaction rules keeps
:class:`~lv24_seo.core.errors.SeoError` codes and messages consistent for the CLI, MCP and
reports.

Design notes
------------
* IPv4 is forced with ``httpx.HTTPTransport(local_address="0.0.0.0")``. Some VPS images have
  no IPv6 route: for a host that publishes both ``A`` and ``AAAA`` records the resolver may
  pick the unreachable ``AAAA`` first and fail with ``ENETUNREACH``. Binding to the IPv4 any
  address makes httpx/httpcore use IPv4 only.
* ``GET``/``HEAD`` and explicitly marked read POSTs are retried. Retrying a write could
  duplicate a submission and burn provider quota (see ``core/write_gate.py``).
* Error messages contain ``host + path`` only. Query strings often carry API keys
  (``?key=...``) and headers carry bearer tokens, so neither ever reaches an error message.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any

import httpx

from lv24_seo.core.errors import (
    AUTH_INVALID,
    INVALID_INPUT,
    PROPERTY_NOT_FOUND,
    QUOTA_EXCEEDED,
    UPSTREAM_UNAVAILABLE,
    SeoError,
)
from lv24_seo.core.ratelimit import Limiter

DEFAULT_USER_AGENT = "lv24-seo/0.1 (+https://github.com/LevapVeeskela/lv24-seo-optimizer)"

_LOGIN_NEXT_ACTION = "lv24-seo auth login"

# Architecture §7: retries are capped at three attempts in total.
_MAX_ATTEMPTS = 3
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})
_BACKOFF_BASE_SECONDS = 0.5
_MAX_RETRY_AFTER_SECONDS = 30.0
_JITTER_RATIO = 0.2
_RETRYABLE_STATUSES = frozenset({429})
_DEFAULT_LIMITER = Limiter()


class HttpClient:
    """Thin httpx wrapper with retries, IPv4 pinning and :class:`SeoError` mapping."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
        headers: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        limiter: Limiter | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")

        # ``max_retries`` is a retry budget; the total attempt count never exceeds _MAX_ATTEMPTS.
        self._max_attempts = max(1, min(max_retries, _MAX_ATTEMPTS - 1) + 1)
        self._timeout_seconds = timeout
        self._sleep = sleep
        self._limiter = limiter if limiter is not None else _DEFAULT_LIMITER
        self._base_url = httpx.URL(base_url) if base_url else None

        merged_headers: dict[str, str] = {"User-Agent": user_agent}
        if headers:
            merged_headers.update(headers)

        if transport is None:
            # See the module docstring: many VPS hosts are IPv4-only.
            transport = httpx.HTTPTransport(local_address="0.0.0.0")  # noqa: S104

        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
            headers=merged_headers,
            transport=transport,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        retry_post: bool = False,
        rate_limit_key: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a request, retrying transient failures for idempotent methods.

        ``retry_post`` opts a *safe read* POST (GSC ``searchAnalytics/query``,
        CrUX ``queryRecord``) into the same 429/5xx + ``Retry-After`` retry path
        as ``GET``/``HEAD``. Write POSTs (submit, recrawl)
        must keep ``retry_post=False`` so one call can never become two side
        effects. ``rate_limit_key`` acquires one token from the attached
        limiter (see :mod:`lv24_seo.core.ratelimit`) before every attempt.
        """
        verb = method.upper()
        idempotent = verb in _IDEMPOTENT_METHODS or (verb == "POST" and retry_post)
        attempt = 1

        while True:
            if rate_limit_key is not None:
                self._limiter.acquire(rate_limit_key)
            try:
                response = self._client.request(verb, url, **kwargs)
            except httpx.InvalidURL as exc:
                raise SeoError(
                    code=INVALID_INPUT,
                    message="invalid request URL",
                    next_action="check the URL and retry",
                    detail={"method": verb},
                ) from exc
            except httpx.TransportError as exc:
                if idempotent and attempt < self._max_attempts:
                    self._sleep(self._backoff(attempt - 1))
                    attempt += 1
                    continue
                raise self._network_error(url, exc) from exc

            if response.is_error:
                if (
                    idempotent
                    and attempt < self._max_attempts
                    and _is_retryable_status(response.status_code)
                ):
                    delay = self._retry_delay(response, attempt - 1)
                    response.close()
                    self._sleep(delay)
                    attempt += 1
                    continue

                error = _status_error(
                    response.status_code,
                    method=verb,
                    target=_describe_url(response.request.url),
                )
                response.close()
                raise error

            return response

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        rate_limit_key: str | None = None,
    ) -> Any:
        """GET ``url`` and decode the body as JSON."""
        response = self.request(
            "GET", url, params=params, headers=headers, rate_limit_key=rate_limit_key
        )
        return _decode_json(response)

    def post_json(
        self,
        url: str,
        *,
        json: Any = None,
        data: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = False,
        rate_limit_key: str | None = None,
    ) -> Any:
        """POST to ``url`` and decode the body as JSON.

        Retries are opt-in via ``retry=True`` for safe *read* POSTs only
        (GSC searchAnalytics, CrUX queryRecord). Write POSTs keep the default
        ``retry=False`` so a submission is never duplicated by the transport.
        """
        response = self.request(
            "POST",
            url,
            json=json,
            data=data,
            params=params,
            headers=headers,
            retry_post=retry,
            rate_limit_key=rate_limit_key,
        )
        return _decode_json(response)

    def close(self) -> None:
        """Close the underlying connection pool. Safe to call more than once."""
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _backoff(self, retry_index: int) -> float:
        base: float = _BACKOFF_BASE_SECONDS * (2.0**retry_index)
        # Jitter only spreads retries from concurrent workers; it is not security-sensitive.
        jitter: float = random.uniform(  # noqa: S311
            1.0 - _JITTER_RATIO, 1.0 + _JITTER_RATIO
        )
        return base * jitter

    def _retry_delay(self, response: httpx.Response, retry_index: int) -> float:
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            return min(retry_after, _MAX_RETRY_AFTER_SECONDS)
        return self._backoff(retry_index)

    def _network_error(self, url: str, exc: httpx.TransportError) -> SeoError:
        request = getattr(exc, "request", None)
        target = (
            _describe_url(request.url)
            if isinstance(request, httpx.Request)
            else _describe_request(self._base_url, url)
        )
        # Never interpolate ``exc``: httpx messages embed the full URL including the query.
        if isinstance(exc, httpx.TimeoutException):
            message = f"request timed out after {self._timeout_seconds:g}s for {target}"
        else:
            message = f"network error ({type(exc).__name__}) for {target}"
        return SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=message,
            retryable=True,
            detail={"target": target, "error_type": type(exc).__name__},
        )


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUSES or 500 <= status_code <= 599


def _decode_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        target = _describe_url(response.request.url)
        raise SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"invalid JSON response from {target}",
            retryable=False,
            detail={"status_code": response.status_code, "target": target},
        ) from exc


def _status_error(status_code: int, *, method: str, target: str) -> SeoError:
    detail = {"status_code": status_code, "method": method, "target": target}

    if status_code == 401:
        return SeoError(
            code=AUTH_INVALID,
            message=f"authentication failed (HTTP 401) for {target}",
            next_action=_LOGIN_NEXT_ACTION,
            retryable=False,
            detail=detail,
        )
    if status_code == 403:
        return SeoError(
            code=AUTH_INVALID,
            message=f"access denied (HTTP 403) for {target}",
            retryable=False,
            detail=detail,
        )
    if status_code == 404:
        return SeoError(
            code=PROPERTY_NOT_FOUND,
            message=f"property not found (HTTP 404) for {target}",
            retryable=False,
            detail=detail,
        )
    if status_code == 429:
        return SeoError(
            code=QUOTA_EXCEEDED,
            message=f"quota exceeded (HTTP 429) for {target}",
            retryable=True,
            detail=detail,
        )
    if status_code >= 500:
        return SeoError(
            code=UPSTREAM_UNAVAILABLE,
            message=f"upstream unavailable (HTTP {status_code}) for {target}",
            retryable=True,
            detail=detail,
        )
    return SeoError(
        code=INVALID_INPUT,
        message=f"request rejected (HTTP {status_code}) for {target}",
        retryable=False,
        detail=detail,
    )


def _parse_retry_after(value: str | None) -> float | None:
    """Parse ``Retry-After`` as delay-seconds or an HTTP-date; ``None`` when unusable."""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    if seconds is not None:
        return max(0.0, seconds) if math.isfinite(seconds) else None

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - time.time())


def _describe_url(url: httpx.URL) -> str:
    """Return ``host + path`` only: never query, userinfo or port."""
    return f"{url.host or '<unknown-host>'}{url.path or '/'}"


def _describe_request(base_url: httpx.URL | None, url: str) -> str:
    try:
        parsed = httpx.URL(url)
        if base_url is not None and not parsed.host:
            parsed = base_url.join(url)
    except (httpx.InvalidURL, TypeError):
        return "<invalid-url>"
    return _describe_url(parsed)
