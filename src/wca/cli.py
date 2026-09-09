"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import defaultdict
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
from wca.rules.evaluate import missing_facts
from wca.rules.render import to_english
from wca.rules.specificity import unknown_rules
from wca.rules.store import load_ruleset
from wca.tools import CONVERSATIONAL_FACTS, ESCALATION_REASONS

DEFAULT_RULES = Path("policy/salon.rules.json")
DEFAULT_CATALOGUE = Path("policy/salon.catalogue.json")
DEFAULT_CASES = Path("cases/v0.cases.json")

#: Fixed, not `datetime.now()` -- a demo run today and a demo run next
#: week offer the same slots. There is no real calendar behind v0 (see
#: `wca.calendar.mock`), so a live conversation needs something to check
#: availability against and hold.
DEMO_CALENDAR_START = utc(2026, 9, 8, 9)

REGISTRY = TemplateRegistry(templates=(
    # Rule-id escalations: `wca.propose.propose` sets `escalation_reason`
    # to a matched rule's own id (e.g. a `require_escalation` outcome),
    # so these two are named after the rules, not after ESCALATION_REASONS.
    Template(reason="under_16_needs_guardian", name="guardian_notice", approved=True),
    Template(reason="patch_test_first_colour", name="patch_test_notice", approved=True),
    # One approved template for every reason the `escalate` tool's schema
    # allows the model to send (see `wca.tools.ESCALATION_REASONS`). This
    # is what keeps the tool's enum and this registry from drifting apart
    # again -- see tests/test_cli.py's test against this exact registry.
    *(
        Template(reason=reason, name=f"{reason}_notice", approved=True)
        for reason in ESCALATION_REASONS
    ),
))

#: A short, generic reply sent instead of leaving the customer with
#: silence -- when the agent's own reply comes back empty (iteration cap
#: hit with no final text), when the turn raises unexpectedly, or when a
#: thread has gone over its message budget (see MAX_MESSAGES_PER_WINDOW
#: below). On WhatsApp, silence is indistinguishable from a dead number.
FALLBACK_REPLY = "Sorry, I'm having trouble with that request. A member of our team will follow up with you shortly."

#: A rolling per-thread budget. The webhook is public and holds a real
#: API key; without a bound, a thread with no natural end could call the
#: model without limit. v0 keeps this simple and in-memory: a plain count
#: of messages handled per thread_id within a trailing window. Over
#: budget, the thread gets FALLBACK_REPLY once per message and the model
#: is never called for it.
MAX_MESSAGES_PER_WINDOW = 20
MESSAGE_BUDGET_WINDOW_SECONDS = 3600.0

#: How many past turns (customer message + agent reply, each counted
#: separately) are replayed to the model on the next turn. Unbounded
#: history would grow every request's token cost forever; this caps it.
MAX_HISTORY_MESSAGES = 20

#: A prompt is a request, not a guarantee -- a model that ignores its
#: instructions can keep asking the same customer-only fact
#: (`wca.tools.CONVERSATIONAL_FACTS`) forever, which is exactly what
#: happened on the live thread this task exists to fix: four escalating
#: variations of "are you over 16?" to one customer, none of them ever
#: accepting her answer.
#:
#: This is the hard stop, independent of anything a prompt says. Whether
#: a fact is still needed is computed the same way `wca.tools
#: .request_booking` computes it -- `unknown_rules` + `missing_facts`
#: against the ruleset, not by reading the model's reply text -- so it
#: is exact about which fact is actually blocking a decision and immune
#: to how the model happens to have phrased the question. Two turns get
#: to ask; the third message for a still-unanswered fact escalates
#: instead of asking again.
MAX_ASKS_PER_FACT = 2

#: The registered reason (see `wca.tools.ESCALATION_REASONS` and
#: `REGISTRY` below) used when MAX_ASKS_PER_FACT trips: a customer-only
#: fact that keeps not arriving is a conversation the agent cannot
#: complete on its own -- not a policy question, not a technical fault --
#: which is exactly what "outside_agent_scope" is for.
ASK_LOOP_ESCALATION_REASON = "outside_agent_scope"


#: Both `CONVERSATIONAL_FACTS` are booleans (`is_first_colour_visit`,
#: `customer_is_over_16`), so a single one still missing after a turn is
#: always a yes/no question -- never a shape the model had to choose.
YES_NO_LABELS: tuple[str, ...] = ("Yes", "No")


def _interactive_offer(
    tool_calls: list[tuple[str, Any]], needed_facts: set[str]
) -> tuple[str, list[str]] | None:
    """What this turn's own tool results justify sending as an
    interactive message instead of plain text -- or `None` for plain text.

    This is the whole answer to "how does the agent signal an interactive
    reply": it does not. Nothing here reads anything the model said or
    declared. The only inputs are `tool_calls` (`Agent.tool_calls` --
    each tool's name and its actual return value, in the order the tools
    ran this turn) and `needed_facts` (the same `_facts_still_needed`
    computation the ask-loop guard above already uses, straight off the
    ruleset and the conversation's own facts). A model that wanted to
    force an interactive send with nothing behind it has no lever to pull
    -- the shape is recomputed here from ground truth every turn, so it
    cannot drift from what the tools actually returned.

    - A committed booking, or a successful escalation, is always plain
      text: there is nothing left to choose from.
    - The most recent `check_availability` call this turn, if it
      returned a concrete list of slots (1-10 of them -- more is not a
      list message WhatsApp accepts), becomes a list offer: one row per
      slot, titled with that slot's own `label`, verbatim.
    - Otherwise, if exactly one `CONVERSATIONAL_FACTS` entry is still
      missing, that is a single yes/no question -- buttons.
    - Anything else -- nothing checked, two facts still missing (a
      compound question, not a clean yes/no), zero or eleven-plus
      slots -- is plain text.
    """
    booked = any(
        name == "request_booking" and isinstance(result, dict) and result.get("ok")
        for name, result in tool_calls
    )
    escalated = any(
        name == "escalate" and isinstance(result, dict) and result.get("ok")
        for name, result in tool_calls
    )
    if booked or escalated:
        return None

    slot_results = [
        result for name, result in tool_calls
        if name == "check_availability" and isinstance(result, list)
    ]
    if slot_results:
        slots = slot_results[-1]
        labels = [s["label"] for s in slots if isinstance(s, dict) and "label" in s]
        if 1 <= len(labels) <= 10:
            return ("list", labels)
        return None  # zero slots, or more than a list message can hold

    if len(needed_facts) == 1:
        return ("buttons", list(YES_NO_LABELS))

    return None


def _send_reply(
    transport: Any, to: str, reply: str, offer: tuple[str, list[str]] | None
) -> None:
    """Send `reply` -- as the interactive message `offer` describes, if
    any, falling back to plain text on any failure to send it that way.

    `reply` is always the message body, in either shape: the interactive
    path is not a second channel with its own content, only a different
    envelope around the same text the customer would otherwise have
    read. A send that cannot be expressed as interactive (more rows than
    `send_list` accepts, a transport error) must not raise and must not
    leave the customer with silence -- it falls back to the same plain
    `send_text` this function always ends with otherwise.
    """
    if offer is not None:
        kind, labels = offer
        try:
            if kind == "list":
                transport.send_list(to, reply, labels)
                return
            if kind == "buttons":
                transport.send_buttons(to, reply, labels)
                return
        except Exception as exc:
            print(f"[serve] interactive send ({kind}) failed, falling back to text: {exc!r}")
    transport.send_text(to, reply)


def _facts_still_needed(ruleset: Any, facts: dict[str, Any]) -> set[str]:
    """Which `CONVERSATIONAL_FACTS` a rule that could still apply is
    missing, given what the conversation knows so far.

    Same two functions `wca.tools.request_booking` uses to build the
    "ask" question for a proposal -- `unknown_rules` (a rule whose
    condition cannot yet be decided true or false) and `missing_facts`
    (which of that rule's facts we don't have) -- called here directly,
    read-only, with no slot and no proposal, purely to answer "is this
    fact still blocking something" for the loop guard below. A booking
    that has already been decided (a rule matched TRUE or FALSE) never
    shows up here, so a fact irrelevant to what the customer actually
    asked for -- age, for a haircut -- never counts against the guard.
    """
    needed: set[str] = set()
    for rule in unknown_rules(ruleset, facts):
        needed.update(missing_facts(rule, facts))
    return needed & CONVERSATIONAL_FACTS


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
    extractor: Any,
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
    client, an `AnthropicExtractor`, `WhatsAppTransport`, a calendar
    seeded with demo slots) and a test can supply fakes (a stubbed
    client, a `FakeExtractor`, `FakeTransport`, an in-memory calendar)
    and drive the exact same wiring with no network and no real model
    call. `extractor` has no default, same as `client` and `transport`:
    every caller must say explicitly what turns a message into facts,
    rather than the pipeline silently running with none (see `run_job`
    below -- that silence was the bug this parameter exists to close).

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
    from wca.tools import escalate as escalate_tool  # the ask-loop guard's own escalation
    from wca.transport.webhook import create_app

    audit = audit if audit is not None else AuditLog()
    conversations = conversations if conversations is not None else ConversationStore()
    dedup = dedup if dedup is not None else DedupStore()
    queue = queue if queue is not None else ThreadQueue()
    escalations = escalations if escalations is not None else EscalationBook()
    reap_interval = reap_interval_seconds or DEFAULT_REAP_INTERVAL_SECONDS
    watchdog_interval = watchdog_interval_seconds or DEFAULT_WATCHDOG_INTERVAL_SECONDS

    # Per-thread message history (for the model), send timestamps (for
    # the budget below), and the ask-loop guard's own counters. All three
    # are plain in-memory dicts, same posture as DedupStore and
    # ConversationStore: v0, never evicted, good enough for a demo
    # deployment. Kept here rather than on ConversationState so this
    # task's fix stays inside wca.cli.
    histories: dict[str, list[dict[str, str]]] = defaultdict(list)
    send_times: dict[str, list[datetime]] = defaultdict(list)
    #: thread_id -> {fact_name: consecutive turns asked with no answer}.
    #: See MAX_ASKS_PER_FACT above.
    ask_counts: dict[str, dict[str, int]] = defaultdict(dict)

    def _over_budget(thread_id: str, now: datetime) -> bool:
        window_start = now - timedelta(seconds=MESSAGE_BUDGET_WINDOW_SECONDS)
        recent = [t for t in send_times[thread_id] if t > window_start]
        recent.append(now)
        send_times[thread_id] = recent
        return len(recent) > MAX_MESSAGES_PER_WINDOW

    def _record_new_escalations(before: int) -> None:
        # Anything this turn escalated is now something the watchdog
        # needs to know about. Read it off the audit trail rather than
        # threading a callback into wca.tools.escalate, which this task
        # does not touch. Shared by the normal agent path and the ask-loop
        # guard's own direct call to wca.tools.escalate below -- both
        # append to the same audit log, so both need the same bookkeeping.
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

    def run_job(message: Any) -> None:
        # 1. Dedup, inside the job -- see the docstring above for why.
        if dedup.seen(message.message_id):
            return
        dedup.remember(message.message_id)

        # 2. Get or create the conversation, move last_inbound_at.
        now = datetime.now(timezone.utc)
        state = conversations.get_or_create(message.thread_id, now=now)

        # 3. Per-sender budget. The webhook is public and holds a real
        # key; nothing upstream of this caps how many messages one
        # thread can send, and DedupStore/ConversationStore/the queue's
        # dicts never evict. Over budget, the model is never called.
        if _over_budget(message.thread_id, now):
            print(
                f"[serve] thread {message.thread_id} is over its budget of "
                f"{MAX_MESSAGES_PER_WINDOW} messages per "
                f"{MESSAGE_BUDGET_WINDOW_SECONDS:.0f}s; not calling the model"
            )
            transport.send_text(message.thread_id, FALLBACK_REPLY)
            return

        thread_history = histories[message.thread_id]

        # Wrapped so that any unexpected failure in the job still leaves
        # the customer with a reply -- silence on WhatsApp is
        # indistinguishable from a dead number -- while the error itself
        # is re-raised afterwards, so it still surfaces in `after_enqueue`'s
        # logging and in `queue.drain_thread`'s error list.
        try:
            # 4. Extract facts from the message and merge them into the
            # conversation before running the agent. Without this step,
            # `conversation.facts` never gains anything the customer
            # actually said, and every rule that depends on a fact only
            # the model could have read from the message text (e.g.
            # is_first_colour_visit) blocks forever with "we never
            # established" -- the agent could not book anything.
            extraction = extractor.extract(
                message.message_id,
                message.text,
                thread_history,
            )
            state.add_facts(extraction.facts, now=now)

            ctx = ToolContext(
                ruleset=ruleset, catalogue=catalogue, calendar=calendar,
                registry=registry, audit=audit, conversation=state, now=now,
            )

            # 5. The ask-loop guard: which customer-only facts this
            # thread's earlier turns have already asked about
            # MAX_ASKS_PER_FACT times with no answer captured, and are
            # still needed now. If there is one, this message does not
            # reach the model at all -- a third ask never happens, no
            # matter what the model would have said. See MAX_ASKS_PER_FACT
            # above for why this cannot be a prompt instruction instead.
            fact_asks = ask_counts[message.thread_id]
            needed = _facts_still_needed(ruleset, state.facts)
            stuck = sorted(f for f in needed if fact_asks.get(f, 0) >= MAX_ASKS_PER_FACT)

            # What, if anything, this turn's own tool results justify
            # sending as an interactive message -- see _interactive_offer.
            # Stays None on the ask-loop-escalation path below: that reply
            # is FALLBACK_REPLY, not something a slot list or a yes/no
            # question could ever apply to.
            offer: tuple[str, list[str]] | None = None

            if stuck:
                print(
                    f"[serve] thread {message.thread_id} asked about "
                    f"{stuck} {MAX_ASKS_PER_FACT} times with no answer; "
                    "escalating instead of asking again"
                )
                before = len(audit)
                escalate_tool(ctx, ASK_LOOP_ESCALATION_REASON)
                _record_new_escalations(before)
                reply = FALLBACK_REPLY
            else:
                # 6. One Agent turn with the tool context and the thread's
                # recent turns, not just the current message -- otherwise no
                # conversation history ever reaches the model.
                agent = Agent(client=client, tool_context=ctx)
                turn_messages = [*thread_history, {"role": "user", "content": message.text}]
                before = len(audit)
                reply = agent.run_turn(turn_messages)
                _record_new_escalations(before)
                offer = _interactive_offer(agent.tool_calls, needed)

                # Still needed after this turn's own attempt (extraction
                # already ran above; nothing else in this turn writes to
                # conversation.facts) counts as one more ask towards the
                # cap. A fact that got answered resets to zero rather than
                # just stopping -- if it comes back into question later
                # (the customer changes their mind, a later message
                # contradicts an earlier answer), it gets the same two
                # tries again, not zero.
                for fact in CONVERSATIONAL_FACTS:
                    if fact in needed:
                        fact_asks[fact] = fact_asks.get(fact, 0) + 1
                    else:
                        fact_asks[fact] = 0

            # 7. Never leave the customer with silence. Hitting the
            # iteration cap with no final text is an availability trade,
            # not a safety one (see wca.agent's MAX_ITERATIONS docstring)
            # -- but the customer still needs an answer. An interactive
            # offer only ever makes sense paired with the model's own
            # text, so an empty reply drops any offer along with it --
            # FALLBACK_REPLY always goes out as plain text.
            if not reply:
                offer = None
            reply = reply or FALLBACK_REPLY

            thread_history.append({"role": "user", "content": message.text})
            thread_history.append({"role": "assistant", "content": reply})
            del thread_history[:-MAX_HISTORY_MESSAGES]

            _send_reply(transport, message.thread_id, reply, offer)
        except Exception:
            transport.send_text(message.thread_id, FALLBACK_REPLY)
            raise

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

    return create_app(
        settings, on_message, after_message=after_enqueue, lifespan=lifespan,
        calendar=calendar, catalogue=catalogue,
    )


def reminder_secret_from_env() -> str | None:
    """The n8n shared secret, or `None` if it is not usable.

    An empty `N8N_REMINDER_SECRET=` is treated exactly like an absent
    one. An operator who adds the line and saves before filling it in
    must get the same closed default as one who never added it, not a
    shared secret of "". `verify_reminder_secret` refuses every request
    when this is `None`, so closed is the failure mode either way.
    """
    return os.environ.get("N8N_REMINDER_SECRET") or None


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from anthropic import Anthropic

    from wca.extract.anthropic import AnthropicExtractor
    from wca.transport.webhook import WebhookSettings
    from wca.transport.whatsapp import WhatsAppTransport

    env_path = _load_dotenv()
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    verify_token = os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    reminder_secret = reminder_secret_from_env()
    if not secret or not verify_token:
        print("WHATSAPP_APP_SECRET and WHATSAPP_WEBHOOK_VERIFY_TOKEN must be set in .env")
        return 1
    if not phone_number_id or not access_token:
        print("WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN must be set in .env")
        return 1
    if not reminder_secret:
        # Not fatal -- the WhatsApp webhook and the rest of `serve` work
        # fine without the n8n hop turned on -- but silent is how a
        # missing credential stays missing for weeks. See webhook.py's
        # verify_reminder_secret: with no secret, /reminders/due and
        # /reminders/sent refuse every request rather than allow them.
        print("[serve] N8N_REMINDER_SECRET not set -- /reminders/* will refuse every request")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Fail loudly here rather than let AnthropicExtractor() raise a
        # moment later -- and, far worse, rather than silently running
        # without an extractor at all. A factless agent still starts up
        # cleanly and looks fine right up until every rule that depends
        # on something the customer said blocks with "we never
        # established" -- that silent failure mode is exactly what this
        # check exists to rule out.
        print("ANTHROPIC_API_KEY must be set in .env -- the agent cannot extract facts without it")
        return 1

    # Length only — never the secret itself. Confirms which .env won.
    print(f"[serve] loaded {env_path} (app_secret_len={len(secret)})")

    app = build_serve_app(
        settings=WebhookSettings(
            app_secret=secret, verify_token=verify_token, reminder_secret=reminder_secret,
        ),
        ruleset=load_ruleset(args.rules),
        catalogue=load_catalogue(str(args.catalogue)),
        calendar=_demo_calendar(),
        client=Anthropic(),
        extractor=AnthropicExtractor(),
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
