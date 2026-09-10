"""HoldLedger: the durable lease Phase 2's LiveCalendar and Task 6's
SqliteCalendar both build on. Two properties matter: a hold survives a
restart, and the cross-process race MockCalendar's own threading.Lock
cannot close (two writers, two connections) is closed by BEGIN IMMEDIATE
instead.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from wca.calendar.ledger import HoldLedger
from wca.calendar.mock import HOLD_TTL_SECONDS, HoldRefused, RefusalReason
from wca.clock import utc
from wca.db import connect, init_schema

NOW = utc(2026, 8, 21, 10)
SLOT = "s_2026_08_25_1400"


def test_a_hold_takes_the_slot_and_a_second_hold_is_refused(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    ledger = HoldLedger(conn)
    hold = ledger.create(SLOT, thread_id="t1", now=NOW)
    assert hold.slot_id == SLOT
    assert (hold.expires_at - hold.created_at).total_seconds() == HOLD_TTL_SECONDS
    with pytest.raises(HoldRefused) as err:
        ledger.create(SLOT, thread_id="t2", now=NOW)
    assert err.value.reason is RefusalReason.ALREADY_HELD


def test_release_frees_the_slot_and_is_safe_to_repeat(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    ledger = HoldLedger(conn)
    hold = ledger.create(SLOT, thread_id="t1", now=NOW)
    ledger.release(hold.hold_id, now=NOW)
    ledger.release(hold.hold_id, now=NOW)  # safe to repeat
    ledger.create(SLOT, thread_id="t2", now=NOW)  # slot is free again


def test_expire_due_releases_only_the_expired_hold(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    ledger = HoldLedger(conn)
    expiring = ledger.create(SLOT, thread_id="t1", now=NOW)
    later = NOW + timedelta(seconds=HOLD_TTL_SECONDS + 1)
    live = ledger.create("s_other", thread_id="t2", now=later)

    released = ledger.expire_due(later)

    assert [h.hold_id for h in released] == [expiring.hold_id]
    assert ledger.get(expiring.hold_id).released is True
    assert ledger.get(live.hold_id).released is False
    assert ledger.live_for_slot(SLOT, later) is None


def test_expire_due_releases_nothing_when_no_hold_has_expired(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    ledger = HoldLedger(conn)
    ledger.create(SLOT, thread_id="t1", now=NOW)
    assert ledger.expire_due(NOW) == []


def test_get_returns_none_for_an_unknown_hold(tmp_path):
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    ledger = HoldLedger(conn)
    assert ledger.get("no_such_hold") is None


# --- the property that matters: survives a restart ----------------------------

def test_a_hold_survives_a_restart_and_still_blocks_a_second_hold(tmp_path):
    db_path = tmp_path / "wca.db"
    conn1 = connect(db_path)
    init_schema(conn1)
    HoldLedger(conn1).create("s1", thread_id="t1", now=NOW)
    conn1.close()

    conn2 = connect(db_path)
    ledger2 = HoldLedger(conn2)
    assert ledger2.live_for_slot("s1", now=NOW) is not None
    with pytest.raises(HoldRefused) as err:
        ledger2.create("s1", thread_id="t2", now=NOW)
    assert err.value.reason is RefusalReason.ALREADY_HELD


# --- the property BEGIN IMMEDIATE, not a Python-level check, provides --------

def test_two_concurrent_creates_on_the_same_slot_only_one_wins(tmp_path):
    # Two threads racing HoldLedger.create on the same slot_id/now.
    # Proves the BEGIN IMMEDIATE transaction, not just the Python-level
    # check, is what prevents the double-hold -- a plain SELECT-then-
    # INSERT with no transaction would let both threads pass the SELECT.
    conn = connect(tmp_path / "wca.db")
    init_schema(conn)
    ledger = HoldLedger(conn)
    results = []

    def attempt(thread_id):
        try:
            results.append(("ok", ledger.create("s1", thread_id, now=NOW)))
        except HoldRefused as e:
            results.append(("refused", e.reason))

    threads = [threading.Thread(target=attempt, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r[0] == "ok") == 1
    assert sum(1 for r in results if r[0] == "refused") == 7
