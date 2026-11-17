"""Canonical MCP tool registry (single source of truth).

The registry drives MCP JSON schemas, CLI help, `schemas/tools.json` and the docs tables.
Wave W5 fills the tool implementations; this module only declares the shape now.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ToolKind = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    title: str
    summary: str
    kind: ToolKind = "read"
    writes_local_state: bool = False
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
        writes_local_state=True,
        providers=("yandex_webmaster",),
        returns="{history:[...], problems:[...], data_age_days}",
        limitations=("per-URL sitemap errors are not exposed",),
    ),
    ToolSpec(
        name="seo_crawl_site",
        title="Crawl site",
        summary="Crawl a site (robots-respecting), return crawl summary and rule violations.",
        writes_local_state=True,
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
        writes_local_state=True,
        providers=("gsc", "yandex_webmaster", "yandex_metrika", "crux", "crawl"),
        inputs={
            "days": "current period length in days (1..90, default 7)",
            "lag_days": "provider data lag in days (0..14, default 2)",
        },
        returns="{period, headline, deltas, issues, opportunities, limitations}",
    ),
    ToolSpec(
        name="seo_report_diff",
        title="Report diff",
        summary="Diff two stored weekly report snapshots (metrics, issues, opportunity churn).",
        providers=("store",),
        inputs={
            "base": "older period START..END (default: second newest snapshot)",
            "head": "newer period START..END (default: newest snapshot)",
        },
        returns=(
            "{headline:[{metric,base,head,abs_delta,pct_delta}], unknown_metrics, issues, "
            "opportunities:{added,resolved,unchanged}, limitations_changed, comparable, markdown}"
        ),
        limitations=(
            "needs two stored weekly snapshots (`lv24-seo init` + two report runs)",
            "metrics present in one snapshot only are unknown, not zero",
        ),
    ),
    ToolSpec(
        name="seo_indexing_triage",
        title="Indexing triage",
        summary="Group not-indexed URLs by reason and propose the cheapest fix.",
        providers=("gsc", "yandex_webmaster", "crawl"),
        inputs={"max_examples": "example URLs per bucket (0..20, default 5)"},
        returns="{buckets:[{reason,count,examples,next_action}]}",
    ),
    ToolSpec(
        name="seo_log_analysis",
        title="Analyze access log",
        summary=(
            "Parse one local nginx JSON access log and report bot crawl rollups and wasted share."
        ),
        providers=("logs",),
        inputs={
            "file": "absolute path to one nginx JSON access log (max 100 MiB)",
            "bot": "optional bot filter: googlebot, yandexbot, bingbot or other",
        },
        returns=(
            "{total_lines, parsed_lines, skipped_lines, "
            "bot_daily:[{date,bot,status,hits,bytes,p95_ms}], "
            "crawl_waste:{hits,bytes,share,total_hits,total_bytes,by_bot}}"
        ),
        limitations=(
            "user-agent classification only; not a verified-crawler check",
            "nginx JSON logs only; other formats are skipped",
            "one local file per call; no rotation, glob or remote fetch",
        ),
    ),
    ToolSpec(
        name="seo_opportunities",
        title="Opportunities",
        summary="Striking-distance queries, CTR gaps, decayed pages, cannibalisation candidates.",
        providers=("gsc", "yandex_webmaster"),
        inputs={
            "days": "current period length in days (1..90, default 28)",
            "limit": "1..500 (default 50)",
            "min_impressions": "minimum impressions for a candidate (default 100)",
        },
        returns="{opportunities:[{kind,url,query,evidence,expected_gain}]}",
    ),
    ToolSpec(
        name="seo_recrawl_request",
        title="Request recrawl (write)",
        summary="Queue URLs for Yandex recrawl within the daily quota.",
        kind="write",
        providers=("yandex_webmaster",),
        inputs={
            "host_id": "https:example.com:443 (default: the configured host)",
            "urls": "list[url]",
            "allow_write": "explicit opt-in (default false)",
            "plan_hash": "plan_hash returned by the dry run (apply only)",
            "confirm_token": "confirm_token returned by the dry run (apply only)",
        },
        returns="{queued, quota_left, task_ids}",
        limitations=(
            "requires allow_write=true",
            "apply needs the confirm_token from a dry run; a replay is skipped",
            "hard daily quota",
        ),
    ),
    ToolSpec(
        name="seo_sitemap_submit",
        title="Submit sitemap (write)",
        summary="Submit a sitemap to GSC or Bing; Bing defaults to <base_url>/sitemap.xml.",
        kind="write",
        providers=("gsc", "bing"),
        inputs={
            "provider": "gsc|bing (default gsc)",
            "property": "GSC property id; Bing site URL (default: providers.bing.site)",
            "sitemap_url": "absolute URL; Bing defaults to <project.base_url>/sitemap.xml",
            "action": "submit|delete",
            "allow_write": "explicit opt-in (default false)",
            "plan_hash": "plan_hash returned by the dry run (apply only)",
            "confirm_token": "confirm_token returned by the dry run (apply only)",
        },
        returns="{provider, status, site_url, sitemap_url}",
        limitations=(
            "requires allow_write=true",
            "apply needs the confirm_token from a dry run; a replay is skipped",
            "GSC submit and Bing delete stay WRITE_DISABLED until the provider adapter lands",
        ),
    ),
)


def tool_annotations(spec: ToolSpec) -> dict[str, bool]:
    """MCP hints derived from the registry contract.

    A tool that writes local state (crawl snapshots, weekly snapshots) is not read-only even
    when it never touches a provider. It is not destructive either: local state can be
    regenerated, so ``destructiveHint`` stays ``False``.
    """
    read_only = spec.kind == "read" and not spec.writes_local_state
    return {
        "readOnlyHint": read_only,
        "destructiveHint": spec.kind == "write",
        "idempotentHint": read_only,
    }


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
                "writes_local_state": t.writes_local_state,
                "providers": list(t.providers),
                "inputs": t.inputs,
                "returns": t.returns,
                "limitations": list(t.limitations),
                "annotations": tool_annotations(t),
            }
            for t in TOOLS
        ],
    }


def render_tools_json() -> str:
    """Canonical ``schemas/tools.json`` text (LF, trailing newline)."""
    return json.dumps(tools_as_json(), ensure_ascii=False, indent=2) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Print, write or verify the generated tool schema.

    ``--write`` is the documented regeneration command; ``--check`` exits non-zero when the
    checked-in file no longer matches the registry (useful in CI and before a release).
    """
    parser = argparse.ArgumentParser(description="Render or verify schemas/tools.json.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--write",
        action="store_true",
        help="Write the generated schema (default: schemas/tools.json).",
    )
    action.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when the schema on disk differs from the registry.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Destination file (default: schemas/tools.json under the current directory).",
    )
    args = parser.parse_args(argv)
    destination = args.out if args.out is not None else Path.cwd() / "schemas" / "tools.json"
    rendered = render_tools_json()

    if args.check:
        try:
            existing = destination.read_text(encoding="utf-8")
        except OSError:
            print(f"{destination}: missing; run python -m lv24_seo.mcp.registry --write")
            return 1
        if existing != rendered:
            print(
                f"{destination}: drift detected; run python -m lv24_seo.mcp.registry --write",
                file=sys.stderr,
            )
            return 1
        print(f"{destination}: up to date")
        return 0

    if args.write:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {len(TOOLS)} tools to {destination}")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover - developer utility
    raise SystemExit(main())
