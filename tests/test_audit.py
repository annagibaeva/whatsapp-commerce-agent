import json

import pytest

from wca.audit import AuditLog, SqliteAuditLog
from wca.clock import utc
from wca.db import connect, init_schema
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


def _sqlite_log(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    return SqliteAuditLog(conn)


@pytest.fixture(params=["memory", "sqlite"])
def log(request, tmp_path):
    if request.param == "memory":
        return AuditLog()
    return _sqlite_log(tmp_path)


def test_append_adds_a_record(log):
    assert log.append(_record()) is True
    assert len(log) == 1


def test_the_same_proposal_is_not_recorded_twice(log):
    log.append(_record())
    assert log.append(_record()) is False
    assert len(log) == 1


def test_records_returns_a_snapshot_that_does_not_change(log):
    log.append(_record("prop_0001"))
    snapshot = log.records()
    log.append(_record("prop_0002"))
    assert len(snapshot) == 1
    assert len(log) == 2


def test_every_record_names_the_ruleset_version_and_the_rules_in_english(log):
    log.append(_record())
    payload = json.loads(log.to_json())
    assert payload[0]["ruleset_version"] == "abc123def456"
    assert payload[0]["rules_english"] == ["service category is colour"]


# --- Fix 3b/3c: turn_cost_usd on the record, cost_for_thread across turns --

def test_a_record_with_no_turn_cost_set_defaults_to_none():
    """Optional field, default -- every existing AuditRecord(...) call in
    this codebase (wca.harness in particular, which never sets this)
    must keep constructing unchanged."""
    record = _record()
    assert record.turn_cost_usd is None


def test_add_turn_cost_stamps_every_record_appended_since_count_before(log):
    log.append(_record("prop_0001"))  # before this "turn" started
    before = len(log)
    log.append(_record("prop_0002"))
    log.append(_record("prop_0003"))

    log.add_turn_cost(before, 0.0042)

    records = log.records()
    assert records[0].turn_cost_usd is None  # untouched: appended before `before`
    assert records[1].turn_cost_usd == pytest.approx(0.0042)
    assert records[2].turn_cost_usd == pytest.approx(0.0042)


def test_add_turn_cost_over_an_empty_range_is_a_no_op(log):
    log.append(_record("prop_0001"))
    before = len(log)  # no records appended after this

    log.add_turn_cost(before, 0.01)  # must not raise

    assert log.records()[0].turn_cost_usd is None


def test_cost_for_thread_sums_turn_cost_across_every_record_for_that_thread(log):
    """A booking spans several turns -- an earlier request_booking the
    gate refused, then the one that succeeded, could both leave a record
    for the same thread_id. cost_for_thread must sum them, not read one."""
    log.append(_record("prop_0001"))  # thread_id "t1", see _record()
    log.add_turn_cost(0, 0.10)
    before = len(log)
    log.append(_record("prop_0002"))
    log.add_turn_cost(before, 0.25)

    assert log.cost_for_thread("t1") == pytest.approx(0.35)
    assert log.cost_for_thread("no_such_thread") == 0.0


def test_cost_for_thread_is_not_vacuous_against_a_single_record(log):
    """Guards against an implementation that reads only the first or
    last matching record instead of actually summing: with two records
    for the same thread, the total must differ from either record alone."""
    log.append(_record("prop_0001"))
    log.add_turn_cost(0, 0.10)
    before = len(log)
    log.append(_record("prop_0002"))
    log.add_turn_cost(before, 0.25)

    total = log.cost_for_thread("t1")
    assert total != pytest.approx(0.10)
    assert total != pytest.approx(0.25)


# --- SqliteAuditLog: the property that matters -------------------------------

def test_dedup_survives_reopening_the_connection(tmp_path):
    """Not a fresh in-memory set: a second SqliteAuditLog, built from
    nothing but the connection, already knows about a proposal_id a
    prior instance appended before the process (here, the connection)
    was closed. AuditLog._seen is exactly the bug this guards against --
    see the module docstring in wca/audit.py."""
    db_path = tmp_path / "wca.db"
    conn1 = connect(db_path)
    init_schema(conn1)
    log1 = SqliteAuditLog(conn1)
    record = _record(pid="prop_0001")
    assert log1.append(record) is True
    conn1.close()

    conn2 = connect(db_path)
    log2 = SqliteAuditLog(conn2)
    assert log2.append(record) is False          # not a fresh in-memory set
    assert len(log2.records()) == 1               # not double-written either
