"""Sitemap ↔ crawl coverage diff: buckets, honesty flags and caps."""

from __future__ import annotations

from lv24_seo.analysis.coverage import BUCKETS, sitemap_crawl_diff
from lv24_seo.crawl.models import PageData

SITE = "https://example.com"


def _page(path: str, *, links: tuple[str, ...] = (), **kwargs: object) -> PageData:
    url = f"{SITE}{path}"
    defaults: dict[str, object] = {
        "url": url,
        "status": 200,
        "final_url": url,
        "content_type": "text/html; charset=utf-8",
        "links": [f"{SITE}{link}" for link in links],
        "redirect_chain": [url],
    }
    defaults.update(kwargs)
    return PageData(**defaults)  # type: ignore[arg-type]


def _site() -> list[PageData]:
    return [
        _page("/", links=("/linked", "/extra", "/gone", "/moved", "/hidden")),
        _page("/linked", links=("/",)),
        _page("/orphan"),
        _page("/extra"),
        _page("/gone", status=404),
        _page(
            "/moved",
            final_url=f"{SITE}/linked",
            redirect_chain=[f"{SITE}/moved", f"{SITE}/linked"],
        ),
        _page("/hidden", meta_robots="noindex, follow"),
        _page("/private", status=0, error="robots_blocked"),
        _page("/flaky", status=0, error="timeout"),
    ]


SITEMAP = [
    f"{SITE}/",
    f"{SITE}/linked",
    f"{SITE}/orphan",
    f"{SITE}/gone",
    f"{SITE}/moved",
    f"{SITE}/hidden",
    f"{SITE}/private",
    f"{SITE}/flaky",
    f"{SITE}/never-fetched",
]


def test_every_disagreement_lands_in_exactly_one_bucket() -> None:
    diff = sitemap_crawl_diff(_site(), SITEMAP)
    buckets = diff.to_dict()["buckets"]

    assert buckets["unlinked"] == [f"{SITE}/orphan"]
    assert buckets["missing_from_sitemap"] == [f"{SITE}/extra"]
    assert buckets["not_ok"] == [f"{SITE}/gone", f"{SITE}/moved"]
    assert buckets["noindex"] == [f"{SITE}/hidden"]
    assert buckets["blocked"] == [f"{SITE}/private"]
    # A network failure and a never-fetched URL are unknown, never "broken".
    assert buckets["unvisited"] == [f"{SITE}/flaky", f"{SITE}/never-fetched"]
    # The root is linked from /linked; nothing claims it is an orphan.
    assert f"{SITE}/" not in buckets["unlinked"]
    assert diff.sitemap_urls == len(SITEMAP)


def test_unvisited_urls_make_the_diff_incomplete() -> None:
    diff = sitemap_crawl_diff(_site(), SITEMAP)
    assert diff.complete is False
    assert any("status unknown, not broken" in note for note in diff.limitations)


def test_clean_site_is_complete() -> None:
    pages = [_page("/", links=("/a",)), _page("/a", links=("/",))]
    diff = sitemap_crawl_diff(pages, [f"{SITE}/", f"{SITE}/a"])
    assert diff.complete is True
    assert diff.limitations == []
    assert all(count == 0 for count in diff.to_dict()["counts"].values())


def test_truncation_and_errors_downgrade_to_candidates() -> None:
    pages = [_page("/", links=("/a",)), _page("/a", links=("/",))]
    diff = sitemap_crawl_diff(
        pages,
        [f"{SITE}/", f"{SITE}/a"],
        crawl_truncated=True,
        sitemap_truncated=True,
        sitemap_errors=["https://example.com/sitemap.xml: http_500"],
    )
    assert diff.complete is False
    text = " ".join(diff.limitations)
    assert "candidates" in text
    assert "over-reported" in text
    assert "fetch errors (1)" in text


def test_no_sitemap_is_empty_not_clean() -> None:
    diff = sitemap_crawl_diff([_page("/"), _page("/a")], [])
    assert diff.complete is False
    assert diff.to_dict()["counts"]["missing_from_sitemap"] == 0
    assert "empty, not clean" in diff.limitations[0]


def test_lists_are_capped_but_counts_are_exact() -> None:
    pages = [_page("/")] + [_page(f"/p{i}") for i in range(5)]
    sitemap = [f"{SITE}/"] + [f"{SITE}/p{i}" for i in range(5)]
    diff = sitemap_crawl_diff(pages, sitemap, limit=2)
    payload = diff.to_dict()
    assert payload["counts"]["unlinked"] == 6
    assert len(payload["buckets"]["unlinked"]) == 2
    assert payload["truncated"] is True
    assert set(payload["buckets"]) == set(BUCKETS)


def test_output_is_deterministic() -> None:
    first = sitemap_crawl_diff(_site(), SITEMAP).to_dict()
    second = sitemap_crawl_diff(list(reversed(_site())), list(reversed(SITEMAP))).to_dict()
    assert first == second
