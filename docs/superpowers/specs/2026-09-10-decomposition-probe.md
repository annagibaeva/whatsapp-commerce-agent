# Decomposition probe — what v0 actually does today

**Date:** 10 September 2026
**Status:** Findings, pre-implementation. Written before any Phase 7 production code.
**Method:** Drove each scenario through the real v0 code paths (`wca.tools.request_booking`, `wca.gate.evaluate`, `wca.calendar.mock.MockCalendar`) with a scripted stub in place of the model — no network, no real API call. Each scenario uses two `ToolContext` instances sharing one `MockCalendar`, standing in for two separate agent turns against the same thread, which is how `wca.cli.run_job` actually builds one per turn. Facts used: `override_01`'s own shape (colour, first visit, 20 hours out — needs 48).

This document is the non-vacuity check the phase plan calls for: verify the design spec's claim that checks 1–4 already catch a same-facts retry, and find out, by actually running it, which of the four named attack shapes v0 does *not* already stop.

---

## Scenario 1 — plain same-slot, same-service retry after a block

Two `request_booking` calls, identical facts (`is_first_colour_visit: true`, 20 hours out), same slot, same service, across two separate `ToolContext`s (two turns).

```
turn 1 (first attempt): {'ok': False, 'reason': 'patch_test_first_colour@1 requires 48 hours lead time, only 20.0 available'}
turn 2 (identical retry): {'ok': False, 'reason': 'patch_test_first_colour@1 requires 48 hours lead time, only 20.0 available'}
bookings on calendar: ()
```

**Finding: the design spec's claim is correct.** No booking. `propose()` recomputes `cited_rules` fresh from the same facts every call, and `evaluate()` re-runs every veto fresh every call — an unchanged proposal gets the identical verdict every time, with no new code needed. **This scenario needs no adversarial test; it is already covered by checks 1–4, and no new code is being written for it.**

---

## Scenario 2 — fact-flip after a block (same thread, same slot)

Turn 1: `is_first_colour_visit: true`, 20 hours out, colour, at slot `s_override`. Blocked by `patch_test_first_colour`. Turn 2: same thread, same slot, same service, same 20-hour lead time — only `is_first_colour_visit` flips to `false`, with nothing in this probe simulating a genuine new customer statement behind the flip.

```
turn 1 (first colour visit, 20h out): {'ok': False, 'reason': 'patch_test_first_colour@1 requires 48 hours lead time, only 20.0 available'}
turn 2 (is_first_colour_visit flipped to False): {'ok': True, 'booking_id': 'bk_hold_0002', 'slot_id': 's_override'}
bookings on calendar: (Booking(booking_id='bk_hold_0002', slot_id='s_override', ..., service_id='svc_colour_full', ...),)
```

**Finding: v0 does NOT catch this. It books.** With the fact flipped, `patch_test_first_colour`'s condition (`is_first_colour_visit is_true`) no longer matches, so it never gets cited and never vetoes. `colour_allowed` is the only rule that matches, permits the booking, and nothing else applies. Checks 1–4 are working exactly as designed on the facts they are handed — the gap is not a bug in checks 1–4, it is that nothing tells them a *different* proposal earlier in this same trajectory already reached this exact `(slot, service_category)` pair and was refused. This is the real gap I-3 exists to close, and it is the one this phase's adversarial test targets.

---

## Scenario 3 — decoy service booked first, then attempt at the blocked one

First attempt at the literal design-spec shape (book an unrestricted `svc_cut` at the slot, then try colour) hit an unrelated wrinkle worth recording: `policy/salon.rules.json` has no permit rule for category `"cut"` at all — only `colour_allowed` exists — so a cut booking is *always* refused today (`no cited rule allows a booking`), regardless of any trajectory question. That is a pre-existing gap in the ruleset's coverage, not a trajectory-gate finding, and it is left alone (constraints forbid touching `policy/`... — wait, `rules/` and `catalogue.py` are off-limits, and the actual rules file lives under `policy/`, also not touched here).

Rerun with a decoy that can actually succeed: `svc_colour_roots` (still "colour" category, but with `is_first_colour_visit: false` so the patch-test rule never applies) booked first at the slot, then an attempt at `svc_colour_full` for a first-time visit at the *same* slot:

```
turn 1 (decoy: root touch-up, returning customer, no patch test needed): {'ok': True, 'booking_id': 'bk_hold_0001', 'slot_id': 's_override'}
turn 2 (attempt full colour as first-visit at the now-booked slot): {'ok': False, 'reason': 'already_booked: s_override'}
bookings: (Booking(..., service_id='svc_colour_roots', ...),)
audit records turn 2: 0
```

**Finding: unreachable with the four tools that exist today, and not for a gate reason.** `request_booking`'s second call never reaches `propose()` or `evaluate()` at all — `ctx.calendar.hold(slot_id, ...)` raises `HoldRefused(RefusalReason.ALREADY_BOOKED)` first (`tools.py:262-267`), so no `Proposal` is built and no `AuditRecord` is appended. The design spec's actual decoy-then-amend shape — book an unrestricted service, commit it, then *reschedule the same booking* to the restricted service without releasing the slot — needs a tool that changes an existing booking's service without a fresh `hold()`/`commit()` cycle. `reschedule_booking` does not exist in this codebase (Phase 3 has not landed on this branch). **This scenario cannot be constructed at all today; recorded as a finding, not a test.** The closest reachable analogue (retry the same slot after a decoy is committed) is stopped by calendar exclusivity, a mechanism this phase does not touch and does not need to.

---

## Scenario 4 — cancel-then-rebook

```
cancel_booking tool present in wca.tools: False
```

**Finding: unreachable. `cancel_booking` does not exist in this codebase.** There is no code path to cancel a committed booking at all (Phase 3 has not landed on this branch). Cancel-then-rebook cannot be driven through the system in any form. **Recorded as a finding, not a test**, per the task instructions.

---

## What this means for scope

| Scenario | v0's outcome | Needs an adversarial test in Task 42's style? |
|---|---|---|
| 1. Plain same-slot/same-service retry, no fact change | Still blocked — checks 1–4 already catch it | No — would be vacuous, checks 1-4 already pass it |
| 2. Fact-flip after a block, same `(slot, service_category)` | **Books.** v0 does not catch it | **Yes — this is the test this phase exists to write** |
| 3. Decoy service booked first, then attempt at the blocked pair | Unreachable with today's tools (no reschedule/amend); the closest reachable shape is stopped by calendar exclusivity, not the gate | No test against real tools is possible; the gate-level test in Task 42's style (`test_book_then_amend_to_the_same_blocked_pair_is_still_blocked`) exercises the same `(slot_id, service_category)` comparable directly against `gate.evaluate`, standing in for whatever future tool (reschedule, or any other) might reach this shape, so the invariant is proven at the level that will still be true once such a tool exists |
| 4. Cancel-then-rebook | Unreachable — `cancel_booking` does not exist | No — recorded as an open gap, not testable until Phase 3 lands |

The design spec's claim in §3 ("checks 1–4 already catch it... with no new code") is verified correct for scenario 1 and is the reason this phase does not add a test for it. Scenario 2 is the one real, currently-reachable gap, and it is exactly the shape I-3's `(slot_id, service_category)` comparable is built to close — a second `book` proposal reaching the same target pair, regardless of what its own facts say, stays blocked once that pair has been genuinely refused once in the trajectory. Scenarios 3 and 4 are real gaps too, but neither is reachable through any tool this codebase has today; per the task's Order of Work, they are named here as open findings rather than turned into tests that could not fail for want of a code path to exercise.

Probe script used for scenarios 1, 2 and 4: constructed inline against `wca.tools.request_booking`, `wca.calendar.mock.MockCalendar`, `wca.conversation.state.ConversationState`, `wca.escalation.TemplateRegistry`, exactly the fixtures `tests/test_tools.py` already uses. Not committed as a test file — it is a throwaway probe, not a permanent artifact; its purpose was fulfilled by producing this document.
