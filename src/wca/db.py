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
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
