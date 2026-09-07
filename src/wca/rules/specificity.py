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


def outcomes_conflict(a: Rule, b: Rule) -> bool:
    """True when two rules' outcomes cannot both apply to one booking.

    At v0 the only outcome that conflicts with another is `deny`: it means
    refuse the booking, which cannot stand next to `allow` or a permit that
    adds a condition (`require_lead_time`, `require_deposit`,
    `require_escalation`) instead of refusing. Two permits are fine
    together and are expected to stack, so `allow` next to
    `require_deposit` next to `require_lead_time` is not a conflict.
    """
    types = {a.outcome.type, b.outcome.type}
    return "deny" in types and len(types) == 2


def check_decidable(ruleset: RuleSet) -> None:
    """Fail if two rules could both apply, cannot be ranked, and disagree.

    Two rules matching the same booking is normal and expected: a
    first-time customer over the deposit threshold should get both the
    patch-test lead time and the deposit requirement. That is not a bug,
    so it does not raise here.

    It is a bug when one rule says deny and the other says allow (or any
    of the permit outcomes) and nothing decides which one wins: neither
    rule is more specific, and their priorities are equal. Whether the two
    rules' fact sets overlap or not makes no difference to how dangerous
    this is, so it plays no part in the check.
    """
    for a, b in combinations(ruleset.rules, 2):
        if is_more_specific(a, b) or is_more_specific(b, a):
            continue
        if a.priority != b.priority:
            continue
        if not outcomes_conflict(a, b):
            continue
        raise ValueError(
            f"cannot decide between {a.ref()} and {b.ref()}: their outcomes "
            "conflict, neither is more specific, and their priorities are "
            "equal. Set a priority to rank them."
        )
