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
from typing import Sequence

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import ValidationError

from wca.extract.base import ExtractionResult, RawFactSet, build_facts, load_prompt

SMALL_MODEL = "claude-haiku-4-5"
LARGE_MODEL = "claude-opus-5"

#: US dollars per million tokens. Check against current published prices
#: before quoting any figure outside this repo.
PRICES = {
    SMALL_MODEL: {"input": 1.00, "output": 5.00},
    LARGE_MODEL: {"input": 5.00, "output": 25.00},
}


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
        self, message_id: str, text: str, thread: Sequence[str]
    ) -> ExtractionResult:
        prompt = self._template.format(
            thread="\n".join(f"  {line}" for line in thread[-6:]) or "  (nothing yet)",
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
