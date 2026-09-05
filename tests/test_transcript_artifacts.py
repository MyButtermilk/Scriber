import pytest

from src.config import Config
from src.core.provider_audio_formats import AudioInputFormat, AudioSelectionMode
from src.data.transcript_artifact_store import AlignmentQuality
from src.transcript_artifacts import (
    duration_label_to_ms,
    freeze_caption_route,
    freeze_provider_route,
    provider_batch_model,
    stage_units_from_captions,
    stage_units_from_provider,
)
from src.youtube_download import YouTubeCaptionCue


def test_frozen_route_persists_only_vocabulary_metadata():
    route = freeze_provider_route(
        workload="file",
        provider="soniox_async",
        language="de",
        custom_vocab="Secret customer,Scriber",
    )
    draft = route.snapshot_draft()
    assert route.execution_route()["custom_vocab"] == "Secret customer,Scriber"
    assert "Secret customer" not in str(draft.request_options)
    assert draft.request_options["customVocabularyCount"] == 2
    assert len(draft.request_options["customVocabularySha256"]) == 64
    assert draft.model == Config.SONIOX_ASYNC_MODEL
    assert draft.request_options["providerRoute"] == "async_transcription"
    assert draft.request_options["audioInputFormat"] is None
    assert draft.request_options["providerAudioCapabilityId"].startswith("soniox_async:async_transcription:")
    assert draft.request_options["providerAudioCapabilityRevision"]
    assert "https://" not in str(draft.request_options)

    execution = route.execution_route()
    assert execution["provider_route"] == "async_transcription"
    assert execution["audio_input_format"] is None
    assert execution["provider_audio_capability_id"]
    assert execution["provider_audio_capability_revision"]


def test_provider_route_can_freeze_task_scoped_transport():
    route = freeze_provider_route(
        workload="meeting",
        provider="soniox_async",
        transport="webm_opus_task_derivative",
    )

    assert route.transport == "webm_opus_task_derivative"
    assert route.audio_input_format == AudioInputFormat.WEBM_OPUS
    assert route.audio_input_format_verified is True
    assert route.execution_route()["transport"] == "webm_opus_task_derivative"
    assert route.execution_route()["audio_input_format"] == "webm_opus"
    assert route.snapshot_draft().transport == "webm_opus_task_derivative"
    assert route.snapshot_draft().request_options["audioInputFormat"] == "webm_opus"


def test_exact_preparation_metadata_is_persisted_in_route_snapshot():
    route = freeze_provider_route(
        workload="file",
        provider="soniox_async",
        audio_input_format=AudioInputFormat.WEBM_OPUS,
        audio_selection_mode=AudioSelectionMode.ORIGINAL_PASSTHROUGH,
        audio_preparation_implementation="original_passthrough",
    )

    execution = route.execution_route()
    options = route.snapshot_draft().request_options
    assert execution["audio_selection_mode"] == "original_passthrough"
    assert execution["audio_preparation_implementation"] == "original_passthrough"
    assert options["audioSelectionMode"] == "original_passthrough"
    assert options["audioPreparationImplementation"] == "original_passthrough"


def test_gemini_live_route_freezes_exact_pcm_model_and_implementation():
    route = freeze_provider_route(workload="live", provider="gemini_realtime")

    assert route.model == "gemini-3.5-transcribe-live"
    assert route.provider_route == "live_transcription"
    assert route.audio_input_format == AudioInputFormat.RAW_PCM16
    assert route.audio_preparation_implementation == "scriber_gemini_live_raw_pcm16"
    assert (
        route.snapshot_draft()
        .request_options["providerAudioCapabilityId"]
        .startswith("gemini_realtime:live_transcription:gemini-3.5-transcribe-live")
    )


def test_frozen_route_rejects_an_explicit_format_for_an_unknown_model():
    unverified = freeze_provider_route(
        workload="file",
        provider="soniox_async",
        model="custom-soniox-model",
    )
    assert unverified.provider_route == "async_transcription"
    assert unverified.provider_audio_capability_id == ""
    assert unverified.audio_input_format is None

    with pytest.raises(ValueError, match="exact provider route/model"):
        freeze_provider_route(
            workload="file",
            provider="soniox_async",
            model="custom-soniox-model",
            audio_input_format=AudioInputFormat.WAV_PCM16,
        )


def test_groq_batch_model_is_reported_as_the_actual_supported_model():
    assert provider_batch_model("groq") == "whisper-large-v3-turbo"
    route = freeze_provider_route(
        workload="meeting",
        provider="groq",
    )
    assert route.model == "whisper-large-v3-turbo"
    assert route.provider_route == "openai_v1_segmented_audio_transcriptions"
    assert route.audio_input_format == AudioInputFormat.WAV_PCM16
    assert route.audio_selection_mode == AudioSelectionMode.GENERATED
    assert route.audio_preparation_implementation == "pipecat_segmented_wav_pcm16"


def test_google_route_freezes_speech_v2_model_and_raw_pcm_request_contract():
    assert provider_batch_model("google") == "latest_long"
    route = freeze_provider_route(workload="file", provider="google")

    assert route.model == "latest_long"
    assert route.provider_route == "cloud_streaming_v2"
    assert route.audio_input_format == AudioInputFormat.RAW_PCM16
    assert route.audio_input_format_verified is True
    assert route.audio_selection_mode == AudioSelectionMode.GENERATED
    assert route.audio_preparation_implementation == "pipecat_google_speech_v2_raw_pcm16"


@pytest.mark.parametrize(
    ("provider", "expected_model", "expected_route", "expected_implementation"),
    (
        (
            "assemblyai_realtime",
            Config.ASSEMBLYAI_RT_MODEL,
            "streaming",
            "pipecat_assemblyai_streaming_raw_pcm16",
        ),
        (
            "deepgram",
            Config.DEEPGRAM_MODEL,
            "nova_streaming",
            "pipecat_deepgram_nova_streaming_raw_pcm16",
        ),
        (
            "openai",
            Config.OPENAI_REALTIME_STT_MODEL,
            "realtime_transcription",
            "pipecat_openai_realtime_pcm16_24khz",
        ),
        (
            "google",
            "latest_long",
            "cloud_streaming_v2",
            "pipecat_google_speech_v2_raw_pcm16",
        ),
        (
            "elevenlabs",
            "scribe_v2_realtime",
            "scribe_v2_realtime",
            "pipecat_elevenlabs_scribe_v2_realtime_raw_pcm16",
        ),
        (
            "speechmatics",
            "enhanced",
            "realtime_v2",
            "pipecat_speechmatics_realtime_v2_raw_pcm16",
        ),
    ),
)
def test_streaming_only_background_routes_freeze_exact_pcm_contract(
    provider,
    expected_model,
    expected_route,
    expected_implementation,
):
    route = freeze_provider_route(
        workload="file",
        provider=provider,
        language="de-DE",
        custom_vocab="Scriber,Pipecat",
    )

    assert route.model == expected_model
    assert route.provider_route == expected_route
    assert route.transport == "decoded_pcm"
    assert route.audio_input_format == AudioInputFormat.RAW_PCM16
    assert route.audio_input_format_verified is True
    assert route.audio_selection_mode == AudioSelectionMode.GENERATED
    assert route.audio_preparation_implementation == expected_implementation
    assert route.provider_audio_capability_id == (f"{provider}:{expected_route}:{expected_model}")


@pytest.mark.parametrize(
    "provider",
    (
        "assemblyai_realtime",
        "deepgram",
        "openai",
        "google",
        "elevenlabs",
        "speechmatics",
        "groq",
    ),
)
def test_pipecat_owned_routes_do_not_claim_unknown_models(provider):
    route = freeze_provider_route(
        workload="file",
        provider=provider,
        model="unknown-request-model",
    )

    assert route.provider_audio_capability_id == ""
    assert route.audio_input_format is None
    assert route.audio_input_format_verified is None


@pytest.mark.parametrize(
    ("provider", "expected_model"),
    (
        ("modulate", "velma-2-stt-batch"),
        ("modulate_async", "velma-2-stt-batch"),
        ("openrouter_stt", "microsoft/mai-transcribe-2"),
    ),
)
def test_text_only_route_reports_final_text_with_estimated_timing(provider, expected_model):
    route = freeze_provider_route(
        workload="meeting",
        provider=provider,
        language="de",
    )
    draft = route.snapshot_draft()

    assert route.model == expected_model
    assert route.execution_route()["language"] == "de"
    assert draft.response_shape == "final_text"
    assert draft.timestamp_mode == "estimated"
    assert draft.diarization_mode == "local_fallback_if_enabled"


def test_provider_speaker_zero_and_exact_timing_become_stage_units():
    units, evidence = stage_units_from_provider(
        provider="soniox_async",
        payload={
            "tokens": [
                {"text": "Hallo", "start_ms": 10, "end_ms": 400, "speaker": 0},
                {"text": " Welt", "start_ms": 410, "end_ms": 800, "speaker": 0},
            ]
        },
        text="Hallo Welt",
        duration_ms=1_000,
    )
    assert len(units) == 1
    assert units[0].speaker_key == "0"
    assert units[0].speaker_origin == "provider_native"
    assert units[0].alignment_quality == "exact_word"
    assert evidence["nativeSpeakerEvidence"] is True
    assert evidence["sourceMediaDurationMs"] == 1_000


@pytest.mark.parametrize("placeholder", ["", "--:--", "00:00", "0:00:00", "00:00:00"])
def test_unknown_source_duration_uses_provider_units_without_exact_duration_evidence(placeholder):
    units, evidence = stage_units_from_provider(
        provider="soniox_async",
        payload={
            "tokens": [
                {
                    "text": "Recovered duration.",
                    "start_ms": 0,
                    "end_ms": 5_000,
                    "speaker": 0,
                }
            ]
        },
        text="Recovered duration.",
        duration_ms=duration_label_to_ms(placeholder, fallback_ms=0),
    )

    assert duration_label_to_ms(placeholder, fallback_ms=0) == 0
    assert units[-1].end_ms == 5_000
    assert "sourceMediaDurationMs" not in evidence


def test_soniox_microphone_diarization_preserves_multiple_native_speakers():
    units, evidence = stage_units_from_provider(
        provider="soniox_async",
        payload={
            "tokens": [
                {"text": "First.", "start_ms": 0, "end_ms": 300, "speaker": "1"},
                {"text": "Second.", "start_ms": 400, "end_ms": 700, "speaker": "2"},
                {"text": "Third.", "start_ms": 800, "end_ms": 1_100, "speaker": "3"},
            ]
        },
        text="First. Second. Third.",
        duration_ms=1_100,
        source_track="microphone",
    )

    assert [unit.speaker_key for unit in units] == ["1", "2", "3"]
    assert [unit.speaker_label for unit in units] == ["Speaker 1", "Speaker 2", "Speaker 3"]
    assert all(unit.speaker_origin == "provider_native" for unit in units)
    assert evidence["nativeSpeakerEvidence"] is True


def test_plain_provider_text_is_honestly_estimated_over_duration():
    units, evidence = stage_units_from_provider(
        provider="gemini_stt",
        payload={"text": "Erster Satz. Zweiter Satz."},
        text="Erster Satz. Zweiter Satz.",
        duration_ms=4_000,
    )
    assert len(units) == 2
    assert units[0].start_ms == 0
    assert units[-1].end_ms == 4_000
    assert all(unit.alignment_quality == AlignmentQuality.ESTIMATED for unit in units)
    assert evidence["estimatedTiming"] is True


def test_gemini_transcribe_word_annotations_preserve_timing_and_speakers():
    units, evidence = stage_units_from_provider(
        provider="gemini_stt",
        payload={
            "output_text": "Hallo Welt",
            "steps": [
                {
                    "content": [
                        {
                            "annotations": [
                                {
                                    "type": "word_info",
                                    "text": "Hallo",
                                    "speaker": "spk_1",
                                    "start_offset": "0.100s",
                                    "end_offset": "0.400s",
                                },
                                {
                                    "type": "word_info",
                                    "text": "Welt",
                                    "speaker": "spk_2",
                                    "start_offset": "0.500s",
                                    "end_offset": "0.900s",
                                },
                                {
                                    "type": "word_info",
                                    "text": "ignored",
                                    "start_offset": "invalid",
                                    "end_offset": "1.000s",
                                },
                            ]
                        }
                    ]
                }
            ],
        },
        text="Hallo Welt",
        duration_ms=1_000,
    )

    assert [(unit.text, unit.start_ms, unit.end_ms) for unit in units] == [
        ("Hallo", 100, 400),
        ("Welt", 500, 900),
    ]
    assert [unit.speaker_key for unit in units] == ["spk_1", "spk_2"]
    assert all(unit.alignment_quality == AlignmentQuality.EXACT_WORD for unit in units)
    assert evidence["nativeSpeakerEvidence"] is True


def test_plain_provider_text_estimation_scales_to_many_uniform_blocks():
    block_count = 10_000
    duration_ms = block_count * 100
    text = " ".join(f"Satz {index}." for index in range(block_count))

    units, evidence = stage_units_from_provider(
        provider="gemini_stt",
        payload={"text": text},
        text=text,
        duration_ms=duration_ms,
    )

    assert len(units) == block_count
    assert units[0].start_ms == 0
    assert units[0].end_ms == 100
    assert units[block_count // 2].start_ms == (block_count // 2) * 100
    assert units[-1].end_ms == duration_ms
    assert all(previous.end_ms == current.start_ms for previous, current in zip(units, units[1:], strict=False))
    assert evidence["estimatedTiming"] is True


def test_caption_route_and_units_preserve_real_cue_times_without_speakers():
    route = freeze_caption_route(workload="youtube", language="de", automatic=True)
    units, evidence = stage_units_from_captions([YouTubeCaptionCue(120, 980, "Guten Morgen")])
    assert route.provider == "youtube_captions_auto"
    assert route.snapshot_draft().diarization_mode == "disabled"
    assert units[0].start_ms == 120
    assert units[0].end_ms == 980
    assert units[0].speaker_key is None
    assert evidence["nativeSpeakerEvidence"] is False


def test_duration_parser_supports_hours_and_invalid_fallback():
    assert duration_label_to_ms("1:02:03") == 3_723_000
    assert duration_label_to_ms("--:--", fallback_ms=99) == 99
