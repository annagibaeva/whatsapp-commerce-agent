"""One sqlite file, one schema, one place both are defined.

Every durable store in this codebase (audit, conversation, dedup, the
hold ledger, escalations) opens its own connection to the same file via
`connect()` and calls `init_schema()` once at startup. `CREATE TABLE IF
NOT EXISTS` everywhere -- this file never drops or alters a table, so
running it against a db that already has data in it is always safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    proposal_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    thread_id TEXT PRIMARY KEY,
    last_inbound_at TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    hold_id TEXT
);
CREATE TABLE IF NOT EXISTS dedup_messages (
    message_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS holds (
    hold_id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    service_id TEXT,
    booked_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    reminder_sent_at TEXT,
    cancelled_at TEXT,
    UNIQUE (idempotency_key)
);
-- One live booking per slot, enforced by the database rather than by the
-- check-then-insert in SqliteCalendar.commit. That check reads the table
-- and then writes it; two threads holding different idempotency keys for
-- the same slot can both pass the read and both insert. Nothing above
-- this line stops them -- UNIQUE(idempotency_key) is a different claim.
-- "No double-booking" is the property this whole system is named for, so
-- it belongs where it cannot be raced. Partial, because a cancelled
-- booking must free its slot for the reschedule work still to come.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_booking_per_slot
    ON bookings (slot_id) WHERE cancelled_at IS NULL;
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    window_closes_at TEXT NOT NULL,
    template_name TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: this connection is shared by every durable
    # store `build_serve_app` wires (Task 8), and `run_job` executes on
    # whatever thread the webhook's background-task pool hands it -- not
    # necessarily the thread that opened the connection. Safe because the
    # SQLite library CPython ships is built with serialized threading
    # (SQLITE_THREADSAFE=1): it takes its own internal mutex around each
    # call, so concurrent use of one connection from multiple threads is
    # the library's problem to serialize, not ours -- see
    # `wca.calendar.ledger.HoldLedger`, whose own restart-and-concurrency
    # test drives exactly this (one connection, eight threads).
    #
    # busy_timeout: without it, a second writer that loses a `BEGIN
    # IMMEDIATE` race (see HoldLedger.create) gets an immediate
    # `sqlite3.OperationalError: database is locked` instead of blocking
    # until the first writer commits and then seeing the row it wrote --
    # the clean "ok" vs "refused" split every caller here depends on.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
