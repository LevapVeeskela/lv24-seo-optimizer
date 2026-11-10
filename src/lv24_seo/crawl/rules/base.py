"""Rule interface.

A rule is a pure function of :class:`RuleContext`: no network, no clock, no filesystem.
Every rule module lives in this package as ``rule_<slug>.py`` and exposes a module-level
``RULE`` object; :func:`lv24_seo.crawl.rules.load_rules` discovers them, so a new rule never
edits a registry file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from lv24_seo.crawl.models import Issue, RuleContext, Severity


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    severity: Severity
    title: str
    description: str
    check: Callable[[RuleContext], list[Issue]]
    tags: tuple[str, ...] = field(default=())

    def run(self, ctx: RuleContext) -> list[Issue]:
        issues = self.check(ctx)
        for issue in issues:
            # Never let a buggy rule mislabel its own severity or id.
            issue.rule_id = self.id
            issue.severity = self.severity
        return issues


def run_rules(ctx: RuleContext, rules: Iterable[Rule]) -> list[Issue]:
    issues: list[Issue] = []
    for rule in rules:
        issues.extend(rule.run(ctx))
    return issues
