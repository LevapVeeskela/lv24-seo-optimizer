"""MCP public surface: build the server or run a transport.

The two names are exported lazily (PEP 562) so importing
:mod:`lv24_seo.mcp.registry` does not pull in the SDK, the service layer and every
provider. ``from lv24_seo.mcp import create_server`` behaves exactly like an eager
export.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - static visibility only
    from lv24_seo.mcp.server import create_server, serve

__all__ = ["create_server", "serve"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from lv24_seo.mcp import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
