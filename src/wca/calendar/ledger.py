"""A durable lease on a slot -- who holds it, and until when.

This is the piece Phase 2's `LiveCalendar` and `SqliteCalendar` (Task 6)
both build on. It owns only the lease -- never a booking. That split
matters for Phase 2: a live calendar's bookings live in the external
calendar's own data, not here.

`create`'s check-then-write is wrapped in `BEGIN IMMEDIATE`, not guarded
by a Python `threading.Lock` the way `MockCalendar` guards itself. A
`Lock` only closes the race within one process; two `wca serve` workers
(or two threads on separate connections to the same db file) racing the
same slot would both still pass an unguarded `SELECT` before either
`INSERT`s. `BEGIN IMMEDIATE` takes SQLite's RESERVED lock up front, so
the second writer's transaction blocks (see `wca.db.connect`'s
`busy_timeout`) until the first commits, then sees the row that was
just written and refuses cleanly instead of racing it.

Each write (`create`, `expire_due`) opens its own short-lived connection
to the same db file rather than reusing the connection passed to
`__init__`. This is not an optimisation -- it is required. A single
`sqlite3.Connection` can only have one transaction open at a time, full
stop; two threads sharing one connection and both issuing `BEGIN
IMMEDIATE` would race each other into "cannot start a transaction within
a transaction", a Python/SQLite-API limitation, not the double-hold this
module exists to prevent. Real cross-request concurrency in this
codebase is exactly this shape -- `wca.conversation.queue.ThreadQueue`
lets two different threads' jobs run at once against the same shared
calendar object (see `tests/test_conversation.py`'s
`test_two_different_threads_jobs_can_overlap_in_time`) -- so this had to
be handled, not assumed away. Separate connections to the same file is
what makes SQLite's own file-level locking (not any Python-level state)
the thing that actually serializes two writers, which is the property
`test_two_concurrent_creates_on_the_same_slot_only_one_wins` proves.
Reads (`get`, `live_for_slot`) stay on the shared connection -- a bare
`SELECT` never opens an explicit transaction, so there is nothing here
for two readers to collide over.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from wca.calendar.mock import HOLD_TTL_SECONDS, HoldRefused, RefusalReason


@dataclass
class Hold:  # same shape as calendar/mock.py's Hold
    hold_id: str
    slot_id: str
    thread_id: str
    created_at: datetime
    expires_at: datetime
    released: bool = False


def _row_to_hold(row: tuple) -> Hold:
    hold_id, slot_id, thread_id, created_at, expires_at, released = row
    return Hold(
        hold_id=hold_id,
        slot_id=slot_id,
        thread_id=thread_id,
        created_at=datetime.fromisoformat(created_at),
        expires_at=datetime.fromisoformat(expires_at),
        released=bool(released),
    )


def _db_path(conn: sqlite3.Connection) -> str:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file
    raise RuntimeError("could not determine the database file for this connection")


class HoldLedger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._path = _db_path(conn)

    def _write_connection(self) -> sqlite3.Connection:
        # A fresh connection per write -- see the module docstring for why
        # this is not a plain reuse of self._conn.
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def create(self, slot_id: str, thread_id: str, now: datetime) -> Hold:
        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT hold_id FROM holds WHERE slot_id = ? AND released = 0 AND expires_at > ?",
                (slot_id, now.isoformat()),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                raise HoldRefused(RefusalReason.ALREADY_HELD, slot_id)

            hold_id = f"hold_{uuid.uuid4().hex[:12]}"
            expires_at = now + timedelta(seconds=HOLD_TTL_SECONDS)
            conn.execute(
                "INSERT INTO holds (hold_id, slot_id, thread_id, created_at, expires_at, released) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (hold_id, slot_id, thread_id, now.isoformat(), expires_at.isoformat()),
            )
            conn.commit()
        except HoldRefused:
            raise
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return Hold(
            hold_id=hold_id, slot_id=slot_id, thread_id=thread_id,
            created_at=now, expires_at=expires_at, released=False,
        )

    def live_for_slot(self, slot_id: str, now: datetime) -> Hold | None:
        row = self._conn.execute(
            "SELECT hold_id, slot_id, thread_id, created_at, expires_at, released "
            "FROM holds WHERE slot_id = ? AND released = 0 AND expires_at > ? "
            "ORDER BY created_at LIMIT 1",
            (slot_id, now.isoformat()),
        ).fetchone()
        return _row_to_hold(row) if row is not None else None

    def get(self, hold_id: str) -> Hold | None:
        row = self._conn.execute(
            "SELECT hold_id, slot_id, thread_id, created_at, expires_at, released "
            "FROM holds WHERE hold_id = ?",
            (hold_id,),
        ).fetchone()
        return _row_to_hold(row) if row is not None else None

    def release(self, hold_id: str, now: datetime) -> None:
        # A fresh connection, same reasoning as create()/expire_due(): this
        # must not collide with a concurrent write elsewhere on the shared
        # self._conn's own transaction state.
        conn = self._write_connection()
        try:
            conn.execute("UPDATE holds SET released = 1 WHERE hold_id = ?", (hold_id,))
            conn.commit()
        finally:
            conn.close()

    def expire_due(self, now: datetime) -> list[Hold]:
        conn = self._write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT hold_id, slot_id, thread_id, created_at, expires_at, released "
                "FROM holds WHERE released = 0 AND expires_at <= ?",
                (now.isoformat(),),
            ).fetchall()
            expired = [_row_to_hold(row) for row in rows]
            for hold in expired:
                conn.execute(
                    "UPDATE holds SET released = 1 WHERE hold_id = ?", (hold.hold_id,)
                )
                hold.released = True
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return expired
