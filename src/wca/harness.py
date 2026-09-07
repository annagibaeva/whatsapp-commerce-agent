"""Running the twenty cases and printing what happened.

Every case runs twice in practice: once with the gate on, once with it
off. If turning the gate off changes nothing, the gate is not a control.

Counts sit next to every percentage. Twenty cases cannot support a rate,
and a percentage without its count invites someone to read it as one.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from wca.calendar.mock import MockCalendar
from wca.cases import Case, CaseFile
from wca.clock import utc
from wca.escalation import TemplateRegistry, window_view
from wca.gate import evaluate
from wca.models import BlockKind, Verdict
from wca.propose import propose
from wca.rules.schema import RuleSet

START = utc(2026, 8, 21, 9)
GOOD_SLOT = "s_2026_08_25_1400"
TAKEN_SLOT = "s_taken"


class CaseOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    tier: str
    action: str
    verdict: Verdict
    action_matched: bool
    verdict_matched: bool
    booked: bool
    bad_booking: bool


class HarnessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_on: bool
    outcomes: tuple[CaseOutcome, ...]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def bad_bookings(self) -> int:
        return sum(1 for o in self.outcomes if o.bad_booking)

    @property
    def booked(self) -> int:
        return sum(1 for o in self.outcomes if o.booked)

    @property
    def grounding_blocks(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict.kind is BlockKind.GROUNDING)

    @property
    def conclusion_blocks(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict.kind is BlockKind.CONCLUSION)

    def render(self) -> str:
        state = "on" if self.gate_on else "off"
        lines = [
            f"Gate {state}. 20 cases.",
            "",
            f"  bad bookings        {self.bad_bookings}  (n={self.total})",
            f"  bookings completed  {self.booked}  (n={self.total})",
            f"  grounding blocks    {self.grounding_blocks}",
            f"  conclusion blocks   {self.conclusion_blocks}",
            "",
            "These are counts, not rates. Twenty cases cannot support a percentage.",
            "The cases were written and reviewed, not observed in a real salon.",
        ]
        return "\n".join(lines)


def run_cases(
    cases: CaseFile, ruleset: RuleSet, registry: TemplateRegistry, gate_on: bool
) -> HarnessReport:
    outcomes: list[CaseOutcome] = []

    for index, case in enumerate(cases.cases, start=1):
        calendar = MockCalendar(slot_ids=[GOOD_SLOT, TAKEN_SLOT])
        now = START + timedelta(hours=case.hours_into_window)

        # A case that names the taken slot models someone else getting there first.
        if case.slot_id == TAKEN_SLOT:
            calendar.hold(TAKEN_SLOT, thread_id="someone_else", now=now)

        proposal = propose(
            thread_id=case.id, facts=case.facts, ruleset=ruleset,
            slot_id=case.slot_id, now=now, counter=index,
        )

        hold_id = None
        if proposal.action.type == "book" and case.slot_id != TAKEN_SLOT:
            try:
                hold_id = calendar.hold(case.slot_id, thread_id=case.id, now=now).hold_id
            except Exception:  # noqa: BLE001
                hold_id = None
        proposal = proposal.model_copy(update={"hold_id": hold_id})

        cal_view = calendar.view(case.slot_id or GOOD_SLOT, now=now)
        win_view = window_view(
            START, now, proposal.action.escalation_reason or "", registry
        )

        if gate_on:
            verdict = evaluate(proposal, ruleset, cal_view, win_view)
        else:
            verdict = Verdict.passed()

        booked = verdict.allowed and proposal.action.type == "book"
        outcomes.append(CaseOutcome(
            case_id=case.id,
            tier=case.tier,
            action=proposal.action.type,
            verdict=verdict,
            action_matched=proposal.action.type == case.expect_action,
            verdict_matched=(
                verdict.allowed == case.expect_allowed
                and (verdict.check.value if verdict.check else None) == case.expect_check
            ) if gate_on else True,
            booked=booked,
            bad_booking=booked and case.would_be_bad_booking,
        ))

    return HarnessReport(gate_on=gate_on, outcomes=tuple(outcomes))
