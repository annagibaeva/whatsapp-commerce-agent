# WhatsApp Business Cloud API Commerce Agent

**Repo:** `whatsapp-commerce-agent`
**Author:** Anna Gibaeva
**Status:** v0 built and validated in code, including one live WhatsApp thread. v1 specified in
`docs/superpowers/specs/2026-09-09-v1-gap-analysis.md`, not started. Where this document and the
code disagree, the code is the record. Three disagreements were found on 9 September 2026. Two
were this document being stale and are corrected below, each marked. The third was the document
being right and the code being incomplete — outbound interactive messages — and was fixed in code.

---

## 1. Summary

A conversational agent on WhatsApp that takes a customer from first enquiry to a booked
appointment, without a human involved.

The hard part is not the conversation. It is the booking. An agent that answers a question
wrongly gives a bad answer. An agent that books wrongly creates an obligation the business now
has to honour or break.

So the agent cannot book directly. Every booking passes through a gate that checks it against
written policy and live calendar state, and blocks anything it cannot verify. Blocked cases go
to a human.

Demonstrated on a single salon, because that is the smallest case that exercises every control.

---

## 2. Objective, background and context

**Objective.** Let a business put a booking agent on WhatsApp without accepting unlimited risk
from what the agent commits to.

**Why now.** Three things changed.

Models became reliable enough at structured tool calls that an agent can genuinely operate a
calendar. Most deployed business messaging is still decision trees, so the constraint moved
from "can it understand?" to "can we let it act?"

Meta made agents part of the platform. Meta Business Agent has been billed per token since
August 2026, so conversational AI is now native rather than partner-bolted-on.

Cost now depends on turn count. Per-message billing replaced per-conversation billing in July
2025, and from 1 October 2026 service messages and in-window utility messages become billable
too. An agent that asks six questions costs more than one that asks two.

**Where it comes from.** This extends a returns agent I built previously, which used a gate to
block ungrounded answers. That gate verified claims. This one has to verify actions, which
turns out to be a different problem.

**Where this sits in agentic commerce.** Agent payment standards arrived fast. Stripe and
OpenAI's ACP handles agent-initiated checkout. Google's AP2 attaches signed authorization
mandates. Visa's Trusted Agent Protocol gives agents a cryptographic identity. In April 2026
the FIDO Alliance formed a working group to standardise how agent-initiated transactions stay
inside user-controlled boundaries.

All of them answer the same question: **was this agent allowed to spend the user's money?**

None of them answer the other one: **was this agent allowed to promise the business's
inventory?**

AP2 proves a customer delegated authority to their agent. It says nothing about whether the
salon's agent was allowed to give away tomorrow at 2pm. That is the seller side of the same
transaction, and it is unstandardised.

This project builds the seller-side control. The buyer-side protocols and this are
complements, not alternatives — a fully agentic transaction needs both ends verified.

---

## 3. The problem

Businesses want agents on WhatsApp. They mostly deploy them read-only — answering questions,
deflecting tickets — because that is what they can sign off on.

The moment an agent can book, three things can go wrong:

**It books something it should not.** The salon requires a 48-hour patch test before a first
colour appointment. The customer asks for tomorrow. A helpful agent finds tomorrow's free slot
and takes it. The booking is now unsafe and the business has to call and cancel.

**It books a slot that is already gone.** The agent checks availability, talks for another
thirty seconds, then writes. Someone else booked in between.

**It escalates to a human too late to help.** A customer's message opens a 24-hour window in
which the business can reply freely. Outside that window, only a pre-approved template can be
sent. An escalation raised at hour 23 produces a human reply that cannot be delivered.

The third one is the interesting one, and most implementations miss it entirely — because a
demo thread runs in one sitting and never reaches hour 23.

---

## 4. Who the end user is

Three different people, with different needs.

| | Who | What they need |
|---|---|---|
| **Customer** | Messages the salon on WhatsApp | A booking, or a fast handover to a person |
| **Salon staff** | Owner or receptionist | No bad bookings to clean up; escalations that arrive in time |
| **Deploying business** | Whoever runs the agent | Evidence that every booking was authorised |

The customer is the end user. The deploying business is the buyer. The document optimises for
the buyer, because they are the one who decides whether this ships.

---

## 5. How it gets solved

The agent proposes; it does not commit.

It reads the conversation, extracts facts, and proposes an action along with the specific
policy rules it relied on. The proposed slot is held but not booked. A gate then checks the
proposal: do the cited rules exist, do the facts support them, is there a more specific rule
that overrides, is the slot still free, and is there still time for a human to reply if this
gets escalated. If every check passes, the booking commits. If any fails, the hold is released
and it goes to a human.

The gate can only block. It never supplies an answer, so it cannot introduce a mistake of its
own.

---

## 6. Versioning and route to MVP

**v0 — the weekend build. Built.** Three services across two categories, six rules, 21 test
cases, mock calendar, gate with all six checks, one live WhatsApp thread. Runs against a fake
transport so it is fully testable offline. Purpose: prove the gate works and measure what it costs
in bookings.

*Corrected 9 September 2026: this said "one service (colour)". The catalogue holds three — two
colour services and a cut — because a single service cannot exercise a rule that reads
`service_category`. The broader catalogue is the reason the category rules mean anything.*

**v1 — MVP, one real salon.** Live calendar integration, booking screen as a WhatsApp Flow,
rescheduling and cancellation, durable state, approved templates for one market.

**v2 — multiple businesses.** Per-tenant rules and test sets, quality-rating monitoring, a
configuration surface so a new client is a config change rather than a code change.

MVP is v1. v0 exists to prove the control before anyone integrates a real calendar against it.

---

## 7. Key results, KPIs and value proposition

### What good looks like

Every metric here is gameable alone. An agent that escalates everything makes zero bad
bookings. An agent that books everything maximises completion. Only a selective agent clears
all five.

| KPI | Measure | Target (v0) |
|---|---|---|
| Bad bookings | Bookings that break a rule or double-book | 0 |
| Booking rate | Bookable enquiries the agent completes alone | ≥ 80% |
| Escalation precision | Escalations that genuinely needed a human | ≥ 85% |
| Escalation deliverability | Escalations where a human reply could still be sent | 100% |
| Gate effect | Same tests run with the gate off and on | Published with counts |
| Cost of control | Bookings the gate blocked that a human then confirmed | Reported, not targeted |

### What v0 measured

Run `uv run wca cases`, and again with `--gate-off`. As of 10 September 2026, over 21 cases:

| KPI | Gate on | Gate off | Target |
|---|---|---|---|
| Bad bookings | **0** | 5 | 0 |
| Booking rate | **6/6, 100%** | 6/6, 100% | ≥ 80% |
| Escalation precision | **4/4, 100%** | 4/4, 100% | ≥ 85% |
| Escalation deliverability | enforced by check 6 | — | 100% |
| Gate effect | 0 bad bookings | 5 bad bookings | published |
| Cost of control | **0** | 0 | reported |

Five targets met, one held by construction: no escalation can pass the gate without check 6, so
deliverability is enforced rather than counted. Cost of control is zero — over these 21 cases
the gate blocked nothing that should have been booked. That is the number most likely to move
once the case set grows, and it is the one to watch.

Booking rate counts only the six cases that were meant to be booked. These cases are not 21
chances to book: most of them exist to be refused.

Escalation precision is the counter-metric. Without it, zero bad bookings is achieved by
refusing to book.

Cost of control is the price of safety. A gate that blocks nothing is useless; a gate that
blocks good bookings costs revenue. v0 reports the number rather than optimising it, because
twenty cases cannot support a target.

Twenty test cases proves the control works. It does not prove it works at volume.

### Value proposition

Against **decision trees:** handles enquiries that do not fit the tree, without losing the
tree's predictability on the booking itself. The conversation flexes; the commitment does not.

Against **an unguarded agent:** an unguarded agent's failure is a confident wrong booking,
which is worse than no agent because the business is now committed.

Against **Meta Business Agent:** a different job. MBA is the right answer for a business with
no engineering resource and simple policy. It does not expose a policy layer the business
controls, or a test set against the business's own cases.

---

## 8. Solution and system architecture

```
┌─ META PLATFORM ─────────────────────────────────────────────────────┐
│                                                                      │
│   WhatsApp client ◀──▶ Cloud API ──▶ Webhook  (at-least-once,        │
│                            │    ▲             order not guaranteed)  │
│                     Flow runtime│                                    │
└─────────────────────────────────┼────────────────────────────────────┘
                                  │  ── trust boundary ──
┌─ OUR SYSTEM ────────────────────┼────────────────────────────────────┐
│                                 ▼                                    │
│   transport/ ──▶ dedup (message_id) ──▶ per-thread queue             │
│   (only module         ▲                        │                    │
│    knowing WhatsApp)  idempotency               ▼                    │
│                        store            conversation state           │
│                                                 │                    │
│                    ┌────────────────────────────┤                    │
│                    ▼                            ▼                    │
│            fact extraction                policy store               │
│            (model call, never              (rules as data,           │
│             in the booking path)            versioned)               │
│                    │                            │                    │
│                    └─────────────┬──────────────┘                    │
│                                  ▼                                   │
│                  proposal {action, cited_rules@version}              │
│                                  │                                   │
│                          hold(slot, ttl)                             │
│                                  ▼                                   │
│                    ╔════════════════════════════╗                    │
│                    ║          GATE              ║  no model call     │
│                    ║  1 rules exist             ║  deterministic     │
│                    ║  2 facts support them      ║  p99 < 50ms        │
│                    ║  3 no override missed      ║                    │
│                    ║  4 booking cites a rule    ║                    │
│                    ║  5 slot still free         ║                    │
│                    ║  6 escalation deliverable  ║                    │
│                    ╚═══════╤═════════════╤══════╝                    │
│                        PASS│         BLOCK                           │
│                            ▼             ▼                           │
│                  commit(idem_key)   release() ──▶ human              │
│                            │             │                           │
│                            └──────┬──────┘                           │
│                                   ▼                                  │
│                             audit record                             │
│                                                                      │
│   reaper ──▶ releases expired holds                                  │
└──────────────────────────────────────────────────────────────────────┘
```

Three properties matter:

**The gate makes no model call.** Rules are evaluated deterministically, so the gate is fast
enough to sit in a synchronous path and cheap enough to run on every proposal.

**The gate is a pure function.** The same code gates live traffic and scores the offline test
set, so the tests measure the code that actually runs.

**Only `transport/` knows WhatsApp.** Everything else runs against a fake transport with a
simulated 24-hour clock. That is the only way to test escalation expiry in CI, since the real
failure takes a day to reproduce.

---

## 9. Key features

**Rules as data.** Each rule is a record: condition, facts it needs, priority, outcome, and the
policy text it came from. Conditions are evaluated by a restricted parser, not by running code.
A rule that needs a fact the conversation lacks cannot fire — which is how the agent knows when
it does not know.

**The gate.** Six checks. Blocks are labelled either *grounding* (invented or misapplied rule)
or *conclusion* (right rules, wrong call), so the two failure types are measured separately.
Check 3 is the one that catches the patch-test case: a booking that looks individually correct
but ignores a more specific rule.

**Hold, then commit.** Bookings are two-phase. Necessary because releasing a hold undoes a
mistake and cancelling a confirmed booking does not.

**Escalation with a deadline.** Every escalation records when the reply window closes and is
monitored against it.

**Audit record.** Every booking and every block stores the proposal, the rules cited, the
verdict, and the state read.

**Test harness.** Cases written before the agent, in tiers: clean, adversarial,
override, unanswerable, ambiguous. Run with the gate off and on.

**Fake transport.** Simulated clock, deterministic replay, no Meta account needed to develop.

**The deposit rule.** Services over a threshold require a deposit. This is the only rule where
money moves, and it is where the booking gate becomes a payment gate: the same six checks
apply, but the commitment is a charge rather than a slot. v0 evaluates the rule and stops short
of collecting. Collecting it is the shortest path from this build to genuine agentic commerce.

---

## 10. Stack

| | |
|---|---|
| Messaging | WhatsApp Business Cloud API (test number) |
| Model | Anthropic API, structured output. Temperature is not sent — see below |
| Language | Python, standard library on the default path |
| Built with | Claude Code |
| Scheduling | n8n for the reminder hop |
| Interface | Interactive messages (3 buttons or a 10-item list); Flow for the booking screen in v1 |

*Corrected 9 September 2026: this said "temperature 0". The code never sends `temperature`,
`top_p` or `top_k`. All three were removed on Opus 5 and sending any of them returns a 400.
Determinism now comes from the structure — the gate makes no model call at all, so the decision
that matters was never temperature-dependent in the first place.*

---

## 11. Limitations and assumptions

### Out of scope for v0

Multiple businesses. Rescheduling and cancellation. More than one language. Live availability
inside a Flow.

**Taking the deposit.** The rule fires; no money moves. This is the single most valuable thing
left undone, and the point at which this becomes agentic commerce rather than agentic
scheduling.

### Platform constraints designed around

| Constraint | Consequence |
|---|---|
| Webhooks are delivered at least once | A redelivered message could book twice. Dedup on message ID; commit takes an idempotency key. |
| Webhook order is not guaranteed | Two messages in one thread could both pass the gate for the same slot. Serial queue per thread. |
| A hold is a lease, not a lock | A crash between hold and commit orphans the slot. TTL plus a reaper. |
| Rules change over time | An audit record must cite the rule version that was live at the time, or editing a rule rewrites history. |

### Assumptions, none yet validated

| # | Assumption | How to check |
|---|---|---|
| A1 | Salons really do require a 48-hour patch test, and it really does conflict with next-day availability | Message five salons as a customer |
| A2 | Escalations really do expire in practice, rather than only in theory | Measure reply gaps in real threads |
| A3 | A human can label these cases consistently enough for the test set to mean anything | Hand-label 20 cases before writing code |
| A4 | Policy in other industries is also expressible as rules | Try one financial services policy set |

A1 is currently modelled on standard practice, not confirmed. A4 is the one that would most
damage the approach if false — returns policy and salon policy are both rule-shaped, but a
mortgage affordability assessment may not be.

### What is missing from the platform

Three things the build has to work around, which only Meta could provide:

**Deliverability, not just a countdown.** Time left in the window is easy to calculate from the
last inbound message. Whether a template will actually send is not — that depends on approval
status, the number's quality rating, and per-user frequency caps. Check 6 needs a yes or no and
has to guess.

**Rejecting a Flow submission before it completes.** Validation currently happens after the
customer has finished the form.

**Tamper-evident records tied to message IDs.** An application-level log records what we did.
It cannot prove it.
