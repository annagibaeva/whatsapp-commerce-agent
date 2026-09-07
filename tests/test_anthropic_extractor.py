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
