"""Turning a condition into a sentence.

The salon owner reads this in the audit record. It means nobody has to
build a rule language for them to check their own policy.
"""

from __future__ import annotations

from typing import Any

from wca.rules.schema import Comparison, Condition, Group

PHRASE = {
    "eq": "is",
    "ne": "is not",
    "lt": "is less than",
    "lte": "is at most",
    "gt": "is more than",
    "gte": "is at least",
    "in": "is one of",
}


def _name(fact: str) -> str:
    return fact.replace("is_", "").replace("_", " ")


def _value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def to_english(condition: Condition) -> str:
    if isinstance(condition, Comparison):
        if condition.op == "is_true":
            return f"{_name(condition.fact)} is true"
        if condition.op == "is_false":
            return f"{_name(condition.fact)} is false"
        return f"{_name(condition.fact)} {PHRASE[condition.op]} {_value(condition.value)}"

    def wrap(part: Condition) -> str:
        text = to_english(part)
        return f"({text})" if isinstance(part, Group) else text

    if condition.all is not None:
        return " AND ".join(wrap(p) for p in condition.all)
    if condition.any is not None:
        return " OR ".join(wrap(p) for p in condition.any)
    return f"NOT ({to_english(condition.not_)})"
