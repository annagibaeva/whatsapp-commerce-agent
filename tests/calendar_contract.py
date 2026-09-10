"""Shared behavioural contract every CalendarPort implementation must
satisfy.

Written once here and called from test_calendar.py (against
MockCalendar) and test_calendar_sqlite.py (against SqliteCalendar) so the
two implementations are proven to agree on every documented behaviour,
not just the restart-specific ones -- see
docs/superpowers/plans/2026-09-09-wca-v1.md Phase 1 Task 6.

Two of MockCalendar's original tests are deliberately NOT here:
`test_two_threads_racing_for_one_hold_produce_one_hold_and_one_refusal`
(monkeypatches `MockCalendar._live_hold_for`, a white-box hook that does
not exist on SqliteCalendar) and `test_two_live_holds_cannot_both_commit`
(plants a second live hold by mutating a `Hold.slot_id` directly,
bypassing `hold()` entirely -- an in-process object hack with no sqlite
equivalent). Both stay as MockCalendar-only tests in test_calendar.py.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Callable

import pytest

from wca.calendar.base import CalendarPort
from wca.calendar.mock import HOLD_TTL_SECONDS, HoldRefused, RefusalReason
from wca.clock import utc

NOW = utc(2026, 8, 21, 10)
SLOT = "s_2026_08_25_1400"
SLOT_STARTS_AT = utc(2026, 8, 25, 14)
OTHER_SLOT = "s_2026_08_25_1500"
OTHER_SLOT_STARTS_AT = utc(2026, 8, 25, 15)


def run_contract_tests(make_calendar: Callable[[], CalendarPort]) -> None:
    """Call every assertion test_calendar.py's original functions made,
    against whatever `make_calendar()` returns.

    Each assertion gets its own fresh calendar via a new `make_calendar()`
    call -- callers must not share state between assertions, since one
    calendar's slot/hold/booking state would otherwise leak into the
    next assertion. `make_calendar` must build a calendar seeded with
    `Slot(SLOT, SLOT_STARTS_AT)` and `Slot(OTHER_SLOT, OTHER_SLOT_STARTS_AT)`.
    """
    _hold_takes_the_slot_and_expires_later(make_calendar())
    _a_second_hold_on_the_same_slot_is_refused(make_calendar())
    _an_unknown_slot_is_refused_differently_from_a_taken_one(make_calendar())
    _expiry_frees_the_slot(make_calendar())
    _expiry_does_not_touch_a_live_hold(make_calendar())
    _commit_is_idempotent(make_calendar())
    _release_frees_the_slot_and_is_safe_to_repeat(make_calendar())
    _view_reports_what_the_gate_needs(make_calendar())
    _view_reports_the_slots_start_time_and_hours_until(make_calendar())
    _hours_until_is_positive_for_a_future_slot(make_calendar())
    _hours_until_is_negative_for_a_past_slot_not_clamped_to_zero(make_calendar())
    _hours_until_is_none_for_an_unknown_slot(make_calendar())
    _slot_finds_and_misses(make_calendar())
    _committing_an_expired_hold_is_refused(make_calendar())
    _same_idempotency_key_twice_still_returns_one_booking(make_calendar())


def _hold_takes_the_slot_and_expires_later(cal: CalendarPort) -> None:
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    assert hold.slot_id == SLOT
    assert (hold.expires_at - hold.created_at).total_seconds() == HOLD_TTL_SECONDS


def _a_second_hold_on_the_same_slot_is_refused(cal: CalendarPort) -> None:
    cal.hold(SLOT, thread_id="t1", now=NOW)
    with pytest.raises(HoldRefused) as err:
        cal.hold(SLOT, thread_id="t2", now=NOW)
    assert err.value.reason is RefusalReason.ALREADY_HELD


def _an_unknown_slot_is_refused_differently_from_a_taken_one(cal: CalendarPort) -> None:
    with pytest.raises(HoldRefused) as err:
        cal.hold("s_does_not_exist", thread_id="t1", now=NOW)
    assert err.value.reason is RefusalReason.NO_SUCH_SLOT


def _expiry_frees_the_slot(cal: CalendarPort) -> None:
    cal.hold(SLOT, thread_id="t1", now=NOW)
    later = NOW + timedelta(seconds=HOLD_TTL_SECONDS + 1)
    released = cal.expire_due(now=later)
    assert len(released) == 1
    cal.hold(SLOT, thread_id="t2", now=later)


def _expiry_does_not_touch_a_live_hold(cal: CalendarPort) -> None:
    cal.hold(SLOT, thread_id="t1", now=NOW)
    assert cal.expire_due(now=NOW) == []


def _commit_is_idempotent(cal: CalendarPort) -> None:
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    first = cal.commit(hold.hold_id, idempotency_key="k1", now=NOW)
    second = cal.commit(hold.hold_id, idempotency_key="k1", now=NOW)
    assert first.booking_id == second.booking_id
    assert len(cal.bookings()) == 1


def _release_frees_the_slot_and_is_safe_to_repeat(cal: CalendarPort) -> None:
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    cal.release(hold.hold_id, reason="blocked", now=NOW)
    cal.release(hold.hold_id, reason="blocked", now=NOW)
    cal.hold(SLOT, thread_id="t2", now=NOW)


def _view_reports_what_the_gate_needs(cal: CalendarPort) -> None:
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    view = cal.view(SLOT, now=NOW)
    assert view["slot_exists"] is True
    assert view["held_by_thread"] == "t1"
    assert view["hold_id"] == hold.hold_id
    assert view["booked"] is False


def _view_reports_the_slots_start_time_and_hours_until(cal: CalendarPort) -> None:
    view = cal.view(SLOT, now=NOW)
    assert view["starts_at"] == SLOT_STARTS_AT
    assert view["hours_until"] == pytest.approx(100.0)


def _hours_until_is_positive_for_a_future_slot(cal: CalendarPort) -> None:
    assert cal.hours_until(SLOT, now=NOW) == pytest.approx(100.0)


def _hours_until_is_negative_for_a_past_slot_not_clamped_to_zero(cal: CalendarPort) -> None:
    after = SLOT_STARTS_AT + timedelta(hours=2)
    assert cal.hours_until(SLOT, now=after) == pytest.approx(-2.0)


def _hours_until_is_none_for_an_unknown_slot(cal: CalendarPort) -> None:
    assert cal.hours_until("s_does_not_exist", now=NOW) is None


def _slot_finds_and_misses(cal: CalendarPort) -> None:
    found = cal.slot(SLOT)
    assert found is not None
    assert found.starts_at == SLOT_STARTS_AT
    assert cal.slot("s_does_not_exist") is None


def _committing_an_expired_hold_is_refused(cal: CalendarPort) -> None:
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    later = NOW + timedelta(seconds=HOLD_TTL_SECONDS + 1)
    with pytest.raises(HoldRefused) as err:
        cal.commit(hold.hold_id, idempotency_key="k1", now=later)
    assert err.value.reason is RefusalReason.HOLD_EXPIRED


def _same_idempotency_key_twice_still_returns_one_booking(cal: CalendarPort) -> None:
    hold = cal.hold(SLOT, thread_id="t1", now=NOW)
    first = cal.commit(hold.hold_id, idempotency_key="same-key", now=NOW)
    second = cal.commit(hold.hold_id, idempotency_key="same-key", now=NOW)
    assert first.booking_id == second.booking_id
    assert len(cal.bookings()) == 1
