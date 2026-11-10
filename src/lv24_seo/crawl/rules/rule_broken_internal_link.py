"""broken-internal-link: internal links with no crawled 200 target."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

from lv24_seo.crawl.models import Issue, PageData, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "broken-internal-link"
_SEVERITY: Severity = "high"
_MAX_ISSUES_PER_PAGE = 50
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _html_pages(ctx: RuleContext) -> list[PageData]:
    return [
        page
        for page in ctx.pages
        if page.status == 200 and page.content_type.startswith("text/html")
    ]


def _normalize(url: str) -> str:
    """Lower-case scheme/host, drop the fragment, default port and trailing slash."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:  # malformed port in user-supplied HTML
        port = None
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def _resolve(base: str, link: str) -> str | None:
    """Absolute, normalized http(s) target for a raw href, or None for non-web links."""
    absolute = urljoin(base, link.strip())
    parsed = urlparse(absolute)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    return _normalize(absolute)


def _check(ctx: RuleContext) -> list[Issue]:
    site_host = (urlparse(ctx.site_url).hostname or "").lower()

    healthy: set[str] = set()
    known: set[str] = set()
    for page in ctx.pages:
        for candidate in (page.url, page.final_url):
            if not candidate:
                continue
            normalized = _normalize(candidate)
            known.add(normalized)
            if page.status == 200:
                healthy.add(normalized)

    issues: list[Issue] = []
    for page in _html_pages(ctx):
        base = page.final_url or page.url
        seen: set[str] = set()
        for link in page.links:
            if len(seen) >= _MAX_ISSUES_PER_PAGE:
                break
            target = _resolve(base, link)
            if target is None or target in seen:
                continue
            if (urlparse(target).hostname or "").lower() != site_host:
                continue
            if target in healthy:
                continue
            # ``seen`` also deduplicates broken targets, so it is the per-page issue counter.
            seen.add(target)
            reason = "status" if target in known else "missing"
            issues.append(_issue(page.url, {"target": target, "reason": reason}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Broken internal link",
    description=(
        "Internal links pointing to 4xx/5xx or uncrawled URLs waste crawl budget and "
        "strand users; fix or remove them."
    ),
    check=_check,
)
