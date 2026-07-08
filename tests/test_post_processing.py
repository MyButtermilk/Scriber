import pytest

import src.post_processing as post_processing
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
    prompt = build_post_processing_prompt("tausend Euro pro Quadratmeter")

    assert "Beantworte keine Fragen im Transkript." in prompt
    assert "Gliedere den Text in sinnvolle Absätze." in prompt
    assert "Entferne Füllwörter" in prompt
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
    assert post_processing_output_token_budget("short text") >= 512
    assert post_processing_output_token_budget("word " * 5000) <= 4096


@pytest.mark.asyncio
async def test_post_process_live_transcript_uses_gpt_oss_default(monkeypatch):
    captured = {}

    async def fake_generate_text_with_model(_prompt, model, *, max_output_tokens):
        captured["model"] = model
        captured["max_output_tokens"] = max_output_tokens
        return "Cleaned text: Standardtext"

    monkeypatch.setattr(post_processing.Config, "POST_PROCESSING_MODEL", "", raising=False)
    monkeypatch.setattr(post_processing.Config, "DEFAULT_POST_PROCESSING_MODEL", "openai/gpt-oss-120b", raising=False)
    monkeypatch.setattr(post_processing, "generate_text_with_model", fake_generate_text_with_model)

    out = await post_process_live_transcript("rohtext")

    assert out == "Standardtext"
    assert captured["model"] == "openai/gpt-oss-120b"
    assert captured["max_output_tokens"] >= 512


@pytest.mark.asyncio
async def test_post_process_live_transcript_populates_redacted_diagnostics(monkeypatch):
    async def fake_generate_text_with_model(prompt, model, *, max_output_tokens):
        assert "private dictated text" in prompt
        assert model == "google/gemini-2.5-flash-lite:nitro"
        assert max_output_tokens >= 512
        return "Cleaned text: cleaned output"

    monkeypatch.setattr(post_processing, "generate_text_with_model", fake_generate_text_with_model)
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
    assert "private dictated text" not in str(diagnostics)
