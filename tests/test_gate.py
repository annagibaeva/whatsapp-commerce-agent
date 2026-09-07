import pytest

from wca.clock import utc
from wca.gate import evaluate
from wca.models import Action, BlockKind, CitedRule, GateCheck, Proposal
from wca.rules.store import load_ruleset

RULES = load_ruleset("policy/salon.rules.json")
NOW = utc(2026, 8, 21, 10)
FREE = {"slot_exists": True, "booked": False, "held_by_thread": "t1", "hold_id": "hold_0001"}
DELIVERABLE = {"deliverable": True, "hours_left": 22.0, "why_not": ""}


def _proposal(cited, facts, action=None, thread="t1", versions=None):
    versions = versions or {}
    return Proposal(
        proposal_id="prop_0001",
        thread_id=thread,
        action=action or Action(type="book", slot_id="s1"),
        cited_rules=tuple(CitedRule(rule_id=r, version=versions.get(r, 1)) for r in cited),
        facts=facts,
        created_at=NOW,
        hold_id="hold_0001",
    )


def test_a_clean_booking_passes():
    p = _proposal(
        ["colour_allowed"],
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_age": 30, "requested_weekday": "tuesday"},
    )
    assert evaluate(p, RULES, FREE, DELIVERABLE).allowed is True


def test_check_1_blocks_a_rule_that_does_not_exist():
    p = _proposal(["invented_rule"], {"service_category": "colour"})
    v = evaluate(p, RULES, FREE, DELIVERABLE)
    assert v.check is GateCheck.RULES_EXIST
    assert v.kind is BlockKind.GROUNDING


def test_check_2_blocks_a_rule_the_facts_do_not_support():
    p = _proposal(["colour_allowed"], {"service_category": "cut"})
    v = evaluate(p, RULES, FREE, DELIVERABLE)
    assert v.check is GateCheck.FACTS_SUPPORT
    assert v.kind is BlockKind.GROUNDING


def test_check_3_blocks_the_patch_test_case():
    # The agent cites the general colour rule and books tomorrow. The
    # patch-test rule also applies and needs 48 hours.
    p = _proposal(
        ["colour_allowed"],
        {"service_category": "colour", "is_first_colour_visit": True,
         "quoted_price_minor": 9000, "customer_age": 30, "requested_weekday": "tuesday"},
    )
    v = evaluate(p, RULES, FREE, DELIVERABLE)
    assert v.check is GateCheck.OVERRIDE_MISSED
    assert v.kind is BlockKind.GROUNDING
    assert "patch_test_first_colour" in v.reason


def test_check_3_also_blocks_when_the_fact_was_never_established():
    # Nobody asked whether this is a first colour visit. That is not a no.
    p = _proposal(
        ["colour_allowed"],
        {"service_category": "colour", "quoted_price_minor": 9000,
         "customer_age": 30, "requested_weekday": "tuesday"},
    )
    v = evaluate(p, RULES, FREE, DELIVERABLE)
    assert v.check is GateCheck.OVERRIDE_MISSED
    assert "is_first_colour_visit" in v.reason


def test_check_4_blocks_a_booking_that_cites_nothing():
    p = _proposal([], {"service_category": "colour"})
    v = evaluate(p, RULES, FREE, DELIVERABLE)
    assert v.check is GateCheck.BOOKING_CITES_RULE


def test_check_4_blocks_a_booking_whose_only_rule_denies_it():
    # no_colour_on_sunday is version 2 in the live ruleset: a wave-1 ruling
    # gave it priority 10 and bumped the version when the record changed.
    p = _proposal(
        ["no_colour_on_sunday"],
        {"service_category": "colour", "requested_weekday": "sunday",
         "is_first_colour_visit": False, "quoted_price_minor": 9000, "customer_age": 30},
        versions={"no_colour_on_sunday": 2},
    )
    v = evaluate(p, RULES, FREE, DELIVERABLE)
    assert v.check is GateCheck.BOOKING_CITES_RULE


def test_check_5_blocks_when_the_hold_is_gone():
    p = _proposal(
        ["colour_allowed"],
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_age": 30, "requested_weekday": "tuesday"},
    )
    gone = {"slot_exists": True, "booked": False, "held_by_thread": None, "hold_id": None}
    v = evaluate(p, RULES, gone, DELIVERABLE)
    assert v.check is GateCheck.SLOT_STILL_FREE
    assert v.kind is BlockKind.CONCLUSION


def test_check_5_blocks_when_another_thread_holds_the_slot():
    p = _proposal(
        ["colour_allowed"],
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_age": 30, "requested_weekday": "tuesday"},
    )
    other = {"slot_exists": True, "booked": False, "held_by_thread": "t9", "hold_id": "hold_0002"}
    v = evaluate(p, RULES, other, DELIVERABLE)
    assert v.check is GateCheck.SLOT_STILL_FREE


def test_check_6_blocks_an_escalation_with_no_time_left():
    p = _proposal(
        ["under_16_needs_guardian"],
        {"service_category": "colour", "customer_age": 14, "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "requested_weekday": "tuesday"},
        action=Action(type="escalate", escalation_reason="under_16"),
    )
    late = {"deliverable": False, "hours_left": 1.0, "why_not": "1.0h left, margin is 2h"}
    v = evaluate(p, RULES, FREE, late)
    assert v.check is GateCheck.ESCALATION_DELIVERABLE
    assert v.kind is BlockKind.CONCLUSION


def test_check_6_does_not_apply_to_a_booking():
    p = _proposal(
        ["colour_allowed"],
        {"service_category": "colour", "is_first_colour_visit": False,
         "quoted_price_minor": 9000, "customer_age": 30, "requested_weekday": "tuesday"},
    )
    late = {"deliverable": False, "hours_left": 1.0, "why_not": "no time"}
    assert evaluate(p, RULES, FREE, late).allowed is True


def test_checks_run_in_order_and_report_the_first_failure():
    # Both check 1 and check 5 would fail. Check 1 is reported.
    p = _proposal(["invented_rule"], {"service_category": "colour"})
    gone = {"slot_exists": True, "booked": False, "held_by_thread": None, "hold_id": None}
    assert evaluate(p, RULES, gone, DELIVERABLE).check is GateCheck.RULES_EXIST


def test_the_gate_gives_the_same_answer_twice():
    p = _proposal(["colour_allowed"], {"service_category": "cut"})
    assert evaluate(p, RULES, FREE, DELIVERABLE) == evaluate(p, RULES, FREE, DELIVERABLE)
