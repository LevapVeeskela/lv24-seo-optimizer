"""Contract tests for the provider result envelope."""

from __future__ import annotations

from datetime import UTC, datetime

from lv24_seo.providers.base import ProviderResult, data_age_days


def test_meta_carries_honesty_fields() -> None:
    fetched = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    result: ProviderResult[dict[str, object]] = ProviderResult(
        source="gsc",
        property_id="sc-domain:example.com",
        fetched_at=fetched,
        data_age_days=2,
        rows=[{"clicks": 1}],
        total=50000,
        truncated=True,
        limitations=("rows_cap_50000",),
    )
    meta = result.to_meta()
    assert meta["truncated"] is True
    assert meta["data_age_days"] == 2
    assert meta["limitations"] == ["rows_cap_50000"]
    assert meta["total"] == 50000
    assert result.empty is False


def test_empty_result_is_still_serialisable() -> None:
    result: ProviderResult[dict[str, object]] = ProviderResult(
        source="yandex_webmaster",
        property_id="https:example.com:443",
        fetched_at=datetime(2026, 11, 10, tzinfo=UTC),
        data_age_days=4,
    )
    payload = result.to_dict()
    assert payload["rows"] == []
    assert payload["meta"]["truncated"] is False
    assert payload["warnings"] == []
    assert result.empty is True


def test_data_age_never_negative() -> None:
    fetched = datetime(2026, 11, 10, tzinfo=UTC)
    assert data_age_days(end=datetime(2026, 11, 12, tzinfo=UTC), fetched_at=fetched) == 0
    assert data_age_days(end=datetime(2026, 11, 3, tzinfo=UTC), fetched_at=fetched) == 7
