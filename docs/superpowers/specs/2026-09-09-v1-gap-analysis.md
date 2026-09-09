# WCA v1 — Gap Analysis and Implementation Plan

**Date:** 9 September 2026
**Status:** For review, pre-implementation
**Source:** `docs/PRD-whatsapp-commerce-agent.md` §6, §11; `docs/superpowers/specs/2026-08-21-wca-v0-design.md`; `docs/superpowers/specs/2026-08-21-wca-v0-addendum-tools.md`
**Scope:** v0 is built, on branch `live-thread`. This document maps the five things PRD §6 names as v1 (live calendar, Flow booking screen, reschedule/cancel, durable state, approved templates for one market) against the actual code in `src/wca/`, and proposes an ordered build.

---

## 0. What v0 actually is, read from code

**The invariant.** From the addendum: *"Every booking is preceded by a gate verdict of PASS on a proposal built from the conversation's facts. There is no code path that commits a slot without one."* `src/wca/gate.py` enforces this; `src/wca/tools.py::request_booking` is the only caller of `MockCalendar.commit`. Nothing else calls `commit`.

**State today is 100% in-process memory.** `build_serve_app` in `src/wca/cli.py` constructs, per server process: `ConversationStore` (`conversation/state.py`), `DedupStore` (`conversation/dedup.py`), `ThreadQueue` (`conversation/queue.py`), `AuditLog` (`audit.py`), `EscalationBook` (`scheduler.py`), `MockCalendar` (`calendar/mock.py`, holds + bookings), plus three bare dicts closed over inside `build_serve_app` itself: `histories` (LLM context), `send_times` (rate budget), `ask_counts` (loop guard). None of this is written anywhere. A process restart loses all of it.

**`CalendarPort`** (`calendar/base.py`) is a `Protocol` with eight methods: `availability`, `hold`, `commit`, `release`, `expire_due`, `view`, `mark_reminded`, `due_reminders`. Two of these — `hold` and `expire_due` — describe a two-phase lease that no consumer calendar API (Google Calendar, Calendly, Cal.com, a salon POS) natively has. `MockCalendar` implements the lease itself, in memory, under a `threading.Lock`. A live implementation has to build that same lease machinery in front of a real calendar, because the real calendar only knows "booked" or "not booked," never "held."

**`transport/`** knows two outbound shapes: `send_text` and `send_buttons` (≤3 buttons, `transport/whatsapp.py`). Nothing sends a WhatsApp list message (the PRD's own "10-item list") or a Flow. More importantly: **`cli.py::run_job`, the only place a live reply goes out, calls `transport.send_text` unconditionally** (line ~411). `send_buttons` exists and is tested (`test_whatsapp_client.py`) but nothing in the live path ever calls it. Interactive messages are not wired in, full stop.

**The gate reads:** `Proposal` (facts, cited rules, action), the `RuleSet`, a `calendar_view` dict from `CalendarPort.view()`, and a `window_view` dict from `escalation.window_view`. Check 5 (`slot_still_free`) re-reads `calendar_view` fresh at gate time rather than trusting the hold it was handed — this is deliberate and it is what would catch a second writer, provided `view()` reflects the real calendar's live state rather than a cache.

**No reschedule/cancel code exists.** `grep -ri "resched\|cancel" src/wca` returns nothing except unrelated matches (`cli.py`'s `--gate-off` help text, transport files with no bearing). `models.py::ActionType` is `Literal["book", "ask", "escalate", "decline"]` — there is no `reschedule` or `cancel` action type, and `MockCalendar` has no operation that un-books a `Booking`.

---

## 1. Live calendar integration

**PRD requires:** "Live calendar integration" (§6). Out of scope for v0 by explicit statement (§11, design spec §2): "A real calendar... The calendar stays mock even on the live thread."

**What exists:** `CalendarPort` protocol (`calendar/base.py`), fully exercised by `MockCalendar` (`calendar/mock.py`) — hold/commit/release/expire_due semantics, idempotency-key dedup on commit, typed refusals (`RefusalReason`), `view()` as a read-only gate snapshot. `gate.py` only ever consumes `calendar_view`, never the calendar object — it is already calendar-implementation-agnostic. `tools.py::request_booking` and `check_availability` already go through the port, not the mock directly.

**The gap:** No implementation of `CalendarPort` against a real API exists. The harder gap is conceptual, not just missing code: `hold()` and `expire_due()` assume a lease that has no counterpart in a real calendar's data model. A live `CalendarPort` has to be two things wired together — (a) a real-calendar client for reading availability and writing confirmed events, and (b) a hold ledger, durable, that this codebase owns and the real calendar does not know about. `view()`'s `held_by_thread` field has to come from that ledger, not from the external calendar, while `slot_exists`/`booked` have to come from a live read of the external calendar so an external write (the owner books a walk-in directly) is visible.

**Implementation steps:**
1. Add `calendar/live.py` with a `LiveCalendar` class implementing `CalendarPort`, backed by (a) an injected calendar client interface (`create_event`, `list_events`, `delete_event` — keep this narrow, do not adopt a vendor SDK's full surface) and (b) the durable hold ledger from item 4. Start with `availability()` and `view()` only, both read-only, both testable against a fake client with no salon credentials.
2. Implement `hold()`: write to the ledger only, never to the real calendar. A hold is purely ours; the real calendar is not told about it until commit. This keeps `HOLD_TTL_SECONDS` and the reaper meaningful unchanged.
3. Implement `commit()`: idempotency-key check against the ledger first (mirrors `MockCalendar.commit`), then one write to the real calendar creating the event, then a ledger write marking the hold consumed. Handle the real calendar refusing the write (conflict, API error) as a `HoldRefused`, not an exception that escapes — the gate already passed, so a refusal here is now a user-facing failure that needs an honest "actually, that didn't work" message, which v0 has no code path for today.
4. Implement `release()` and `expire_due()` against the ledger only.
5. Add a `wca calendar-check` CLI command that runs `availability()` against the real API with no side effects, for manual verification before pointing a live thread at it — mirrors the offline-first posture the design spec insists on for everything else.
6. Swap `_demo_calendar()` in `cli.py` for `LiveCalendar` behind a flag/env var, so `MockCalendar` stays available for `wca cases` and tests. Do not delete `MockCalendar` — it is what makes the test suite run with no network, and that property should survive v1.

**What could break:** Check 5 (`slot_still_free`) depends on `calendar_view["held_by_thread"]` correctly reflecting who holds a slot. If the ledger and the real calendar ever disagree (ledger says held, real calendar shows the slot booked by someone else, or vice versa), `view()` has to pick one source of truth per field and that choice determines whether the gate catches a real double-book or not. Get this wrong and check 5 passes on a slot that is not actually free — the exact failure PRD §3 calls out ("it books a slot that is already gone"). This is the single highest-risk piece of v1.

---

## 2. Booking screen as a WhatsApp Flow

**PRD requires:** "Booking screen as a WhatsApp Flow" (§6). §10 lists Flow as v1-only interface; §11 (platform gaps) names "Rejecting a Flow submission before it completes. Validation currently happens after the customer has finished the form" as a known Meta limitation to design around.

**What exists:** Nothing. `transport/base.py::TransportPort` has no Flow-shaped method. `transport/whatsapp.py::parse_inbound` handles three inbound shapes (`text`, `button` quick-reply, `interactive` button/list reply) — no `nfm_reply` (Flow's inbound submission shape) parsing. `tools.py::request_booking` is the one gated entry point a booking can come through; it takes `service_id`/`slot_id`, which is a plausible target for a Flow submission to feed into, provided it goes through the same call.

**The gap:** Three separate pieces, none present: (a) sending a Flow message (a new outbound type, needs a published Flow JSON registered with Meta), (b) receiving the Flow's completion payload, which arrives as an inbound message of a new shape, and (c) — this is the load-bearing one — making sure that payload is *treated exactly like a tool call the model made*, i.e. it goes through `propose()` → `gate.evaluate()` → `commit()`, not a shortcut that writes to the calendar because "the customer already filled out a form so it must be fine." The design spec's platform-gaps section already names the danger: Meta cannot reject a bad submission before the form completes, so this codebase is the only place that can.

**Implementation steps:**
1. Design the Flow's own screen(s) and register it in Meta's console — this is Meta configuration, not code, and can start immediately, in parallel with everything else.
2. Add `send_flow(to, flow_id, ...)` to `TransportPort` and `WhatsAppTransport`; `FakeTransport` gets the same method so tests keep running offline.
3. Add Flow-submission parsing to `transport/whatsapp.py::_visible_text`/`parse_inbound` — or, better, a separate `parse_flow_submission` that returns a structured payload rather than forcing a form submission through the "customer typed text" shape, since a service_id/slot_id pair is not conversational text and should not pretend to be.
4. Route a parsed Flow submission into `tools.request_booking` with the same `ToolContext` the Agent tool loop uses — not a parallel code path. This is the step that keeps the invariant intact; skipping it is the step that breaks it.
5. Decide what "availability inside the Flow" means given §11 also lists "Live availability inside a Flow" as v0-out-of-scope and not explicitly named as in-v1 by §6. If the Flow shows a static or periodically-refreshed slot list rather than a live one, a slot picked in the Flow can already be gone by submission time — `request_booking`'s existing hold→gate→commit sequence already handles this correctly (check 5 catches it), so no new gate logic is needed here, only a user-facing message for "that slot's gone, please pick another," which does not exist today.

**What could break:** The gate's grounding checks (1–4) assume `proposal.facts` came from `propose()`, which builds `cited_rules` from the conversation's own accumulated facts. A Flow submission that skips fact accumulation (customer never told the agent it's their first colour visit, because they went straight to the Flow) produces a proposal with fewer facts than a conversational booking would — every rule needing an unestablished fact is `UNKNOWN`, and check 3/4's veto behaviour blocks it, which is correct but will look like "the Flow is broken" to whoever demos it unless this is anticipated. This is a UX gap, not a safety one — the safety property still holds.

---

## 3. Rescheduling and cancellation

**PRD requires:** "Rescheduling and cancellation" (§6). Explicitly out of v0 (§11, design spec §2).

**What exists:** Nothing to reschedule or cancel with. `models.py::ActionType` has no `reschedule`/`cancel` value. `calendar/mock.py::Booking` has no state beyond "exists"; there is no un-book operation. `escalation.py`/`gate.py` have no checks shaped for "does this cancellation still leave time to notify the salon" or similar.

**The gap:** This is the most gate-shaped of the five — it needs the same propose→gate→commit structure `request_booking` uses, not new machinery. But it needs a fact the current schema has no field for (which existing booking is being changed) and a calendar operation (`cancel`, and `reschedule` as cancel+rebook or an atomic move) that does not exist on `CalendarPort` at all.

**Implementation steps:**
1. Add `cancel` and `reschedule` to `ActionType` in `models.py`, and matching outcome handling in `gate.py` (a cancellation needs its own check: is this booking still ours to cancel — i.e. does `thread_id` match the booking's owner — a check `slot_still_free` does not cover, since a booking, not a hold, is what's in play).
2. Add `cancel(booking_id, thread_id, now)` and let `reschedule` be implemented in `tools.py` as cancel-then-`request_booking`, reusing the existing hold/gate/commit path rather than inventing a second one — this is the same principle §5 of the addendum uses for `request_booking` itself: minimise new gated surface area.
3. Add `cancel_booking`/`reschedule_booking` tools to `tools.py::TOOL_SPECS`, gated the same way `request_booking` is — `dispatch` already routes by name, so this is additive.
4. New gate check, or an extension of an existing one: does the *thread asking* own the booking. Nothing today checks that a `thread_id` matches a `Booking.thread_id` before allowing a change to it — `MockCalendar` stores `thread_id` on `Booking` already (`calendar/mock.py` line ~75), so the data is there, it is just never read for authorization.
5. Update `escalation.py`/rules if a cancellation inside some lead-time window should itself require human review (a policy question for the salon, not a code question — flag it, do not invent an answer).
6. Extend `harness.py`/`cases/v0.cases.json` with reschedule/cancel cases in the same five-tier shape (clean, adversarial, override, unanswerable, ambiguous) the existing twenty use, per design spec §12 — this is what makes the new gate checks provably work rather than merely exist.

**Sequencing note:** this item depends on durable state (§4 below) being done first. A booking that only exists in `MockCalendar._bookings` or a live calendar without a durable hold ledger cannot be safely cancelled across a restart — the customer says "cancel my 2pm," the process restarted an hour ago, and `ConversationState.facts` (which booking, which thread) is gone. Reschedule/cancel without durable state means the agent has no memory of what it booked.

**What could break:** The gate's existing checks are keyed to `proposal.action.type == "book"` (see `gate.py` comment block above check 4). Adding `cancel`/`reschedule` without auditing every place that condition appears risks a proposal of the new type sailing through checks written only with "book" in mind — e.g. check 4's deny/escalation/lead-time vetoes currently only fire `if proposal.action.type == "book"`, so a cancellation today would pass through checks 1–3 (grounding) but skip 4 entirely, which is probably correct for a cancel (nothing to veto) but has not been decided, only defaulted into.

---

## 4. Durable state

**PRD requires:** "Durable state" (§6). No explicit v0 statement that state is ephemeral, but design spec §16's file list and the "v0, never evicted, good enough for a demo deployment" comments throughout `cli.py` make it implicit.

**What exists:** Nothing persists. Concretely, everything below lives in one Python process's memory and is gone on restart or crash:

| Structure | File | Loses on restart |
|---|---|---|
| `ConversationState` (facts, `last_inbound_at`, `hold_id`) per thread | `conversation/state.py::ConversationStore` | Everything the agent knows about an in-flight conversation |
| Seen `message_id`s | `conversation/dedup.py::DedupStore` | Dedup protection — a redelivered webhook after restart can double-process |
| Audit trail | `audit.py::AuditLog` | Every booking/block record — the thing PRD §5 says proves a booking was authorised |
| Open escalations | `scheduler.py::EscalationBook` | The watchdog has nothing to watch after restart; an escalation raised at hour 2 that the process restarts at hour 10 is untracked from then on |
| Holds and bookings | `calendar/mock.py::MockCalendar` (`_holds`, `_bookings`) | The calendar itself. A held slot is silently freed; a booked slot forgets it exists (v0 only — see item 1 for the live-calendar case, where bookings live externally but holds still need to survive) |
| LLM message history per thread | `cli.py::build_serve_app`'s `histories` dict | Conversation context — the customer has to re-explain everything |
| Rate-limit counters | `cli.py`'s `send_times` dict | Not safety-critical, but a restart resets anyone's budget |
| Ask-loop counters | `cli.py`'s `ask_counts` dict | The `MAX_ASKS_PER_FACT` guard forgets it already asked twice — a restart re-opens the exact ask-loop bug this guard was built to close |

**The gap:** No persistence layer of any kind — no database, no file-backed store, no ORM in `pyproject.toml` dependencies (only `pydantic`, `fastapi`, `uvicorn`, `httpx`, `dotenv`, `anthropic`). Every one of the eight in-memory stores above needs a durable-backed equivalent with the same interface, so the fakes can keep serving tests.

**Implementation steps:**
1. Pick a store. Given "Python, standard library on the default path" (PRD §10) and v0's existing discipline about deliberate dependencies (FastAPI/uvicorn justified explicitly in design spec §3), SQLite via the stdlib `sqlite3` module fits without adding a new external dependency, and is enough for one salon's traffic.
2. Persist `AuditLog` first — append-only, no concurrent-write complexity beyond what `AuditLog.append`'s dedup-by-`proposal_id` already handles, and it is the one piece with a standing claim ("audit record... stores what we did") that a memory-only implementation currently cannot honour past a restart. One table, one write path.
3. Persist `ConversationState` next — same shape as `AuditLog`: `ConversationStore.get_or_create`/`add_facts` becomes a read-then-write against SQLite instead of a dict. This is what makes reschedule/cancel (item 3) and Flow (item 2) safe to build on.
4. Persist `DedupStore` — a `message_id` unique-constraint table. This is the one most directly protecting the "commit twice, book once" invariant (design spec exit criterion 5) across a restart, not just within one process's lifetime.
5. Persist the hold ledger (needed either way — for `MockCalendar` today, and for the live-calendar hold layer in item 1 regardless). `MockCalendar`'s `_holds`/`_bookings` move to the same store; `HOLD_TTL_SECONDS` and the reaper (`scheduler.py::reap_expired_holds`) keep working unchanged since they only need `expire_due(now)` to see every open hold, wherever it lives.
6. Persist `EscalationBook` — needed for the watchdog (`check_escalations`) to survive a restart mid-window; this is the piece PRD §3 calls "the interesting one" (late escalation), so it should not be the last piece done.
7. Decide `histories`/`ask_counts`/`send_times`: these can plausibly be reconstructed from the audit trail plus `ConversationState.facts` rather than stored separately, which would be less new surface area than a ninth table — worth a design pass before committing to a shape, flagged here rather than pre-decided.
8. Add a migration/init path (`wca db init` or similar) and update `build_serve_app` to accept the durable stores as the defaults instead of the in-memory classes, keeping the in-memory classes as what tests inject — mirrors exactly how `CalendarPort`/`TransportPort` already separate "what production uses" from "what tests use."

**Sequencing note:** this is the first thing to build. Rescheduling needs it (§3). A live calendar's hold ledger needs it (§1). It has no dependency on Meta, no dependency on a real salon, and it is fully testable offline the same way everything else in v0 is — SQLite needs no network and no tunnel. This should be the first v1 work, full stop.

**What could break:** None of the gate's checks change shape — `gate.evaluate` is still a pure function over `Proposal`/`RuleSet`/`calendar_view`/`window_view`, all still plain data. The risk is entirely in wiring: `AuditLog.append`'s in-memory dedup-by-`proposal_id` (`audit.py` line ~24) has to become a real uniqueness constraint, not a `set` that itself resets on restart — get this wrong and the exact bug being fixed (loses dedup on restart) reappears one layer down. The gate-purity test (`test_gate_purity.py`, "the gate calls no model," import-graph test per design spec §4) should be extended to also assert the gate imports nothing from a persistence module — the same discipline, one more boundary.

---

## 5. Approved templates for one market

**PRD requires:** "Approved templates for one market" (§6). v0 hard-codes `quality_rating: str = "green"` (`escalation.py`) and ships a `REGISTRY` in `cli.py` where every template is `approved=True` by fiat (line ~40-49).

**What exists:** `escalation.py::TemplateRegistry`/`Template` — the data model is already shaped for this: `reason`, `name`, `approved: bool`. `cli.py::REGISTRY` populates one entry per rule-triggered escalation (`under_16_needs_guardian`, `patch_test_first_colour`) plus one per `ESCALATION_REASONS` enum value. `gate.py` check 6 and `escalation.py::window_view` already consume `template.approved` correctly — a `False` value already produces a real block today, this path is not hypothetical.

**The gap:** Every template in `REGISTRY` is asserted approved in code; none has been through Meta's actual template review, and none is localised for a specific market (language, currency formatting in `human_slot_label`/pricing, opt-in language Meta requires in the template body). This is mostly not a code gap — it is a business process (submit templates to Meta, wait for review, which can take days) that the code should be built to slot into rather than assume away.

**Implementation steps:**
1. Draft the actual template bodies needed: one per `ESCALATION_REASONS` value, one per rule-triggered escalation reason, plus whatever the reminder flow (`transport/webhook.py`'s `/reminders/due`) sends — check whether the 24-hour reminder itself needs an approved template (if it's sent outside a 24h customer-initiated window, it does) or can ride the free-form window. This drafting can start immediately; it has no code dependency.
2. Submit templates to Meta for the target market/language, in parallel with all other v1 work — this is the longest lead-time item in the whole plan and should start on day one, not after code is ready.
3. Change `cli.py::REGISTRY` from a hard-coded literal to a loaded, versioned file (`policy/salon.templates.json`, same pattern as `policy/salon.rules.json` and `policy/salon.catalogue.json`) so `approved` reflects Meta's actual review state and can be updated without a code change — mirrors the rules/catalogue pattern already established.
4. Add a manual or scripted way to sync `approved`/`quality_rating` from Meta's actual API/console state rather than hand-editing the file — PRD §11 already flags that Meta doesn't expose deliverability reliably, so this stays a "best available signal," not a guarantee, same as today.
5. Localise `human_slot_label` (`tools.py`) and any other customer-facing string generation for the target market's language/date conventions if "one market" implies non-English — currently hard-coded to English weekday/month names and `am`/`pm`.

**What could break:** Nothing in the gate changes. This item is the lowest-risk of the five precisely because `escalation.py`/`gate.py` check 6 were already built assuming templates might not be approved — the current hard-coded `True` is scaffolding, not a load-bearing assumption anything else depends on. The risk is entirely external: a template Meta rejects, or approves with edited wording, has to be re-synced into whatever replaces `REGISTRY`, and a stale `approved=True` after Meta silently revokes a template (quality-rating drop) is exactly the "Meta doesn't tell us reliably" gap PRD §11 already names as unfixable from this side.

---

## 6. Sequencing

```
1. Durable state (§4)         — no external dependency, unlocks 2 and 3, fully offline-testable
        │
        ├──▶ 2. Live calendar (§1)     — needs the hold ledger from §4
        │           │
        │           ▼
        └──▶ 3. Reschedule/cancel (§3) — needs persisted ConversationState + Booking.thread_id from §4;
                                          benefits from but does not strictly require §1
                    │
                    ▼
             4. Flow booking screen (§2) — needs §1 for a meaningful live-slot picker;
                                            the gate-routing work (step 4 in §2) can start once
                                            request_booking exists, i.e. now

5. Approved templates (§5) — no code dependency on 1–4; start the Meta submission (step 2) on day one,
                              in parallel with everything else, because it is the longest lead time
```

Durable state first because nothing else survives a restart without it, and because it is the only item with zero external blocker. Live calendar and reschedule/cancel both sit downstream of it; live calendar is placed before reschedule/cancel because a reschedule against a mock calendar demonstrates less than a reschedule against a real one, but the two are not strictly sequential — reschedule/cancel's gate and model work could proceed against `MockCalendar` in parallel with live-calendar work, then get re-pointed. Flow is placed last among the code items because its highest-risk step (routing a Flow submission through the same gated path as a tool call) is best done once that path has already been exercised by reschedule/cancel's own new action types, not as the first thing to touch `request_booking`'s neighbourhood. Templates run on their own clock the whole time — submit early, build against `approved=False` until Meta says otherwise, same discipline the gate already applies to "we never established X."

**Recommended first three commits:**
1. `AuditLog` → SQLite-backed, same interface, in-memory version kept as the test double (§4, step 2).
2. `ConversationStore` → SQLite-backed, same interface, in-memory version kept as the test double (§4, step 3).
3. `DedupStore` → SQLite-backed with a `message_id` unique constraint, same interface (§4, step 4).

Each is independently shippable, none touches the gate, and together they retire the largest and least-debatable gap before any calendar, Flow, or template work begins.

---

## 7. Where v1 threatens the gate's invariant

The invariant — every booking preceded by a gate PASS, no code path that commits without one — is a property of `tools.py::request_booking` being the only caller of `commit`. Nothing proposed above changes that structurally, provided two things are actually built, not assumed:

**A live calendar is a second writer the gate cannot see between polls.** `gate.py` check 5 re-reads `calendar_view` at gate time specifically so a stale hold gets caught — this is already the right design for a second writer, but it only works if `CalendarPort.view()` for a live calendar does a live read of the external calendar's current state, not a cached one. If `view()` is ever implemented against a periodically-refreshed cache (tempting, for latency), check 5 stops being able to catch an external double-book, and the gate would PASS a booking onto a slot someone already took directly in the salon's calendar. This has to be a live read, every time, no exceptions, or the check is theatre.

**A Flow lets a customer submit data that never touched fact extraction.** The gate does not care where `proposal.facts` came from — it only checks whether cited rules are grounded in whatever facts exist. A Flow submission routed through `request_booking` (the correct design, §2 above) inherits every one of the gate's checks automatically, because they are facts-shaped, not conversation-shaped. The actual risk is not the gate being bypassed — it is a *shortcut being built* that writes to the calendar directly from a Flow's webhook payload without going through `propose()`/`gate.evaluate()` at all, because a Flow submission looks "already validated" (the customer filled out a real form) when structurally it is exactly as untrusted as a typed message. Nothing in the current code makes this mistake possible today only because Flow support does not exist yet; it becomes possible the day someone wires a Flow's data-exchange endpoint straight to a calendar write instead of to `tools.request_booking`. The plan in §2 is written specifically to make that the only path.

For the invariant to hold through v1, both of those have to be true by construction, not by discipline: `view()` must be a live read, and every new inbound surface (Flow, reschedule/cancel) must terminate in the same `propose → gate.evaluate → commit-or-release` sequence `request_booking` already uses, with no second entry point to `CalendarPort.commit`.
