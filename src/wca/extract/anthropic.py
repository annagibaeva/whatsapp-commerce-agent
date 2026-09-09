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

from wca.extract.base import ExtractionResult, RawFactSet, build_facts, load_prompt

#: `thread_history`'s own roles ("user" for the customer, "assistant" for
#: the agent) are not the words a customer reads when this is rendered
#: into the prompt -- `customer`/`agent` are. Anything else (a role this
#: codebase never produces) is rendered as-is rather than dropped, so a
#: future role does not silently vanish from what the model sees.
ROLE_LABELS: dict[str, str] = {"user": "customer", "assistant": "agent"}

SMALL_MODEL = "claude-haiku-4-5"
LARGE_MODEL = "claude-opus-5"

#: US dollars per million tokens. Check against current published prices
#: before quoting any figure outside this repo.
PRICES = {
    SMALL_MODEL: {"input": 1.00, "output": 5.00},
    LARGE_MODEL: {"input": 5.00, "output": 25.00},
}

#: One retry, never a loop: SMALL_MODEL, then LARGE_MODEL if that failed.
MAX_ATTEMPTS = 2


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollars for one call, from PRICES -- US dollars per *million*
    tokens, hence the divisor below."""
    prices = PRICES[model]
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


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
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            response = self._client.messages.parse(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
                output_format=RawFactSet,
            )
            usage = response.usage
            total_cost += estimate_cost(model, usage.input_tokens, usage.output_tokens)

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
                )

            # This attempt did not produce a usable extraction. Escalate to
            # LARGE_MODEL for the one retry MAX_ATTEMPTS allows; on the
            # final attempt, fall through and report the same failed shape
            # callers have always gotten.
            if attempt < MAX_ATTEMPTS:
                model = LARGE_MODEL
                continue
            return ExtractionResult(
                message_id=message_id, model=model, parse_failed=True,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                attempts=attempt,
                cost_usd=total_cost,
            )
