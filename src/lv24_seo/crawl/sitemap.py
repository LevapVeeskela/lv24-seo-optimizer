"""Sitemap discovery: parse ``<urlset>`` documents and walk ``<sitemapindex>`` trees.

Parsing is pure and offline: namespaces are ignored, gzip bodies are decompressed, and every
failure is reported as a string in :attr:`SitemapDoc.errors` instead of an exception. A crawl
that cannot read one sitemap is still useful, so the caller decides how loud to be about it.

:class:`SitemapReader` never imports the HTTP fetcher; it duck-types the result of
``fetch(url)`` (``status``, ``body``, ``final_url``, ``error`` — see ``crawl.models.FetchResult``).
"""

from __future__ import annotations

import gzip
import io
import zlib
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

MAX_URLS_PER_SITEMAP = 50_000
MAX_BYTES = 50 * 1024 * 1024

_GZIP_MAGIC = b"\x1f\x8b"
_HTTP_SCHEMES = frozenset({"http", "https"})
_INVALID_XML = "invalid_xml"
_INVALID_ROOT = "invalid_root"
_TOO_LARGE = "too_large"


class _FetchResultLike(Protocol):
    """The slice of a fetcher result this module needs (duck-typed on purpose)."""

    status: int
    body: bytes
    final_url: str
    error: str | None


@dataclass(slots=True)
class SitemapDoc:
    """One sitemap document: page URLs, child sitemaps and what went wrong.

    ``lastmod`` maps a page URL to the raw ``<lastmod>`` text (no date parsing here).
    ``truncated`` is true when a protocol or caller cap stopped the document early, so an
    empty-but-truncated result never looks like a complete one.
    """

    url: str
    urls: list[str]
    child_sitemaps: list[str] = field(default_factory=list)
    lastmod: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    errors: list[str] = field(default_factory=list)


def parse_sitemap(xml: bytes | str, *, base_url: str = "") -> SitemapDoc:
    """Parse one sitemap document. Never raises; malformed input becomes ``invalid_xml``."""
    doc = SitemapDoc(url=base_url, urls=[])
    prepared = _prepare(xml, doc)
    if prepared is None:
        return doc

    try:
        # Stdlib-only by contract; ElementTree never resolves external entities, and expat
        # ships entity-expansion limits for untrusted sitemap XML.
        root = ElementTree.fromstring(prepared)  # noqa: S314
    except ElementTree.ParseError:
        doc.errors.append(_INVALID_XML)
        return doc

    root_name = _local_name(root.tag)
    if root_name == "urlset":
        _read_urlset(root, doc, base_url)
    elif root_name == "sitemapindex":
        _read_index(root, doc, base_url)
    else:
        doc.errors.append(_INVALID_ROOT)
    return doc


class SitemapReader:
    """Breadth-first walk over a sitemap tree with a global URL cap and a depth cap.

    The starting document is depth 0; sitemaps linked from it are depth 1, and so on. A
    document at ``max_depth`` is still read, but its own children are only recorded (in
    :attr:`SitemapDoc.child_sitemaps`), not fetched.
    """

    def __init__(
        self,
        fetch: Callable[[str], _FetchResultLike],
        *,
        max_urls: int = MAX_URLS_PER_SITEMAP,
        max_depth: int = 2,
        max_docs: int = 100,
    ) -> None:
        self._fetch = fetch
        self._max_urls = max(0, max_urls)
        self._max_depth = max(0, max_depth)
        self._max_docs = max(1, max_docs)

    def read(self, sitemap_url: str) -> SitemapDoc:
        """Read ``sitemap_url`` and its child sitemaps; fetch failures become ``errors``."""
        doc = SitemapDoc(url=sitemap_url, urls=[])
        queue: deque[tuple[str, int]] = deque([(sitemap_url, 0)])
        seen_sitemaps = {sitemap_url}
        seen_urls: set[str] = set()
        docs_read = 0

        while queue:
            if docs_read >= self._max_docs:
                doc.errors.append("sitemap_docs_cap")
                doc.truncated = True
                break
            current, depth = queue.popleft()
            # Failed fetches consume the same outbound budget as successful ones.
            docs_read += 1
            result, failure = self._load(current)
            if failure is not None:
                doc.errors.append(failure)
                continue
            assert result is not None  # narrowed by _load's contract
            page = parse_sitemap(result.body, base_url=result.final_url or current)
            doc.errors.extend(page.errors)
            doc.truncated = doc.truncated or page.truncated

            for child in page.child_sitemaps:
                if child in seen_sitemaps:
                    continue
                seen_sitemaps.add(child)
                doc.child_sitemaps.append(child)
                if depth < self._max_depth:
                    queue.append((child, depth + 1))
                else:
                    # Depth cutoff: the child is known but was deliberately not
                    # read, so the inventory is explicitly incomplete.
                    doc.truncated = True

            for url in page.urls:
                if url in seen_urls:
                    continue
                if len(doc.urls) >= self._max_urls:
                    doc.truncated = True
                    break
                seen_urls.add(url)
                doc.urls.append(url)
                if url in page.lastmod:
                    doc.lastmod.setdefault(url, page.lastmod[url])

            if len(doc.urls) >= self._max_urls and queue:
                # The cap is global: do not spend more fetches on URLs we would have to drop.
                doc.truncated = True
                break
        return doc

    def _load(self, url: str) -> tuple[_FetchResultLike | None, str | None]:
        try:
            result = self._fetch(url)
        except Exception as exc:  # the reader reports failures, never raises them
            return None, str(exc) or type(exc).__name__
        error = getattr(result, "error", None)
        if error:
            return None, str(error)
        status = getattr(result, "status", 0)
        if status != 200:
            return None, f"http_{status}"
        return result, None


def _local_name(tag: str) -> str:
    """Drop the ``{namespace}`` prefix; sitemap namespaces are irrelevant to the consumer."""
    return tag.rsplit("}", 1)[-1]


def _prepare(xml: bytes | str, doc: SitemapDoc) -> bytes | str | None:
    """Enforce the byte cap and decompress gzip magic; ``None`` means "already reported"."""
    if isinstance(xml, str):
        if len(xml) > MAX_BYTES:
            doc.errors.append(_TOO_LARGE)
            doc.truncated = True
            return None
        return xml

    if len(xml) > MAX_BYTES:
        doc.errors.append(_TOO_LARGE)
        doc.truncated = True
        return None
    if not xml.startswith(_GZIP_MAGIC):
        return xml

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(xml)) as gzipped:
            # Bounded read: a gzip body must not be able to inflate past the byte cap.
            inflated = gzipped.read(MAX_BYTES + 1)
    except (OSError, EOFError, zlib.error):
        doc.errors.append(_INVALID_XML)
        return None
    if len(inflated) > MAX_BYTES:
        doc.errors.append(_TOO_LARGE)
        doc.truncated = True
        return None
    return inflated


def _entries(root: ElementTree.Element, name: str) -> Iterator[ElementTree.Element]:
    for element in root:
        if _local_name(element.tag) == name:
            yield element


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def _resolve_loc(loc: str | None, base_url: str) -> str | None:
    """Absolute http(s) URL or ``None`` for empty, non-http(s) and unresolvable locations."""
    if not loc:
        return None
    try:
        resolved = urljoin(base_url, loc)
        parts = urlsplit(resolved)
    except ValueError:
        return None
    if parts.scheme.lower() not in _HTTP_SCHEMES or not parts.netloc:
        return None
    return resolved


def _read_urlset(root: ElementTree.Element, doc: SitemapDoc, base_url: str) -> None:
    seen: set[str] = set()
    for count, entry in enumerate(_entries(root, "url"), start=1):
        if count > MAX_URLS_PER_SITEMAP:
            doc.truncated = True
            break
        url = _resolve_loc(_child_text(entry, "loc"), base_url)
        if url is None or url in seen:
            continue
        seen.add(url)
        doc.urls.append(url)
        lastmod = _child_text(entry, "lastmod")
        if lastmod:
            doc.lastmod[url] = lastmod


def _read_index(root: ElementTree.Element, doc: SitemapDoc, base_url: str) -> None:
    seen: set[str] = set()
    for count, entry in enumerate(_entries(root, "sitemap"), start=1):
        if count > MAX_URLS_PER_SITEMAP:
            doc.truncated = True
            break
        child = _resolve_loc(_child_text(entry, "loc"), base_url)
        if child is None or child in seen:
            continue
        seen.add(child)
        doc.child_sitemaps.append(child)
