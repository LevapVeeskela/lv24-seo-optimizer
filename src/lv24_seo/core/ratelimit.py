"""Token-bucket rate limiting shared by every provider client.

Why a token bucket
------------------
Provider quotas are expressed as a steady rate plus a burst budget (for example
Metrika allows 200 ``/stat/v1/data`` calls per 5 minutes). A token bucket smooths
the steady state while still allowing a short burst, so a weekly report stays fast
without tripping 429s.

Rules
-----
* One bucket per key (``gsc``, ``gsc.url_inspection``, ``yandex_metrika``, ...).
  Keys never share tokens; an unknown key is a caller bug, not a silent default.
* ``clock`` and ``sleep`` are injectable so tests never touch wall time.
* ``try_acquire`` never blocks. ``acquire`` returns the seconds it waited and
  raises :class:`~lv24_seo.core.errors.SeoError` (``QUOTA_EXCEEDED``) when the
  caller's ``timeout`` budget cannot cover the wait.
* Buckets are thread-safe: refill and consumption happen under one lock, and
  ``acquire`` sleeps outside that lock so ``try_acquire`` callers are not starved.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from lv24_seo.core.errors import INVALID_INPUT, QUOTA_EXCEEDED, SeoError

__all__ = ["DEFAULT_LIMITS", "BucketSpec", "Limiter", "TokenBucket"]

# Tolerance for float comparisons: "exactly refilled" must mean "one token available".
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class BucketSpec:
    """Refill rate (tokens per second) and maximum burst capacity for one key."""

    rate_per_second: float
    capacity: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.rate_per_second) or self.rate_per_second <= 0:
            raise ValueError(
                f"rate_per_second must be a positive finite number: {self.rate_per_second!r}"
            )
        if not math.isfinite(self.capacity) or self.capacity <= 0:
            raise ValueError(f"capacity must be a positive finite number: {self.capacity!r}")


# Conservative ceilings derived from the quota table in ``docs/data-sources.md``:
# * ``gsc``                - 1,200 queries/min per site and per user -> 20 rps, capacity = 1 min.
# * ``gsc.url_inspection`` - 600 inspections/min                    -> 10 rps, capacity = 1 min.
# * ``yandex_webmaster``   - no published rps; 5 rps conservative default, capacity 100.
# * ``yandex_metrika``     - ``/stat/v1/data`` 200 req / 5 min -> 0.65 rps (exact ratio 0.667
#                            with a safety margin), capacity 200 = the whole 5-minute window.
# * ``bing``               - no published rps; 5 rps conservative default, capacity 100.
# * ``crux``               - 150 queries/min                       -> 2.5 rps, capacity 150.
# * ``psi``                - 25 queries/s                          -> 25 rps, capacity 1000.
#
# Capacity is the size of the provider's burst window, so one burst can never exceed the
# documented window budget while the refill rate keeps long runs below the steady ceiling.
# Provider adapters with a tighter contract override this table via ``Limiter(limits=...)``.
DEFAULT_LIMITS: Mapping[str, BucketSpec] = MappingProxyType(
    {
        "gsc": BucketSpec(rate_per_second=20.0, capacity=1200.0),
        "gsc.url_inspection": BucketSpec(rate_per_second=10.0, capacity=600.0),
        "yandex_webmaster": BucketSpec(rate_per_second=5.0, capacity=100.0),
        "yandex_metrika": BucketSpec(rate_per_second=0.65, capacity=200.0),
        "bing": BucketSpec(rate_per_second=5.0, capacity=100.0),
        "crux": BucketSpec(rate_per_second=2.5, capacity=150.0),
        "psi": BucketSpec(rate_per_second=25.0, capacity=1000.0),
    }
)


class TokenBucket:
    """Thread-safe token bucket for a single key.

    The bucket starts full. ``acquire`` blocks (via the injected ``sleep``) until
    ``cost`` tokens are available or the ``timeout`` budget is exhausted.
    """

    def __init__(
        self,
        spec: BucketSpec,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._spec = spec
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = spec.capacity
        self._updated_at = clock()

    @property
    def tokens(self) -> float:
        """Tokens currently available, after refilling for the elapsed time."""
        with self._lock:
            self._refill_locked()
            return self._tokens

    def try_acquire(self, cost: float = 1.0) -> bool:
        """Take ``cost`` tokens if available, otherwise return ``False`` without waiting."""
        self._validate_cost(cost)
        with self._lock:
            self._refill_locked()
            if self._tokens + _EPSILON < cost:
                return False
            self._tokens = max(0.0, self._tokens - cost)
            return True

    def acquire(self, cost: float = 1.0, timeout: float | None = None) -> float:
        """Block until ``cost`` tokens are available and return the seconds waited.

        ``timeout`` is the maximum total wait in seconds (``None`` waits forever,
        ``0`` is a non-blocking probe). When the budget cannot cover the wait,
        ``SeoError(QUOTA_EXCEEDED, retryable=True)`` is raised and its message
        states how long the caller would need to wait.
        """
        self._validate_cost(cost)
        if cost > self._spec.capacity:
            raise SeoError(
                code=INVALID_INPUT,
                message=(
                    f"cost {cost:g} exceeds bucket capacity {self._spec.capacity:g}; "
                    "it can never be satisfied"
                ),
                next_action="split the batch or raise the bucket capacity",
            )
        if timeout is not None and timeout < 0:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"timeout must be >= 0, got {timeout!r}",
                next_action="pass timeout=None to wait, or a non-negative number of seconds",
            )

        started_at = self._clock()
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens + _EPSILON >= cost:
                    self._tokens = max(0.0, self._tokens - cost)
                    return max(0.0, self._clock() - started_at)
                wait_needed = (cost - self._tokens) / self._spec.rate_per_second

            if timeout is not None:
                waited = max(0.0, self._clock() - started_at)
                if waited + wait_needed > timeout + _EPSILON:
                    raise SeoError(
                        code=QUOTA_EXCEEDED,
                        message=(
                            f"rate limit reached: need {wait_needed:.3f}s more "
                            f"(timeout {timeout:.3f}s, waited {waited:.3f}s)"
                        ),
                        next_action="retry after the indicated delay or lower the request rate",
                        retryable=True,
                        detail={
                            "retry_after_seconds": round(wait_needed, 3),
                            "waited_seconds": round(waited, 3),
                        },
                    )

            self._sleep(wait_needed)

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._updated_at
        if elapsed > 0:
            self._tokens = min(
                self._spec.capacity,
                self._tokens + elapsed * self._spec.rate_per_second,
            )
            self._updated_at = now

    @staticmethod
    def _validate_cost(cost: float) -> None:
        if not math.isfinite(cost) or cost <= 0:
            raise SeoError(
                code=INVALID_INPUT,
                message=f"cost must be a positive finite number, got {cost!r}",
                next_action="use cost=1.0 unless one call spends several requests",
            )


class Limiter:
    """Lazily creates one :class:`TokenBucket` per key.

    ``limits`` replaces the default table when given (it is not merged), so tests
    and provider adapters can define exactly the keys they use. ``bucket_kwargs``
    are forwarded to every bucket, mainly for ``clock`` / ``sleep`` injection.
    """

    def __init__(
        self,
        limits: Mapping[str, BucketSpec] | None = None,
        **bucket_kwargs: Any,
    ) -> None:
        source = DEFAULT_LIMITS if limits is None else limits
        self._limits: dict[str, BucketSpec] = dict(source)
        self._bucket_kwargs = bucket_kwargs
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket(self, key: str) -> TokenBucket:
        """Return the bucket for ``key``, creating it on first use."""
        with self._lock:
            existing = self._buckets.get(key)
            if existing is not None:
                return existing
            spec = self._limits.get(key)
            if spec is None:
                raise self._unknown_key(key)
            bucket = self._create_bucket(spec)
            self._buckets[key] = bucket
            return bucket

    def acquire(self, key: str, cost: float = 1.0, timeout: float | None = None) -> float:
        """Wait for ``cost`` tokens from ``key`` and return the seconds waited."""
        return self.bucket(key).acquire(cost, timeout)

    def try_acquire(self, key: str, cost: float = 1.0) -> bool:
        """Take ``cost`` tokens from ``key`` if available, without waiting."""
        return self.bucket(key).try_acquire(cost)

    def _create_bucket(self, spec: BucketSpec) -> TokenBucket:
        return TokenBucket(spec, **self._bucket_kwargs)

    def _unknown_key(self, key: str) -> SeoError:
        known = ", ".join(sorted(self._limits))
        return SeoError(
            code=INVALID_INPUT,
            message=f"unknown rate-limit key {key!r}; known keys: {known}",
            next_action="use a key from DEFAULT_LIMITS or pass a custom limits mapping",
        )
