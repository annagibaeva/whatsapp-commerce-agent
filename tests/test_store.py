import json

import pytest

from wca.rules.store import load_ruleset


def test_the_real_rules_file_loads():
    rs = load_ruleset("policy/salon.rules.json")
    assert len(rs.rules) == 5
    assert rs.get("patch_test_first_colour", 1) is not None
    assert len(rs.ruleset_version) == 12


def test_an_unknown_operator_fails_to_load(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"rules": [{
        "id": "x", "version": 1,
        "condition": {"fact": "f", "op": "regex", "value": "y"},
        "outcome": {"type": "allow", "reason": "r"}, "source_text": "s",
    }]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_ruleset(bad)


def test_a_deny_next_to_an_unrelated_permit_loads_fine(tmp_path):
    # This used to be the "undecidable file" case: a deny and an allow on
    # unrelated facts, no specificity relation, equal priority. It used
    # to fail to load because check_decidable treated deny-vs-permit as a
    # conflict nothing ranked. It no longer does: the gate vetoes on any
    # true deny directly (gate.py check 4), so there is nothing left to
    # decide at load time and the file loads.
    tie = tmp_path / "tie.json"
    tie.write_text(json.dumps({"rules": [
        {"id": "a", "version": 1, "condition": {"fact": "x", "op": "eq", "value": 1},
         "outcome": {"type": "deny", "reason": "r"}, "source_text": "s"},
        {"id": "b", "version": 1, "condition": {"fact": "y", "op": "eq", "value": 2},
         "outcome": {"type": "allow", "reason": "r"}, "source_text": "s"},
    ]}), encoding="utf-8")
    rs = load_ruleset(tie)
    assert len(rs.rules) == 2
