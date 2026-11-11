"""Render a report as plain text, Markdown or email-safe HTML (Wave 5, WP5.2).

The renderers are pure functions over :class:`~lv24_seo.report.models.Report`:

- :func:`render_text` fits a 100-column terminal and never emits ANSI;
- :func:`render_markdown` is diff-friendly and escapes table separators;
- :func:`render_html` is standalone and email-safe: inline CSS only, no scripts,
  no external assets and every value HTML-escaped.

Honesty rules: missing values stay empty instead of being invented, limitations
always come last, and no renderer creates a timestamp of its own — the only time
shown is ``report.generated_at``.
"""

from __future__ import annotations

import html
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from lv24_seo.report.models import Report, ReportSection

__all__ = ["render_html", "render_markdown", "render_text"]

_TEXT_WIDTH = 100
_LIMITATIONS_TITLE = "Data limitations"
_FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

# C0 control bytes are never useful in a report and make terminal output unsafe;
# tab and newline are kept because wrapping and Markdown paragraphs need them.
_CONTROL_CHARACTERS: dict[int, None] = {
    code: None for code in range(0x20) if code not in (0x09, 0x0A)
}
_CONTROL_CHARACTERS[0x7F] = None

_OUTER_TABLE_STYLE = "border-collapse:collapse;width:100%;background-color:#f4f5f7;"
_CARD_STYLE = (
    "border-collapse:collapse;width:100%;max-width:640px;"
    "background-color:#ffffff;border-radius:8px;"
)
_CARD_CELL_STYLE = "padding:28px 24px;"
_BODY_STYLE = f"margin:0;padding:0;background-color:#f4f5f7;font-family:{_FONT_STACK};"
_H1_STYLE = "margin:0 0 8px 0;font-size:22px;line-height:1.3;color:#111827;font-weight:700;"
_META_STYLE = "margin:0;font-size:13px;line-height:1.5;color:#6b7280;"
_H2_STYLE = "margin:24px 0 8px 0;font-size:17px;line-height:1.35;color:#111827;font-weight:600;"
_P_STYLE = "margin:0 0 12px 0;font-size:15px;line-height:1.55;color:#1f2937;"
_DATA_TABLE_STYLE = "border-collapse:collapse;width:100%;margin:8px 0 12px 0;font-size:14px;"
_TH_STYLE = "padding:8px 10px;border-bottom:2px solid #e5e7eb;text-align:left;color:#111827;"
_TD_STYLE = "padding:8px 10px;border-bottom:1px solid #e5e7eb;color:#1f2937;vertical-align:top;"
_UL_STYLE = "margin:0 0 12px 0;padding-left:20px;font-size:14px;line-height:1.55;color:#374151;"
_FOOTER_STYLE = (
    "margin-top:28px;padding-top:16px;border-top:1px solid #e5e7eb;"
    "font-size:12px;line-height:1.55;color:#6b7280;"
)
_FOOTER_HEADING_STYLE = "margin:0 0 6px 0;font-size:12px;font-weight:600;color:#6b7280;"
_FOOTER_LIST_STYLE = "margin:0;padding-left:18px;"


def _stringify(value: Any) -> str:
    """Return a stable display string, dropping terminal control bytes."""
    if value is None:
        return ""
    return str(value).translate(_CONTROL_CHARACTERS)


def _period(report: Report) -> str:
    start = _stringify(report.period_start)
    end = _stringify(report.period_end)
    if start and end:
        return f"{start} - {end}"
    return start or end


def _wrap(text: str, *, subsequent_indent: str = "") -> list[str]:
    if not text:
        return []
    return textwrap.wrap(
        text,
        width=_TEXT_WIDTH,
        subsequent_indent=subsequent_indent,
        break_long_words=True,
        break_on_hyphens=False,
    )


def _text_section(section: ReportSection) -> list[str]:
    lines = _wrap(_stringify(section.title))
    lines.extend(_wrap(_stringify(section.summary)))
    for row in section.rows:
        for label, value in row.items():
            lines.extend(
                _wrap(
                    f"{_stringify(label)}: {_stringify(value)}",
                    subsequent_indent="  ",
                )
            )
    for note in section.notes:
        lines.extend(_wrap(f"- {_stringify(note)}", subsequent_indent="  "))
    return lines


def render_text(report: Report) -> str:
    """Render *report* as plain text no wider than 100 columns."""
    blocks: list[str] = []
    header: list[str] = []
    header.extend(_wrap(_stringify(report.headline)))
    period = _period(report)
    if period:
        header.extend(_wrap(f"Period: {period}"))
    generated_at = _stringify(report.generated_at)
    if generated_at:
        header.extend(_wrap(f"Generated: {generated_at}"))
    if header:
        blocks.append("\n".join(header))

    for section in report.sections:
        section_text = "\n".join(_text_section(section))
        if section_text:
            blocks.append(section_text)

    if report.warnings:
        warning_lines = ["Warnings"]
        for item in report.warnings:
            warning_lines.extend(_wrap(f"- {_stringify(item)}", subsequent_indent="  "))
        blocks.append("\n".join(warning_lines))

    if report.limitations:
        limitations = [_LIMITATIONS_TITLE]
        for item in report.limitations:
            limitations.extend(_wrap(f"- {_stringify(item)}", subsequent_indent="  "))
        blocks.append("\n".join(limitations))

    return "\n\n".join(blocks)


def _markdown_text(value: Any) -> str:
    """Escape angle brackets so report data cannot become raw HTML."""
    return _stringify(value).replace(">", "&gt;").replace("<", "&lt;")


def _markdown_cell(value: Any) -> str:
    text = _stringify(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return _markdown_text(text).replace("|", "\\|")


def _columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the union of row keys in first-seen order."""
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _markdown_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    columns = _columns(rows)
    if not columns:
        return []
    header = "| " + " | ".join(_markdown_cell(column) for column in columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, separator]
    for row in rows:
        cells = " | ".join(_markdown_cell(row.get(column, "")) for column in columns)
        lines.append(f"| {cells} |")
    return lines


def _markdown_section(section: ReportSection) -> str:
    lines = [f"## {_markdown_text(section.title)}"]
    summary = _stringify(section.summary)
    if summary.strip():
        lines.extend(["", _markdown_text(summary)])
    table = _markdown_table(section.rows)
    if table:
        lines.extend(["", *table])
    if section.notes:
        lines.extend(["", *(f"- {_markdown_text(note)}" for note in section.notes)])
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """Render *report* as a Markdown document."""
    blocks: list[str] = []
    header: list[str] = []
    headline = _markdown_text(report.headline)
    if headline:
        header.append(f"# {headline}")

    period = _period(report)
    if period:
        header.append(f"_{_markdown_text(period)}_")
    generated_at = _stringify(report.generated_at)
    if generated_at:
        header.append(f"_Generated: {_markdown_text(generated_at)}_")
    if header:
        blocks.append("\n".join(header))

    for section in report.sections:
        blocks.append(_markdown_section(section))

    if report.warnings:
        blocks.append(
            "\n".join(["## Warnings", *(f"- {_markdown_text(item)}" for item in report.warnings)])
        )

    if report.limitations:
        lines = [f"## {_LIMITATIONS_TITLE}"]
        lines.extend(f"- {_markdown_text(item)}" for item in report.limitations)
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def _html_text(value: Any) -> str:
    return html.escape(_stringify(value), quote=True)


def _html_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    columns = _columns(rows)
    if not columns:
        return []
    lines = [
        (
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="{_DATA_TABLE_STYLE}">'
        ),
        "<thead>",
        "<tr>",
    ]
    for column in columns:
        lines.append(f'<th scope="col" style="{_TH_STYLE}">{_html_text(column)}</th>')
    lines.extend(["</tr>", "</thead>", "<tbody>"])
    for row in rows:
        lines.append("<tr>")
        for column in columns:
            lines.append(f'<td style="{_TD_STYLE}">{_html_text(row.get(column, ""))}</td>')
        lines.append("</tr>")
    lines.extend(["</tbody>", "</table>"])
    return lines


def _html_section(section: ReportSection) -> list[str]:
    lines = [f'<h2 style="{_H2_STYLE}">{_html_text(section.title)}</h2>']
    summary = _stringify(section.summary)
    if summary.strip():
        lines.append(f'<p style="{_P_STYLE}">{_html_text(summary)}</p>')
    if section.rows:
        lines.extend(_html_rows(section.rows))
    if section.notes:
        items = "".join(f"<li>{_html_text(note)}</li>" for note in section.notes)
        lines.append(f'<ul style="{_UL_STYLE}">{items}</ul>')
    return lines


def render_html(report: Report, *, title: str | None = None, locale: str = "en") -> str:
    """Render *report* as a standalone, email-safe HTML document."""
    document_title = _stringify(title) or _stringify(report.headline) or "SEO report"
    language = _stringify(locale) or "en"
    lines: list[str] = [
        "<!DOCTYPE html>",
        f'<html lang="{_html_text(language)}">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_html_text(document_title)}</title>",
        "</head>",
        f'<body style="{_BODY_STYLE}">',
        (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="{_OUTER_TABLE_STYLE}">'
        ),
        '<tr><td align="center" style="padding:24px 12px;">',
        (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="{_CARD_STYLE}">'
        ),
        "<tr>",
        f'<td style="{_CARD_CELL_STYLE}">',
    ]

    headline = _stringify(report.headline)
    if headline:
        lines.append(f'<h1 style="{_H1_STYLE}">{_html_text(headline)}</h1>')

    meta: list[str] = []
    period = _period(report)
    if period:
        meta.append(f"Period: {_html_text(period)}")
    generated_at = _stringify(report.generated_at)
    if generated_at:
        meta.append(f"Generated: {_html_text(generated_at)}")
    if meta:
        lines.append(f'<p style="{_META_STYLE}">{" &middot; ".join(meta)}</p>')

    for section in report.sections:
        lines.extend(_html_section(section))

    if report.warnings:
        lines.extend(
            _html_section(
                ReportSection(
                    key="warnings",
                    title="Warnings",
                    summary="",
                    notes=list(report.warnings),
                )
            )
        )

    if report.limitations:
        lines.append(f'<div style="{_FOOTER_STYLE}">')
        lines.append(f'<h2 style="{_FOOTER_HEADING_STYLE}">{_LIMITATIONS_TITLE}</h2>')
        items = "".join(f"<li>{_html_text(item)}</li>" for item in report.limitations)
        lines.append(f'<ul style="{_FOOTER_LIST_STYLE}">{items}</ul>')
        lines.append("</div>")

    lines.extend(
        [
            "</td>",
            "</tr>",
            "</table>",
            "</td></tr>",
            "</table>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(lines)
