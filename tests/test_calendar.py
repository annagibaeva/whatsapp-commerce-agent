import threading
import time

import pytest

from wca.calendar.mock import HOLD_TTL_SECONDS, HoldRefused, MockCalendar, RefusalReason
from wca.clock import utc

NOW = utc(2026, 8, 21, 10)
SLOT = "s_2026_08_25_1400"


def _cal():
    return MockCalendar(slot_ids=[SLOT, "s_2026_08_25_1500"])


def test_a_hold_takes_the_slot_and_expires_later():
    cal = _cal()
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    assert hold.slot_id == SLOT
    assert (hold.expires_at - hold.created_at).total_seconds() == HOLD_TTL_SECONDS


def test_a_second_hold_on_the_same_slot_is_refused():
    cal = _cal()
    cal.hold(SLOT, thread_id="t1", now=NOW)
    with pytest.raises(HoldRefused) as err:
        cal.hold(SLOT, thread_id="t2", now=NOW)
    assert err.value.reason is RefusalReason.ALREADY_HELD


def test_an_unknown_slot_is_refused_differently_from_a_taken_one():
    cal = _cal()
    with pytest.raises(HoldRefused) as err:
        cal.hold("s_does_not_exist", thread_id="t1", now=NOW)
    assert err.value.reason is RefusalReason.NO_SUCH_SLOT


def test_expiry_frees_the_slot():
    from datetime import timedelta

    cal = _cal()
    cal.hold(SLOT, thread_id="t1", now=NOW)
    later = NOW + timedelta(seconds=HOLD_TTL_SECONDS + 1)
    released = cal.expire_due(now=later)
    assert len(released) == 1
    cal.hold(SLOT, thread_id="t2", now=later)


def test_expiry_does_not_touch_a_live_hold():
    cal = _cal()
    cal.hold(SLOT, thread_id="t1", now=NOW)
    assert cal.expire_due(now=NOW) == []


def test_commit_is_idempotent():
    cal = _cal()
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    first = cal.commit(hold.hold_id, idempotency_key="k1", now=NOW)
    second = cal.commit(hold.hold_id, idempotency_key="k1", now=NOW)
    assert first.booking_id == second.booking_id
    assert len(cal.bookings()) == 1


def test_release_frees_the_slot_and_is_safe_to_repeat():
    cal = _cal()
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    cal.release(hold.hold_id, reason="blocked", now=NOW)
    cal.release(hold.hold_id, reason="blocked", now=NOW)
    cal.hold(SLOT, thread_id="t2", now=NOW)


def test_view_reports_what_the_gate_needs():
    cal = _cal()
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    view = cal.view(SLOT, now=NOW)
    assert view["slot_exists"] is True
    assert view["held_by_thread"] == "t1"
    assert view["hold_id"] == hold.hold_id
    assert view["booked"] is False


def test_committing_an_expired_hold_is_refused():
    from datetime import timedelta

    cal = _cal()
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    later = NOW + timedelta(seconds=HOLD_TTL_SECONDS + 1)
    with pytest.raises(HoldRefused) as err:
        cal.commit(hold.hold_id, idempotency_key="k1", now=later)
    assert err.value.reason is RefusalReason.HOLD_EXPIRED


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
    hold_b = cal.hold("s_2026_08_25_1500", thread_id="tB", now=NOW)
    hold_b.slot_id = SLOT

    first = cal.commit(hold_a.hold_id, idempotency_key="k_a", now=NOW)
    with pytest.raises(HoldRefused) as err:
        cal.commit(hold_b.hold_id, idempotency_key="k_b", now=NOW)
    assert err.value.reason is RefusalReason.ALREADY_BOOKED
    assert len(cal.bookings()) == 1
    assert cal.bookings()[0].booking_id == first.booking_id


def test_same_idempotency_key_twice_still_returns_one_booking():
    cal = _cal()
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    first = cal.commit(hold.hold_id, idempotency_key="same-key", now=NOW)
    second = cal.commit(hold.hold_id, idempotency_key="same-key", now=NOW)
    assert first.booking_id == second.booking_id
    assert len(cal.bookings()) == 1
