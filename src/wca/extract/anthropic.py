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
        started = time.perf_counter()
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=RawFactSet,
        )
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return ExtractionResult(
                message_id=message_id, model=self.model, parse_failed=True,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        try:
            facts = build_facts(parsed)
        except ValidationError:
            return ExtractionResult(
                message_id=message_id, model=self.model, parse_failed=True,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        return ExtractionResult(
            message_id=message_id,
            facts=facts,
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
