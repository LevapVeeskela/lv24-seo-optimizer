"""Provider contract shared by every adapter.

An adapter never decides what the caller does with the data; it only reports what it got and
how complete that is. Completeness is a first-class field because every SEO API is lossy in a
different way (see ``docs/data-sources.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

Row = dict[str, Any]
T = TypeVar("T")

# Stable limitation codes. Reports, MCP responses and skills match on these strings.
ROWS_CAP_50000 = "rows_cap_50000"
NO_QUERY_PAGE_JOIN = "no_query_page_join"
TOP_3000_QUERIES = "top_3000_queries"
SAMPLED = "sampled"
LOW_TRAFFIC = "low_traffic"
DEGRADED_HOST = "degraded_host"
ONE_URL_PER_CALL = "one_url_per_call"
NO_PER_URL_SITEMAP_ERRORS = "no_per_url_sitemap_errors"
TOKEN_EXPIRING = "token_expiring"  # noqa: S105 - a limitation code, not a secret
PAID_PROVIDER = "paid_provider"
PARTIAL_RESPONSE = "partial_response"

# Canonical source identifiers. Collectors key ``sources`` by these, the store
# persists them verbatim, and the report layer maps them to engine sections.
# A provider's ``SOURCE`` constant must equal one of these; anything else is a
# wiring bug, not a new engine.
SOURCE_GSC = "gsc"
SOURCE_YANDEX_WEBMASTER = "yandex_webmaster"
SOURCE_YANDEX_METRIKA = "yandex_metrika"
SOURCE_BING = "bing"
SOURCE_CRUX = "crux"
SOURCE_PSI = "psi"


def now_utc() -> datetime:
    return datetime.now(UTC)


def data_age_days(*, end: datetime, fetched_at: datetime | None = None) -> int:
    """Whole days between the end of the reported window and the fetch time."""
    fetched = fetched_at or now_utc()
    delta = fetched - end
    return max(0, delta.days)


@dataclass(frozen=True, slots=True)
class ProviderResult(Generic[T]):
    """One provider call: rows plus everything a consumer must know to trust them."""

    source: str
    property_id: str
    fetched_at: datetime
    data_age_days: int
    rows: list[T] = field(default_factory=list)
    total: int | None = None
    truncated: bool = False
    limitations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.rows

    def to_meta(self) -> dict[str, Any]:
        """The ``meta`` block expected by the response envelope."""
        meta: dict[str, Any] = {
            "source": self.source,
            "property_id": self.property_id,
            "fetched_at": self.fetched_at.isoformat(),
            "data_age_days": self.data_age_days,
            "truncated": self.truncated,
        }
        if self.total is not None:
            meta["total"] = self.total
        if self.limitations:
            meta["limitations"] = list(self.limitations)
        return meta

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": list(self.rows),
            "meta": self.to_meta(),
            "warnings": list(self.warnings),
        }
