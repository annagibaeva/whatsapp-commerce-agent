from wca.clock import utc
from wca.conversation.dedup import DedupStore
from wca.conversation.queue import ThreadQueue
from wca.conversation.state import ConversationState, ConversationStore


def test_dedup_reports_a_repeat():
    store = DedupStore()
    assert store.seen("m1") is False
    store.remember("m1")
    assert store.seen("m1") is True
    assert store.seen("m2") is False


def test_dedup_remember_is_safe_to_repeat():
    store = DedupStore()
    store.remember("m1")
    store.remember("m1")
    assert len(store) == 1


def test_state_accumulates_facts_across_messages():
    store = ConversationStore()
    state = store.get_or_create("t1", now=utc(2026, 8, 21, 9))
    state.add_facts({"service_category": "colour"}, now=utc(2026, 8, 21, 9))
    state.add_facts({"is_first_colour_visit": True}, now=utc(2026, 8, 21, 10))
    assert state.facts == {"service_category": "colour", "is_first_colour_visit": True}


def test_a_later_fact_replaces_an_earlier_one():
    state = ConversationState(thread_id="t1", last_inbound_at=utc(2026, 8, 21, 9))
    state.add_facts({"customer_age": 30}, now=utc(2026, 8, 21, 9))
    state.add_facts({"customer_age": 31}, now=utc(2026, 8, 21, 10))
    assert state.facts["customer_age"] == 31


def test_last_inbound_moves_with_each_message_and_drives_the_window():
    state = ConversationState(thread_id="t1", last_inbound_at=utc(2026, 8, 21, 9))
    state.add_facts({}, now=utc(2026, 8, 21, 15))
    assert state.last_inbound_at == utc(2026, 8, 21, 15)


def test_the_queue_runs_one_thread_in_order():
    queue = ThreadQueue()
    seen = []
    for n in range(5):
        queue.submit("t1", lambda n=n: seen.append(n))
    queue.drain()
    assert seen == [0, 1, 2, 3, 4]


def test_two_threads_do_not_interleave_within_themselves():
    queue = ThreadQueue()
    order: list[str] = []
    for n in range(3):
        queue.submit("t1", lambda n=n: order.append(f"a{n}"))
        queue.submit("t2", lambda n=n: order.append(f"b{n}"))
    queue.drain()
    assert [x for x in order if x.startswith("a")] == ["a0", "a1", "a2"]
    assert [x for x in order if x.startswith("b")] == ["b0", "b1", "b2"]


def test_a_failing_job_does_not_stop_the_rest_of_the_thread():
    queue = ThreadQueue()
    seen = []

    def boom():
        raise RuntimeError("boom")

    queue.submit("t1", boom)
    queue.submit("t1", lambda: seen.append("after"))
    errors = queue.drain()
    assert seen == ["after"]
    assert len(errors) == 1
