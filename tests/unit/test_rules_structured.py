"""Unit tests for Wave 3 rule group C: structured data, i18n and transport hygiene."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import PageData, RuleContext
from lv24_seo.crawl.rules import (
    load_rules,
    rule_duplicate_organization,
    rule_hreflang_reciprocal,
    rule_hreflang_target,
    rule_hreflang_x_default,
    rule_html_lang,
    rule_html_lang_mismatch,
    rule_jsonld_event,
    rule_jsonld_organization,
    rule_jsonld_required,
    rule_mixed_content,
)

_RULES = (
    rule_jsonld_required,
    rule_jsonld_organization,
    rule_jsonld_event,
    rule_hreflang_reciprocal,
    rule_hreflang_x_default,
    rule_hreflang_target,
    rule_html_lang,
    rule_html_lang_mismatch,
    rule_mixed_content,
    rule_duplicate_organization,
)

_EXPECTED_CONTRACT = {
    "jsonld-required": "high",
    "jsonld-organization": "low",
    "jsonld-event-required": "high",
    "hreflang-not-reciprocal": "high",
    "hreflang-missing-x-default": "medium",
    "hreflang-target-not-ok": "high",
    "html-lang-missing": "medium",
    "html-lang-mismatch": "medium",
    "mixed-content": "high",
    "jsonld-organization-duplicate": "low",
}


def _page(url: str = "https://example.com/", *, status: int = 200, **overrides: Any) -> PageData:
    final_url = overrides.pop("final_url", url)
    return PageData(url=url, status=status, final_url=final_url, **overrides)


def _ctx(*pages: PageData, site_url: str = "https://example.com/") -> RuleContext:
    return RuleContext(pages=list(pages), site_url=site_url)


def _hreflang_page(url: str, variants: list[tuple[str, str]]) -> PageData:
    return _page(
        url,
        hreflang=[{"lang": lang, "url": target} for lang, target in variants],
    )


def test_every_rule_exposes_the_expected_contract() -> None:
    actual = {module.RULE.id: module.RULE.severity for module in _RULES}
    assert actual == _EXPECTED_CONTRACT


def test_all_rules_are_discoverable() -> None:
    discovered = {rule.id for rule in load_rules()}
    assert set(_EXPECTED_CONTRACT) <= discovered


# --- jsonld-required -------------------------------------------------------


def test_jsonld_required_flags_missing_fields() -> None:
    page = _page(jsonld=[{"@type": "Article", "headline": "Hello"}])

    issues = rule_jsonld_required.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].rule_id == "jsonld-required"
    assert issues[0].severity == "high"
    assert issues[0].url == page.url
    assert issues[0].detail == {
        "type": "Article",
        "missing": ["datePublished", "author", "image"],
    }


def test_jsonld_required_clean() -> None:
    page = _page(
        jsonld=[
            {
                "@type": "NewsArticle",
                "headline": "Hello",
                "datePublished": "2026-01-01T09:00:00+03:00",
                "author": {"@type": "Person", "name": "Ada"},
                "image": "https://example.com/cover.png",
            }
        ]
    )

    assert rule_jsonld_required.RULE.run(_ctx(page)) == []


def test_jsonld_required_accepts_type_list() -> None:
    page = _page(jsonld=[{"@type": ["WebPage", "BlogPosting"], "headline": "Hello"}])

    issues = rule_jsonld_required.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail["type"] == "BlogPosting"
    assert issues[0].detail["missing"] == ["datePublished", "author", "image"]


def test_jsonld_required_ignores_other_types_and_failed_pages() -> None:
    clean_type = _page(jsonld=[{"@type": "WebPage", "headline": "Hello"}])
    failed = _page("https://example.com/broken", status=404, jsonld=[{"@type": "Article"}])

    assert rule_jsonld_required.RULE.run(_ctx(clean_type, failed)) == []


def test_jsonld_required_flattens_graph() -> None:
    page = _page(jsonld=[{"@graph": [{"@type": "Article", "headline": "Hello"}]}])

    issues = rule_jsonld_required.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail["missing"] == ["datePublished", "author", "image"]


# --- jsonld-organization ---------------------------------------------------


def test_jsonld_organization_flags_site_without_organization() -> None:
    pages = [
        _page("https://example.com/a"),
        _page("https://example.com/b", jsonld=[{"@type": "WebSite", "name": "Example"}]),
    ]

    issues = rule_jsonld_organization.RULE.run(_ctx(*pages))

    assert len(issues) == 1
    assert issues[0].url == "https://example.com/"
    assert issues[0].detail == {"pages_checked": 2}


def test_jsonld_organization_clean_when_any_page_has_it() -> None:
    pages = [
        _page("https://example.com/a"),
        _page("https://example.com/b", jsonld=[{"@type": "Organization", "name": "Example"}]),
    ]

    assert rule_jsonld_organization.RULE.run(_ctx(*pages)) == []


def test_jsonld_organization_accepts_list_type_and_counts_only_ok_pages() -> None:
    failed = _page("https://example.com/missing", status=404, jsonld=[{"@type": "Organization"}])
    ok = _page("https://example.com/", jsonld=[{"@type": ["Organization", "Corporation"]}])

    assert rule_jsonld_organization.RULE.run(_ctx(failed, ok)) == []
    issues = rule_jsonld_organization.RULE.run(_ctx(failed))
    assert issues[0].detail == {"pages_checked": 0}


# --- jsonld-event-required -------------------------------------------------


def test_jsonld_event_flags_missing_name_and_start_date() -> None:
    page = _page(jsonld=[{"@type": "Event", "location": {"@type": "Place", "name": "Hall"}}])

    issues = rule_jsonld_event.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {"missing": ["name", "startDate"]}


def test_jsonld_event_partial_location_is_flagged() -> None:
    page = _page(
        jsonld=[
            {
                "@type": "Event",
                "name": "Concert",
                "startDate": "2026-02-02T19:00:00+03:00",
                "location": {"@type": "Place"},
            }
        ]
    )

    issues = rule_jsonld_event.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {"missing": ["location"]}


def test_jsonld_event_clean_with_address() -> None:
    page = _page(
        jsonld=[
            {
                "@type": "Event",
                "name": "Concert",
                "startDate": "2026-02-02T19:00:00+03:00",
                "location": {"@type": "Place", "address": {"addressLocality": "Minsk"}},
            }
        ]
    )

    assert rule_jsonld_event.RULE.run(_ctx(page)) == []


def test_jsonld_event_accepts_location_list() -> None:
    page = _page(
        jsonld=[
            {
                "@type": "Event",
                "name": "Concert",
                "startDate": "2026-02-02T19:00:00+03:00",
                "location": [{"@type": "Place"}, {"@type": "Place", "name": "Hall"}],
            }
        ]
    )

    assert rule_jsonld_event.RULE.run(_ctx(page)) == []


def test_jsonld_event_ignores_non_event_items() -> None:
    page = _page(jsonld=[{"@type": "Organization", "name": "Example"}])

    assert rule_jsonld_event.RULE.run(_ctx(page)) == []


# --- hreflang-not-reciprocal ----------------------------------------------


def test_hreflang_reciprocal_flags_one_way_pair() -> None:
    en = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("ru", "https://example.com/ru/")],
    )
    ru = _hreflang_page(
        "https://example.com/ru/",
        [("ru", "https://example.com/ru/")],
    )

    issues = rule_hreflang_reciprocal.RULE.run(_ctx(en, ru))

    assert len(issues) == 1
    assert issues[0].url == en.url
    assert issues[0].detail == {"lang": "ru", "target": "https://example.com/ru/"}


def test_hreflang_reciprocal_clean_pair() -> None:
    variants = [("en", "https://example.com/en/"), ("ru", "https://example.com/ru/")]
    en = _hreflang_page("https://example.com/en/", variants)
    ru = _hreflang_page("https://example.com/ru/", variants)

    assert rule_hreflang_reciprocal.RULE.run(_ctx(en, ru)) == []


def test_hreflang_reciprocal_ignores_uncrawled_target() -> None:
    en = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("ru", "https://example.com/ru/")],
    )

    assert rule_hreflang_reciprocal.RULE.run(_ctx(en)) == []


def test_hreflang_reciprocal_matches_relative_and_trailing_slash_targets() -> None:
    en = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en"), ("ru", "/ru/")],
    )
    ru = _hreflang_page("https://example.com/ru", [("en", "/en")])

    assert rule_hreflang_reciprocal.RULE.run(_ctx(en, ru)) == []


# --- hreflang-missing-x-default -------------------------------------------


def test_hreflang_missing_x_default_flags_page_without_entry() -> None:
    page = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("ru", "https://example.com/ru/")],
    )

    issues = rule_hreflang_x_default.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {"langs": ["en", "ru"]}


def test_hreflang_missing_x_default_clean_case_insensitively() -> None:
    page = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("X-Default", "https://example.com/")],
    )

    assert rule_hreflang_x_default.RULE.run(_ctx(page)) == []


def test_hreflang_missing_x_default_ignores_pages_without_entries() -> None:
    page = _page()

    assert rule_hreflang_x_default.RULE.run(_ctx(page)) == []


# --- hreflang-target-not-ok ------------------------------------------------


def test_hreflang_target_not_ok_flags_non_200_target() -> None:
    source = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("ru", "https://example.com/ru/")],
    )
    broken = _page("https://example.com/ru/", status=404)

    issues = rule_hreflang_target.RULE.run(_ctx(source, broken))

    assert len(issues) == 1
    assert issues[0].url == source.url
    assert issues[0].detail == {
        "lang": "ru",
        "target": "https://example.com/ru/",
        "status": 404,
    }


def test_hreflang_target_not_ok_clean_for_working_targets() -> None:
    source = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("ru", "https://example.com/ru/")],
    )
    target = _page("https://example.com/ru/")

    assert rule_hreflang_target.RULE.run(_ctx(source, target)) == []


def test_hreflang_target_not_ok_ignores_uncrawled_target() -> None:
    source = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("ru", "https://example.com/ru/")],
    )

    assert rule_hreflang_target.RULE.run(_ctx(source)) == []


# --- html-lang-missing -----------------------------------------------------


def test_html_lang_missing_flags_none() -> None:
    page = _page()

    issues = rule_html_lang.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {"html_lang": None}


def test_html_lang_missing_flags_blank_value() -> None:
    page = _page(html_lang="   ")

    issues = rule_html_lang.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {"html_lang": "   "}


def test_html_lang_missing_clean() -> None:
    page = _page(html_lang="ru")

    assert rule_html_lang.RULE.run(_ctx(page)) == []


# --- html-lang-mismatch (viewport-missing substitution) -------------------


def test_html_lang_mismatch_flags_foreign_variants() -> None:
    page = _hreflang_page(
        "https://example.com/en/",
        [("en", "https://example.com/en/"), ("be", "https://example.com/be/")],
    )
    page.html_lang = "ru"

    issues = rule_html_lang_mismatch.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].rule_id == "html-lang-mismatch"
    assert issues[0].detail == {"html_lang": "ru", "hreflang": ["en", "be"]}


def test_html_lang_mismatch_clean_when_subtag_matches() -> None:
    page = _hreflang_page(
        "https://example.com/ru/",
        [("ru", "https://example.com/ru/"), ("en", "https://example.com/en/")],
    )
    page.html_lang = "ru-RU"

    assert rule_html_lang_mismatch.RULE.run(_ctx(page)) == []


def test_html_lang_mismatch_ignores_page_without_hreflang() -> None:
    page = _page(html_lang="ru")

    assert rule_html_lang_mismatch.RULE.run(_ctx(page)) == []


# --- mixed-content ---------------------------------------------------------


def test_mixed_content_flags_http_assets() -> None:
    page = _page(
        links=["http://cdn.example.com/app.js"],
        images=[{"src": "http://cdn.example.com/cover.png", "alt": "cover"}],
    )

    issues = rule_mixed_content.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {
        "urls": ["http://cdn.example.com/app.js", "http://cdn.example.com/cover.png"]
    }


def test_mixed_content_reports_first_five_unique_urls() -> None:
    page = _page(
        links=[f"http://cdn.example.com/{index}.js" for index in range(4)],
        images=[{"src": f"http://cdn.example.com/{index}.png"} for index in range(4, 7)],
    )

    issues = rule_mixed_content.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail["urls"] == [
        "http://cdn.example.com/0.js",
        "http://cdn.example.com/1.js",
        "http://cdn.example.com/2.js",
        "http://cdn.example.com/3.js",
        "http://cdn.example.com/4.png",
    ]


def test_mixed_content_clean_for_https_assets() -> None:
    page = _page(
        links=["https://cdn.example.com/app.js"],
        images=[{"src": "https://cdn.example.com/cover.png"}],
    )

    assert rule_mixed_content.RULE.run(_ctx(page)) == []


def test_mixed_content_ignores_plain_http_page() -> None:
    page = _page("http://example.com/", links=["http://cdn.example.com/app.js"])

    assert rule_mixed_content.RULE.run(_ctx(page)) == []


# --- jsonld-organization-duplicate ----------------------------------------


def test_duplicate_organization_flags_multiple_items() -> None:
    page = _page(
        jsonld=[
            {"@type": "Organization", "name": "Example"},
            {"@type": "Organization", "name": "Example duplicate"},
        ]
    )

    issues = rule_duplicate_organization.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {"count": 2}


def test_duplicate_organization_clean_for_single_item() -> None:
    page = _page(jsonld=[{"@type": "Organization", "name": "Example"}])

    assert rule_duplicate_organization.RULE.run(_ctx(page)) == []


def test_duplicate_organization_counts_graph_items() -> None:
    page = _page(
        jsonld=[
            {
                "@graph": [
                    {"@type": "Organization", "name": "Example"},
                    {"@type": ["Organization", "Corporation"], "name": "Example AG"},
                ]
            }
        ]
    )

    issues = rule_duplicate_organization.RULE.run(_ctx(page))

    assert len(issues) == 1
    assert issues[0].detail == {"count": 2}

