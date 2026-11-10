"""Canonical MCP tool registry (single source of truth).

The registry drives MCP JSON schemas, CLI help, `schemas/tools.json` and the docs tables.
Wave W5 fills the tool implementations; this module only declares the shape now.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

ToolKind = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    title: str
    summary: str
    kind: ToolKind = "read"
    providers: tuple[str, ...] = field(default_factory=tuple)
    inputs: dict[str, str] = field(default_factory=dict)
    returns: str = ""
    limitations: tuple[str, ...] = field(default_factory=tuple)


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="seo_properties",
        title="List properties",
        summary="List every site/property visible to the configured tokens.",
        providers=("gsc", "yandex_webmaster", "bing"),
        returns="array of {provider, property_id, display_name, verified}",
    ),
    ToolSpec(
        name="gsc_search_analytics",
        title="Google search analytics",
        summary="Query GSC clicks/impressions/CTR/position by query, page, country or device.",
        providers=("gsc",),
        inputs={
            "property": "GSC property id, e.g. sc-domain:example.com",
            "start_date": "ISO date",
            "end_date": "ISO date",
            "dimensions": "list[query|page|country|device|date]",
            "limit": "1..500 (default 50)",
        },
        returns="{rows:[{keys,clicks,impressions,ctr,position}], total, truncated, data_age_days}",
        limitations=("50k rows/day/site/type", "data lag 2-3 days", "no query x page join"),
    ),
    ToolSpec(
        name="yandex_query_stats",
        title="Yandex queries",
        summary="Top Yandex queries for a host with shows, clicks, position.",
        providers=("yandex_webmaster",),
        inputs={
            "host_id": "https:example.com:443",
            "limit": "1..500",
            "order_by": "TOTAL_SHOWS|TOTAL_CLICKS",
        },
        returns="{rows:[{query,shows,clicks,position}], total, truncated, data_age_days}",
        limitations=("top-3000 queries only", "no query x page join", "~4 day lag"),
    ),
    ToolSpec(
        name="yandex_indexing_state",
        title="Yandex indexing state",
        summary="Indexing history, in-search page counts and diagnostics for a host.",
        providers=("yandex_webmaster",),
        returns="{history:[...], problems:[...], data_age_days}",
        limitations=("per-URL sitemap errors are not exposed",),
    ),
    ToolSpec(
        name="seo_crawl_site",
        title="Crawl site",
        summary="Crawl a site (robots-respecting), return crawl summary and rule violations.",
        providers=("crawl",),
        inputs={
            "start_url": "https://example.com/",
            "max_urls": "1..5000",
            "include_rules": "list[rule_id]",
        },
        returns="{summary:{urls,fetched,errors}, issues:[{id,severity,url,detail}]}",
    ),
    ToolSpec(
        name="seo_weekly_report",
        title="Weekly report",
        summary="Build the weekly SEO digest (deltas vs last snapshot) for one property.",
        providers=("gsc", "yandex_webmaster", "yandex_metrika", "crux", "crawl"),
        returns="{period, headline, deltas, issues, opportunities, limitations}",
    ),
    ToolSpec(
        name="seo_indexing_triage",
        title="Indexing triage",
        summary="Group not-indexed URLs by reason and propose the cheapest fix.",
        providers=("gsc", "yandex_webmaster", "crawl"),
        returns="{buckets:[{reason,count,examples,next_action}]}",
    ),
    ToolSpec(
        name="seo_opportunities",
        title="Opportunities",
        summary="Striking-distance queries, CTR gaps, decayed pages, cannibalisation candidates.",
        providers=("gsc", "yandex_webmaster"),
        returns="{opportunities:[{kind,url,query,evidence,expected_gain}]}",
    ),
    ToolSpec(
        name="seo_recrawl_request",
        title="Request recrawl (write)",
        summary="Queue URLs for Yandex recrawl within the daily quota.",
        kind="write",
        providers=("yandex_webmaster",),
        inputs={"host_id": "https:example.com:443", "urls": "list[url]"},
        returns="{queued, quota_left, task_ids}",
        limitations=("requires --allow-write", "hard daily quota"),
    ),
    ToolSpec(
        name="seo_sitemap_submit",
        title="Submit sitemap (write)",
        summary="Submit or delete a sitemap in GSC/Bing.",
        kind="write",
        providers=("gsc", "bing"),
        inputs={
            "property": "property id",
            "sitemap_url": "absolute URL",
            "action": "submit|delete",
        },
        returns="{ok, provider, status}",
        limitations=("requires --allow-write",),
    ),
)


def tools_as_json() -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_from": "lv24_seo.mcp.registry",
        "tools": [
            {
                "name": t.name,
                "title": t.title,
                "description": t.summary,
                "kind": t.kind,
                "providers": list(t.providers),
                "inputs": t.inputs,
                "returns": t.returns,
                "limitations": list(t.limitations),
                "annotations": {
                    "readOnlyHint": t.kind == "read",
                    "destructiveHint": t.kind == "write",
                    "idempotentHint": t.kind == "read",
                },
            }
            for t in TOOLS
        ],
    }


if __name__ == "__main__":  # pragma: no cover - developer utility
    print(json.dumps(tools_as_json(), indent=2, ensure_ascii=False))
