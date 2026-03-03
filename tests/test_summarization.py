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
    assert short_target >= 320
    assert short_tokens >= 900
    assert long_target <= 2200


def test_summary_budget_short_floor_is_configurable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_SHORT_INPUT_MAX_WORDS", "3000")
    monkeypatch.setenv("SCRIBER_SUMMARY_SHORT_MIN_WORDS", "480")
    monkeypatch.setenv("SCRIBER_SUMMARY_SHORT_MIN_OUTPUT_TOKENS", "1300")

    _, target_words, output_tokens = summarization._summary_budget_for_text(_words(900), "gpt-5-mini")

    assert target_words >= 480
    assert output_tokens >= 1300


def test_summary_budget_long_video_gets_token_bonus(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_MIN_SECONDS", "1800")
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_TOKEN_BONUS", "700")

    _, _, normal_tokens = summarization._summary_budget_for_text(
        _words(3500),
        "gpt-5-mini",
        duration_seconds=1700,
    )
    _, _, boosted_tokens = summarization._summary_budget_for_text(
        _words(3500),
        "gpt-5-mini",
        duration_seconds=1900,
    )

    assert boosted_tokens > normal_tokens


def test_summary_budget_gemini_includes_thinking_reserve(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_THINKING_RESERVE_TOKENS", "2400")

    _, _, gpt_tokens = summarization._summary_budget_for_text(_words(1800), "gpt-5-mini")
    _, _, gemini_tokens = summarization._summary_budget_for_text(_words(1800), "gemini-3-flash-preview")

    assert gemini_tokens > gpt_tokens


def test_dynamic_length_instruction_contains_budget():
    instruction = summarization._dynamic_length_instruction(1500, 270)
    assert "1500" in instruction
    assert "270" in instruction
    assert "Längenregel" in instruction


def test_normalize_summary_markdown_converts_unicode_bullets():
    raw = "Titel\n\n•\tErster Punkt\n• Zweiter Punkt"
    normalized = summarization._normalize_summary_markdown(raw)
    assert normalized == "Titel\n\n- Erster Punkt\n- Zweiter Punkt"


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
    assert "niemals den Bullet-Charakter '•'" in str(captured["prompt"])


@pytest.mark.asyncio
async def test_summarize_text_empty_input_returns_empty():
    out = await summarization.summarize_text("   ", model="gpt-5-mini")
    assert out == ""


@pytest.mark.asyncio
async def test_summarize_text_uses_duration_based_boost(monkeypatch: pytest.MonkeyPatch):
    captured_tokens: list[int] = []

    async def _fake_openai(_prompt: str, _model: str, max_output_tokens: int) -> str:
        captured_tokens.append(max_output_tokens)
        return "ok"

    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_MIN_SECONDS", "1800")
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_TOKEN_BONUS", "700")

    text = _words(3500)
    await summarization.summarize_text(text, model="gpt-5-mini", duration="29:59")
    await summarization.summarize_text(text, model="gpt-5-mini", duration="30:01")

    assert len(captured_tokens) == 2
    assert captured_tokens[1] > captured_tokens[0]


@pytest.mark.asyncio
async def test_summarize_text_normalizes_markdown_bullets(monkeypatch: pytest.MonkeyPatch):
    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        return "Zusammenfassung\n\n•\tPunkt A\n• Punkt B"

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)

    out = await summarization.summarize_text("x y z", model="gemini-3-flash-preview")

    assert out == "Zusammenfassung\n\n- Punkt A\n- Punkt B"
