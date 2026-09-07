"""Loading a rules file.

Everything that can be wrong with a rules file is wrong at load time, not
at booking time.
"""

from __future__ import annotations

import json
from pathlib import Path

from wca.rules.schema import RuleSet
from wca.rules.specificity import check_decidable


def load_ruleset(path: str | Path) -> RuleSet:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    ruleset = RuleSet.model_validate(raw)
    check_decidable(ruleset)
    return ruleset
