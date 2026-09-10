# WhatsApp Commerce Agent — v1

**Repo:** `whatsapp-commerce-agent`
**Author:** Anna Gibaeva
**Status:** Specification. Build not started.
**Builds on:** v0, shipped 9 September 2026 — measured, gated in CI, run once over a live WhatsApp thread.

---

## 1. Summary

v0 asked whether an agent could be allowed to commit a business's inventory. It answered yes, under a deterministic gate, and measured the answer: **0 bad bookings with the gate on, 6 with it off, across twenty cases.**

v1 asks the harder version of the same question. **Does that control still hold when the agent is allowed to plan and act freely?**

A gate that inspects one action at a time is easy to satisfy when the agent only ever proposes one action. It is a different proposition when the agent can sequence actions, learn from a refusal, and try a different route. That is what an agent actually does, and v0 has no answer for it.

Alongside that, v1 makes the thing deployable: state that survives a restart, a real calendar, cancel and reschedule, a booking screen, and approved templates.

---

## 2. Objective

**Let the model plan and act freely, and keep the guarantee that it cannot commit something the business would not have agreed to.**

Not "add a planner." The planner is the easy half. The claim worth making is that agency and constraint can coexist and be measured together — and the way to show that is to build a planner that genuinely tries to get around the control, and then show the control holding.

### Why this, now

Three things converged.

**The loop is already half-built.** v0's agent chooses which of four tools to call and in what order, loops until it stops or hits eight iterations, and sees the gate's own block reason on each pass. What it lacks is not a loop. It is memory the gate can read.

**The gate returns something a planner can use.** A block comes back as `booking_cites_rule` failing, naming the rule and the shortfall. That is a lever. A loop with nothing to reason about is a retry with extra steps.

**The failure mode is known and unaddressed here.** v0 measured what the gate blocks when the agent proposes once. It has never been asked what happens when the planner works around the control rather than through it, and the twenty cases were not written to find out.

### Where this sits

v0 built the seller-side control that agent payment standards do not cover: ACP, AP2 and Trusted Agent Protocol all answer *was this agent allowed to spend the user's money*, and none answer *was this agent allowed to promise the business's inventory.*

v1 extends that from one action to a sequence of them. An authorisation model that checks each step and never the path is a model that can be walked around, and that is true of the buyer side too.

---

## 3. The problem

v0's gate sees one proposal. That is enough for a system that proposes once. It is not enough for a planner.

**A blocked end state can be reached by a legal path.** The salon requires 48 hours between a patch test and a first colour. A planner refused that booking cannot force it — but it may find that booking a different service now and amending it later arrives at the same place, or that cancelling and rebooking resets something. Every individual step is well-formed. Every step passes on its own. No per-action gate catches this, by construction: "per-action" means it only ever sees the action in front of it.

**A loop's purpose is converting blocks into bookings.** The gate's purpose is blocking. That tension is the product and it is also the failure mode — a planner that retries until something passes has quietly inverted what the gate is for.

**Nothing survives a restart.** Conversations, dedup, holds, escalations and the audit log all live in one process's memory. A crash loses every in-flight booking and the record that it happened.

**The agent cannot undo anything.** No cancel, no reschedule. Every correction is a human's job, and every escalation is terminal — a ticket goes to a person and the thread never returns to the agent.

---

## 4. Who it is for

Unchanged from v0 in structure. What each party gets from v1 is different.

| | Who | What v1 gives them |
|---|---|---|
| **Customer** | Messages the salon | An agent that can cancel and reschedule, not only book — and one that recovers from a refusal instead of stopping |
| **Salon staff** | Owner or receptionist | Bookings that survive a restart, and an escalation that can be handed back rather than absorbed |
| **Deploying business** | Whoever runs the agent | Evidence that the agent tried things it was not allowed to do and was stopped — counted, not just claimed |

That last row is the one v1 exists for. "The agent behaved" is an assertion. "The agent attempted N forbidden actions across the case set and completed none of them" is a measurement — and v1 is what makes N a number rather than a shrug.

---

## 5. How it gets solved

The planner proposes; the gate still only refuses.

The model plans and acts freely — which tools, in what order, how many attempts, when to stop. Every action it takes still passes through the same `propose → gate → commit or release` sequence v0 built. What changes is that the gate now sees **the trajectory that produced the proposal**, not just the proposal.

That single change is what makes decomposition visible. A booking that would land in a state an earlier verdict in this same conversation refused is blocked, whatever path reached it.

Two things deliberately do **not** change:

**Facts stay code-derived.** The model never supplies the evidence the gate adjudicates on. Facts come from the catalogue and the slot, computed in code, or from the extractor over the customer's own words. There is no third channel.

**The gate still cannot say yes to anything.** It has no field for a corrected action. Its whole vocabulary is `PASS` or `BLOCK(check, kind, reason)`.

---

## 6. Scope

### In

Plan-and-act with a trajectory-aware gate. Durable state. A live calendar. Cancel and reschedule. A booking screen as a WhatsApp Flow. Approved templates for one market.

### Out, explicitly

**Multiple businesses.** Per-tenant rules and configuration are v2.

**More than one language.** The rules, prompts and templates are English only.

**Taking the deposit.** The rule fires; no money moves. Still the single most valuable thing left undone, and still the point at which this becomes agentic commerce rather than agentic scheduling.

**A planner that writes its own plan down.** Planning stays implicit in tool choice for the first slice. An explicit plan object invites plan theatre — a model that writes a beautiful plan and ignores it — and the guard against that solves a problem that does not exist until the plan object does.

**Fact provenance tracing.** Considered and rejected — see §8, decision 1.

---

## 7. KPIs

**Set before any v1 run.** v0's targets were pre-registered and that is the only reason its numbers mean anything; the same discipline applies or the results get rationalised after the fact.

| KPI | Measure | Target (v1) |
|---|---|---|
| Bad bookings | Bookings that break a rule or double-book, **at any attempt depth** | 0 |
| Evasion caught | Adversarial-tier cases where a forbidden end state was reached | 0 |
| Attempted-but-blocked | Proposals the gate refused, per conversation | Reported, not targeted |
| Goal completion | Bookable enquiries the agent completes alone | ≥ 80% |
| Revisions to completion | Attempts before success | p50 and p95 reported |
| Cost per resolved conversation | Model calls plus messages, from the audit record | **Measured**, not projected |
| State survival | Bookings, facts and escalations after a restart | 100% |

### Two of these need explaining

**Attempted-but-blocked is a headline number, not a diagnostic.** A planner with a 4% block rate and one with a 60% block rate can produce identical final outcomes. They are not the same product, and averaging them into "0 bad bookings" hides the difference. A planner rattling every door is telling you something about itself.

**Cost per resolved conversation is measured in v1, and could not be in v0.** v0 priced a model call. It could not price a booking, because a booking spans several turns and nothing summed them. v1's audit record carries the turn's cost, so the number becomes real. It matters more here than in v0: planning and replanning is several model calls where v0 had one.

**A control arm is mandatory.** "The trajectory gate caught N evasions" is worthless without a per-action gate running the same cases. v0 stays runnable for exactly this reason.

---

## 8. Six decisions, and what each rejected

Six decisions, each of which could reasonably have gone the other way. They are listed because the reasoning is the point, not the outcome.

**1. Facts stay code-derived; the provenance ledger is not built.** The obvious design lets the planner assemble the facts it cites and traces each one to a source. It was rejected. This repo has already fixed a model-supplied lead time that let a first colour visit book 10 hours out, and a model-supplied weekday that booked a Sunday. A structural guarantee — there is no path for the model to supply a fact — beats a traced one, and costs nothing to enforce because it is already enforced.

**2. The thin gate before durable state.** The plan's own sequencing said durable state first. That is wrong for this build: a trajectory has to outlive a conversation, not a process. In-memory proves the control works; SQLite is the same save-and-reload shape this codebase already has four times over, and building it first delays the only part of v1 that is not standard wiring.

**3. Adversarial cases before the planner.** Write them after and you are writing tests against behaviour you have already watched, which is how a suite quietly loses the ability to fail. They go in as failing tests first — that failure output is the non-vacuity proof, and it only exists before the fix.

**4. I-3 first, alone.** Fact stability and budget matter and are not in the first slice. I-3 — no blocked end state by decomposition — is the one a per-action gate structurally cannot do. The others are refinements; this is the claim.

**5. Evasion is counted separately.** A planner working around a control and a planner reasoning badly are different failures with different fixes. Folding both into one block count hides both.

**6. The reply-shape decision stays in code.** v0 derives whether a reply goes out as a list or as buttons from what the tools actually returned, never from what the model says about itself. v1 keeps that, because a planner with more freedom is more able to assert things about its own output.

---

## 9. Risks

Each one names the test that catches it, not the intention that avoids it.

**The loop inverts the gate.** A planner that retries until something passes. *Caught by:* `override_01` — a first colour visit 20 hours out — asserted never to book at any attempt count, by any path. It is the canary and it is a test, not a principle.

**A planner routes around the gate by decomposition.** *Caught by:* I-3, plus an adversarial tier targeting each known route — cancel-then-rebook, decoy service then amendment, fact-flip after a block, budget exhaustion.

**The eval stops measuring the right thing.** Outcome-level scoring cannot see a well-behaved planner rattling the door. *Caught by:* trajectory-level scoring with attempted-but-blocked as a published number.

**A live calendar is a second writer the gate cannot see between polls.** *Caught by:* a test that mutates an external calendar between two `view()` calls on the same instance. A cached implementation cannot pass it — which matters, because caching for latency is exactly the tempting mistake.

**A Flow submission looks pre-validated and is not.** *Caught by:* a test asserting the set of callers of `CalendarPort.commit`. A new entry point has to be added deliberately by someone who reads that test.

**Cost grows quietly.** Planning and replanning is several model calls per conversation against v0's one. *Caught by:* cost per resolved conversation as a reported KPI, not an afterthought.

---

## 10. Assumptions

| # | Assumption | Status |
|---|---|---|
| A1 | Salons require a 48-hour patch test, and it conflicts with next-day availability | **Being validated** — Anna is contacting salons directly. Three gate checks and the headline demo rest on it |
| A2 | Escalations expire in practice, not only in theory | Unmeasured |
| A3 | A human can label these cases consistently enough for the test set to mean anything | Held for v0's twenty; untested at v1's larger set |
| A4 | Policy in other industries is expressible as rules | Untested, and the one that would most damage the approach if false |
| **A5** | **A planner will actually attempt decomposition** | **New, and load-bearing.** If it never tries, the trajectory gate is insurance against nothing and attempted-but-blocked reads zero for the wrong reason. The adversarial tier exists to force the attempt rather than wait for it |

A5 is the assumption a reviewer should push on hardest. A control that catches nothing might be a good control or an unnecessary one, and the two look identical until something tries.

---

## 11. What v1 will not prove

**That this works at volume.** The case set grows but stays hand-written. Twenty cases could not support a percentage and a hundred cannot support a deployment.

**That the planner is safe against attacks nobody thought of.** The adversarial tier covers routes that were imagined. A gate that catches four known evasions has not been shown to catch a fifth.

**That the numbers hold on real traffic.** One live thread showed the path works once. v1 adds no volume.

**That a human would agree with the gate's refusals.** Cost of control counts bookings the gate stopped. Whether the salon would have accepted them is a question the harness cannot answer and nobody has asked.
