"""Offline tests for the quota-aware indexing engine (no provider writes)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lv24_seo import indexing as indexing_module
from lv24_seo.core.config import (
    BingConfig,
    GscConfig,
    IndexingConfig,
    ProjectConfig,
    ProvidersConfig,
    SeoConfig,
    YandexWebmasterConfig,
)
from lv24_seo.core.errors import WRITE_DISABLED, SeoError
from lv24_seo.core.timeutil import utc_now
from lv24_seo.indexing import (
    BING_NO_PER_URL_INDEX_STATUS,
    GSC_INDEXED_COUNT_DEPRECATED,
    INDEXNOW_NO_STATUS_API,
    NO_PER_URL_INDEX_COUNT,
    SITE_SCAN_NO_API,
    WRITE_SECRET_MISSING,
    YANDEX_NO_PER_URL_INDEX_STATUS,
    IndexingEngine,
    IndexingTarget,
    Queue,
    QueueItem,
    build_queue,
    gsc_index_status,
    plan_provider,
)
from lv24_seo.providers.base import ProviderResult
from lv24_seo.store import Store

SITE = "https://example.com"
HOST_ID = "https:example.com:443"
PROPERTY = "sc-domain:example.com"
URL_A = "https://example.com/a"
URL_B = "https://example.com/b"
URL_C = "https://example.com/c"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def _result(source: str, rows: list[dict[str, Any]], **kwargs: Any) -> ProviderResult:
    return ProviderResult(
        source=source,
        property_id=kwargs.pop("property_id", SITE),
        fetched_at=utc_now(),
        data_age_days=0,
        rows=rows,
        **kwargs,
    )


class FakeBing:
    def __init__(
        self,
        *,
        daily: int = 10,
        monthly: int = 100,
        crawled: dict[str, str | None] | None = None,
        fail: bool = False,
    ) -> None:
        self.daily = daily
        self.monthly = monthly
        self.crawled = crawled or {}
        self.fail = fail
        self.submit_calls: list[list[str]] = []
        self.url_info_calls: list[str] = []

    def submission_quota(self) -> ProviderResult:
        return _result(
            "bing",
            [{"daily_remaining": self.daily, "monthly_remaining": self.monthly, "raw": {}}],
            limitations=(
                "quota_monthly_can_bind_first",
                "quota_monthly_reset_undocumented",
            ),
        )

    def url_info(self, url: str) -> ProviderResult:
        self.url_info_calls.append(url)
        return _result(
            "bing",
            [
                {
                    "url": url,
                    "http_status": 200,
                    "last_crawled_date": self.crawled.get(url),
                    "discovery_date": None,
                }
            ],
        )

    def submit_url_batches(self, urls: list[str], *, batch_size: int = 500) -> ProviderResult:
        self.submit_calls.append(list(urls))
        if self.fail:
            raise SeoError(code="UPSTREAM_UNAVAILABLE", message="bing is down")
        return _result(
            "bing",
            [
                {
                    "accepted": len(urls),
                    "submitted": len(urls),
                    "batches": 1,
                    "batch_size": batch_size,
                    "duplicates_removed": 0,
                }
            ],
            limitations=("submit_idempotency_undocumented",),
        )


class FakeGsc:
    def __init__(self, verdicts: dict[str, str] | None = None, indexed_total: int = 0) -> None:
        self.verdicts = verdicts or {}
        self.indexed_total = indexed_total
        self.inspect_calls: list[str] = []

    def inspect_url(self, url: str, *, language_code: str = "en-US") -> ProviderResult:
        self.inspect_calls.append(url)
        verdict = self.verdicts.get(url, "PASS")
        coverage = {
            "PASS": "Submitted and indexed",
            "NEUTRAL": "Crawled - currently not indexed",
            "FAIL": "Excluded",
        }.get(verdict, "Unknown")
        return _result(
            "gsc",
            [
                {
                    "url": url,
                    "verdict": verdict,
                    "coverage_state": coverage,
                    "indexing_state": "INDEXING_ALLOWED",
                    "last_crawl_time": "2026-11-15T00:00:00+00:00",
                }
            ],
            limitations=("one_url_per_call",),
        )

    def list_sitemaps(self) -> ProviderResult:
        return _result(
            "gsc",
            [
                {
                    "path": "https://example.com/sitemap.xml",
                    "contents": [
                        {"type": "web", "submitted": 10, "indexed": self.indexed_total},
                    ],
                }
            ],
            limitations=("sitemap_indexed_count_deprecated",),
        )


class FakeYandex:
    def __init__(self, *, remainder: int = 10, fail_urls: set[str] | None = None) -> None:
        self.remainder = remainder
        self.fail_urls = fail_urls or set()
        self.enqueue_calls: list[str] = []

    def recrawl_quota(self, host_id: str) -> ProviderResult:
        return _result(
            "yandex_webmaster",
            [{"daily_quota": 10, "quota_remainder": self.remainder, "raw": {}}],
            property_id=host_id,
        )

    def recrawl_enqueue(self, host_id: str, url: str) -> ProviderResult:
        self.enqueue_calls.append(url)
        if url in self.fail_urls:
            raise SeoError(code="UPSTREAM_UNAVAILABLE", message="temporary failure")
        return _result(
            "yandex_webmaster",
            [{"url": url, "status": "queued", "task_id": "task-1"}],
            property_id=host_id,
            limitations=("one_url_per_call",),
        )


def _config(**indexing: Any) -> SeoConfig:
    values: dict[str, Any] = {"enabled": True}
    values.update(indexing)
    return SeoConfig(
        project=ProjectConfig(name="Example", base_url=SITE),
        providers=ProvidersConfig(
            gsc=GscConfig(property=PROPERTY),
            yandex_webmaster=YandexWebmasterConfig(host_id=HOST_ID),
            bing=BingConfig(
                site=SITE,
                indexnow_key="abc12345",
                indexnow_key_location="https://example.com/abc12345.txt",
            ),
        ),
        indexing=IndexingConfig(**values),
    )


def _targets() -> list[IndexingTarget]:
    return [
        IndexingTarget("bing", SITE, "bing"),
        IndexingTarget("yandex", HOST_ID, "yandex_webmaster"),
        IndexingTarget("gsc", PROPERTY, "gsc"),
        IndexingTarget("indexnow", "example.com"),
    ]


def _engine(
    store: Store,
    clients: dict[str, object],
    *,
    config: SeoConfig | None = None,
    allow_write: bool = True,
    force: bool = False,
    max_submit: int | None = None,
    max_inspect: int | None = None,
    secret: str = "unit-test-write-secret",
) -> IndexingEngine:
    def client_for(name: str) -> object:
        if name in clients:
            return clients[name]
        return clients["yandex" if name == "yandex_webmaster" else name]

    return IndexingEngine(
        config=config or _config(),
        store=store,
        targets=_targets(),
        client_for=client_for,
        allow_write=allow_write,
        force=force,
        max_submit=max_submit,
        max_inspect=max_inspect,
        write_secret=secret,
    )


def _queue(*urls: str, lastmod: datetime | None = None) -> Queue:
    return Queue(
        items=[QueueItem(url, lastmod) for url in urls],
        total=len(urls),
    )


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    instance = Store(tmp_path / "seo.db")
    try:
        yield instance
    finally:
        instance.close()


def test_build_queue_reads_sitemap_lastmod_and_filters_off_host() -> None:
    body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/a</loc><lastmod>2026-11-10</lastmod></url>"
        b"<url><loc>https://example.com/b</loc></url>"
        b"<url><loc>https://other.example/c</loc></url>"
        b"</urlset>"
    )

    from lv24_seo.crawl.models import FetchResult

    def fetch(url: str) -> FetchResult:
        return FetchResult(url=url, status=200, final_url=url, body=body)

    queue = build_queue(_config(), fetch=fetch)

    assert [item.url for item in queue.items] == [URL_A, URL_B]
    assert queue.items[0].lastmod == datetime(2026, 11, 10, tzinfo=UTC)
    assert queue.off_host == 0
    assert "sitemap_errors" in queue.limitations
    assert queue.sitemap_url == "https://example.com/sitemap.xml"


def test_plan_priority_is_never_submitted_then_lastmod_then_stale_check() -> None:
    states = {
        URL_A: {},
        URL_B: {
            "last_submitted_at": "2026-11-01T00:00:00+00:00",
            "last_checked_at": "2026-11-16T00:00:00+00:00",
        },
        URL_C: {
            "last_submitted_at": "2026-11-01T00:00:00+00:00",
            "last_checked_at": "2026-10-01T00:00:00+00:00",
        },
    }
    queue = [
        QueueItem(URL_B),
        QueueItem(URL_C),
        QueueItem(URL_A),
        QueueItem("https://example.com/d", datetime(2026, 11, 5, tzinfo=UTC)),
    ]
    plan = plan_provider(
        "bing",
        SITE,
        queue,
        states,
        config=_config().indexing,
        now=NOW,
        force=False,
        quota_before=None,
        quota_remaining=2,
        max_submit=10,
    )

    # A is never submitted; D has a lastmod newer than its submission; C is stale-check;
    # B was checked recently and is not due. C and B are skipped as submissions; B is also
    # skipped as a check.
    assert plan.submit == [URL_A, "https://example.com/d"]
    assert plan.skipped == 2
    assert plan.not_attempted == 0


def test_gsc_index_status_mapping_is_explicit() -> None:
    assert gsc_index_status("PASS", "Submitted and indexed") == "indexed"
    assert gsc_index_status(None, "Submitted and indexed") == "indexed"
    assert gsc_index_status("NEUTRAL", "Crawled - currently not indexed") == "not_indexed"
    assert gsc_index_status("FAIL", "Excluded") == "not_indexed"
    assert gsc_index_status("VERDICT_UNSPECIFIED", "Unknown") == "unknown"


def test_bing_checks_submitted_url_then_submits_with_quota_cap(store: Store) -> None:
    bing = FakeBing(daily=10, crawled={URL_B: "2026-11-15T00:00:00+00:00"})
    store.upsert_indexing_state(
        "bing",
        SITE,
        URL_B,
        last_submitted_at="2026-11-01T00:00:00+00:00",
        submit_status="accepted",
    )
    engine = _engine(store, {"bing": bing}, max_submit=1)

    result = engine.run(_queue(URL_A, URL_B), provider="bing")

    assert bing.url_info_calls == [URL_B]
    assert bing.submit_calls == [[URL_A]]
    outcome = result["providers"]["bing"]
    assert outcome["submitted"] == 1
    assert outcome["checked"] == 1
    assert outcome["status"] == "applied"
    assert store.get_indexing_state("bing", SITE, URL_A)["submit_status"] == "accepted"
    assert store.get_indexing_state("bing", SITE, URL_B)["index_status"] == "crawled"


def test_bing_unknown_crawl_stays_unknown_and_no_per_url_count(store: Store) -> None:
    bing = FakeBing(crawled={})
    store.upsert_indexing_state("bing", SITE, URL_A, last_submitted_at="2026-11-01T00:00:00+00:00")
    engine = _engine(store, {"bing": bing}, allow_write=False)

    engine.run(_queue(URL_A), provider="bing")
    status = engine.status(provider="bing", live=True)

    assert store.get_indexing_state("bing", SITE, URL_A)["index_status"] == "unknown"
    entry = status["providers"]["bing"]
    assert entry["indexed_total"] is None
    assert NO_PER_URL_INDEX_COUNT in entry["limitations"]
    assert SITE_SCAN_NO_API in entry["limitations"]
    assert BING_NO_PER_URL_INDEX_STATUS in entry["limitations"]
    assert entry["quota"]["reset"] is None
    assert "bing_quota_reset_undocumented" in status["limitations"]


def test_gsc_checks_and_deprecated_sitemap_aggregate(store: Store) -> None:
    gsc = FakeGsc(verdicts={URL_A: "PASS", URL_B: "NEUTRAL", URL_C: "FAIL"}, indexed_total=12)
    engine = _engine(store, {"gsc": gsc}, allow_write=False, max_inspect=3)

    engine.run(_queue(URL_A, URL_B, URL_C), provider="gsc")
    status = engine.status(provider="gsc", live=True)

    assert gsc.inspect_calls == [URL_A, URL_B, URL_C]
    assert store.get_indexing_state("gsc", PROPERTY, URL_A)["index_status"] == "indexed"
    assert store.get_indexing_state("gsc", PROPERTY, URL_B)["index_status"] == "not_indexed"
    assert store.get_indexing_state("gsc", PROPERTY, URL_C)["index_status"] == "not_indexed"
    entry = status["providers"]["gsc"]
    assert entry["inspected"] == 3
    assert entry["indexed"] == 1
    assert entry["not_indexed"] == 2
    assert entry["indexed_total"] == 12
    assert entry["indexed_total_source"] == "gsc_sitemaps_contents"
    assert GSC_INDEXED_COUNT_DEPRECATED in status["limitations"]


def test_status_reports_write_secret_presence_without_the_value(store: Store) -> None:
    engine = _engine(store, {"bing": FakeBing()}, allow_write=False, secret="")
    status = engine.status(provider="bing", live=False)

    assert status["write_secret_present"] is False
    assert WRITE_SECRET_MISSING in status["limitations"]
    assert "unit-test-write-secret" not in str(status)


def test_empty_bing_quota_is_quota_exhausted_not_success(store: Store) -> None:
    bing = FakeBing(daily=0, monthly=100)
    engine = _engine(store, {"bing": bing})

    result = engine.run(_queue(URL_A, URL_B), provider="bing")

    outcome = result["providers"]["bing"]
    assert bing.submit_calls == []
    assert outcome["submitted"] == 0
    assert outcome["planned"] == 0
    assert outcome["not_attempted"] == 2
    assert outcome["status"] == "quota_exhausted"
    assert "quota_exhausted" in outcome["limitations"]


def test_yandex_keeps_going_after_one_url_failure(store: Store) -> None:
    yandex = FakeYandex(remainder=10, fail_urls={URL_A})
    engine = _engine(store, {"yandex": yandex})

    result = engine.run(_queue(URL_A, URL_B), provider="yandex")

    outcome = result["providers"]["yandex"]
    assert yandex.enqueue_calls == [URL_A, URL_B]
    assert outcome["submitted"] == 1
    assert outcome["errors"] == 1
    assert outcome["status"] == "partial"
    assert store.get_indexing_state("yandex", HOST_ID, URL_A)["submit_status"] == "error"
    assert store.get_indexing_state("yandex", HOST_ID, URL_B)["submit_status"] == "accepted"
    assert YANDEX_NO_PER_URL_INDEX_STATUS in outcome["limitations"]
    assert NO_PER_URL_INDEX_COUNT in outcome["limitations"]


def test_idempotency_skips_submitted_url_and_force_resubmits(store: Store) -> None:
    store.upsert_indexing_state(
        "bing",
        SITE,
        URL_A,
        last_submitted_at="2026-11-10T00:00:00+00:00",
        submit_status="accepted",
    )
    bing = FakeBing()
    engine = _engine(store, {"bing": bing}, force=False)

    first = engine.run(_queue(URL_A), provider="bing")
    assert first["providers"]["bing"]["skipped"] == 1
    assert bing.submit_calls == []

    forced = _engine(store, {"bing": bing}, force=True)
    second = forced.run(_queue(URL_A), provider="bing")
    assert second["providers"]["bing"]["submitted"] == 1
    assert bing.submit_calls == [[URL_A]]


def test_yandex_quota_remainder_caps_submissions(store: Store) -> None:
    yandex = FakeYandex(remainder=1)
    engine = _engine(store, {"yandex": yandex}, max_submit=10)

    result = engine.run(_queue(URL_A, URL_B), provider="yandex")

    outcome = result["providers"]["yandex"]
    assert yandex.enqueue_calls == [URL_A]
    assert outcome["submitted"] == 1
    assert outcome["not_attempted"] == 1


def test_bing_monthly_quota_can_bind_before_daily(store: Store) -> None:
    bing = FakeBing(daily=100, monthly=1)
    engine = _engine(store, {"bing": bing}, max_submit=10)

    result = engine.run(_queue(URL_A, URL_B), provider="bing")

    assert bing.submit_calls == [[URL_A]]
    assert result["providers"]["bing"]["submitted"] == 1
    assert result["providers"]["bing"]["not_attempted"] == 1


def test_gsc_local_inspection_cap(store: Store) -> None:
    gsc = FakeGsc(verdicts={URL_A: "PASS", URL_B: "PASS"})
    config = _config(gsc_inspect_daily_limit=1)
    engine = _engine(store, {"gsc": gsc}, config=config, allow_write=False, max_inspect=10)

    result = engine.run(_queue(URL_A, URL_B), provider="gsc")

    assert gsc.inspect_calls == [URL_A]
    outcome = result["providers"]["gsc"]
    assert outcome["checked"] == 1
    assert outcome["not_attempted"] == 1
    assert store.get_quota_usage("gsc", PROPERTY, utc_now().date(), "inspect") == 1


def test_indexnow_local_daily_cap_and_no_status_api(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_indexnow(**kwargs: Any) -> ProviderResult:
        calls.append(list(kwargs["urls"]))
        return _result("indexnow", [{"submitted": len(kwargs["urls"]), "status": "accepted"}])

    monkeypatch.setattr(indexing_module, "indexnow_submit", fake_indexnow)
    config = _config(indexnow_daily_limit=1)
    engine = _engine(store, {}, config=config)

    result = engine.run(_queue(URL_A, URL_B), provider="indexnow")

    outcome = result["providers"]["indexnow"]
    assert calls == [[URL_A]]
    assert outcome["submitted"] == 1
    assert outcome["not_attempted"] == 1
    assert INDEXNOW_NO_STATUS_API in outcome["limitations"]
    assert NO_PER_URL_INDEX_COUNT in outcome["limitations"]
    assert store.get_quota_usage("indexnow", "example.com", utc_now().date(), "submit") == 1


def test_run_records_one_history_row_per_provider(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        indexing_module,
        "indexnow_submit",
        lambda **kwargs: _result(
            "indexnow", [{"submitted": len(kwargs["urls"]), "status": "accepted"}]
        ),
    )
    engine = _engine(
        store,
        {"bing": FakeBing(), "yandex": FakeYandex(), "gsc": FakeGsc()},
        max_submit=1,
        max_inspect=1,
    )

    engine.run(_queue(URL_A), provider="all")

    runs = store.list_indexing_runs(limit=10)
    assert {run["provider"] for run in runs} == {"bing", "yandex", "gsc", "indexnow"}
    assert all(run["finished_at"] for run in runs)


def test_replan_hash_change_stops_the_write(store: Store) -> None:
    class ShrinkingBing(FakeBing):
        def __init__(self) -> None:
            super().__init__(daily=10)
            self.quota_calls = 0

        def submission_quota(self) -> ProviderResult:
            self.quota_calls += 1
            self.daily = 10 if self.quota_calls == 1 else 0
            return super().submission_quota()

    bing = ShrinkingBing()
    engine = _engine(store, {"bing": bing})

    result = engine.run(_queue(URL_A), provider="bing")

    outcome = result["providers"]["bing"]
    assert bing.submit_calls == []
    assert outcome["submitted"] == 0
    assert outcome["status"] == "error"
    assert "CONFIRM_REQUIRED" in outcome["limitations"]
    assert store.get_indexing_state("bing", SITE, URL_A)["submit_status"] == "error"


def test_missing_write_secret_refuses_apply(store: Store) -> None:
    engine = _engine(store, {"bing": FakeBing()}, secret="")

    with pytest.raises(SeoError) as excinfo:
        engine.run(_queue(URL_A), provider="bing")

    assert excinfo.value.code == WRITE_DISABLED
