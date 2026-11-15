"""Offline tests for the MCP server surface (Wave 7, WP7.1-WP7.5).

No provider, network or real credential is involved: services are small fakes that return
the same envelopes the integration layer returns, or raise the same typed errors.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest

import lv24_seo.mcp.server as server_module
from lv24_seo.core.config import SeoConfig
from lv24_seo.core.envelope import fail, ok
from lv24_seo.core.errors import (
    AUTH_REQUIRED,
    CONFIG_INVALID,
    CONFIRM_REQUIRED,
    INTERNAL,
    INVALID_INPUT,
    UPSTREAM_UNAVAILABLE,
    WRITE_DISABLED,
    SeoError,
)
from lv24_seo.mcp.registry import TOOLS
from lv24_seo.mcp.server import create_server, serve
from lv24_seo.store import Store

# -- helpers ---------------------------------------------------------------------------------


def _call(server: Any, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
    return asyncio.run(server.call_tool(name, arguments or {}))


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def _tools(server: Any) -> list[Any]:
    return asyncio.run(server.list_tools())


def _resources(server: Any) -> list[Any]:
    return asyncio.run(server.list_resources())


def _read(server: Any, uri: str) -> str:
    contents = asyncio.run(server.read_resource(uri))
    return contents[0].content


def _schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", None)
    if schema is None:  # mcp 1.x names the field with a camelCase alias
        schema = getattr(tool, "inputSchema", None)
    assert isinstance(schema, dict)
    return schema


WEEKLY_PAYLOAD = ok(
    {
        "kind": "weekly",
        "property_id": "sc-domain:example.com",
        "period": {"start": "2026-11-01", "end": "2026-11-07"},
        "generated_at": "2026-11-08T06:00:00+00:00",
        "headline": "Clicks +12.5%",
        "sections": [
            {
                "key": "gsc",
                "title": "Google Search Console",
                "summary": "clicks up",
                "rows": [],
                "notes": [],
            }
        ],
        "limitations": [],
        "warnings": [],
    },
    meta={"period": {"start": "2026-11-01", "end": "2026-11-07"}},
)

PROPERTIES_ENVELOPE = ok(
    {
        "project": {"name": "Example", "base_url": "https://example.com/"},
        "providers": [
            {
                "provider": "gsc",
                "configured": True,
                "target": "sc-domain:example.com",
                "token_present": True,
            }
        ],
    },
    meta={"ready": ["gsc"]},
    warnings=["metrika: token missing"],
)


# -- fakes -----------------------------------------------------------------------------------


class ReadService:
    """Minimal read service: only ``properties`` is exercised unless a test adds more."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def properties(self) -> dict[str, Any]:
        self.calls.append("properties")
        return PROPERTIES_ENVELOPE


class GscService(ReadService):
    def __init__(self) -> None:
        super().__init__()
        self.limit: int | None = None
        self.dimensions: tuple[str, ...] | None = None
        self.windows: list[tuple[Any, Any]] = []

    def gsc_query(
        self,
        *,
        start: Any,
        end: Any,
        dimensions: tuple[str, ...] = ("query",),
        limit: int = 100,
    ) -> dict[str, Any]:
        self.limit = limit
        self.dimensions = dimensions
        self.windows.append((start, end))
        return ok({"rows": []})


class FailingService:
    def __init__(self, error: SeoError) -> None:
        self.error = error

    def properties(self) -> dict[str, Any]:
        raise self.error


class ExplodingService:
    def properties(self) -> dict[str, Any]:
        raise RuntimeError("token=super-secret")


class WeeklyService(ReadService):
    def __init__(self) -> None:
        super().__init__()
        self.send: bool | None = None

    def weekly_report(
        self, *, days: int = 7, lag_days: int = 2, send: bool = False
    ) -> dict[str, Any]:
        self.send = send
        return WEEKLY_PAYLOAD


class TriageService(ReadService):
    def yandex_indexing(self) -> dict[str, Any]:
        return ok(
            {
                "events": [
                    {"url": "https://example.com/a", "reason": "NOINDEX"},
                    {"url": "https://example.com/b", "reason": "ROBOTS_DISALLOW"},
                    {"url": "https://example.com/c", "reason": "NOINDEX"},
                ]
            },
            meta={"data_age_days": 4},
        )


class OpportunityService(ReadService):
    ROW: ClassVar[dict[str, Any]] = {
        "query": "seo",
        "page": "https://example.com/seo",
        "clicks": 2.0,
        "impressions": 1000.0,
        "ctr": 0.002,
        "position": 9.0,
    }

    def __init__(self) -> None:
        super().__init__()
        self.dimensions: list[tuple[str, ...]] = []

    def gsc_query(
        self,
        *,
        start: Any,
        end: Any,
        dimensions: tuple[str, ...] = ("query",),
        limit: int = 100,
    ) -> dict[str, Any]:
        self.dimensions.append(dimensions)
        return ok({"rows": [dict(self.ROW)]})


class WriteService(ReadService):
    def __init__(self) -> None:
        super().__init__()
        self.recrawl: tuple[str, list[str]] | None = None
        self.recrawl_calls = 0
        self.sitemap: tuple[str, str, str] | None = None
        self.sitemap_calls = 0

    def recrawl_request(self, *, host_id: str, urls: list[str]) -> dict[str, Any]:
        self.recrawl = (host_id, urls)
        self.recrawl_calls += 1
        return ok({"queued": len(urls), "quota_left": 10, "task_ids": []})

    def sitemap_submit(self, *, property: str, sitemap_url: str, action: str) -> dict[str, Any]:
        self.sitemap = (property, sitemap_url, action)
        self.sitemap_calls += 1
        return ok({"provider": "gsc", "status": action})


# -- WP7.1: server assembly ------------------------------------------------------------------


def test_package_exports_create_server() -> None:
    from lv24_seo.mcp import create_server as exported

    assert exported is create_server


def test_create_server_lists_exactly_the_registry_tools() -> None:
    server = create_server(None)
    tools = _tools(server)

    assert len(tools) == len(TOOLS) == 11
    assert [tool.name for tool in tools] == [spec.name for spec in TOOLS]
    assert len({tool.name for tool in tools}) == 11


def test_tool_metadata_and_annotations_match_registry() -> None:
    tools = {tool.name: tool for tool in _tools(create_server(ReadService()))}

    for spec in TOOLS:
        tool = tools[spec.name]
        assert tool.title == spec.title
        assert tool.description == spec.summary
        annotations = tool.annotations
        assert annotations is not None
        read_only = spec.kind == "read" and not spec.writes_local_state
        assert annotations.read_only_hint is read_only
        assert annotations.destructive_hint is (spec.kind == "write")
        assert annotations.idempotent_hint is read_only


def test_gsc_default_window_uses_the_service_timezone() -> None:
    from lv24_seo.core.timeutil import compare_period

    service = GscService()
    service.timezone = "Pacific/Kiritimati"  # type: ignore[attr-defined]

    _call(create_server(service), "gsc_search_analytics")

    expected = compare_period(7, lag_days=2, tz="Pacific/Kiritimati")[0]
    assert service.windows == [expected]


def test_input_schemas_are_generated_from_typed_parameters() -> None:
    tools = {tool.name: tool for tool in _tools(create_server(ReadService()))}

    gsc = _schema(tools["gsc_search_analytics"])
    assert {"property", "start_date", "end_date", "dimensions", "limit"} <= set(gsc["properties"])
    assert gsc["properties"]["limit"]["type"] == "integer"
    assert gsc["properties"]["limit"]["default"] == 50
    assert gsc["properties"]["dimensions"]["anyOf"][0]["type"] == "array"

    recrawl = _schema(tools["seo_recrawl_request"])
    assert recrawl["properties"]["allow_write"]["type"] == "boolean"
    assert recrawl["properties"]["allow_write"]["default"] is False
    assert recrawl["properties"]["urls"]["anyOf"][0]["type"] == "array"
    assert recrawl["properties"]["plan_hash"]["default"] == ""
    assert recrawl["properties"]["confirm_token"]["default"] == ""


# -- WP7.2: read tools -----------------------------------------------------------------------


def test_fake_service_envelope_is_returned_as_compact_json_and_structured_content() -> None:
    result = _call(create_server(ReadService()), "seo_properties")

    assert result.is_error is False
    assert _payload(result) == PROPERTIES_ENVELOPE
    assert result.structured_content == PROPERTIES_ENVELOPE
    assert result.content[0].text == json.dumps(
        PROPERTIES_ENVELOPE, ensure_ascii=False, separators=(",", ":")
    )


def test_gsc_tool_defaults_and_clamps_limit() -> None:
    default_service = GscService()
    default = _call(create_server(default_service), "gsc_search_analytics")
    assert default.is_error is False
    assert default_service.limit == 50
    assert default_service.dimensions == ("query",)

    clamped_service = GscService()
    clamped = _call(
        create_server(clamped_service),
        "gsc_search_analytics",
        {"limit": 5000, "property": "sc-domain:other.example"},
    )
    payload = _payload(clamped)
    assert clamped.is_error is False
    assert clamped_service.limit == 500
    assert "limit: clamped to 500" in payload["warnings"]
    assert "property: ignored; this service uses its configured value" in payload["warnings"]


def test_yandex_query_stats_sorts_rows_by_requested_indicator() -> None:
    class YandexService(ReadService):
        def yandex_queries(self, *, limit: int = 100) -> dict[str, Any]:
            return ok(
                {
                    "host_id": "https:example.com:443",
                    "rows": [
                        {"query": "a", "shows": 10, "clicks": 1, "position": 4.0},
                        {"query": "b", "shows": 30, "clicks": 9, "position": 2.0},
                    ],
                }
            )

    result = _call(
        create_server(YandexService()),
        "yandex_query_stats",
        {"order_by": "TOTAL_CLICKS"},
    )
    payload = _payload(result)

    assert result.is_error is False
    assert [row["query"] for row in payload["data"]["rows"]] == ["b", "a"]
    assert payload["meta"]["sorted_by"] == "clicks"


def test_crawl_tool_filters_rules_and_caps_issues() -> None:
    class CrawlService(ReadService):
        def crawl_and_store(
            self, *, start_url: str | None = None, max_urls: int | None = None
        ) -> dict[str, Any]:
            issues = [
                {"rule_id": "title-missing", "severity": "medium", "url": f"/p{i}", "detail": ""}
                for i in range(3)
            ]
            issues.append({"rule_id": "h1-missing", "severity": "high", "url": "/h1", "detail": ""})
            return ok(
                {
                    "summary": {"urls": 4, "issues": len(issues)},
                    "issues": issues,
                },
                meta={"max_urls": max_urls},
            )

    result = _call(
        create_server(CrawlService()),
        "seo_crawl_site",
        {"include_rules": ["h1-missing"], "max_urls": 10},
    )
    payload = _payload(result)

    assert result.is_error is False
    assert [issue["rule_id"] for issue in payload["data"]["issues"]] == ["h1-missing"]
    assert payload["meta"]["issues_shown"] == 1
    assert payload["meta"]["issues_filtered_by"] == ["h1-missing"]
    assert any("include_rules" in warning for warning in payload["warnings"])


def test_weekly_report_is_cached_for_the_resource_and_never_sends() -> None:
    service = WeeklyService()
    server = create_server(service)

    result = _call(server, "seo_weekly_report", {"days": 7, "lag_days": 2})

    assert result.is_error is False
    assert service.send is False
    markdown = _read(server, "seo://report/latest")
    assert "# Clicks +12.5%" in markdown
    assert "2026-11-01 - 2026-11-07" in markdown


def test_indexing_triage_builds_buckets_from_yandex_events() -> None:
    result = _call(create_server(TriageService()), "seo_indexing_triage")
    payload = _payload(result)

    assert result.is_error is False
    buckets = {bucket["reason"]: bucket for bucket in payload["data"]["buckets"]}
    assert buckets["noindex"]["count"] == 2
    assert buckets["noindex"]["examples"] == [
        "https://example.com/a",
        "https://example.com/c",
    ]
    assert buckets["blocked_by_robots"]["count"] == 1
    assert buckets["noindex"]["next_action"]
    assert payload["meta"]["data_age_days"] == 4
    assert any("google:" in item for item in payload["meta"]["limitations"])


def test_opportunities_uses_two_gsc_windows_and_real_rows() -> None:
    service = OpportunityService()
    result = _call(create_server(service), "seo_opportunities")
    payload = _payload(result)

    assert result.is_error is False
    assert len(service.dimensions) == 2
    assert service.dimensions[0] == ("query", "page")
    assert payload["meta"]["limit"] == 50
    opportunities = payload["data"]["opportunities"]
    assert opportunities
    assert {item["kind"] for item in opportunities} >= {"striking_distance", "ctr_gap"}
    assert all("expected_gain" in item for item in opportunities)
    assert any("estimate" in item for item in payload["meta"]["limitations"])


# -- errors ----------------------------------------------------------------------------------


def test_seo_error_becomes_ok_false_with_the_same_code() -> None:
    error = SeoError(
        code=UPSTREAM_UNAVAILABLE,
        message="Google Search Console is unavailable",
        next_action="retry in a minute",
        retryable=True,
    )
    result = _call(create_server(FailingService(error)), "seo_properties")
    payload = _payload(result)

    assert result.is_error is True
    assert result.structured_content == payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert payload["error"]["message"] == error.message
    assert payload["error"]["retryable"] is True


def test_unexpected_exception_becomes_internal_without_leaking_text() -> None:
    result = _call(create_server(ExplodingService()), "seo_properties")
    payload = _payload(result)

    assert result.is_error is True
    assert payload["ok"] is False
    assert payload["error"]["code"] == INTERNAL
    assert "super-secret" not in result.content[0].text


def test_missing_service_method_is_upstream_unavailable() -> None:
    result = _call(create_server(ReadService()), "gsc_search_analytics")
    payload = _payload(result)

    assert result.is_error is True
    assert payload["error"]["code"] == UPSTREAM_UNAVAILABLE


# -- WP7.3: write gating ---------------------------------------------------------------------

WRITE_SECRET = "mcp-unit-test-write-secret"  # gitleaks:allow -- synthetic fixture
RECRAWL_ARGS: dict[str, Any] = {
    "host_id": "https:example.com:443",
    "urls": ["https://example.com/a"],
}


def _dry_run(server: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    result = _call(server, name, {**arguments, "allow_write": True})
    payload = _payload(result)
    assert result.is_error is False, payload
    assert payload["meta"]["dry_run"] is True
    assert payload["data"]["status"] == "dry_run"
    assert payload["data"]["confirm_token"]
    return payload["data"]


def test_recrawl_refuses_without_allow_write() -> None:
    service = WriteService()
    result = _call(create_server(service), "seo_recrawl_request", RECRAWL_ARGS)
    payload = _payload(result)

    assert result.is_error is True
    assert payload["error"]["code"] == WRITE_DISABLED
    assert service.recrawl is None


def test_sitemap_refuses_without_allow_write() -> None:
    service = WriteService()
    result = _call(
        create_server(service),
        "seo_sitemap_submit",
        {
            "property": "sc-domain:example.com",
            "sitemap_url": "https://example.com/sitemap.xml",
        },
    )
    payload = _payload(result)

    assert result.is_error is True
    assert payload["error"]["code"] == WRITE_DISABLED
    assert service.sitemap is None


def test_write_tools_refuse_when_the_service_has_no_write_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", WRITE_SECRET)
    server = create_server(ReadService())
    recrawl = _call(
        server,
        "seo_recrawl_request",
        {**RECRAWL_ARGS, "allow_write": True},
    )
    sitemap = _call(
        server,
        "seo_sitemap_submit",
        {
            "property": "sc-domain:example.com",
            "sitemap_url": "https://example.com/sitemap.xml",
            "allow_write": True,
        },
    )

    assert _payload(recrawl)["error"]["code"] == WRITE_DISABLED
    assert _payload(sitemap)["error"]["code"] == WRITE_DISABLED


def test_write_dry_run_returns_a_plan_and_token_without_calling_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", WRITE_SECRET)
    service = WriteService()

    data = _dry_run(create_server(service), "seo_recrawl_request", RECRAWL_ARGS)

    assert data["plan"]["action"] == "recrawl"
    assert data["plan"]["target"] == "https:example.com:443"
    assert data["plan"]["payload"]["urls"] == ["https://example.com/a"]
    assert data["plan_hash"]
    assert data["ttl_seconds"] == 300
    assert service.recrawl is None
    assert service.recrawl_calls == 0


def test_write_apply_calls_the_provider_once_and_rejects_a_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", WRITE_SECRET)
    service = WriteService()
    store = Store(tmp_path / "seo.db")
    service._store = store  # type: ignore[attr-defined]
    try:
        server = create_server(service)
        dry = _dry_run(server, "seo_recrawl_request", RECRAWL_ARGS)
        arguments = {
            **RECRAWL_ARGS,
            "allow_write": True,
            "plan_hash": dry["plan_hash"],
            "confirm_token": dry["confirm_token"],
        }

        first = _call(server, "seo_recrawl_request", arguments)
        second = _call(server, "seo_recrawl_request", arguments)
    finally:
        store.close()

    first_payload = _payload(first)
    second_payload = _payload(second)
    assert first.is_error is False
    assert first_payload["data"]["queued"] == 1
    assert first_payload["meta"]["write_status"] == "applied"
    assert second.is_error is False
    assert second_payload["data"]["status"] == "skipped"
    assert second_payload["data"]["reason"] == "duplicate"
    assert service.recrawl_calls == 1, "a replayed token must not reach the provider again"
    assert service.recrawl == ("https:example.com:443", ["https://example.com/a"])


def test_write_apply_needs_the_write_secret() -> None:
    service = WriteService()
    result = _call(
        create_server(service),
        "seo_recrawl_request",
        {**RECRAWL_ARGS, "allow_write": True},
    )
    payload = _payload(result)

    assert result.is_error is True
    assert payload["error"]["code"] == WRITE_DISABLED
    assert "LV24_SEO_WRITE_SECRET" in payload["error"]["message"]
    assert service.recrawl_calls == 0


def test_write_apply_rejects_a_changed_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", WRITE_SECRET)
    service = WriteService()
    server = create_server(service)
    dry = _dry_run(server, "seo_recrawl_request", RECRAWL_ARGS)

    result = _call(
        server,
        "seo_recrawl_request",
        {
            "host_id": "https:example.com:443",
            "urls": ["https://example.com/other"],
            "allow_write": True,
            "plan_hash": dry["plan_hash"],
            "confirm_token": dry["confirm_token"],
        },
    )

    assert _payload(result)["error"]["code"] == CONFIRM_REQUIRED
    assert service.recrawl_calls == 0


def test_write_apply_rejects_a_mismatched_plan_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", WRITE_SECRET)
    service = WriteService()
    server = create_server(service)
    dry = _dry_run(server, "seo_recrawl_request", RECRAWL_ARGS)

    result = _call(
        server,
        "seo_recrawl_request",
        {
            **RECRAWL_ARGS,
            "allow_write": True,
            "plan_hash": "0" * 64,
            "confirm_token": dry["confirm_token"],
        },
    )

    assert _payload(result)["error"]["code"] == CONFIRM_REQUIRED
    assert service.recrawl_calls == 0


def test_sitemap_write_uses_the_two_phase_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", WRITE_SECRET)
    service = WriteService()
    store = Store(tmp_path / "seo.db")
    service._store = store  # type: ignore[attr-defined]
    arguments = {
        "property": "sc-domain:example.com",
        "sitemap_url": "https://example.com/sitemap.xml",
        "action": "submit",
    }
    try:
        server = create_server(service)
        dry = _dry_run(server, "seo_sitemap_submit", arguments)
        result = _call(
            server,
            "seo_sitemap_submit",
            {
                **arguments,
                "allow_write": True,
                "plan_hash": dry["plan_hash"],
                "confirm_token": dry["confirm_token"],
            },
        )
    finally:
        store.close()

    payload = _payload(result)
    assert result.is_error is False
    assert payload["data"]["provider"] == "gsc"
    assert payload["meta"]["action"] == "sitemap_submit"
    assert service.sitemap == (
        "sc-domain:example.com",
        "https://example.com/sitemap.xml",
        "submit",
    )


def test_a_failed_provider_write_releases_the_ledger_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingWrite(WriteService):
        def recrawl_request(self, *, host_id: str, urls: list[str]) -> dict[str, Any]:
            self.recrawl_calls += 1
            return fail(
                SeoError(
                    code=UPSTREAM_UNAVAILABLE,
                    message="provider is down",
                    retryable=True,
                )
            )

    monkeypatch.setenv("LV24_SEO_WRITE_SECRET", WRITE_SECRET)
    service = FailingWrite()
    store = Store(tmp_path / "seo.db")
    service._store = store  # type: ignore[attr-defined]
    try:
        server = create_server(service)
        dry = _dry_run(server, "seo_recrawl_request", RECRAWL_ARGS)
        arguments = {
            **RECRAWL_ARGS,
            "allow_write": True,
            "plan_hash": dry["plan_hash"],
            "confirm_token": dry["confirm_token"],
        }
        first = _call(server, "seo_recrawl_request", arguments)
        second = _call(server, "seo_recrawl_request", arguments)
    finally:
        store.close()

    assert _payload(first)["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert _payload(second)["error"]["code"] == UPSTREAM_UNAVAILABLE
    assert service.recrawl_calls == 2, "a failed write must release its claim for a retry"


def test_write_tools_validate_inputs_after_the_gate() -> None:
    service = WriteService()
    result = _call(
        create_server(service),
        "seo_sitemap_submit",
        {
            "property": "sc-domain:example.com",
            "sitemap_url": "file:///etc/passwd",
            "allow_write": True,
        },
    )
    payload = _payload(result)

    assert result.is_error is True
    assert payload["error"]["code"] == INVALID_INPUT
    assert service.sitemap is None


# -- WP7.4: resources and prompts ------------------------------------------------------------


def test_resources_are_listed_and_docs_expose_the_registry() -> None:
    server = create_server(ReadService())
    resources = {str(resource.uri) for resource in _resources(server)}
    assert resources == {
        "seo://config",
        "seo://report/latest",
        "seo://docs/tools",
    }

    docs = json.loads(_read(server, "seo://docs/tools"))
    assert docs["schema_version"] == 1
    assert [tool["name"] for tool in docs["tools"]] == [spec.name for spec in TOOLS]

    note = _read(server, "seo://report/latest")
    assert "No weekly report" in note


def test_config_resource_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "load_config", lambda: SeoConfig())
    server = create_server(ReadService())

    payload = json.loads(_read(server, "seo://config"))

    assert payload["ok"] is True
    assert payload["data"]["project"]["name"] == ""
    assert set(payload["data"]["providers"]) >= {"gsc", "yandex_webmaster", "bing"}


def test_prompts_are_listed_and_reference_the_right_tools() -> None:
    server = create_server(ReadService())
    prompts = {prompt.name: prompt for prompt in asyncio.run(server.list_prompts())}
    assert set(prompts) == {
        "weekly_seo_review",
        "indexing_triage",
        "content_opportunities",
    }
    assert prompts["weekly_seo_review"].arguments is not None

    review = asyncio.run(server.get_prompt("weekly_seo_review", {"property": "example.com"}))
    review_text = review.messages[0].content.text
    assert "seo_weekly_report" in review_text
    assert "example.com" in review_text

    triage_text = asyncio.run(server.get_prompt("indexing_triage", {})).messages[0].content.text
    assert "seo_indexing_triage" in triage_text

    opportunity_text = (
        asyncio.run(server.get_prompt("content_opportunities", {})).messages[0].content.text
    )
    assert "seo_opportunities" in opportunity_text


# -- transports and import safety ------------------------------------------------------------


def test_importing_the_server_module_does_not_open_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("importing the MCP module must not touch a socket")

    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket.socket, "listen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    importlib.reload(server_module)


def test_serve_runs_stdio_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class DummyServer:
        async def run_stdio_async(self) -> None:
            calls.append("stdio")

    monkeypatch.setattr(server_module, "create_server", lambda: DummyServer())

    asyncio.run(serve())

    assert calls == ["stdio"]


def test_serve_rejects_unknown_transport_without_opening_a_socket() -> None:
    with pytest.raises(SeoError) as excinfo:
        asyncio.run(serve("sse"))

    assert excinfo.value.code == INVALID_INPUT
    assert "streamable HTTP" in (excinfo.value.next_action or "")


def test_serve_reports_a_missing_streamable_http_api(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoHttpServer:
        pass

    monkeypatch.setattr(server_module, "create_server", lambda: NoHttpServer())

    with pytest.raises(SeoError) as excinfo:
        asyncio.run(serve("http", port=8765))

    assert excinfo.value.code == CONFIG_INVALID
    assert "streamable HTTP" in excinfo.value.message


def test_serve_validates_the_port_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class DummyServer:
        async def run_streamable_http_async(self, *, port: int = 8000) -> None:
            calls.append(port)

    monkeypatch.setattr(server_module, "create_server", lambda: DummyServer())

    with pytest.raises(SeoError) as excinfo:
        asyncio.run(serve("http", port=0))

    assert excinfo.value.code == INVALID_INPUT
    assert calls == []


# -- ENG-15: one exit-code mapping -----------------------------------------------------------


def test_mcp_serve_cli_uses_the_shared_exit_code_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from lv24_seo.cli.main import app

    def fake_serve(*args: Any, **kwargs: Any) -> None:
        raise SeoError(code=AUTH_REQUIRED, message="Google Search Console token is missing")

    monkeypatch.setattr("lv24_seo.mcp.serve", fake_serve)

    result = CliRunner().invoke(app, ["mcp", "serve"])

    assert result.exit_code == 3, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == AUTH_REQUIRED


def test_mcp_server_main_uses_the_shared_exit_code_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_serve(*args: Any, **kwargs: Any) -> None:
        raise SeoError(code=AUTH_REQUIRED, message="Google Search Console token is missing")

    monkeypatch.setattr(server_module, "serve", fake_serve)

    with pytest.raises(SystemExit) as excinfo:
        server_module.main([])

    assert excinfo.value.code == 3
