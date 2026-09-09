"""The objects that move between components.

The important one is Verdict. It says yes or no and nothing else. There
is no field on it that carries a corrected booking, so the gate cannot
suggest anything. A component that can only refuse cannot cause a bad
booking of its own.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ActionType = Literal["book", "ask", "escalate", "decline"]


class Action(_Strict):
    type: ActionType
    slot_id: str | None = None
    quantity: int = Field(default=1, ge=1)
    question: str | None = None
    escalation_reason: str | None = None


class CitedRule(_Strict):
    rule_id: str
    version: int = Field(ge=1)

    def ref(self) -> str:
        return f"{self.rule_id}@{self.version}"


class Proposal(_Strict):
    proposal_id: str
    thread_id: str
    action: Action
    cited_rules: tuple[CitedRule, ...]
    facts: dict[str, Any]
    created_at: datetime
    hold_id: str | None = None


class GateCheck(StrEnum):
    RULES_EXIST = "rules_exist"
    FACTS_SUPPORT = "facts_support"
    OVERRIDE_MISSED = "override_missed"
    BOOKING_CITES_RULE = "booking_cites_rule"
    SLOT_STILL_FREE = "slot_still_free"
    ESCALATION_DELIVERABLE = "escalation_deliverable"


class BlockKind(StrEnum):
    """Which kind of mistake this was.

    grounding: the agent used a rule that does not exist or does not apply.
    conclusion: the rules were right and the call was still wrong.
    """

    GROUNDING = "grounding"
    CONCLUSION = "conclusion"


class Verdict(_Strict):
    allowed: bool
    check: GateCheck | None = None
    kind: BlockKind | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _blocks_explain_themselves(self) -> Verdict:
        if self.allowed:
            if self.check is not None or self.kind is not None:
                raise ValueError("a passing verdict names no failed check")
            return self
        if self.check is None or self.kind is None or not self.reason.strip():
            raise ValueError("a block names its check, its kind and a reason")
        return self

    @classmethod
    def passed(cls) -> Verdict:
        return cls(allowed=True)

    @classmethod
    def blocked(cls, check: GateCheck, kind: BlockKind, reason: str) -> Verdict:
        return cls(allowed=False, check=check, kind=kind, reason=reason)


class EscalationTicket(_Strict):
    thread_id: str
    reason: str
    raised_at: datetime
    window_closes_at: datetime
    template_name: str


class AuditRecord(_Strict):
    proposal: Proposal
    verdict: Verdict
    ruleset_version: str
    rules_english: tuple[str, ...]
    calendar_read: dict[str, Any]
    window_read: dict[str, Any]
    decided_at: datetime
    #: What the conversation turn this record's proposal came out of cost
    #: in US dollars -- extraction plus every agent model call that turn
    #: made (see `wca.agent.Agent.cost_usd` and `wca.audit.AuditLog
    #: .add_turn_cost`). `None` when nothing populated it: `wca.harness`
    #: builds `AuditRecord`s straight from `propose()`/`evaluate()`, no
    #: model call and no cost involved, so it never sets this field.
    #: Optional with a default so every existing `AuditRecord(...)` call
    #: -- in this codebase and in any test -- keeps constructing unchanged.
    turn_cost_usd: float | None = None
