# ruff: noqa: RUF001
"""Unit tests for presentation localization (``report/i18n.py``)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from lv24_seo.report.i18n import (
    _CATALOGS,
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    Translator,
    normalize_locale,
    translator,
)

NBSP = "\u00a0"
EN_DASH = "\u2013"
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_REQUIRED_KEYS = (
    "brand.name",
    "brand.suffix",
    "report.type.weekly",
    "report.type.indexing",
    "report.type.opportunities",
    "report.type.audit",
    "report.title",
    "report.preheader",
    "label.property",
    "label.period",
    "label.generated",
    "label.vs_previous",
    "kpi.clicks",
    "kpi.impressions",
    "kpi.ctr",
    "kpi.position",
    "kpi.previous",
    "kpi.unknown",
    "kpi.no_data",
    "kpi.no_comparison",
    "kpi.significant",
    "kpi.better",
    "kpi.worse",
    "kpi.flat",
    "headline.improving",
    "headline.worsening",
    "headline.no_change",
    "headline.no_comparison",
    "headline.no_data",
    "headline.partial",
    "section.opportunities",
    "section.issues",
    "section.sources",
    "section.notes",
    "section.warnings",
    "traffic.rows",
    "traffic.sessions",
    "traffic.no_rows",
    "engine.gsc",
    "engine.yandex_webmaster",
    "engine.yandex_metrika",
    "engine.bing",
    "engine.crux",
    "engine.psi",
    "source.rows",
    "source.missing",
    "source.kind.field",
    "source.kind.lab",
    "source.kind.traffic",
    "opportunity.kind.striking_distance",
    "opportunity.kind.ctr_gap",
    "opportunity.kind.decay",
    "opportunity.query",
    "opportunity.page",
    "opportunity.page_missing",
    "opportunity.position",
    "opportunity.impressions",
    "opportunity.expected_clicks",
    "opportunity.estimate",
    "opportunity.none",
    "opportunity.no_data",
    "issue.critical",
    "issue.high",
    "issue.medium",
    "issue.low",
    "issue.unknown",
    "issue.no_data",
    "issue.zero",
    "notes.original",
    "notes.estimates",
    "notes.other_limitation",
    "notes.source_totals",
    "notes.limitation.top_3000_queries",
    "notes.limitation.no_query_page_join",
    "notes.limitation.sessions_not_clicks",
    "notes.limitation.rows_cap_50000",
    "notes.limitation.sampled",
    "notes.limitation.low_traffic",
    "notes.limitation.degraded_host",
    "notes.limitation.one_url_per_call",
    "notes.limitation.no_per_url_sitemap_errors",
    "notes.limitation.token_expiring",
    "notes.limitation.paid_provider",
    "notes.limitation.partial_response",
    "warning.no_metrics",
    "warning.previous_no_metrics",
    "warning.no_provider_rows",
    "source.top_rows",
    "opportunity.top3",
    "warning.title",
    "footer.generated_by",
    "footer.property_id",
    "footer.license",
    "subject.weekly",
    "subject.no_data",
    "subject.no_comparison",
    "subject.critical",
    "subject.clicks",
    "subject.impressions",
    "subject.ctr",
    "subject.position",
    "unit.clicks",
    "unit.impressions",
    "unit.rows",
    "unit.sessions",
    "unit.positions",
    "unit.pp",
)

_PLURAL_KEYS = ("headline.significant", "issue.count", "issue.checks")
_PLURAL_FORMS = ("one", "few", "many", "other")


def test_public_constants() -> None:
    assert SUPPORTED_LOCALES == ("en", "ru", "zh")
    assert DEFAULT_LOCALE == "en"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "en"),
        ("", "en"),
        ("   ", "en"),
        ("en", "en"),
        ("EN-us", "en"),
        ("en_GB", "en"),
        ("ru", "ru"),
        (" RU ", "ru"),
        ("ru-RU", "ru"),
        ("ru_UA", "ru"),
        ("zh", "zh"),
        ("zh-CN", "zh"),
        ("zh_CN", "zh"),
        ("zh-SG", "zh"),
        ("zh-Hans", "zh"),
        ("zh-Hans-CN", "zh"),
        ("ZH-hans", "zh"),
        ("zh-TW", "en"),
        ("zh-HK", "en"),
        ("zh-Hant", "en"),
        ("zh-MO", "en"),
        ("de-DE", "en"),
        ("xx-YY", "en"),
        (123, "en"),
    ],
)
def test_normalize_locale_aliases(raw: object, expected: str) -> None:
    assert normalize_locale(raw) == expected  # type: ignore[arg-type]


def test_html_lang() -> None:
    assert translator("en").html_lang == "en"
    assert translator("ru").html_lang == "ru"
    assert translator("zh-CN").html_lang == "zh-Hans"
    assert translator("zh-TW").html_lang == "en"


def test_translator_constructor_normalizes_locale() -> None:
    assert Translator().locale == "en"
    assert Translator("ru-RU").locale == "ru"
    assert Translator("zh-Hans").html_lang == "zh-Hans"


def test_catalog_parity() -> None:
    reference = _CATALOGS[DEFAULT_LOCALE]
    for locale in SUPPORTED_LOCALES:
        catalog = _CATALOGS[locale]
        assert set(catalog) == set(reference), locale
        for key, value in catalog.items():
            assert value.strip(), (locale, key)
            assert set(_PLACEHOLDER_RE.findall(value)) == set(
                _PLACEHOLDER_RE.findall(reference[key])
            ), (locale, key)


def test_required_keys_exist() -> None:
    for locale in SUPPORTED_LOCALES:
        catalog = _CATALOGS[locale]
        for key in _REQUIRED_KEYS:
            assert catalog.get(key), (locale, key)
        for base in _PLURAL_KEYS:
            for form in _PLURAL_FORMS:
                assert catalog.get(f"{base}.{form}"), (locale, base, form)


def test_catalog_text_has_no_controls_or_emoji() -> None:
    for locale, catalog in _CATALOGS.items():
        for key, value in catalog.items():
            assert not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value), (
                locale,
                key,
            )
            assert not any(0x1F000 <= ord(char) <= 0x1FAFF for char in value), (
                locale,
                key,
            )
            assert not any(0x2600 <= ord(char) <= 0x27BF for char in value), (
                locale,
                key,
            )


def test_t_returns_localized_text() -> None:
    assert translator("en").t("kpi.clicks") == "Clicks"
    assert translator("ru").t("kpi.clicks") == "Клики"
    assert translator("zh").t("kpi.clicks") == "点击次数"


def test_t_unknown_key_returns_key() -> None:
    assert translator("ru").t("missing.key") == "missing.key"


def test_t_missing_localized_key_falls_back_to_english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(_CATALOGS["ru"], "kpi.clicks")
    assert Translator("ru").t("kpi.clicks") == "Clicks"


def test_t_missing_placeholder_is_empty() -> None:
    assert Translator("en").t("footer.property_id") == "Property ID: "


def test_t_parameters_are_literal_and_single_pass() -> None:
    assert Translator("en").t("footer.property_id", property_id="{x}") == "Property ID: {x}"


def test_t_strips_control_and_bidi_characters() -> None:
    value = "a\x00b\u202e\u2066c"
    assert Translator("en").t("footer.property_id", property_id=value) == "Property ID: abc"


def test_t_ignores_extra_parameters_and_renders_none_empty() -> None:
    assert Translator("en").t("kpi.clicks", unused="x") == "Clicks"
    assert Translator("en").t("footer.property_id", property_id=None) == "Property ID: "


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 проблем"),
        (1, "1 проблема"),
        (2, "2 проблемы"),
        (5, "5 проблем"),
        (11, "11 проблем"),
        (21, "21 проблема"),
        (22, "22 проблемы"),
        (25, "25 проблем"),
    ],
)
def test_russian_plural_forms(n: int, expected: str) -> None:
    assert Translator("ru").count("issue.count", n) == expected


def test_english_and_chinese_plural_forms() -> None:
    assert Translator("en").count("issue.count", 0) == "0 issues"
    assert Translator("en").count("issue.count", 1) == "1 issue"
    assert Translator("en").count("issue.count", 2) == "2 issues"
    assert Translator("en").count("issue.count", 1234) == "1,234 issues"
    assert Translator("zh").count("issue.count", 1) == "1 个问题"
    assert Translator("zh").count("issue.count", 5) == "5 个问题"


def test_plural_headline_and_checks() -> None:
    assert Translator("ru").count("headline.significant", 1) == (
        "1 показатель значительно изменился"
    )
    assert Translator("ru").count("headline.significant", 3) == (
        "3 показателя значительно изменились"
    )
    assert Translator("ru").count("headline.significant", 5) == (
        "5 показателей значительно изменилось"
    )
    # "checks" are unique rules, not individual findings.
    assert Translator("ru").count("issue.checks", 2) == "2 правила"
    assert Translator("en").count("issue.checks", 2) == "2 rules"
    assert Translator("zh").count("issue.checks", 2) == "2 条规则"
    assert Translator("zh").count("headline.significant", 3) == "3 项指标变化显著"


def test_count_unknown_key_returns_key() -> None:
    assert Translator("ru").count("missing.key", 3) == "missing.key"


def test_count_cannot_override_count_parameter() -> None:
    assert Translator("en").count("issue.count", 2, count="999") == "2 issues"


def test_number_formatting_all_locales() -> None:
    assert Translator("en").number(1234.5, 1) == "1,234.5"
    assert Translator("ru").number(1234.5, 1) == f"1{NBSP}234,5"
    assert Translator("zh").number(1234.5, 1) == "1,234.5"
    assert Translator("ru").number(1234567) == f"1{NBSP}234{NBSP}567"
    assert Translator("en").number("1234.5", 1) == "1,234.5"
    assert Translator("en").number(1.5, 10) == "1.5000000000"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.5, "1"),
        (1.5, "2"),
        (2.5, "3"),
        (-1.5, "-2"),
        (-0.4, "0"),
        (-0.0, "0"),
        (0.0, "0"),
    ],
)
def test_number_half_up_and_no_negative_zero(value: float, expected: str) -> None:
    assert Translator("en").number(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, float("nan"), float("inf"), float("-inf"), True, False, "", "abc", [1]],
)
def test_invalid_numbers_return_empty(value: object) -> None:
    assert Translator("en").number(value) == ""
    assert Translator("en").percent(value) == ""
    assert Translator("en").position(value) == ""


def test_percent_formatting_all_locales() -> None:
    assert Translator("en").percent(0.184, signed=True, scale="ratio") == "+18.4%"
    assert Translator("ru").percent(0.184, signed=True, scale="ratio") == f"+18,4{NBSP}%"
    assert Translator("zh").percent(0.184, signed=True, scale="ratio") == "+18.4%"


def test_percent_signs_scale_and_digits() -> None:
    en = Translator("en")
    assert en.percent(18.4) == "18.4%"
    assert en.percent(18.4, signed=True) == "+18.4%"
    assert en.percent(-18.4, signed=True) == "-18.4%"
    assert en.percent(0, signed=True) == "0.0%"
    assert en.percent(-0.001, digits=1) == "0.0%"
    assert en.percent(18.444, digits=2) == "18.44%"
    assert en.percent(1234.5) == "1,234.5%"
    assert Translator("ru").percent(0.184, signed=True, scale="ratio") == f"+18,4{NBSP}%"


def test_position_formatting() -> None:
    assert Translator("en").position(8.44) == "8.4"
    assert Translator("ru").position(8.44) == "8,4"
    assert Translator("zh").position(8.44) == "8.4"
    assert Translator("en").position(8.0) == "8"
    assert Translator("ru").position(8.0) == "8"
    assert Translator("zh").position(8.0) == "8"
    assert Translator("en").position(2.05) == "2.1"
    assert Translator("en").position(1234.5) == "1,234.5"


def test_date_formatting() -> None:
    assert Translator("en").date("2026-11-08") == "2026-11-08"
    assert Translator("ru").date("2026-11-08") == "08.11.2026"
    assert Translator("zh").date("2026-11-08") == "2026年11月8日"
    assert Translator("en").date(date(2026, 11, 8)) == "2026-11-08"
    assert Translator("en").date("2026-11-08T23:30:00-05:00") == "2026-11-09"
    assert Translator("en").date(None) == ""
    assert Translator("en").date("not-a-date") == ""


def test_datetime_formatting() -> None:
    value = "2026-11-08T06:05:00Z"
    # English uses a human date, not an ISO timestamp, in mail footers.
    assert Translator("en").datetime(value) == "8 Nov 2026, 06:05 UTC"
    assert Translator("ru").datetime(value) == "08.11.2026 06:05 UTC"
    assert Translator("zh").datetime(value) == "2026年11月8日 06:05 UTC"
    assert Translator("en").datetime("2026-11-08T09:00:00+03:00") == "8 Nov 2026, 06:00 UTC"
    assert Translator("en").datetime("2026-11-08") == "8 Nov 2026, 00:00 UTC"
    moment = datetime(2026, 11, 8, 6, 5, tzinfo=UTC)
    assert Translator("en").datetime(moment) == "8 Nov 2026, 06:05 UTC"
    assert Translator("en").datetime(None) == ""


def test_period_formatting() -> None:
    start, end = "2026-11-03", "2026-11-09"
    assert Translator("en").period(start, end) == f"3{EN_DASH}9 Nov 2026"
    assert Translator("ru").period(start, end) == f"3{EN_DASH}9 нояб. 2026"
    assert Translator("zh").period(start, end) == f"2026年11月3{EN_DASH}9日"
    assert Translator("en").period("2026-10-28", "2026-11-03") == (f"28 Oct {EN_DASH} 3 Nov 2026")
    assert Translator("ru").period("2026-10-28", "2026-11-03") == (
        f"28 окт. {EN_DASH} 3 нояб. 2026"
    )
    assert Translator("zh").period("2026-10-28", "2026-11-03") == (
        f"2026年10月28日{EN_DASH}11月3日"
    )
    assert Translator("en").period("2025-12-28", "2026-01-03") == (
        f"28 Dec 2025 {EN_DASH} 3 Jan 2026"
    )
    assert Translator("zh").period("2025-12-28", "2026-01-03") == (
        f"2025年12月28日{EN_DASH}2026年1月3日"
    )


def test_period_single_equal_reversed_and_invalid() -> None:
    en = Translator("en")
    assert en.period("2026-11-09", "2026-11-03") == f"3{EN_DASH}9 Nov 2026"
    assert en.period("2026-11-08", "2026-11-08") == "2026-11-08"
    assert en.period(None, "2026-11-08") == "2026-11-08"
    assert en.period("2026-11-08", None) == "2026-11-08"
    assert en.period(None, None) == ""
    assert en.period("bad", "2026-11-08") == "2026-11-08"
    assert en.period("bad", "worse") == ""


def test_short_period_formatting() -> None:
    start, end = "2026-11-03", "2026-11-09"
    assert Translator("en").short_period(start, end) == f"3{EN_DASH}9 Nov"
    assert Translator("ru").short_period(start, end) == f"3{EN_DASH}9 нояб."
    assert Translator("zh").short_period(start, end) == f"11月3{EN_DASH}9日"
    assert Translator("en").short_period("2026-10-28", "2026-11-03") == (f"28 Oct {EN_DASH} 3 Nov")
    assert Translator("zh").short_period("2026-10-28", "2026-11-03") == (
        f"10月28日{EN_DASH}11月3日"
    )
    assert Translator("en").short_period("2025-12-28", "2026-01-03") == (
        f"28 Dec 2025 {EN_DASH} 3 Jan 2026"
    )
    assert Translator("en").short_period("2026-11-08", "2026-11-08") == "8 Nov"
    assert Translator("en").short_period(None, "2026-11-08") == "8 Nov"
    assert Translator("en").short_period(None, None) == ""


def test_formatting_is_deterministic() -> None:
    for locale in SUPPORTED_LOCALES:
        first = translator(locale)
        second = translator(locale)
        assert first.locale == second.locale
        assert first.html_lang == second.html_lang
        assert first.number(1234.5, 1) == second.number(1234.5, 1)
        assert first.percent(0.184, signed=True, scale="ratio") == second.percent(
            0.184, signed=True, scale="ratio"
        )
        assert first.period("2026-11-03", "2026-11-09") == second.period("2026-11-03", "2026-11-09")
        assert first.count("issue.count", 21) == second.count("issue.count", 21)


def test_russian_severity_labels_agree_with_feminine_noun() -> None:
    ru = translator("ru")
    assert ru.t("issue.critical") == "Критические"
    assert ru.t("issue.high") == "Высокие"
    assert ru.t("issue.medium") == "Средние"
    assert ru.t("issue.low") == "Низкие"


def test_chinese_units_precede_the_number() -> None:
    zh = translator("zh")
    assert zh.t("unit.clicks") == "点击次数"
    assert zh.t("unit.impressions") == "展示次数"
    assert zh.t("unit.sessions") == "会话数"
    assert zh.t("unit.pp") == "个百分点"
    assert f"{zh.t('unit.clicks')} {zh.number(165)}" == "点击次数 165"


def test_estimate_note_and_new_catalog_copy() -> None:
    assert translator("en").t("notes.estimates") == (
        "Expected clicks are a prioritisation estimate, not a traffic promise."
    )
    assert translator("ru").t("notes.estimates") == (
        "Ожидаемые клики — оценка приоритета, а не гарантия трафика."
    )
    assert translator("zh").t("notes.estimates") == "预计点击次数为优先级预估，并非流量承诺。"
    assert translator("en").t("notes.other_limitation", code="new_code") == (
        "Other limitation (new_code)"
    )
    assert translator("ru").t("notes.other_limitation", code="new_code") == (
        "Другое ограничение (new_code)"
    )
    assert translator("zh").t("notes.other_limitation", code="new_code") == "其他限制（new_code）"
    assert translator("en").t("source.top_rows") == "top query rows"
    assert translator("en").t("opportunity.top3") == "Top 3 by expected clicks"
