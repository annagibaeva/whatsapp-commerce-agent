import threading
import time

import pytest

from wca.calendar.mock import HoldRefused, MockCalendar, RefusalReason, Slot

from calendar_contract import (
    NOW,
    OTHER_SLOT,
    OTHER_SLOT_STARTS_AT,
    SLOT,
    SLOT_STARTS_AT,
    run_contract_tests,
)


def _cal():
    return MockCalendar(slots=[Slot(SLOT, SLOT_STARTS_AT), Slot(OTHER_SLOT, OTHER_SLOT_STARTS_AT)])


def test_mock_calendar_agrees_with_the_shared_calendar_contract():
    run_contract_tests(_cal)


# --- MockCalendar-specific: white-box concurrency tests -----------------------
# Not part of the shared contract (tests/calendar_contract.py) -- both of
# these reach into MockCalendar's own internals (monkeypatching a private
# method, mutating a Hold's slot_id directly after creation) in a way
# that has no sqlite equivalent.

def test_two_threads_racing_for_one_hold_produce_one_hold_and_one_refusal():
    """Widen the check-then-write window inside hold() with a small sleep,
    so the race lands every run instead of depending on luck. This is the
    exact window the lock in hold() closes."""
    cal = _cal()
    original_live_hold_for = MockCalendar._live_hold_for

    def slow_live_hold_for(self, slot_id, now):
        result = original_live_hold_for(self, slot_id, now)
        time.sleep(0.02)
        return result

    MockCalendar._live_hold_for = slow_live_hold_for
    try:
        barrier = threading.Barrier(2)
        holds: dict[str, object] = {}
        refusals: dict[str, RefusalReason] = {}

        def take(name, thread_id):
            barrier.wait()
            try:
                holds[name] = cal.hold(SLOT, thread_id=thread_id, now=NOW)
            except HoldRefused as err:
                refusals[name] = err.reason

        t1 = threading.Thread(target=take, args=("A", "tA"))
        t2 = threading.Thread(target=take, args=("B", "tB"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        MockCalendar._live_hold_for = original_live_hold_for

    assert len(holds) == 1
    assert len(refusals) == 1
    assert next(iter(refusals.values())) is RefusalReason.ALREADY_HELD


def test_two_live_holds_cannot_both_commit():
    """hold()'s lock should stop two live holds from existing on one slot
    in the first place. Test commit()'s recheck on its own by planting two
    live holds on the same slot directly (bypassing hold()), the way a
    weaker lock or a future bug could still let through. commit() must
    still refuse the second one instead of double booking."""
    cal = _cal()
    hold_a = cal.hold(SLOT, thread_id="tA", now=NOW)
    # Insert a second, independent live hold on the same slot without
    # going through hold() at all -- this is exactly what a mistake
    # elsewhere in the system could produce, and it's what commit() must
    # defend against on its own.
    hold_b = cal.hold(OTHER_SLOT, thread_id="tB", now=NOW)
    hold_b.slot_id = SLOT

    first = cal.commit(hold_a.hold_id, idempotency_key="k_a", now=NOW)
    with pytest.raises(HoldRefused) as err:
        cal.commit(hold_b.hold_id, idempotency_key="k_b", now=NOW)
    assert err.value.reason is RefusalReason.ALREADY_BOOKED
    assert len(cal.bookings()) == 1
    assert cal.bookings()[0].booking_id == first.booking_id
