import pytest

from src import summarization


def _words(count: int) -> str:
    return " ".join(f"w{i}" for i in range(count))


def test_summary_budget_scales_with_input_size():
    short_input = _words(700)
    long_input = _words(12_000)

    short_words, short_target, short_tokens = summarization._summary_budget_for_text(short_input, "gpt-5-mini")
    long_words, long_target, long_tokens = summarization._summary_budget_for_text(long_input, "gpt-5-mini")

    assert short_words == 700
    assert long_words == 12_000
    assert long_target > short_target
    assert long_tokens > short_tokens
    assert short_target >= 180
    assert long_target <= 2200


def test_dynamic_length_instruction_contains_budget():
    instruction = summarization._dynamic_length_instruction(1500, 270)
    assert "1500" in instruction
    assert "270" in instruction
    assert "Längenregel" in instruction


@pytest.mark.asyncio
async def test_summarize_text_passes_dynamic_budget_to_model(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    async def _fake_openai(prompt: str, model: str, max_output_tokens: int) -> str:
        captured["prompt"] = prompt
        captured["model"] = model
        captured["max_output_tokens"] = max_output_tokens
        return "ok"

    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)

    out = await summarization.summarize_text(_words(4000), model="gpt-5-mini")

    assert out == "ok"
    assert captured["model"] == "gpt-5-mini"
    assert isinstance(captured["max_output_tokens"], int)
    assert captured["max_output_tokens"] >= 512
    assert "Zusätzliche Längenregel" in str(captured["prompt"])


@pytest.mark.asyncio
async def test_summarize_text_empty_input_returns_empty():
    out = await summarization.summarize_text("   ", model="gpt-5-mini")
    assert out == ""

