from wca.rules.schema import Comparison, Group, Rule, RuleSet
from wca.rules.specificity import is_more_specific, matching_rules, unknown_rules

COLOUR = Comparison(fact="service_category", op="eq", value="colour")
FIRST = Comparison(fact="is_first_colour_visit", op="is_true")


def _rule(rid, cond, priority=0, outcome_type="allow"):
    outcome = {"type": outcome_type, "reason": "x"}
    if outcome_type == "require_lead_time":
        outcome["hours"] = 1
    return Rule(
        id=rid, version=1, condition=cond,
        outcome=outcome, priority=priority, source_text="x",
    )


GENERAL = _rule("colour_allowed", COLOUR)
SPECIFIC = _rule("patch_test", Group(all=[COLOUR, FIRST]))


def test_a_superset_of_facts_is_more_specific():
    assert is_more_specific(SPECIFIC, GENERAL) is True
    assert is_more_specific(GENERAL, SPECIFIC) is False


def test_a_rule_is_not_more_specific_than_itself():
    assert is_more_specific(GENERAL, GENERAL) is False


def test_unrelated_fact_sets_are_neither_more_specific():
    other = _rule("sunday", Comparison(fact="requested_weekday", op="eq", value="sunday"))
    assert is_more_specific(other, GENERAL) is False
    assert is_more_specific(GENERAL, other) is False


def test_matching_rules_returns_only_true_ones():
    rs = RuleSet(rules=[GENERAL, SPECIFIC])
    facts = {"service_category": "colour", "is_first_colour_visit": True}
    assert {r.id for r in matching_rules(rs, facts)} == {"colour_allowed", "patch_test"}

    facts_general_only = {"service_category": "colour", "is_first_colour_visit": False}
    assert {r.id for r in matching_rules(rs, facts_general_only)} == {"colour_allowed"}


def test_unknown_rules_are_reported_separately():
    rs = RuleSet(rules=[GENERAL, SPECIFIC])
    facts = {"service_category": "colour"}
    assert {r.id for r in unknown_rules(rs, facts)} == {"patch_test"}
    assert {r.id for r in matching_rules(rs, facts)} == {"colour_allowed"}
