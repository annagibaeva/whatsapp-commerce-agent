"""Command line entry point."""

from __future__ import annotations

import argparse
import os
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


def cmd_send(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from wca.transport.whatsapp import WhatsAppTransport

    load_dotenv()
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not phone_number_id or not token:
        print("WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN must be set in .env")
        return 1
    WhatsAppTransport(phone_number_id, token).send_text(args.to, args.text)
    print(f"sent to {args.to}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from dotenv import load_dotenv

    from wca.conversation.dedup import DedupStore
    from wca.conversation.queue import ThreadQueue
    from wca.transport.webhook import WebhookSettings, create_app

    load_dotenv()
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    token = os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
    if not secret or not token:
        print("WHATSAPP_APP_SECRET and WHATSAPP_WEBHOOK_VERIFY_TOKEN must be set in .env")
        return 1

    dedup = DedupStore()
    queue = ThreadQueue()

    def on_message(message):
        # Dedup runs where the queue is consumed, not here. Several
        # retries can arrive together and all pass an edge check before
        # any of them is written down.
        def job():
            if dedup.seen(message.message_id):
                return
            dedup.remember(message.message_id)
            print(f"[{message.thread_id}] {message.text}")

        queue.submit(message.thread_id, job)
        queue.drain()

    app = create_app(WebhookSettings(app_secret=secret, verify_token=token), on_message)
    uvicorn.run(app, host="0.0.0.0", port=args.port)
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

    p_send = sub.add_parser("send", help="send one message on the live thread")
    p_send.add_argument("--to", required=True, help="recipient in full international form, digits only")
    p_send.add_argument("--text", required=True)

    p_serve = sub.add_parser("serve", help="run the webhook receiver")
    p_serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    if args.command == "cases":
        return cmd_cases(args)
    if args.command == "rules":
        return cmd_rules(args)
    if args.command == "send":
        return cmd_send(args)
    if args.command == "serve":
        return cmd_serve(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
