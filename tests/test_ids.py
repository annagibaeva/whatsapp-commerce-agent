from wca.clock import utc
from wca.ids import hold_id, idempotency_key, proposal_id


def test_idempotency_key_ignores_dict_ordering():
    a = idempotency_key("msg_1", "commit", {"slot": "s1", "thread": "t1"})
    b = idempotency_key("msg_1", "commit", {"thread": "t1", "slot": "s1"})
    assert a == b


def test_idempotency_key_changes_with_each_input():
    base = idempotency_key("msg_1", "commit", {"slot": "s1"})
    assert idempotency_key("msg_2", "commit", {"slot": "s1"}) != base
    assert idempotency_key("msg_1", "release", {"slot": "s1"}) != base
    assert idempotency_key("msg_1", "commit", {"slot": "s2"}) != base


def test_id_formatters():
    assert hold_id(7) == "hold_0007"
    now = utc(2026, 8, 21, 10)
    pid = proposal_id("t1", now, 31)
    prefix, scope, counter = pid.split("_")
    assert prefix == "prop"
    assert len(scope) == 12
    assert counter == "0031"


def test_proposal_id_is_stable_for_the_same_scope_and_counter():
    now = utc(2026, 8, 21, 10)
    assert proposal_id("t1", now, 31) == proposal_id("t1", now, 31)


def test_proposal_id_differs_across_threads_for_the_same_counter():
    now = utc(2026, 8, 21, 10)
    assert proposal_id("t1", now, 1) != proposal_id("t2", now, 1)


def test_proposal_id_differs_across_turns_for_the_same_thread_and_counter():
    from datetime import timedelta
    t1 = utc(2026, 8, 21, 10)
    t2 = t1 + timedelta(hours=1)
    assert proposal_id("t1", t1, 1) != proposal_id("t1", t2, 1)
