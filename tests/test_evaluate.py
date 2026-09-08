import pytest

from wca.rules.evaluate import Tri, evaluate, evaluate_rule, missing_facts
from wca.rules.schema import Comparison, Group, Rule

COLOUR = Comparison(fact="service_category", op="eq", value="colour")
FIRST = Comparison(fact="is_first_colour_visit", op="is_true")


def test_a_missing_fact_is_unknown_not_false():
    assert evaluate(COLOUR, {}) is Tri.UNKNOWN
    assert evaluate(COLOUR, {"service_category": "cut"}) is Tri.FALSE


def test_all_is_false_if_any_part_is_false():
    facts = {"service_category": "cut"}
    assert evaluate(Group(all=[COLOUR, FIRST]), facts) is Tri.FALSE


def test_all_is_unknown_if_a_part_is_unknown_and_none_are_false():
    facts = {"service_category": "colour"}
    assert evaluate(Group(all=[COLOUR, FIRST]), facts) is Tri.UNKNOWN


def test_all_is_true_only_when_every_part_is_true():
    facts = {"service_category": "colour", "is_first_colour_visit": True}
    assert evaluate(Group(all=[COLOUR, FIRST]), facts) is Tri.TRUE


def test_any_is_true_if_a_part_is_true_even_with_unknowns():
    facts = {"service_category": "colour"}
    assert evaluate(Group(any=[COLOUR, FIRST]), facts) is Tri.TRUE


def test_any_is_unknown_if_no_part_is_true_and_one_is_unknown():
    facts = {"service_category": "cut"}
    assert evaluate(Group(any=[COLOUR, FIRST]), facts) is Tri.UNKNOWN


def test_not_leaves_unknown_alone():
    assert evaluate(Group(**{"not": COLOUR}), {}) is Tri.UNKNOWN
    assert evaluate(Group(**{"not": COLOUR}), {"service_category": "colour"}) is Tri.FALSE
    assert evaluate(Group(**{"not": COLOUR}), {"service_category": "cut"}) is Tri.TRUE


@pytest.mark.parametrize(
    "op,value,fact_value,expected",
    [
        ("eq", 5, 5, Tri.TRUE),
        ("ne", 5, 6, Tri.TRUE),
        ("lt", 16, 15, Tri.TRUE),
        ("lte", 16, 16, Tri.TRUE),
        ("gt", 10, 11, Tri.TRUE),
        ("gte", 10, 10, Tri.TRUE),
        ("in", ["a", "b"], "a", Tri.TRUE),
        ("is_true", None, True, Tri.TRUE),
        ("is_false", None, False, Tri.TRUE),
    ],
)
def test_every_operator_works(op, value, fact_value, expected):
    cond = Comparison(fact="f", op=op, value=value)
    assert evaluate(cond, {"f": fact_value}) is expected


def test_a_type_mismatch_is_unknown_not_a_crash():
    cond = Comparison(fact="hours_until_appointment", op="lt", value=16)
    assert evaluate(cond, {"hours_until_appointment": "sixteen"}) is Tri.UNKNOWN


def test_an_explicit_none_fact_is_unknown():
    assert evaluate(COLOUR, {"service_category": None}) is Tri.UNKNOWN


def test_missing_facts_names_what_the_rule_still_needs():
    rule = Rule(
        id="patch", version=1, condition=Group(all=[COLOUR, FIRST]),
        outcome={"type": "require_lead_time", "hours": 48, "reason": "patch test"},
        source_text="x",
    )
    assert missing_facts(rule, {"service_category": "colour"}) == ("is_first_colour_visit",)
    assert missing_facts(rule, {"service_category": "colour", "is_first_colour_visit": True}) == ()
    assert evaluate_rule(rule, {"service_category": "colour"}) is Tri.UNKNOWN
