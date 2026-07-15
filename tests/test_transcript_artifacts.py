from src.config import Config
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


def test_provider_route_can_freeze_task_scoped_transport():
    route = freeze_provider_route(
        workload="meeting",
        provider="soniox_async",
        transport="webm_opus_task_derivative",
    )

    assert route.transport == "webm_opus_task_derivative"
    assert route.execution_route()["transport"] == "webm_opus_task_derivative"
    assert route.snapshot_draft().transport == "webm_opus_task_derivative"


def test_groq_batch_model_is_reported_as_the_actual_supported_model():
    assert provider_batch_model("groq") == "whisper-large-v3-turbo"
    assert freeze_provider_route(
        workload="meeting",
        provider="groq",
    ).model == "whisper-large-v3-turbo"


def test_modulate_route_reports_final_text_with_estimated_timing():
    for provider in ("modulate", "modulate_async"):
        route = freeze_provider_route(
            workload="meeting",
            provider=provider,
            language="de",
        )
        draft = route.snapshot_draft()

        assert route.model == "velma-2-stt-batch"
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


def test_caption_route_and_units_preserve_real_cue_times_without_speakers():
    route = freeze_caption_route(workload="youtube", language="de", automatic=True)
    units, evidence = stage_units_from_captions(
        [YouTubeCaptionCue(120, 980, "Guten Morgen")]
    )
    assert route.provider == "youtube_captions_auto"
    assert route.snapshot_draft().diarization_mode == "disabled"
    assert units[0].start_ms == 120
    assert units[0].end_ms == 980
    assert units[0].speaker_key is None
    assert evidence["nativeSpeakerEvidence"] is False


def test_duration_parser_supports_hours_and_invalid_fallback():
    assert duration_label_to_ms("1:02:03") == 3_723_000
    assert duration_label_to_ms("--:--", fallback_ms=99) == 99
