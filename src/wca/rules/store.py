"""Loading a rules file.

Everything that can be wrong with a rules file is wrong at load time, not
at booking time.
"""

from __future__ import annotations

import json
from pathlib import Path

from wca.rules.schema import RuleSet


def load_ruleset(path: str | Path) -> RuleSet:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    # A load-time ranking guard (rules/specificity.check_decidable) used
    # to run here, raising if two rules could both apply, could not be
    # ranked by specificity or priority, and had outcomes that "conflict"
    # (rules/specificity.outcomes_conflict). It was removed once deny
    # became an absolute veto enforced by the gate itself (gate.py check
    # 4): outcomes_conflict was hard-wired to return False for every
    # pair, so the loop it drove could never raise — dead code with a
    # docstring that still read like a live safety net. If a future
    # outcome type introduces real ambiguity that specificity and the
    # gate's own vetoes cannot settle, a ranking guard would need to come
    # back here with a real predicate, not this one restored as-is.
    return RuleSet.model_validate(raw)
