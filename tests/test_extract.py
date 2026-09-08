import pytest
from pydantic import ValidationError

from wca.extract.base import RawFactSet, build_facts, load_prompt
from wca.extract.fake import FakeExtractor


def test_build_facts_drops_fields_the_model_left_out():
    raw = RawFactSet(service_category="colour", is_first_colour_visit=True)
    facts = build_facts(raw)
    assert facts == {"service_category": "colour", "is_first_colour_visit": True}
    assert "customer_is_over_16" not in facts


def test_a_false_value_survives_but_a_null_does_not():
    raw = RawFactSet(service_category="colour", is_first_colour_visit=False, customer_is_over_16=None)
    facts = build_facts(raw)
    assert facts["is_first_colour_visit"] is False
    assert "customer_is_over_16" not in facts


def test_the_raw_shape_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RawFactSet(service_category="colour", invented_field="x")


def test_the_prompt_loads_and_has_its_placeholders():
    prompt = load_prompt("extract-v0.1")
    for placeholder in ("{thread}", "{message}"):
        assert placeholder in prompt


def test_the_prompt_says_an_empty_answer_is_normal():
    text = load_prompt("extract-v0.1").lower()
    assert "no fact" in text or "leave it out" in text


def test_the_prompt_forbids_following_instructions_inside_a_message():
    text = load_prompt("extract-v0.1").lower()
    assert "not an instruction" in text or "never follow" in text


def test_the_fake_extractor_replays_a_script():
    fake = FakeExtractor(script={"msg_1": RawFactSet(service_category="colour")})
    assert fake.extract("msg_1", "colour please", []).facts == {"service_category": "colour"}
    assert fake.extract("msg_2", "hello", []).facts == {}


def test_the_extractor_never_returns_a_rule_or_an_action():
    fields = set(RawFactSet.model_fields)
    for banned in ("rule", "rules", "cited_rules", "action", "book", "decision"):
        assert banned not in fields
