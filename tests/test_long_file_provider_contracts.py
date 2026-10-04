"""Provider wire contracts and preservation across long-file merge boundaries."""

import pytest

from src.azure_mai_stt import build_azure_mai_definition
from src.cloud_async_stt import _build_openrouter_stt_mp3_body
from src.core.provider_audio_formats import (
    AZURE_MAI_MAX_AUDIO_BYTES,
    AZURE_MAI_MAX_AUDIO_DURATION_MS,
    OPENROUTER_STT_MAX_AUDIO_BYTES,
    resolve_batch_provider_audio_capabilities,
)
from src.file_transcription_parts import validate_part_result
from src.provider_transcript import normalize_provider_words
from src.transcript_artifacts import freeze_provider_route, stage_units_from_provider


@pytest.mark.parametrize("model,words", [("microsoft/mai-transcribe-2", True), ("microsoft/mai-transcribe-1.5", False)])
def test_word_response_is_exact_model_gated_on_wire_and_in_frozen_route(model, words):
    with _build_openrouter_stt_mp3_body(
        b"original-mp3", model=model, language="de", boundary="safe", request_word_timestamps=True
    ) as body:
        request = body.read()
    assert (b"verbose_json" in request) is words
    assert (b"timestamp_granularities[]" in request) is words
    assert b"original-mp3" in request
    route = freeze_provider_route(workload="file", provider="openrouter_stt", model=model, language="de")
    assert route.response_shape == ("verbose_json_words" if words else "final_text")
    assert route.timestamp_mode == ("word" if words else "estimated")
    assert route.execution_route()["timestamp_mode"] == route.timestamp_mode
    assert route.diarization_mode == "local_fallback_if_enabled"


def test_frozen_text_only_request_remains_json_even_for_mai_two():
    with _build_openrouter_stt_mp3_body(
        b"original-mp3",
        model="microsoft/mai-transcribe-2",
        language="de",
        boundary="safe",
        request_word_timestamps=False,
    ) as body:
        request = body.read()
    assert b"verbose_json" not in request
    assert b"timestamp_granularities[]" not in request


def test_azure_file_word_times_do_not_require_enabling_diarization():
    definition = build_azure_mai_definition("de", model="MAI-Transcribe-2", request_word_timestamps=True)
    assert definition["enhancedMode"]["modelOptions"]["timestamps"] == "word"
    assert "diarization" not in definition
    legacy = build_azure_mai_definition("de", model="mai-transcribe-1.5", request_word_timestamps=True)
    assert "timestamps" not in legacy["enhancedMode"].get("modelOptions", {})


def test_route_limits_are_not_invented_from_other_providers_or_storage_quota():
    assert OPENROUTER_STT_MAX_AUDIO_BYTES == 25_000_000
    assert AZURE_MAI_MAX_AUDIO_BYTES == 249_999_999
    assert AZURE_MAI_MAX_AUDIO_DURATION_MS == 7_199_999
    soniox = resolve_batch_provider_audio_capabilities("soniox_async", "stt-async-v5")
    assert soniox.max_upload_bytes is None


def test_openrouter_timing_is_seconds_and_partial_word_metadata_never_loses_text():
    payload = {
        "text": "Hallo Anna.",
        "words": [
            {"word": "Hallo", "start": 1.1, "end": 1.6, "speaker": 0},
            {"word": "Anna.", "start": 1.7, "end": 2.1, "speaker": 0},
        ],
    }
    words = normalize_provider_words("openrouter_stt", payload, origin_ms=5_000)
    assert [(word["startMs"], word["endMs"]) for word in words] == [(6_100, 6_600), (6_700, 7_100)]
    assert words[0]["speaker"] == "0"
    payload["words"].pop()
    units, evidence = stage_units_from_provider(
        provider="openrouter_stt", payload=payload, text=payload["text"], duration_ms=3_000
    )
    assert " ".join(unit.text for unit in units) == "Hallo Anna."
    assert evidence["estimatedTiming"] is True


def test_uncertain_boundary_survives_canonical_stage_without_spoken_error_text():
    warning = {
        "code": "overlap_conflicting_words",
        "leftPartIndex": 1,
        "rightPartIndex": 2,
        "startMs": 56_000,
        "endMs": 64_000,
    }
    payload = {
        "text": "Müller sagte 15. Müller sagte 50.",
        "_scriberMerged": {
            "wordTimingComplete": False,
            "words": [],
            "boundaryWarnings": [warning],
        },
    }
    units, evidence = stage_units_from_provider(
        provider="azure_mai", payload=payload, text=payload["text"], duration_ms=120_000
    )
    assert evidence["chunkBoundaryWarnings"] == [warning]
    assert " ".join(unit.text for unit in units) == payload["text"]
    assert evidence["estimatedTiming"] is True


@pytest.mark.parametrize("provider,payload", [("openrouter_stt", {"text": ""}), ("azure_mai", {"phrases": []})])
def test_explicit_silent_success_is_valid(provider, payload):
    validate_part_result(provider, payload)


@pytest.mark.parametrize("provider", ["openrouter_stt", "azure_mai"])
def test_missing_transcript_is_not_silently_cached_as_silence(provider):
    from src.core.provider_errors import ProviderTransportError

    with pytest.raises(ProviderTransportError):
        validate_part_result(provider, {"usage": {"seconds": 30}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,duration_ms,expected_count",
    [
        ("microsoft/mai-transcribe-2", 7_199_999, 1),
        ("microsoft/mai-transcribe-2", 7_200_000, 1),
        ("microsoft/mai-transcribe-2", 7_200_001, 2),
        ("microsoft/mai-transcribe-1.5", 7_200_001, 1),
    ],
)
async def test_low_bitrate_openrouter_mai_two_uses_model_duration_bound(
    monkeypatch, tmp_path, model, duration_ms, expected_count
):
    from src import cloud_async_stt, openrouter_audio
    from src.audio_prepare import ProbedAudioInput
    from src.core.provider_audio_formats import AudioInputFormat

    source = tmp_path / "low-bitrate.mp3"
    source.write_bytes(b"original-low-bitrate-audio")
    durations = {}
    uploads = []

    def probe(path):
        return ProbedAudioInput(
            AudioInputFormat.MP3,
            "mp3",
            "mp3",
            8000,
            1,
            duration_ms if path == source else durations[path],
            path.stat().st_size,
        )

    async def copy(command, target):
        durations[target] = round(float(command[command.index("-t") + 1]) * 1000)
        target.write_bytes(b"locally-split-fixture")

    async def cut(_ffmpeg, _source, candidate, _minimum, _maximum):
        return candidate

    async def transcribe(**kwargs):
        uploads.append(kwargs["audio_source"].read())
        return {"text": f"Part {len(uploads)}"}

    monkeypatch.setattr(openrouter_audio, "probe_audio_input_file", probe)
    monkeypatch.setattr(openrouter_audio, "_run_part_command", copy)
    monkeypatch.setattr(openrouter_audio, "_silence_cut", cut)
    monkeypatch.setattr(openrouter_audio, "require_media_tool", lambda _name: "fixture-ffmpeg")
    monkeypatch.setattr(cloud_async_stt, "transcribe_with_openrouter_audio_transcription", transcribe)
    result = await cloud_async_stt.transcribe_openrouter_file(
        session=object(), api_key="inert", path=source, content_type="audio/mpeg", language="de", model=model
    )
    assert result["_scriberChunkCount"] == expected_count
    assert len(uploads) == expected_count
    if expected_count == 1:
        assert uploads == [source.read_bytes()]
    else:
        assert all(value <= 7_200_000 for value in durations.values())
