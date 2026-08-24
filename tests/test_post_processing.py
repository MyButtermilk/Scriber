import asyncio
import time

import pytest

import src.post_processing as post_processing
from src.local_polishing import PolishOutcome
from src.post_processing import (
    build_post_processing_prompt,
    clean_post_processing_output,
    post_process_live_transcript,
    post_processing_output_token_budget,
)


def test_build_post_processing_prompt_replaces_handy_output_placeholder():
    prompt = build_post_processing_prompt(
        "uh hello comma world",
        "Clean this and return only text:\n${output}",
    )

    assert "${output}" not in prompt
    assert "uh hello comma world" in prompt


def test_build_post_processing_prompt_appends_transcript_without_placeholder():
    prompt = build_post_processing_prompt("raw dictation", "Fix grammar.")

    assert prompt.endswith("Raw transcript:\nraw dictation")


def test_default_post_processing_prompt_covers_dictation_cleanup_structure():
    prompt = build_post_processing_prompt(
        "tausend Euro pro Quadratmeter",
        post_processing.Config._DEFAULT_POST_PROCESSING_PROMPT,
    )

    assert "Beantworte keine Fragen im Transkript." in prompt
    assert "Gliedere den Text in sinnvolle Absätze." in prompt
    assert "Entferne Füllwörter" in prompt
    assert "Sehr geehrter Herr Müller" in prompt
    assert "Sehr geehrte Damen und Herren" in prompt
    assert 'Nutze Aufzählungszeichen mit "- "' in prompt
    assert "mehrere Punkte, Aufgaben, Beispiele, Voraussetzungen oder Argumente" in prompt
    assert "zweitausend fünfhundert Euro -> 2.500 €" in prompt
    assert "Euro pro Quadratmeter -> €/m²" in prompt
    assert "Kilowattstunden pro Quadratmeter und Jahr -> kWh/m²a" in prompt


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Output: hello world", "hello world"),
        ("<think>hidden reasoning</think>\nCleaned text: Hallo Welt", "Hallo Welt"),
        ("```final text```", "final text"),
    ],
)
def test_clean_post_processing_output(raw, expected):
    assert clean_post_processing_output(raw) == expected


def test_post_processing_output_token_budget_is_bounded():
    assert post_processing_output_token_budget("short text") >= 768
    assert post_processing_output_token_budget("word " * 5000) <= 4096


def test_post_processing_output_token_budget_tolerates_invalid_env(monkeypatch):
    monkeypatch.setenv("SCRIBER_POST_PROCESSING_TOKEN_MULTIPLIER", "nan")
    monkeypatch.setenv("SCRIBER_POST_PROCESSING_MIN_OUTPUT_TOKENS", "invalid")
    monkeypatch.setenv("SCRIBER_POST_PROCESSING_MAX_OUTPUT_TOKENS", "-1")

    assert post_processing_output_token_budget("hello world") == 768


@pytest.mark.asyncio
async def test_post_process_live_transcript_uses_cerebras_gemma_default(monkeypatch):
    captured = {}

    async def fake_generate_live_mic_text(
        _prompt,
        model,
        *,
        max_output_tokens,
        openrouter_reasoning_effort,
        openrouter_max_semantic_attempts,
        routing_diagnostics,
        provider_http_transport,
    ):
        captured["model"] = model
        captured["max_output_tokens"] = max_output_tokens
        captured["openrouter_reasoning_effort"] = openrouter_reasoning_effort
        captured["openrouter_max_semantic_attempts"] = openrouter_max_semantic_attempts
        captured["provider_http_transport"] = provider_http_transport
        assert routing_diagnostics is None
        return "Cleaned text: Standardtext"

    monkeypatch.setattr(post_processing.Config, "POST_PROCESSING_MODEL", "", raising=False)
    monkeypatch.setattr(post_processing.Config, "DEFAULT_POST_PROCESSING_MODEL", "cerebras/gemma-4-31b", raising=False)
    monkeypatch.setattr(post_processing.Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    monkeypatch.setattr(post_processing, "generate_live_mic_text", fake_generate_live_mic_text)

    provider_http_transport = object()
    out = await post_process_live_transcript(
        "rohtext",
        provider_http_transport=provider_http_transport,  # type: ignore[arg-type]
    )

    assert out == "Standardtext"
    assert captured["model"] == "cerebras/gemma-4-31b"
    assert captured["max_output_tokens"] >= 768
    assert captured["openrouter_reasoning_effort"] == "low"
    assert captured["openrouter_max_semantic_attempts"] == 1
    assert captured["provider_http_transport"] is provider_http_transport


@pytest.mark.asyncio
async def test_post_process_live_transcript_populates_redacted_diagnostics(monkeypatch):
    async def fake_generate_live_mic_text(
        prompt,
        model,
        *,
        max_output_tokens,
        openrouter_reasoning_effort,
        openrouter_max_semantic_attempts,
        routing_diagnostics,
        provider_http_transport,
    ):
        assert "private dictated text" in prompt
        assert model == "google/gemini-2.5-flash-lite:nitro"
        assert max_output_tokens >= 768
        assert openrouter_reasoning_effort == "low"
        assert openrouter_max_semantic_attempts == 1
        assert routing_diagnostics is diagnostics
        assert provider_http_transport is None
        routing_diagnostics.update(
            {
                "primaryBypassed": False,
                "primaryCircuitCooldownMs": 60000.0,
            }
        )
        return "Cleaned text: cleaned output"

    monkeypatch.setattr(post_processing, "generate_live_mic_text", fake_generate_live_mic_text)
    monkeypatch.setattr(post_processing.Config, "POST_PROCESSING_ENGINE", "cloud", raising=False)
    diagnostics = {}

    out = await post_process_live_transcript(
        "private dictated text",
        model="google/gemini-2.5-flash-lite:nitro",
        diagnostics=diagnostics,
    )

    assert out == "cleaned output"
    assert diagnostics["status"] == "completed"
    assert diagnostics["model"] == "google/gemini-2.5-flash-lite:nitro"
    assert diagnostics["rawChars"] == len("private dictated text")
    assert diagnostics["rawWords"] == 3
    assert diagnostics["promptChars"] > diagnostics["rawChars"]
    assert diagnostics["providerResponseChars"] == len("Cleaned text: cleaned output")
    assert diagnostics["cleanedChars"] == len("cleaned output")
    assert diagnostics["outputChanged"] is True
    assert diagnostics["deadlineMs"] == 7000.0
    assert diagnostics["openRouterReasoningEffort"] == "low"
    assert diagnostics["openRouterSemanticAttemptLimit"] == 1
    assert diagnostics["primaryCircuitCooldownMs"] == 60000.0
    assert diagnostics["fallbackPolicy"] == "bounded_cross_provider"
    assert "private dictated text" not in str(diagnostics)


@pytest.mark.asyncio
async def test_cloud_post_processing_bounds_the_complete_provider_fallback_chain(monkeypatch):
    captured = {}
    cancelled = asyncio.Event()

    async def hanging_generate_live_mic_text(
        _prompt,
        _model,
        *,
        max_output_tokens,
        openrouter_reasoning_effort,
        openrouter_max_semantic_attempts,
        routing_diagnostics,
        provider_http_transport,
    ):
        captured["max_output_tokens"] = max_output_tokens
        assert openrouter_reasoning_effort == "low"
        assert openrouter_max_semantic_attempts == 1
        assert routing_diagnostics is diagnostics
        assert provider_http_transport is None
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setenv("SCRIBER_LIVE_POST_PROCESSING_TIMEOUT_SEC", "0.05")
    monkeypatch.setattr(post_processing, "generate_live_mic_text", hanging_generate_live_mic_text)
    diagnostics = {}
    started = time.perf_counter()

    with pytest.raises(RuntimeError, match="deadline"):
        await asyncio.wait_for(
            post_process_live_transcript(
                "unveränderter Rohtext",
                engine="cloud",
                diagnostics=diagnostics,
            ),
            timeout=0.5,
        )

    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert captured["max_output_tokens"] >= 768
    assert diagnostics["status"] == "deadline_exceeded"
    assert diagnostics["fallbackToRaw"] is True
    assert diagnostics["reasonCodes"] == ["deadline_exceeded"]
    assert diagnostics["durationMs"] < 500
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_cloud_post_processing_records_sanitized_provider_failure_timing(monkeypatch):
    async def rejected_generate_live_mic_text(
        _prompt,
        _model,
        *,
        max_output_tokens,
        openrouter_reasoning_effort,
        openrouter_max_semantic_attempts,
        routing_diagnostics,
        provider_http_transport,
    ):
        assert max_output_tokens >= 768
        assert openrouter_reasoning_effort == "low"
        assert openrouter_max_semantic_attempts == 1
        assert routing_diagnostics is diagnostics
        assert provider_http_transport is None
        raise post_processing.ProviderTransportError(
            provider="cerebras",
            operation="summarization",
            status=401,
            code="authentication_error",
        )

    monkeypatch.setattr(post_processing, "generate_live_mic_text", rejected_generate_live_mic_text)
    diagnostics = {}

    with pytest.raises(post_processing.ProviderTransportError):
        await post_process_live_transcript(
            "unveränderter Rohtext",
            engine="cloud",
            diagnostics=diagnostics,
        )

    assert diagnostics["status"] == "failed"
    assert diagnostics["fallbackToRaw"] is True
    assert diagnostics["reasonCodes"] == ["provider_error", "authentication_error"]
    assert diagnostics["durationMs"] >= 0


@pytest.mark.asyncio
async def test_local_post_processing_failure_returns_raw_without_cloud_fallback(monkeypatch):
    class RejectingLocalPolisher:
        async def polish(self, transcript, variant):
            assert transcript == "unveränderter Rohtext"
            assert variant == "q8_0"
            return PolishOutcome(
                text="Dieser abgelehnte Text darf nicht verwendet werden.",
                variant="q8_0",
                status="original_fallback",
                reason_codes=("content_validation_failed",),
                duration_ms=12.5,
                runtime_backend="cpu",
            )

    async def cloud_must_not_run(*_args, **_kwargs):
        raise AssertionError("local failures must never fall through to a cloud provider")

    monkeypatch.setattr(post_processing, "generate_live_mic_text", cloud_must_not_run)
    diagnostics = {}

    result = await post_process_live_transcript(
        "unveränderter Rohtext",
        engine="local",
        local_polisher=RejectingLocalPolisher(),
        local_variant="q8_0",
        diagnostics=diagnostics,
    )

    assert result == "unveränderter Rohtext"
    assert diagnostics["status"] == "original_fallback"
    assert diagnostics["fallbackToRaw"] is True
    assert diagnostics["reasonCodes"] == ["content_validation_failed"]
