# SDD ledger — plan: docs/superpowers/plans/2026-08-21-wca-v0.md

Spec: docs/superpowers/specs/2026-08-21-wca-v0-design.md (binding authority)
Branch: v0-implementation (off master @ 9475072)

## Preflight scan

| Tasks | Interface | Finding |
|---|---|---|
| 1 -> all | clock.utc, hours_between, SimulatedClock, ids.idempotency_key | Clean. Serial prerequisite for everything. |
| 2 -> 3,4,8,10 | Rule, RuleSet, Condition, facts_used | Clean. |
| 3 -> 4,8,10 | Tri, evaluate_rule, missing_facts | Clean. |
| 4 -> 8,10,17 | is_more_specific, matching_rules, unknown_rules, load_ruleset, to_english | Clean. |
| **5 -> 8,9,16** | models.Verdict, Proposal, Action | **Task 5's Interfaces block says "Consumes: rules.schema.Outcome" but the code it specifies imports only pydantic. models.py is standalone.** See Ruling 1. |
| 6 -> 8,16 | MockCalendar.view returns a plain dict | Clean. That dict shape is what lets the gate take views, not objects. |
| 7 -> 8,16 | window_view returns a plain dict | Clean. |
| 8 -> 11,16 | evaluate(proposal, ruleset, calendar_view, window_view) | Clean. Same 4 params in Tasks 8, 11, 16. |
| 10 -> 16 | propose(...) | **Ordering constraint, documented in the plan:** if the proposer changes which rules it cites, Task 16's expected verdicts in cases/v0.cases.json may need updating. Plan says fix the case file, not the gate, unless the gate is genuinely wrong. |
| 15 -> 16,17 | Case.would_be_bad_booking | Clean. Task 15 asserts at least one case is marked, which is what stops --gate-off passing trivially. |
| 18 -> 19,20 | InboundMessage, OutboundMessage | Clean. |
| 1,17,19,20 | **cli.py** | Created in 17, modified in 19 and 20. Both are in the SAME wave-3 agent (F handles 19,20; E handles 16,17). See Ruling 3. |

Per-task self-consistency: all 21 checked. Task 5's Interfaces line is the only mismatch found.

## Rulings

Ruling 1: Task 5's "Consumes: rules.schema.Outcome" is wrong; models.py is standalone. Treat Task 5 as depending only on pydantic, which makes it parallelisable in wave 1.
— Cost if wrong: if models.py does turn out to need Outcome, wave 1 agent B blocks on agent A. Both are in worktrees, so a rebase fixes it.

Ruling 2: Four parallel agents in separate git worktrees for wave 1. Two implementers committing into one .git/index collide on index.lock; worktrees remove that. File sets are disjoint by construction.
— Cost if wrong: a merge conflict. All four tracks are additive on distinct packages.

Ruling 3: cli.py is created in Task 17 and modified in Tasks 19 and 20. Wave 3 splits as E=(16,17) and F=(19,20), so agent F must NOT edit cli.py. It records its intended subcommand snippets in its report, and Task 21 wires `send` and `serve` during integration.
— Why: the same shared-file conflict that broke the parallel map on the previous project. Caught at preflight this time.
— Cost if wrong: `wca send` and `wca serve` are unavailable until Task 21. Nothing offline depends on them.

Ruling 4: Wave plan — 0: Task 1 serial. 1: A=(2,3,4) B=(5,6,7) C=(12,13) D=(14,15,18), four worktrees. 2: Tasks 8,9,10,11 serial in the main tree, ending at the checkpoint. 3: E=(16,17) F=(19,20). 4: Task 21.
— Cost if wrong: if wave 1 tracks do collide, one rebases.

## Progress

Task 1: complete (commit 2f36d02, 8 passed)
Track B: Tasks 5,6,7 complete (19cc347, 52366ee, 0d260a7), merged. 31 passed.
Track B: Ruling 1 CONFIRMED correct — models.py imports nothing from wca.rules, so it was safe to parallelise.
Track B: minor (deferred): Task 6's brief lists `Slot` as a produced type but no such class exists in its code or tests; slots are plain strings. Flag if a later task needs a real Slot type.
Track B: minor (deferred): MockCalendar.release() on an unknown hold_id is a silent no-op rather than a HoldRefused. Matches the brief and tests.
Track A: Tasks 2,3,4 complete (b43324e, d7ec7b0, 7bb3bd7), merged. 75 passed.
Track A: found a real bug in the Task 4 brief. My check_decidable skipped any pair whose fact sets differed, so the brief's own disjoint-fact-set test could never raise. Two required tests failed. Agent narrowed the raise to fully-disjoint fact sets, which makes the tests pass and keeps the real policy loading. It flagged that this catches unrankable ties but not conflicting outcomes.

Ruling 5: check_decidable is about CONFLICTING OUTCOMES, not fact-set shape. Both my original and the agent's fix are wrong in different directions.
— Two rules matching together with compatible outcomes is normal and correct. The real policy does it: patch_test_first_colour (require_lead_time) and deposit_over_threshold (require_deposit) both fire for a first-time customer spending over the threshold, and both should. Raising on that would reject the actual policy.
— Fully-disjoint fact sets is an arbitrary criterion. Two rules sharing no facts are about different things and are LESS likely to conflict than overlapping ones.
— The real hazard: `no_colour_on_sunday` is `deny` with facts {requested_weekday, service_category}. Against `patch_test_first_colour` {is_first_colour_visit, service_category} neither is a superset, both have priority 0, and both match for a first-time customer asking for Sunday. One says deny, the other says require_lead_time, and NOTHING ranks them. Same for deposit_over_threshold and under_16_needs_guardian. Three unranked deny-versus-permit pairs sit in the shipped policy today and neither version of the check sees them.
— Fix: raise when two rules can both match, neither is more specific, priorities are equal, and one outcome is `deny` while the other permits. Then give no_colour_on_sunday a higher priority so deny wins, which is what propose.py already assumes with "deny beats escalate beats ask beats book". The data then agrees with the code.
— Cost if wrong: a policy author must set a priority whenever a deny can co-apply with a permit. That is friction in exactly the place friction is worth having.
Track D: Tasks 14,15,18 complete (a45f057, 6c3bb15, 0538943), merged.
Track D: environment finding — uv fails with a hardlink error (os error 396) in worktrees under OneDrive. Fix: export UV_LINK_MODE=copy. Pass this to any later worktree agent.
Track D: concern for Task 20 — DedupStore and ThreadQueue are built but not wired. The dedup check must run INSIDE the job that ThreadQueue.drain() executes, not before submit(). The plan already says this; carry it into Task 20's dispatch.
Track D: minor (deferred): TransportPort requires only send_text/send_buttons; FakeTransport.receive/inbox are simulation helpers outside the port contract.
Track C: Tasks 12,13 complete (6ff005c, d0e7b79), merged. Wave 1 done, 113 passed.
Track C: found TWO self-defeating tests in my briefs. Task 12's prompt file must contain "not an instruction", but my prompt text line-wrapped that phrase across two lines so its own grep test failed. Task 13's module docstring explained that temperature/top_p/top_k must never be sent, and the test greps the source for exactly those words, so the docstring failed the test it was documenting. Both reworded without changing meaning.
Track C: minor (deferred): `FactSet` is listed in Task 12's Interfaces but never defined or tested. Left out.
Ruling 5 fix: commit ff54247, 99 passed. Hazard script found exactly the three predicted deny-vs-permit pairs before the fix and none after. no_colour_on_sunday is now priority 10, version 2.

Ruling 6: Ruling 5's version bump breaks a Task 8 test. tests/test_gate.py as written cites every rule at version 1, but no_colour_on_sunday is now version 2, so test_check_4_blocks_a_booking_whose_only_rule_denies_it would fail check 1 (rule not found) instead of check 4. Carry into wave 2: cite that rule at version 2. This is the correct behaviour, not a workaround — a stale version reference SHOULD fail check 1, which is exactly what check 1 is for.
— Cost if wrong: if a later task hardcodes version 1 again, check 1 fires and the failure looks like a gate bug rather than a stale citation.
Wave 2: Tasks 8,9,10,11 complete (f753fdb, 7e6f5ee, 44e5106, 73073c6). 139 passed.
Wave 2: agent found my Task 11 transitive-import test was itself broken — it read the whole process's sys.modules, which false-fails in a full run because test_anthropic_extractor.py loads anthropic at collection time. Fixed by measuring in a fresh subprocess. Re-verified the deliberate break still catches an injected import in isolation and in the full suite.
CONTROLLER-VERIFIED: I independently injected `import httpx` into gate.py (after the __future__ line, so it parses) and both purity tests went red — the ast check and the transitive check. Removing it returns 139 green, tree clean. The purity claim is falsifiable, not decorative.

Ruling 7: proceed past the Task 11 checkpoint without waiting. The checkpoint exists to validate the gate's interface before anything is built on it, and that validation is done: six checks tested, the verdict type carries no corrected action, and the purity test is proven able to fail. Waiting adds no information.
— Cost if wrong: if Verdict or evaluate()'s signature needs changing after wave 3, the harness and the webhook need a fix round. Both consume the gate through a narrow interface, so the blast radius is small.
Track F: Tasks 19,20 complete (ef4b85a, b642d0b), merged. 154 passed in the main tree.
Track F: deliberate-break check PASSED — making verify_signature return True unconditionally turned exactly the two forged-body tests red (both expected 403, got 200) and left everything else green. Signature verification is falsifiable, not decorative.
Track F: cli.py untouched per Ruling 3. The `send` and `serve` snippets are in track-f-report.md ready to paste in Task 21.

Ruling 8: the purity test's subprocess check is environment-fragile and must be fixed in Task 21. It runs `subprocess.run([sys.executable, "-c", "import wca.gate ..."], check=True)`, which inherits the environment but NOT pytest's `pythonpath = ["src"]` setting. So it only works when the package is installed in the venv. Track F's worktree had not been synced, `import wca.gate` failed, check=True raised, and the test reported a failure that had nothing to do with gate.py — the agent reasonably called it "pre-existing and unrelated".
— Fix: pass `src` explicitly on PYTHONPATH in the subprocess env so the test measures what it claims regardless of install state.
— Why it matters: a fresh clone that runs the suite before `uv sync` sees a red purity test and no hint why. That is an hour of someone's life, and it makes the project's headline claim look broken when it is not.
— Cost if wrong: none material. Passing PYTHONPATH explicitly is strictly more robust than inheriting it.
Track E: Tasks 16,17 complete (89ebfb1, 1db64eb), merged. 163 passed.
Track E: gate on -> 0 bad bookings (n=20, exit 0). Gate off -> 2 bad bookings (n=20, exit 1). The gate is load-bearing.
Track E: changed 9 of 20 case expectations and FLAGGED the reason rather than hiding it. That flag is the most valuable output of the whole implementation.

Ruling 9: CRITICAL. The gate never enforces `require_lead_time`, so the patch-test case books. I reproduced it directly: propose() cites BOTH colour_allowed@1 and patch_test_first_colour@1, check 3 finds no uncited rule so it does not fire, check 4 sees colour_allowed permits and lets it through. The 48-hour requirement is evaluated and then ignored. This is the PRD's opening example and the demo's entire spine.
— Root cause is in my SPEC, not the plan. Spec section 7 defines check 4 as "a commit action cites no rule whose outcome permits it". That only asks whether something permits. It never asks whether a cited rule imposes a requirement that is unmet. The spec assumed the agent would MISS the specific rule; my proposer cites every matching rule, so there is never a gap for check 3 to find.
— Fix: strengthen check 4 rather than adding a seventh check, so the spec's "six checks" stays true. Check 4 becomes: the cited rules must actually permit THIS booking. At least one permits, AND no cited rule imposes an unmet requirement. A cited `require_lead_time: N` blocks unless a new fact `hours_until_appointment` is present and >= N. A missing fact blocks, consistent with UNKNOWN never meaning no.
— `require_deposit` stays deliberately unenforced in v0. PRD section 9 says "v0 evaluates the rule and stops short of collecting". That is a scope decision, not an oversight, and the code must say so in a comment so nobody 'fixes' it later.
— Cost if wrong: if a real salon's lead-time rule is not expressible as hours-until-appointment, this check needs rethinking. For v0's single rule it is exact.
Ruling 9 fix: commit fa80cd1. 163 passed. Gate on 0 bad bookings / 4 completed. Gate off 7 bad bookings, up from 2 — the broken case file had been hiding five.
CONTROLLER-VERIFIED across three scenarios: first-colour asking for tomorrow (20h) BLOCKS on check 4; the same customer asking 120h out BOOKS, so the fix is not over-broad; and a booking where hours_until_appointment was never established BLOCKS. That third case proves the fix honours "a missing fact is not a no" rather than being a bare >= comparison.
Ruling 8 fix: purity subprocess test now passes PYTHONPATH explicitly and says plainly when a failure is environmental. Re-verified it still catches a deliberate `import httpx`.
Task 21: complete (4e9bcf9, 1206e20). 164 passed. All six Part-2 checks confirmed by throwaway script, output in task-21-report.md.
Task 21: finding worth carrying — propose() still proposes "book" when hours_until_appointment is missing, because that fact appears in no rule's CONDITION, only in an outcome. Only the gate catches it. That is the design working as intended (the proposer may be wrong, the gate is what stops it) but in a real conversation the agent should ASK when the appointment is rather than propose and be blocked. Worth a v1 improvement to propose().
Task 21: live thread NOT RUN. No API calls made. Runbook written and ready but unexecuted. README lists this under what it does not demonstrate.

=== FINAL WHOLE-BRANCH REVIEW (opus) ===
Verdict: DO NOT SHOW YET. The gate does not stop bad bookings. Check 4 is an any-permits test with no veto.
C1: a proposal can cite the deny rule ITSELF and still book. Check 2 passes on it (it evaluates TRUE), then check 4 only asks whether something permits. Same shape books a 14-year-old via under_16_needs_guardian.
C2: brute-forcing every citation subset over the 5-rule policy, 59 combinations book while a deny, escalate or unmet-lead rule matched. Check 3 only fires on a strict fact-superset, so citing a narrower subset dodges it.

Ruling 10: RULING 5 WAS WRONG AND MADE THINGS WORSE. I set no_colour_on_sunday to priority 10 believing it made deny outrank a permit. `grep -rn priority src/wca` shows priority is read in exactly two places, schema.py:85 and specificity.py:65, and NEITHER is the gate. All my change did was make check_decidable skip the pair — it silenced the detector that was correctly flagging a real hazard and fixed nothing. The ledger line "so deny wins" is false. Spec section 6's "priority only settles a genuine tie" is not true of this code.
— Correct fix: deny is an ABSOLUTE VETO in the gate, checked against every rule that evaluates TRUE, cited or not. Once deny always wins, an unranked deny-vs-permit pair is no longer ambiguous, so check_decidable should stop treating it as a conflict and priority should go back to 0.
— Cost if wrong: none. A veto is strictly safer than a ranking, and it cannot be defeated by citing a subset.

Ruling 11: Ruling 9 fixed one instance of a general defect. The comment I specified says "no cited rule imposes a requirement that is unmet" but the loop handles require_lead_time and nothing else. Generalise it: deny and require_escalation both veto a book action, and both are checked against ALL matching rules rather than only cited ones, which is what closes C2.

I3: the FIFTH vacuous test. test_gate.py:82 test_check_4_blocks_a_booking_whose_only_rule_denies_it cites only the deny rule, so it passes through the "no cited rule allows a booking" branch and the deny outcome plays no part. Reviewer mutated the policy, changing that rule's outcome from deny to require_escalation, and the test still passed. It is the test that hid C1.
I4: harness "bookings completed" commits nothing. No caller of MockCalendar.commit exists anywhere in src/. The two-phase claim is unit-tested but never exercised end to end.
I5: no AuditRecord is ever produced. grep "AuditRecord(" src finds only the class definition. Spec section 14 criterion 8 is undelivered and README:49 implies a live audit trail.
Security at the edge: SOUND. Forged, missing, sha1-prefixed, empty and reserialised-body requests all returned 403 with zero downstream calls. Constant-time compare, raw bytes, nothing parses before verification.

CORRECTION TO MY REPORTING: I told the user "0 bad bookings with the gate on" and "the gate is load-bearing". Both true of the 20 cases and both misleading. The cases do not exercise the citation patterns that defeat the gate.
Fix wave: commits 24f2fc8, 6f4ea82, 7c93b22, ba4c41c. 169 passed. Gate on 0 bad / 4 booked. Gate off 6 bad.
CONTROLLER-VERIFIED by independent brute force over every citation subset (32 per scenario) across five scenarios: sunday+under16+first-colour+10h 0/32 book, sunday-only 0/32, under16-only 0/32, first-colour-20h 0/32, and a LEGITIMATE booking 1/32. That last row is the guard against an over-broad fix — a gate that refuses everything would show 0/32 there and be worthless.
Fix wave mutation check: flipping no_colour_on_sunday's outcome to `allow` turned both the rewritten unit test and the new brute-force test red. The fifth vacuous test is now a real one.
Note: check_decidable / outcomes_conflict is now structurally inert for every v0 outcome type, because deny vetoes and nothing else conflicts. Correct, and worth knowing: it guards nothing until a future outcome type needs real ranking.
Corrective: commits eb5a5d0, 528931b. 167 passed (169 minus 7 deleted dead check_decidable tests, plus 5 new).
CONTROLLER-VERIFIED: sparse facts where weekday and age were never established now book 0 of 32 citation subsets, down from booking freely. With those facts supplied, 2 of 32 book, so the fix is not over-broad. The app secret leaks through none of repr, str, f-string or model_dump.
Escalation veto now locked in: deleting the clause turns the new brute-force test red.
check_decidable and outcomes_conflict DELETED. They were dead code carrying a docstring claiming a load-time safety net, with tests asserting only that nothing raises. store.py carries a comment saying a ranking guard was removed when deny became an absolute veto.

=== RESIDUALS, surfaced not fixed ===
- The 20 cases exercise only 4 of the 6 checks. No case reaches rules_exist or facts_support.
- Spec section 14 criterion 3 says the patch-test case blocks at check 3; it blocks at check 4. The spec was never updated after Ruling 9.
- harness.py's bare `except Exception` around commit would silently downgrade a failed commit to "not booked". No occurrences today.
- An appointment in the past books: hours_until_appointment=-500 passes. Nothing knows slot times. Not in the README's exclusions.
- webhook.py's GET handshake compares verify_token with != rather than compare_digest, unlike the POST path.
- propose() proposes "book" when hours_until_appointment is missing, because that fact is in no rule's condition. Only the gate catches it. In a real conversation the agent should ask.
- THE LIVE WHATSAPP THREAD WAS NEVER RUN. No API call was made in the entire build.

=== LIVE THREAD WORK (branch live-thread) ===
Scope agreed: (1) fix the lead-time hole, (2) locks on the calendar and the per-thread queue, (3) wire the worker so on_message runs the real pipeline in the background, (4) reaper and escalation-watchdog timers.
Classified bounded, not architectural: the design was presented and approved, and every flow being changed already exists. No plan document.
Steps 1 and 2 are disjoint (gate.py vs calendar/mock.py + conversation/queue.py) so they run as parallel worktree agents. Steps 3 and 4 are sequential behind them.
FULL SCOPE (Anna confirmed all eight): 1 lead-time hole, 2 locks, 3 mock catalogue, 4 tool loop, 5 wire the worker, 6 timers, 7 n8n reminder hop, 8 live thread end to end.
Spec addendum written at docs/superpowers/specs/2026-08-21-wca-v0-addendum-tools.md covering the catalogue shape and the four tool contracts.
The addendum restates the security invariant honestly. Old wording: "no output the model can produce is able to book anything". That stops being true once request_booking exists. New wording: every booking is preceded by a gate PASS on a proposal built from the conversation facts, and no code path commits a slot without one. Weaker sentence, identical effect, and more demonstrable.
It also closes audit finding 2: slots gain a real starts_at, hours_until_appointment is computed in code, and the field is removed from the model wire schema. Today the one number standing between a first colour visit and a booking is supplied by the thing the gate exists to check.
Step 1 complete (c7d0700, merged). Lead-time veto now iterates all matching and unknown rules like the other two. CONTROLLER-VERIFIED across four scenarios: the hole 0/32, under-threshold 0/32, when-never-established 0/32, legitimate 1/32.
Step 3 complete (7b7b1a0, merged). Catalogue loads, search refuses rather than guesses, hours_until returns -44.0 for a past slot rather than clamping — which is what makes the appointment-in-the-past finding fixable at all.
Step 2 complete (8502040, merged with a hand-resolved conflict in mock.py). Both races demonstrated before and after: two threads racing hold() gave 2 bookings on 1 slot, now gives 1; two drains of one thread interleaved j1-enter/j2-enter/j2-exit/j1-exit, now j1-enter/j1-exit/j2-enter/j2-exit.
Step 2 note: commit()'s new ALREADY_BOOKED recheck is unreachable through the public API now that hold() locks. Kept as defence in depth.
191 passed after the merge.

=== REVIEW OF STEPS 4-6 (opus) ===
Verdict: the safety invariant HOLDS. No constructible path commits a slot without a gate PASS on a book proposal. Reviewer tried unknown service, HoldRefused, non-book action, PASS on non-book, duplicate idempotency key, two tools in one turn. Request path measured: 200 sent at +0.001s against a 2.0s model call. All three addendum-mandated tests proven non-vacuous by mutation.
What is broken is the other direction: the wiring cannot book ANYTHING, and fails silently.

C1: cli.py:191 is state.add_facts({}, now=now). No extractor is wired into run_job, so conversation.facts is permanently empty. Every customer, every message: "patch_test_first_colour@1 might apply but we never established is_first_colour_visit". Safe, zero function.
C2: dispatch() handles an unknown tool NAME but not bad ARGS. check_availability(from_date="next monday") raises ValueError, propagates through run_turn -> run_job -> drain_thread, gets printed server-side, and the customer gets NO REPLY. Haiku will hit this readily.
I3: THE SEVENTH VACUOUS TEST. tools.py:116-117 merges conversation facts then catalogue facts so the catalogue wins. Reversing the order so the MODEL wins leaves all 226 tests passing. The addendum's whole point is untested.
I4: escalate() is closed in production. window_view does an exact-string registry.find(reason); cli.py registers only rule ids; the model supplies free text and is never told those strings. Tests use a fabricated registry so none sees it.
I5: two request_booking calls in one turn produce two bookings. Not a gate bypass, still wrong.
I6: MAX_ITERATIONS=8 is enforced, but there is no per-sender or global bound on messages, and DedupStore/ConversationStore/ThreadQueue/EscalationBook never evict. A leaked app secret is unmetered spend.
Minors: run_turn gets only the current message, no history; hitting the cap yields reply=="" and sends nothing; no AuditRecord if commit raises; an exception in evaluate orphans a hold until TTL.

CORRECTION TO MY OWN REPORTING: I told Anna the pipeline was "genuinely wired" and listed call sites as evidence. I grepped six symbols and omitted `.extract(` — the exact symbol I had previously reported as having ZERO call sites. The one already known missing is the one I did not re-check. Second incomplete verification of the same kind in this project.

=== n8n REQUIREMENTS (from Anna) ===
n8n runs in the cloud. Trigger: 24 hours before an appointment, to confirm. Endpoint needs building.
Consequences: the endpoint is PUBLIC (same host as the webhook) so it needs a shared secret, not open access. And a message 24h ahead falls OUTSIDE the WhatsApp service window, so it must be an approved template. Only hello_world is approved today. That approval gates the hop regardless of code and has real lead time.

=== WEEKDAY BYPASS + UX ===
Anna reported the agent asking the customer what today's date was. Checking that surfaced a second, worse problem.
BYPASS: requested_weekday was model-supplied, and no_colour_on_sunday reads it. Reproduced: a Sunday slot BOOKED because the model claimed "tuesday". The rule never fired.
This is the same class as hours_until_appointment, which I had already fixed. I fixed the instance I was shown and left its sibling. A review had told me I did exactly that once before with require_lead_time. Second time.

Ruling 12: the principle, not the instance. A fact that describes the BOOKING must be derived from the booking. Only facts that describe the CUSTOMER may come from the conversation.
— DERIVED_FACTS: service_category, quoted_price_minor (catalogue); hours_until_appointment, requested_weekday (slot).
— CONVERSATIONAL_FACTS: is_first_colour_visit, customer_age. Only the customer knows these.
— Both lists live in tools.py next to the derivation, with a test walking every fact the ruleset actually reads and failing if one is unclassified. That test is the point: it catches the NEXT sibling instead of waiting for someone to find it in production.
— Cost if wrong: a genuinely conversational fact misfiled as derived would be silently overwritten. The test forces the classification to be deliberate.

CONTROLLER-VERIFIED both directions: Sunday slot + model claims tuesday -> blocked, 0 bookings. Tuesday slot + model claims sunday -> BOOKED, 1 booking. The derived value wins either way, so the fix is not over-broad.

UX: the system threads `now` through every function and never told the model. Fixed: the prompt renders {now} with the weekday, check_availability takes optional dates defaulting to a 14-day window, slots come back with labels like "Tuesday 25 August, 2:00pm", and the prompt now says act-then-confirm rather than interrogate.
HONEST LIMIT: prompt adherence is not testable. The tests prove the wiring permits a single silent multi-tool turn. They cannot prove the model will actually act instead of asking. That only shows up on a real message.
254 passed.
