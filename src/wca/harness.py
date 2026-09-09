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
from wca.ids import idempotency_key as make_idempotency_key
from wca.models import AuditRecord, BlockKind, Verdict
from wca.propose import propose
from wca.rules.render import to_english
from wca.rules.schema import RuleSet


def _ratio(hit: int, total: int) -> str:
    """"4/4   100%", or "0/0     n/a" when there is nothing to divide.

    The counts lead and the percentage follows, never the other way
    round: 100% of four is a different claim from 100% of four hundred,
    and a percentage printed alone hides which one this is.
    """
    pct = f"{100 * hit / total:.0f}%" if total else "n/a"
    return f"{hit}/{total}".ljust(6) + pct.rjust(4)


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
    #: This case was one the agent should have completed alone -- the
    #: denominator for booking rate. Twenty cases is not twenty chances
    #: to book: most of them are meant to be refused, so "4 of 20" reads
    #: as failure when 4 of 4 is the target being hit.
    bookable: bool = False
    #: This case genuinely needed a human -- the denominator for
    #: escalation precision, the counter-metric to bad bookings. Without
    #: it, zero bad bookings is achieved by escalating everything.
    should_escalate: bool = False


class HarnessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_on: bool
    outcomes: tuple[CaseOutcome, ...]
    #: One audit record per case, spec section 14 criterion 8: every
    #: case's record must name rule_id@version for what it cited and the
    #: ruleset_version that was live when it was decided.
    audit_records: tuple[AuditRecord, ...] = ()

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

    @property
    def bookable(self) -> int:
        return sum(1 for o in self.outcomes if o.bookable)

    @property
    def bookable_booked(self) -> int:
        return sum(1 for o in self.outcomes if o.bookable and o.booked)

    @property
    def escalated(self) -> int:
        return sum(1 for o in self.outcomes if o.action == "escalate")

    @property
    def escalated_correctly(self) -> int:
        return sum(1 for o in self.outcomes if o.action == "escalate" and o.should_escalate)

    @property
    def cost_of_control(self) -> int:
        """Bookable cases the gate stopped.

        The price of the control, not a failure. A gate that blocks
        nothing is useless; one that blocks good bookings costs revenue.
        Reported, never targeted -- twenty cases cannot support a target.
        """
        return sum(1 for o in self.outcomes if o.bookable and not o.booked)

    def render(self) -> str:
        state = "on" if self.gate_on else "off"
        lines = [
            f"Gate {state}. 20 cases.",
            "",
            f"  bad bookings          {self.bad_bookings}"
            f"{' ' * 8}target 0",
            f"  booking rate          {_ratio(self.bookable_booked, self.bookable)}"
            f"{' ' * 3}target >=80%",
            f"  escalation precision  {_ratio(self.escalated_correctly, self.escalated)}"
            f"{' ' * 3}target >=85%",
            f"  cost of control       {self.cost_of_control}"
            f"{' ' * 8}reported, not targeted",
            "",
            f"  bookings completed    {self.booked}  (n={self.total})",
            f"  grounding blocks      {self.grounding_blocks}",
            f"  conclusion blocks     {self.conclusion_blocks}",
            "",
            "Every percentage carries its counts, because twenty cases cannot",
            "support a rate on their own. Booking rate counts only the cases",
            "that were meant to be booked -- most of these twenty are meant to",
            "be refused, so a low 'bookings completed' is the design working.",
            "The cases were written and reviewed, not observed in a real salon.",
        ]
        return "\n".join(lines)


def run_cases(
    cases: CaseFile, ruleset: RuleSet, registry: TemplateRegistry, gate_on: bool
) -> HarnessReport:
    outcomes: list[CaseOutcome] = []
    audit_records: list[AuditRecord] = []

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

        # A booking only counts as completed if we actually commit it.
        # Reporting "booked" off the verdict alone (as before) counted
        # bookings that were never written to the calendar at all.
        booked = False
        if verdict.allowed and proposal.action.type == "book" and hold_id is not None:
            key = make_idempotency_key(
                case.id, "book", {"proposal_id": proposal.proposal_id, "slot_id": case.slot_id}
            )
            try:
                calendar.commit(hold_id, idempotency_key=key, now=now)
                booked = True
            except Exception:  # noqa: BLE001
                booked = False

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
            bookable=case.expect_action == "book" and case.expect_allowed,
            should_escalate=case.expect_action == "escalate",
        ))

        cited_rules = [
            rule for rule in (
                ruleset.get(ref.rule_id, ref.version) for ref in proposal.cited_rules
            )
            if rule is not None
        ]
        audit_records.append(AuditRecord(
            proposal=proposal,
            verdict=verdict,
            ruleset_version=ruleset.ruleset_version,
            rules_english=tuple(to_english(rule.condition) for rule in cited_rules),
            calendar_read=cal_view,
            window_read=win_view,
            decided_at=now,
        ))

    return HarnessReport(gate_on=gate_on, outcomes=tuple(outcomes), audit_records=tuple(audit_records))
