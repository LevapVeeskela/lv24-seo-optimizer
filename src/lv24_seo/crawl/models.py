"""Data structures shared by the crawler, the rules and the reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["critical", "high", "medium", "low"]


@dataclass(slots=True)
class FetchResult:
    """Raw HTTP outcome for one URL (before parsing)."""

    url: str
    status: int
    final_url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: float = 0.0
    redirect_chain: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class PageData:
    """Parsed, rule-friendly view of one crawled page."""

    url: str
    status: int
    final_url: str
    content_type: str = "text/html"
    headers: dict[str, str] = field(default_factory=dict)
    title: str | None = None
    meta_description: str | None = None
    meta_robots: str | None = None
    x_robots_tag: str | None = None
    canonical: str | None = None
    html_lang: str | None = None
    h1: list[str] = field(default_factory=list)
    hreflang: list[dict[str, str]] = field(default_factory=list)
    jsonld: list[dict[str, Any]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    text: str = ""
    word_count: int = 0
    redirect_chain: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def robots_directives(self) -> set[str]:
        """Lower-cased directives from meta robots and X-Robots-Tag combined."""
        raw = " ".join(filter(None, [self.meta_robots, self.x_robots_tag]))
        return {part.strip().lower() for part in raw.replace(",", " ").split() if part.strip()}

    @property
    def is_indexable(self) -> bool:
        return self.status == 200 and "noindex" not in self.robots_directives


@dataclass(slots=True)
class Issue:
    """One rule violation. ``detail`` must be JSON-serialisable and evidence-based."""

    rule_id: str
    severity: Severity
    url: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuleContext:
    """Everything a rule may look at. Rules never perform network calls."""

    pages: list[PageData]
    site_url: str
    sitemap_urls: list[str] = field(default_factory=list)
    robots_sitemaps: list[str] = field(default_factory=list)
    robots_disallow: list[str] = field(default_factory=list)
