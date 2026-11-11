"""MCP server assembly and transports (Wave 7, WP7.1 / WP7.4).

The server surface is deliberately small: eleven tools from
:data:`lv24_seo.mcp.registry.TOOLS`, three resources and three prompts. Tool
implementations live in :mod:`lv24_seo.mcp.tools`; this module only registers them,
adds resources/prompts and runs a transport.

SDK choice
----------
``mcp`` 1.x ships the high-level ``FastMCP`` class. In ``mcp`` 2.x it was renamed to
``mcp.server.mcpserver.MCPServer`` and importing ``mcp.server.fastmcp`` raises a
migration error. We therefore resolve, in order, ``FastMCP`` then ``MCPServer``
(the same high-level API) and use whichever this installation provides. A low-level
``mcp.server.Server`` fallback is intentionally **not** implemented: both supported
SDK majors ship a high-level server, so a hand-written low-level shim would duplicate
the SDK, add untested protocol code and change the return type of
:func:`create_server`. If neither class imports, :func:`create_server` raises
``CONFIG_INVALID`` with an install hint instead of failing with an opaque
``ImportError``.

Importing this module never opens a socket. Only :func:`serve` (or ``python -m
lv24_seo.mcp.server``) starts a transport.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import sys
from collections.abc import Callable, Sequence
from functools import wraps
from typing import Any, Protocol, cast, get_type_hints

from mcp.types import ToolAnnotations

from lv24_seo.core.config import SeoConfig, load_config, redacted_dump
from lv24_seo.core.envelope import fail, ok
from lv24_seo.core.errors import (
    CONFIG_INVALID,
    INTERNAL,
    INVALID_INPUT,
    SeoError,
    exit_code_for,
)
from lv24_seo.mcp.registry import TOOLS, ToolSpec, tools_as_json
from lv24_seo.mcp.tools import TOOL_BUILDERS, ToolState, compact_json
from lv24_seo.services import SeoService

__all__ = ["close_workspaces", "create_server", "serve"]

logger = logging.getLogger(__name__)

#: Workspaces opened by :func:`create_server` (shared CLI/MCP history); closed by
#: :func:`serve` on shutdown or explicitly via :func:`close_workspaces`.
_OPEN_WORKSPACES: list[Any] = []

SERVER_NAME = "lv24-seo"
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "streamable-http"
_HTTP_ALIASES = frozenset({"http", TRANSPORT_HTTP, "streamable_http"})
_INSTRUCTIONS = (
    "lv24-seo is a free-first SEO control plane. Start with `seo_weekly_report` for a broad "
    "review, then use the focused read tools; `seo_report_diff` compares two stored weekly "
    "snapshots without provider calls. Every response is a JSON envelope: check `ok`, "
    "`meta.truncated`, `meta.data_age_days` and `warnings` before drawing conclusions. "
    "The two write tools are disabled unless the caller explicitly passes `allow_write=true`. "
    "CLI runs and MCP sessions share one SQLite history when the workspace is initialised "
    "(`lv24-seo init`); without it, snapshots are not saved."
)
_MAX_PORT = 65_535


class McpServer(Protocol):
    """The slice of the SDK high-level server this module registers against.

    ``FastMCP`` (mcp 1.x) and ``MCPServer`` (mcp 2.x) both satisfy it; naming the
    contract keeps :func:`create_server` typed without importing either class for type
    checking and without pretending only one SDK major exists.
    """

    def tool(
        self,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

    def resource(
        self,
        uri: str,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        mime_type: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

    def prompt(
        self,
        *,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


def _resolve_server_class() -> tuple[Any, str]:
    """Return the SDK's high-level server class and a human-readable flavour name."""
    for module_name, attribute, flavour in (
        ("mcp.server.fastmcp", "FastMCP", "FastMCP"),
        ("mcp.server.mcpserver", "MCPServer", "MCPServer"),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # mcp 2.x makes mcp.server.fastmcp raise a migration error
            logger.debug("MCP SDK candidate %s unavailable: %s", module_name, type(exc).__name__)
            continue
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate, flavour
    return None, "unavailable"


_SERVER_CLASS, _SERVER_FLAVOR = _resolve_server_class()


def _annotations(spec: ToolSpec) -> ToolAnnotations:
    """Map the registry ``kind`` onto MCP tool hints."""
    return ToolAnnotations(
        read_only_hint=spec.kind == "read",
        destructive_hint=spec.kind == "write",
        idempotent_hint=spec.kind == "read",
    )


def _register_tools(server: McpServer, state: ToolState) -> None:
    specs = {spec.name: spec for spec in TOOLS}
    missing = sorted(set(specs) - set(TOOL_BUILDERS))
    unknown = sorted(set(TOOL_BUILDERS) - set(specs))
    if missing or unknown:
        raise SeoError(
            code=CONFIG_INVALID,
            message=f"MCP tool registry mismatch (missing={missing}, unknown={unknown})",
            next_action="align lv24_seo.mcp.tools.TOOL_BUILDERS with lv24_seo.mcp.registry.TOOLS",
        )

    for spec in TOOLS:
        tool = TOOL_BUILDERS[spec.name](state)
        server.tool(
            name=spec.name,
            title=spec.title,
            description=spec.summary,
            annotations=_annotations(spec),
        )(_owner_loop_tool(tool))


def _owner_loop_tool(tool: Callable[..., Any]) -> Callable[..., Any]:
    """Keep the synchronous service and SQLite connection on their owning loop.

    MCP 2 dispatches synchronous handlers in worker threads (see
    ``FuncMetadata.call_fn`` → ``anyio.to_thread.run_sync``). A ``sqlite3``
    connection with ``check_same_thread=True`` (the project default) cannot be
    touched from those workers, and ``check_same_thread=False`` would only trade
    the thread error for concurrent transactions on one connection. Registering
    an ``async`` adapter keeps every tool body on the event-loop thread: the
    synchronous core runs serially there and blocks the loop while it runs, so
    concurrent ``tools/call`` requests are serialised instead of racing in
    SQLite. Resolve annotations before wrapping so SDK schema generation stays
    exact (``include_extras=True`` preserves ``Annotated`` metadata).
    """

    @wraps(tool)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        return tool(*args, **kwargs)

    try:
        hints = get_type_hints(tool, include_extras=True)
    except Exception:
        hints = dict(getattr(tool, "__annotations__", {}))
    invoke.__annotations__ = hints
    with contextlib.suppress(TypeError, ValueError):
        invoke.__signature__ = inspect.signature(tool, eval_str=True)  # type: ignore[attr-defined]
    return invoke


def _register_resources(
    server: McpServer,
    state: ToolState,
    config: SeoConfig | None,
) -> None:
    def read_config() -> str:
        try:
            effective = config if config is not None else load_config()
        except SeoError as exc:
            return compact_json(fail(exc))
        except Exception:
            logger.warning("unexpected failure while reading seo://config")
            return compact_json(
                fail(
                    SeoError(
                        code=INTERNAL,
                        message="could not read the effective configuration",
                        next_action="run `lv24-seo doctor`",
                    )
                )
            )
        return compact_json(
            ok(redacted_dump(effective), meta={"schema_version": 1, "source": "seo://config"})
        )

    def read_report() -> str:
        return state.report.read()

    def read_tools() -> str:
        return json.dumps(tools_as_json(), ensure_ascii=False, indent=2)

    server.resource(
        "seo://config",
        name="config",
        title="Effective config",
        description="Effective lv24-seo configuration with secret values redacted.",
        mime_type="application/json",
    )(read_config)
    server.resource(
        "seo://report/latest",
        name="report_latest",
        title="Latest weekly report",
        description=(
            "The last successful weekly report generated by this server session, rendered as "
            "Markdown. A note is returned before the first report."
        ),
        mime_type="text/markdown",
    )(read_report)
    server.resource(
        "seo://docs/tools",
        name="docs_tools",
        title="Tool reference",
        description="Generated JSON reference for the eleven MCP tools (registry source of truth).",
        mime_type="application/json",
    )(read_tools)


def _target(property_id: str) -> str:
    return property_id.strip() or "the configured property"


def _register_prompts(server: McpServer) -> None:
    def weekly_seo_review(property: str = "") -> str:
        return (
            f"Run a weekly SEO review for {_target(property)}.\n\n"
            "1. Call `seo_weekly_report` with days=7 and lag_days=2.\n"
            "2. Call `seo_indexing_triage` and `seo_opportunities` for the same property.\n"
            "3. Summarise the headline deltas (clicks, impressions, CTR, position), the top "
            "five actions ranked by expected impact, and every data limitation that affects "
            "the conclusion.\n\n"
            "Use only numbers returned by the tools. If a source is missing, name the "
            "unavailable provider instead of estimating. Keep the answer under 400 words."
        )

    def indexing_triage(property: str = "") -> str:
        return (
            f"Build a fix plan from `seo_indexing_triage` for {_target(property)}.\n\n"
            "Call the tool, then for every non-empty bucket report: the reason, the count, "
            "two or three example URLs, the proposed `next_action`, and how the fix will be "
            "verified. Order buckets by count. Do not invent URLs or counts; if the tool "
            "returns no buckets, say which source is missing and what to run next."
        )

    def content_opportunities(property: str = "") -> str:
        target = _target(property)
        return (
            f"Turn `seo_opportunities` into a prioritised content backlog for {target}.\n\n"
            "Call the tool with days=28 and min_impressions=100. For each candidate report "
            "the kind, query, URL, evidence and `expected_gain`; label `expected_gain` as a "
            "prioritisation estimate, never a traffic promise. Group candidates by "
            "striking_distance, ctr_gap and decay, then pick the top five and say what to "
            "change on the page."
        )

    server.prompt(
        name="weekly_seo_review",
        title="Weekly SEO review",
        description="Full report, indexing triage and opportunities turned into five actions.",
    )(weekly_seo_review)
    server.prompt(
        name="indexing_triage",
        title="Indexing triage",
        description="Turn indexing buckets into a verification-first fix plan.",
    )(indexing_triage)
    server.prompt(
        name="content_opportunities",
        title="Content opportunities",
        description="Turn ranked query/page opportunities into a prioritised backlog.",
    )(content_opportunities)


def create_server(service: SeoService | None = None) -> McpServer:
    """Build the MCP server and register exactly the eleven registry tools.

    ``service`` defaults to a lazily wired :class:`~lv24_seo.services.SeoService`
    built from :func:`~lv24_seo.core.config.load_config`. Tests and embedders may
    pass any object that implements the read methods they exercise; tools report an
    honest ``UPSTREAM_UNAVAILABLE`` failure for methods it does not implement.
    """
    if _SERVER_CLASS is None:
        raise SeoError(
            code=CONFIG_INVALID,
            message="the MCP SDK does not provide a high-level server class",
            next_action=(
                "install a supported SDK: pip install 'mcp>=1.2' "
                "(mcp 2.x ships MCPServer, mcp 1.x ships FastMCP)"
            ),
        )

    config: SeoConfig | None = None
    workspace = None
    if service is None:
        from lv24_seo.composition import open_workspace

        workspace = open_workspace()
        config = workspace.config
        service = workspace.service

    try:
        state = ToolState(service=service)
        server = _SERVER_CLASS(name=SERVER_NAME, instructions=_INSTRUCTIONS)
        _register_tools(server, state)
        _register_resources(server, state, config)
        _register_prompts(server)
    except BaseException:
        if workspace is not None:
            workspace.close()
        raise
    if workspace is not None:
        _OPEN_WORKSPACES.append(workspace)
    logger.debug("MCP server assembled with %s (%s)", _SERVER_FLAVOR, SERVER_NAME)
    return cast(McpServer, server)


def close_workspaces() -> None:
    """Close every store opened by :func:`create_server` (idempotent)."""
    while _OPEN_WORKSPACES:
        workspace = _OPEN_WORKSPACES.pop()
        try:
            workspace.close()
        except Exception:
            logger.warning("could not close the MCP workspace store")


def _accepts_keyword(function: Callable[..., Any], name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables only
        return True
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == name and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= _MAX_PORT:
        raise SeoError(
            code=INVALID_INPUT,
            message="port must be an integer between 1 and 65535",
            next_action="pass e.g. --port 8765",
        )
    return port


async def serve(transport: str = TRANSPORT_STDIO, *, port: int = 8765) -> None:
    """Run the MCP server over stdio (default) or streamable HTTP.

    ``transport`` accepts ``stdio``, ``http`` and ``streamable-http``. The deprecated
    HTTP+SSE transport is intentionally rejected. A missing streamable-HTTP API in the
    installed SDK raises a clear :class:`~lv24_seo.core.errors.SeoError` before any
    socket is opened.
    """
    if not isinstance(transport, str):
        raise SeoError(
            code=INVALID_INPUT,
            message="transport must be a string",
            next_action="use stdio or http",
        )
    mode = transport.strip().lower()
    if mode == TRANSPORT_STDIO:
        server = create_server()
        try:
            runner = getattr(server, "run_stdio_async", None)
            if not callable(runner):
                raise SeoError(
                    code=CONFIG_INVALID,
                    message="the installed MCP SDK has no stdio async runner",
                    next_action="upgrade mcp or run the CLI instead",
                )
            await runner()
        finally:
            close_workspaces()
        return

    if mode in _HTTP_ALIASES:
        http_port = _validate_port(port)
        server = create_server()
        try:
            runner = getattr(server, "run_streamable_http_async", None)
            if not callable(runner):
                raise SeoError(
                    code=CONFIG_INVALID,
                    message="the installed MCP SDK does not support streamable HTTP",
                    next_action="upgrade mcp (1.9+ or 2.x) or keep the stdio transport",
                )
            if _accepts_keyword(runner, "port"):
                await runner(port=http_port)
                return
            # mcp 1.x reads host/port from server settings rather than the runner signature.
            settings = getattr(server, "settings", None)
            if settings is None or not hasattr(settings, "port"):
                raise SeoError(
                    code=CONFIG_INVALID,
                    message="this MCP SDK cannot be given an HTTP port",
                    next_action="upgrade mcp or configure the port through the SDK settings",
                )
            settings.port = http_port
            await runner()
        finally:
            close_workspaces()
        return

    raise SeoError(
        code=INVALID_INPUT,
        message=f"unsupported transport: {transport}",
        next_action="use stdio or http (streamable HTTP); HTTP+SSE is intentionally unsupported",
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Console entry point for ``python -m lv24_seo.mcp.server``."""
    parser = argparse.ArgumentParser(
        prog="lv24-seo-mcp",
        description="Run the lv24-seo MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for local clients (default); http for streamable HTTP.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for --transport http (default: 8765).",
    )
    args = parser.parse_args(argv)

    try:
        asyncio.run(serve(args.transport, port=args.port))
    except SeoError as exc:
        sys.stderr.write(compact_json(fail(exc)) + "\n")
        raise SystemExit(exit_code_for(exc)) from None


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
