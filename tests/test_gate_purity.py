"""The gate must not be able to call a model.

Reading the import graph is the only way to know. A comment saying so is
not evidence, and neither is the absence of a call today.
"""

import ast
import pathlib

import pytest

from wca.clock import utc
from wca.gate import evaluate
from wca.models import Action, CitedRule, Proposal
from wca.rules.store import load_ruleset

GATE = pathlib.Path("src/wca/gate.py")
BANNED_PREFIXES = (
    "wca.extract", "wca.transport", "wca.conversation", "wca.db",
    "wca.calendar.sqlite", "wca.calendar.live", "wca.calendar.ledger",
    "anthropic", "httpx", "requests", "sqlite3",
)


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_gate_imports_nothing_that_could_reach_a_model():
    for name in _imported_modules(GATE):
        for banned in BANNED_PREFIXES:
            assert not name.startswith(banned), f"gate.py imports {name}"


def test_the_gate_module_pulls_in_no_network_package_transitively():
    # Run in a fresh interpreter. Inside this test session other modules
    # (the anthropic extractor's tests, for one) may have already loaded
    # anthropic and httpx before this test runs, which would make a check
    # against the current process's sys.modules pass or fail depending on
    # test order rather than on what gate.py itself imports. A subprocess
    # that imports only wca.gate has no such pollution.
    import os
    import subprocess
    import sys

    # pytest's own pythonpath = ["src"] setting (pyproject.toml) is not
    # inherited by a subprocess. Without passing PYTHONPATH explicitly,
    # this only works when wca happens to be installed in the venv, which
    # made this test fail in two worktrees where it was not installed and
    # looked like a purity violation when it was really an environment gap.
    src_dir = str(pathlib.Path("src").resolve())
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir

    code = (
        "import sys\n"
        "import wca.gate\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        "the subprocess could not import wca.gate even with PYTHONPATH set "
        f"to {src_dir}. This looks like an environment problem (wca not "
        "importable), not a gate purity violation. stderr:\n" + result.stderr
    )
    loaded_modules = set(result.stdout.splitlines())
    banned = {m for m in loaded_modules if m.startswith(("anthropic", "httpx", "fastapi"))}
    assert banned == set(), f"importing the gate loaded {banned}"


def test_the_gate_returns_the_same_verdict_every_time():
    rules = load_ruleset("policy/salon.rules.json")
    proposal = Proposal(
        proposal_id="prop_0001", thread_id="t1",
        action=Action(type="book", slot_id="s1"),
        cited_rules=(CitedRule(rule_id="colour_allowed", version=1),),
        facts={"service_category": "colour", "is_first_colour_visit": False,
               "quoted_price_minor": 9000, "customer_is_over_16": True, "requested_weekday": "tuesday"},
        created_at=utc(2026, 8, 21, 10), hold_id="hold_0001",
    )
    cal = {"slot_exists": True, "booked": False, "held_by_thread": "t1", "hold_id": "hold_0001"}
    win = {"deliverable": True, "hours_left": 22.0, "why_not": ""}

    verdicts = [evaluate(proposal, rules, cal, win) for _ in range(5)]
    assert len(set(verdicts)) == 1


def test_the_gate_does_not_change_what_it_is_given():
    rules = load_ruleset("policy/salon.rules.json")
    facts = {"service_category": "colour", "is_first_colour_visit": True}
    proposal = Proposal(
        proposal_id="prop_0001", thread_id="t1",
        action=Action(type="book", slot_id="s1"),
        cited_rules=(CitedRule(rule_id="colour_allowed", version=1),),
        facts=facts, created_at=utc(2026, 8, 21, 10), hold_id="hold_0001",
    )
    cal = {"slot_exists": True, "booked": False, "held_by_thread": "t1", "hold_id": "hold_0001"}
    win = {"deliverable": True, "hours_left": 22.0, "why_not": ""}

    before_facts = dict(facts)
    before_cal = dict(cal)
    before_win = dict(win)
    evaluate(proposal, rules, cal, win)
    assert facts == before_facts
    assert cal == before_cal
    assert win == before_win
