import pytest
from pydantic import ValidationError

from wca.rules.schema import Comparison, Group, Rule, RuleSet, facts_used


def _rule(**over):
    base = dict(
        id="colour_allowed",
        version=1,
        condition=Comparison(fact="service_category", op="eq", value="colour"),
        outcome={"type": "allow", "reason": "colour is offered"},
        priority=0,
        source_text="We offer colour.",
    )
    base.update(over)
    return Rule(**base)


def test_facts_used_walks_a_nested_condition():
    cond = Group(all=[
        Comparison(fact="service_category", op="eq", value="colour"),
        Group(any=[
            Comparison(fact="is_first_colour_visit", op="is_true"),
            Comparison(fact="months_since_last_visit", op="gt", value=12),
        ]),
    ])
    assert facts_used(cond) == frozenset(
        {"service_category", "is_first_colour_visit", "months_since_last_visit"}
    )


def test_requires_facts_is_derived_when_absent():
    rule = _rule()
    assert rule.requires_facts == ("service_category",)


def test_hand_written_requires_facts_must_match_the_condition():
    with pytest.raises(ValidationError, match="requires_facts"):
        _rule(requires_facts=["service_category", "not_in_the_condition"])


def test_unknown_operator_is_rejected():
    with pytest.raises(ValidationError):
        Comparison(fact="x", op="regex_match", value="y")


def test_unknown_outcome_type_is_rejected():
    with pytest.raises(ValidationError):
        _rule(outcome={"type": "send_flowers", "reason": "no"})


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        _rule(colour="blue")


def test_ruleset_version_is_stable_and_content_addressed():
    a = RuleSet(rules=[_rule()])
    b = RuleSet(rules=[_rule()])
    c = RuleSet(rules=[_rule(version=2)])
    assert a.ruleset_version == b.ruleset_version
    assert a.ruleset_version != c.ruleset_version


def test_ruleset_get_finds_by_id_and_version():
    rs = RuleSet(rules=[_rule()])
    assert rs.get("colour_allowed", 1) is not None
    assert rs.get("colour_allowed", 2) is None
    assert rs.get("nope", 1) is None


def test_duplicate_id_and_version_is_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        RuleSet(rules=[_rule(), _rule()])
