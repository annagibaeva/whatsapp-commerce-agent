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


#: Facts only the customer's own words can establish -- mirrors
#: wca.tools.CONVERSATIONAL_FACTS, which is the source of truth for this
#: classification (guarded there by
#: test_every_fact_the_ruleset_reads_is_classified). Not imported from
#: wca.tools directly: wca.tools already does `from wca.gate import
#: evaluate`, so a gate -> tools import would be circular, and wca.tools
#: also imports wca.conversation.state, a prefix test_gate_purity.py's
#: BANNED_PREFIXES explicitly forbids gate.py from pulling in even
#: transitively. wca.models is a leaf both wca.gate and wca.tools already
#: import from without incident, so the classification lives here instead
#: -- gate.py reads it without gaining any new import edge, and it never
#: has to be re-listed inline in gate.py itself.
CONVERSATIONAL_FACTS: frozenset[str] = frozenset({
    "is_first_colour_visit",
    "customer_is_over_16",
})


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
    #: I-3 (see docs/superpowers/specs/2026-09-10-v1-trajectory-gate-design.md
    #: §5). A book/reschedule proposal targets a (slot_id, service_category)
    #: pair this trajectory already refused, by any earlier proposal, via
    #: any tool. Checks 1-6 above have no memory of a *different* proposal
    #: in the same conversation; this is the one check that does.
    BLOCKED_END_STATE = "blocked_end_state"
    #: I-2 (see the design spec's Decision 4, and whatsapp-v1-plan-and-act-
    #: spec.md §4). A CONVERSATIONAL_FACTS value already seen in an earlier
    #: proposal in this trajectory changed on a later one. I-3's sibling:
    #: I-3 only compares the (slot_id, service_category) end state a
    #: book/reschedule proposal targets, so a flip aimed at a *different*
    #: slot -- never blocked there before -- slips past it. This check
    #: catches the flip itself, independent of which slot it targets.
    FACT_STABILITY = "fact_stability"
    #: I-4 (see whatsapp-v1-plan-and-act-spec.md §4). This trajectory has
    #: already spent its cap of booking attempts (gate.MAX_REVISIONS); only
    #: `escalate` is permitted now. CONCLUSION, not EVASION -- running out
    #: of budget honestly, without ever reaching a blocked end state twice,
    #: is a different failure than evading one (design spec §6).
    BUDGET_EXHAUSTED = "budget_exhausted"


class BlockKind(StrEnum):
    """Which kind of mistake this was.

    grounding: the agent used a rule that does not exist or does not apply.
    conclusion: the rules were right and the call was still wrong.
    evasion: neither -- the planner reached a state this trajectory
        already refused, by a different route. Counted separately from
        grounding and conclusion because it is a different failure with a
        different fix: a planner rattling the door, not reasoning badly
        about policy or about the world.
    """

    GROUNDING = "grounding"
    CONCLUSION = "conclusion"
    EVASION = "evasion"


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


class Trajectory(_Strict):
    """Every proposal and verdict this thread has produced so far, in order.

    In memory only in this slice -- no SQLite store, see
    docs/superpowers/specs/2026-09-10-v1-trajectory-gate-design.md §4 and
    Decision 3. `proposals` and `verdicts` are parallel tuples, not a list
    of pairs, because `AuditRecord` already stores `proposal`/`verdict`
    this way and I-3 (`gate.py`) walks both with `zip()` -- one shape for
    "a decision" across this codebase, not two.

    Deliberately thin: no `plans` field (no `Plan`/`PlanStep` object
    exists in this slice, see the design spec's Decision 2). I-2 and I-4
    are both built (gate.py) directly on `proposals`/`verdicts` rather
    than adding the `fact_ledger` or `tool_calls` fields the design
    spec's §4 sketches: I-2 compares CONVERSATIONAL_FACTS values across
    `proposals[i].facts` directly, with no separate sourced ledger, and
    I-4 counts book/reschedule entries in `proposals` itself rather than
    keeping a running counter. Both give the same guarantee this slim
    shape already supports; a `fact_ledger` or `tool_calls` field would
    be bookkeeping nothing here reads.

    Every "append" is a `model_copy` -- `Trajectory` is frozen like every
    other `_Strict` model, so `t.model_copy(update={"proposals": t.proposals
    + (new,)})` is how a caller grows one, never in-place mutation.
    """

    thread_id: str
    proposals: tuple[Proposal, ...] = ()
    verdicts: tuple[Verdict, ...] = ()   # verdicts[i] is the verdict for proposals[i]


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
