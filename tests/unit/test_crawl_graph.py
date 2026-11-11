"""Contract tests for the internal link graph (Wave 3, WP3.4)."""

from __future__ import annotations

import pytest

from lv24_seo.crawl.graph import GraphStats, build_graph, normalize_url
from lv24_seo.crawl.models import PageData

SITE = "example.com"
HOME = "https://example.com/"


def make_page(
    url: str,
    links: list[str] | None = None,
    *,
    status: int = 200,
    error: str | None = None,
) -> PageData:
    """Build a minimal PageData for graph tests."""
    return PageData(
        url=url,
        status=status,
        final_url=url,
        links=list(links or []),
        error=error,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/Path/", "https://example.com/Path/"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/a/#team", "https://example.com/a/"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com/a/?q=1&b=2", "https://example.com/a/?q=1&b=2"),
        ("https://example.com/?q=1", "https://example.com/?q=1"),
        ("  https://example.com/a#frag  ", "https://example.com/a"),
        ("//example.com/a/", "https://example.com/a/"),
        ("/a/#x", "/a/"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_three_page_chain_depths_and_counts() -> None:
    pages = [
        make_page(HOME, ["https://example.com/a"]),
        make_page("https://example.com/a", ["https://example.com/b"]),
        make_page("https://example.com/b"),
    ]

    stats = build_graph(pages, site_host=SITE)

    assert isinstance(stats, GraphStats)
    assert stats.nodes == 3
    assert stats.edges == 2
    assert stats.depths == {
        HOME: 0,
        "https://example.com/a": 1,
        "https://example.com/b": 2,
    }
    assert stats.max_depth == 2
    assert stats.inbound == {HOME: 0, "https://example.com/a": 1, "https://example.com/b": 1}
    assert stats.orphans == []
    assert stats.broken == []


def test_unreachable_nodes_are_orphans_only_without_inbound_links() -> None:
    pages = [
        make_page(HOME, ["https://example.com/a"]),
        make_page("https://example.com/a"),
        # An island: both pages are unreachable, but only /u has no inbound link.
        make_page("https://example.com/u", ["https://example.com/v"]),
        make_page("https://example.com/v"),
    ]

    stats = build_graph(pages, site_host=SITE)

    assert stats.depths["https://example.com/u"] == -1
    assert stats.depths["https://example.com/v"] == -1
    assert stats.inbound["https://example.com/v"] == 1
    assert stats.orphans == ["https://example.com/u"]
    assert stats.max_depth == 1


def test_broken_internal_target_is_reported_and_external_link_is_ignored() -> None:
    pages = [
        make_page(HOME, ["https://example.com/gone"]),
        make_page("https://example.com/gone", status=404),
    ]

    stats = build_graph(pages, site_host=SITE)

    assert stats.broken == ["https://example.com/gone"]
    assert stats.edges == 1
    assert stats.max_depth == 0


def test_unvisited_target_is_not_reported_as_broken() -> None:
    pages = [
        make_page(
            HOME,
            ["https://example.com/missing/", "https://other.test/nope"],
        )
    ]

    stats = build_graph(pages, site_host=SITE)

    assert stats.broken == []
    assert stats.unvisited == ["https://example.com/missing/"]
    assert stats.edges == 1
    assert stats.max_depth == 0


def test_non_200_and_errored_pages_are_not_nodes() -> None:
    pages = [
        make_page(HOME, ["https://example.com/gone", "https://example.com/boom"]),
        make_page("https://example.com/gone", ["https://example.com/deep"], status=404),
        make_page("https://example.com/boom", status=200, error="timeout"),
    ]

    stats = build_graph(pages, site_host=SITE)

    assert stats.nodes == 1
    assert stats.edges == 2
    assert stats.broken == ["https://example.com/gone"]
    assert stats.failed == ["https://example.com/boom"]
    assert stats.unvisited == ["https://example.com/deep"]
    assert stats.depths == {HOME: 0}


def test_external_links_do_not_create_edges_or_broken_targets() -> None:
    pages = [
        make_page(
            HOME,
            [
                "https://other.test/a",
                "http://example.com.evil/a",
                "https://sub.example.com/a",
                "mailto:hello@example.com",
            ],
        ),
        make_page("https://example.com/orphan"),
    ]

    stats = build_graph(pages, site_host=SITE)

    assert stats.nodes == 2
    assert stats.edges == 0
    assert stats.broken == []
    assert stats.orphans == ["https://example.com/orphan"]
    assert stats.max_depth == 0


def test_trailing_slash_spellings_are_distinct_nodes_until_fetched() -> None:
    pages = [
        make_page(HOME, ["https://example.com/about/#team", "https://EXAMPLE.com/about"]),
        make_page("https://example.com/about/"),
        make_page("https://example.com/about#frag"),
    ]

    stats = build_graph(pages, site_host=SITE)

    # /about and /about/ are different resources until a fetch proves otherwise.
    assert stats.nodes == 3
    assert stats.edges == 2
    assert stats.depths == {
        HOME: 0,
        "https://example.com/about": 1,
        "https://example.com/about/": 1,
    }


def test_self_links_are_ignored() -> None:
    pages = [make_page(HOME, [HOME, "https://example.com/#top", "https://example.com"])]

    stats = build_graph(pages, site_host=SITE)

    assert stats.nodes == 1
    assert stats.edges == 0
    assert stats.inbound == {HOME: 0}
    assert stats.depths == {HOME: 0}
    assert stats.orphans == []


def test_cycle_terminates_with_shortest_depths() -> None:
    pages = [
        make_page(HOME, ["https://example.com/a"]),
        make_page("https://example.com/a", [HOME, "https://example.com/b"]),
        make_page("https://example.com/b", ["https://example.com/a"]),
    ]

    stats = build_graph(pages, site_host=SITE)

    assert stats.edges == 4
    assert stats.depths == {
        HOME: 0,
        "https://example.com/a": 1,
        "https://example.com/b": 2,
    }
    assert stats.max_depth == 2


def test_explicit_root_changes_depths_but_inbound_still_counts() -> None:
    pages = [
        make_page(HOME, ["https://example.com/a"]),
        make_page("https://example.com/a", ["https://example.com/b"]),
        make_page("https://example.com/b"),
        # /u is unreachable and never linked; it links back to HOME.
        make_page("https://example.com/u", [HOME]),
    ]

    stats = build_graph(pages, site_host=SITE, root_url="https://example.com/a")

    assert stats.depths == {
        HOME: -1,
        "https://example.com/a": 0,
        "https://example.com/b": 1,
        "https://example.com/u": -1,
    }
    assert stats.max_depth == 1
    # HOME is unreachable from the explicit root but still has an inbound link
    # (from the island page), so only /u is an orphan.
    assert stats.inbound[HOME] == 1
    assert stats.orphans == ["https://example.com/u"]


def test_default_root_falls_back_to_shortest_node_without_root_path() -> None:
    pages = [
        make_page("https://example.com/b/"),
        make_page("https://example.com/a"),
    ]

    stats = build_graph(pages, site_host=SITE)

    assert stats.depths == {"https://example.com/a": 0, "https://example.com/b/": -1}
    assert stats.max_depth == 0
    assert stats.orphans == ["https://example.com/b/"]


def test_empty_input_is_a_valid_empty_graph() -> None:
    stats = build_graph([], site_host=SITE)

    assert stats == GraphStats(
        nodes=0,
        edges=0,
        orphans=[],
        broken=[],
        depths={},
        max_depth=0,
        inbound={},
    )


def test_output_is_deterministic_regardless_of_page_order() -> None:
    pages = [
        make_page(HOME, ["https://example.com/c", "https://example.com/b"]),
        make_page("https://example.com/b", ["https://example.com/a"]),
        make_page("https://example.com/a", ["https://example.com/b"]),
        make_page("https://example.com/c", ["https://example.com/missing"]),
    ]

    forward = build_graph(pages, site_host=SITE)
    backward = build_graph(list(reversed(pages)), site_host=SITE)

    assert forward == backward
    assert forward.depths == {
        HOME: 0,
        "https://example.com/a": 2,
        "https://example.com/b": 1,
        "https://example.com/c": 1,
    }
