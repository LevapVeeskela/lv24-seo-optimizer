"""Unit tests for the shared token-bucket rate limiter.

Every test drives time through :class:`FakeClock`; nothing here sleeps for real.
"""

from __future__ import annotations

import threading

import pytest

from lv24_seo.core.errors import INVALID_INPUT, QUOTA_EXCEEDED, SeoError
from lv24_seo.core.ratelimit import DEFAULT_LIMITS, BucketSpec, Limiter, TokenBucket


class FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_bucket(rate: float = 1.0, capacity: float = 1.0) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock()
    bucket = TokenBucket(BucketSpec(rate, capacity), clock=clock, sleep=clock.sleep)
    return bucket, clock


def make_limiter(
    limits: dict[str, BucketSpec] | None = None,
) -> tuple[Limiter, FakeClock]:
    clock = FakeClock()
    limiter = Limiter(limits, clock=clock, sleep=clock.sleep)
    return limiter, clock


def test_default_limits_match_documented_keys_and_values() -> None:
    assert set(DEFAULT_LIMITS) == {
        "gsc",
        "gsc.url_inspection",
        "yandex_webmaster",
        "yandex_metrika",
        "bing",
        "crux",
        "psi",
    }
    assert DEFAULT_LIMITS["gsc"] == BucketSpec(20.0, 1200.0)
    assert DEFAULT_LIMITS["gsc.url_inspection"] == BucketSpec(10.0, 600.0)
    assert DEFAULT_LIMITS["yandex_webmaster"] == BucketSpec(5.0, 100.0)
    assert DEFAULT_LIMITS["yandex_metrika"] == BucketSpec(0.65, 200.0)
    assert DEFAULT_LIMITS["bing"] == BucketSpec(5.0, 100.0)
    assert DEFAULT_LIMITS["crux"] == BucketSpec(2.5, 150.0)
    assert DEFAULT_LIMITS["psi"] == BucketSpec(25.0, 1000.0)


def test_bucket_starts_full_and_burst_drains_it() -> None:
    bucket, _ = make_bucket(rate=2.0, capacity=5.0)
    assert bucket.tokens == pytest.approx(5.0)

    granted = [bucket.try_acquire() for _ in range(6)]

    assert granted == [True, True, True, True, True, False]
    assert bucket.tokens == pytest.approx(0.0)


def test_refill_after_elapsed_time() -> None:
    bucket, clock = make_bucket(rate=2.0, capacity=5.0)
    for _ in range(5):
        assert bucket.try_acquire()

    clock.now += 0.5

    assert bucket.tokens == pytest.approx(1.0)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_refill_is_capped_at_capacity() -> None:
    bucket, clock = make_bucket(rate=1.0, capacity=3.0)
    assert bucket.try_acquire()

    clock.now += 10_000.0

    assert bucket.tokens == pytest.approx(3.0)


def test_acquire_returns_zero_when_tokens_are_available() -> None:
    bucket, clock = make_bucket(rate=1.0, capacity=1.0)

    assert bucket.acquire() == pytest.approx(0.0)
    assert clock.sleeps == []
    assert bucket.tokens == pytest.approx(0.0)


def test_acquire_waits_and_reports_seconds_waited() -> None:
    bucket, clock = make_bucket(rate=2.0, capacity=1.0)
    assert bucket.try_acquire()

    waited = bucket.acquire(timeout=1.0)

    assert waited == pytest.approx(0.5)
    assert clock.sleeps == [pytest.approx(0.5)]
    assert clock.now == pytest.approx(0.5)


def test_acquire_without_timeout_waits_until_available() -> None:
    bucket, clock = make_bucket(rate=1.0, capacity=2.0)
    assert bucket.try_acquire(cost=2.0)

    waited = bucket.acquire(cost=2.0)

    assert waited == pytest.approx(2.0)
    assert clock.now == pytest.approx(2.0)


def test_acquire_timeout_raises_quota_exceeded_with_wait_time() -> None:
    bucket, clock = make_bucket(rate=1.0, capacity=1.0)
    assert bucket.try_acquire()

    with pytest.raises(SeoError) as excinfo:
        bucket.acquire(cost=1.0, timeout=0.25)

    error = excinfo.value
    assert error.code == QUOTA_EXCEEDED
    assert error.retryable is True
    assert "1.000s" in error.message
    assert error.detail["retry_after_seconds"] == pytest.approx(1.0)
    assert error.detail["waited_seconds"] == pytest.approx(0.0)
    assert clock.sleeps == []
    assert bucket.tokens == pytest.approx(0.0)


def test_zero_timeout_is_a_non_blocking_probe() -> None:
    bucket, clock = make_bucket(rate=1.0, capacity=1.0)

    assert bucket.acquire(timeout=0.0) == pytest.approx(0.0)
    assert bucket.try_acquire() is False

    with pytest.raises(SeoError) as excinfo:
        bucket.acquire(timeout=0.0)

    assert excinfo.value.code == QUOTA_EXCEEDED
    assert clock.sleeps == []


def test_acquire_rejects_cost_above_capacity() -> None:
    bucket, _ = make_bucket(rate=1.0, capacity=2.0)

    with pytest.raises(SeoError) as excinfo:
        bucket.acquire(cost=3.0)

    assert excinfo.value.code == INVALID_INPUT
    assert "capacity" in excinfo.value.message


@pytest.mark.parametrize("cost", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_cost_is_rejected(cost: float) -> None:
    bucket, _ = make_bucket()

    with pytest.raises(SeoError) as excinfo:
        bucket.try_acquire(cost=cost)
    assert excinfo.value.code == INVALID_INPUT

    with pytest.raises(SeoError):
        bucket.acquire(cost=cost)


def test_negative_timeout_is_rejected() -> None:
    bucket, _ = make_bucket()

    with pytest.raises(SeoError) as excinfo:
        bucket.acquire(timeout=-1.0)

    assert excinfo.value.code == INVALID_INPUT


@pytest.mark.parametrize(
    ("rate", "capacity"),
    [(0.0, 1.0), (-1.0, 1.0), (float("inf"), 1.0), (1.0, 0.0), (1.0, float("nan"))],
)
def test_bucket_spec_rejects_invalid_values(rate: float, capacity: float) -> None:
    with pytest.raises(ValueError):
        BucketSpec(rate, capacity)


def test_independent_keys_do_not_share_tokens() -> None:
    limiter, _ = make_limiter()
    gsc = limiter.bucket("gsc")

    assert limiter.try_acquire("gsc", cost=1200.0) is True
    assert limiter.try_acquire("gsc") is False
    assert gsc.tokens == pytest.approx(0.0)

    assert limiter.try_acquire("bing") is True
    assert limiter.bucket("bing") is not gsc
    assert limiter.bucket("bing").tokens == pytest.approx(99.0)
    assert limiter.try_acquire("gsc.url_inspection") is True


def test_bucket_is_cached_per_key() -> None:
    limiter, _ = make_limiter()

    assert limiter.bucket("crux") is limiter.bucket("crux")
    assert limiter.bucket("crux") is not limiter.bucket("psi")


def test_unknown_key_raises_invalid_input() -> None:
    limiter, _ = make_limiter()

    with pytest.raises(SeoError) as excinfo:
        limiter.bucket("does-not-exist")
    error = excinfo.value
    assert error.code == INVALID_INPUT
    assert "does-not-exist" in error.message
    assert "gsc" in error.message

    with pytest.raises(SeoError) as excinfo:
        limiter.acquire("does-not-exist")
    assert excinfo.value.code == INVALID_INPUT

    with pytest.raises(SeoError) as excinfo:
        limiter.try_acquire("does-not-exist")
    assert excinfo.value.code == INVALID_INPUT


def test_custom_limits_replace_the_default_table() -> None:
    limiter, _ = make_limiter({"gsc": BucketSpec(1000.0, 10.0)})

    assert limiter.try_acquire("gsc", cost=10.0) is True
    assert limiter.try_acquire("gsc") is False
    with pytest.raises(SeoError) as excinfo:
        limiter.bucket("bing")
    assert excinfo.value.code == INVALID_INPUT


def test_limiter_propagates_timeout_as_quota_exceeded() -> None:
    limiter, clock = make_limiter({"gsc": BucketSpec(1.0, 1.0)})
    assert limiter.try_acquire("gsc")

    with pytest.raises(SeoError) as excinfo:
        limiter.acquire("gsc", timeout=0.1)
    assert excinfo.value.code == QUOTA_EXCEEDED
    assert excinfo.value.retryable is True

    assert limiter.acquire("gsc", timeout=2.0) == pytest.approx(1.0)
    assert clock.now == pytest.approx(1.0)


def test_metrika_window_budget_of_200_requests_per_5_minutes() -> None:
    limiter, clock = make_limiter()
    assert DEFAULT_LIMITS["yandex_metrika"].capacity == 200.0
    # 0.65 rps stays under the documented 200 req / 5 min ceiling (0.667 rps).

    granted = sum(1 for _ in range(201) if limiter.try_acquire("yandex_metrika"))

    assert granted == 200
    assert limiter.bucket("yandex_metrika").tokens == pytest.approx(0.0)

    clock.now += 400.0

    assert limiter.bucket("yandex_metrika").tokens == pytest.approx(200.0)


def test_thread_safety_smoke_ten_threads_ten_acquires() -> None:
    capacity = 1_000_000.0
    bucket = TokenBucket(BucketSpec(rate_per_second=1_000_000.0, capacity=capacity))
    waits: list[float] = []
    failures: list[BaseException] = []
    results_lock = threading.Lock()

    def worker() -> None:
        try:
            for _ in range(10):
                waited = bucket.acquire()
                with results_lock:
                    waits.append(waited)
        except BaseException as exc:  # pragma: no cover - only on a limiter bug
            with results_lock:
                failures.append(exc)

    threads = [threading.Thread(target=worker, name=f"rate-limit-{i}") for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(waits) == 100
    assert all(waited >= 0.0 for waited in waits)
    assert capacity - 100.0 <= bucket.tokens <= capacity
