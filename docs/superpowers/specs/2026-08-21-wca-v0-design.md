# WhatsApp Commerce Agent v0 — Design Spec

**Date:** 21 August 2026
**Status:** For review, pre-implementation
**Source:** `docs/PRD-whatsapp-commerce-agent.md`
**Prior art:** `../business-state`. Its clock, id, hold and extraction modules are reused here. See §11.

---

## 1. Goal

A booking agent makes promises the salon then has to keep. This build limits what it is allowed to promise.

The agent never books. It proposes a booking and says which policy rules it relied on. A separate component called the gate checks that proposal. If any check fails, the booking does not happen and a human is told.

v0 has to show seven things work:

1. **Rules live in a file, not in code.** Each rule has a version number. Nothing is executed to evaluate one.
2. **A missing fact is not a "no".** The patch-test rule needs to know if this is a first colour visit. If nobody asked, the answer is unknown. The rule must not read that as "no".
3. **The gate calls no model.** It runs the same way every time, and it can only block.
4. **The gate catches a missed override.** Two rules can apply to one booking. The agent cites the general one and books. The gate blocks it, because the more specific rule applied too.
5. **A booking happens in two steps.** First a hold on the slot, then a commit. A failed booking releases the hold. Committing twice books once.
6. **A late escalation is caught before the booking, not after.** If there is no time left for a human to reply, the gate blocks.
7. **The test set runs the real gate.** The same function scores the offline cases and would run on live traffic.

What v0 does not show: that any of this holds at volume. Twenty test cases cannot support a percentage. §15 says so plainly.

## 2. Scope

**In v0:** one service (colour), five rules, twenty test cases, a mock calendar, all six gate checks, fact extraction through the Anthropic API, a fake transport with a fake clock, an audit record, a test harness, and one live WhatsApp thread.

**Not in v0:**

| Out | Why |
|---|---|
| Taking the deposit | The rule fires. No money moves. PRD §11. |
| Multiple businesses, rescheduling, cancellation, a second language, Flows | PRD §11 |
| A real calendar | v1. The calendar stays mock even on the live thread. |

**About the live thread.** Every automated test runs against the fake transport. The live thread is a demo, not a test. The test suite must pass with no network, no token and no tunnel. §14 splits the exit criteria to keep that true: items 1 to 8 run offline, items 9 to 11 need Meta.

## 3. Stack

Python 3.12, `uv`, Pydantic v2 at every boundary, pytest. The Anthropic API is used for fact extraction and nothing else. The PRD asks for standard library on the default path, and the default path holds to that: rules, gate, calendar and audit import no framework.

The webhook receiver uses FastAPI and uvicorn. It has two endpoints and it has to reply to Meta within a few seconds while real work continues in the background. Writing that concurrency by hand on top of `http.server` is more risk than one dependency at the edge.

The fake transport is a plain Python object with no server at all. That is what makes the fake clock possible.

## 4. Architecture

```
Meta Cloud API
      │  POST, at-least-once, order not guaranteed
      ▼
webhook receiver ── check X-Hub-Signature-256 ──▶ 200 OK immediately
      │                  (reject if it fails)
      ▼
   dedup(message_id) ─▶ per-thread queue, one at a time
                                        │
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
                 fact extraction                  policy store
                 (calls a model)                  (rules file,
                        │                          versioned)
                        └───────────────┬───────────────┘
                                        ▼
                        proposal {action, rules cited}
                                        │
                                hold(slot, ttl)
                                        ▼
                        ╔═══════════════════════════════╗
                        ║  GATE                         ║  calls no model
                        ║  1 rules exist                ║  same result
                        ║  2 facts support them         ║  every time
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

reaper ─▶ releases holds that expired
```

Three rules about the gate. Each has a test that fails if it stops being true.

**The gate calls no model.** `gate.py` does not import anything from `extract/`. A test reads the import graph and fails if it ever does.

**The gate is a pure function.** `evaluate(proposal, facts, ruleset, calendar_view, window_view, now) -> Verdict`. No clients, no writes, no hidden state. Call it twice with the same arguments and you get the same verdict.

**The gate can only say no.** A `Verdict` is either `PASS` or `BLOCK(check, kind, reason)`. There is no third option that carries a corrected booking. Because the gate cannot suggest anything, it cannot be wrong in a way that creates a bad booking.

### 4.1 The webhook edge

Three things the fake transport never needed. Each fails only in production.

**Check the signature.** Meta signs every webhook with your app secret and puts the result in the `X-Hub-Signature-256` header. If we skip that check, anyone who finds the URL can post a fake customer message and get an appointment booked. So the receiver computes HMAC-SHA256 over the raw request bytes and compares it using `hmac.compare_digest`.

Use the raw bytes, not the parsed-and-reserialised JSON. Reserialising changes whitespace and key order, the hash no longer matches, and every real message gets rejected. A request that fails the check is dropped before anything parses it.

A test posts a body with a bad signature and asserts that no conversation state and no proposal were created.

**Reply first, work second.** Meta retries any webhook it does not get a fast answer to. If we wait for a model call, we get retries, and retries mean duplicate messages. So the receiver checks the signature, puts the message on a queue, and returns 200. It never waits for the gate.

Dedup on `message_id` is what makes those retries safe. Do the dedup where the queue is consumed, not at the edge. If it only runs at the edge, several retries can arrive at once and all pass the check before any of them has been recorded.

**The verification handshake.** When you save a callback URL, Meta sends a `GET` with `hub.mode`, `hub.challenge` and `hub.verify_token`. Echo the challenge back, but only if the token matches the one you configured.

The live thread also needs a public HTTPS URL. That is an operations problem, not a code one: a tunnel while developing, a host in v1. It is written down here so nobody discovers it on demo day.

## 5. Rules as data

A rule is a record in a JSON file. Its condition is nested data. A small function walks that data and returns an answer. Nothing is compiled and nothing is executed, so there is no way for a rule to run code.

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

**Operators.** Only these: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `is_true`, `is_false`. **Combinators:** `all`, `any`, `not`. Anything else fails when the file loads.

**Outcomes.** Only these: `allow`, `deny`, `require_lead_time`, `require_deposit`, `require_escalation`.

`requires_facts` is worked out from the condition when the file loads. If the file also lists it by hand and the two disagree, loading fails. This stops the list drifting away from the condition it describes.

### Unknown is a third answer

A condition returns `TRUE`, `FALSE` or `UNKNOWN`. A rule that returns `UNKNOWN` does not fire, and it does not count as satisfied either.

Here is why that matters. The patch-test rule needs `is_first_colour_visit`. Suppose the agent never asked. If `UNKNOWN` collapsed into `FALSE`, the rule would quietly decide this is not a first visit, and the booking would go through. The customer arrives with no patch test. Nothing in the logs would show anything went wrong.

So `UNKNOWN` stays separate all the way through:

- `all` returns `FALSE` if any part is `FALSE`. Otherwise `UNKNOWN` if any part is `UNKNOWN`. Otherwise `TRUE`.
- `any` returns `TRUE` if any part is `TRUE`. Otherwise `UNKNOWN` if any part is `UNKNOWN`. Otherwise `FALSE`.
- `not` swaps `TRUE` and `FALSE`, and leaves `UNKNOWN` alone.

When a rule comes back `UNKNOWN`, the proposer is told which fact is missing. That is how the agent decides to ask a question instead of booking.

### Versions

A `RuleSet` cannot be changed once loaded. Its `ruleset_version` is a hash of the rules inside it. A proposal cites `rule_id@rule_version`. The audit record stores those plus the `ruleset_version` that was live at the time. Editing a rule creates a new version, so old audit records still point at what was actually applied.

### Reading a rule

`rules/render.py` turns any condition into a sentence: `service category is colour AND first colour visit is true`. That sentence goes in the audit record and the rule listing. The salon owner can read their own policy without anyone building a rule language.

## 6. Which rule wins

Check 3 exists for one situation. Two rules apply to the same booking. The agent cites the general one, books, and never notices the specific one.

**The code works out which rule is more specific.** Nobody ranks them by hand. Rule B is more specific than rule A when B needs every fact A needs, plus at least one more, and both come back `TRUE` on the same facts.

That is exactly the patch-test case. The general rule needs `{service_category}`. The patch-test rule needs `{service_category, is_first_colour_visit}`. The second set contains the first, so the patch-test rule wins, and nobody had to remember to say so.

`priority` only settles a genuine tie, where two rules match and neither needs everything the other needs. If such a pair has the same priority, **the rules file fails to load.** A policy that cannot say which of two rules applies is a policy bug. Better to find it when writing the rules than when a customer is booking.

## 7. The six checks

`gate.evaluate` runs the checks in order and stops at the first failure. Every block is tagged `grounding` or `conclusion`, so the two kinds of mistake are counted separately. Grounding means the agent used a rule that does not exist or does not apply. Conclusion means the rules were right and the call was still wrong.

| # | Check | Blocks when | Kind |
|---|---|---|---|
| 1 | Rules exist | A cited `rule_id@version` is not in the ruleset | grounding |
| 2 | Facts support them | A cited rule does not come back `TRUE` on the facts | grounding |
| 3 | No override missed | A rule the agent did not cite comes back `TRUE` and is more specific. Also blocks if a relevant rule comes back `UNKNOWN`. | grounding |
| 4 | Booking cites a rule | A booking cites no rule whose outcome allows it | grounding |
| 5 | Slot still free | The hold is gone, expired, or belongs to another thread | conclusion |
| 6 | Escalation deliverable | Less than the margin left in the window, or no approved template for this reason | conclusion |

Check 3 blocks on `UNKNOWN` on purpose. A fact nobody established, and a rule the agent ignored, produce the same outcome for the customer: an appointment that should not have been made.

Check 5 reads the calendar again rather than trusting the hold it was handed. If the reaper released that hold in the meantime, this is where we find out.

## 8. Calendar and holds

`CalendarPort` is a protocol with five methods: `availability(window)`, `hold(slot, thread_id, ttl, now)`, `commit(hold_id, idempotency_key, now)`, `release(hold_id, reason, now)` and `expire_due(now)`.

`calendar/mock.py` implements it in memory. The behaviour comes from `business-state/reservations.py`:

- One slot takes one hold. The calendar enforces that itself. It is not a check the caller can forget to make.
- A hold expires. A reaper releases expired holds. If the process dies between hold and commit, the slot is stuck until expiry. That is why the TTL exists.
- `commit` takes an idempotency key. Commit twice with the same key and you get the same booking back, not a second one. A retried webhook cannot book twice.
- A refusal says which kind it is: already held, outside opening hours, or no such slot. A caller that cannot tell "taken" from "does not exist" cannot reply sensibly to the customer.

## 9. Messages arriving

**Dedup on `message_id`.** Meta delivers a webhook at least once, which means sometimes more than once.

**One queue per thread, processed one message at a time.** Meta does not guarantee order. Without a queue, two messages in the same conversation could both reach the gate for the same slot and both pass. Threads do not block each other.

`ConversationState` holds the facts gathered so far, the open hold if there is one, and `last_inbound_at`. That timestamp is where the 24-hour window is measured from.

## 10. Fact extraction

The model reads language. The code decides what to trust. Taken from `business-state/extract/`.

The extractor returns a `RawFactSet` shaped by a JSON schema, through `client.messages.parse`. Every field is listed explicitly and can be null, because a strict schema cannot describe a free-form dictionary. The extractor never sees a rule, never cites one, and never proposes a booking. `propose.py` matches facts to rules separately.

Never send `temperature`, `top_p` or `top_k`. They were removed on Claude Opus 5 and return a 400. Model ids are exact and carry no date suffix: `claude-haiku-4-5`, `claude-opus-5`.

`FakeExtractor` replays a script, so the whole test suite runs with no API key.

**One test simulates a compromised extractor.** Script it to return confident facts that an injected model would produce from hostile customer text, then check the gate still blocks. This is a stronger test than checking the schema rejects bad input, and it needs no key.

## 11. Reused from `business-state`

Ported deliberately and reviewed, not copied. Tests come across with the code.

| From | To | What changes |
|---|---|---|
| `clock.py` | `clock.py` | Add `SimulatedClock`. Keep the rule that library code never calls `datetime.now()`. Every function takes `now`. Without that, the 24-hour tests are impossible. |
| `ids.py` | `ids.py` | Almost unchanged. `idempotency_key(source, kind, payload)`. |
| `reservations.py` | `calendar/mock.py` | `Reservation` becomes `Hold`. Stock line becomes slot. Keep the TTL, the reaper and the typed refusals. |
| `log.py` | `audit.py` | Append-only, ignores duplicate keys. Records now carry rule versions. |
| `extract/{base,fake,anthropic}.py` | same paths | `RawCandidate`/`Candidate` becomes `RawFact`/`Fact`. Keep the two-shape split and the structured output. |
| patterns from `schema.py` | `models.py`, `rules/schema.py` | Frozen models, `extra="forbid"`, tagged unions, fields computed rather than passed in. Not the event types. |

**Not reused:** `catalogue.py`, `projection.py`, `state.py`, `health.py`, `answers.py`, `score.py`, and the six modules that generated the synthetic message corpus. That project wrote the truth first and generated messages from it. This one hand-writes twenty cases, which is the right choice at this size.

## 12. The test set

Twenty cases in five groups, from PRD §9: clean, adversarial, override, unanswerable, ambiguous. A case is a thread script, a set of facts, an expected verdict, and one line saying why.

The cases are written and then reviewed. They are not taken from real salons. That is recorded here so nobody treats them as field data.

`harness.py` runs every case twice, gate off and gate on, and prints the KPI table with counts next to every percentage:

| KPI | Target, PRD §7 |
|---|---|
| Bad bookings | 0 |
| Booking rate | 80% or better |
| Escalation precision | 85% or better |
| Escalation deliverability | 100% |
| Gate effect | printed with counts |
| Cost of control | printed, not targeted |

## 13. Escalation and the 24-hour window

The window closes at `last_inbound_at + 24h`. Check 6 needs two things to be true: at least `ESCALATION_MARGIN` left before it closes (2 hours by default, set in one place), and an approved template for this escalation reason.

`escalation.py` keeps a registry of templates by reason, each with its approval status. Quality rating is a field in that registry, hard-coded green in v0. It is there so that turning it on later is a config change rather than a rewrite, and the audit record shows it was not used.

The PRD is clear that Meta does not expose any of this reliably and this check has to guess. This guess covers two of the three inputs the PRD names. It leaves out per-user frequency caps, because nobody outside Meta knows how they behave, and inventing them would produce a check that looks careful while asserting things we made up.

**On the live thread the window is real.** A template that is not actually approved in Meta's console will fail to send, whatever check 6 decided. So seed the registry from the real approval state before demoing. The audit record stores what check 6 believed, so if Meta disagrees you can see it afterwards.

## 14. Exit criteria

Each one is a command whose output gets recorded. None of them is a claim.

Offline. These must all pass, and none of them touches the network:

1. `wca cases` runs all twenty with the gate on. Zero bad bookings.
2. `wca cases --gate-off` runs the same twenty. At least one bad booking. If removing the gate changes nothing, it is not doing anything.
3. The patch-test case blocks at check 3. It also blocks in the variant where nobody established whether it was a first colour visit.
4. An escalation raised at hour 23 fails check 6 under the fake clock. The same escalation at hour 2 passes.
5. The same `message_id` delivered twice books once. The same idempotency key committed twice returns the first booking.
6. A hold orphaned by a crash gets released by the reaper and the slot frees up.
7. The gate is model-free: the import test passes, and calling `evaluate` twice with the same arguments gives the same verdict.
8. Every audit record names `rule_id@version` and the `ruleset_version` in force at the time.

On the live thread. Demonstrated by hand. Not needed for a green test run:

9. One real WhatsApp conversation goes from question to booked appointment with no human involved, and the audit record names the rules the gate checked.
10. A webhook with a bad `X-Hub-Signature-256` is rejected, and no conversation state is created. Shown by posting a forged body at the endpoint.
11. The patch-test case blocks on the live thread and reaches a human while the window is still open.

## 15. What this does not prove

| Claim | Why not |
|---|---|
| These percentages hold at volume | Twenty cases. One case moves a percentage by five points. Counts are printed. The targets point in a direction, nothing more. |
| Salons require a 48-hour patch test | PRD assumption A1. Based on standard practice and not confirmed with any salon. Check 3, the override cases and the main demo all depend on it. |
| The test cases match real salon policy | Written then reviewed, not observed. PRD assumption A3. |
| Escalations expire in practice | A2 is unmeasured. The fake clock proves the mechanism works. It says nothing about how often it happens. |
| Policy in other industries is rule-shaped | A4 is untested, and it is the assumption that would hurt most if it turns out false. |
| This works on WhatsApp at scale | One live conversation shows the path works once. It does not test two customers at the same time, retry storms, a rejected template, or quality-rating throttling. |
| Check 6 predicts what Meta will do | It checks time and template approval. Meta also weighs quality rating and frequency caps, which are not visible to us. A send that fails after check 6 passed is a finding, and the audit record is written so you can see it happened. |

## 16. Files

```
whatsapp-commerce-agent/
  policy/salon.rules.json          five rules, versioned
  cases/v0.cases.json              twenty cases
  prompts/extract-v0.1.md          versioned, never written inline
  src/wca/
    clock.py            ids.py            models.py
    rules/              schema.py  evaluate.py  specificity.py  render.py  store.py
    calendar/           base.py    mock.py
    conversation/       state.py   queue.py   dedup.py
    extract/            base.py    fake.py    anthropic.py
    propose.py          gate.py    escalation.py    audit.py
    transport/          base.py    fake.py    whatsapp.py    webhook.py
    harness.py          cli.py
  tests/
```

`gate.py` imports only `models`, `rules`, and read-only views of the calendar and the window. It imports nothing from `extract`, `transport` or `conversation`. That is what makes the import test possible.

`transport/whatsapp.py` talks to the Cloud API: send text, send buttons, parse an inbound payload. `transport/webhook.py` is the FastAPI app: signature check, the GET handshake, fast reply, queue. Those two files are the only ones that know WhatsApp exists, which is what lets everything else run against the fake transport.
