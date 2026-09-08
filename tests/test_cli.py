import os
import subprocess
import sys


def _run(*args):
    # Inherit the parent environment rather than replacing it outright.
    # Windows needs PATH (and friends) present for the interpreter to
    # start up at all (socket/overlapped-IO init fails without it); only
    # PYTHONPATH needs overriding, so wca resolves from the source tree.
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    return subprocess.run(
        [sys.executable, "-m", "wca.cli", *args],
        capture_output=True, text=True, cwd=".",
        env=env,
    )


def test_cases_runs_with_the_gate_on_and_reports_zero_bad_bookings():
    result = _run("cases")
    assert result.returncode == 0
    assert "bad bookings        0" in result.stdout


def test_cases_with_gate_off_reports_at_least_one_bad_booking_and_exits_nonzero():
    result = _run("cases", "--gate-off")
    assert "bad bookings" in result.stdout
    assert result.returncode == 1


def test_rules_lists_every_rule_in_english():
    result = _run("rules")
    assert result.returncode == 0
    assert "patch_test_first_colour@1" in result.stdout
    assert "first colour visit is true" in result.stdout


def test_help_lists_all_four_subcommands():
    result = _run("--help")
    assert result.returncode == 0
    for name in ("cases", "rules", "send", "serve"):
        assert name in result.stdout


# --- I5: escalate's fixed enum and the REAL registry cannot drift apart -----

def test_every_escalate_enum_reason_resolves_to_an_approved_template_in_the_real_registry():
    """Uses wca.cli.REGISTRY itself -- the one build_serve_app actually
    wires up -- not a registry fabricated for the test. A fabricated
    registry is exactly what let this drift happen in the first place:
    every existing test built its own TemplateRegistry, so none of them
    could see that cli.py's real one only ever registered rule ids."""
    from wca.cli import REGISTRY
    from wca.tools import ESCALATION_REASONS

    assert len(ESCALATION_REASONS) > 0
    for reason in ESCALATION_REASONS:
        template = REGISTRY.find(reason)
        assert template is not None, f"no template registered for {reason!r}"
        assert template.approved


def test_the_real_registry_still_serves_the_rule_id_escalations():
    from wca.cli import REGISTRY

    for reason in ("under_16_needs_guardian", "patch_test_first_colour"):
        template = REGISTRY.find(reason)
        assert template is not None
        assert template.approved


# --- C1: serve fails fast without an extraction key, rather than running
# --- with a silent no-op agent ------------------------------------------

def test_cmd_serve_fails_fast_when_anthropic_api_key_is_missing(monkeypatch):
    """Never touches the real .env: _load_dotenv is stubbed out and every
    other required variable is set directly on the environment, so this
    isolates the ANTHROPIC_API_KEY check itself. Must return before
    constructing a real Anthropic client or calling uvicorn.run -- neither
    of which this test wants to trigger."""
    import argparse

    import wca.cli as cli

    monkeypatch.setattr(cli, "_load_dotenv", lambda: cli._project_root() / ".env")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-secret")
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "test-verify")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "test-phone-id")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-access-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    args = argparse.Namespace(
        rules=cli.DEFAULT_RULES, catalogue=cli.DEFAULT_CATALOGUE, port=8000,
    )
    result = cli.cmd_serve(args)

    assert result == 1
