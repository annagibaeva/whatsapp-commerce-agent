"""SqliteCalendar: full CalendarPort, durable, same contract as MockCalendar.

Holds go through `HoldLedger` (`wca.calendar.ledger`) -- this class never
touches the `holds` table itself. Bookings live in their own `bookings`
table (see `wca.db`), keyed by a `UNIQUE` `idempotency_key`, so `commit`
stays idempotent across a restart, not just within one process (design
spec exit criterion 5: "commit twice, book once"). `MockCalendar` stays
the test double the suite injects by default; this is what `serve` uses
once Task 8 wires it in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wca.calendar.ledger import HoldLedger
from wca.calendar.mock import Booking, HoldRefused, RefusalReason, Slot

_BOOKING_COLUMNS = (
    "booking_id, slot_id, thread_id, service_id, booked_at, "
    "idempotency_key, reminder_sent_at, cancelled_at"
)


def _row_to_booking(row: tuple) -> Booking:
    booking_id, slot_id, thread_id, service_id, booked_at, _key, reminder_sent_at, _cancelled = row
    return Booking(
        booking_id=booking_id,
        slot_id=slot_id,
        thread_id=thread_id,
        booked_at=datetime.fromisoformat(booked_at),
        service_id=service_id,
        reminder_sent_at=datetime.fromisoformat(reminder_sent_at) if reminder_sent_at else None,
    )


@dataclass
class SqliteCalendar:
    conn: sqlite3.Connection
    slots: tuple[Slot, ...] = ()
    _ledger: HoldLedger = field(init=False, repr=False)
    _slots_by_id: dict[str, Slot] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._ledger = HoldLedger(self.conn)
        self._slots_by_id = {s.slot_id: s for s in self.slots}

    # --- helpers, same shape as MockCalendar's -----------------------------

    def slot(self, slot_id: str) -> Slot | None:
        return self._slots_by_id.get(slot_id)

    def hours_until(self, slot_id: str, now: datetime) -> float | None:
        found = self.slot(slot_id)
        if found is None or found.starts_at is None:
            return None
        return (found.starts_at - now).total_seconds() / 3600

    def _booking_row_for_slot(self, slot_id: str) -> tuple | None:
        return self.conn.execute(
            f"SELECT {_BOOKING_COLUMNS} FROM bookings "
            "WHERE slot_id = ? AND cancelled_at IS NULL",
            (slot_id,),
        ).fetchone()

    def _booking_row_by_id(self, booking_id: str) -> tuple | None:
        return self.conn.execute(
            f"SELECT {_BOOKING_COLUMNS} FROM bookings WHERE booking_id = ?",
            (booking_id,),
        ).fetchone()

    def _booking_row_by_key(self, idempotency_key: str) -> tuple | None:
        return self.conn.execute(
            f"SELECT {_BOOKING_COLUMNS} FROM bookings WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    # --- CalendarPort --------------------------------------------------------

    def availability(self, now: datetime) -> tuple[str, ...]:
        return tuple(
            slot_id for slot_id in self._slots_by_id
            if self._ledger.live_for_slot(slot_id, now) is None
            and self._booking_row_for_slot(slot_id) is None
        )

    def hold(self, slot_id: str, thread_id: str, now: datetime) -> Any:
        if slot_id not in self._slots_by_id:
            raise HoldRefused(RefusalReason.NO_SUCH_SLOT, slot_id)
        if self._booking_row_for_slot(slot_id) is not None:
            raise HoldRefused(RefusalReason.ALREADY_BOOKED, slot_id)
        return self._ledger.create(slot_id, thread_id, now)

    def commit(
        self, hold_id: str, idempotency_key: str, now: datetime, service_id: str | None = None
    ) -> Booking:
        existing = self._booking_row_by_key(idempotency_key)
        if existing is not None:
            return _row_to_booking(existing)

        hold = self._ledger.get(hold_id)
        if hold is None or hold.released:
            raise HoldRefused(RefusalReason.NO_SUCH_HOLD, hold_id)
        if hold.expires_at <= now:
            raise HoldRefused(RefusalReason.HOLD_EXPIRED, hold_id)
        if self._booking_row_for_slot(hold.slot_id) is not None:
            raise HoldRefused(RefusalReason.ALREADY_BOOKED, hold.slot_id)

        booking_id = f"bk_{hold.hold_id}"
        try:
            self.conn.execute(
                "INSERT INTO bookings (booking_id, slot_id, thread_id, service_id, "
                "booked_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
                (booking_id, hold.slot_id, hold.thread_id, service_id, now.isoformat(), idempotency_key),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # Lost a race on idempotency_key between the SELECT above and
            # this INSERT -- the other writer's row is the answer now.
            self.conn.rollback()
            row = self._booking_row_by_key(idempotency_key)
            if row is not None:
                return _row_to_booking(row)
            raise

        self._ledger.release(hold.hold_id, now)
        return _row_to_booking(self._booking_row_by_id(booking_id))

    def mark_reminded(self, booking_id: str, now: datetime) -> Booking | None:
        row = self._booking_row_by_id(booking_id)
        if row is None:
            return None
        reminder_sent_at = row[6]
        if reminder_sent_at is None:
            self.conn.execute(
                "UPDATE bookings SET reminder_sent_at = ? WHERE booking_id = ?",
                (now.isoformat(), booking_id),
            )
            self.conn.commit()
        return _row_to_booking(self._booking_row_by_id(booking_id))

    def due_reminders(self, within_hours: float, now: datetime) -> list[Booking]:
        due: list[Booking] = []
        rows = self.conn.execute(
            f"SELECT {_BOOKING_COLUMNS} FROM bookings WHERE reminder_sent_at IS NULL"
        ).fetchall()
        for row in rows:
            booking = _row_to_booking(row)
            hours = self.hours_until(booking.slot_id, now)
            if hours is None:
                continue
            if hours <= within_hours:
                due.append(booking)
        return due

    def release(self, hold_id: str, reason: str, now: datetime) -> None:
        self._ledger.release(hold_id, now)

    def expire_due(self, now: datetime) -> list[Any]:
        return self._ledger.expire_due(now)

    def view(self, slot_id: str, now: datetime) -> dict[str, Any]:
        """A read-only snapshot for the gate. The gate never mutates."""
        hold = self._ledger.live_for_slot(slot_id, now)
        booking_row = self._booking_row_for_slot(slot_id)
        slot = self.slot(slot_id)
        return {
            "slot_exists": slot_id in self._slots_by_id,
            "booked": booking_row is not None,
            "held_by_thread": hold.thread_id if hold else None,
            "hold_id": hold.hold_id if hold else None,
            "starts_at": slot.starts_at if slot else None,
            "hours_until": self.hours_until(slot_id, now),
        }

    def bookings(self) -> tuple[Booking, ...]:
        rows = self.conn.execute(
            f"SELECT {_BOOKING_COLUMNS} FROM bookings WHERE cancelled_at IS NULL"
        ).fetchall()
        return tuple(_row_to_booking(row) for row in rows)
