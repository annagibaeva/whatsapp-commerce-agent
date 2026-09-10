import threading
import time

from wca.clock import utc
from wca.conversation.dedup import DedupStore
from wca.conversation.queue import ThreadQueue
from wca.conversation.state import ConversationState, ConversationStore, SqliteConversationStore
from wca.db import connect, init_schema


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
    state.add_facts({"customer_is_over_16": False}, now=utc(2026, 8, 21, 9))
    state.add_facts({"customer_is_over_16": True}, now=utc(2026, 8, 21, 10))
    assert state.facts["customer_is_over_16"] is True


def test_last_inbound_moves_with_each_message_and_drives_the_window():
    state = ConversationState(thread_id="t1", last_inbound_at=utc(2026, 8, 21, 9))
    state.add_facts({}, now=utc(2026, 8, 21, 15))
    assert state.last_inbound_at == utc(2026, 8, 21, 15)


# --- ConversationStore.save: a no-op-shaped extra call ------------------------

def test_in_memory_store_save_is_a_no_op_that_does_not_raise():
    """`ConversationStore.save` exists purely so a call site that does not
    know whether it holds the in-memory or the durable store can call
    `save` unconditionally. The in-memory store's own object identity
    already makes it a no-op -- this just proves it does not raise and
    the state is still there afterwards."""
    store = ConversationStore()
    state = store.get_or_create("t1", now=utc(2026, 8, 21, 9))
    state.add_facts({"service_category": "colour"}, now=utc(2026, 8, 21, 9))
    store.save(state)  # must not raise
    assert store.get("t1").facts == {"service_category": "colour"}


# --- SqliteConversationStore: the property that matters ------------------------

def test_facts_survive_reopening_the_connection(tmp_path):
    db_path = tmp_path / "wca.db"
    conn1 = connect(db_path)
    init_schema(conn1)
    store1 = SqliteConversationStore(conn1)
    state = store1.get_or_create("t1", now=utc(2026, 9, 9, 10))
    state.add_facts({"service_category": "colour"}, now=utc(2026, 9, 9, 10, 5))
    store1.save(state)
    conn1.close()

    conn2 = connect(db_path)
    store2 = SqliteConversationStore(conn2)
    reloaded = store2.get("t1")
    assert reloaded is not None
    assert reloaded.facts == {"service_category": "colour"}
    assert reloaded.last_inbound_at == utc(2026, 9, 9, 10, 5)


def test_sqlite_store_get_returns_none_for_an_unknown_thread(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    store = SqliteConversationStore(conn)
    assert store.get("no_such_thread") is None


def test_sqlite_store_get_or_create_persists_a_fresh_thread_immediately(tmp_path):
    """get_or_create writes the new row itself -- a caller that never
    calls save() on a brand-new thread (e.g. one that adds no facts this
    turn) must still see it survive a restart."""
    db_path = tmp_path / "wca.db"
    conn1 = connect(db_path)
    init_schema(conn1)
    SqliteConversationStore(conn1).get_or_create("t1", now=utc(2026, 9, 9, 10))
    conn1.close()

    conn2 = connect(db_path)
    reloaded = SqliteConversationStore(conn2).get("t1")
    assert reloaded is not None
    assert reloaded.facts == {}


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


def test_a_failing_job_does_not_stop_the_rest_of_its_thread_via_drain_thread():
    queue = ThreadQueue()
    seen = []

    def boom():
        raise RuntimeError("boom")

    queue.submit("t1", boom)
    queue.submit("t1", lambda: seen.append("after"))
    errors = queue.drain_thread("t1")
    assert seen == ["after"]
    assert len(errors) == 1


def test_two_different_threads_jobs_can_overlap_in_time():
    """Two callers draining different thread_ids must not block each
    other. Prove it with recorded enter/exit events, not wall-clock
    timing: both jobs must have started before either one finishes."""
    queue = ThreadQueue()
    events: list[str] = []
    lock = threading.Lock()

    def record(tag):
        with lock:
            events.append(tag)

    def make_job(tag):
        def job():
            record(f"{tag}-enter")
            time.sleep(0.05)
            record(f"{tag}-exit")
        return job

    queue.submit("t1", make_job("t1"))
    queue.submit("t2", make_job("t2"))

    barrier = threading.Barrier(2)

    def run(thread_id):
        barrier.wait()
        queue.drain_thread(thread_id)

    a = threading.Thread(target=run, args=("t1",))
    b = threading.Thread(target=run, args=("t2",))
    a.start()
    b.start()
    a.join()
    b.join()

    # Both jobs must have entered before either exited -- that is what
    # "do not block each other" means in terms of recorded events.
    first_exit_index = min(events.index("t1-exit"), events.index("t2-exit"))
    entered_before_any_exit = events[:first_exit_index]
    assert "t1-enter" in entered_before_any_exit
    assert "t2-enter" in entered_before_any_exit


def test_two_jobs_on_the_same_thread_never_overlap():
    """Two callers both trying to drain the SAME thread_id at once must
    still run that thread's jobs one at a time. Prove it with recorded
    enter/exit events: the second job's enter never appears before the
    first job's exit."""
    queue = ThreadQueue()
    events: list[str] = []
    lock = threading.Lock()

    def record(tag):
        with lock:
            events.append(tag)

    def make_job(tag):
        def job():
            record(f"{tag}-enter")
            time.sleep(0.05)
            record(f"{tag}-exit")
        return job

    queue.submit("t1", make_job("j1"))
    queue.submit("t1", make_job("j2"))

    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        queue.drain_thread("t1")

    a = threading.Thread(target=run)
    b = threading.Thread(target=run)
    a.start()
    b.start()
    a.join()
    b.join()

    assert events == ["j1-enter", "j1-exit", "j2-enter", "j2-exit"]


def test_drain_still_drains_every_thread():
    queue = ThreadQueue()
    seen: list[str] = []
    queue.submit("t1", lambda: seen.append("t1"))
    queue.submit("t2", lambda: seen.append("t2"))
    errors = queue.drain()
    assert errors == []
    assert set(seen) == {"t1", "t2"}
