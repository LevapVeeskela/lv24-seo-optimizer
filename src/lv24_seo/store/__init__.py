"""SQLite storage layer: connection, schema and repositories.

Public surface::

    from lv24_seo.store import Store, StoreError, connect, initialize

* :func:`connect` opens a database with the project PRAGMAs (WAL, foreign keys, ``Row``).
* :func:`initialize` creates the schema and is safe to run on every start.
* :class:`Store` bundles the two with the repository methods used by providers, crawl, reports
  and delivery.
* :class:`StoreError` types a locked (retryable), corrupt (``STORE_CORRUPT`` + backup copy) or
  otherwise unusable database instead of leaking a raw ``sqlite3`` exception.
"""

from lv24_seo.store.db import connect, initialize
from lv24_seo.store.errors import (
    STORE_CORRUPT,
    STORE_LOCKED,
    STORE_UNAVAILABLE,
    StoreError,
)
from lv24_seo.store.repositories import Store

__all__ = [
    "STORE_CORRUPT",
    "STORE_LOCKED",
    "STORE_UNAVAILABLE",
    "Store",
    "StoreError",
    "connect",
    "initialize",
]
