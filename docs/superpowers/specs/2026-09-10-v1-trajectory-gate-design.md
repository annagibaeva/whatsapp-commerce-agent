# WCA v1 — Trajectory Gate Design Spec

**Date:** 10 September 2026
**Status:** For review, pre-implementation
**Source:** `whatsapp-v1-plan-and-act-spec.md`, corrected against the repo at HEAD
**Prior art:** `2026-08-21-wca-v0-design.md` §7 (the six checks), §4 (the gate's three invariants). This document extends both, not restates them.

---

## 1. Goal

v0's gate checks one proposal. It has no memory of any other proposal in the same conversation. That was fine when the agent proposed once per turn and stopped. It stops being fine the moment the agent can retry.

It can already retry. `agent.run_turn` loops up to `MAX_ITERATIONS = 8` model round-trips in a single turn, and the model chooses which of four tools to call, in what order, as many times as the iteration cap allows. Nothing about that loop is new in v1 — it is what v0's tool addendum already built. What v1 adds is a gate that remembers what happened earlier in the same trajectory, so a blocked outcome cannot be reached twice by two different routes.

This document is the design spec for that gate extension. It sits where `2026-08-21-wca-v0-design.md` sat for v0: between the PRD-level plan-and-act spec and the task-by-task plan (`docs/superpowers/plans/2026-09-09-wca-v1.md`, Phase 7). It says what was decided, what was rejected, what the failure modes are, and what catches each one.

## 2. The loop that already exists

Verified against `src/wca/agent.py` and `src/wca/tools.py` at HEAD, not assumed from the spec.

- **Tool choice is the model's.** `Agent.run_turn` sends the four `TOOL_SPECS` (`search_catalogue`, `check_availability`, `request_booking`, `escalate`) on every call. The model decides which to call and in what order; nothing in `agent.py` sequences them.
- **The loop is bounded, not single-shot.** `while response.stop_reason == "tool_use" and iterations < MAX_ITERATIONS`, `MAX_ITERATIONS = 8`. A turn can already contain several `request_booking` attempts before the cap stops it — v0's `_booked_this_turn` flag on `ToolContext` only stops a *second successful* booking, not a second *attempt*.
- **History accumulates every tool result**, including a block. `run_turn` appends `{"role": "user", "content": tool_results}` after every round, and `tool_results` is built straight from `dispatch(...)`'s return value — for a blocked `request_booking`, that is `{"ok": False, "reason": verdict.reason}`, the gate's own block reason, verbatim (`tools.py:319`). The model sees exactly why it was refused and can act on that text in its next tool call. This is the channel a decomposition attack uses: the model reads the refusal, then tries something the refusal itself didn't rule out.
- **Two tools mutate state.** `search_catalogue` and `check_availability` are read-only by their own docstrings. `request_booking` holds, gates, and commits-or-releases the calendar, and always appends to `ctx.audit`. `escalate` never touches the calendar but always appends to `ctx.audit` too. Those are the two tools a trajectory gate has to watch; the other two cannot produce an outcome worth remembering.

Nothing above is new work. It is the mechanism this phase's gate extension has to fit into, stated precisely so the next sections can say exactly what it cannot see.

## 3. What a per-action gate cannot see, and why that's structural

Checks 1–4 (`gate.py`, unchanged) already re-run in full on every fresh proposal. `propose()` recomputes `cited_rules` from `matching_rules(ruleset, facts)` every call; `evaluate()` re-runs every veto every call. So a same-slot, same-service retry with unchanged facts is already caught today, with no new code: the second proposal looks exactly like the first one, and the first one was blocked for a reason that still holds.

What checks 1–4 cannot see is a blocked *end state* reached by a *different path*, where each individual step's proposal is well-formed and would pass in isolation:

- **Cancel-then-rebook.** Cancel an existing booking, then book the same slot again with one fact changed (`is_first_colour_visit: false`, say). The second `request_booking` call is a clean proposal on its own facts. Checked alone, it passes checks 1–4 — there is nothing in that one proposal that says a related attempt was blocked five minutes earlier.
- **Decoy service, then amendment.** Book an unrestricted service (a trim) at the slot that would trip the patch-test rule for colour, commit it, then reschedule it to colour. Each half is individually well-formed: booking a trim needs no patch test, and a reschedule call carries its own facts.

A per-action gate cannot close this by adding a seventh check to `evaluate`, because the information it is missing is not about the proposal in front of it — it is about a *different* proposal in the same conversation. "Per-action" means the function only ever sees the action currently being evaluated. No amount of scrutiny applied to one action can recover a fact that lives in a sibling action's history. The only fix is giving the gate something that spans actions: a trajectory.

## 4. `Trajectory`

```python
class FactEntry(_Strict):
    key: str                         # always a CONVERSATIONAL_FACTS name
    value: Any
    source_message_id: str
    established_at: datetime

class Trajectory(_Strict):
    thread_id: str
    plans: tuple[Plan, ...] = ()         # every revision, oldest first, never dropped
    proposals: tuple[Proposal, ...] = ()
    verdicts: tuple[Verdict, ...] = ()   # verdicts[i] is the verdict for proposals[i]
    fact_ledger: tuple[FactEntry, ...] = ()
    tool_calls: int = 0
```

**What it holds.** Every plan revision the planner has written for this thread, every proposal `request_booking`/`escalate` built, the verdict each one got, and a ledger of conversational facts with the message that established them.

**What it deliberately does not hold.** `DERIVED_FACTS` — `hours_until_appointment`, `requested_weekday`, `service_category`, `quoted_price_minor` — never enter `fact_ledger`. Those are recomputed fresh from the slot and catalogue on every `request_booking` call (`tools.py:230–246`); a ledger entry for them would be tracking a value that is already deterministic given `(slot, now)`, which adds bookkeeping without adding a guarantee. The ledger exists to answer one question — did a fact *the customer* established change value without a new message behind it — and scoping it to `CONVERSATIONAL_FACTS` is what keeps that question well-formed.

**Why in-memory is the right first call, not durable-first.** A trajectory has to outlive a conversation — several turns, possibly hours apart — but it does not have to outlive a process restart to do its job in this slice. The property this phase exists to prove (I-3 catching a decomposition attack) is provable entirely in memory, inside one turn or across turns within one running process. Durability is a wiring task, not a design question: it is one more Sqlite table with the same save/reload shape every other store in this codebase already has. Building it first would spend effort on the part of this phase nobody else has already proven works, before spending any on the part nobody else has built at all.

## 5. I-3, precisely

**What "blocked end state" means as a comparable value.** The pair `(slot_id, service_category)` a `book` or `reschedule` proposal targets. Nothing richer — not the whole fact set, not the tool sequence that produced it. Kept mechanical on purpose: a comparison that tried to reason about *why* two attempts were "the same" would itself be a judgment call the gate is not supposed to make.

**How a proposal is tested against earlier verdicts.** If this trajectory's history contains an earlier `book`/`reschedule` proposal targeting the same `(slot_id, service_category)` pair, and that earlier proposal's verdict was a genuine block — not `OVERRIDE_MISSED`'s "might apply" noise, an actual grounding or conclusion refusal — the new proposal is blocked too, regardless of which tool produced it, how many steps came between, or what the new proposal's own facts say.

```python
if trajectory is not None and proposal.action.type in ("book", "reschedule"):
    target = (proposal.action.slot_id, proposal.facts.get("service_category"))
    for earlier_proposal, earlier_verdict in zip(trajectory.proposals, trajectory.verdicts):
        if earlier_verdict.allowed or earlier_proposal.action.type not in ("book", "reschedule"):
            continue
        if (earlier_proposal.action.slot_id, earlier_proposal.facts.get("service_category")) == target:
            return Verdict.blocked(GateCheck.BLOCKED_END_STATE, BlockKind.EVASION, ...)
```

**What it is concrete about.** `override_01`'s shape — colour, first visit, 20 hours out, blocked needing 48 — stays blocked at that same `(slot, "colour")` pair for the rest of the trajectory, even if a later attempt arrives with `is_first_colour_visit` flipped to `false` and no new fact behind the flip, and even if it arrives through `reschedule_booking` instead of `request_booking`.

**What it deliberately does not catch.** A genuinely different slot is a genuinely new attempt — `(other_slot, "colour")` is a different pair and is evaluated on its own merits, because a customer legitimately asking about a different, farther-out slot is not evasion. It also does not catch a fact flip on its own — that is I-2's job, not built in this slice's I-3 (see §9). And it says nothing about a blocked pair becoming bookable later for a real reason (the 48 hours actually elapsing) — this slice does not distinguish "still blocked because nothing changed" from "would legitimately un-block with time," because within one bounded trajectory that distinction does not arise: `hours_until_appointment` for a fixed slot only moves in one direction as `now` advances within a turn.

## 6. `BlockKind.EVASION`

A third member alongside `GROUNDING` and `CONCLUSION`. `GROUNDING` means the agent cited a rule that does not exist or does not apply — a reasoning mistake about policy. `CONCLUSION` means the rules were right and the call was still wrong — a mistake in the world (the slot filled, the window closed). `EVASION` is neither: it means the planner reached a state this trajectory already refused, by a different route.

Counting it separately matters because averaging it into the other two hides two different failures. A planner with a high grounding-block rate is reasoning badly about policy — it needs better prompting or a better ruleset render. A planner with a high evasion-block rate is not reasoning badly, it is rattling the door: retrying a refusal with cosmetic changes instead of accepting it. Those call for different fixes, and a single blended block rate cannot tell you which one you have. `BUDGET_EXHAUSTED` (I-4) stays `CONCLUSION`, not `EVASION` — running out of budget honestly, without ever having reached a blocked end state twice, is not the same failure as evading one.

## 7. The gate's invariants that survive unchanged

- **No model call.** `gate.evaluate` still imports nothing from `extract/`, `transport/`, or `conversation/`. The purity test (`tests/test_gate_purity.py`) extends its banned-prefix list to cover the new persistence modules (`wca.db`, `wca.calendar.sqlite`, `wca.calendar.live`, `wca.calendar.ledger`) rather than being replaced — a trajectory gate that could reach a database directly would be exactly as dangerous as one that could reach a model.
- **Can only block.** `Verdict` gains no new field. A trajectory-aware block is still `Verdict.blocked(check, kind, reason)`.
- **No field carries a corrected action.** Same reasoning as v0 §4: because the gate cannot suggest anything, it cannot itself cause a bad booking. Adding trajectory awareness only adds ways to say no, never a way to say "book this instead."
- **One production caller of `CalendarPort.commit`.** At HEAD, that is `tools.py::request_booking` (`harness.py`'s own `calendar.commit(...)` call is the offline test harness building `AuditRecord`s directly from `propose()`/`evaluate()`, not a second production path). This phase adds no second way to write to the calendar — the planner's freedom is in *which* of the four tools to call and how many times, never in a new tool that reaches `commit` directly.

## 8. Decisions

### Decision 1 — Facts stay code-derived; the I-1 provenance ledger is not built

The spec's I-1 proposes a traced ledger: the planner assembles the facts it cites, and a provenance check verifies where each one came from. This repo does not give the planner that ability in the first place, so there is nothing for a ledger to trace.

Two fixes already in this codebase prove why that is the right call. `propose()` (`propose.py:31`) builds `cited_rules` itself, in code, from `matching_rules(ruleset, facts)` — the model has never chosen which rules get cited. And `request_booking` (`tools.py:230–246`) pops any model- or conversation-supplied `hours_until_appointment` and `requested_weekday` and recomputes both from the slot's own `starts_at`, with the fix comment naming the exact bugs this closed: a model-supplied lead time let a first-colour booking through, and a model-supplied weekday booked a Sunday. Those are not hypothetical risks I-1 would guard against — they are bugs this repo already had and already fixed, by removing the channel rather than by adding a check on it.

**Rejected:** a checked provenance ledger. A structural guarantee — there is no code path for the planner to supply a fact — beats a traced one, because a traced guarantee can be wrong in a way a structural one cannot: a ledger has to be populated correctly on every write, checked correctly on every read, and kept in sync as new facts are added. A missing code path needs none of that maintenance and cannot silently stop being enforced.

### Decision 2 — No `Plan` object gates tool use in the first slice

The spec's plan-theatre risk (§8 of the plan-and-act spec) assumes a `Plan` object exists and a model could write one and then ignore it. Planning is already implicit in tool choice today: the model's sequence of `search_catalogue` → `check_availability` → `request_booking` calls *is* its plan, expressed as actions rather than as a declared object.

**Rejected:** building `Plan`/`PlanStep` and a `step_id`-checking dispatch wrapper in this slice. A plan-theatre guard solves a problem that does not exist until a plan object does — there is no plan to ignore if there is no plan to write. That guard belongs with the `Plan` object itself, when the plan-and-act loop is built (plan Task 45), not in the trajectory gate this document specs.

### Decision 3 — In-memory before durable

Inverts the obvious order: normally you would want the trajectory to survive a crash before you trust it with anything else. Here, the property worth proving first is I-3 catching a decomposition attack that a per-action gate cannot see by construction — that is the one thing nobody else in this portfolio has built, and it is fully provable within a single process, across turns, with no database at all.

**Rejected:** building `SqliteTrajectoryStore` before I-2/I-3/I-4. Durability is correct and demonstrates nothing new — this codebase already has the save/reload pattern for four other stores (audit, conversation, dedup, escalation), so adding a fifth proves the pattern generalizes, not that the gate works. Sequencing it first would spend the harder, more interesting part of this phase's budget on infrastructure that is well-understood, and leave the actually novel part — I-3 — under-tested by comparison. A trajectory that has to outlive a conversation still only has to outlive a process for this slice; outliving a restart is a real requirement for v1 as a whole, met by Phase 1's durable-state work, not skipped, just not first.

### Decision 4 — I-3 only in the first slice, not I-2 or I-4

Of the three invariants a per-action gate structurally cannot check (I-2 fact stability, I-3 blocked end-state, I-4 budget), this slice builds I-3 alone. I-3 is what closes the gap named in §3 — the one no other check in this codebase, old or new, closes by accident. I-2 and I-4 are real and worth building, but neither is the thing this phase exists to prove:

- **I-4 (budget)** bounds a resource, not a safety property. `MAX_REVISIONS`/`MAX_TOOL_CALLS` stop a runaway loop, but a loop that stops without exhausting the budget is not thereby safe — I-3 is what makes it safe regardless of how many attempts it gets.
- **I-2 (fact stability)** catches a flip-after-block directly, but §5's I-3 already blocks the *end state* a flip is usually aimed at reaching. A flip that doesn't change the `(slot, service_category)` pair — a customer genuinely revising an unrelated fact — is not something I-3 touches, and is not something this slice claims to catch either.

**Rejected:** shipping all three together. Uncovered by this choice: a fact flip that changes a proposal's outcome *without* the `(slot, service_category)` pair having been blocked before — for example, a first attempt that never got far enough to be evaluated as `book` at all, so nothing is in `trajectory.verdicts` yet to compare against. That gap is real, named here rather than discovered later, and closed by I-2 in a follow-on slice, not this one.

## 9. The risk this document exists to name

A loop whose purpose is converting blocks into bookings sits directly on top of a gate whose purpose is blocking. Every replan in the plan-and-act loop is, by design, "try again" — and the same mechanism that makes the agent useful (it doesn't give up on the first refusal) is exactly the mechanism a decomposition attack rides. This is the whole risk surface of this phase in one sentence.

For each risk below, the test that catches it — not the intention that avoids it. This repo has repeatedly found tests that could not fail; "how would we know" is the question every entry here has to answer.

| Risk | Test that catches it | How it could still pass vacuously, and the check against that |
|---|---|---|
| The planner books the blocked pair via a different tool sequence (cancel-then-rebook, decoy-then-amend) | `test_book_then_amend_to_the_same_blocked_pair_is_still_blocked` — records a block, then a second proposal at the same `(slot, service_category)` via a *different* `action.type`, with facts that would pass checks 1–4 on their own | Comment out the I-3 block entirely and re-run: the second attempt must now pass through and book. If it still doesn't, the test was never exercising I-3 |
| A fact flip alone (no path change) evades the gate | `test_a_flipped_conversational_fact_is_blocked` (I-2, deferred — see Decision 4) | Not covered by this slice. Named as an open gap, not silently assumed closed |
| I-3 is too broad and blocks a legitimately different attempt | `test_a_different_slot_is_a_genuinely_new_attempt_not_blocked_by_i3` | A version of I-3 keyed on `thread_id` alone instead of `(slot_id, service_category)` would fail this test by over-blocking; the test exists specifically to catch that over-broad shape |
| The trajectory gate looks like it's doing something but isn't | Control arm: `trajectory_gate_on=False` runs the identical adversarial cases with I-2/I-3/I-4 disabled | If the control arm also shows zero evasions, the case set stopped exercising the new checks — see §11 |
| Budget exhaustion silently delivers a best-effort booking instead of escalating | `test_budget_exhaustion_forces_an_escalation` | Set `MAX_TOOL_CALLS` absurdly high locally and confirm the forced-escalation path never fires for a bounded run — proves the path only triggers because the budget was actually spent |
| A stale `step_id` from a dropped plan revision gets dispatched anyway | Deferred with Decision 2 — no `Plan`/`step_id` guard exists in this slice, so no claim is made here either | N/A this slice |

**The canary: `override_01` must never book, at any attempt count, by any path.** Colour, first colour visit, 20 hours out — blocked today at `booking_cites_rule` because `patch_test_first_colour` needs 48. This slice's obligation is that no sequence of tool calls within the budget reaches a committed booking for that customer at that slot, no matter how many revisions the planner tries or which tool it tries them through. Every other case in this document is in service of that one property holding.

## 10. Eval: outcome-level to trajectory-level

v0 scored one proposal against one expected outcome per case. That stops being the right unit once a case can retry. Two changes:

**Attempted-but-blocked becomes a headline number, not a diagnostic.** A planner with a 4% block rate and one with a 60% block rate can land on the identical final booking. They are not the same product — one is well-behaved, the other is rattling every door it finds — and a metric that only reports the final outcome cannot tell them apart. Reporting it as a headline, next to goal completion, is what keeps a "looks fine at the end" trajectory from hiding a planner that spent the whole conversation testing the gate's edges.

**A control arm is mandatory.** The claim "the trajectory gate caught N evasions" is meaningless without a run of the same adversarial cases against a per-action-only gate (`trajectory=None`, I-2/I-3/I-4 disabled) to compare against. Without that second run, N could be N because the cases were designed around what the trajectory gate happens to catch, and the number says nothing about whether the trajectory gate is doing more than checks 1–6 already did. `trajectory_gate_on=False` is the same-shape control v0 already runs for the per-action gate itself (`wca cases --gate-off`) — this is that pattern applied one level up, not a new idea.

## 11. What v1 does not do

- No fact-provenance ledger (I-1) — structurally unnecessary, per Decision 1.
- No `Plan`/`step_id` plan-theatre guard — nothing to guard yet, per Decision 2.
- No durable trajectory store in this slice — in-memory proves the property; durability is wiring, deferred per Decision 3.
- No I-2 (fact stability) or I-4 (budget) enforcement in the gate itself — named as open gaps in Decision 4, not silently assumed covered.
- No claim about volume, generalization beyond the four adversarial cases named here, or behaviour on a real salon's traffic. Same posture v0 took in its own §15: what is proven here is proven on a small, hand-written, adversarial set, not observed in the field.
