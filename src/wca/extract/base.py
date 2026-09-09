"""Turning a message into facts.

The model reads language. The code decides what to trust. The model never
sees a rule, never cites one, and never proposes a booking. It returns
facts and nothing else, so no output it can produce is able to book
anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts"


class RawFactSet(BaseModel):
    """The wire shape given to the model as a JSON schema.

    Every field is listed and nullable. A strict schema cannot describe a
    dictionary with arbitrary keys, so the fields are spelled out.
    """

    model_config = ConfigDict(extra="forbid")
    service_category: Literal["colour", "cut", "treatment"] | None = None
    is_first_colour_visit: bool | None = None
    customer_is_over_16: bool | None = None
    quoted_price_minor: int | None = Field(default=None, ge=0)
    # `requested_weekday` used to be reported here, from the model's
    # reading of the message. It described the booking, not the
    # customer, and `service_category`/`quoted_price_minor` had already
    # moved off this path for the same reason (see wca.catalogue). It is
    # now derived in `wca.tools.request_booking` from the slot's own
    # `starts_at`, the same way `hours_until_appointment` is -- see
    # `wca.tools.DERIVED_FACTS`. `requested_date_text` stays: it is what
    # the customer said, kept verbatim, not a fact a rule reads.
    requested_date_text: str | None = None


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    facts: dict[str, Any] = Field(default_factory=dict)
    model: str = "fake"
    input_tokens: int = 0
    output_tokens: int = 0
    parse_failed: bool = False
    #: How many model calls this extraction took, 1 unless it escalated.
    #: Without this, "extracted first time" and "extracted only after
    #: escalating" collapse into one number and the cost argument for
    #: tiering cannot be made.
    attempts: int = 1
    #: US dollars, computed from PRICES in wca.extract.anthropic. Summed
    #: across every attempt this extraction actually made.
    cost_usd: float = 0.0


class Extractor(Protocol):
    version: str

    def extract(
        self, message_id: str, text: str, thread: Sequence[Mapping[str, str]]
    ) -> ExtractionResult:
        """`thread` carries each past turn's role alongside its content --
        `{"role": "user" | "assistant", "content": ...}`, the same shape
        `wca.cli` already builds. Without the role, a bare "yes" has
        nothing to resolve against: the extractor cannot tell a customer's
        answer from the agent's own question, so it reports nothing and
        the agent asks again. See `prompts/extract-v0.1.md`."""
        ...


def build_facts(raw: RawFactSet) -> dict[str, Any]:
    """Drop anything the model left out.

    A field the model omitted must stay missing. If it became None and
    then a fact, a rule that needs it would read 'we asked and the answer
    was nothing' instead of 'we never asked'.
    """
    return {k: v for k, v in raw.model_dump().items() if v is not None}


def load_prompt(version: str) -> str:
    return (PROMPT_DIR / f"{version}.md").read_text(encoding="utf-8")
