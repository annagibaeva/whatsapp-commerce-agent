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
    """True when two rules' outcomes cannot both apply to one booking and
    the *ranking mechanism below* (specificity, then priority) is what
    would need to settle it.

    At v0 this always returns False. `deny` used to be listed here as
    conflicting with `allow` or a permit, on the theory that whichever
    outcome ranked higher should win. That theory was wrong: the gate
    (see gate.py check 4) now treats any rule that evaluates true with
    outcome `deny` as an absolute veto over a `book` action, regardless
    of whether it was cited and regardless of any other rule's priority
    or specificity. A deny next to a permit is therefore never ambiguous
    — deny always wins — so it is not a conflict for this function to
    catch, and priority plays no part in resolving it (see the comment on
    `priority` in rules/schema.py).

    The function, and check_decidable's use of it, stay in place as a
    guard for a future outcome type that might introduce real ambiguity
    (two outcomes that could both apply, disagree, and are not settled by
    the gate itself). Nothing in v0's outcome set needs it.
    """
    return False


def check_decidable(ruleset: RuleSet) -> None:
    """Fail if two rules could both apply, cannot be ranked, and disagree.

    Two rules matching the same booking is normal and expected: a
    first-time customer over the deposit threshold should get both the
    patch-test lead time and the deposit requirement. That is not a bug,
    so it does not raise here.

    `deny` is not a case this needs to catch (see outcomes_conflict): the
    gate vetoes on any true deny unconditionally, so a deny sitting next
    to a permit needs no ranking and is not treated as a conflict here.
    This function remains as a guard for any future outcome type that
    really would need a priority or specificity relation to resolve.
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
