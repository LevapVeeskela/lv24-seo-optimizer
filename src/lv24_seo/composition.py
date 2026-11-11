"""Shared composition root for the CLI, the MCP server and embedders (F24).

Every surface builds its :class:`~lv24_seo.services.SeoService` through
:func:`open_workspace`, so CLI runs and MCP sessions share one SQLite history
instead of the MCP server silently running store-less with an in-memory report
cache. The store opens only when ``init`` created the data directory; otherwise
the workspace is store-less with an explicit warning from the caller.

Lifecycle: :meth:`Workspace.close` closes the store. CLI commands close after
every invocation; the MCP server closes when its transport stops (see
:func:`lv24_seo.mcp.server.serve`). The connection is never shared between
threads: each ``open_workspace`` call owns its connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lv24_seo.core.config import SeoConfig, load_config
from lv24_seo.core.secrets import SecretStore
from lv24_seo.store.repositories import Store

if TYPE_CHECKING:
    from lv24_seo.services import SeoService

DB_FILENAME = "seo.sqlite3"


@dataclass(slots=True)
class Workspace:
    """One configured service plus the store it owns (if any)."""

    config: SeoConfig
    service: SeoService
    store: Store | None

    def close(self) -> None:
        """Close the owned store; safe to call more than once."""
        if self.store is not None:
            self.store.close()


def open_store(config: SeoConfig) -> Store | None:
    """Open the SQLite history only when ``init`` created the data directory."""
    data_dir = Path(config.storage.data_dir).expanduser()
    if not data_dir.is_dir():
        return None
    return Store(data_dir / DB_FILENAME)


def open_workspace(
    config_path: str | Path | None = None,
    *,
    persist: bool = True,
    secrets: SecretStore | None = None,
) -> Workspace:
    """Load config, open the store and wire one :class:`SeoService`.

    ``persist=False`` forces a store-less workspace (read-only surfaces);
    otherwise the store opens when the data directory exists. Importing
    :mod:`lv24_seo.services` here keeps this module free of import cycles.
    """
    from lv24_seo.services import SeoService

    config = load_config(Path(config_path) if config_path is not None else None)
    store = open_store(config) if persist else None
    try:
        service = SeoService(config, store=store, secrets=secrets)
    except BaseException:
        if store is not None:
            store.close()
        raise
    return Workspace(config=config, service=service, store=store)
