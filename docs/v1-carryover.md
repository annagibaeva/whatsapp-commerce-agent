# What v0 leaves behind, and what it costs to run

**9 September 2026.** Two things: the open items migrating from v0 into v1, and a cost model for running this at volume.

---

## Part 1 — Carried into v1

The project artifact listed eight open items. Four were closed on 9 September. Four migrate, plus one that is not a code problem.

### Closed, not carried

| Item | How |
|---|---|
| Model tiering declared but not implemented | Real escalation built: haiku first, one retry on opus when extraction fails to parse. Capped at two attempts. Tests fail if the escalation is removed. |
| The eval gated nothing | CI now reads the harness exit code on every push — and checks **both** directions, so a case set that stops exercising the gate fails the build instead of looking like success. |
| No written rule for "the gate is working" | Stated in the README: 0 bad bookings with the gate on, at least one with it off. Recorded as written *after* v0's runs, so it binds what comes next. |
| Attempts not logged, so first-pass and retried extractions were indistinguishable | `model` and `attempts` now on every extraction result. |

### Carried into v1

**1. Cost per booking is still not measurable from real traffic.** Extraction cost is now computed per call. The agent's own calls — up to eight per turn — capture no usage at all, and nothing persists the number anywhere. Until both are fixed, every figure in Part 2 is a model, not a measurement. *This is the one I would do first, because it is the input to every commercial decision below.*

**2. Escalation is a dead end.** A ticket goes to a human and the thread never comes back to the agent. There is no return path. Every escalation is therefore a permanently lost automation, which makes escalation look more expensive than it needs to be.

**3. Cost of control is 0 across four bookable cases.** Four is too few to claim the gate never blocks a good booking. This needs more cases before the number means anything, and it is the figure most likely to move.

**4. Escalation precision can be gamed by a component that is not the gate.** The two-ask cap converts extractor failures into escalations. A worse extractor produces more escalations that all score as "correct", because a human genuinely did have to step in. Measuring this honestly needs a control arm, not a before-and-after.

### Not a code problem

**Assumption A1 is still unvalidated.** The 48-hour patch test rule is modelled on standard practice, and no salon has confirmed it. Check 3, the override cases and the headline demo all rest on it. The PRD's own suggested test — message five salons as a customer — is a morning's work and would retire the largest untested assumption in the build.

---

## Part 2 — What it costs to run

**Every figure here is PROJECTED.** Published API rates are exact; token counts are estimated from measured prompt sizes. Nothing has been measured against real traffic — that is carryover item 1. Replace this table with measured numbers as soon as it lands.

### Rates and assumptions

| | |
|---|---|
| Extraction and agent model | `claude-haiku-4-5` — $1.00 / $5.00 per million tokens in/out |
| Extraction retry model | `claude-opus-5` — $5.00 / $25.00 per million |
| Extraction prompt | 652 tokens (measured from `prompts/extract-v0.1.md`) |
| Agent system prompt | 1,213 tokens (measured from `prompts/agent-v0.1.md`) |
| Tool definitions | ~400 tokens, resent every iteration |
| Agent loop | 1–3 model calls per turn, capped at 8 |

**Prompt caching does not apply.** Both prompts run on Haiku 4.5, whose minimum cacheable prefix is 4,096 tokens. At 652 and 1,213 tokens they are far below it, and caching fails *silently* below the minimum — no error, just no saving. Padding prompts to reach the threshold would cost more than it saves.

### Cost per completed booking — model calls only

| Scenario | Turns | Model calls | $ / booking |
|---|---:|---:|---:|
| **Fast** — knows what they want | 2 | 7 | **$0.0148** |
| **Typical** — a question or two, then books | 5 | 17 | **$0.0375** |
| **Hard** — one garbled message, ends in escalation | 8 | 26 | **$0.0628** |

### At volume — model calls only

| Bookings / month | Fast | Typical | Hard |
|---:|---:|---:|---:|
| 100 | $1.48 | $3.75 | $6.28 |
| 1,000 | $14.85 | $37.54 | $62.78 |
| 10,000 | $148.49 | $375.41 | $627.82 |
| 100,000 | $1,484.90 | $3,754.10 | $6,278.20 |

### The finding that matters commercially

**The model is not the expensive part. The messaging is.**

WhatsApp bills per message. A typical booking is about ten messages. So model cost is exceeded by messaging cost at any per-message rate above:

| Scenario | Model $ | Messages | Break-even $/message |
|---|---:|---:|---:|
| Fast | 0.0148 | 4 | **$0.0037** |
| Typical | 0.0375 | 10 | **$0.0037** |
| Hard | 0.0628 | 16 | **$0.0039** |

Published WhatsApp utility rates are above $0.0037 in essentially every market — check Meta's current pricing for the markets you care about rather than taking a number from here, since they vary several-fold by country and change. But the direction is not in doubt: **at scale, this is a messaging bill with a model bill attached, not the other way round.**

Three consequences:

**Turn count is the cost lever, not token count.** Every question the agent asks is a billable message on top of a model call. The PRD says this already — "an agent that asks six questions costs more than one that asks two" — and this is the arithmetic behind it. The fast scenario costs a quarter of the hard one in model terms and a quarter as many messages.

**That makes the interactive messages built on 9 September a cost feature, not a polish feature.** Offering slots as a tappable list instead of prose replaces a typed answer — and often a clarifying round trip — with one tap.

**And it changes what the escalation dead end costs.** An escalation spends the whole conversation and returns nothing automatable. With no path back to the agent, every escalated thread is paid for twice: once in messages and model calls, once in staff time.

### What would change these numbers

- **Measured token counts** replacing estimates (carryover item 1). Estimates could be out by a factor of two either way.
- **From 1 October 2026**, service messages and in-window utility messages become billable, which raises the messaging side further and sharpens every point above.
- **A cheaper conversation shape.** Fewer turns is worth more than a cheaper model here.
