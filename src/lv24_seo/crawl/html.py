"""Tolerant HTML extraction built on the standard-library parser.

``lxml`` is not a dependency yet, so this module relies on
:class:`html.parser.HTMLParser`, which never validates markup: truncated or malformed
pages still expose whatever fields can be recovered.

Design notes
------------
* Charset is taken from the HTTP ``Content-Type`` header only, as the crawler contract
  requires. Bytes without a declared charset are decoded as UTF-8 with
  ``errors="replace"``, so a wrong or missing declaration never breaks a crawl.
  ``<meta charset>`` is not sniffed.
* URLs are resolved against the first ``<base href>`` when present, else
  ``final_url or url``, with :func:`urllib.parse.urljoin`. Link fragments are
  dropped: for a crawler every fragment is the same document.
* Extraction is best effort. Invalid JSON-LD blocks and hostile markup are skipped,
  never raised; only a catastrophic parser failure produces ``error="parse_error"``.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from lv24_seo.crawl.models import PageData

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_SUPPRESSED_TAGS = frozenset({"script", "style", "noscript"})
_BREAK_TAGS = frozenset({"br", "hr"})
_LINK_SCHEMES = frozenset({"http", "https"})
_JSONLD_MEDIA_TYPE = "application/ld+json"
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INPUT_IGNORED_TYPES = frozenset({"hidden", "image"})
_INPUT_BUTTON_TYPES = frozenset({"submit", "button", "reset"})
_SKIP_LINK_TARGETS = frozenset({"#main", "#content", "#main-content"})
MAX_STORED_TEXT_CHARS = 20_000
"""Upper bound for :attr:`PageData.text`.

``word_count`` is always computed from the full visible text; only the retained copy is
capped. No crawl rule reads ``page.text`` today, so a 20k prefix keeps diagnostics useful
without letting a crawl of thousands of pages hold every byte in memory.
"""


def parse_html(
    url: str,
    body: bytes | str,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    final_url: str | None = None,
) -> PageData:
    """Parse one fetched page into :class:`PageData` without ever raising."""
    header_map = dict(headers or {})
    resolved_final_url = final_url or url
    try:
        html_text = _decode_body(body, header_map)
        parser = _HtmlParser(base_url=resolved_final_url)
        parser.feed(html_text)
        parser.close()
        parser.finalize()
    except Exception:
        return PageData(
            url=url,
            status=status,
            final_url=resolved_final_url,
            content_type=_content_type(header_map),
            headers=header_map,
            error="parse_error",
        )

    text = parser.text
    return PageData(
        url=url,
        status=status,
        final_url=resolved_final_url,
        content_type=_content_type(header_map),
        headers=header_map,
        title=parser.title,
        meta_description=parser.meta_description,
        meta_robots=", ".join(parser.meta_robots) or None,
        x_robots_tag=_header_value(header_map, "x-robots-tag"),
        canonical=parser.canonical,
        html_lang=parser.html_lang,
        h1=parser.h1,
        hreflang=parser.hreflang,
        jsonld=parser.jsonld,
        links=parser.links,
        images=parser.images,
        link_details=parser.link_details,
        inputs=parser.inputs,
        buttons=parser.buttons,
        headings=parser.headings,
        has_main=parser.has_main,
        has_skip_link=parser.has_skip_link,
        text=text[:MAX_STORED_TEXT_CHARS],
        word_count=_word_count(text),
    )


def _word_count(text: str) -> int:
    """Whitespace words, but every CJK ideograph counts as one word.

    Chinese/Japanese pages use few spaces, so ``len(text.split())`` reported a
    substantial page as "thin" and produced a false soft-404 on
    ``/games?lang=zh``. Latin/Cyrillic tokens still count once.
    """
    count = 0
    for token in text.split():
        cjk = sum(1 for char in token if "\u4e00" <= char <= "\u9fff")
        rest = "".join(char for char in token if not "\u4e00" <= char <= "\u9fff")
        count += cjk + (1 if any(char.isalnum() for char in rest) else 0)
    return count


def extract_links(html_text: str, base_url: str) -> list[str]:
    """Return absolute, fragment-free http(s) URLs from every ``<a href>``."""
    parser = _HtmlParser(base_url=base_url)
    try:
        parser.feed(html_text)
        parser.close()
        parser.finalize()
    except Exception:
        return parser.links
    return parser.links


def extract_jsonld(html_text: str) -> list[dict[str, Any]]:
    """Return every valid JSON-LD node; invalid blocks and non-dict nodes are skipped."""
    parser = _HtmlParser(base_url="")
    try:
        parser.feed(html_text)
        parser.close()
        parser.finalize()
    except Exception:
        return parser.jsonld
    return parser.jsonld


class _HtmlParser(HTMLParser):
    """Single-pass collector for the fields :class:`PageData` exposes."""

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._base_href: str | None = None
        self.title: str | None = None
        self.meta_description: str | None = None
        self.meta_robots: list[str] = []
        self.canonical: str | None = None
        self.html_lang: str | None = None
        self.h1: list[str] = []
        self.hreflang: list[dict[str, str]] = []
        self.jsonld: list[dict[str, Any]] = []
        self.links: list[str] = []
        self.images: list[dict[str, str]] = []
        self.link_details: list[dict[str, Any]] = []
        self.inputs: list[dict[str, Any]] = []
        self.buttons: list[dict[str, Any]] = []
        self.headings: list[dict[str, Any]] = []
        self.has_main = False
        self.has_skip_link = False
        self.text_parts: list[str] = []
        self._seen_links: set[str] = set()
        self._seen_hreflang: set[tuple[str, str]] = set()
        self._suppressed = 0
        self._title_seen = False
        self._title_parts: list[str] | None = None
        self._h1_parts: list[str] | None = None
        self._jsonld_parts: list[str] | None = None
        self._heading_level: int | None = None
        self._heading_parts: list[str] | None = None
        self._anchor_detail: dict[str, Any] | None = None
        self._anchor_parts: list[str] | None = None
        self._anchor_href = ""
        self._button_detail: dict[str, Any] | None = None
        self._button_parts: list[str] | None = None
        self._label_stack: list[dict[str, Any]] = []
        self._labels_for: dict[str, list[str]] = {}

    @property
    def text(self) -> str:
        """Collapsed visible text (script/style/noscript and the document title removed)."""
        return _collapse(self.text_parts)

    @property
    def effective_base(self) -> str:
        """Resolution base: the first ``<base href>`` when present, else the page URL."""
        if self._base_href:
            return urljoin(self.base_url, self._base_href)
        return self.base_url

    def finalize(self) -> None:
        """Flush constructs left open at end of input (tolerant parsing)."""
        if self._title_parts is not None:
            self._close_title()
        if self._h1_parts is not None:
            self.h1.append(_collapse(self._h1_parts))
            self._h1_parts = None
        if self._jsonld_parts is not None:
            self._close_jsonld()
        self._flush_heading()
        self._flush_anchor()
        self._flush_button()
        while self._label_stack:
            self._close_label()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)

        if tag in _SUPPRESSED_TAGS:
            self._suppressed += 1
            if tag == "script" and _is_jsonld_script(attributes):
                self._jsonld_parts = []
            return

        if tag == "title":
            self._title_parts = []
            return

        if tag in _BLOCK_TAGS or tag in _BREAK_TAGS:
            self._add_text(" ")

        if tag == "html":
            if self.html_lang is None:
                lang = _attr(attributes, "lang")
                if lang:
                    self.html_lang = lang
        elif tag == "base":
            # The first <base href> wins per the HTML standard; it must appear
            # before any relative URL is used.
            if self._base_href is None:
                href = _attr(attributes, "href")
                if href:
                    self._base_href = href
        elif tag == "meta":
            self._handle_meta(attributes)
        elif tag == "link":
            self._handle_link(attributes)
        elif tag == "a":
            self._flush_anchor()
            href = _attr(attributes, "href")
            if href:
                self._add_link(href, attributes)
        elif tag == "img":
            src = _attr(attributes, "src")
            alt = _attr(attributes, "alt")
            self.images.append(
                {
                    "src": _resolve_url(src, self.effective_base) if src else "",
                    "alt": alt,
                }
            )
            if self._anchor_detail is not None and alt and not self._anchor_detail["image_alt"]:
                self._anchor_detail["image_alt"] = alt
        elif tag == "h1":
            if self._h1_parts is None:
                self._h1_parts = []
            self._start_heading(1)
        elif tag in _HEADING_LEVELS:
            self._start_heading(_HEADING_LEVELS[tag])
        elif tag == "main":
            self.has_main = True
        elif tag == "label":
            self._label_stack.append({"for": _attr(attributes, "for"), "parts": [], "wrapped": []})
        elif tag == "input":
            self._handle_input(attributes)
        elif tag == "button":
            self._start_button(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SUPPRESSED_TAGS:
            if tag == "script" and self._jsonld_parts is not None:
                self._close_jsonld()
            self._suppressed = max(0, self._suppressed - 1)
            return

        if tag == "title":
            if self._title_parts is not None:
                self._close_title()
            return

        if tag in _HEADING_LEVELS:
            self._close_heading(_HEADING_LEVELS[tag])
        if tag == "a":
            self._flush_anchor()
        elif tag == "button":
            self._flush_button()
        elif tag == "label":
            self._close_label()

        if tag == "h1":
            if self._h1_parts is not None:
                self.h1.append(_collapse(self._h1_parts))
                self._h1_parts = None
            self._add_text(" ")
            return

        if tag in _BLOCK_TAGS:
            self._add_text(" ")

    def handle_data(self, data: str) -> None:
        if self._jsonld_parts is not None:
            self._jsonld_parts.append(data)
            return
        if self._suppressed:
            return
        if self._title_parts is not None:
            self._title_parts.append(data)
            return
        if self._h1_parts is not None:
            self._h1_parts.append(data)
        self._capture_text(data)
        self._add_text(data)

    def _capture_text(self, data: str) -> None:
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        if self._anchor_parts is not None:
            self._anchor_parts.append(data)
        if self._button_parts is not None:
            self._button_parts.append(data)
        if self._label_stack:
            self._label_stack[-1]["parts"].append(data)

    def _start_heading(self, level: int) -> None:
        self._flush_heading()
        self._heading_level = level
        self._heading_parts = []

    def _close_heading(self, level: int) -> None:
        if self._heading_level == level:
            self._flush_heading()

    def _flush_heading(self) -> None:
        if self._heading_level is None:
            return
        self.headings.append(
            {"level": self._heading_level, "text": _collapse(self._heading_parts or [])}
        )
        self._heading_level = None
        self._heading_parts = None

    def _start_button(self, attributes: Mapping[str, str | None]) -> None:
        self._flush_button()
        detail: dict[str, Any] = {
            "text": "",
            "aria_label": _attr(attributes, "aria-label"),
            "aria_labelledby": _attr(attributes, "aria-labelledby"),
            "title": _attr(attributes, "title"),
        }
        self.buttons.append(detail)
        self._button_detail = detail
        self._button_parts = []

    def _flush_button(self) -> None:
        if self._button_detail is None:
            return
        self._button_detail["text"] = _collapse(self._button_parts or [])
        self._button_detail = None
        self._button_parts = None

    def _flush_anchor(self) -> None:
        if self._anchor_detail is None:
            return
        detail = self._anchor_detail
        detail["text"] = _collapse(self._anchor_parts or [])
        self._anchor_detail = None
        self._anchor_parts = None
        if not self.has_skip_link and _is_skip_link(self._anchor_href, detail["text"]):
            self.has_skip_link = True

    def _handle_input(self, attributes: Mapping[str, str | None]) -> None:
        input_type = (_attr(attributes, "type") or "text").lower()
        if input_type in _INPUT_BUTTON_TYPES:
            self.buttons.append(
                {
                    "text": _attr(attributes, "value"),
                    "aria_label": _attr(attributes, "aria-label"),
                    "aria_labelledby": _attr(attributes, "aria-labelledby"),
                    "title": _attr(attributes, "title"),
                }
            )
            return
        if input_type in _INPUT_IGNORED_TYPES:
            return

        record: dict[str, Any] = {
            "type": input_type,
            "id": _attr(attributes, "id"),
            "name": _attr(attributes, "name"),
            "aria_label": _attr(attributes, "aria-label"),
            "aria_labelledby": _attr(attributes, "aria-labelledby"),
            "title": _attr(attributes, "title"),
            "placeholder": _attr(attributes, "placeholder"),
            "label": "",
        }
        self.inputs.append(record)
        if self._label_stack:
            self._label_stack[-1]["wrapped"].append(record)
        identifier = record["id"]
        if identifier and identifier in self._labels_for:
            record["label"] = " ".join(self._labels_for[identifier])

    def _close_label(self) -> None:
        if not self._label_stack:
            return
        entry = self._label_stack.pop()
        text = _collapse(entry["parts"])
        identifier = entry["for"]
        targets: list[dict[str, Any]] = []
        if identifier:
            self._labels_for.setdefault(identifier, []).append(text)
            targets.extend(record for record in self.inputs if record["id"] == identifier)
        targets.extend(entry["wrapped"])
        seen: set[int] = set()
        for record in targets:
            # A wrapping label may also carry for="<its own input>": count it once.
            if id(record) in seen:
                continue
            seen.add(id(record))
            _append_label(record, text)

    def _handle_meta(self, attributes: Mapping[str, str | None]) -> None:
        name = _attr(attributes, "name").lower()
        if name == "description":
            if self.meta_description is None:
                self.meta_description = _attr(attributes, "content")
        elif name == "robots":
            content = _attr(attributes, "content")
            if content:
                self.meta_robots.append(content)

    def _handle_link(self, attributes: Mapping[str, str | None]) -> None:
        rel = {token.lower() for token in _attr(attributes, "rel").split()}
        href = _attr(attributes, "href")
        if not href:
            return

        if "canonical" in rel and self.canonical is None:
            self.canonical = _resolve_url(href, self.effective_base)

        if "alternate" not in rel:
            return
        lang = _attr(attributes, "hreflang")
        if not lang:
            return
        url = _resolve_url(href, self.effective_base)
        key = (lang.lower(), url)
        if key in self._seen_hreflang:
            return
        self._seen_hreflang.add(key)
        self.hreflang.append({"lang": lang, "url": url})

    def _add_link(self, href: str, attributes: Mapping[str, str | None]) -> None:
        url = urldefrag(_resolve_url(href, self.effective_base)).url
        if not url or urlsplit(url).scheme.lower() not in _LINK_SCHEMES:
            return
        if url in self._seen_links:
            return
        self._seen_links.add(url)
        self.links.append(url)
        detail: dict[str, Any] = {
            "href": url,
            "text": "",
            "aria_label": _attr(attributes, "aria-label"),
            "aria_labelledby": _attr(attributes, "aria-labelledby"),
            "title": _attr(attributes, "title"),
            "image_alt": "",
        }
        self.link_details.append(detail)
        self._anchor_detail = detail
        self._anchor_parts = []
        self._anchor_href = href.strip()

    def _close_title(self) -> None:
        parts = self._title_parts or []
        self._title_parts = None
        if self._title_seen:
            return
        self._title_seen = True
        self.title = _collapse(parts)

    def _close_jsonld(self) -> None:
        parts = self._jsonld_parts or []
        self._jsonld_parts = None
        raw = "".join(parts).strip()
        if not raw:
            return
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            return
        self.jsonld.extend(_iter_jsonld_items(payload))

    def _add_text(self, value: str) -> None:
        if value:
            self.text_parts.append(value)


def _iter_jsonld_items(payload: Any) -> Iterator[dict[str, Any]]:
    """Flatten top-level arrays and ``@graph`` containers into individual nodes."""
    if isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_jsonld_items(item)
            return
        yield payload
        return
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_jsonld_items(item)


def _is_jsonld_script(attributes: Mapping[str, str | None]) -> bool:
    media_type = _attr(attributes, "type").split(";", 1)[0].strip().lower()
    return media_type == _JSONLD_MEDIA_TYPE


def _attr(attributes: Mapping[str, str | None], name: str) -> str:
    return (attributes.get(name) or "").strip()


def _append_label(record: dict[str, Any], text: str) -> None:
    """Attach one label text to an input record, joining multiple labels with a space."""
    if not text:
        return
    current = record.get("label") or ""
    record["label"] = f"{current} {text}".strip() if current else text


def _is_skip_link(href: str, text: str) -> bool:
    """A same-page fragment link that targets the main landmarks or says "skip"."""
    target = href.strip()
    if not target.startswith("#"):
        return False
    if target.lower() in _SKIP_LINK_TARGETS:
        return True
    return "skip" in text.lower()


def _resolve_url(href: str, base_url: str) -> str:
    try:
        return urljoin(base_url, href)
    except ValueError:
        return href


def _collapse(parts: Iterable[str]) -> str:
    """Join text nodes and collapse every whitespace run (including ``&nbsp;``)."""
    return " ".join("".join(parts).split())


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    raw = _header(headers, name)
    if raw is None:
        return None
    return raw.strip() or None


def _content_type(headers: Mapping[str, str]) -> str:
    raw = _header(headers, "content-type")
    if not raw:
        return "text/html"
    media_type = raw.split(";", 1)[0].strip().lower()
    return media_type or "text/html"


def _decode_body(body: bytes | str, headers: Mapping[str, str]) -> str:
    if isinstance(body, str):
        return body.removeprefix("\ufeff")
    if not body:
        return ""
    normalized = _header_value(headers, "content-type")
    encoding = _charset_from_content_type(normalized)
    try:
        codec = codecs.lookup(encoding).name if encoding else "utf-8"
    except LookupError:
        codec = "utf-8"
    return body.decode(codec, errors="replace").removeprefix("\ufeff")


def _charset_from_content_type(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";")[1:]:
        key, separator, raw = part.partition("=")
        if separator and key.strip().lower() == "charset":
            return raw.strip().strip("\"'") or None
    return None
