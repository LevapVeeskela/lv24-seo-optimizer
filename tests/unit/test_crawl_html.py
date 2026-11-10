"""Offline tests for the stdlib HTML extractor (inline fixtures, no network)."""

from __future__ import annotations

import textwrap
from html.parser import HTMLParser

import pytest

from lv24_seo.crawl import html as html_module
from lv24_seo.crawl.html import extract_jsonld, extract_links, parse_html

FULL_PAGE = textwrap.dedent(
    """\
    <!DOCTYPE html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <title>  Экскурсии по Минску — LV24 </title>
      <meta name="description" content="Афиша и экскурсии по Минску">
      <meta name="robots" content="index, follow">
      <link rel="canonical" href="/guides/minsk">
      <link rel="alternate" hreflang="ru" href="/guides/minsk">
      <link rel="alternate" hreflang="en" href="https://lv24.by/en/guides/minsk">
      <script type="application/ld+json">
      {"@context": "https://schema.org", "@type": "TouristAttraction", "name": "Минск"}
      </script>
    </head>
    <body>
      <h1>Минск</h1>
      <h1>Экскурсии</h1>
      <p>Привет мир! Это тестовая страница про Минск.</p>
      <a href="/places/1">Место 1</a>
      <a href="https://example.com/page#frag">Внешняя</a>
      <a href="mailto:hi@example.com">Почта</a>
      <a href="tel:+375291234567">Телефон</a>
      <a href="javascript:void(0)">JS</a>
      <a href="/places/1#reviews">Дубль</a>
      <img src="/img/minsk.jpg" alt="Минск">
      <img src="https://cdn.example.com/x.png">
      <script>var x = 1;</script>
      <style>.x { color: red }</style>
      <noscript>JS отключён</noscript>
    </body>
    </html>
    """
)


def test_full_page_extracts_every_field() -> None:
    page = parse_html(
        "https://lv24.by/original",
        FULL_PAGE,
        status=200,
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )

    assert page.url == "https://lv24.by/original"
    assert page.final_url == "https://lv24.by/original"
    assert page.status == 200
    assert page.content_type == "text/html"
    assert page.title == "Экскурсии по Минску — LV24"
    assert page.meta_description == "Афиша и экскурсии по Минску"
    assert page.meta_robots == "index, follow"
    assert page.x_robots_tag == "noindex, nofollow"
    assert page.canonical == "https://lv24.by/guides/minsk"
    assert page.html_lang == "ru"
    assert page.h1 == ["Минск", "Экскурсии"]
    assert page.hreflang == [
        {"lang": "ru", "url": "https://lv24.by/guides/minsk"},
        {"lang": "en", "url": "https://lv24.by/en/guides/minsk"},
    ]
    assert [item["@type"] for item in page.jsonld] == ["TouristAttraction"]
    assert page.links == ["https://lv24.by/places/1", "https://example.com/page"]
    assert page.images == [
        {"src": "https://lv24.by/img/minsk.jpg", "alt": "Минск"},
        {"src": "https://cdn.example.com/x.png", "alt": ""},
    ]
    assert "Привет мир!" in page.text
    assert "var x = 1" not in page.text
    assert "color: red" not in page.text
    assert "JS отключён" not in page.text
    assert page.error is None
    assert page.word_count == len(page.text.split())
    assert page.is_indexable is False


def test_canonical_and_links_resolve_against_final_url() -> None:
    html_text = (
        '<html><head><link rel="Canonical" href="../canonical"></head>'
        '<body><a href="child?x=1#top">child</a></body></html>'
    )

    page = parse_html("https://lv24.by/old", html_text, final_url="https://lv24.by/new/path/")

    assert page.url == "https://lv24.by/old"
    assert page.final_url == "https://lv24.by/new/path/"
    assert page.canonical == "https://lv24.by/new/canonical"
    assert page.links == ["https://lv24.by/new/path/child?x=1"]


def test_hreflang_entries_are_absolute_and_need_a_lang() -> None:
    html_text = (
        '<link rel="alternate" hreflang="ru-BY" href="/be">'
        '<link rel="alternate" hreflang="x-default" href="https://lv24.by/">'
        '<link rel="alternate" href="/no-lang">'
        '<link rel="stylesheet" href="/app.css">'
    )

    page = parse_html("https://lv24.by/page", html_text)

    assert page.hreflang == [
        {"lang": "ru-BY", "url": "https://lv24.by/be"},
        {"lang": "x-default", "url": "https://lv24.by/"},
    ]


def test_jsonld_expands_graph_and_skips_invalid_blocks() -> None:
    html_text = textwrap.dedent(
        """\
        <html><head>
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Organization", "name": "LV24"}
        </script>
        <script type="application/ld+json">
        {"@context": "https://schema.org",
         "@graph": [{"@type": "WebSite", "name": "LV24"}, {"@type": "BreadcrumbList"}]}
        </script>
        <script type="application/ld+json">{not-json</script>
        <script type="application/ld+json">["ignored", 42, {"@type": "Thing"}]</script>
        <script type="text/javascript">{"@type": "NotJsonLd"}</script>
        </head></html>
        """
    )

    page = parse_html("https://lv24.by/", html_text)

    assert page.jsonld == [
        {"@context": "https://schema.org", "@type": "Organization", "name": "LV24"},
        {"@type": "WebSite", "name": "LV24"},
        {"@type": "BreadcrumbList"},
        {"@type": "Thing"},
    ]
    assert extract_jsonld(html_text) == page.jsonld


def test_word_count_is_cyrillic_safe() -> None:
    html_text = (
        "<html><body><p>Привет, мир!</p><p>Это проверка подсчёта слов.</p><p>Ёлка</p></body></html>"
    )

    page = parse_html("https://lv24.by/", html_text)

    assert page.text == "Привет, мир! Это проверка подсчёта слов. Ёлка"
    assert page.word_count == 7


def test_x_robots_tag_header_is_case_insensitive() -> None:
    page = parse_html(
        "https://lv24.by/",
        "<html><head><meta name='robots' content='noarchive'></head></html>",
        headers={"x-ROBOTS-tag": "  noindex, nofollow  "},
    )

    assert page.x_robots_tag == "noindex, nofollow"
    assert page.meta_robots == "noarchive"
    assert page.robots_directives == {"noarchive", "noindex", "nofollow"}
    assert page.is_indexable is False


def test_meta_charset_bytes_are_decoded_as_utf8_by_default() -> None:
    body = (
        '<html><head><meta charset="utf-8"><title>Ёлка</title></head>'
        "<body><p>Привет, мир</p></body></html>"
    ).encode()

    page = parse_html("https://lv24.by/", body)

    assert page.title == "Ёлка"
    assert page.text == "Привет, мир"


def test_content_type_charset_header_is_honoured() -> None:
    body = "<html><body><p>Привет</p></body></html>".encode("cp1251")

    page = parse_html(
        "https://lv24.by/",
        body,
        headers={"Content-Type": "text/html; charset=windows-1251"},
    )

    assert page.text == "Привет"
    assert page.content_type == "text/html"


def test_unknown_charset_falls_back_to_utf8_replacement() -> None:
    page = parse_html(
        "https://lv24.by/",
        b"<p>ok \xff</p>",
        headers={"Content-Type": "text/html; charset=no-such-charset"},
    )

    assert page.error is None
    assert "ok" in page.text
    assert "\ufffd" in page.text


def test_empty_body_returns_defaults() -> None:
    page = parse_html("https://lv24.by/", b"")

    assert page.url == "https://lv24.by/"
    assert page.final_url == "https://lv24.by/"
    assert page.status == 200
    assert page.headers == {}
    assert page.content_type == "text/html"
    assert page.title is None
    assert page.meta_description is None
    assert page.meta_robots is None
    assert page.x_robots_tag is None
    assert page.canonical is None
    assert page.html_lang is None
    assert page.h1 == []
    assert page.hreflang == []
    assert page.jsonld == []
    assert page.links == []
    assert page.images == []
    assert page.text == ""
    assert page.word_count == 0
    assert page.error is None


def test_links_are_deduped_absolute_and_fragment_free() -> None:
    html_text = textwrap.dedent(
        """\
        <html><body>
        <a href="/a#one">1</a>
        <a href="/a#two">2</a>
        <a href="/a">3</a>
        <a href="  mailto:hi@example.com  ">mail</a>
        <a href="tel:+375291234567">tel</a>
        <a href="javascript:void(0)">js</a>
        <a href="https://other.example/x#frag">out</a>
        <a>no href</a>
        </body></html>
        """
    )

    page = parse_html("https://lv24.by/root/", html_text)

    assert page.links == ["https://lv24.by/a", "https://other.example/x"]


def test_extract_links_resolves_relative_urls() -> None:
    html_text = '<a href="next">next</a><a href="../up#x">up</a>'

    assert extract_links(html_text, "https://lv24.by/dir/page") == [
        "https://lv24.by/dir/next",
        "https://lv24.by/up",
    ]


def test_visible_text_skips_non_rendered_elements() -> None:
    html_text = textwrap.dedent(
        """\
        <html><head><title>Заголовок</title><style>.a { color: red }</style></head>
        <body>
          <p>Первый <b>абзац</b>.</p>
          <script>var secret = 1;</script>
          <noscript>Нет JS</noscript>
          <p>Второй&nbsp;абзац</p>
        </body></html>
        """
    )

    page = parse_html("https://lv24.by/", html_text)

    assert page.text == "Первый абзац. Второй абзац"
    assert page.title == "Заголовок"


def test_malformed_markup_never_raises() -> None:
    html_text = "<html><body><h1>Заголовок<p>текст<a href='/x'>ссылка<img src='/i.png' alt='и'>"

    page = parse_html("https://lv24.by/", html_text)

    assert page.error is None
    assert len(page.h1) == 1
    assert "Заголовок" in page.h1[0]
    assert page.links == ["https://lv24.by/x"]
    assert page.images == [{"src": "https://lv24.by/i.png", "alt": "и"}]


def test_catastrophic_parser_failure_returns_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(self: HTMLParser, data: str) -> None:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(html_module._HtmlParser, "feed", explode)

    page = parse_html(
        "https://lv24.by/",
        b"<p>hi</p>",
        status=503,
        headers={"X-Test": "1"},
    )

    assert page.error == "parse_error"
    assert page.url == "https://lv24.by/"
    assert page.status == 503
    assert page.headers == {"X-Test": "1"}
