"""Unit tests for the ``schema-required`` rule (Schema.org required properties)."""

from __future__ import annotations

from typing import Any

import pytest

from lv24_seo.crawl.models import PageData, RuleContext
from lv24_seo.crawl.rules import load_rules
from lv24_seo.crawl.rules.rule_schema_required import RULE

SITE = "https://example.com/"
URL = "https://example.com/page"

_COMPLETE: dict[str, dict[str, Any]] = {
    "Product": {
        "@type": "Product",
        "name": "Trail shoe",
        "image": "https://example.com/shoe.png",
        "offers": {"@type": "Offer", "price": "120.00", "priceCurrency": "EUR"},
    },
    "Offer": {"@type": "Offer", "price": "120.00", "priceCurrency": "EUR"},
    "BreadcrumbList": {
        "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": 1, "name": "Home", "item": SITE}],
    },
    "FAQPage": {
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": "Do you ship abroad?",
                "acceptedAnswer": {"@type": "Answer", "text": "Yes."},
            }
        ],
    },
    "Recipe": {
        "@type": "Recipe",
        "name": "Pancakes",
        "image": "https://example.com/pancakes.png",
        "recipeIngredient": ["flour", "milk", "egg"],
        "recipeInstructions": [{"@type": "HowToStep", "text": "Mix and fry."}],
    },
    "JobPosting": {
        "@type": "JobPosting",
        "title": "Backend engineer",
        "description": "Build the crawler.",
        "datePosted": "2026-11-01",
        "hiringOrganization": {"@type": "Organization", "name": "LV24"},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": "Minsk"},
        },
    },
    "VideoObject": {
        "@type": "VideoObject",
        "name": "Crawl walkthrough",
        "description": "How the crawler works.",
        "thumbnailUrl": "https://example.com/thumb.png",
        "uploadDate": "2026-11-01T09:00:00Z",
    },
    "LocalBusiness": {
        "@type": "LocalBusiness",
        "name": "LV24 Studio",
        "address": {"@type": "PostalAddress", "streetAddress": "Main 1"},
    },
    "Course": {
        "@type": "Course",
        "name": "Technical SEO",
        "description": "Crawl, index, report.",
        "provider": {"@type": "Organization", "name": "LV24"},
    },
    "SoftwareApplication": {
        "@type": "SoftwareApplication",
        "name": "lv24-seo",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"},
    },
}

# Type -> fields a bare ``{"@type": <type>}`` item must be told it is missing.
_EXPECTED_MISSING: dict[str, list[str]] = {
    "Product": ["name", "image", "offers"],
    "Offer": ["price", "priceCurrency"],
    "BreadcrumbList": ["itemListElement"],
    "FAQPage": ["mainEntity"],
    "Recipe": ["name", "image", "recipeIngredient", "recipeInstructions"],
    "JobPosting": [
        "title",
        "description",
        "datePosted",
        "hiringOrganization",
        "jobLocation",
    ],
    "VideoObject": ["name", "description", "thumbnailUrl", "uploadDate"],
    "LocalBusiness": ["name", "address"],
    "Course": ["name", "description", "provider"],
    "SoftwareApplication": ["name", "offers"],
}


def _page(url: str = URL, *, status: int = 200, **overrides: Any) -> PageData:
    final_url = overrides.pop("final_url", url)
    return PageData(url=url, status=status, final_url=final_url, **overrides)


def _ctx(*pages: PageData, site_url: str = SITE) -> RuleContext:
    return RuleContext(pages=list(pages), site_url=site_url)


def _run(*items: Any) -> list[Any]:
    return RULE.run(_ctx(_page(jsonld=list(items))))


def test_rule_metadata() -> None:
    assert RULE.id == "schema-required"
    assert RULE.severity == "medium"
    assert RULE.title
    assert RULE.description


def test_rule_is_discovered_by_load_rules() -> None:
    discovered = {rule.id for rule in load_rules()}

    assert "schema-required" in discovered
    assert "jsonld-required" in discovered
    assert "jsonld-event-required" in discovered
    assert "jsonld-organization" in discovered


@pytest.mark.parametrize("schema_type", sorted(_COMPLETE))
def test_complete_item_is_clean(schema_type: str) -> None:
    assert _run(_COMPLETE[schema_type]) == []


@pytest.mark.parametrize("schema_type", sorted(_EXPECTED_MISSING))
def test_bare_item_reports_every_required_field(schema_type: str) -> None:
    issues = _run({"@type": schema_type})

    assert len(issues) == 1
    assert issues[0].rule_id == "schema-required"
    assert issues[0].severity == "medium"
    assert issues[0].url == URL
    assert issues[0].detail == {
        "type": schema_type,
        "missing": _EXPECTED_MISSING[schema_type],
    }


def test_software_application_accepts_aggregate_rating_instead_of_offers() -> None:
    item = {
        "@type": "SoftwareApplication",
        "name": "lv24-seo",
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8"},
    }

    assert _run(item) == []


def test_software_application_reports_offers_when_both_are_missing() -> None:
    issues = _run({"@type": "SoftwareApplication", "name": "lv24-seo"})

    assert issues[0].detail == {"type": "SoftwareApplication", "missing": ["offers"]}


def test_graph_is_flattened() -> None:
    issues = _run({"@graph": [{"@type": "Recipe", "name": "Pancakes"}]})

    assert len(issues) == 1
    assert issues[0].detail == {
        "type": "Recipe",
        "missing": ["image", "recipeIngredient", "recipeInstructions"],
    }


def test_nested_lists_are_flattened() -> None:
    issues = _run([[{"@type": "Offer"}]], {"@graph": [[{"@type": "LocalBusiness"}]]})

    assert [issue.detail["type"] for issue in issues] == ["Offer", "LocalBusiness"]


def test_type_list_matches_a_known_type() -> None:
    item = {"@type": ["WebPage", "Course"], "name": "Technical SEO"}

    issues = _run(item)

    assert len(issues) == 1
    assert issues[0].detail == {"type": "Course", "missing": ["description", "provider"]}


def test_blank_and_empty_values_are_missing() -> None:
    item = {
        "@type": "Product",
        "name": "   ",
        "image": [],
        "offers": {},
    }

    issues = _run(item)

    assert issues[0].detail["missing"] == ["name", "image", "offers"]


def test_list_of_empty_objects_is_missing() -> None:
    item = {
        "@type": "Product",
        "name": "Trail shoe",
        "image": [{}],
        "offers": [{}, {}],
    }

    issues = _run(item)

    assert issues[0].detail["missing"] == ["image", "offers"]


def test_faq_question_without_answer_is_reported_on_the_faq_issue() -> None:
    item = {
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": "Do you ship abroad?"},
            {
                "@type": "Question",
                "name": "Returns?",
                "acceptedAnswer": {"@type": "Answer", "text": "30 days."},
            },
        ],
    }

    issues = _run(item)

    assert len(issues) == 1
    assert issues[0].detail == {
        "type": "FAQPage",
        "missing": [],
        "questions_missing": [
            {"index": 0, "name": "Do you ship abroad?", "missing": ["acceptedAnswer"]}
        ],
    }


def test_faq_question_gaps_are_bounded() -> None:
    item = {
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": f"Q{index}?"} for index in range(15)],
    }

    issues = _run(item)

    assert len(issues) == 1
    assert len(issues[0].detail["questions_missing"]) == 10


def test_non_html_page_is_skipped() -> None:
    page = _page(content_type="application/json", jsonld=[{"@type": "Product"}])

    assert RULE.run(_ctx(page)) == []


def test_non_200_page_is_skipped() -> None:
    page = _page(status=404, jsonld=[{"@type": "Product"}])

    assert RULE.run(_ctx(page)) == []


def test_page_without_jsonld_is_clean() -> None:
    assert RULE.run(_ctx(_page())) == []


def test_malformed_items_are_ignored_without_raising() -> None:
    items: list[Any] = [
        None,
        "junk",
        42,
        {"@type": 42},
        {"@type": ["Product", None], "name": "Shoe"},
        {"@graph": [None, "junk", {"@type": "Organization"}]},
    ]

    issues = _run(*items)

    assert len(issues) == 1
    assert issues[0].detail["type"] == "Product"
    assert issues[0].detail["missing"] == ["image", "offers"]


def test_article_event_and_organization_are_left_to_their_own_rules() -> None:
    items = [
        {"@type": "Article"},
        {"@type": "NewsArticle"},
        {"@type": "BlogPosting"},
        {"@type": "Event"},
        {"@type": "Organization"},
    ]

    assert _run(*items) == []


def test_findings_are_capped_per_page() -> None:
    issues = _run(*[{"@type": "Offer"} for _ in range(15)])

    assert len(issues) == 10
    assert all(issue.detail["type"] == "Offer" for issue in issues)


def test_multiple_pages_each_get_their_own_findings() -> None:
    first = _page(URL, jsonld=[{"@type": "Offer"}])
    second = _page(f"{SITE}other", jsonld=[{"@type": "BreadcrumbList"}])

    issues = RULE.run(_ctx(first, second))

    assert [issue.url for issue in issues] == [URL, f"{SITE}other"]
