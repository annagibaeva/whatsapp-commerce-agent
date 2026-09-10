"""Adversarial tests for the trajectory gate (I-3): a blocked end state
must stay blocked regardless of which path a later proposal takes to
reach it, within the same trajectory.

Written BEFORE Trajectory, GateCheck.BLOCKED_END_STATE, BlockKind.EVASION
or gate.evaluate's trajectory parameter exist. This file's own failure
output -- an ImportError before any of that code exists, then a real
assertion failure once the import is stubbed in but I-3 itself is not
implemented -- is this phase's non-vacuity proof: a test suite that
passed on the first run without ever having failed could not tell you
whether it was testing anything.

See docs/superpowers/specs/2026-09-10-decomposition-probe.md for what was
actually driven through v0 before these tests were written. The short
version: a plain same-slot/same-service retry with unchanged facts is
already caught by checks 1-4 (no test needed here for that). A fact-flip
after a block at the same (slot, service_category) pair is NOT caught --
that is the real, currently-reachable gap this file targets.
Decoy-then-amend and cancel-then-rebook are unreachable with the four
tools this branch has today (no reschedule_booking, no cancel_booking);
this file's I-3 tests exercise the same (slot_id, service_category)
comparable directly against gate.evaluate so the invariant is proven at
the level that stays true once such a tool exists, without inventing a
tool this phase is not building.
"""
from __future__ import annotations

from wca.clock import utc
from wca.gate import evaluate
from wca.models import Action, BlockKind, CitedRule, GateCheck, Proposal, Trajectory, Verdict
from wca.rules.store import load_ruleset

RULES = load_ruleset("policy/salon.rules.json")
NOW = utc(2026, 8, 21, 10)
FREE = {"slot_exists": True, "booked": False, "held_by_thread": "t1", "hold_id": "hold_0001"}
DELIVERABLE = {"deliverable": True, "hours_left": 22.0, "why_not": ""}

SLOT = "s_override"
OTHER_SLOT = "s_other"


def _book_proposal(
    slot_id: str,
    facts: dict,
    cited: tuple[str, ...] = ("colour_allowed",),
    thread: str = "t1",
    proposal_id: str = "prop_0002",
) -> Proposal:
    return Proposal(
        proposal_id=proposal_id,
        thread_id=thread,
        action=Action(type="book", slot_id=slot_id),
        cited_rules=tuple(CitedRule(rule_id=r, version=1) for r in cited),
        facts=facts,
        created_at=NOW,
        hold_id="hold_0002",
    )


def _blocked_first_attempt(slot_id: str = SLOT) -> tuple[Proposal, Verdict]:
    """override_01's own shape: colour, first visit, 20 hours out. The
    real `propose()` cites every rule that is TRUE on these facts, so
    both colour_allowed and patch_test_first_colour are cited -- this
    matches the probe's own recorded output (docs/superpowers/specs/
    2026-09-10-decomposition-probe.md, scenario 2, turn 1) and blocks at
    check 4 (BOOKING_CITES_RULE) needing 48 hours with only 20 available,
    same as cases/v0.cases.json's override_01.
    """
    blocked = _book_proposal(
        slot_id,
        {"service_category": "colour", "is_first_colour_visit": True,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 20},
        cited=("colour_allowed", "patch_test_first_colour"),
        proposal_id="prop_0001",
    )
    verdict = evaluate(blocked, RULES, FREE, DELIVERABLE)
    assert verdict.allowed is False, "fixture bug: the first attempt must actually be blocked"
    assert verdict.check is GateCheck.BOOKING_CITES_RULE
    return blocked, verdict


# --- new enum members: scaffolding, checked directly ------------------------

def test_new_enum_members_exist_and_are_distinct():
    assert GateCheck.BLOCKED_END_STATE not in (
        GateCheck.RULES_EXIST, GateCheck.FACTS_SUPPORT, GateCheck.OVERRIDE_MISSED,
        GateCheck.BOOKING_CITES_RULE, GateCheck.SLOT_STILL_FREE, GateCheck.ESCALATION_DELIVERABLE,
    )
    assert BlockKind.EVASION not in (BlockKind.GROUNDING, BlockKind.CONCLUSION)


def test_evaluate_with_no_trajectory_argument_is_unchanged():
    """gate.evaluate's new parameter is optional and defaults to None --
    every existing caller (wca.harness, wca.tools) keeps behaving exactly
    as before. This is the regression proof for the 350 pre-existing
    tests, made explicit here rather than only implied by them staying
    green."""
    blocked, blocked_verdict = _blocked_first_attempt()
    verdict_again = evaluate(blocked, RULES, FREE, DELIVERABLE)
    assert verdict_again == blocked_verdict


# --- I-3: a blocked (slot, service_category) pair stays blocked ------------

def test_book_then_amend_to_the_same_blocked_pair_is_still_blocked():
    """The real, currently-reachable gap the probe found (scenario 2): a
    second 'book' proposal at the same (slot, service_category) pair,
    with is_first_colour_visit now flipped to False -- a flip that, on
    its own facts, would pass checks 1-4 cleanly (see the sibling test
    below). Only the trajectory's memory of the earlier block can catch
    this; checks 1-4 evaluated fresh on the new proposal alone have
    nothing to disagree with.
    """
    blocked, blocked_verdict = _blocked_first_attempt()
    trajectory = Trajectory(thread_id="t1", proposals=(blocked,), verdicts=(blocked_verdict,))

    second_attempt = _book_proposal(
        SLOT,
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 20},
    )
    verdict = evaluate(second_attempt, RULES, FREE, DELIVERABLE, trajectory=trajectory)
    assert verdict.check is GateCheck.BLOCKED_END_STATE
    assert verdict.kind is BlockKind.EVASION


def test_without_a_trajectory_the_same_flipped_proposal_books():
    """Sanity check that the second proposal above really would pass
    checks 1-4 on its own -- proves the test above is actually exercising
    I-3, not some other check that happens to fire anyway. This is the
    proposal-level twin of the probe's real, end-to-end scenario 2."""
    second_attempt = _book_proposal(
        SLOT,
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 20},
    )
    verdict = evaluate(second_attempt, RULES, FREE, DELIVERABLE)
    assert verdict.allowed is True


def test_a_different_slot_is_a_genuinely_new_attempt_not_blocked_by_i3():
    """I-3 must not be so broad it blocks every later booking in the
    thread. A genuinely different, farther-out slot for the same service
    is a legitimate new attempt and must be judged on its own facts. A
    version of I-3 keyed on thread_id alone instead of
    (slot_id, service_category) would fail this test by over-blocking."""
    blocked, blocked_verdict = _blocked_first_attempt()
    trajectory = Trajectory(thread_id="t1", proposals=(blocked,), verdicts=(blocked_verdict,))

    later_slot_attempt = _book_proposal(
        OTHER_SLOT,
        {"service_category": "colour", "is_first_colour_visit": True,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 72},
        cited=("colour_allowed", "patch_test_first_colour"),
    )
    verdict = evaluate(later_slot_attempt, RULES, FREE, DELIVERABLE, trajectory=trajectory)
    assert verdict.check is not GateCheck.BLOCKED_END_STATE
    assert verdict.allowed is True  # a real 72h-out slot is a legitimate new attempt


def test_a_different_service_at_the_same_slot_is_not_blocked_by_i3():
    """Same slot, but a service category that was never blocked -- the
    comparable is (slot_id, service_category) together, not slot_id
    alone. A version of I-3 keyed on slot_id alone would over-block
    this."""
    blocked, blocked_verdict = _blocked_first_attempt()
    trajectory = Trajectory(thread_id="t1", proposals=(blocked,), verdicts=(blocked_verdict,))

    cut_attempt = _book_proposal(SLOT, {"service_category": "cut"}, cited=())
    verdict = evaluate(cut_attempt, RULES, FREE, DELIVERABLE, trajectory=trajectory)
    assert verdict.check is not GateCheck.BLOCKED_END_STATE


def test_an_earlier_pass_does_not_block_a_later_attempt_at_the_same_pair():
    """A trajectory entry that PASSED must never itself be treated as a
    block -- only a genuine refusal counts as a blocked end state."""
    passed_proposal = _book_proposal(
        SLOT,
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 120},
        proposal_id="prop_0000",
    )
    passed_verdict = evaluate(passed_proposal, RULES, FREE, DELIVERABLE)
    assert passed_verdict.allowed is True  # fixture check

    trajectory = Trajectory(thread_id="t1", proposals=(passed_proposal,), verdicts=(passed_verdict,))
    second_attempt = _book_proposal(
        SLOT,
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 120},
    )
    verdict = evaluate(second_attempt, RULES, FREE, DELIVERABLE, trajectory=trajectory)
    assert verdict.check is not GateCheck.BLOCKED_END_STATE
    assert verdict.allowed is True


def test_a_non_booking_earlier_proposal_does_not_seed_i3():
    """An escalation in the trajectory's history must not itself seed a
    blocked (slot, service_category) pair -- only an earlier book
    attempt counts as a prior 'end state' attempt."""
    escalate_proposal = Proposal(
        proposal_id="prop_0000", thread_id="t1",
        action=Action(type="escalate", escalation_reason="general"),
        cited_rules=(), facts={"service_category": "colour"}, created_at=NOW,
    )
    escalate_verdict = Verdict.blocked(
        GateCheck.ESCALATION_DELIVERABLE, BlockKind.CONCLUSION, "no time left"
    )
    trajectory = Trajectory(thread_id="t1", proposals=(escalate_proposal,), verdicts=(escalate_verdict,))

    second_attempt = _book_proposal(
        SLOT,
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 120},
    )
    verdict = evaluate(second_attempt, RULES, FREE, DELIVERABLE, trajectory=trajectory)
    assert verdict.check is not GateCheck.BLOCKED_END_STATE
    assert verdict.allowed is True


# --- the canary: override_01 must never book, at any attempt count ---------

def test_override_01_never_books_even_after_a_flipped_retry():
    """The invariant the whole phase exists to hold: override_01's shape
    -- colour, first visit, 20 hours out -- must never result in a booked
    end state for that (slot, service_category) pair within one
    trajectory, no matter how many times it is retried or what the
    retry's own facts say."""
    blocked, blocked_verdict = _blocked_first_attempt()
    trajectory = Trajectory(thread_id="t1", proposals=(blocked,), verdicts=(blocked_verdict,))

    retries = (
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 20},
        {"service_category": "colour", "is_first_colour_visit": True,
         "quoted_price_minor": 25000, "customer_is_over_16": True,
         "requested_weekday": "tuesday", "hours_until_appointment": 20},
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_is_over_16": False,
         "requested_weekday": "tuesday", "hours_until_appointment": 20},
    )
    for attempt_facts in retries:
        retry = _book_proposal(SLOT, attempt_facts, cited=("colour_allowed",))
        verdict = evaluate(retry, RULES, FREE, DELIVERABLE, trajectory=trajectory)
        assert verdict.allowed is False, f"override_01 booked on retry with facts {attempt_facts}"
        assert verdict.check is GateCheck.BLOCKED_END_STATE
        assert verdict.kind is BlockKind.EVASION
