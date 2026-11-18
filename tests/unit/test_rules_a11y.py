"""Unit tests for the accessibility-basics crawl rule."""

from __future__ import annotations

import json
from typing import Any

import pytest

from lv24_seo.crawl.models import Issue, PageData, RuleContext
from lv24_seo.crawl.rules import load_rules
from lv24_seo.crawl.rules.rule_a11y_basics import RULE

SITE = "https://example.com"


def make_page(url: str = f"{SITE}/", **overrides: Any) -> PageData:
    fields: dict[str, Any] = {
        "status": 200,
        "final_url": url,
        "content_type": "text/html",
        "has_main": True,
        "has_skip_link": True,
    }
    fields.update(overrides)
    return PageData(url=url, **fields)


def run(*pages: PageData) -> list[Issue]:
    return RULE.run(RuleContext(pages=list(pages), site_url=SITE))


def checks(issues: list[Issue]) -> list[str]:
    return [issue.detail["check"] for issue in issues]


def test_rule_metadata_and_discovery() -> None:
    assert RULE.id == "a11y-basics"
    assert RULE.severity == "medium"
    assert RULE.title
    assert RULE.description
    assert "a11y-basics" in {rule.id for rule in load_rules()}


def test_clean_page_has_no_issues() -> None:
    page = make_page(
        inputs=[
            {
                "type": "email",
                "name": "email",
                "label": "Email",
                "aria_label": "",
                "aria_labelledby": "",
                "title": "",
            }
        ],
        buttons=[{"text": "Send", "aria_label": "", "aria_labelledby": "", "title": ""}],
        link_details=[
            {
                "href": f"{SITE}/about",
                "text": "About",
                "aria_label": "",
                "aria_labelledby": "",
                "title": "",
                "image_alt": "",
            }
        ],
        headings=[{"level": 1, "text": "Home"}, {"level": 2, "text": "About"}],
    )

    assert run(page) == []


@pytest.mark.parametrize("key", ["label", "aria_label", "aria_labelledby", "title"])
def test_input_with_any_name_source_passes(key: str) -> None:
    item = {
        "type": "text",
        "name": "q",
        "label": "",
        "aria_label": "",
        "aria_labelledby": "",
        "title": "",
    }
    item[key] = "Query"

    assert run(make_page(inputs=[item])) == []


def test_input_without_name_is_flagged() -> None:
    issues = run(
        make_page(
            inputs=[
                {
                    "type": "search",
                    "name": "q",
                    "label": "",
                    "aria_label": "",
                    "aria_labelledby": "",
                    "title": "",
                }
            ]
        )
    )

    assert checks(issues) == ["input-label"]
    assert issues[0].detail == {"check": "input-label", "input_type": "search", "name": "q"}
    assert issues[0].severity == "medium"
    assert issues[0].rule_id == "a11y-basics"


@pytest.mark.parametrize("key", ["text", "aria_label", "aria_labelledby", "title"])
def test_button_with_any_name_source_passes(key: str) -> None:
    item = {"text": "", "aria_label": "", "aria_labelledby": "", "title": ""}
    item[key] = "Save"

    assert run(make_page(buttons=[item])) == []


def test_unnamed_button_is_flagged_once_per_button() -> None:
    issues = run(make_page(buttons=[{"text": "", "aria_label": "", "title": ""}] * 2))

    assert checks(issues) == ["button-name", "button-name"]
    assert issues[0].detail == {"check": "button-name"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"text": "Read more"},
        {"aria_label": "Read more"},
        {"aria_labelledby": "caption"},
        {"title": "Read more"},
        {"image_alt": "Logo"},
    ],
)
def test_named_link_passes(overrides: dict[str, str]) -> None:
    item = {
        "href": f"{SITE}/more",
        "text": "",
        "aria_label": "",
        "aria_labelledby": "",
        "title": "",
        "image_alt": "",
    }
    item.update(overrides)

    assert run(make_page(link_details=[item])) == []


def test_empty_link_is_flagged_with_its_href() -> None:
    issues = run(
        make_page(
            link_details=[
                {
                    "href": f"{SITE}/more",
                    "text": "",
                    "aria_label": "",
                    "aria_labelledby": "",
                    "title": "",
                    "image_alt": "",
                }
            ]
        )
    )

    assert checks(issues) == ["link-name"]
    assert issues[0].detail == {"check": "link-name", "href": f"{SITE}/more"}


def test_link_findings_are_capped_at_twenty() -> None:
    links = [
        {
            "href": f"{SITE}/empty-{index}",
            "text": "",
            "aria_label": "",
            "aria_labelledby": "",
            "title": "",
            "image_alt": "",
        }
        for index in range(25)
    ]

    issues = run(make_page(link_details=links))

    assert checks(issues) == ["link-name"] * 20
    assert issues[-1].detail["href"] == f"{SITE}/empty-19"


def test_missing_main_landmark_is_flagged() -> None:
    issues = run(make_page(has_main=False))

    assert checks(issues) == ["main-landmark"]
    assert issues[0].detail == {"check": "main-landmark"}


def test_missing_skip_link_is_flagged_once() -> None:
    issues = run(make_page(has_skip_link=False))

    assert checks(issues) == ["skip-link"]
    assert issues[0].detail == {"check": "skip-link"}


def test_heading_order_jumps_are_flagged() -> None:
    issues = run(
        make_page(
            headings=[
                {"level": 1, "text": "Home"},
                {"level": 2, "text": "Section"},
                {"level": 4, "text": "Deep"},
                {"level": 3, "text": "Back"},
            ]
        )
    )

    assert checks(issues) == ["heading-order"]
    assert issues[0].detail == {"check": "heading-order", "from": 2, "to": 4}


def test_heading_order_findings_are_capped_at_five() -> None:
    levels = [1, 3, 5, 1, 3, 5, 1, 3, 5]
    issues = run(make_page(headings=[{"level": level, "text": ""} for level in levels]))

    assert checks(issues) == ["heading-order"] * 5


def test_heading_order_ignores_invalid_levels() -> None:
    issues = run(
        make_page(
            headings=[
                {"level": 0, "text": ""},
                {"level": "2", "text": ""},
                {"level": 7, "text": ""},
                {"level": True, "text": ""},
            ]
        )
    )

    assert issues == []


def test_total_findings_are_capped_at_thirty_per_page() -> None:
    page = make_page(
        has_main=False,
        has_skip_link=False,
        inputs=[
            {
                "type": "text",
                "name": f"q{index}",
                "label": "",
                "aria_label": "",
                "aria_labelledby": "",
                "title": "",
            }
            for index in range(10)
        ],
        buttons=[{"text": "", "aria_label": "", "aria_labelledby": "", "title": ""}] * 10,
        link_details=[
            {
                "href": f"{SITE}/empty-{index}",
                "text": "",
                "aria_label": "",
                "aria_labelledby": "",
                "title": "",
                "image_alt": "",
            }
            for index in range(20)
        ],
    )

    assert len(run(page)) == 30


def test_non_html_and_non_200_pages_are_skipped() -> None:
    violating: dict[str, Any] = {
        "has_main": False,
        "has_skip_link": False,
        "inputs": [{"type": "text", "name": "q"}],
        "buttons": [{"text": ""}],
        "headings": [{"level": 1}, {"level": 4}],
    }

    gone = make_page(f"{SITE}/gone", status=404, **violating)
    not_html = make_page(f"{SITE}/api", content_type="application/json", **violating)
    parse_error = make_page(f"{SITE}/broken", error="parse_error", **violating)

    assert run(gone, not_html, parse_error) == []


def test_details_are_json_safe() -> None:
    issues = run(
        make_page(
            has_main=False,
            has_skip_link=False,
            inputs=[{"type": "text", "name": "q"}],
            buttons=[{"text": ""}],
            link_details=[{"href": f"{SITE}/x"}],
            headings=[{"level": 1}, {"level": 3}],
        )
    )

    payload = json.dumps([issue.detail for issue in issues], sort_keys=True)

    assert "input-label" in payload
    assert "main-landmark" in payload
