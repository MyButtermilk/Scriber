from src.core.error_taxonomy import ErrorCategory, classify_error_message, is_retryable, user_message_for_category


def test_classify_auth_invalid():
    assert classify_error_message("401 unauthorized: invalid api key") is ErrorCategory.AUTH_INVALID


def test_classify_provider_limit():
    assert classify_error_message("429 rate limit exceeded") is ErrorCategory.PROVIDER_LIMIT


def test_classify_network_timeout():
    assert classify_error_message("websocket timeout while connecting") is ErrorCategory.TRANSIENT_NETWORK


def test_classify_device_permission():
    assert classify_error_message("microphone access denied by OS") is ErrorCategory.DEVICE_PERMISSION


def test_retryable_categories():
    assert is_retryable(ErrorCategory.TRANSIENT_NETWORK) is True
    assert is_retryable(ErrorCategory.TRANSIENT_PROVIDER) is True
    assert is_retryable(ErrorCategory.AUTH_INVALID) is False


def test_user_message_exists_for_all_categories():
    for category in ErrorCategory:
        message = user_message_for_category(category)
        assert isinstance(message, str)
        assert message

