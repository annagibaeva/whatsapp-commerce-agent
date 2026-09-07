"""Which rule wins when two apply.

The code works this out. Nobody ranks rules by hand.

Rule B is more specific than rule A when B needs every fact A needs, plus
at least one more. The patch-test rule needs service category and first
colour visit. The general colour rule needs only service category. So the
patch-test rule wins, and nobody had to say so.
"""

from __future__ import annotations

from typing import Any

from wca.rules.evaluate import Tri, evaluate_rule
from wca.rules.schema import Rule, RuleSet


def is_more_specific(a: Rule, b: Rule) -> bool:
    """True when a needs strictly more facts than b."""
    fa, fb = set(a.requires_facts), set(b.requires_facts)
    return fa > fb


def matching_rules(ruleset: RuleSet, facts: dict[str, Any]) -> tuple[Rule, ...]:
    return tuple(r for r in ruleset.rules if evaluate_rule(r, facts) is Tri.TRUE)


def unknown_rules(ruleset: RuleSet, facts: dict[str, Any]) -> tuple[Rule, ...]:
    return tuple(r for r in ruleset.rules if evaluate_rule(r, facts) is Tri.UNKNOWN)
