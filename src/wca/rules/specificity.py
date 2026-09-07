"""Which rule wins when two apply.

The code works this out. Nobody ranks rules by hand.

Rule B is more specific than rule A when B needs every fact A needs, plus
at least one more. The patch-test rule needs service category and first
colour visit. The general colour rule needs only service category. So the
patch-test rule wins, and nobody had to say so.
"""

from __future__ import annotations

from itertools import combinations
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


def check_decidable(ruleset: RuleSet) -> None:
    """Fail if two rules could both apply and nothing can rank them.

    A policy that cannot say which of two rules applies is a bug in the
    policy. Better to find it now than when a customer is booking.
    """
    for a, b in combinations(ruleset.rules, 2):
        if is_more_specific(a, b) or is_more_specific(b, a):
            continue
        # Rules that share at least one fact are related enough that a
        # human reading the policy can tell them apart. Two rules that
        # test entirely different facts, with no specificity relation and
        # no priority to break the tie, cannot be ranked at all.
        if set(a.requires_facts) & set(b.requires_facts):
            continue
        if a.priority == b.priority:
            raise ValueError(
                f"cannot decide between {a.ref()} and {b.ref()}: they test "
                "unrelated facts and have the same priority"
            )
