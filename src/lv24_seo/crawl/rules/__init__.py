"""Crawl rules package: discovery + batch execution."""

from __future__ import annotations

import importlib
import pkgutil

from lv24_seo.crawl.models import Issue, RuleContext
from lv24_seo.crawl.rules.base import Rule, run_rules

__all__ = ["Issue", "Rule", "RuleContext", "load_rules", "run_all", "run_rules"]


def load_rules() -> list[Rule]:
    """Import every ``rule_*`` module in this package and collect its ``RULE``."""
    rules: list[Rule] = []
    for module_info in pkgutil.iter_modules(__path__):
        if not module_info.name.startswith("rule_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        rule = getattr(module, "RULE", None)
        if isinstance(rule, Rule):
            rules.append(rule)
    return sorted(rules, key=lambda r: r.id)


def run_all(ctx: RuleContext, rules: list[Rule] | None = None) -> list[Issue]:
    return run_rules(ctx, load_rules() if rules is None else rules)
