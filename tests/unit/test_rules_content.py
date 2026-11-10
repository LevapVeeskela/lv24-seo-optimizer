"""Unit tests for the content/link rule group (Wave 3, group B)."""

from __future__ import annotations

from typing import Any

import pytest

from lv24_seo.crawl.models import Issue, PageData, RuleContext
from lv24_seo.crawl.rules.base import Rule
from lv24_seo.crawl.rules.rule_broken_internal_link import RULE as BROKEN_INTERNAL_LINK
from lv24_seo.crawl.rules.rule_h1_missing import RULE as H1_MISSING
from lv24_seo.crawl.rules.rule_h1_multiple import RULE as H1_MULTIPLE
from lv24_seo.crawl.rules.rule_image_alt_missing import RULE as IMAGE_ALT_MISSING
from lv24_seo.crawl.rules.rule_meta_description_duplicate import (
    RULE as META_DESCRIPTION_DUPLICATE,
)
from lv24_seo.crawl.rules.rule_meta_description_missing import RULE as META_DESCRIPTION_MISSING
from lv24_seo.crawl.rules.rule_thin_content import RULE as THIN_CONTENT
from lv24_seo.crawl.rules.rule_title_duplicate import RULE as TITLE_DUPLICATE
from lv24_seo.crawl.rules.rule_title_length import RULE as TITLE_LENGTH
from lv24_seo.crawl.rules.rule_title_missing import RULE as TITLE_MISSING

SITE = "https://example.com"

RULES: list[tuple[Rule, str, str]] = [
    (TITLE_MISSING, "title-missing", "high"),
    (TITLE_DUPLICATE, "title-duplicate", "high"),
    (TITLE_LENGTH, "title-length", "low"),
    (META_DESCRIPTION_MISSING, "meta-description-missing", "medium"),
    (META_DESCRIPTION_DUPLICATE, "meta-description-duplicate", "medium"),
    (H1_MISSING, "h1-missing", "medium"),
    (H1_MULTIPLE, "h1-multiple", "low"),
    (THIN_CONTENT, "thin-content", "medium"),
    (BROKEN_INTERNAL_LINK, "broken-internal-link", "high"),
    (IMAGE_ALT_MISSING, "image-alt-missing", "medium"),
]


def make_page(url: str = f"{SITE}/", **overrides: Any) -> PageData:
    fields: dict[str, Any] = {"status": 200, "final_url": url, "content_type": "text/html"}
    fields.update(overrides)
    return PageData(url=url, **fields)


def make_ctx(*pages: PageData, site_url: str = SITE) -> RuleContext:
    return RuleContext(pages=list(pages), site_url=site_url)


def run(rule: Rule, *pages: PageData) -> list[Issue]:
    return rule.run(make_ctx(*pages))


@pytest.mark.parametrize(("rule", "rule_id", "severity"), RULES)
def test_rule_metadata(rule: Rule, rule_id: str, severity: str) -> None:
    assert rule.id == rule_id
    assert rule.severity == severity
    assert rule.title
    assert rule.description


@pytest.mark.parametrize("rule", [spec[0] for spec in RULES], ids=[spec[1] for spec in RULES])
def test_rules_skip_non_200_and_non_html_pages(rule: Rule) -> None:
    """A page that would violate every content rule if it were evaluated."""
    violating = {
        "title": None,
        "meta_description": None,
        "h1": [],
        "word_count": 10,
        "links": [f"{SITE}/missing-{index}" for index in range(3)],
        "images": [{"src": "logo.png"}],
    }
    gone = make_page(f"{SITE}/blog/thin-post", status=404, **violating)
    not_html = make_page(f"{SITE}/blog/thin-post", content_type="application/json", **violating)

    assert run(rule, gone, not_html) == []


# --- title-missing ---------------------------------------------------------------


def test_title_missing_flags_blank_titles() -> None:
    none_title = make_page(f"{SITE}/a", title=None)
    empty_title = make_page(f"{SITE}/b", title="")
    spaces_title = make_page(f"{SITE}/c", title="   ")

    issues = run(TITLE_MISSING, none_title, empty_title, spaces_title)

    assert [issue.url for issue in issues] == [none_title.url, empty_title.url, spaces_title.url]
    assert all(issue.rule_id == "title-missing" for issue in issues)
    assert all(issue.severity == "high" for issue in issues)
    assert all(issue.detail == {} for issue in issues)


def test_title_missing_clean_page_has_no_issue() -> None:
    page = make_page(f"{SITE}/about", title="About our company")

    assert run(TITLE_MISSING, page) == []


# --- title-duplicate -------------------------------------------------------------


def test_title_duplicate_groups_three_pages() -> None:
    first = make_page(f"{SITE}/one", title="Minsk city guide")
    second = make_page(f"{SITE}/two", title="  minsk CITY guide  ")
    unique = make_page(f"{SITE}/three", title="Gomel city guide")

    issues = run(TITLE_DUPLICATE, first, second, unique)

    assert [(issue.url, issue.detail["count"]) for issue in issues] == [
        (first.url, 2),
        (second.url, 2),
    ]
    assert [issue.detail["title"] for issue in issues] == ["Minsk city guide", "minsk CITY guide"]


def test_title_duplicate_ignores_blank_titles() -> None:
    pages = [
        make_page(f"{SITE}/{index}", title=title) for index, title in enumerate([None, "", " "])
    ]

    assert run(TITLE_DUPLICATE, *pages) == []


# --- title-length ----------------------------------------------------------------


def test_title_length_flags_titles_outside_range() -> None:
    short = make_page(f"{SITE}/short", title="Too short")
    long = make_page(f"{SITE}/long", title="a" * 61)
    min_boundary = make_page(f"{SITE}/min", title="b" * 15)
    max_boundary = make_page(f"{SITE}/max", title="c" * 60)

    issues = run(TITLE_LENGTH, short, long, min_boundary, max_boundary)

    assert [(issue.url, issue.detail) for issue in issues] == [
        (short.url, {"length": 9, "title": "Too short"}),
        (long.url, {"length": 61, "title": "a" * 61}),
    ]


def test_title_length_skips_missing_titles() -> None:
    pages = [make_page(f"{SITE}/{index}", title=title) for index, title in enumerate([None, "  "])]

    assert run(TITLE_LENGTH, *pages) == []


# --- meta-description-missing ----------------------------------------------------


def test_meta_description_missing_flags_blank_values() -> None:
    none_value = make_page(f"{SITE}/a", meta_description=None)
    empty_value = make_page(f"{SITE}/b", meta_description="")
    spaces_value = make_page(f"{SITE}/c", meta_description="  ")

    issues = run(META_DESCRIPTION_MISSING, none_value, empty_value, spaces_value)

    assert [issue.url for issue in issues] == [none_value.url, empty_value.url, spaces_value.url]
    assert all(issue.detail == {} for issue in issues)


def test_meta_description_missing_clean_page_has_no_issue() -> None:
    page = make_page(f"{SITE}/about", meta_description="Company history and contacts.")

    assert run(META_DESCRIPTION_MISSING, page) == []


# --- meta-description-duplicate --------------------------------------------------


def test_meta_description_duplicate_groups_three_pages() -> None:
    first = make_page(f"{SITE}/one", meta_description="Cheap flights to Minsk.")
    second = make_page(f"{SITE}/two", meta_description="  cheap flights to MINSK.  ")
    unique = make_page(f"{SITE}/three", meta_description="Train schedules to Brest.")

    issues = run(META_DESCRIPTION_DUPLICATE, first, second, unique)

    assert [(issue.url, issue.detail["count"]) for issue in issues] == [
        (first.url, 2),
        (second.url, 2),
    ]
    assert [issue.detail["description"] for issue in issues] == [
        "Cheap flights to Minsk.",
        "cheap flights to MINSK.",
    ]


def test_meta_description_duplicate_ignores_blank_values() -> None:
    pages = [
        make_page(f"{SITE}/{index}", meta_description=value)
        for index, value in enumerate([None, "", " "])
    ]

    assert run(META_DESCRIPTION_DUPLICATE, *pages) == []


# --- h1-missing / h1-multiple ----------------------------------------------------


def test_h1_missing_flags_pages_without_h1() -> None:
    missing = make_page(f"{SITE}/no-h1", h1=[])
    present = make_page(f"{SITE}/with-h1", h1=["About us"])

    issues = run(H1_MISSING, missing, present)

    assert [issue.url for issue in issues] == [missing.url]
    assert issues[0].severity == "medium"


def test_h1_multiple_flags_pages_with_several_h1() -> None:
    multiple = make_page(f"{SITE}/many-h1", h1=["First", "Second"])
    single = make_page(f"{SITE}/one-h1", h1=["Only"])

    issues = run(H1_MULTIPLE, multiple, single)

    assert [(issue.url, issue.detail) for issue in issues] == [(multiple.url, {"count": 2})]
    assert issues[0].severity == "low"


# --- thin-content ----------------------------------------------------------------


def test_thin_content_flags_slug_pages_and_skips_index_and_listing() -> None:
    thin_slug = make_page(f"{SITE}/blog/thin-post", word_count=50)
    digit_slug = make_page(f"{SITE}/news/2026", word_count=30)
    long_slug = make_page(f"{SITE}/blog/long-post", word_count=500)
    non_slug = make_page(f"{SITE}/about", word_count=50)
    trailing_slash = make_page(f"{SITE}/blog/thin-post/", word_count=50)
    root = make_page(f"{SITE}/", word_count=50)

    listing = make_page(
        f"{SITE}/blog",
        word_count=50,
        links=[f"{SITE}/post-{index}" for index in range(21)],
    )

    issues = run(
        THIN_CONTENT,
        thin_slug,
        digit_slug,
        long_slug,
        non_slug,
        trailing_slash,
        root,
        listing,
    )

    assert [(issue.url, issue.detail) for issue in issues] == [
        (thin_slug.url, {"word_count": 50}),
        (digit_slug.url, {"word_count": 30}),
    ]


# --- broken-internal-link --------------------------------------------------------


def test_broken_internal_link_flags_missing_and_error_targets() -> None:
    healthy = make_page(f"{SITE}/ok", title="Healthy")
    gone = make_page(f"{SITE}/gone", status=404, title="Gone")
    source = make_page(
        f"{SITE}/articles/source",
        title="Source page",
        links=[
            f"{SITE}/missing",
            "/relative-missing",
            f"{SITE}/gone",
            f"{SITE}/ok#section",
            f"{SITE}/ok/",
            "https://external.test/page",
            "mailto:hello@example.com",
        ],
    )

    issues = run(BROKEN_INTERNAL_LINK, source, healthy, gone)

    assert [(issue.url, issue.detail) for issue in issues] == [
        (source.url, {"target": f"{SITE}/missing", "reason": "missing"}),
        (source.url, {"target": f"{SITE}/relative-missing", "reason": "missing"}),
        (source.url, {"target": f"{SITE}/gone", "reason": "status"}),
    ]


def test_broken_internal_link_caps_issues_per_page() -> None:
    source = make_page(
        f"{SITE}/hub",
        title="Hub",
        links=[f"{SITE}/missing-{index}" for index in range(60)],
    )

    issues = run(BROKEN_INTERNAL_LINK, source)

    assert len(issues) == 50
    assert all(issue.url == source.url for issue in issues)
    assert [issue.detail["target"] for issue in issues] == [
        f"{SITE}/missing-{index}" for index in range(50)
    ]


def test_broken_internal_link_clean_page_has_no_issue() -> None:
    healthy = make_page(f"{SITE}/ok", title="Healthy")
    source = make_page(
        f"{SITE}/hub",
        title="Hub",
        links=[f"{SITE}/ok", "https://external.test/page"],
    )

    assert run(BROKEN_INTERNAL_LINK, source, healthy) == []


# --- image-alt-missing -----------------------------------------------------------


def test_image_alt_missing_counts_images_without_alt_attribute() -> None:
    page = make_page(
        f"{SITE}/gallery",
        images=[
            {"src": "a.jpg"},
            {"src": "b.jpg", "alt": None},
            {"src": "c.jpg", "alt": ""},
            {"src": "d.jpg", "alt": "Company logo"},
        ],
    )

    issues = run(IMAGE_ALT_MISSING, page)

    assert [(issue.url, issue.detail) for issue in issues] == [
        (page.url, {"missing": 2, "total": 4})
    ]


def test_image_alt_missing_clean_page_has_no_issue() -> None:
    page = make_page(
        f"{SITE}/gallery",
        images=[{"src": "a.jpg", "alt": "Photo"}, {"src": "b.jpg", "alt": ""}],
    )

    assert run(IMAGE_ALT_MISSING, page) == []
