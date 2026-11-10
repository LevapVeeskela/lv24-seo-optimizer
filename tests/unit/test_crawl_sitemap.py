"""Offline tests for the sitemap parser and reader (fake fetcher, no network)."""

from __future__ import annotations

import gzip
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest

from lv24_seo.crawl import sitemap as sitemap_module
from lv24_seo.crawl.sitemap import SitemapReader, parse_sitemap

_XMLNS = ' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def _urlset(entries: Sequence[tuple[str, str | None]]) -> bytes:
    items = []
    for loc, lastmod in entries:
        item = f"<url><loc>{loc}</loc>"
        if lastmod is not None:
            item += f"<lastmod>{lastmod}</lastmod>"
        items.append(item + "</url>")
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><urlset{_XMLNS}>{"".join(items)}</urlset>'
    ).encode()


def _sitemapindex(locs: Sequence[str]) -> bytes:
    items = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex{_XMLNS}>{items}</sitemapindex>'
    ).encode()


@dataclass(slots=True)
class FakeResult:
    """Stand-in for ``crawl.models.FetchResult`` (the reader only duck-types it)."""

    status: int = 200
    body: bytes = b""
    final_url: str = ""
    error: str | None = None


class FakeFetch:
    def __init__(self, results: Mapping[str, FakeResult]) -> None:
        self.results = dict(results)
        self.calls: list[str] = []

    def __call__(self, url: str) -> FakeResult:
        self.calls.append(url)
        return self.results.get(url, FakeResult(status=404))


def test_parse_urlset_reads_urls_and_raw_lastmod() -> None:
    xml = _urlset(
        [
            ("https://example.com/", "2026-11-01T10:00:00+00:00"),
            ("https://example.com/about", None),
        ]
    )

    doc = parse_sitemap(xml.decode("utf-8"), base_url="https://example.com/sitemap.xml")

    assert doc.url == "https://example.com/sitemap.xml"
    assert doc.urls == ["https://example.com/", "https://example.com/about"]
    assert doc.lastmod == {"https://example.com/": "2026-11-01T10:00:00+00:00"}
    assert doc.child_sitemaps == []
    assert doc.errors == []
    assert doc.truncated is False


def test_parse_ignores_namespaces_and_extraneous_whitespace() -> None:
    xml = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:x="http://www.w3.org/1999/xhtml">'
        "<url><x:loc> https://example.com/a </x:loc></url>"
        "<url><loc>https://example.com/a</loc></url>"
        "<url><loc>https://example.com/b</loc></url>"
        "</urlset>"
    )

    doc = parse_sitemap(xml)

    assert doc.urls == ["https://example.com/a", "https://example.com/b"]


def test_sitemapindex_merges_two_children_breadth_first() -> None:
    root = "https://example.com/sitemap.xml"
    first = "https://example.com/sitemap-pages.xml"
    second = "https://example.com/sitemap-news.xml"
    fetch = FakeFetch(
        {
            root: FakeResult(body=_sitemapindex([first, second]), final_url=root),
            first: FakeResult(
                body=_urlset(
                    [("https://example.com/a", "2026-11-01"), ("https://example.com/b", None)]
                ),
                final_url=first,
            ),
            second: FakeResult(
                body=_urlset([("https://example.com/c", "2026-11-02")]), final_url=second
            ),
        }
    )

    doc = SitemapReader(fetch).read(root)

    assert fetch.calls == [root, first, second]
    assert doc.url == root
    assert doc.urls == ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
    assert doc.child_sitemaps == [first, second]
    assert doc.lastmod == {
        "https://example.com/a": "2026-11-01",
        "https://example.com/c": "2026-11-02",
    }
    assert doc.errors == []
    assert doc.truncated is False


def test_gzip_body_is_decompressed() -> None:
    url = "https://example.com/sitemap.xml.gz"
    xml = _urlset([("https://example.com/a", None)])

    doc = SitemapReader(FakeFetch({url: FakeResult(body=gzip.compress(xml), final_url=url)})).read(
        url
    )

    assert doc.urls == ["https://example.com/a"]
    assert doc.errors == []


def test_malformed_xml_reports_invalid_xml_without_raising() -> None:
    doc = parse_sitemap(b"<urlset><url><loc>https://example.com/a</loc>")

    assert doc.urls == []
    assert doc.child_sitemaps == []
    assert doc.errors == ["invalid_xml"]


def test_corrupt_gzip_reports_invalid_xml() -> None:
    doc = parse_sitemap(b"\x1f\x8bnot-really-gzip")

    assert doc.urls == []
    assert doc.errors == ["invalid_xml"]


def test_urlset_stops_at_the_entries_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sitemap_module, "MAX_URLS_PER_SITEMAP", 3)
    entries = [(f"https://example.com/{index}", None) for index in range(5)]

    doc = parse_sitemap(_urlset(entries))

    assert [url.rsplit("/", 1)[-1] for url in doc.urls] == ["0", "1", "2"]
    assert doc.truncated is True
    assert doc.errors == []


def test_relative_locs_are_resolved_against_the_document_url() -> None:
    doc = parse_sitemap(
        _urlset(
            [
                ("/about", None),
                ("faq", None),
                ("https://other.example.com/a", None),
                ("mailto:hello@example.com", None),
                ("ftp://example.com/file", None),
            ]
        ),
        base_url="https://example.com/sitemaps/sitemap.xml",
    )

    assert doc.urls == [
        "https://example.com/about",
        "https://example.com/sitemaps/faq",
        "https://other.example.com/a",
    ]


def test_reader_resolves_relative_children_against_the_final_url() -> None:
    root = "https://example.com/sitemap.xml"
    final = "https://cdn.example.com/mirror/sitemap.xml"
    child = "https://cdn.example.com/mirror/sitemap-pages.xml"
    fetch = FakeFetch(
        {
            root: FakeResult(body=_sitemapindex(["sitemap-pages.xml"]), final_url=final),
            child: FakeResult(body=_urlset([("https://example.com/a", None)]), final_url=child),
        }
    )

    doc = SitemapReader(fetch).read(root)

    assert fetch.calls == [root, child]
    assert doc.child_sitemaps == [child]
    assert doc.urls == ["https://example.com/a"]


def test_reader_dedupes_urls_across_child_sitemaps() -> None:
    root = "https://example.com/sitemap.xml"
    first = "https://example.com/first.xml"
    second = "https://example.com/second.xml"
    fetch = FakeFetch(
        {
            root: FakeResult(body=_sitemapindex([first, second]), final_url=root),
            first: FakeResult(
                body=_urlset([("https://example.com/a", None), ("https://example.com/b", None)]),
                final_url=first,
            ),
            second: FakeResult(
                body=_urlset(
                    [
                        ("https://example.com/b", None),
                        ("https://example.com/c", None),
                        ("https://example.com/a", None),
                    ]
                ),
                final_url=second,
            ),
        }
    )

    doc = SitemapReader(fetch).read(root)

    assert doc.urls == ["https://example.com/a", "https://example.com/b", "https://example.com/c"]


def test_reader_applies_the_global_url_cap() -> None:
    root = "https://example.com/sitemap.xml"
    first = "https://example.com/first.xml"
    second = "https://example.com/second.xml"
    fetch = FakeFetch(
        {
            root: FakeResult(body=_sitemapindex([first, second]), final_url=root),
            first: FakeResult(
                body=_urlset([("https://example.com/a", None), ("https://example.com/b", None)]),
                final_url=first,
            ),
            second: FakeResult(
                body=_urlset([("https://example.com/c", None), ("https://example.com/d", None)]),
                final_url=second,
            ),
        }
    )

    doc = SitemapReader(fetch, max_urls=3).read(root)

    assert doc.urls == ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
    assert doc.truncated is True


def test_reader_does_not_fetch_past_max_depth() -> None:
    root = "https://example.com/root.xml"
    child = "https://example.com/child.xml"
    grandchild = "https://example.com/grandchild.xml"
    fetch = FakeFetch(
        {
            root: FakeResult(body=_sitemapindex([child]), final_url=root),
            child: FakeResult(body=_sitemapindex([grandchild]), final_url=child),
            grandchild: FakeResult(body=_urlset([("https://example.com/deep", None)])),
        }
    )

    doc = SitemapReader(fetch, max_depth=1).read(root)

    assert fetch.calls == [root, child]
    assert doc.urls == []
    assert doc.child_sitemaps == [child, grandchild]


def test_fetch_error_is_reported_without_raising() -> None:
    timeout_url = "https://example.com/timeout.xml"
    timeout = SitemapReader(FakeFetch({timeout_url: FakeResult(status=0, error="timeout")})).read(
        timeout_url
    )
    assert timeout.errors == ["timeout"]
    assert timeout.urls == []
    assert timeout.child_sitemaps == []

    missing_url = "https://example.com/missing.xml"
    missing = SitemapReader(FakeFetch({missing_url: FakeResult(status=404)})).read(missing_url)
    assert missing.errors == ["http_404"]
    assert missing.urls == []


def test_fetch_exception_is_reported_without_raising() -> None:
    def boom(_url: str) -> FakeResult:
        raise RuntimeError("connect failed")

    doc = SitemapReader(boom).read("https://example.com/sitemap.xml")

    assert doc.errors == ["connect failed"]
    assert doc.urls == []
