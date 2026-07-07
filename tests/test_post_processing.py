import pytest

from src.post_processing import (
    build_post_processing_prompt,
    clean_post_processing_output,
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
