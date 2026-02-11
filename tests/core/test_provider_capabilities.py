from src.core.provider_capabilities import (
    get_capabilities,
    injects_immediately_in_live_mode,
    supports_direct_file_upload,
)


def test_provider_capabilities_known_providers():
    assert supports_direct_file_upload("soniox") is True
    assert supports_direct_file_upload("mistral_async") is True
    assert supports_direct_file_upload("openai") is False


def test_provider_capabilities_injection_flags():
    assert injects_immediately_in_live_mode("mistral") is True
    assert injects_immediately_in_live_mode("mistral_async") is False
    assert injects_immediately_in_live_mode("openai") is False
    assert get_capabilities("unknown-provider").supports_live_streaming is True

