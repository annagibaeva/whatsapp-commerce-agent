from wca.clock import utc
from wca.propose import propose
from wca.rules.store import load_ruleset

RULES = load_ruleset("policy/salon.rules.json")
NOW = utc(2026, 8, 21, 10)
FULL = {
    "service_category": "colour", "is_first_colour_visit": False,
    "quoted_price_minor": 9000, "customer_is_over_16": True, "requested_weekday": "tuesday",
}


def test_a_complete_clean_case_proposes_a_booking():
    p = propose("t1", FULL, RULES, slot_id="s1", now=NOW, counter=1)
    assert p.action.type == "book"
    assert "colour_allowed" in {c.rule_id for c in p.cited_rules}


def test_a_missing_relevant_fact_produces_a_question_not_a_booking():
    facts = dict(FULL)
    del facts["is_first_colour_visit"]
    p = propose("t1", facts, RULES, slot_id="s1", now=NOW, counter=1)
    assert p.action.type == "ask"
    assert "is_first_colour_visit" in p.action.question


def test_a_rule_requiring_escalation_produces_an_escalation():
    facts = dict(FULL, customer_is_over_16=False)
    p = propose("t1", facts, RULES, slot_id="s1", now=NOW, counter=1)
    assert p.action.type == "escalate"
    assert p.action.escalation_reason == "under_16_needs_guardian"


def test_a_denying_rule_produces_a_decline():
    facts = dict(FULL, requested_weekday="sunday")
    p = propose("t1", facts, RULES, slot_id="s1", now=NOW, counter=1)
    assert p.action.type == "decline"


def test_the_proposal_cites_every_rule_that_is_true():
    facts = dict(FULL, quoted_price_minor=20000)
    p = propose("t1", facts, RULES, slot_id="s1", now=NOW, counter=1)
    cited = {c.rule_id for c in p.cited_rules}
    assert "colour_allowed" in cited
    assert "deposit_over_threshold" in cited
