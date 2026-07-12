from src.core.provider_capabilities import (
    get_capabilities,
    injects_immediately_in_live_mode,
    meeting_max_duration_seconds,
    supports_direct_file_upload,
    supports_batch_diarization,
    supports_five_hour_meeting,
    supports_word_timestamps,
)


def test_provider_capabilities_known_providers():
    assert supports_direct_file_upload("soniox") is True
    assert supports_direct_file_upload("mistral_async") is True
    assert supports_direct_file_upload("smallest_async") is True
    assert supports_direct_file_upload("assemblyai") is True
    assert supports_direct_file_upload("assemblyai_realtime") is False
    assert supports_direct_file_upload("azure_mai") is True
    assert supports_direct_file_upload("gladia") is True
    assert supports_direct_file_upload("gladia_async") is True
    assert supports_direct_file_upload("deepgram_async") is True
    assert supports_direct_file_upload("openai_async") is True
    assert supports_direct_file_upload("speechmatics_async") is True
    assert supports_direct_file_upload("openai") is False


def test_provider_capabilities_distinguish_streaming_from_segmented_live():
    assert get_capabilities("soniox").supports_live_streaming is True
    assert get_capabilities("smallest").supports_live_streaming is True
    assert get_capabilities("deepgram").supports_live_streaming is True
    assert get_capabilities("gladia").supports_live_streaming is True
    assert get_capabilities("google").supports_live_streaming is True
    assert get_capabilities("speechmatics").supports_live_streaming is True
    assert get_capabilities("assemblyai_realtime").supports_live_streaming is True
    assert get_capabilities("openai").supports_live_streaming is True

    assert get_capabilities("mistral").supports_live_streaming is False
    assert get_capabilities("assemblyai").supports_live_streaming is False
    assert get_capabilities("deepgram_async").supports_live_streaming is False
    assert get_capabilities("gladia_async").supports_live_streaming is False
    assert get_capabilities("openai_async").supports_live_streaming is False
    assert get_capabilities("groq").supports_live_streaming is False
    assert get_capabilities("elevenlabs").supports_live_streaming is True
    assert get_capabilities("speechmatics_async").supports_live_streaming is False


def test_provider_capabilities_injection_flags():
    assert injects_immediately_in_live_mode("mistral") is False
    assert injects_immediately_in_live_mode("mistral_async") is False
    assert injects_immediately_in_live_mode("smallest") is True
    assert injects_immediately_in_live_mode("smallest_async") is False
    assert injects_immediately_in_live_mode("assemblyai") is False
    assert injects_immediately_in_live_mode("assemblyai_realtime") is False
    assert injects_immediately_in_live_mode("azure_mai") is False
    assert injects_immediately_in_live_mode("gladia") is False
    assert injects_immediately_in_live_mode("openai") is False
    assert injects_immediately_in_live_mode("openai_async") is False
    assert get_capabilities("unknown-provider").supports_live_streaming is False


def test_batch_capabilities_describe_the_active_request_and_normalizer_contract():
    # The shared ``soniox`` key still uses async upload for File/YouTube, so it
    # must not accidentally route those jobs through the local diarizer.
    assert supports_batch_diarization("soniox") is True
    assert supports_word_timestamps("soniox") is True

    assert supports_word_timestamps("smallest_async") is True
    assert supports_word_timestamps("assemblyai") is True
    assert supports_word_timestamps("deepgram_async") is True
    assert supports_word_timestamps("speechmatics_async") is True
    assert supports_word_timestamps("openai_async") is True

    # These active adapters request/normalize provider-level intervals only.
    for provider in ("mistral", "mistral_async", "azure_mai", "gladia", "gladia_async"):
        assert supports_word_timestamps(provider) is False


def test_five_hour_meeting_capability_tracks_the_implemented_transport_route():
    assert all(
        supports_five_hour_meeting(provider)
        for provider in (
            "soniox", "soniox_async", "assemblyai", "azure_mai", "onnx_local",
        )
    )
    assert meeting_max_duration_seconds("soniox_async") == 18_000
    assert meeting_max_duration_seconds("mistral_async") == 10_800
    assert meeting_max_duration_seconds("mistral_async", "voxtral-mini-2507") == 1_800
    assert meeting_max_duration_seconds("gladia_async") == 8_100
    assert meeting_max_duration_seconds("deepgram_async") is None
    assert all(
        not supports_five_hour_meeting(provider)
        for provider in (
            "smallest", "smallest_async", "openai_async", "gemini_stt",
            "mistral_async", "gladia_async", "deepgram_async", "speechmatics_async",
            "groq", "unknown-provider",
        )
    )
