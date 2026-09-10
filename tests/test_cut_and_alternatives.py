"""Cuts are bookable, and a blocked colour booking can offer one as a
real, checked alternative -- never a model-invented one.

Two changes, proven separately and then together:

1. `cut_allowed` (policy/salon.rules.json) actually grounds a cut
   booking -- at the gate level directly, and through the real
   `wca.tools.request_booking` path.
2. `wca.tools.request_booking`'s own alternatives computation
   (`_bookable_alternatives`) surfaces that fact on a blocked colour
   attempt: it runs `propose()` + `gate.evaluate()` for real, once per
   other catalogue service, the same discipline `wca.cli
   ._interactive_offer` uses for its own reply shape -- never guessed,
   never declared by a model.

The test that matters most is `test_colour_blocked_then_cut_allowed_
then_colour_still_blocked` below: it proves the gate tells "offer a real
alternative" apart from "evade the same refusal by a different route".
Both look, superficially, like "book something at the slot that first
got refused" -- I-3 (`gate.py`, `docs/superpowers/specs/
2026-09-10-v1-trajectory-gate-design.md`) is what tells them apart, on
the (slot_id, service_category) pair alone: (SLOT, "cut") was never
refused, so it books; (SLOT, "colour") was, so no later route to it in
the same trajectory un-blocks it, cut booked in between or not.
"""
from __future__ import annotations

from datetime import timedelta

from wca.audit import AuditLog
from wca.calendar.mock import MockCalendar, Slot
from wca.catalogue import Catalogue, Service, load_catalogue
from wca.clock import utc
from wca.conversation.state import ConversationState
from wca.escalation import Template, TemplateRegistry
from wca.gate import evaluate
from wca.models import Action, BlockKind, CitedRule, GateCheck, Proposal, Trajectory
from wca.rules.schema import RuleSet
from wca.rules.store import load_ruleset
from wca.tools import ToolContext, _bookable_alternatives, request_booking

RULES = load_ruleset("policy/salon.rules.json")
CATALOGUE = load_catalogue("policy/salon.catalogue.json")

NOW = utc(2026, 8, 21, 10)  # a Friday
SLOT = "s_slot"
SLOT_STARTS_AT = NOW + timedelta(hours=20)  # 20 hours out -- too soon for a patch test

REGISTRY = TemplateRegistry(templates=(
    Template(reason="patch_test_first_colour", name="patch_test_notice", approved=True),
))

FREE = {"slot_exists": True, "booked": False, "held_by_thread": "t1", "hold_id": "hold_0001"}
DELIVERABLE = {"deliverable": True, "hours_left": 22.0, "why_not": ""}

FIRST_VISIT_FACTS = {"is_first_colour_visit": True, "customer_is_over_16": True}


def _calendar() -> MockCalendar:
    return MockCalendar(slots=[Slot(SLOT, SLOT_STARTS_AT)])


def _ctx(calendar: MockCalendar, facts: dict | None = None) -> ToolContext:
    conversation = ConversationState(
        thread_id="t1", last_inbound_at=NOW, facts=dict(facts or {}),
    )
    return ToolContext(
        ruleset=RULES, catalogue=CATALOGUE, calendar=calendar, registry=REGISTRY,
        audit=AuditLog(), conversation=conversation, now=NOW,
    )


# --- gate-level: the test that matters most ---------------------------------

def _book_proposal(slot_id: str, facts: dict, cited: tuple[str, ...], proposal_id: str) -> Proposal:
    return Proposal(
        proposal_id=proposal_id, thread_id="t1",
        action=Action(type="book", slot_id=slot_id),
        cited_rules=tuple(CitedRule(rule_id=r, version=1) for r in cited),
        facts=facts, created_at=NOW, hold_id="hold_0002",
    )


def test_colour_blocked_then_cut_allowed_then_colour_still_blocked():
    colour_facts = {
        "service_category": "colour", "is_first_colour_visit": True,
        "quoted_price_minor": 9000, "customer_is_over_16": True,
        "requested_weekday": "saturday", "hours_until_appointment": 20,
    }

    # 1. Colour, first visit, 20 hours out: blocked on the patch test.
    colour_proposal = _book_proposal(
        SLOT, colour_facts, cited=("colour_allowed", "patch_test_first_colour"),
        proposal_id="prop_0001",
    )
    colour_verdict = evaluate(colour_proposal, RULES, FREE, DELIVERABLE)
    assert colour_verdict.allowed is False
    assert colour_verdict.check is GateCheck.BOOKING_CITES_RULE

    trajectory = Trajectory(thread_id="t1", proposals=(colour_proposal,), verdicts=(colour_verdict,))

    # 2. A cut at that same slot, in the same trajectory: allowed. The
    # customer ends up with a cut, which was never refused. I-3 compares
    # (slot_id, service_category), and (SLOT, "cut") != (SLOT, "colour").
    cut_proposal = _book_proposal(
        SLOT, {"service_category": "cut"}, cited=("cut_allowed",), proposal_id="prop_0002",
    )
    cut_verdict = evaluate(cut_proposal, RULES, FREE, DELIVERABLE, trajectory=trajectory)
    assert cut_verdict.allowed is True, (
        "a cut at the same slot must be a genuinely different, bookable "
        "outcome -- if this is blocked, cuts and I-3 are incompatible as "
        "built and nothing past this point should change"
    )

    trajectory = trajectory.model_copy(update={
        "proposals": trajectory.proposals + (cut_proposal,),
        "verdicts": trajectory.verdicts + (cut_verdict,),
    })

    # 3. The same colour again, with is_first_colour_visit flipped to
    # false, same slot, same trajectory: still blocked, as
    # blocked_end_state / EVASION. The cut booked in between does not
    # un-block the (SLOT, "colour") pair -- this is the decoy-evasion
    # shape, and it must not be confused with step 2's real alternative.
    retry_facts = dict(colour_facts, is_first_colour_visit=False)
    retry_proposal = _book_proposal(
        SLOT, retry_facts, cited=("colour_allowed",), proposal_id="prop_0003",
    )
    retry_verdict = evaluate(retry_proposal, RULES, FREE, DELIVERABLE, trajectory=trajectory)
    assert retry_verdict.check is GateCheck.BLOCKED_END_STATE
    assert retry_verdict.kind is BlockKind.EVASION


def test_without_a_trajectory_the_flipped_colour_retry_really_would_book():
    """Sanity twin of the test above: proves step 3's block really is I-3
    doing the work, not some other check that would have fired anyway."""
    colour_facts = {
        "service_category": "colour", "is_first_colour_visit": False,
        "quoted_price_minor": 9000, "customer_is_over_16": True,
        "requested_weekday": "saturday", "hours_until_appointment": 20,
    }
    retry_proposal = _book_proposal(
        SLOT, colour_facts, cited=("colour_allowed",), proposal_id="prop_0003",
    )
    assert evaluate(retry_proposal, RULES, FREE, DELIVERABLE).allowed is True


# --- cut_allowed grounds a real booking, end to end --------------------------

def test_a_plain_cut_books_through_request_booking():
    """Before cut_allowed, this exact call blocked with 'no cited rule
    allows a booking' (see the background this task started from). Now
    it succeeds, with no patch test, no deposit, no guardian check, no
    Sunday restriction -- the four colour-only rules never reach a cut."""
    calendar = _calendar()
    ctx = _ctx(calendar)

    result = request_booking(ctx, service_id="svc_cut", slot_id=SLOT)

    assert result["ok"] is True
    assert len(calendar.bookings()) == 1


def test_removing_cut_allowed_blocks_the_same_booking_again():
    """Non-vacuity check for the rule itself: a ruleset with cut_allowed
    taken back out reproduces the original bug exactly."""
    without_cut_allowed = RuleSet(rules=tuple(r for r in RULES.rules if r.id != "cut_allowed"))
    calendar = _calendar()
    ctx = ToolContext(
        ruleset=without_cut_allowed, catalogue=CATALOGUE, calendar=calendar, registry=REGISTRY,
        audit=AuditLog(), conversation=ConversationState(thread_id="t1", last_inbound_at=NOW),
        now=NOW,
    )

    result = request_booking(ctx, service_id="svc_cut", slot_id=SLOT)

    assert result["ok"] is False
    assert "no cited rule allows a booking" in result["reason"]


# --- request_booking offers a real alternative on a patch-test block --------

def test_a_patch_test_block_offers_a_cut_as_a_real_alternative():
    calendar = _calendar()
    ctx = _ctx(calendar, facts=FIRST_VISIT_FACTS)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=SLOT)

    assert result["ok"] is False
    assert "48" in result["reason"]
    assert "alternatives" in result
    ids = {a["service_id"] for a in result["alternatives"]}
    assert ids == {"svc_cut"}, (
        "the other colour service (svc_colour_roots) needs the same patch "
        "test and must not appear; only the genuinely bookable cut should"
    )


def test_a_successful_booking_carries_no_alternatives_field():
    calendar = _calendar()
    ctx = _ctx(calendar, facts={"is_first_colour_visit": False, "customer_is_over_16": True})

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=SLOT)

    assert result["ok"] is True
    assert "alternatives" not in result


# --- vacuity check for the alternatives computation itself ------------------

def test_bookable_alternatives_is_empty_when_the_catalogue_has_nothing_else():
    """Direct unit test of `_bookable_alternatives`: with only one service
    in the catalogue, there is nothing else to offer, and the function
    must say so rather than fabricating something -- proves the empty
    case is a real branch, not just the untested half of an always-
    non-empty function."""
    lone_service = Service(
        id="svc_only", name="only service", category="colour",
        duration_minutes=30, price_minor=5000,
    )
    lone_catalogue = Catalogue(services=(lone_service,))
    calendar = _calendar()
    ctx = ToolContext(
        ruleset=RULES, catalogue=lone_catalogue, calendar=calendar, registry=REGISTRY,
        audit=AuditLog(), conversation=ConversationState(thread_id="t1", last_inbound_at=NOW),
        now=NOW,
    )
    facts = {"service_category": "colour", "is_first_colour_visit": True,
              "quoted_price_minor": 5000, "customer_is_over_16": True,
              "requested_weekday": "saturday", "hours_until_appointment": 20}

    alternatives = _bookable_alternatives(ctx, SLOT, "svc_only", facts, FREE, DELIVERABLE)

    assert alternatives == []
