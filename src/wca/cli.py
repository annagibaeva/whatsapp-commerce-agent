"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wca.calendar.mock import MockCalendar, Slot
from wca.cases import load_cases
from wca.catalogue import load_catalogue
from wca.clock import utc
from wca.escalation import Template, TemplateRegistry
from wca.harness import run_cases
from wca.rules.render import to_english
from wca.rules.store import load_ruleset

DEFAULT_RULES = Path("policy/salon.rules.json")
DEFAULT_CATALOGUE = Path("policy/salon.catalogue.json")
DEFAULT_CASES = Path("cases/v0.cases.json")

#: Fixed, not `datetime.now()` -- a demo run today and a demo run next
#: week offer the same slots. There is no real calendar behind v0 (see
#: `wca.calendar.mock`), so a live conversation needs something to check
#: availability against and hold.
DEMO_CALENDAR_START = utc(2026, 9, 8, 9)

REGISTRY = TemplateRegistry(templates=(
    Template(reason="under_16_needs_guardian", name="guardian_notice", approved=True),
    Template(reason="patch_test_first_colour", name="patch_test_notice", approved=True),
))


def _demo_slots(
    start: datetime, days: int = 3, hours: tuple[int, ...] = (9, 11, 14, 16)
) -> tuple[Slot, ...]:
    """A few days of bookable slots with real start times, from `start`."""
    slots: list[Slot] = []
    for day_offset in range(days):
        day = start + timedelta(days=day_offset)
        for hour in hours:
            starts_at = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            slot_id = f"s_{starts_at.strftime('%Y%m%d_%H%M')}"
            slots.append(Slot(slot_id=slot_id, starts_at=starts_at))
    return tuple(slots)


def _demo_calendar() -> MockCalendar:
    return MockCalendar(slots=_demo_slots(DEMO_CALENDAR_START))


def _project_root() -> Path:
    # src/wca/cli.py -> parents[0]=wca, [1]=src, [2]=repo root
    return Path(__file__).resolve().parents[2]


def _load_dotenv() -> Path:
    """Load the repo-root .env so cwd and stale process env cannot win.

    python-dotenv's default load_dotenv() searches from the process cwd and
    does not override variables already present in os.environ. Either failure
    mode leaves WHATSAPP_APP_SECRET wrong while the file on disk looks fine,
    and every Meta-signed POST then returns 403.
    """
    from dotenv import load_dotenv

    path = _project_root() / ".env"
    load_dotenv(path, override=True)
    return path


def cmd_cases(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    rules = load_ruleset(args.rules)
    report = run_cases(cases, rules, REGISTRY, gate_on=not args.gate_off)
    print(report.render())
    if report.bad_bookings:
        print(f"\n{report.bad_bookings} bad booking(s) got through.")
        return 1
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    ruleset = load_ruleset(args.rules)
    print(f"ruleset {ruleset.ruleset_version}, {len(ruleset.rules)} rules\n")
    for rule in ruleset.rules:
        print(f"  {rule.ref()}")
        print(f"    when   {to_english(rule.condition)}")
        print(f"    then   {rule.outcome.type}: {rule.outcome.reason}")
        print(f"    from   {rule.source_text}")
        print()
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    from wca.transport.whatsapp import WhatsAppTransport

    _load_dotenv()
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not phone_number_id or not token:
        print("WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN must be set in .env")
        return 1
    WhatsAppTransport(phone_number_id, token).send_text(args.to, args.text)
    print(f"sent to {args.to}")
    return 0


def build_serve_app(
    *,
    settings: Any,
    ruleset: Any,
    catalogue: Any,
    calendar: Any,
    client: Any,
    transport: Any,
    registry: TemplateRegistry = REGISTRY,
    audit: Any = None,
    conversations: Any = None,
    dedup: Any = None,
    queue: Any = None,
    escalations: Any = None,
    enable_scheduler: bool = True,
    reap_interval_seconds: float | None = None,
    watchdog_interval_seconds: float | None = None,
) -> Any:
    """Wire the pipeline to the webhook. The composition root's real work.

    Every collaborator comes in as a parameter rather than being built
    here, so `cmd_serve` supplies the live ones (a real Anthropic
    client, `WhatsAppTransport`, a calendar seeded with demo slots) and
    a test can supply fakes (a stubbed client, `FakeTransport`, an
    in-memory calendar) and drive the exact same wiring with no network
    and no real model call.

    The request path stays fast: `on_message` only appends a job to
    `queue`, which is a dict lookup and a deque append. Everything that
    can be slow -- the dedup check, building `ConversationState`,
    running one `Agent` turn, sending the reply -- happens in `run_job`,
    which only ever runs from `after_enqueue`, scheduled by
    `create_app` as a `BackgroundTasks` job that starts after the
    response has already gone out (see `wca.transport.webhook`).

    Dedup lives inside `run_job`, not inside `on_message`. Several
    retries for the same `message_id` can have their `on_message` calls
    interleave -- there is nothing serializing the request path across
    connections -- but their jobs for one `thread_id` cannot: they run
    one at a time, under `queue`'s own per-thread lock (see
    `wca.conversation.queue`). Checking there, not at the edge, is what
    keeps a lucky interleaving of two retries from both passing the
    check before either is recorded.
    """
    from wca.agent import Agent
    from wca.audit import AuditLog
    from wca.conversation.dedup import DedupStore
    from wca.conversation.queue import ThreadQueue
    from wca.conversation.state import ConversationStore
    from wca.models import EscalationTicket
    from wca.scheduler import (
        DEFAULT_REAP_INTERVAL_SECONDS,
        DEFAULT_WATCHDOG_INTERVAL_SECONDS,
        EscalationBook,
        check_escalations,
        reap_expired_holds,
        run_periodically,
    )
    from wca.tools import ToolContext
    from wca.transport.webhook import create_app

    audit = audit if audit is not None else AuditLog()
    conversations = conversations if conversations is not None else ConversationStore()
    dedup = dedup if dedup is not None else DedupStore()
    queue = queue if queue is not None else ThreadQueue()
    escalations = escalations if escalations is not None else EscalationBook()
    reap_interval = reap_interval_seconds or DEFAULT_REAP_INTERVAL_SECONDS
    watchdog_interval = watchdog_interval_seconds or DEFAULT_WATCHDOG_INTERVAL_SECONDS

    def run_job(message: Any) -> None:
        # 1. Dedup, inside the job -- see the docstring above for why.
        if dedup.seen(message.message_id):
            return
        dedup.remember(message.message_id)

        # 2. Get or create the conversation, move last_inbound_at.
        now = datetime.now(timezone.utc)
        state = conversations.get_or_create(message.thread_id, now=now)
        state.add_facts({}, now=now)

        # 3. One Agent turn with the message text and the tool context.
        ctx = ToolContext(
            ruleset=ruleset, catalogue=catalogue, calendar=calendar,
            registry=registry, audit=audit, conversation=state, now=now,
        )
        agent = Agent(client=client, tool_context=ctx)
        before = len(audit)
        reply = agent.run_turn([{"role": "user", "content": message.text}])

        # Anything this turn escalated is now something the watchdog
        # needs to know about. Read it off the audit trail rather than
        # threading a callback into wca.tools.escalate, which this task
        # does not touch.
        for record in audit.records()[before:]:
            if record.verdict.allowed and record.proposal.action.type == "escalate":
                escalations.add(EscalationTicket(
                    thread_id=record.proposal.thread_id,
                    reason=record.proposal.action.escalation_reason or "",
                    raised_at=record.decided_at,
                    window_closes_at=datetime.fromisoformat(
                        record.window_read["window_closes_at"]
                    ),
                    template_name=record.window_read.get("template_name") or "",
                ))

        # 4. Send the reply.
        if reply:
            transport.send_text(message.thread_id, reply)

    def on_message(message: Any) -> None:
        # Fast: append to the deque and return. Nothing here can block
        # on a model call.
        queue.submit(message.thread_id, lambda: run_job(message))

    def after_enqueue(message: Any) -> None:
        # Scheduled as a BackgroundTask by create_app, so it runs after
        # the response is already on the wire. Drains one thread only
        # (drain_thread, not drain), so a slow conversation never
        # blocks another, and two messages on the same thread can never
        # interleave.
        errors = queue.drain_thread(message.thread_id)
        for err in errors:
            print(f"[serve] job failed for thread {message.thread_id}: {err!r}")

    lifespan = None
    if enable_scheduler:
        @asynccontextmanager
        async def lifespan(app):  # noqa: ANN001
            def reaper_tick() -> None:
                reap_expired_holds(datetime.now(timezone.utc), calendar)

            def watchdog_tick() -> None:
                check_escalations(datetime.now(timezone.utc), escalations.open())

            tasks = [
                asyncio.create_task(run_periodically(reaper_tick, reap_interval)),
                asyncio.create_task(run_periodically(watchdog_tick, watchdog_interval)),
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    return create_app(settings, on_message, after_message=after_enqueue, lifespan=lifespan)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from anthropic import Anthropic

    from wca.transport.webhook import WebhookSettings
    from wca.transport.whatsapp import WhatsAppTransport

    env_path = _load_dotenv()
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    verify_token = os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not secret or not verify_token:
        print("WHATSAPP_APP_SECRET and WHATSAPP_WEBHOOK_VERIFY_TOKEN must be set in .env")
        return 1
    if not phone_number_id or not access_token:
        print("WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN must be set in .env")
        return 1

    # Length only — never the secret itself. Confirms which .env won.
    print(f"[serve] loaded {env_path} (app_secret_len={len(secret)})")

    app = build_serve_app(
        settings=WebhookSettings(app_secret=secret, verify_token=verify_token),
        ruleset=load_ruleset(args.rules),
        catalogue=load_catalogue(str(args.catalogue)),
        calendar=_demo_calendar(),
        client=Anthropic(),
        transport=WhatsAppTransport(phone_number_id, access_token),
    )
    uvicorn.run(app, host="0.0.0.0", port=args.port)
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    """Run one reaper pass and report what it released.

    v0's calendar lives only in the serving process's memory (see
    `wca.calendar.mock`), so this cannot reach into a running `serve`
    and clear its holds — there is nothing here to attach to. What it
    demonstrates is the reaper itself, against a freshly built demo
    calendar, without leaving a server running.
    """
    from wca.scheduler import reap_expired_holds

    calendar = _demo_calendar()
    now = datetime.now(timezone.utc)
    released = reap_expired_holds(now, calendar)
    print(f"reaper: {len(released)} hold(s) released")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="wca", description="WhatsApp Commerce Agent v0")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cases = sub.add_parser("cases", help="run the twenty test cases")
    p_cases.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p_cases.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    p_cases.add_argument("--gate-off", action="store_true", help="run without the gate")

    p_rules = sub.add_parser("rules", help="list the rules in English")
    p_rules.add_argument("--rules", type=Path, default=DEFAULT_RULES)

    p_send = sub.add_parser("send", help="send one message on the live thread")
    p_send.add_argument("--to", required=True, help="recipient in full international form, digits only")
    p_send.add_argument("--text", required=True)

    p_serve = sub.add_parser("serve", help="run the webhook receiver")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    p_serve.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE)

    sub.add_parser("reap", help="run one reaper pass and report what it released")

    args = parser.parse_args()
    if args.command == "cases":
        return cmd_cases(args)
    if args.command == "rules":
        return cmd_rules(args)
    if args.command == "send":
        return cmd_send(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "reap":
        return cmd_reap(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
