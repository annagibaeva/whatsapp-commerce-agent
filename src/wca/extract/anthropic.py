"""The real extractor.

Output is constrained by a JSON schema through client.messages.parse, so
a response either validates into RawFactSet or is recorded as a parse
failure. There is no searching a text block for braces.

Do not send any sampling parameter. Every one of them was removed on
Claude Opus 5 and a call that sends one returns a 400. The schema
constrains the output far more tightly than a sampling knob ever did.
"""

from __future__ import annotations

import os
import time
from typing import Mapping, Sequence

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import ValidationError

from wca.extract.base import AttemptRecord, ExtractionResult, RawFactSet, build_facts, load_prompt
from wca.pricing import LARGE_MODEL, PRICES, SMALL_MODEL, estimate_cost

#: `thread_history`'s own roles ("user" for the customer, "assistant" for
#: the agent) are not the words a customer reads when this is rendered
#: into the prompt -- `customer`/`agent` are. Anything else (a role this
#: codebase never produces) is rendered as-is rather than dropped, so a
#: future role does not silently vanish from what the model sees.
ROLE_LABELS: dict[str, str] = {"user": "customer", "assistant": "agent"}

#: Re-exported from wca.pricing -- see that module for why the model ids
#: and PRICES table live there and not here. Kept importable from this
#: module too so existing callers (and tests) that do
#: `from wca.extract.anthropic import SMALL_MODEL, ...` keep working.
__all__ = [
    "SMALL_MODEL", "LARGE_MODEL", "PRICES", "MAX_ATTEMPTS",
    "estimate_cost", "AnthropicExtractor",
]

#: One retry, never a loop: SMALL_MODEL, then LARGE_MODEL if that failed.
MAX_ATTEMPTS = 2


def _render_turn(turn: Mapping[str, str]) -> str:
    """One line of `{thread}`, attributed -- `  customer: ...` or `  agent:
    ...` -- so the model can tell a customer's answer from the agent's own
    question instead of seeing a flat, unattributed list of lines."""
    role = ROLE_LABELS.get(turn.get("role", ""), turn.get("role", "?"))
    return f"  {role}: {turn.get('content', '')}"


class AnthropicExtractor:
    def __init__(
        self,
        version: str = "extract-v0.1",
        model: str = SMALL_MODEL,
        client: Anthropic | None = None,
    ) -> None:
        load_dotenv()
        self.version = version
        self.model = model
        if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        self._client = client or Anthropic()
        self._template = load_prompt(version)

    def extract(
        self, message_id: str, text: str, thread: Sequence[Mapping[str, str]]
    ) -> ExtractionResult:
        prompt = self._template.format(
            thread="\n".join(_render_turn(turn) for turn in thread[-6:]) or "  (nothing yet)",
            message=text,
        )
        model = self.model
        total_cost = 0.0
        records: list[AttemptRecord] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            response = self._client.messages.parse(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
                output_format=RawFactSet,
            )
            usage = response.usage
            call_cost = estimate_cost(model, usage.input_tokens, usage.output_tokens)
            total_cost += call_cost
            records.append(AttemptRecord(
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=call_cost,
            ))

            facts: dict | None = None
            parsed = getattr(response, "parsed_output", None)
            if parsed is not None:
                try:
                    facts = build_facts(parsed)
                except ValidationError:
                    facts = None

            if facts is not None:
                return ExtractionResult(
                    message_id=message_id,
                    facts=facts,
                    model=model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    attempts=attempt,
                    cost_usd=total_cost,
                    attempt_records=tuple(records),
                )

            # This attempt did not produce a usable extraction. Escalate to
            # LARGE_MODEL for the one retry MAX_ATTEMPTS allows -- but only
            # when there is somewhere left to escalate to. Without the
            # `model != LARGE_MODEL` guard, an extractor already
            # constructed with LARGE_MODEL (see `__init__`'s `model`
            # parameter) would "escalate" from LARGE_MODEL to LARGE_MODEL:
            # the same model, a second time, at five times SMALL_MODEL's
            # price, for nothing. On the final attempt, or once already on
            # LARGE_MODEL, fall through and report the same failed shape
            # callers have always gotten.
            if attempt < MAX_ATTEMPTS and model != LARGE_MODEL:
                model = LARGE_MODEL
                continue
            return ExtractionResult(
                message_id=message_id, model=model, parse_failed=True,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                attempts=attempt,
                cost_usd=total_cost,
                attempt_records=tuple(records),
            )
