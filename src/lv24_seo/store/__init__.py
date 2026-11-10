"""SQLite storage layer: connection, schema and repositories.

Public surface::

    from lv24_seo.store import Store, connect, initialize

* :func:`connect` opens a database with the project PRAGMAs (WAL, foreign keys, ``Row``).
* :func:`initialize` creates the schema and is safe to run on every start.
* :class:`Store` bundles the two with the repository methods used by providers, crawl, reports
  and delivery.
"""

from lv24_seo.store.db import connect, initialize
from lv24_seo.store.repositories import Store

__all__ = ["Store", "connect", "initialize"]
