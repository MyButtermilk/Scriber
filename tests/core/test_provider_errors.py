import pytest

from src.core.error_taxonomy import ErrorCategory
from src.core.provider_errors import (
    parse_provider_json_response,
    provider_transport_error,
    provider_user_error,
)


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


def test_gemini_model_error_explains_dedicated_transcribe_contract():
    error = provider_transport_error(
        "gemini",
        "transcription",
        code="model_not_available",
        retryable=False,
    )

    info = provider_user_error(None, error)

    assert info.provider == "gemini"
    assert info.provider_label == "Gemini 3.5 Transcribe"
    assert info.category is ErrorCategory.CONFIG_INVALID
    assert info.code == "model_not_available"
    assert "gemini-3.5-transcribe" in info.message
    assert "SCRIBER_GEMINI_STT_MODEL" in info.message
    assert info.retryable is False


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
        "mistral": "Mistral (Segmented)",
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
        "modulate realtime error: Cannot connect to host modulate-developer-apis.com:443 ssl:default",
    )

    assert info.provider == "modulate"
    assert info.category is ErrorCategory.TRANSIENT_NETWORK
    assert "Modulate connection" in info.message
    assert info.code == ""
    assert info.retryable is True


def test_provider_transport_error_discards_private_response_body():
    private_marker = "private spoken words from the provider response"
    error = provider_transport_error(
        "openai",
        "transcription",
        status=429,
        response_body=(f'{{"error":{{"code":"rate_limit_error","message":"{private_marker}"}}}}'),
    )

    assert private_marker not in str(error)
    assert private_marker not in repr(error)
    assert error.status == 429
    assert error.code == "rate_limit_error"
    assert error.response_bytes

    info = provider_user_error(None, error)
    assert info.provider == "openai"
    assert info.category is ErrorCategory.PROVIDER_LIMIT
    assert info.code == "rate_limit_error"
    assert private_marker not in info.message


def test_provider_transport_error_rejects_unbounded_or_message_like_codes():
    private_marker = "private response sentence with spaces"
    error = provider_transport_error(
        "deepgram",
        "transcription",
        status=500,
        response_body=f'{{"code":"{private_marker}"}}',
    )

    assert error.code == ""
    assert private_marker not in str(error)


def test_provider_transport_error_rejects_private_identifier_shaped_codes():
    private_markers = (
        "patient_Alice_2026",
        "PRIVATE_TRANSCRIPT_91a5e7",
        "550e8400-e29b-41d4-a716-446655440000",
    )

    for marker in private_markers:
        error = provider_transport_error(
            "mistral",
            "transcription",
            status=500,
            response_body=f'{{"code":"{marker}"}}',
        )
        info = provider_user_error("mistral", error)

        assert error.code == ""
        assert marker not in str(error)
        assert marker not in info.code
        assert marker not in info.message


def test_provider_transport_error_derives_only_bounded_semantic_signals():
    cases = (
        (
            "mistral",
            404,
            '{"message":"model not found for PRIVATE_TRANSCRIPT"}',
            "model_not_available",
            ErrorCategory.CONFIG_INVALID,
        ),
        (
            "mistral",
            400,
            '{"message":"audio duration too long: PRIVATE_TRANSCRIPT"}',
            "audio_limit_exceeded",
            ErrorCategory.CONFIG_INVALID,
        ),
        (
            "gladia",
            403,
            '{"message":"quota exceeded for patient_Alice_2026"}',
            "quota_exceeded",
            ErrorCategory.AUTH_INVALID,
        ),
        (
            "gladia",
            400,
            '{"message":"unsupported audio format PRIVATE_TRANSCRIPT"}',
            "unsupported_audio",
            ErrorCategory.CONFIG_INVALID,
        ),
    )

    for provider, status, body, expected_code, expected_category in cases:
        error = provider_transport_error(
            provider,
            "transcription",
            status=status,
            response_body=body,
        )
        info = provider_user_error(provider, error)

        assert error.code == expected_code
        assert info.category is expected_category
        assert "PRIVATE_TRANSCRIPT" not in str(error)
        assert "patient_Alice_2026" not in str(error)


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        ('{"message":"private transcript says invalid api key"}', "authentication_error"),
        ('{"message":"quota exceeded in private transcript"}', "quota_exceeded"),
    ],
)
def test_http_500_status_wins_over_body_derived_signal(body, expected_code):
    error = provider_transport_error(
        "gladia",
        "transcription",
        status=500,
        response_body=body,
    )

    assert error.code == expected_code
    info = provider_user_error("gladia", error)
    assert info.category is ErrorCategory.TRANSIENT_PROVIDER
    assert info.code == expected_code
    assert info.retryable is True


@pytest.mark.parametrize(
    ("status", "body", "expected_category"),
    [
        (
            400,
            '{"message":"private transcript says invalid api key"}',
            ErrorCategory.CONFIG_INVALID,
        ),
        (
            404,
            '{"message":"quota exceeded in private transcript"}',
            ErrorCategory.CONFIG_INVALID,
        ),
        (
            409,
            '{"message":"unsupported audio in private transcript"}',
            ErrorCategory.CONFIG_INVALID,
        ),
        (
            413,
            '{"message":"invalid api key in private transcript"}',
            ErrorCategory.AUDIO_INVALID,
        ),
        (
            415,
            '{"message":"quota exceeded in private transcript"}',
            ErrorCategory.AUDIO_INVALID,
        ),
        (
            422,
            '{"message":"invalid api key in private transcript"}',
            ErrorCategory.AUDIO_INVALID,
        ),
    ],
)
def test_http_4xx_status_wins_over_body_derived_signal(
    status,
    body,
    expected_category,
):
    error = provider_transport_error(
        "mistral",
        "transcription",
        status=status,
        response_body=body,
    )

    info = provider_user_error("mistral", error)

    assert info.category is expected_category
    assert info.retryable is False


def test_provider_json_parser_drops_invalid_document_and_exception_context():
    private_marker = "PRIVATE_TRANSCRIPT_aa76"

    with pytest.raises(RuntimeError) as caught:
        parse_provider_json_response(
            "assemblyai",
            "upload_response",
            "{invalid " + private_marker,
        )

    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert not hasattr(caught.value, "doc")
    assert private_marker not in str(caught.value)
    assert private_marker not in repr(caught.value)
