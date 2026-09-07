import pytest
from pydantic import ValidationError

from wca.rules.schema import Comparison, Group, Rule, RuleSet
from wca.rules.specificity import (
    check_decidable,
    is_more_specific,
    matching_rules,
    unknown_rules,
)

COLOUR = Comparison(fact="service_category", op="eq", value="colour")
FIRST = Comparison(fact="is_first_colour_visit", op="is_true")


def _rule(rid, cond, priority=0):
    return Rule(
        id=rid, version=1, condition=cond,
        outcome={"type": "allow", "reason": "x"}, priority=priority, source_text="x",
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


def test_two_rules_that_cannot_be_ranked_fail_to_load():
    a = _rule("a", Comparison(fact="x", op="eq", value=1))
    b = _rule("b", Comparison(fact="y", op="eq", value=2))
    with pytest.raises(ValueError, match="cannot decide"):
        check_decidable(RuleSet(rules=[a, b]))


def test_a_priority_difference_makes_a_tie_decidable():
    a = _rule("a", Comparison(fact="x", op="eq", value=1), priority=1)
    b = _rule("b", Comparison(fact="y", op="eq", value=2), priority=0)
    check_decidable(RuleSet(rules=[a, b]))


def test_the_real_ruleset_is_decidable():
    from wca.rules.store import load_ruleset

    check_decidable(load_ruleset("policy/salon.rules.json"))
