from src.core.provider_capabilities import (
    get_capabilities,
    injects_immediately_in_live_mode,
    supports_direct_file_upload,
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

    assert get_capabilities("mistral").supports_live_streaming is False
    assert get_capabilities("assemblyai").supports_live_streaming is False
    assert get_capabilities("deepgram_async").supports_live_streaming is False
    assert get_capabilities("gladia_async").supports_live_streaming is False
    assert get_capabilities("openai").supports_live_streaming is False
    assert get_capabilities("openai_async").supports_live_streaming is False
    assert get_capabilities("groq").supports_live_streaming is False
    assert get_capabilities("elevenlabs").supports_live_streaming is False
    assert get_capabilities("speechmatics_async").supports_live_streaming is False


def test_provider_capabilities_injection_flags():
    assert injects_immediately_in_live_mode("mistral") is True
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
