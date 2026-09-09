"""What one model call costs.

Shared by `wca.extract.anthropic` (fact extraction) and `wca.agent` (the
tool loop) -- both make real model calls against the same two models and
both need to price them the same way.

Kept here rather than in `wca.extract`, so `wca.agent` -- the core
tool-loop module, wired into every turn -- never has to import the
extraction subsystem (and drag in its real Anthropic client construction,
`load_dotenv()` call, and prompt-file loading) just to price a call.
`wca.extract.anthropic` imports its model ids and `estimate_cost` from
here too, so there is exactly one table of prices, not two that can
quietly drift apart.
"""

from __future__ import annotations

SMALL_MODEL = "claude-haiku-4-5"
LARGE_MODEL = "claude-opus-5"

#: US dollars per million tokens. Check against current published prices
#: before quoting any figure outside this repo.
PRICES = {
    SMALL_MODEL: {"input": 1.00, "output": 5.00},
    LARGE_MODEL: {"input": 5.00, "output": 25.00},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollars for one call, from PRICES -- US dollars per *million*
    tokens, hence the divisor below."""
    prices = PRICES[model]
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
