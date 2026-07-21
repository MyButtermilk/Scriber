from datetime import date

import pytest

from src.config import Config
from src.core.provider_audio_formats import (
    CAPABILITY_REVISION,
    AudioCodec,
    AudioContainer,
    AudioInputFormat,
    AudioSelectionMode,
    InactiveProviderAudioRoute,
    PROVIDER_AUDIO_CAPABILITY_MATRIX,
    ProviderAudioRouteKind,
    SPEECHMATICS_REALTIME_DEFAULT_BASE_URL,
    UnsupportedAudioInputFormat,
    UnsupportedProviderAudioRoute,
    coerce_audio_input_format,
    exact_audio_input_format,
    resolve_batch_provider_audio_capabilities,
    resolve_provider_audio_capabilities,
    realtime_pcm_preparation_implementation,
    select_audio_input_format,
    speechmatics_realtime_base_url,
    speechmatics_realtime_endpoint_is_custom,
    supports_exact_audio_input_format,
)


def test_audio_formats_keep_container_and_codec_exact():
    assert AudioInputFormat.OGG_OPUS.container == AudioContainer.OGG
    assert AudioInputFormat.OGG_OPUS.codec == AudioCodec.OPUS
    assert AudioInputFormat.WEBM_VORBIS.container == AudioContainer.WEBM
    assert AudioInputFormat.WEBM_VORBIS.codec == AudioCodec.VORBIS
    assert (
        exact_audio_input_format(AudioContainer.OGG, AudioCodec.OPUS)
        == AudioInputFormat.OGG_OPUS
    )

    with pytest.raises(UnsupportedAudioInputFormat):
        coerce_audio_input_format("ogg")
    with pytest.raises(UnsupportedAudioInputFormat):
        coerce_audio_input_format("webm")


def test_generic_ogg_and_webm_evidence_does_not_grant_opus():
    capability = resolve_batch_provider_audio_capabilities(
        "mistral_async", "voxtral-mini-2602"
    )
    assert AudioContainer.OGG in capability.batch_generic_containers
    assert AudioContainer.WEBM in capability.batch_generic_containers
    assert not supports_exact_audio_input_format(
        capability,
        AudioInputFormat.OGG_OPUS,
        route_kind=ProviderAudioRouteKind.BATCH,
    )
    assert not supports_exact_audio_input_format(
        capability,
        AudioInputFormat.WEBM_OPUS,
        route_kind=ProviderAudioRouteKind.BATCH,
    )

    # An unsupported original container is transcoded to the first verified
    # representation; it is never reinterpreted as Opus.
    selected = select_audio_input_format(
        capability,
        route_kind=ProviderAudioRouteKind.BATCH,
        original_format=AudioInputFormat.OGG_OPUS,
    )
    assert selected.audio_format == AudioInputFormat.WAV_PCM16
    assert selected.mode == AudioSelectionMode.GENERATED


def test_exact_original_passthrough_precedes_generated_preferences():
    capability = resolve_batch_provider_audio_capabilities(
        "smallest_async", "pulse"
    )
    selected = select_audio_input_format(
        capability,
        route_kind=ProviderAudioRouteKind.BATCH,
        original_format=AudioInputFormat.FLAC,
    )
    assert selected.audio_format == AudioInputFormat.FLAC
    assert selected.mode == AudioSelectionMode.ORIGINAL_PASSTHROUGH
    assert selected.capability_id == capability.capability_id
    assert selected.capability_revision == CAPABILITY_REVISION

    soniox = resolve_batch_provider_audio_capabilities(
        "soniox_async", "stt-async-v5"
    )
    assert AudioInputFormat.WEBM_OPUS in soniox.direct_passthrough_formats
    assert AudioInputFormat.AAC not in soniox.direct_passthrough_formats


def test_batch_and_realtime_formats_are_route_scoped_and_separate():
    batch = resolve_provider_audio_capabilities(
        "assemblyai", "pre_recorded", "universal-3-5-pro"
    )
    realtime = resolve_provider_audio_capabilities(
        "assemblyai_realtime", "streaming", "universal-3-5-pro"
    )
    assert AudioInputFormat.OGG_OPUS in batch.batch_formats
    assert not batch.realtime_formats
    assert AudioInputFormat.RAW_PCM16 in realtime.realtime_formats
    assert not realtime.batch_formats
    assert not supports_exact_audio_input_format(
        realtime,
        AudioInputFormat.OGG_OPUS,
        route_kind=ProviderAudioRouteKind.REALTIME,
    )

    gladia_live = resolve_provider_audio_capabilities(
        "gladia", "v2_live", "solaria-1"
    )
    assert AudioInputFormat.OGG_OPUS not in gladia_live.realtime_formats

    google_live = resolve_provider_audio_capabilities(
        "google", "cloud_streaming_v2", "latest_long"
    )
    assert google_live.realtime_formats == {AudioInputFormat.RAW_PCM16}

    groq_segmented = resolve_provider_audio_capabilities(
        "groq",
        "openai_v1_segmented_audio_transcriptions",
        "whisper-large-v3-turbo",
    )
    assert groq_segmented.route_kind == ProviderAudioRouteKind.BATCH
    assert AudioInputFormat.WAV_PCM16 in groq_segmented.batch_formats


@pytest.mark.parametrize(
    ("provider", "route", "model"),
    (
        ("assemblyai_realtime", "streaming", "universal-3-5-pro"),
        ("deepgram", "nova_streaming", "nova-3"),
        ("openai", "realtime_transcription", "gpt-realtime-whisper"),
        ("google", "cloud_streaming_v2", "latest_long"),
        ("elevenlabs", "scribe_v2_realtime", "scribe_v2_realtime"),
        ("speechmatics", "realtime_v2", "enhanced"),
    ),
)
def test_streaming_only_routes_have_exact_raw_pcm16_implementation(
    provider,
    route,
    model,
):
    capability = resolve_provider_audio_capabilities(provider, route, model)

    assert capability.route_kind == ProviderAudioRouteKind.REALTIME
    assert AudioInputFormat.RAW_PCM16 in capability.realtime_formats
    assert realtime_pcm_preparation_implementation(provider)


def test_speechmatics_realtime_endpoint_matches_pipecat_1_5_default():
    assert (
        speechmatics_realtime_base_url(None)
        == SPEECHMATICS_REALTIME_DEFAULT_BASE_URL
    )
    assert (
        speechmatics_realtime_base_url(
            SPEECHMATICS_REALTIME_DEFAULT_BASE_URL + "/"
        )
        == SPEECHMATICS_REALTIME_DEFAULT_BASE_URL
    )
    assert speechmatics_realtime_endpoint_is_custom(None) is False
    assert speechmatics_realtime_endpoint_is_custom("") is False
    assert (
        speechmatics_realtime_endpoint_is_custom(
            "wss://private.invalid/speechmatics/v2"
        )
        is True
    )


def test_google_and_groq_legacy_or_unknown_api_routes_fail_closed():
    for provider, route, model in (
        ("google", "cloud_streaming", "latest_long"),
        ("google", "cloud_streaming_v1", "latest_long"),
        ("groq", "segmented_audio_transcriptions", "whisper-large-v3-turbo"),
        ("groq", "openai_v2_segmented_audio_transcriptions", "whisper-large-v3-turbo"),
    ):
        with pytest.raises(UnsupportedProviderAudioRoute):
            resolve_provider_audio_capabilities(provider, route, model)


def test_unknown_model_and_custom_endpoint_fail_closed():
    with pytest.raises(UnsupportedProviderAudioRoute):
        resolve_batch_provider_audio_capabilities(
            "azure_mai", "custom-mai-model"
        )
    with pytest.raises(UnsupportedProviderAudioRoute):
        resolve_batch_provider_audio_capabilities(
            "azure_mai",
            "mai-transcribe-1.5",
            custom_endpoint=True,
        )
    with pytest.raises(UnsupportedProviderAudioRoute):
        resolve_batch_provider_audio_capabilities("unknown", "default")


def test_openrouter_mai_is_planned_but_inactive_and_never_inherits_generic_opus():
    with pytest.raises(InactiveProviderAudioRoute):
        resolve_provider_audio_capabilities(
            "openrouter_stt",
            "audio_transcriptions",
            "microsoft/mai-transcribe-1.5",
        )

    planned = resolve_provider_audio_capabilities(
        "openrouter_stt",
        "audio_transcriptions",
        "microsoft/mai-transcribe-1.5",
        include_inactive=True,
    )
    assert planned.active is False
    assert planned.batch_formats == {
        AudioInputFormat.WAV_PCM16,
        AudioInputFormat.MP3,
        AudioInputFormat.FLAC,
    }
    assert AudioInputFormat.OGG_OPUS not in planned.batch_formats
    assert AudioInputFormat.WEBM_OPUS not in planned.batch_formats
    with pytest.raises(InactiveProviderAudioRoute):
        select_audio_input_format(
            planned,
            route_kind=ProviderAudioRouteKind.BATCH,
        )


def test_current_gemini_route_is_vorbis_not_opus():
    capability = resolve_batch_provider_audio_capabilities(
        "gemini_stt", "gemini-2.5-flash"
    )
    assert AudioInputFormat.OGG_VORBIS in capability.batch_formats
    assert AudioInputFormat.OGG_OPUS not in capability.batch_formats
    assert AudioInputFormat.WEBM_OPUS not in capability.batch_formats


def test_modulate_webm_opus_is_not_promoted_without_exact_batch_evidence():
    batch = resolve_batch_provider_audio_capabilities(
        "modulate_async", "velma-2-stt-batch"
    )
    realtime = resolve_provider_audio_capabilities(
        "modulate", "velma_2_streaming", "multilingual"
    )
    assert AudioInputFormat.WEBM_OPUS not in batch.batch_formats
    assert AudioInputFormat.MP3 in batch.batch_formats
    assert AudioInputFormat.WEBM_OPUS not in realtime.realtime_formats


def test_matrix_entries_carry_evidence_date_and_revision():
    assert PROVIDER_AUDIO_CAPABILITY_MATRIX
    for capability in PROVIDER_AUDIO_CAPABILITY_MATRIX:
        assert capability.capability_id
        assert capability.revision == CAPABILITY_REVISION
        assert capability.verified_at == date(2026, 7, 20)
        assert capability.evidence_reference


def test_every_settings_stt_provider_has_an_active_route_or_local_classification():
    settings_providers = set(Config.SERVICE_LABELS)
    active_providers = {
        capability.provider
        for capability in PROVIDER_AUDIO_CAPABILITY_MATRIX
        if capability.active
    }
    assert settings_providers <= active_providers


def test_local_provider_cannot_be_selected_as_an_upload_format():
    capability = resolve_batch_provider_audio_capabilities(
        "onnx_local", "nemo-parakeet-tdt-0.6b-v3"
    )
    assert capability.route_kind == ProviderAudioRouteKind.LOCAL_NO_UPLOAD
    assert not capability.batch_formats
    with pytest.raises(UnsupportedProviderAudioRoute):
        select_audio_input_format(
            capability,
            route_kind=ProviderAudioRouteKind.LOCAL_NO_UPLOAD,
        )
