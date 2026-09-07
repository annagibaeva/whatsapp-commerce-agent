"""The test set.

Twenty cases in five tiers. A case is facts, the action we expect the
proposer to reach, and the verdict we expect from the gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Tier = Literal["clean", "adversarial", "override", "unanswerable", "ambiguous"]


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tier: Tier
    note: str
    message: str
    facts: dict[str, Any]
    slot_id: str | None = "s_2026_08_25_1400"
    hours_into_window: float = 1.0
    expect_action: Literal["book", "ask", "escalate", "decline"]
    expect_allowed: bool
    expect_check: str | None = None
    #: True when a booking here would break a rule or double-book.
    would_be_bad_booking: bool = False


class CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: tuple[Case, ...]


def load_cases(path: str | Path) -> CaseFile:
    return CaseFile.model_validate_json(Path(path).read_text(encoding="utf-8"))
