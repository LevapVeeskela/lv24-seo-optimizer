"""canonical-mismatch: the declared canonical points at a different URL."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "canonical-mismatch"
_SEVERITY: Severity = "high"


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _norm(url: str) -> str:
    """Strip the fragment, lowercase the host and drop a trailing slash."""
    parsed = urlsplit(url.strip())
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        if page.status != 200:
            continue
        canonical = (page.canonical or "").strip()
        if not canonical:
            continue
        # Compare against the fetched URL: after a redirect the canonical must
        # point at the final address, not at the pre-redirect one.
        identity = page.final_url or page.url
        # Relative canonicals are legal HTML; compare the URL they resolve to.
        if _norm(urljoin(identity, canonical)) == _norm(identity):
            continue
        issues.append(_issue(page.url, {"canonical": canonical, "url": identity}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Canonical points elsewhere",
    description=(
        "The declared canonical resolves to another URL, so this page asks to be dropped from "
        "the index; point it at itself unless the page is an intentional duplicate."
    ),
    check=_check,
)
