# WhatsApp Commerce Agent v0 — Design Spec

**Date:** 21 August 2026
**Status:** For review, pre-implementation
**Source:** `docs/PRD-whatsapp-commerce-agent.md`
**Prior art:** `../business-state` — a completed prototype whose clock, id, hold and extraction modules are harvested here (§11)

---

## 1. Goal

Prove that a booking agent can operate a calendar on WhatsApp without the deploying business accepting unlimited risk from what the agent commits to.

The agent proposes; it never commits. A deterministic gate checks each proposal against written policy and live calendar state and blocks anything it cannot verify. Blocked cases go to a human, with time left for that human to reply.

v0 must demonstrate seven properties:

1. Rules are data, versioned, and evaluated without running code.
2. A rule that needs a fact the conversation lacks **cannot fire** — the agent knows when it does not know.
3. The gate makes no model call, is deterministic, and can only block.
4. The gate catches a booking that looks individually correct but ignores a more specific rule.
5. Bookings are two-phase: a hold is released on failure, a commit is idempotent.
6. An escalation raised too late to be answered is caught **before** the booking commits.
7. The same gate code scores the offline test set and would gate live traffic.

**It does not demonstrate that these numbers hold at volume.** Twenty cases cannot support a rate. §14 states this plainly.

## 2. Scope

**In:** one service category (colour), five rules, twenty test cases, a mock calendar with TTL holds and a reaper, the gate with all six checks, fact extraction via the Anthropic API, a fake transport with a simulated clock, an audit record, and a harness that runs the cases with the gate off and on.

**Out, deliberately:**

| Out | Why |
|---|---|
| **The live WhatsApp thread** | Needs a Meta test number, app and verified webhook. None is available, and the last project's central claim went unproven because a credential gap surfaced at the checkpoint rather than at design time. `transport/whatsapp.py` defines the port and the message shapes but is **not wired**, and nothing in the exit criteria depends on it. |
| Taking the deposit | PRD §11. The rule fires; no money moves. |
| Multiple businesses, rescheduling, cancellation, second language, Flows | PRD §11 |
| Live calendar integration | v1 |

Bringing the live thread into scope later is additive: implement `TransportPort` against the Cloud API and point the runner at it. No other module changes.

## 3. Stack

Python 3.12, `uv`, Pydantic v2 for every boundary model, pytest. Anthropic API for fact extraction only. Standard library on the default path, per PRD §10.

No web framework in v0 — there is no live webhook to receive. The fake transport is a Python object, which is what makes the simulated clock possible.

## 4. Architecture

```
webhook ─▶ dedup(message_id) ─▶ per-thread serial queue
                                        │
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
                 fact extraction                  policy store
                 (model call; never               (rules as data,
                  in the booking path)             versioned)
                        │                               │
                        └───────────────┬───────────────┘
                                        ▼
                        proposal {action, cited_rules@version}
                                        │
                                hold(slot, ttl)
                                        ▼
                        ╔═══════════════════════════════╗
                        ║  GATE                         ║  no model call
                        ║  1 rules exist                ║  deterministic
                        ║  2 facts support them         ║  pure function
                        ║  3 no override missed         ║
                        ║  4 booking cites a rule       ║
                        ║  5 slot still free            ║
                        ║  6 escalation deliverable     ║
                        ╚═══╤═══════════════════════╤═══╝
                        PASS│                   BLOCK
                            ▼                       ▼
                   commit(idem_key)          release() ─▶ human
                            └───────────┬───────────┘
                                        ▼
                                  audit record

reaper ─▶ releases expired holds
```

Three properties, each with a test that fails if it stops being true:

**The gate makes no model call.** `gate.py` imports nothing from `extract/`. Enforced by a test that asserts the module's import graph is free of the extraction and network packages.

**The gate is a pure function.** `evaluate(proposal, facts, ruleset, calendar_view, window_view, now) -> Verdict`. It takes no clients, mutates nothing, and returns a verdict — never a corrected proposal. Called twice with the same arguments it returns an equal verdict.

**The gate can only block.** `Verdict` is `PASS` or `BLOCK(check, kind, reason)`. There is no variant carrying an alternative action, so the gate structurally cannot supply an answer and therefore cannot introduce a mistake of its own.

## 5. Rules as data

A rule is a record. Conditions are nested data walked by a closed operator set — there is no string parsing and no `ast`, so there is no evaluation surface to get wrong.

```json
{
  "id": "patch_test_first_colour",
  "version": 3,
  "condition": {"all": [
    {"fact": "service_category", "op": "eq", "value": "colour"},
    {"fact": "is_first_colour_visit", "op": "is_true"}
  ]},
  "requires_facts": ["service_category", "is_first_colour_visit"],
  "outcome": {"type": "require_lead_time", "hours": 48, "reason": "patch test"},
  "priority": 0,
  "source_text": "First-time colour clients must patch test 48 hours before their appointment."
}
```

**Operators, closed set:** `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `is_true`, `is_false`. **Combinators:** `all`, `any`, `not`. Anything else fails at load.

**Outcome types, closed set:** `allow`, `deny`, `require_lead_time`, `require_deposit`, `require_escalation`.

`requires_facts` is derived at load from the condition tree and validated against any hand-supplied list, so it cannot drift from the condition it describes.

### Three-valued evaluation — the load-bearing detail

A condition evaluates to `TRUE`, `FALSE` or `INDETERMINATE`. A rule whose condition is `INDETERMINATE` **does not fire, and is not treated as not applying.**

This is how the agent knows when it does not know. Collapsing `INDETERMINATE` into `FALSE` would mean the patch-test rule silently fails to apply whenever the agent simply never established whether it was a first colour visit — producing exactly the unsafe booking this system exists to prevent, with no signal anywhere.

Propagation: `all` is `FALSE` if any child is `FALSE`, else `INDETERMINATE` if any child is, else `TRUE`. `any` is `TRUE` if any child is `TRUE`, else `INDETERMINATE` if any child is, else `FALSE`. `not` maps `TRUE`↔`FALSE` and leaves `INDETERMINATE` unchanged.

An `INDETERMINATE` rule that would otherwise be relevant is surfaced to the proposer as a missing fact, which is how the agent decides to ask a question instead of booking.

### Versioning

`RuleSet` is immutable and content-addressed: `ruleset_version` is a hash of its rules. A proposal cites `rule_id@rule_version`; the audit record stores both plus the `ruleset_version` live at the time. Editing a rule produces a new version rather than rewriting history (PRD §11).

### Readability

`rules/render.py` turns any condition into English — `service category is colour AND first colour visit is true` — used in the audit record and the rule listing, so policy remains readable to the person who owns it without a DSL existing anywhere in the trust path.

## 6. Specificity and override

Check 3 catches a booking that is individually defensible but ignores a more specific rule.

**Specificity is derived, not declared.** Rule B is more specific than rule A when `B.requires_facts` is a strict superset of `A.requires_facts` and both evaluate `TRUE` against the same facts. `{service_category}` versus `{service_category, is_first_colour_visit}` is exactly the patch-test case, caught structurally rather than by anyone remembering to rank a rule.

`priority` breaks genuine ties — two matching rules where neither's fact set contains the other's. When such a pair has equal priority, the ruleset **fails to load**, because a policy that cannot decide between two applicable rules is a policy bug and should surface at author time, not at booking time.

## 7. The six checks

`gate.evaluate` runs them in order and returns on the first failure. Each block is labelled `grounding` (an invented or misapplied rule) or `conclusion` (the right rules, the wrong call), so the two failure types are counted separately.

| # | Check | Fails when | Kind |
|---|---|---|---|
| 1 | Rules exist | A cited `rule_id@version` does not resolve in the ruleset | grounding |
| 2 | Facts support them | Re-evaluating a cited rule against the facts is not `TRUE` | grounding |
| 3 | No override missed | An uncited rule evaluates `TRUE` and is strictly more specific than a cited one — **or** a relevant rule is `INDETERMINATE` | grounding |
| 4 | Booking cites a rule | A commit action cites no rule whose outcome permits it | grounding |
| 5 | Slot still free | The hold is missing, expired, or belongs to another thread | conclusion |
| 6 | Escalation deliverable | Remaining window < margin, or no approved template exists for this escalation reason | conclusion |

Check 3 covering the `INDETERMINATE` case is deliberate: an unestablished fact that would have triggered an override is indistinguishable, from the customer's side, from an override that was ignored.

Check 5 re-reads calendar state at gate time rather than trusting the hold token, so a hold released by the reaper between proposal and gate is caught.

## 8. Calendar and holds

`CalendarPort` is a protocol: `availability(window)`, `hold(slot, thread_id, ttl, now)`, `commit(hold_id, idempotency_key, now)`, `release(hold_id, reason, now)`, `expire_due(now)`.

`calendar/mock.py` implements it in memory. Semantics harvested from `business-state/reservations.py`:

- One slot, one hold, enforced as a **hard constraint in the calendar service** — not an optimistic check a later write might lose.
- TTL-bounded, released by a reaper. A hold is a lease, not a lock: a crash between hold and commit orphans the slot, and only expiry recovers it (PRD §11).
- `commit` takes an idempotency key. Committing the same key twice is a no-op returning the original booking, so a redelivered webhook cannot book twice.
- Refusals carry **distinct reasons** — already held, outside opening hours, no such slot — because a caller that cannot tell "gone" from "never existed" cannot respond correctly.

## 9. Conversation, dedup and ordering

**Dedup on `message_id`,** backed by an idempotency store, because webhooks are delivered at least once.

**One serial queue per thread,** because webhook order is not guaranteed and two messages in one thread could otherwise both pass the gate for the same slot. Serialisation is per `thread_id`; different threads proceed concurrently.

`ConversationState` holds the thread's accumulated facts, its open hold if any, and `last_inbound_at` — the timestamp the 24-hour window is measured from.

## 10. Fact extraction

The model does language; the code does trust. Harvested wholesale from `business-state/extract/`.

The extractor returns a `RawFactSet` constrained by a JSON schema via `client.messages.parse` — every field explicit and nullable, because a strict schema cannot express a free-form dict. It never sees a rule, never cites one, and never proposes an action. `propose.py` matches facts against the ruleset separately.

**No sampling parameters are ever sent** (`temperature`, `top_p`, `top_k` were removed on Claude Opus 5 and return a 400). Model ids are exact strings with no date suffix: `claude-haiku-4-5`, `claude-opus-5`.

`FakeExtractor` replays scripted facts, so the entire suite runs offline with no API key.

**A hostile-extractor test is required**, harvested in spirit from the prior project's trust-boundary trace: script the extractor to return confident facts that a successfully-injected model would emit from adversarial customer text, and assert the gate still blocks. This is stronger than asserting the schema refuses something, and it runs without a key.

## 11. What is harvested from `business-state`

Deliberate port with review, not copy-paste. Each is adapted, and its tests come with it.

| From | To | Change |
|---|---|---|
| `clock.py` | `clock.py` | Add `SimulatedClock`. Keep the rule that **library code never calls `datetime.now()`** — every function taking `now` explicitly is the precondition for testing 24-hour expiry at all. |
| `ids.py` | `ids.py` | Near-verbatim. `idempotency_key(source, kind, payload)`. |
| `reservations.py` | `calendar/mock.py` | Rename `Reservation`→`Hold`, stock line→slot. Keep TTL, reaper, hard constraint, distinct refusal reasons. |
| `log.py` | `audit.py` | Append-only, idempotent on key. Records now carry rule versions. |
| `extract/{base,fake,anthropic}.py` | same paths | `RawCandidate`/`Candidate` becomes `RawFact`/`Fact`. Keep the two-shape bridge and structured output. |
| `schema.py` **patterns** | `models.py`, `rules/schema.py` | Frozen models, `extra="forbid"`, discriminated unions, fields **derived rather than supplied**. Not the event types. |

**Not harvested:** `catalogue.py`, `projection.py`, `state.py`, `health.py`, `answers.py`, `score.py`, and the six-module synthetic corpus generator (`truth`, `render`, `distractors`, `noise`, `adversarial`, `generate`). The prior project generated its corpus labels-first; this one hand-labels twenty cases, which is deliberately the inverse and correct at this size.

## 12. The test set

Twenty cases in five tiers, per PRD §9: **clean, adversarial, override, unanswerable, ambiguous.** Each case is a thread script plus scripted facts plus an expected verdict, with a one-line rationale.

They are **drafted then reviewed**, not observed. The spec records this so nobody mistakes them for field data.

`harness.py` runs every case twice — gate off, gate on — and emits the KPI table with counts beside every rate:

| KPI | Target (PRD §7) |
|---|---|
| Bad bookings | 0 |
| Booking rate | ≥ 80% |
| Escalation precision | ≥ 85% |
| Escalation deliverability | 100% |
| Gate effect | published with counts |
| Cost of control | reported, not targeted |

## 13. Escalation and the window

`window_closes_at = last_inbound_at + 24h`. Check 6 requires `window_closes_at - now >= ESCALATION_MARGIN` (default 2 hours, defined once) **and** that an approved template exists for the escalation reason.

`escalation.py` holds a template registry keyed by reason, each with an approval status. **Quality rating is modelled as a field, stubbed green in v0.** It is present so that turning it on later is not a retrofit, and the audit record shows it was not consulted.

The PRD is explicit that the platform exposes none of this reliably and the check has to guess. This spec's guess models two of the three named inputs and says so; it does not model per-user frequency caps, because their behaviour is opaque and inventing it would produce a check that looks rigorous while asserting fiction.

## 14. Exit criteria

Each is a command whose output is recorded, not a claim.

1. `wca cases` runs all twenty with the gate **on**: zero bad bookings.
2. `wca cases --gate-off` runs the same twenty: at least one bad booking, proving the gate is load-bearing. **A gate whose removal changes nothing is not a control.**
3. The patch-test override case blocks at check 3, and blocks for the `INDETERMINATE` variant where the first-visit fact was never established.
4. An escalation raised at hour 23 of the window fails check 6 under the simulated clock; the same escalation at hour 2 passes.
5. A redelivered `message_id` produces no second booking; a repeated `commit` with the same idempotency key returns the original.
6. A hold left by a crash is released by the reaper and the slot becomes available again.
7. The gate is provably model-free: the import-graph test passes, and calling `evaluate` twice with identical arguments returns equal verdicts.
8. Every case's audit record cites `rule_id@version` and the `ruleset_version` live at the time.

## 15. What this cannot claim

| Claim | Why not |
|---|---|
| These rates hold at volume | Twenty cases. One case moves a rate five points. Counts are reported; the targets are directional. |
| Salons require a 48-hour patch test | PRD assumption A1, modelled on standard practice and **unconfirmed**. Check 3, the override tier and the headline failure story all rest on it. |
| The test cases reflect real salon policy | Drafted then reviewed, not observed (A3). |
| Escalations expire in practice | A2 is unmeasured. The simulated clock proves the *mechanism*, not the frequency. |
| Policy in other industries is rule-shaped | A4 untested, and the one that would most damage the approach if false. |
| The agent works on WhatsApp | The live transport is out of scope for v0 and unwired. Everything runs against the fake transport. |

## 16. Module layout

```
whatsapp-commerce-agent/
  policy/salon.rules.json          five rules, versioned
  cases/v0.cases.json              twenty cases
  prompts/extract-v0.1.md          versioned, never inline
  src/wca/
    clock.py            ids.py            models.py
    rules/              schema.py  evaluate.py  specificity.py  render.py  store.py
    calendar/           base.py    mock.py
    conversation/       state.py   queue.py   dedup.py
    extract/            base.py    fake.py    anthropic.py
    propose.py          gate.py    escalation.py    audit.py
    transport/          base.py    fake.py    whatsapp.py (port only, unwired)
    harness.py          cli.py
  tests/
```

`gate.py` depends only on `models`, `rules`, and read-only views of calendar and window state. It imports nothing from `extract`, `transport` or `conversation`, which is what makes the purity test enforceable rather than aspirational.
