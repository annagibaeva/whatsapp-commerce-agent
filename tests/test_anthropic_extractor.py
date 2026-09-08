import inspect
import pathlib

SOURCE = pathlib.Path("src/wca/extract/anthropic.py")


def test_model_ids_have_no_date_suffix():
    from wca.extract.anthropic import LARGE_MODEL, SMALL_MODEL

    assert SMALL_MODEL == "claude-haiku-4-5"
    assert LARGE_MODEL == "claude-opus-5"


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


def test_prices_cover_both_models():
    from wca.extract.anthropic import LARGE_MODEL, PRICES, SMALL_MODEL

    assert PRICES[SMALL_MODEL] == {"input": 1.00, "output": 5.00}
    assert PRICES[LARGE_MODEL] == {"input": 5.00, "output": 25.00}


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
