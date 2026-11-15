"""redirect-chain: more than two redirect hops before the final URL.

A single redirect is normal housekeeping (a moved page, an added trailing slash); flagging it
buries real problems under noise. The chain in ``PageData.redirect_chain`` starts with the
requested URL, so the hop count is ``len(chain) - 1``.
"""

from __future__ import annotations

from typing import Any

from lv24_seo.crawl.models import Issue, RuleContext, Severity
from lv24_seo.crawl.rules.base import Rule

_RULE_ID = "redirect-chain"
_SEVERITY: Severity = "high"

_MAX_HOPS = 2


def _issue(url: str, detail: dict[str, Any] | None = None) -> Issue:
    # Rule.run() stamps id/severity, but keeping them here also makes direct _check() calls
    # (and focused unit tests) return fully labelled issues.
    return Issue(rule_id=_RULE_ID, severity=_SEVERITY, url=url, detail=detail or {})


def _hops(chain: list[str]) -> int:
    """Redirect hops: the first chain entry is the requested URL, not a hop."""
    return max(0, len(chain) - 1)


def _check(ctx: RuleContext) -> list[Issue]:
    issues: list[Issue] = []
    for page in ctx.pages:
        chain = list(page.redirect_chain)
        if _hops(chain) > _MAX_HOPS:
            issues.append(_issue(page.url, {"chain": chain, "hops": _hops(chain)}))
    return issues


RULE = Rule(
    id=_RULE_ID,
    severity=_SEVERITY,
    title="Redirect chain",
    description=(
        "More than two redirect hops waste crawl budget and dilute link equity; point "
        "redirects straight at the final 200 URL and update internal links."
    ),
    check=_check,
)
