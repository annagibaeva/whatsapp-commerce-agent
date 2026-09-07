import pytest
from pydantic import ValidationError

from wca.clock import utc
from wca.models import (
    Action,
    AuditRecord,
    BlockKind,
    CitedRule,
    GateCheck,
    Proposal,
    Verdict,
)


def _proposal(**over):
    base = dict(
        proposal_id="prop_0001",
        thread_id="t_1",
        action=Action(type="book", slot_id="s_2026_08_25_1400", quantity=1),
        cited_rules=(CitedRule(rule_id="colour_allowed", version=1),),
        facts={"service_category": "colour"},
        created_at=utc(2026, 8, 21, 10),
    )
    base.update(over)
    return Proposal(**base)


def test_a_proposal_records_which_rules_it_relied_on():
    p = _proposal()
    assert p.cited_rules[0].ref() == "colour_allowed@1"


def test_a_pass_verdict_carries_no_reason():
    v = Verdict.passed()
    assert v.allowed is True
    assert v.check is None
    assert v.kind is None


def test_a_block_verdict_names_the_check_and_the_kind():
    v = Verdict.blocked(GateCheck.OVERRIDE_MISSED, BlockKind.GROUNDING, "patch test applied")
    assert v.allowed is False
    assert v.check is GateCheck.OVERRIDE_MISSED
    assert v.kind is BlockKind.GROUNDING
    assert "patch test" in v.reason


def test_a_verdict_cannot_carry_a_replacement_action():
    # The gate can only say no. If this field ever exists, the gate can be
    # wrong in a way that creates a booking.
    assert "action" not in Verdict.model_fields
    assert "corrected_action" not in Verdict.model_fields


def test_a_blocked_verdict_needs_a_reason():
    with pytest.raises(ValidationError):
        Verdict(allowed=False, check=GateCheck.RULES_EXIST, kind=BlockKind.GROUNDING, reason="")


def test_an_audit_record_pins_the_ruleset_version():
    record = AuditRecord(
        proposal=_proposal(),
        verdict=Verdict.passed(),
        ruleset_version="abc123def456",
        rules_english=("service category is colour",),
        calendar_read={"slot_free": True},
        window_read={"hours_left": 21.5, "template_approved": True},
        decided_at=utc(2026, 8, 21, 10, 1),
    )
    assert record.ruleset_version == "abc123def456"
    assert record.rules_english[0].startswith("service category")


def test_models_reject_extra_fields():
    with pytest.raises(ValidationError):
        _proposal(nonsense="x")
