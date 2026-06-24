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
    _, _, gemini_tokens = summarization._summary_budget_for_text(_words(1800), "gemini-flash-latest")

    assert gemini_tokens > gpt_tokens


def test_openrouter_payload_normalizes_models_to_nitro():
    payload = summarization._build_openrouter_payload(
        "prompt",
        ["minimax/minimax-m3", "z-ai/glm-5.2:floor"],
        4096,
    )

    assert payload["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]
    assert payload["max_tokens"] == 4096
    assert payload["messages"] == [{"role": "user", "content": "prompt"}]


def test_gemini_payload_sets_explicit_thinking_level(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL", "low")

    payload = summarization._build_gemini_payload("prompt", "gemini-flash-latest", 4096)

    generation_config = payload["generationConfig"]
    assert generation_config["maxOutputTokens"] == 4096
    assert generation_config["thinkingConfig"] == {"thinkingLevel": "LOW"}


def test_gemini_35_flash_payload_uses_medium_thinking_level_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL", raising=False)

    payload = summarization._build_gemini_payload("prompt", "gemini-3.5-flash", 4096)

    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}


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

    out = await summarization.summarize_text("x y z", model="gemini-flash-latest")

    assert out == "Zusammenfassung\n\n- Punkt A\n- Punkt B"


@pytest.mark.asyncio
async def test_summarize_text_openrouter_model_uses_nitro(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_openrouter(_prompt: str, models, _max_output_tokens: int) -> str:
        captured["models"] = models
        return "openrouter summary"

    monkeypatch.setattr(summarization, "_summarize_openrouter", _fake_openrouter)

    out = await summarization.summarize_text("x y z", model="minimax/minimax-m3")

    assert out == "openrouter summary"
    assert captured["models"] == "minimax/minimax-m3:nitro"


@pytest.mark.asyncio
async def test_summarize_text_falls_back_to_openrouter_when_primary_fails(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        raise RuntimeError("Gemini API error 503: UNAVAILABLE")

    async def _fake_openrouter(_prompt: str, models, _max_output_tokens: int) -> str:
        captured["models"] = models
        return "openrouter fallback"

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _fake_openrouter)

    out = await summarization.summarize_text("x y z", model="gemini-3.5-flash")

    assert out == "openrouter fallback"
    assert captured["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]


@pytest.mark.asyncio
async def test_gemini_summary_does_not_fallback_to_openai_by_default(monkeypatch: pytest.MonkeyPatch):
    openai_calls = 0
    monkeypatch.delenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENAI", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENAI_API_KEY", "openai-key")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "", raising=False)

    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        raise RuntimeError("Gemini API error 429: RESOURCE_EXHAUSTED")

    async def _fake_openai(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        nonlocal openai_calls
        openai_calls += 1
        return "openai summary"

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)
    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)

    with pytest.raises(RuntimeError, match="Gemini API error 429"):
        await summarization.summarize_text("x y z", model="gemini-flash-latest")

    assert openai_calls == 0


@pytest.mark.asyncio
async def test_gemini_summary_openai_fallback_is_explicit_and_contextual(monkeypatch: pytest.MonkeyPatch):
    openai_calls = 0
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENAI", "1")
    monkeypatch.setattr(summarization.Config, "OPENAI_API_KEY", "openai-key")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "", raising=False)

    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        raise RuntimeError("Gemini API error 429: RESOURCE_EXHAUSTED")

    async def _fake_openai(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        nonlocal openai_calls
        openai_calls += 1
        raise RuntimeError("OpenAI API error: insufficient_quota")

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)
    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)

    with pytest.raises(RuntimeError, match="Gemini summarization failed and the configured OpenAI fallback also failed"):
        await summarization.summarize_text("x y z", model="gemini-flash-latest")

    assert openai_calls == 1


@pytest.mark.asyncio
async def test_summarize_gemini_retries_max_tokens_with_larger_budget(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    monkeypatch.setattr(summarization.Config, "GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_MAX_TOKENS_RETRIES", "1")
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_RETRY_MAX_OUTPUT_TOKENS", "8000")
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL", "high")

    async def _fake_post(_session, _url, payload, *, retries):
        calls.append(payload["generationConfig"]["maxOutputTokens"])
        assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "HIGH"}
        if len(calls) == 1:
            return {
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": "abgeschnitten"}]},
                    }
                ],
                "usageMetadata": {"candidatesTokenCount": calls[-1], "totalTokenCount": calls[-1] + 100},
            }
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "vollstaendige zusammenfassung"}]},
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 900, "totalTokenCount": 1200},
        }

    monkeypatch.setattr(summarization, "_post_gemini_generate_content", _fake_post)

    out = await summarization._summarize_gemini("prompt", "gemini-flash-latest", 3000)

    assert out == "vollstaendige zusammenfassung"
    assert calls == [3000, 6000]


@pytest.mark.asyncio
async def test_summarize_gemini_discards_partial_after_repeated_max_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(summarization.Config, "GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_MAX_TOKENS_RETRIES", "0")

    async def _fake_post(_session, _url, payload, *, retries):
        return {
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": "partial darf nicht zurueckkommen"}]},
                }
            ],
            "usageMetadata": {
                "candidatesTokenCount": payload["generationConfig"]["maxOutputTokens"],
                "totalTokenCount": payload["generationConfig"]["maxOutputTokens"] + 100,
            },
        }

    monkeypatch.setattr(summarization, "_post_gemini_generate_content", _fake_post)

    with pytest.raises(RuntimeError, match="MAX_TOKENS"):
        await summarization._summarize_gemini("prompt", "gemini-flash-latest", 3000)
