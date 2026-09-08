"""The two things that must happen without an inbound message.

Both are plain functions that take `now` and their dependencies. Neither
calls `datetime.now()` itself -- the caller supplies the time, which is
what lets each be tested without waiting for a real clock to move.

The reaper. A slot takes one hold, and a hold has a TTL (see
`wca.calendar.mock`). If the process dies between taking a hold and
committing it, that slot is stuck until something calls
`calendar.expire_due(now)`. Nothing has ever called it. This module's
`reap_expired_holds` is that call, meant to run on a timer.

The watchdog. The spec says every escalation "records when the reply
window closes and is monitored against it". Nothing has ever monitored
it. An escalation raised at hour 2 becomes undeliverable at hour 22 in
silence -- a failure that never shows up in a demo thread run in one
sitting, because it never reaches hour 23. `check_escalations` is the
monitor: it logs loudly when an open escalation's window is close to the
margin.

`run_periodically` is the thin asyncio wrapper that turns either function
into a background timer, started from FastAPI's `lifespan`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable, Sequence

from wca.calendar.base import CalendarPort
from wca.clock import hours_between
from wca.escalation import ESCALATION_MARGIN_HOURS
from wca.models import EscalationTicket

#: How often each timer ticks, in seconds. Reasonable v0 defaults -- there
#: is no requirement pinning these to a specific number, only that both
#: run periodically rather than never.
DEFAULT_REAP_INTERVAL_SECONDS = 60.0
DEFAULT_WATCHDOG_INTERVAL_SECONDS = 300.0


def reap_expired_holds(now: datetime, calendar: CalendarPort) -> list[Any]:
    """Release every hold whose TTL has passed as of `now`.

    Thin on purpose: `calendar.expire_due(now)` already does the release
    and returns what it released. This wraps that call with the loud
    logging a background timer needs, and gives the CLI's `reap`
    subcommand and the tests one function to call.
    """
    released = calendar.expire_due(now)
    for hold in released:
        print(
            f"[reaper] released expired hold {hold.hold_id} "
            f"(slot={hold.slot_id}, thread={hold.thread_id})"
        )
    return released


class EscalationBook:
    """Every escalation raised and not yet known to be resolved.

    v0 holds these in memory, same as everything else in this codebase.
    Nothing ever removes a ticket -- there is no "resolved" signal to key
    off yet -- so the watchdog may flag the same open escalation more
    than once as its window keeps closing in. That is the correct
    failure mode: better to repeat a warning than to drop it.
    """

    def __init__(self) -> None:
        self._tickets: list[EscalationTicket] = []

    def add(self, ticket: EscalationTicket) -> None:
        self._tickets.append(ticket)

    def open(self) -> tuple[EscalationTicket, ...]:
        return tuple(self._tickets)


def check_escalations(
    now: datetime,
    tickets: Sequence[EscalationTicket],
    margin_hours: float = ESCALATION_MARGIN_HOURS,
) -> list[EscalationTicket]:
    """Flag every open escalation whose window is about to close.

    "Monitored" has to mean something a human notices, not a value
    nobody reads. Flagged tickets are logged here, loudly, and also
    returned so a caller (or a test) can check them without scraping
    stdout.
    """
    at_risk: list[EscalationTicket] = []
    for ticket in tickets:
        hours_left = hours_between(now, ticket.window_closes_at)
        if hours_left <= margin_hours:
            print(
                f"[watchdog] ESCALATION AT RISK: thread={ticket.thread_id} "
                f"reason={ticket.reason!r} hours_left={hours_left:.2f} "
                f"closes_at={ticket.window_closes_at.isoformat()}"
            )
            at_risk.append(ticket)
    return at_risk


async def run_periodically(fn: Callable[[], None], interval_seconds: float) -> None:
    """Call `fn()` every `interval_seconds`, forever, until cancelled.

    `fn` takes no arguments: the caller closes over `now` (read fresh
    each tick) and whatever dependencies it needs, since
    `reap_expired_holds` and `check_escalations` must not read the clock
    themselves. A tick that raises is logged and swallowed, so one bad
    tick does not take down the loop that runs both timers -- the
    reaper's failure should not silence the watchdog, or the reverse.
    """
    while True:
        try:
            fn()
        except Exception as err:  # noqa: BLE001
            print(f"[scheduler] tick failed: {err!r}")
        await asyncio.sleep(interval_seconds)
