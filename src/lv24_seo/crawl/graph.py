"""Internal link graph built from crawled pages (Wave 3, WP3.4).

This module is a pure computation over :class:`PageData`: no network, no clock
and no filesystem access. A crawler normally resolves hrefs to absolute URLs
before filling ``PageData.links``; :func:`normalize_url` then collapses the
usual spelling differences so the same page is one node.

Graph semantics
---------------
* Nodes are pages fetched successfully: ``status == 200`` and ``error is None``.
  Duplicate page URLs collapse into one node (first page wins).
* Edges are unique, normalised ``(source -> target)`` pairs between internal
  URLs, excluding self-links. Sources are graph nodes; a target may be missing
  from the graph (a dangling edge) and is then also reported in ``broken``.
* ``broken`` lists internal targets referenced by *any* page that are absent
  from the 200-node set, i.e. they were not fetched successfully.
* ``inbound`` counts distinct 200-node sources per node, so
  ``sum(inbound.values()) <= edges`` when dangling edges exist.
* Depth is the BFS distance from ``root_url`` along graph nodes. Unreachable
  nodes get ``-1`` and are orphans only when they also have zero inbound links.

All output collections are sorted so two runs over the same pages produce
identical results.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from lv24_seo.crawl.models import PageData

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


@dataclass(slots=True)
class GraphStats:
    """Deterministic summary of the internal link graph."""

    nodes: int
    edges: int
    orphans: list[str]
    broken: list[str]
    depths: dict[str, int]
    max_depth: int
    inbound: dict[str, int]


def normalize_url(url: str) -> str:
    """Return the canonical form of ``url`` used for graph comparisons.

    The scheme and host are lower-cased, the fragment is dropped, a default
    port (``http:80`` / ``https:443``) is removed, the query string is kept and
    a trailing slash is removed unless the URL points at the site root. A
    scheme-relative URL (``//host/path``) and a URL without a path are treated
    as ``https`` and ``/`` respectively, which matches browser behaviour.

    Relative references are returned with only the fragment and trailing slash
    removed; callers are expected to resolve them before crawling.
    """
    raw = url.strip()
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if not host:
        return _normalize_relative(raw)
    scheme = parts.scheme.lower() or "https"
    netloc = f"[{host}]" if ":" in host else host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def build_graph(
    pages: list[PageData],
    *,
    site_host: str,
    root_url: str | None = None,
) -> GraphStats:
    """Build the internal link graph for ``pages``.

    ``site_host`` is the host that counts as internal; a scheme or port in it
    is tolerated. When ``root_url`` is omitted the shortest crawled URL whose
    path is ``/`` is used, falling back to the shortest node URL. An explicit
    root that is not a successfully fetched page is ignored in favour of that
    default so a stale root never turns the whole site into orphans.
    """
    host = _site_host_key(site_host)
    nodes: dict[str, PageData] = {}
    for page in pages:
        if page.status == 200 and page.error is None:
            nodes.setdefault(normalize_url(page.url), page)

    adjacency: dict[str, set[str]] = {url: set() for url in nodes}
    broken: set[str] = set()
    for page in pages:
        source = normalize_url(page.url)
        for link in page.links:
            if _host_key(link) != host:
                continue
            target = normalize_url(link)
            if source in adjacency and target != source:
                adjacency[source].add(target)
            if target not in nodes:
                broken.add(target)

    inbound: dict[str, int] = dict.fromkeys(sorted(nodes), 0)
    edges = 0
    for source in sorted(adjacency):
        edges += len(adjacency[source])
        for target in adjacency[source]:
            if target in inbound:
                inbound[target] += 1

    root = _pick_root(nodes, root_url)
    depths: dict[str, int] = dict.fromkeys(sorted(nodes), -1)
    if root is not None:
        seen = {root}
        queue: deque[tuple[str, int]] = deque([(root, 0)])
        while queue:
            current, depth = queue.popleft()
            depths[current] = depth
            for target in sorted(adjacency[current]):
                if target in depths and target not in seen:
                    seen.add(target)
                    queue.append((target, depth + 1))

    max_depth = max((depth for depth in depths.values() if depth >= 0), default=0)
    orphans = sorted(url for url, depth in depths.items() if depth == -1 and inbound[url] == 0)
    return GraphStats(
        nodes=len(nodes),
        edges=edges,
        orphans=orphans,
        broken=sorted(broken),
        depths=depths,
        max_depth=max_depth,
        inbound=inbound,
    )


def _pick_root(nodes: Mapping[str, PageData], root_url: str | None) -> str | None:
    """Return the BFS start node, honouring ``root_url`` when it is a node."""
    if root_url is not None:
        candidate = normalize_url(root_url)
        if candidate in nodes:
            return candidate
    roots = [url for url in nodes if urlsplit(url).path == "/"]
    candidates = roots or list(nodes)
    if not candidates:
        return None
    return min(candidates, key=lambda url: (len(url), url))


def _site_host_key(site_host: str) -> str:
    """Return the comparison key for a configured host such as ``example.com``."""
    raw = site_host.strip()
    if "://" not in raw and not raw.startswith("//"):
        raw = f"//{raw}"
    return _host_key(raw)


def _host_key(url: str) -> str:
    """Return the lower-cased host (plus non-default port) of ``url``."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme == "" and port in (80, 443):
        return host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        return f"{host}:{port}"
    return host


def _normalize_relative(url: str) -> str:
    """Best-effort canonical form for references that are not absolute URLs."""
    without_fragment = url.split("#", 1)[0]
    if without_fragment in {"", "/"}:
        return without_fragment
    return without_fragment.rstrip("/")
