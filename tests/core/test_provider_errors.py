from src.core.error_taxonomy import ErrorCategory
from src.core.provider_errors import provider_user_error


def test_soniox_realtime_model_error_points_to_v5():
    info = provider_user_error(
        "soniox",
        '{"error_code": 400, "error_type": "model_not_available", "error_message": "Invalid model specified."}',
    )

    assert info.provider == "soniox"
    assert info.provider_label == "Soniox"
    assert info.category is ErrorCategory.CONFIG_INVALID
    assert info.code == "model_not_available"
    assert "stt-rt-v5" in info.message
    assert info.retryable is False


def test_soniox_async_model_error_points_to_async_v5():
    info = provider_user_error("soniox_async", "Soniox async error: model_not_available")

    assert info.category is ErrorCategory.CONFIG_INVALID
    assert "stt-async-v5" in info.message


def test_azure_mai_service_unavailable_gets_provider_specific_message():
    info = provider_user_error(
        "azure_mai",
        "azure mai error: Azure MAI transcription failed (503): "
        "MAI service returned an error: ServiceUnavailable - no healthy upstream",
    )

    assert info.category is ErrorCategory.TRANSIENT_PROVIDER
    assert info.code == "ServiceUnavailable"
    assert info.title == "Microsoft MAI Transcribe error"
    assert "Microsoft MAI Transcribe is temporarily unavailable" in info.message
    assert info.retryable is True


def test_mistral_rate_limit_is_provider_limit():
    info = provider_user_error(
        "mistral_async",
        '{"type": "rate_limit_error", "message": "Too many requests"}',
    )

    assert info.category is ErrorCategory.PROVIDER_LIMIT
    assert info.code == "rate_limit_error"
    assert "Mistral rate limit" in info.message
    assert info.retryable is True


def test_smallest_forbidden_explains_workspace_or_product_scope():
    info = provider_user_error("smallest", "403 Forbidden: key lacks permission for this resource")

    assert info.category is ErrorCategory.AUTH_INVALID
    assert info.code == "403"
    assert "workspace, product, or trial limit" in info.message


def test_deepgram_data_close_code_is_audio_error():
    info = provider_user_error("deepgram", "websocket closed 1008 DATA-0000 payload cannot be decoded as audio")

    assert info.category is ErrorCategory.AUDIO_INVALID
    assert info.code == "DATA-0000"
    assert "could not decode" in info.message


def test_openai_quota_or_rate_limit_is_provider_limit():
    info = provider_user_error("openai", "RateLimitError: insufficient_quota 429")

    assert info.category is ErrorCategory.PROVIDER_LIMIT
    assert info.code == "429"
    assert "OpenAI rate limit or quota reached" in info.message


def test_missing_api_key_is_provider_specific_configuration_error():
    info = provider_user_error("soniox", "Soniox API Key is missing.")

    assert info.category is ErrorCategory.CONFIG_INVALID
    assert info.code == "missing_api_key"
    assert info.message == "Soniox API key is missing. Add it in Settings."


def test_missing_api_key_uses_provider_specific_labels_for_async_and_optional_providers():
    cases = {
        "assemblyai": "Assembly AI Universal-3.5-Pro",
        "assemblyai_realtime": "Assembly AI Universal-3.5-Pro Realtime",
        "mistral": "Mistral (Realtime)",
        "mistral_async": "Mistral (Async)",
        "smallest": "Smallest AI (Realtime)",
        "smallest_async": "Smallest AI (Async)",
        "elevenlabs": "ElevenLabs",
        "gladia": "Gladia (Streaming)",
    }

    for provider, label in cases.items():
        info = provider_user_error(provider, f"{label} API Key is missing.")

        assert info.provider == provider
        assert info.provider_label == label
        assert info.category is ErrorCategory.CONFIG_INVALID
        assert info.code == "missing_api_key"
        assert info.message == f"{label} API key is missing. Add it in Settings."
        assert info.retryable is False


def test_modulate_aiohttp_connect_failure_is_retryable_network_error():
    info = provider_user_error(
        "modulate",
        "modulate realtime error: Cannot connect to host "
        "modulate-developer-apis.com:443 ssl:default",
    )

    assert info.provider == "modulate"
    assert info.category is ErrorCategory.TRANSIENT_NETWORK
    assert "Modulate connection" in info.message
    assert info.code == ""
    assert info.retryable is True
