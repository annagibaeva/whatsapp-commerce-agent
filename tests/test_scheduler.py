"""The two timers, tested without waiting for a real clock to move.

Both `reap_expired_holds` and `check_escalations` take `now` from the
caller. Every test here builds its own fixed `now` values and moves them
by hand, the same pattern `wca.clock.SimulatedClock` uses elsewhere in
this codebase -- no `time.sleep`, no real clock, no flake.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from wca.calendar.mock import HOLD_TTL_SECONDS, MockCalendar, Slot
from wca.clock import utc
from wca.escalation import ESCALATION_MARGIN_HOURS
from wca.models import EscalationTicket
from wca.scheduler import check_escalations, reap_expired_holds, run_periodically

NOW = utc(2026, 8, 21, 10)
SLOT = "s_2026_08_25_1400"
LIVE_SLOT = "s_2026_08_25_1500"


def _calendar() -> MockCalendar:
    return MockCalendar(slots=[
        Slot(SLOT, utc(2026, 8, 25, 14)),
        Slot(LIVE_SLOT, utc(2026, 8, 25, 15)),
    ])


# --- the reaper --------------------------------------------------------------

def test_the_reaper_releases_an_expired_hold_and_leaves_a_live_one_alone():
    calendar = _calendar()
    expiring = calendar.hold(SLOT, thread_id="t1", now=NOW)

    later = NOW + timedelta(seconds=HOLD_TTL_SECONDS + 1)
    # Taken out just before the reaper runs, so at `later` it is still
    # well inside its own TTL -- unlike `expiring`, held back at NOW.
    live = calendar.hold(LIVE_SLOT, thread_id="t2", now=later)

    released = reap_expired_holds(later, calendar)

    assert [h.hold_id for h in released] == [expiring.hold_id]
    # The live hold's slot is still held, not freed by the same pass.
    assert LIVE_SLOT not in calendar.availability(later)
    assert live.released is False


def test_the_reaper_releases_nothing_when_no_hold_has_expired():
    calendar = _calendar()
    calendar.hold(SLOT, thread_id="t1", now=NOW)

    released = reap_expired_holds(NOW, calendar)

    assert released == []


def test_the_reaper_logs_what_it_released(capsys):
    calendar = _calendar()
    hold = calendar.hold(SLOT, thread_id="t1", now=NOW)
    later = NOW + timedelta(seconds=HOLD_TTL_SECONDS + 1)

    reap_expired_holds(later, calendar)

    logged = capsys.readouterr().out
    assert hold.hold_id in logged
    assert SLOT in logged


# --- the escalation watchdog --------------------------------------------------

def _ticket(*, raised_at, closes_at, thread_id="t1", reason="patch_test_required") -> EscalationTicket:
    return EscalationTicket(
        thread_id=thread_id, reason=reason, raised_at=raised_at,
        window_closes_at=closes_at, template_name="patch_test_notice",
    )


def test_the_watchdog_flags_an_escalation_inside_the_margin():
    closes_at = NOW + timedelta(hours=ESCALATION_MARGIN_HOURS - 0.5)
    ticket = _ticket(raised_at=NOW - timedelta(hours=1), closes_at=closes_at)

    flagged = check_escalations(NOW, [ticket])

    assert flagged == [ticket]


def test_the_watchdog_does_not_flag_one_safely_outside_the_margin():
    closes_at = NOW + timedelta(hours=ESCALATION_MARGIN_HOURS + 5)
    ticket = _ticket(raised_at=NOW - timedelta(hours=1), closes_at=closes_at)

    flagged = check_escalations(NOW, [ticket])

    assert flagged == []


def test_the_watchdog_checks_each_ticket_independently():
    at_risk = _ticket(
        raised_at=NOW, closes_at=NOW + timedelta(hours=1), thread_id="t_risk"
    )
    safe = _ticket(
        raised_at=NOW, closes_at=NOW + timedelta(hours=10), thread_id="t_safe"
    )

    flagged = check_escalations(NOW, [at_risk, safe])

    assert [t.thread_id for t in flagged] == ["t_risk"]


def test_the_watchdog_logs_loudly_when_it_flags_one(capsys):
    ticket = _ticket(raised_at=NOW, closes_at=NOW + timedelta(hours=1))

    check_escalations(NOW, [ticket])

    logged = capsys.readouterr().out
    assert "t1" in logged
    assert "ESCALATION" in logged.upper()


def test_the_watchdog_prints_nothing_for_a_safe_ticket(capsys):
    ticket = _ticket(raised_at=NOW, closes_at=NOW + timedelta(hours=20))

    check_escalations(NOW, [ticket])

    assert capsys.readouterr().out == ""


# --- run_periodically ---------------------------------------------------------

def test_run_periodically_calls_the_function_repeatedly_until_cancelled():
    calls: list[int] = []

    async def drive() -> None:
        task = asyncio.create_task(run_periodically(lambda: calls.append(1), 0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())

    assert len(calls) >= 2


def test_run_periodically_survives_a_failing_tick():
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    async def drive() -> None:
        task = asyncio.create_task(run_periodically(flaky, 0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())

    # The first tick raised. The loop kept going anyway.
    assert len(calls) >= 2
