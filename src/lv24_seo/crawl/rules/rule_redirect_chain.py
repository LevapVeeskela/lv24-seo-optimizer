"""redirect-chain: more than one redirect hop, or a redirect that never lands on the URL."""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "redirect-chain"
_SEVERITY: Severity = "high"

_MAX_CHAIN_LENGTH = 2


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        chain = list(page.redirect_chain)
        if len(chain) > _MAX_CHAIN_LENGTH or (chain and page.final_url != page.url):
            issues.append(_issue(page.url, {"chain": chain}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Redirect chain",
    description=(
        "Every extra redirect hop wastes crawl budget and dilutes link equity; point redirects "
        "straight at the final 200 URL and update internal links."
    ),
    check=_check,
)
