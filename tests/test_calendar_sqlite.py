from wca.calendar.mock import Slot
from wca.calendar.sqlite import SqliteCalendar
from wca.clock import utc
from wca.db import connect, init_schema

from calendar_contract import (
    NOW,
    OTHER_SLOT,
    OTHER_SLOT_STARTS_AT,
    SLOT,
    SLOT_STARTS_AT,
    run_contract_tests,
)


def test_agrees_with_mock_on_every_documented_behaviour(tmp_path):
    """Each call to `make` gets an entirely fresh db file, so one
    assertion's slot/hold/booking state can never leak into the next --
    the same isolation MockCalendar gets for free from a fresh instance."""
    counter = {"n": 0}

    def make() -> SqliteCalendar:
        counter["n"] += 1
        conn = connect(tmp_path / f"wca_{counter['n']}.db")
        init_schema(conn)
        return SqliteCalendar(
            conn, slots=(Slot(SLOT, SLOT_STARTS_AT), Slot(OTHER_SLOT, OTHER_SLOT_STARTS_AT))
        )

    run_contract_tests(make)


def test_a_committed_booking_survives_a_restart_and_stays_deduped(tmp_path):
    db_path = tmp_path / "wca.db"
    conn1 = connect(db_path)
    init_schema(conn1)
    cal1 = SqliteCalendar(conn1, slots=(Slot(SLOT, SLOT_STARTS_AT),))
    hold = cal1.hold(SLOT, thread_id="t1", now=NOW)
    cal1.commit(hold.hold_id, idempotency_key="k1", now=NOW)
    conn1.close()

    conn2 = connect(db_path)
    cal2 = SqliteCalendar(conn2, slots=(Slot(SLOT, SLOT_STARTS_AT),))
    assert len(cal2.bookings()) == 1
    # Re-committing the same key, on a fresh instance, must return the
    # same booking, not create a second one -- proves the idempotency
    # dedup is table-backed, not an in-memory set that reset with cal1.
    again = cal2.commit(hold.hold_id, idempotency_key="k1", now=NOW)
    assert again.booking_id == cal2.bookings()[0].booking_id
    assert len(cal2.bookings()) == 1


def test_mark_reminded_and_due_reminders_survive_a_restart(tmp_path):
    """Not in the shared contract (test_calendar.py never exercised these
    against a restart either), but the restart property matters here too
    -- n8n's reminder job must not re-send after a redeploy."""
    db_path = tmp_path / "wca.db"
    starts_soon = utc(2026, 8, 22, 8)  # ~22 hours after NOW
    conn1 = connect(db_path)
    init_schema(conn1)
    cal1 = SqliteCalendar(conn1, slots=(Slot(SLOT, starts_soon),))
    hold = cal1.hold(SLOT, thread_id="t1", now=NOW)
    booking = cal1.commit(hold.hold_id, idempotency_key="k1", now=NOW)
    assert booking in cal1.due_reminders(within_hours=24, now=NOW)
    cal1.mark_reminded(booking.booking_id, now=NOW)
    conn1.close()

    conn2 = connect(db_path)
    cal2 = SqliteCalendar(conn2, slots=(Slot(SLOT, starts_soon),))
    assert cal2.due_reminders(within_hours=24, now=NOW) == []
    # A redelivered "sent" call must not move the timestamp -- same
    # idempotency contract as MockCalendar.mark_reminded.
    again = cal2.mark_reminded(booking.booking_id, now=NOW)
    assert again is not None
    assert again.reminder_sent_at == cal2.bookings()[0].reminder_sent_at


def test_the_database_refuses_two_live_bookings_on_one_slot(tmp_path):
    """The check in `commit` reads the bookings table and then writes it.
    Two threads holding different idempotency keys for the same slot can
    both pass that read. `one_live_booking_per_slot` is what stops them,
    and it stops them in the database, where a race cannot get underneath
    it. Asserted against raw SQL rather than through `commit`, so it
    tests the constraint and not the check that sits above it.
    """
    import sqlite3

    import pytest

    conn = connect(tmp_path / "one_per_slot.db")
    init_schema(conn)
    insert = (
        "INSERT INTO bookings (booking_id, slot_id, thread_id, service_id, "
        "booked_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?)"
    )
    conn.execute(insert, ("bk_1", SLOT, "t1", "svc_cut", NOW.isoformat(), "key_one"))
    conn.commit()

    # Different booking id, different thread, different idempotency key --
    # every other constraint is satisfied. Only the slot is shared.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("bk_2", SLOT, "t2", "svc_cut", NOW.isoformat(), "key_two"))

    conn.rollback()
    # ...and cancelling the first frees the slot, because the index is
    # partial. Reschedule depends on this.
    conn.execute("UPDATE bookings SET cancelled_at = ? WHERE booking_id = ?", (NOW.isoformat(), "bk_1"))
    conn.execute(insert, ("bk_2", SLOT, "t2", "svc_cut", NOW.isoformat(), "key_two"))
    conn.commit()
    live = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND cancelled_at IS NULL", (SLOT,)
    ).fetchone()[0]
    assert live == 1
