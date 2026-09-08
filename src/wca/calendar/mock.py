"""An in-memory calendar.

Two things make this more than a dictionary.

A slot takes one hold. The calendar enforces that itself, so a caller
cannot forget to check.

A hold expires. If the process dies between taking a hold and committing
it, the slot is stuck until the reaper clears it. That is what the TTL is
for.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from wca.ids import hold_id as make_hold_id

HOLD_TTL_SECONDS = 600


class RefusalReason(StrEnum):
    NO_SUCH_SLOT = "no_such_slot"
    ALREADY_HELD = "already_held"
    ALREADY_BOOKED = "already_booked"
    NO_SUCH_HOLD = "no_such_hold"
    HOLD_EXPIRED = "hold_expired"


class HoldRefused(Exception):
    """Raised instead of double-booking.

    Carries the reason, because a caller that cannot tell 'taken' from
    'does not exist' cannot reply sensibly to the customer.
    """

    def __init__(self, reason: RefusalReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class Slot:
    """A bookable slot with a real start time.

    `hours_until_appointment` used to arrive as a fact from the model.
    That let the thing the gate exists to check supply its own evidence.
    A slot with a real `starts_at` lets code compute the number instead.
    `starts_at` must be timezone-aware, same as every other time in this
    codebase.
    """

    slot_id: str
    starts_at: datetime | None = None


@dataclass
class Hold:
    hold_id: str
    slot_id: str
    thread_id: str
    created_at: datetime
    expires_at: datetime
    released: bool = False


@dataclass
class Booking:
    booking_id: str
    slot_id: str
    thread_id: str
    booked_at: datetime


@dataclass
class MockCalendar:
    #: Bare ids, kept for the existing call site in harness.py that has
    #: not moved to real Slot objects yet. Prefer `slots` for anything new.
    slot_ids: list[str] = field(default_factory=list)
    #: Slots with a real start time. A slot named only in `slot_ids` gets
    #: a synthetic entry with `starts_at=None`, so `hours_until` for it
    #: is `None` rather than a guess.
    slots: tuple[Slot, ...] = ()
    _holds: dict[str, Hold] = field(default_factory=dict)
    _bookings: dict[str, Booking] = field(default_factory=dict)
    _by_key: dict[str, str] = field(default_factory=dict)
    _counter: int = 0
    _slots_by_id: dict[str, Slot] = field(default_factory=dict, init=False, repr=False)

    # Guards hold/commit/release/expire_due. Without it, two threads can
    # both read "no live hold" before either writes one, and both end up
    # holding the same slot. None of the guarded methods call another
    # guarded method, so a plain Lock (not RLock) is enough.
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        by_id: dict[str, Slot] = {s.slot_id: s for s in self.slots}
        for slot_id in self.slot_ids:
            by_id.setdefault(slot_id, Slot(slot_id=slot_id, starts_at=None))
        self._slots_by_id = by_id
        # slot_ids is read everywhere below as "every id this calendar
        # knows about". Normalize it to that once, here, so nothing else
        # in this class has to care which constructor argument a slot
        # came in on.
        self.slot_ids = list(by_id.keys())

    def slot(self, slot_id: str) -> Slot | None:
        return self._slots_by_id.get(slot_id)

    def hours_until(self, slot_id: str, now: datetime) -> float | None:
        """Hours from `now` to the slot's start. `None` if unknown.

        Not clamped. A slot in the past comes back negative, on purpose:
        a caller that cannot tell "two hours away" from "yesterday"
        cannot refuse a booking in the past.
        """
        found = self.slot(slot_id)
        if found is None or found.starts_at is None:
            return None
        return (found.starts_at - now).total_seconds() / 3600

    def _live_hold_for(self, slot_id: str, now: datetime) -> Hold | None:
        for hold in self._holds.values():
            if hold.slot_id == slot_id and not hold.released and hold.expires_at > now:
                return hold
        return None

    def _booking_for(self, slot_id: str) -> Booking | None:
        for booking in self._bookings.values():
            if booking.slot_id == slot_id:
                return booking
        return None

    def availability(self, now: datetime) -> tuple[str, ...]:
        return tuple(
            s for s in self.slot_ids
            if self._live_hold_for(s, now) is None and self._booking_for(s) is None
        )

    def hold(self, slot_id: str, thread_id: str, now: datetime) -> Hold:
        # Locked so the "is it free" check and the "write the hold" write
        # happen as one step. Without the lock, two threads can both see
        # no live hold and both write one.
        with self._lock:
            if slot_id not in self.slot_ids:
                raise HoldRefused(RefusalReason.NO_SUCH_SLOT, slot_id)
            if self._booking_for(slot_id) is not None:
                raise HoldRefused(RefusalReason.ALREADY_BOOKED, slot_id)
            if self._live_hold_for(slot_id, now) is not None:
                raise HoldRefused(RefusalReason.ALREADY_HELD, slot_id)

            self._counter += 1
            hold = Hold(
                hold_id=make_hold_id(self._counter),
                slot_id=slot_id,
                thread_id=thread_id,
                created_at=now,
                expires_at=now + timedelta(seconds=HOLD_TTL_SECONDS),
            )
            self._holds[hold.hold_id] = hold
            return hold

    def commit(self, hold_id: str, idempotency_key: str, now: datetime) -> Booking:
        # Locked for the same reason as hold(): check-then-write must not
        # be split across two threads.
        with self._lock:
            if idempotency_key in self._by_key:
                return self._bookings[self._by_key[idempotency_key]]

            hold = self._holds.get(hold_id)
            if hold is None or hold.released:
                raise HoldRefused(RefusalReason.NO_SUCH_HOLD, hold_id)
            if hold.expires_at <= now:
                raise HoldRefused(RefusalReason.HOLD_EXPIRED, hold_id)
            # Re-check for a booking made under a different hold on this
            # slot. Two holds can both be live (e.g. one just expired and
            # was replaced) only if something upstream already went
            # wrong, but this is the last line of defense: one booking
            # per slot, no matter how many live holds point at it.
            if self._booking_for(hold.slot_id) is not None:
                raise HoldRefused(RefusalReason.ALREADY_BOOKED, hold.slot_id)

            booking = Booking(
                booking_id=f"bk_{hold.hold_id}",
                slot_id=hold.slot_id,
                thread_id=hold.thread_id,
                booked_at=now,
            )
            self._bookings[booking.booking_id] = booking
            self._by_key[idempotency_key] = booking.booking_id
            hold.released = True
            return booking

    def release(self, hold_id: str, reason: str, now: datetime) -> None:
        with self._lock:
            hold = self._holds.get(hold_id)
            if hold is not None:
                hold.released = True

    def expire_due(self, now: datetime) -> list[Hold]:
        with self._lock:
            expired = [
                h for h in self._holds.values()
                if not h.released and h.expires_at <= now
            ]
            for hold in expired:
                hold.released = True
            return expired

    def view(self, slot_id: str, now: datetime) -> dict[str, Any]:
        """A read-only snapshot for the gate. The gate never mutates."""
        hold = self._live_hold_for(slot_id, now)
        booking = self._booking_for(slot_id)
        slot = self.slot(slot_id)
        return {
            "slot_exists": slot_id in self.slot_ids,
            "booked": booking is not None,
            "held_by_thread": hold.thread_id if hold else None,
            "hold_id": hold.hold_id if hold else None,
            "starts_at": slot.starts_at if slot else None,
            "hours_until": self.hours_until(slot_id, now),
        }

    def bookings(self) -> tuple[Booking, ...]:
        return tuple(self._bookings.values())
