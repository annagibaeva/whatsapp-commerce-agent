# WCA v0 — Addendum: catalogue and tool use

**Date:** 21 August 2026
**Extends:** `2026-08-21-wca-v0-design.md`
**Status:** For build

The original spec gave the model one job: read language, return typed facts, never see a rule, never propose an action. This addendum gives it tools. That is a real change to what the model can do, so the invariant has to be restated precisely rather than assumed.

## 1. What changes and what does not

**Changes.** The model now calls tools. It can search a catalogue, check availability, ask for a booking, and escalate to a human.

**Does not change.** No booking exists unless the gate passed. The model can ask for a booking. It cannot decide one.

The old wording was "no output the model can produce is able to book anything." That is no longer the right sentence, because `request_booking` can end in a real booking. The accurate invariant is:

> Every booking is preceded by a gate verdict of PASS on a proposal built from the conversation's facts. There is no code path that commits a slot without one.

That is weaker in wording and identical in effect. It is also more demonstrable: the model genuinely tries, and you can watch the gate stop it.

## 2. The catalogue

A new module, `src/wca/catalogue.py`. Data only, loaded from `policy/salon.catalogue.json`.

```json
{
  "services": [
    {"id": "svc_colour_full", "name": "full head colour", "category": "colour",
     "duration_minutes": 120, "price_minor": 18000},
    {"id": "svc_colour_roots", "name": "root touch-up", "category": "colour",
     "duration_minutes": 60, "price_minor": 9000},
    {"id": "svc_cut", "name": "cut and finish", "category": "cut",
     "duration_minutes": 45, "price_minor": 4500}
  ]
}
```

`category` is what feeds the `service_category` fact the rules already read. `price_minor` is what feeds `quoted_price_minor`. Both facts currently arrive from the model with no source; after this they are derived from the catalogue.

## 3. Slots carry real times

Today a slot is a bare string and `hours_until_appointment` arrives as a fact from the model. That is a hole: the one number standing between a first colour visit and a booking is supplied by the thing the gate exists to check.

`MockCalendar` gains a slot record with a real `starts_at`. `hours_until_appointment` is then **computed in code** from `starts_at` and `now`, and removed from the model's wire schema entirely.

## 4. The four tools

| Tool | Effect | Gated |
|---|---|---|
| `search_catalogue(query)` | read-only | no |
| `check_availability(service_id, from_date, to_date)` | read-only | no |
| `request_booking(service_id, slot_id)` | holds, gates, commits on PASS | **yes** |
| `escalate(reason)` | raises a ticket for a human | **yes**, on deliverability |

`request_booking` is the interesting one. In order: derive facts from the catalogue and the slot, merge with conversation facts, build a `Proposal` citing every matching rule, hold the slot, run `gate.evaluate`, then commit on PASS or release on BLOCK.

**The verdict is returned to the model as the tool result.** A blocked booking comes back as the gate's own reason text, so the model can tell the customer that a first colour visit needs 48 hours' notice. That keeps the conversation useful without letting the model overrule anything.

A block does **not** automatically escalate. The model decides whether to ask a question or call `escalate`, and `escalate` is itself gated on whether a human could still reply.

## 5. What the model still never gets

It never sees the rules file, never chooses which rules are cited, never sets provenance, and never writes to the calendar except through `request_booking`. Citation is done in code by `propose`, from the facts. A model that hallucinates a rule name cannot cite it, because it is not asked to.

## 6. Tests this addendum requires

- A hostile-tool test: script the model client to call `request_booking` directly for a first colour visit 10 hours out, and assert no booking exists afterwards. This is the compromised-extractor test the original spec §10 mandated and never got.
- `hours_until_appointment` is absent from `RawFactSet`, and a booking still blocks when the slot is too soon.
- `search_catalogue` and `check_availability` cannot mutate anything: call each, then assert calendar and audit state are unchanged.
