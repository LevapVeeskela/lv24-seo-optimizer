"""Unit tests for multi-engine fusion (Wave 4, WP4.4)."""

from __future__ import annotations

import pytest

from lv24_seo.analysis.fusion import (
    CAVEAT_METRIKA_SESSIONS_NOT_CLICKS,
    CAVEAT_QUERY_SAMPLING,
    CAVEAT_YANDEX_NO_QUERY_PAGE_JOIN,
    CAVEAT_YANDEX_TOP_3000_QUERIES,
    EngineSummary,
    FusionReport,
    fuse_engines,
)


def test_single_engine_gsc_summary_totals_and_no_caveats() -> None:
    gsc = [
        {
            "query": "seo",
            "page": "https://example.com/a",
            "clicks": 10,
            "impressions": 100,
            "ctr": 0.1,
            "position": 3.0,
        },
        {
            "query": "seo tools",
            "page": "https://example.com/b",
            "clicks": 5,
            "impressions": 50,
            "ctr": 0.1,
            "position": 5.0,
        },
    ]

    report = fuse_engines(gsc=gsc)

    assert [engine.engine for engine in report.engines] == ["gsc"]
    summary = report.engines[0]
    assert summary.clicks == 15.0
    assert summary.impressions == 150.0
    assert summary.ctr == pytest.approx(0.1)
    assert summary.position == pytest.approx((3.0 * 100 + 5.0 * 50) / 150)
    assert summary.rows == 2
    assert summary.limitations == ()
    assert report.caveats == ()
    assert report.page_totals == {
        "https://example.com/a": {"clicks": 10.0, "impressions": 100.0},
        "https://example.com/b": {"clicks": 5.0, "impressions": 50.0},
    }
    assert report.query_totals == {
        "seo": {"clicks": 10.0, "impressions": 100.0},
        "seo tools": {"clicks": 5.0, "impressions": 50.0},
    }


def test_two_engines_sum_overlapping_queries_and_emit_caveats() -> None:
    gsc = [{"query": "seo", "page": "/a", "clicks": 10, "impressions": 100, "position": 2.0}]
    yandex = [{"query": "seo", "clicks": 4, "shows": 40, "position": 6.0}]

    report = fuse_engines(gsc=gsc, yandex=yandex)

    assert [engine.engine for engine in report.engines] == ["gsc", "yandex"]
    assert report.query_totals == {"seo": {"clicks": 14.0, "impressions": 140.0}}
    assert report.page_totals == {"/a": {"clicks": 10.0, "impressions": 100.0}}
    assert report.caveats == (
        CAVEAT_QUERY_SAMPLING,
        CAVEAT_YANDEX_NO_QUERY_PAGE_JOIN,
        CAVEAT_YANDEX_TOP_3000_QUERIES,
    )

    yandex_summary = report.engines[1]
    assert yandex_summary.clicks == 4.0
    assert yandex_summary.impressions == 40.0
    assert yandex_summary.ctr == pytest.approx(0.1)
    assert yandex_summary.position == 6.0
    assert yandex_summary.rows == 1
    assert yandex_summary.limitations == ("top_3000_queries", "no_query_page_join")


def test_yandex_rows_never_contribute_to_page_totals_even_with_a_page_field() -> None:
    yandex = [
        {"query": "seo", "page": "https://example.com/a", "clicks": 4, "shows": 40, "position": 6.0}
    ]

    report = fuse_engines(yandex=yandex)

    assert report.page_totals == {}
    assert report.query_totals == {"seo": {"clicks": 4.0, "impressions": 40.0}}


def test_metrika_sessions_are_page_visits_not_clicks_and_never_queries() -> None:
    gsc = [{"query": "seo", "page": "/a", "clicks": 3, "impressions": 30}]
    metrika = [{"page": "/a", "visits": 11}, {"page": "/b", "visits": 7}]

    report = fuse_engines(gsc=gsc, metrika=metrika)

    assert report.page_totals == {
        "/a": {"clicks": 3.0, "impressions": 30.0, "visits": 11.0},
        "/b": {"visits": 7.0},
    }
    assert report.query_totals == {"seo": {"clicks": 3.0, "impressions": 30.0}}
    assert report.caveats == (CAVEAT_QUERY_SAMPLING, CAVEAT_METRIKA_SESSIONS_NOT_CLICKS)

    metrika_summary = report.engines[1]
    assert metrika_summary.engine == "metrika"
    assert metrika_summary.clicks == 0.0
    assert metrika_summary.impressions is None
    assert metrika_summary.ctr is None
    assert metrika_summary.position is None
    assert metrika_summary.rows == 2
    assert metrika_summary.limitations == ("sessions_not_clicks",)


def test_metrika_accepts_start_url_path_as_page_key() -> None:
    metrika = [{"startURLPath": "/a", "visits": 5}]

    report = fuse_engines(metrika=metrika)

    assert report.page_totals == {"/a": {"visits": 5.0}}
    assert report.query_totals == {}
    assert report.caveats == (CAVEAT_METRIKA_SESSIONS_NOT_CLICKS,)


def test_missing_impressions_stay_none_and_do_not_create_zero_metrics() -> None:
    gsc = [{"query": "seo", "page": "/a", "clicks": 2, "position": 4.0}]
    yandex = [{"query": "seo", "clicks": 3}]

    report = fuse_engines(gsc=gsc, yandex=yandex)

    gsc_summary = report.engines[0]
    assert gsc_summary.clicks == 2.0
    assert gsc_summary.impressions is None
    assert gsc_summary.ctr is None
    assert gsc_summary.position == 4.0

    yandex_summary = report.engines[1]
    assert yandex_summary.clicks == 3.0
    assert yandex_summary.impressions is None
    assert yandex_summary.ctr is None
    assert yandex_summary.position is None

    assert report.page_totals == {"/a": {"clicks": 2.0}}
    assert report.query_totals == {"seo": {"clicks": 5.0}}


def test_blank_and_unparsable_dimensions_are_skipped() -> None:
    gsc = [
        {"query": "  ", "page": "  ", "clicks": 5, "impressions": 50},
        {"query": None, "page": None, "clicks": 1, "impressions": 10},
        {"query": "seo", "page": "/a", "clicks": "bad", "impressions": "10"},
    ]

    report = fuse_engines(gsc=gsc)

    assert report.page_totals == {"/a": {"impressions": 10.0}}
    assert report.query_totals == {"seo": {"impressions": 10.0}}
    # Engine totals still count every row; only the per-key joins skip blank dimensions.
    assert report.engines[0].clicks == 6.0
    assert report.engines[0].impressions == 70.0


def test_numeric_strings_are_parsed() -> None:
    gsc = [{"query": "seo", "page": "/a", "clicks": "2", "impressions": "10", "position": "3.5"}]
    yandex = [{"query": "seo", "clicks": "4", "shows": "40", "position": "6.5"}]

    report = fuse_engines(gsc=gsc, yandex=yandex)

    assert report.query_totals == {"seo": {"clicks": 6.0, "impressions": 50.0}}
    assert report.engines[0].ctr == pytest.approx(0.2)
    assert report.engines[1].ctr == pytest.approx(0.1)
    assert report.engines[1].position == 6.5


def test_totals_are_ordered_by_clicks_then_key() -> None:
    gsc = [
        {"query": "b", "page": "/b", "clicks": 5, "impressions": 50},
        {"query": "a", "page": "/a", "clicks": 5, "impressions": 50},
        {"query": "c", "page": "/c", "clicks": 9, "impressions": 90},
    ]

    report = fuse_engines(gsc=gsc)

    assert list(report.page_totals) == ["/c", "/a", "/b"]
    assert list(report.query_totals) == ["c", "a", "b"]


def test_repeated_rows_are_summed_per_page_and_query() -> None:
    gsc = [
        {"query": "seo", "page": "/a", "clicks": 2, "impressions": 20},
        {"query": "seo", "page": "/a", "clicks": 3, "impressions": 30},
    ]

    report = fuse_engines(gsc=gsc)

    assert report.page_totals == {"/a": {"clicks": 5.0, "impressions": 50.0}}
    assert report.query_totals == {"seo": {"clicks": 5.0, "impressions": 50.0}}
    assert report.engines[0].rows == 2


def test_empty_input_returns_an_empty_report() -> None:
    report = fuse_engines()

    assert isinstance(report, FusionReport)
    assert report.engines == ()
    assert report.page_totals == {}
    assert report.query_totals == {}
    assert report.caveats == ()
    assert isinstance(fuse_engines(gsc=[], yandex=[], metrika=[]), FusionReport)


def test_engine_summary_is_frozen() -> None:
    summary = EngineSummary(
        engine="gsc",
        clicks=1.0,
        impressions=None,
        ctr=None,
        position=None,
        rows=1,
        limitations=(),
    )

    with pytest.raises(AttributeError):
        summary.clicks = 2.0  # type: ignore[misc]
