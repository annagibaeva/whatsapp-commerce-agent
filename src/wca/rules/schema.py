"""What a rule looks like.

A condition is nested data, not a string and not code. A small function
walks it. Nothing is compiled and nothing is executed, so a rules file
cannot run anything.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Op = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "is_true", "is_false"]
OutcomeType = Literal[
    "allow", "deny", "require_lead_time", "require_deposit", "require_escalation"
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Comparison(_Strict):
    """One test against one fact."""

    fact: str
    op: Op
    value: Any = None


class Group(_Strict):
    """A combinator. Exactly one of all, any or not_ is set."""

    all: tuple[Condition, ...] | None = None
    any: tuple[Condition, ...] | None = None
    not_: Condition | None = Field(default=None, alias="not")

    @model_validator(mode="after")
    def _exactly_one(self) -> Group:
        set_count = sum(x is not None for x in (self.all, self.any, self.not_))
        if set_count != 1:
            raise ValueError("a group sets exactly one of all, any or not")
        if self.all is not None and not self.all:
            raise ValueError("an empty all group is not allowed")
        if self.any is not None and not self.any:
            raise ValueError("an empty any group is not allowed")
        return self


Condition = Annotated[Union[Comparison, Group], Field(union_mode="left_to_right")]


class Outcome(_Strict):
    type: OutcomeType
    reason: str
    hours: int | None = Field(default=None, ge=0)
    amount_minor: int | None = Field(default=None, ge=0)


def facts_used(condition: Condition) -> frozenset[str]:
    """Every fact name the condition mentions, at any depth."""
    if isinstance(condition, Comparison):
        return frozenset({condition.fact})
    parts: list[Condition] = []
    if condition.all is not None:
        parts = list(condition.all)
    elif condition.any is not None:
        parts = list(condition.any)
    elif condition.not_ is not None:
        parts = [condition.not_]
    out: frozenset[str] = frozenset()
    for part in parts:
        out |= facts_used(part)
    return out


class Rule(_Strict):
    id: str
    version: int = Field(ge=1)
    condition: Condition
    outcome: Outcome
    # Read by check_decidable (rules/specificity.py) at load time to rank
    # rules whose outcomes conflict and cannot be ordered by specificity.
    # The gate (gate.py) does NOT consult this field at booking time — it
    # is a load-time ranking tool only, never a runtime override. A wave-1
    # ruling once set no_colour_on_sunday's priority high believing that
    # would make its deny outrank a permit at booking time; it does not,
    # because the gate never reads priority, and the bug that ruling was
    # meant to fix (a subset-citation could still book over an applicable
    # deny) was not fixed until the gate itself vetoed on deny directly.
    priority: int = 0
    source_text: str
    requires_facts: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _derive_or_check_requires_facts(self) -> Rule:
        derived = tuple(sorted(facts_used(self.condition)))
        if not self.requires_facts:
            object.__setattr__(self, "requires_facts", derived)
            return self
        if tuple(sorted(self.requires_facts)) != derived:
            raise ValueError(
                f"requires_facts {sorted(self.requires_facts)} does not match the "
                f"condition, which uses {list(derived)}"
            )
        return self

    def ref(self) -> str:
        return f"{self.id}@{self.version}"


class RuleSet(_Strict):
    rules: tuple[Rule, ...]

    @model_validator(mode="after")
    def _no_duplicates(self) -> RuleSet:
        seen: set[tuple[str, int]] = set()
        for rule in self.rules:
            key = (rule.id, rule.version)
            if key in seen:
                raise ValueError(f"duplicate rule {rule.ref()}")
            seen.add(key)
        return self

    @property
    def ruleset_version(self) -> str:
        payload = json.dumps(
            [r.model_dump(mode="json") for r in self.rules],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def get(self, rule_id: str, version: int) -> Rule | None:
        for rule in self.rules:
            if rule.id == rule_id and rule.version == version:
                return rule
        return None
