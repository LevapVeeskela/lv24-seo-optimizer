"""Offline tests for the accessibility fields extracted by the stdlib HTML parser."""

from __future__ import annotations

import textwrap

from lv24_seo.crawl.html import parse_html

A11Y_PAGE = textwrap.dedent(
    """\
    <!DOCTYPE html>
    <html lang="en">
    <head><title>Accessible form</title></head>
    <body>
      <a href="#content">Skip to content</a>
      <main id="main">
        <h1>Contact us</h1>
        <h3>Details</h3>
        <h2>Address</h2>
        <label for="email">Email</label>
        <input id="email" name="email" type="EMAIL" placeholder="you@example.com">
        <label>Phone <input name="phone" type="tel"></label>
        <input name="search" type="search" aria-label="Search">
        <input name="multi" id="multi">
        <label for="multi">First</label>
        <label for="multi">Second</label>
        <input type="hidden" name="token" value="secret">
        <input type="submit" value="Send">
        <input type="button" value="Click" aria-label="Do click">
        <input type="reset" value="Reset">
        <input type="image" src="/go.png" alt="Go">
        <button>Save</button>
        <button aria-label="Close"></button>
        <a href="/text">Visible</a>
        <a href="/aria" aria-label="Aria only"></a>
        <a href="/image"><img src="/logo.png" alt="Logo"></a>
        <a href="/empty"></a>
        <a href="/titled" title="Title only"></a>
        <a href="/a" aria-labelledby="caption"></a>
        <h2></h2>
      </main>
    </body>
    </html>
    """
)


def test_headings_keep_levels_and_empty_text() -> None:
    page = parse_html("https://lv24.by/form", A11Y_PAGE)

    assert page.headings == [
        {"level": 1, "text": "Contact us"},
        {"level": 3, "text": "Details"},
        {"level": 2, "text": "Address"},
        {"level": 2, "text": ""},
    ]
    assert page.h1 == ["Contact us"]


def test_main_landmark_and_skip_link_are_detected() -> None:
    page = parse_html("https://lv24.by/form", A11Y_PAGE)

    assert page.has_main is True
    assert page.has_skip_link is True


def test_skip_link_targets_are_case_insensitive() -> None:
    page = parse_html(
        "https://lv24.by/",
        '<body><a href="#MAIN">Jump</a></body>',
    )

    assert page.has_skip_link is True


def test_skip_link_needs_a_fragment_href() -> None:
    page = parse_html(
        "https://lv24.by/",
        '<body><a href="/skip-to-content">Skip to content</a><main></main></body>',
    )

    assert page.has_skip_link is False


def test_inputs_collect_labels_aria_and_ignored_types() -> None:
    page = parse_html("https://lv24.by/form", A11Y_PAGE)

    by_name = {item["name"]: item for item in page.inputs}

    assert list(by_name) == ["email", "phone", "search", "multi"]
    assert by_name["email"] == {
        "type": "email",
        "id": "email",
        "name": "email",
        "aria_label": "",
        "aria_labelledby": "",
        "title": "",
        "placeholder": "you@example.com",
        "label": "Email",
    }
    assert by_name["phone"]["label"] == "Phone"
    assert by_name["search"]["aria_label"] == "Search"
    assert by_name["multi"]["label"] == "First Second"


def test_input_without_label_has_empty_label() -> None:
    page = parse_html("https://lv24.by/", '<form><input name="bare"></form>')

    assert page.inputs == [
        {
            "type": "text",
            "id": "",
            "name": "bare",
            "aria_label": "",
            "aria_labelledby": "",
            "title": "",
            "placeholder": "",
            "label": "",
        }
    ]


def test_buttons_use_text_aria_and_input_value() -> None:
    page = parse_html("https://lv24.by/form", A11Y_PAGE)

    assert page.buttons == [
        {
            "text": "Send",
            "aria_label": "",
            "aria_labelledby": "",
            "title": "",
        },
        {
            "text": "Click",
            "aria_label": "Do click",
            "aria_labelledby": "",
            "title": "",
        },
        {"text": "Reset", "aria_label": "", "aria_labelledby": "", "title": ""},
        {"text": "Save", "aria_label": "", "aria_labelledby": "", "title": ""},
        {"text": "", "aria_label": "Close", "aria_labelledby": "", "title": ""},
    ]


def test_link_details_mirror_links_and_capture_accessible_names() -> None:
    page = parse_html("https://lv24.by/form", A11Y_PAGE)

    assert [item["href"] for item in page.link_details] == page.links
    details = {item["href"]: item for item in page.link_details}

    assert details["https://lv24.by/text"]["text"] == "Visible"
    assert details["https://lv24.by/aria"]["aria_label"] == "Aria only"
    assert details["https://lv24.by/image"]["image_alt"] == "Logo"
    assert details["https://lv24.by/image"]["text"] == ""
    assert details["https://lv24.by/empty"]["text"] == ""
    assert details["https://lv24.by/titled"]["title"] == "Title only"
    assert details["https://lv24.by/a"]["aria_labelledby"] == "caption"


def test_duplicate_hrefs_keep_one_link_and_one_detail() -> None:
    page = parse_html(
        "https://lv24.by/",
        '<a href="/x#one">First</a><a href="/x#two">Second</a>',
    )

    assert page.links == ["https://lv24.by/x"]
    assert page.link_details == [
        {
            "href": "https://lv24.by/x",
            "text": "First",
            "aria_label": "",
            "aria_labelledby": "",
            "title": "",
            "image_alt": "",
        }
    ]


def test_anchor_text_is_not_taken_from_scripts_or_styles() -> None:
    page = parse_html(
        "https://lv24.by/",
        "<a href='/x'>Real<script>var hidden = 1;</script><style>.a{}</style>"
        "<noscript>No JS</noscript></a>",
    )

    assert page.link_details[0]["text"] == "Real"


def test_malformed_a11y_markup_never_raises() -> None:
    page = parse_html(
        "https://lv24.by/",
        "<main><h2>Title</h2><label for=x>Name</label><input id=x><button>Go<a href='/a'>Link",
    )

    assert page.error is None
    assert page.headings == [{"level": 2, "text": "Title"}]
    assert page.inputs[0]["label"] == "Name"
    assert page.inputs[0]["id"] == "x"
    assert page.buttons[0]["text"].startswith("Go")
    assert page.link_details[0]["href"] == "https://lv24.by/a"
    assert page.link_details[0]["text"] == "Link"
    assert page.has_main is True


def test_empty_document_has_no_a11y_findings() -> None:
    page = parse_html("https://lv24.by/", b"")

    assert page.headings == []
    assert page.inputs == []
    assert page.buttons == []
    assert page.link_details == []
    assert page.has_main is False
    assert page.has_skip_link is False
