"""Real SDK threading regression: persisted MCP tools stay on the event-loop thread.

MCP 2.x runs synchronous handlers in ``anyio`` worker threads
(``FuncMetadata.call_fn`` → ``anyio.to_thread.run_sync``). The project's
``Store`` owns a ``sqlite3`` connection with ``check_same_thread=True`` (the
default), so a sync tool touching SQLite from a worker thread raises
``sqlite3.ProgrammingError``. The server registers an ``async`` adapter
(``_owner_loop_tool``) that keeps the synchronous service on the owning loop:
the core runs serially there and blocks the loop while it runs, so concurrent
``tools/call`` requests are serialised instead of racing in SQLite.

This test uses the real ``create_server``, a real file-backed ``Store`` and the
real SDK ``call_tool`` path (not a stubbed service method). Providers and the
network are faked; persistence is real.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from lv24_seo.core.config import (
    CrawlConfig,
    GscConfig,
    ProjectConfig,
    ProvidersConfig,
    SeoConfig,
    StorageConfig,
)
from lv24_seo.core.secrets import SecretStore
from lv24_seo.core.timeutil import utc_now
from lv24_seo.mcp import server as server_module
from lv24_seo.mcp.registry import TOOLS
from lv24_seo.mcp.server import _owner_loop_tool, close_workspaces, create_server
from lv24_seo.providers.base import ProviderResult
from lv24_seo.services import SeoService
from lv24_seo.store import Store

PROPERTY = "sc-domain:example.com"


def _config(data_dir: Path) -> SeoConfig:
    return SeoConfig(
        project=ProjectConfig(
            name="Example",
            base_url="https://example.com/",
            default_property=PROPERTY,
        ),
        providers=ProvidersConfig(gsc=GscConfig(property=PROPERTY)),
        crawl=CrawlConfig(max_urls=10),
        storage=StorageConfig(data_dir=data_dir),
    )


class _FakeGsc:
    def __init__(self) -> None:
        self.calls = 0

    def search_analytics(self, **kwargs: Any) -> ProviderResult[dict[str, Any]]:
        self.calls += 1
        return ProviderResult(
            source="gsc",
            property_id=PROPERTY,
            fetched_at=utc_now(),
            data_age_days=1,
            rows=[
                {
                    "query": "seo",
                    "page": "https://example.com/seo",
                    "clicks": 7.0,
                    "impressions": 70.0,
                    "ctr": 0.1,
                    "position": 2.0,
                }
            ],
            total=1,
        )


def _payload(result: Any) -> dict[str, Any]:
    import json

    return json.loads(result.content[0].text)


def test_tools_are_async_and_preserve_signatures() -> None:
    from lv24_seo.mcp.tools import TOOL_BUILDERS, ToolState

    state = ToolState(service=SeoService(_config(Path("data")), secrets=SecretStore(env={})))
    for spec in TOOLS:
        original = TOOL_BUILDERS[spec.name](state)
        wrapped = _owner_loop_tool(original)
        assert inspect.iscoroutinefunction(wrapped), spec.name
        assert inspect.signature(wrapped, eval_str=True) == inspect.signature(
            original, eval_str=True
        )
        import typing

        assert typing.get_type_hints(wrapped, include_extras=True) == typing.get_type_hints(
            original, include_extras=True
        )


def test_schemas_are_stable_across_calls(tmp_path: Path) -> None:
    store = Store(tmp_path / "seo.db")
    try:
        service = SeoService(
            _config(tmp_path),
            store=store,
            secrets=SecretStore(env={"LV24_SEO_GSC_TOKEN": "token"}),
            clients={"gsc": _FakeGsc()},
        )
        server = create_server(service)
        first = {t.name: t.input_schema for t in asyncio.run(server.list_tools())}
        asyncio.run(server.call_tool("seo_properties", {}))
        second = {t.name: t.input_schema for t in asyncio.run(server.list_tools())}
        assert first == second
        assert set(first) == {spec.name for spec in TOOLS}
    finally:
        store.close()


def test_persisted_tool_call_runs_on_owner_loop_and_repeats(tmp_path: Path) -> None:
    store = Store(tmp_path / "seo.db")
    try:
        fake = _FakeGsc()
        service = SeoService(
            _config(tmp_path),
            store=store,
            secrets=SecretStore(env={"LV24_SEO_GSC_TOKEN": "token"}),
            clients={"gsc": fake},
        )
        server = create_server(service)

        first = asyncio.run(server.call_tool("gsc_search_analytics", {"limit": 10}))
        assert first.is_error is False
        assert _payload(first)["ok"] is True

        windows = store.get_metric_windows(PROPERTY, source="gsc")
        assert windows, "real SDK call must persist through the owner-loop adapter"
        assert sum(row["value"] for row in windows if row["metric"] == "clicks") == 7.0

        second = asyncio.run(server.call_tool("gsc_search_analytics", {"limit": 10}))
        assert second.is_error is False
        assert fake.calls == 2
        assert store.get_metric_windows(PROPERTY, source="gsc")
    finally:
        store.close()
    close_workspaces()


def test_close_workspaces_is_idempotent() -> None:
    close_workspaces()
    close_workspaces()
    assert server_module._OPEN_WORKSPACES == []
