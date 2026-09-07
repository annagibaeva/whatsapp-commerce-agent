"""An in-memory calendar.

Two things make this more than a dictionary.

A slot takes one hold. The calendar enforces that itself, so a caller
cannot forget to check.

A hold expires. If the process dies between taking a hold and committing
it, the slot is stuck until the reaper clears it. That is what the TTL is
for.
"""

from __future__ import annotations

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
    slot_ids: list[str]
    _holds: dict[str, Hold] = field(default_factory=dict)
    _bookings: dict[str, Booking] = field(default_factory=dict)
    _by_key: dict[str, str] = field(default_factory=dict)
    _counter: int = 0

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
        if idempotency_key in self._by_key:
            return self._bookings[self._by_key[idempotency_key]]

        hold = self._holds.get(hold_id)
        if hold is None or hold.released:
            raise HoldRefused(RefusalReason.NO_SUCH_HOLD, hold_id)
        if hold.expires_at <= now:
            raise HoldRefused(RefusalReason.HOLD_EXPIRED, hold_id)

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
        hold = self._holds.get(hold_id)
        if hold is not None:
            hold.released = True

    def expire_due(self, now: datetime) -> list[Hold]:
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
        return {
            "slot_exists": slot_id in self.slot_ids,
            "booked": booking is not None,
            "held_by_thread": hold.thread_id if hold else None,
            "hold_id": hold.hold_id if hold else None,
        }

    def bookings(self) -> tuple[Booking, ...]:
        return tuple(self._bookings.values())
