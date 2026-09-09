import inspect
import pathlib

import pytest

SOURCE = pathlib.Path("src/wca/extract/anthropic.py")


def test_model_ids_have_no_date_suffix():
    # A wrong model id is a 404, not a validation error -- guard the exact
    # strings that go over the wire.
    from wca.extract.anthropic import LARGE_MODEL, SMALL_MODEL

    import re

    for model_id in (SMALL_MODEL, LARGE_MODEL):
        assert not re.search(r"\d{8}", model_id), f"{model_id} looks date-suffixed"
    assert SMALL_MODEL != LARGE_MODEL


def test_no_sampling_parameters_are_ever_sent():
    text = SOURCE.read_text(encoding="utf-8")
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in text, f"{banned} was removed on Opus 5 and returns a 400"


def test_the_api_key_is_never_hardcoded_or_logged():
    text = SOURCE.read_text(encoding="utf-8")
    assert "sk-ant-" not in text
    assert "print(" not in text


def test_the_installed_sdk_has_the_method_we_call():
    from anthropic.resources.messages import Messages

    assert hasattr(Messages, "parse")
    sig = inspect.signature(Messages.parse)
    assert "output_format" in sig.parameters
    assert "max_tokens" in sig.parameters


def test_estimate_cost_matches_hand_computed_dollars():
    # PRICES is dollars per *million* tokens -- compute the expected
    # figure by hand here rather than calling estimate_cost to produce
    # its own expectation, so a broken divisor (or any other arithmetic
    # slip) shows up as a real failure.
    from wca.extract.anthropic import LARGE_MODEL, SMALL_MODEL, estimate_cost

    small = estimate_cost(SMALL_MODEL, input_tokens=1_000_000, output_tokens=1_000_000)
    assert small == pytest.approx(1.00 + 5.00)  # $1.00 in + $5.00 out

    large = estimate_cost(LARGE_MODEL, input_tokens=2_000_000, output_tokens=500_000)
    assert large == pytest.approx(2 * 5.00 + 0.5 * 25.00)  # $10.00 in + $12.50 out

    tiny = estimate_cost(SMALL_MODEL, input_tokens=100, output_tokens=50)
    assert tiny == pytest.approx((100 * 1.00 + 50 * 5.00) / 1_000_000)


# --- A helper stub for the escalation tests below --------------------------


def _stub_client(monkeypatch, responses):
    """`responses` is a list of (parsed_output_or_None, input_tokens,
    output_tokens), consumed in order -- one per `messages.parse` call.
    Running past the list raises, which is itself a signal the extractor
    made more calls than the test expected."""
    from dataclasses import dataclass
    from typing import Any

    from wca.extract import anthropic as anthropic_module

    monkeypatch.setattr(anthropic_module, "load_dotenv", lambda *a, **k: None)

    @dataclass
    class _Usage:
        input_tokens: int
        output_tokens: int

    @dataclass
    class _Response:
        parsed_output: Any
        usage: _Usage

    class _StubMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self._responses = list(responses)

        def parse(self, **kwargs: Any) -> _Response:
            self.calls.append(kwargs)
            parsed, in_tok, out_tok = self._responses.pop(0)
            return _Response(parsed_output=parsed, usage=_Usage(in_tok, out_tok))

    class _StubClient:
        def __init__(self) -> None:
            self.messages = _StubMessages()

    return _StubClient()


# --- Escalation: SMALL_MODEL first, LARGE_MODEL once on failure ------------


def test_an_unparseable_first_response_escalates_and_the_second_succeeds(monkeypatch):
    from wca.extract.anthropic import LARGE_MODEL, SMALL_MODEL, AnthropicExtractor, estimate_cost
    from wca.extract.base import RawFactSet

    client = _stub_client(
        monkeypatch,
        [
            (None, 100, 10),  # SMALL_MODEL: parse_failed
            (RawFactSet(service_category="colour"), 200, 20),  # LARGE_MODEL: succeeds
        ],
    )
    extractor = AnthropicExtractor(client=client)

    result = extractor.extract("m1", "text", [])

    calls = client.messages.calls
    assert len(calls) == 2
    assert calls[0]["model"] == SMALL_MODEL
    assert calls[1]["model"] == LARGE_MODEL
    assert result.model == LARGE_MODEL
    assert result.attempts == 2
    assert not result.parse_failed
    assert result.facts == {"service_category": "colour"}
    expected_cost = estimate_cost(SMALL_MODEL, 100, 10) + estimate_cost(LARGE_MODEL, 200, 20)
    assert result.cost_usd == pytest.approx(expected_cost)


def test_cost_usd_is_reconstructible_from_attempt_records(monkeypatch):
    """Fix 1: cost and tokens must never silently disagree.

    `result.input_tokens`/`output_tokens` carry only the final attempt
    (LARGE_MODEL's 200/20), so pricing *those* against `result.model`
    would silently under- or over-count a two-attempt extraction.
    `attempt_records` is the reconstruction path that must actually work:
    each record priced at its own model, summed, equal to `cost_usd`.
    """
    from wca.extract.anthropic import LARGE_MODEL, SMALL_MODEL, AnthropicExtractor, estimate_cost
    from wca.extract.base import RawFactSet

    client = _stub_client(
        monkeypatch,
        [
            (None, 100, 10),  # SMALL_MODEL: parse_failed
            (RawFactSet(service_category="colour"), 200, 20),  # LARGE_MODEL: succeeds
        ],
    )
    extractor = AnthropicExtractor(client=client)

    result = extractor.extract("m1", "text", [])

    assert [r.model for r in result.attempt_records] == [SMALL_MODEL, LARGE_MODEL]
    assert [(r.input_tokens, r.output_tokens) for r in result.attempt_records] == [
        (100, 10), (200, 20),
    ]
    # Each attempt record's own cost, computed at its own model's price --
    # never the combined tokens priced at one model.
    assert result.attempt_records[0].cost_usd == pytest.approx(estimate_cost(SMALL_MODEL, 100, 10))
    assert result.attempt_records[1].cost_usd == pytest.approx(estimate_cost(LARGE_MODEL, 200, 20))

    reconstructed = sum(r.cost_usd for r in result.attempt_records)
    assert result.cost_usd == pytest.approx(reconstructed)
    # Naively summing tokens and pricing them all at the *final* model
    # (result.model) is exactly the wrong-answer-no-warning shape Fix 1
    # exists to make impossible to produce from the object's own fields --
    # confirm it actually disagrees with the real cost here, as a sentinel
    # that this reconstruction test is discriminating and not vacuous.
    wrong_combined = estimate_cost(
        result.model,
        sum(r.input_tokens for r in result.attempt_records),
        sum(r.output_tokens for r in result.attempt_records),
    )
    assert wrong_combined != pytest.approx(result.cost_usd)


def test_a_clean_first_response_never_calls_the_large_model(monkeypatch):
    from wca.extract.anthropic import SMALL_MODEL, AnthropicExtractor
    from wca.extract.base import RawFactSet

    client = _stub_client(monkeypatch, [(RawFactSet(service_category="cut"), 50, 5)])
    extractor = AnthropicExtractor(client=client)

    result = extractor.extract("m1", "text", [])

    # Assert on the stub's recorded calls, not just the result: this is
    # what proves the large model was never reached, not merely that the
    # result happens to say so.
    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["model"] == SMALL_MODEL
    assert result.model == SMALL_MODEL
    assert result.attempts == 1
    assert result.facts == {"service_category": "cut"}


def test_both_attempts_failing_returns_the_same_failed_shape_as_today(monkeypatch):
    from wca.extract.anthropic import LARGE_MODEL, AnthropicExtractor

    client = _stub_client(monkeypatch, [(None, 10, 1), (None, 10, 1)])
    extractor = AnthropicExtractor(client=client)

    result = extractor.extract("m1", "text", [])

    assert len(client.messages.calls) == 2
    assert result.parse_failed is True
    assert result.facts == {}
    assert result.model == LARGE_MODEL
    assert result.attempts == 2


def test_constructing_with_the_large_model_makes_exactly_one_call_on_failure(monkeypatch):
    """Fix 2: escalating to a model you are already on.

    `AnthropicExtractor(model=LARGE_MODEL)` starts on LARGE_MODEL. A
    failed first attempt has nowhere left to escalate *to* -- retrying
    LARGE_MODEL against LARGE_MODEL is the same model, a second time, at
    five times SMALL_MODEL's price, for nothing. This must stop after
    exactly one call, not two.
    """
    from wca.extract.anthropic import LARGE_MODEL, AnthropicExtractor

    client = _stub_client(monkeypatch, [(None, 10, 1)])  # only one response queued
    extractor = AnthropicExtractor(model=LARGE_MODEL, client=client)

    result = extractor.extract("m1", "text", [])

    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["model"] == LARGE_MODEL
    assert result.attempts == 1
    assert result.model == LARGE_MODEL
    assert result.parse_failed is True


def test_escalation_never_makes_more_than_max_attempts_calls(monkeypatch):
    from wca.extract.anthropic import MAX_ATTEMPTS, AnthropicExtractor

    assert MAX_ATTEMPTS == 2  # the cap is a constant, not a magic number

    # Queue more failures than MAX_ATTEMPTS allows. If the extractor ever
    # looped past the cap it would ask the stub for a third response and
    # the stub's list.pop(0) would raise IndexError.
    client = _stub_client(monkeypatch, [(None, 1, 1)] * MAX_ATTEMPTS)
    extractor = AnthropicExtractor(client=client)

    result = extractor.extract("m1", "text", [])

    assert len(client.messages.calls) == MAX_ATTEMPTS
    assert result.attempts == MAX_ATTEMPTS


# --- Cause 1/2: the thread reaches the model with roles attributed --------

def test_the_thread_is_rendered_with_customer_and_agent_labels(monkeypatch):
    """`wca.cli` hands the extractor role-bearing turns now (see
    `Extractor.extract`'s signature in `wca.extract.base`). This proves
    `AnthropicExtractor` renders them attributed -- `agent: ...` /
    `customer: ...` -- rather than as a flat, unattributed list of
    strings, which is what left a bare "yes" with nothing to resolve
    against on the live thread this task exists to fix.

    No network: a stub client captures the prompt instead of a real
    Anthropic call, the same technique test_agent.py and
    test_serve_wiring.py already use for the model and the WhatsApp
    client. `load_dotenv` is neutralised so this never reads the real
    `.env` on disk, same as test_a_missing_key_fails_... above.
    """
    from dataclasses import dataclass
    from typing import Any

    from wca.extract import anthropic as anthropic_module
    from wca.extract.anthropic import AnthropicExtractor
    from wca.extract.base import RawFactSet

    monkeypatch.setattr(anthropic_module, "load_dotenv", lambda *a, **k: None)

    @dataclass
    class _Usage:
        input_tokens: int = 1
        output_tokens: int = 1

    @dataclass
    class _Response:
        parsed_output: Any
        usage: _Usage

    class _StubMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def parse(self, **kwargs: Any) -> _Response:
            self.calls.append(kwargs)
            return _Response(
                parsed_output=RawFactSet(customer_is_over_16=True), usage=_Usage()
            )

    class _StubClient:
        def __init__(self) -> None:
            self.messages = _StubMessages()

    client = _StubClient()
    extractor = AnthropicExtractor(client=client)
    thread = [
        {"role": "assistant", "content": "Are you over 16?"},
        {"role": "user", "content": "no, I meant a cut"},
    ]

    result = extractor.extract("m1", "yes", thread)

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "agent: Are you over 16?" in prompt
    assert "customer: no, I meant a cut" in prompt
    # And the extraction itself still comes back as a normal ExtractionResult.
    assert result.facts == {"customer_is_over_16": True}


def test_a_missing_key_fails_with_a_message_that_says_what_to_do(monkeypatch):
    import pytest

    from wca.extract import anthropic as anthropic_module

    # __init__ calls load_dotenv() first, which would read the key back out
    # of a real .env on disk. Neutralise it so this tests the code path and
    # not whether the developer happens to have credentials configured.
    monkeypatch.setattr(anthropic_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match=r"\.env"):
        anthropic_module.AnthropicExtractor()
