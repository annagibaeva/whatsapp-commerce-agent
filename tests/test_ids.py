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
    assert proposal_id(31) == "prop_0031"
