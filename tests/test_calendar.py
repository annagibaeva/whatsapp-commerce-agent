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
