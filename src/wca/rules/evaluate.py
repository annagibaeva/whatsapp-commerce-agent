"""Evaluating a condition against the facts we have.

A condition answers TRUE, FALSE or UNKNOWN. UNKNOWN means we do not have
the facts to decide.

Why that third answer matters. The patch-test rule needs to know if this
is a first colour visit. Suppose nobody asked. If UNKNOWN turned into
FALSE, the rule would quietly decide this is not a first visit and the
booking would go through. The customer would arrive with no patch test
and nothing in the logs would look wrong.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from wca.rules.schema import Comparison, Condition, Group, Rule


class Tri(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def _compare(op: str, left: Any, right: Any) -> Tri:
    try:
        match op:
            case "eq":
                return Tri.TRUE if left == right else Tri.FALSE
            case "ne":
                return Tri.TRUE if left != right else Tri.FALSE
            case "lt":
                return Tri.TRUE if left < right else Tri.FALSE
            case "lte":
                return Tri.TRUE if left <= right else Tri.FALSE
            case "gt":
                return Tri.TRUE if left > right else Tri.FALSE
            case "gte":
                return Tri.TRUE if left >= right else Tri.FALSE
            case "in":
                return Tri.TRUE if left in right else Tri.FALSE
            case "is_true":
                return Tri.TRUE if left is True else Tri.FALSE
            case "is_false":
                return Tri.TRUE if left is False else Tri.FALSE
    except TypeError:
        # Comparing a string to a number, for example. We cannot decide,
        # so we say so rather than guessing or crashing.
        return Tri.UNKNOWN
    return Tri.UNKNOWN


def evaluate(condition: Condition, facts: dict[str, Any]) -> Tri:
    if isinstance(condition, Comparison):
        if condition.fact not in facts or facts[condition.fact] is None:
            return Tri.UNKNOWN
        return _compare(condition.op, facts[condition.fact], condition.value)

    if condition.all is not None:
        results = [evaluate(part, facts) for part in condition.all]
        if Tri.FALSE in results:
            return Tri.FALSE
        if Tri.UNKNOWN in results:
            return Tri.UNKNOWN
        return Tri.TRUE

    if condition.any is not None:
        results = [evaluate(part, facts) for part in condition.any]
        if Tri.TRUE in results:
            return Tri.TRUE
        if Tri.UNKNOWN in results:
            return Tri.UNKNOWN
        return Tri.FALSE

    inner = evaluate(condition.not_, facts)
    if inner is Tri.UNKNOWN:
        return Tri.UNKNOWN
    return Tri.FALSE if inner is Tri.TRUE else Tri.TRUE


def evaluate_rule(rule: Rule, facts: dict[str, Any]) -> Tri:
    return evaluate(rule.condition, facts)


def missing_facts(rule: Rule, facts: dict[str, Any]) -> tuple[str, ...]:
    """Facts the rule needs that we do not have. Drives asking a question."""
    return tuple(
        name for name in rule.requires_facts
        if name not in facts or facts[name] is None
    )
