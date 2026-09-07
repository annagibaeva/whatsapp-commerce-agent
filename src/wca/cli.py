"""Command line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from wca.cases import load_cases
from wca.escalation import Template, TemplateRegistry
from wca.harness import run_cases
from wca.rules.render import to_english
from wca.rules.store import load_ruleset

DEFAULT_RULES = Path("policy/salon.rules.json")
DEFAULT_CASES = Path("cases/v0.cases.json")

REGISTRY = TemplateRegistry(templates=(
    Template(reason="under_16_needs_guardian", name="guardian_notice", approved=True),
    Template(reason="patch_test_first_colour", name="patch_test_notice", approved=True),
))


def cmd_cases(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    rules = load_ruleset(args.rules)
    report = run_cases(cases, rules, REGISTRY, gate_on=not args.gate_off)
    print(report.render())
    if report.bad_bookings:
        print(f"\n{report.bad_bookings} bad booking(s) got through.")
        return 1
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    ruleset = load_ruleset(args.rules)
    print(f"ruleset {ruleset.ruleset_version}, {len(ruleset.rules)} rules\n")
    for rule in ruleset.rules:
        print(f"  {rule.ref()}")
        print(f"    when   {to_english(rule.condition)}")
        print(f"    then   {rule.outcome.type}: {rule.outcome.reason}")
        print(f"    from   {rule.source_text}")
        print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="wca", description="WhatsApp Commerce Agent v0")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cases = sub.add_parser("cases", help="run the twenty test cases")
    p_cases.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p_cases.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    p_cases.add_argument("--gate-off", action="store_true", help="run without the gate")

    p_rules = sub.add_parser("rules", help="list the rules in English")
    p_rules.add_argument("--rules", type=Path, default=DEFAULT_RULES)

    # `serve` (Task 20) and `send` (Task 19) are added later. Adding them
    # here is meant to be a clean insertion: a new sub.add_parser(...) call
    # and a new `if args.command == ...` branch below. Nothing else in
    # this file needs to change for that.

    args = parser.parse_args()
    if args.command == "cases":
        return cmd_cases(args)
    if args.command == "rules":
        return cmd_rules(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
