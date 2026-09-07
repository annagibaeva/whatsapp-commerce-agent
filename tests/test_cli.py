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
