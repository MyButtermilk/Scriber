from pathlib import Path

from src.azure_mai_stt import (
    azure_mai_content_type,
    azure_mai_language_locales,
    azure_mai_region,
    azure_mai_transcript_payload_to_text,
    build_azure_mai_definition,
    validate_azure_mai_region,
)


def test_azure_mai_region_defaults_to_northeurope():
    assert azure_mai_region("") == "northeurope"
    assert azure_mai_region(None) == "northeurope"
    assert azure_mai_region("NorthEurope") == "northeurope"


def test_validate_azure_mai_region_rejects_unsupported():
    assert validate_azure_mai_region("northeurope") == "northeurope"
    try:
        validate_azure_mai_region("westeurope")
    except ValueError as exc:
        assert "not supported" in str(exc)
        assert "northeurope" in str(exc)
    else:
        raise AssertionError("Expected unsupported MAI region to fail")


def test_build_azure_mai_definition_sets_model_and_optional_locale():
    assert build_azure_mai_definition("auto") == {
        "enhancedMode": {
            "enabled": True,
            "model": "mai-transcribe-1",
        }
    }
    assert build_azure_mai_definition("de") == {
        "enhancedMode": {
            "enabled": True,
            "model": "mai-transcribe-1",
        },
        "locales": ["de"],
    }


def test_azure_mai_transcript_payload_to_text_prefers_combined_phrases():
    payload = {
        "combinedPhrases": [{"text": "Hello world"}],
        "phrases": [{"text": "fallback"}],
    }

    assert azure_mai_transcript_payload_to_text(payload) == "Hello world"


def test_azure_mai_transcript_payload_to_text_falls_back_to_phrases():
    assert azure_mai_transcript_payload_to_text({"phrases": [{"text": "Hello"}, {"displayText": "world"}]}) == (
        "Hello world"
    )


def test_azure_mai_content_type_by_extension():
    assert azure_mai_content_type(Path("audio.wav")) == "audio/wav"
    assert azure_mai_content_type(Path("audio.flac")) == "audio/flac"
    assert azure_mai_content_type(Path("audio.webm")) == "audio/mpeg"


def test_azure_mai_language_locales_skips_auto():
    assert azure_mai_language_locales("auto") == []
    assert azure_mai_language_locales("en") == ["en"]
