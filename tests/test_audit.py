import json

from wca.audit import AuditLog
from wca.clock import utc
from wca.models import Action, AuditRecord, CitedRule, Proposal, Verdict


def _record(pid="prop_0001"):
    return AuditRecord(
        proposal=Proposal(
            proposal_id=pid, thread_id="t1",
            action=Action(type="book", slot_id="s1"),
            cited_rules=(CitedRule(rule_id="colour_allowed", version=1),),
            facts={"service_category": "colour"}, created_at=utc(2026, 8, 21, 10),
        ),
        verdict=Verdict.passed(),
        ruleset_version="abc123def456",
        rules_english=("service category is colour",),
        calendar_read={"slot_exists": True},
        window_read={"hours_left": 22.0},
        decided_at=utc(2026, 8, 21, 10, 1),
    )


def test_append_adds_a_record():
    log = AuditLog()
    assert log.append(_record()) is True
    assert len(log) == 1


def test_the_same_proposal_is_not_recorded_twice():
    log = AuditLog()
    log.append(_record())
    assert log.append(_record()) is False
    assert len(log) == 1


def test_records_returns_a_snapshot_that_does_not_change():
    log = AuditLog()
    log.append(_record("prop_0001"))
    snapshot = log.records()
    log.append(_record("prop_0002"))
    assert len(snapshot) == 1
    assert len(log) == 2


def test_every_record_names_the_ruleset_version_and_the_rules_in_english():
    log = AuditLog()
    log.append(_record())
    payload = json.loads(log.to_json())
    assert payload[0]["ruleset_version"] == "abc123def456"
    assert payload[0]["rules_english"] == ["service category is colour"]
