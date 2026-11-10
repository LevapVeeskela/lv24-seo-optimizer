"""Unit tests for the Wave 5 report renderers (WP5.2)."""

from __future__ import annotations

from html.parser import HTMLParser

from lv24_seo.report.models import Report, ReportSection
from lv24_seo.report.render import render_html, render_markdown, render_text

_CYRILLIC_CITY = "\u041c\u0438\u043d\u0441\u043a"


class _TagCollector(HTMLParser):
    """Collect start tags so tests can assert the document structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[str] = []
        self.attributes: list[dict[str, str | None]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:  # pragma: no cover - trivial collector
        self.tags.append(tag)
        self.attributes.append(dict(attrs))


def _sample_report() -> Report:
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-01",
        period_end="2026-11-07",
        headline="Steady growth with one indexing risk",
        generated_at="2026-11-08T06:00:00Z",
        sections=[
            ReportSection(
                key="delta",
                title="Period delta",
                summary="Clicks and impressions are up.",
                rows=[
                    {"metric": "Clicks", "current": 120, "delta": "+20%"},
                    {"metric": "Impressions", "current": 1200, "delta": "+15%"},
                ],
                notes=["CTR is flat."],
            ),
            ReportSection(
                key="indexing",
                title="Indexing",
                summary="Two pages need attention.",
                rows=[{"url": "/a", "status": "404"}],
                notes=[],
            ),
        ],
        limitations=["Yandex data is delayed by two days."],
    )


def _section_report(
    *, rows: list[dict[str, object]] | None = None, notes: list[str] | None = None
) -> Report:
    return Report(
        kind="weekly",
        property_id="sc-domain:example.com",
        period_start="2026-11-01",
        period_end="2026-11-07",
        headline="Headline",
        sections=[
            ReportSection(
                key="one",
                title="Section one",
                summary="Summary one.",
                rows=rows if rows is not None else [],
                notes=notes if notes is not None else [],
            )
        ],
    )


def test_render_text_layout_rows_and_width() -> None:
    report = _sample_report()

    text = render_text(report)
    lines = text.splitlines()

    assert lines[0] == report.headline
    assert lines[1] == "Period: 2026-11-01 - 2026-11-07"
    assert lines[2] == "Generated: 2026-11-08T06:00:00Z"
    assert "metric: Clicks" in text
    assert "current: 120" in text
    assert "delta: +20%" in text
    assert "metric: Impressions" in text
    assert "current: 1200" in text
    assert "- CTR is flat." in text
    assert "\n\n" in text
    assert max(len(line) for line in lines) <= 100
    assert "\x1b[" not in text


def test_render_text_keeps_sections_and_limitations_last() -> None:
    text = render_text(_sample_report())
    blocks = text.split("\n\n")

    assert any("Period delta" in block for block in blocks)
    assert any("Indexing" in block for block in blocks)
    assert text.splitlines()[0] == "Steady growth with one indexing risk"
    assert text.splitlines()[-1] == "- Yandex data is delayed by two days."
    assert "Data limitations" in blocks[-1]


def test_render_text_wraps_long_unbroken_content() -> None:
    report = _section_report(
        rows=[{"long_key": "x" * 250}],
        notes=["q" * 220],
    )
    report.headline = "h" * 220
    report.limitations = ["z" * 220]

    text = render_text(report)

    assert max(len(line) for line in text.splitlines()) <= 100
    assert text.count("x") == 250
    assert text.count("q") == 220
    assert text.count("z") == 220


def test_render_text_strips_control_characters() -> None:
    text = render_text(_section_report(rows=[{"value": "\x1b[31mred\x1b[0m"}]))

    assert "\x1b" not in text
    assert "red" in text


def test_render_text_empty_rows_and_notes_keep_summary() -> None:
    text = render_text(_section_report())

    assert "Summary one." in text
    assert "Data limitations" not in text


def test_render_markdown_structure_table_and_notes() -> None:
    markdown = render_markdown(_sample_report())
    lines = markdown.splitlines()

    assert lines[0] == "# Steady growth with one indexing risk"
    assert lines[1] == "_2026-11-01 - 2026-11-07_"
    assert lines[2] == "_Generated: 2026-11-08T06:00:00Z_"
    assert "## Period delta" in markdown
    assert "Clicks and impressions are up." in markdown
    assert "| metric | current | delta |" in markdown
    assert "| --- | --- | --- |" in markdown
    assert "| Clicks | 120 | +20% |" in markdown
    assert "| Impressions | 1200 | +15% |" in markdown
    assert "## Indexing" in markdown
    assert "| url | status |" in markdown
    assert "| /a | 404 |" in markdown
    assert "- CTR is flat." in markdown
    assert markdown.splitlines()[-1] == "- Yandex data is delayed by two days."
    assert "## Data limitations" in markdown


def test_render_markdown_table_uses_union_of_keys_in_first_seen_order() -> None:
    markdown = render_markdown(_section_report(rows=[{"b": 1, "a": 2}, {"c": 3}]))

    assert "| b | a | c |" in markdown
    assert "| 1 | 2 |  |" in markdown
    assert "|  |  | 3 |" in markdown


def test_render_markdown_escapes_pipes_in_headers_and_cells() -> None:
    markdown = render_markdown(_section_report(rows=[{"page|path": "/a|b"}]))

    assert "| page\\|path |" in markdown
    assert "| /a\\|b |" in markdown


def test_render_markdown_empty_rows_and_notes_keep_summary() -> None:
    markdown = render_markdown(_section_report())

    assert "## Section one" in markdown
    assert "Summary one." in markdown
    assert "|" not in markdown
    assert "\n- " not in markdown


def test_render_html_is_standalone_email_safe_document() -> None:
    html_doc = render_html(_sample_report(), title="Weekly <report>", locale="ru")
    parser = _TagCollector()
    parser.feed(html_doc)
    parser.close()

    assert html_doc.startswith("<!DOCTYPE html>")
    assert '<html lang="ru">' in html_doc
    assert '<table role="presentation"' in html_doc
    assert "max-width:640px" in html_doc
    assert "font-family:-apple-system" in html_doc
    assert "<title>Weekly &lt;report&gt;</title>" in html_doc
    assert "<script" not in html_doc.lower()
    assert "<style" not in html_doc.lower()
    assert "<link" not in html_doc.lower()
    assert "@import" not in html_doc
    assert "script" not in parser.tags
    assert "html" in parser.tags
    assert "body" in parser.tags


def test_render_html_renders_each_section_table_and_notes() -> None:
    html_doc = render_html(_sample_report())

    assert '<html lang="en">' in html_doc
    assert "<title>Steady growth with one indexing risk</title>" in html_doc
    assert ">Period delta<" in html_doc
    assert ">Clicks<" in html_doc
    assert ">120<" in html_doc
    assert ">Impressions<" in html_doc
    assert ">1200<" in html_doc
    assert "CTR is flat." in html_doc
    assert ">Indexing<" in html_doc
    assert "Yandex data is delayed by two days." in html_doc
    assert "color:#6b7280" in html_doc


def test_render_html_empty_rows_and_notes_keep_summary() -> None:
    html_doc = render_html(_section_report())

    assert "Summary one." in html_doc
    assert "<th " not in html_doc
    assert "<tbody>" not in html_doc
    assert "<li>" not in html_doc
    # Only the two presentation layout tables remain.
    assert html_doc.count('<table width="100%"') == 0


def test_xss_payload_is_neutralised() -> None:
    payload = "<script>alert(1)</script>"
    report = _section_report(rows=[{"value": payload}], notes=[payload])
    report.limitations = [payload]

    text = render_text(report)
    markdown = render_markdown(report)
    html_doc = render_html(report)

    # Plain text keeps the payload as inert text; no markup can execute.
    assert text.count(payload) == 3
    assert "\x1b" not in text

    escaped = "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert "<script>" not in markdown
    assert "<script>" not in html_doc
    assert escaped in markdown
    assert escaped in html_doc
    assert markdown.count(escaped) == 3  # table cell + note + limitation
    assert html_doc.count(escaped) == 3  # table cell + note + limitation


def test_render_html_escapes_all_user_controlled_fields() -> None:
    payload = "<b onclick=alert(1)>x</b>"
    report = _section_report(rows=[{"key": payload}])
    report.headline = payload
    report.sections[0].title = payload
    report.sections[0].summary = payload

    html_doc = render_html(report, title=payload)

    assert "<b " not in html_doc
    assert html_doc.count("&lt;b onclick=alert(1)&gt;x&lt;/b&gt;") == 5


def test_xss_payload_in_metadata_is_escaped() -> None:
    html_doc = render_html(
        _section_report(),
        title='<img src=x onerror="alert(1)">',
        locale="en",
    )

    assert "<img" not in html_doc
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html_doc


def test_cyrillic_survives_all_renderers() -> None:
    report = _section_report(rows=[{"city": _CYRILLIC_CITY}])
    report.headline = f"{_CYRILLIC_CITY} traffic grows"

    assert _CYRILLIC_CITY in render_text(report)
    assert _CYRILLIC_CITY in render_markdown(report)
    assert _CYRILLIC_CITY in render_html(report)


def test_renderers_are_deterministic_and_use_generated_at() -> None:
    report = _sample_report()

    assert report.generated_at in render_text(report)
    assert report.generated_at in render_markdown(report)
    assert report.generated_at in render_html(report)
    assert render_text(report) == render_text(report)
    assert render_markdown(report) == render_markdown(report)
    assert render_html(report) == render_html(report)


def test_generated_at_is_omitted_when_empty() -> None:
    report = _section_report()

    assert "Generated:" not in render_text(report)
    assert "Generated:" not in render_markdown(report)
    assert "Generated:" not in render_html(report)
