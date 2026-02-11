from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    TRANSIENT_NETWORK = "transient_network"
    TRANSIENT_PROVIDER = "transient_provider"
    AUTH_INVALID = "auth_invalid"
    AUTH_EXPIRED = "auth_expired"
    DEVICE_UNAVAILABLE = "device_unavailable"
    DEVICE_PERMISSION = "device_permission"
    CONFIG_INVALID = "config_invalid"
    PROVIDER_LIMIT = "provider_limit"
    INTERNAL_BUG = "internal_bug"


_CATEGORY_TO_USER_MESSAGE: dict[ErrorCategory, str] = {
    ErrorCategory.TRANSIENT_NETWORK: "Could not connect to STT service. Please check your internet connection and try again.",
    ErrorCategory.TRANSIENT_PROVIDER: "STT service is temporarily unavailable. Please try again shortly.",
    ErrorCategory.AUTH_INVALID: "Invalid API key. Please check your credentials in Settings.",
    ErrorCategory.AUTH_EXPIRED: "STT service requires payment or your plan has expired.",
    ErrorCategory.DEVICE_UNAVAILABLE: "Selected microphone is not available. Please reconnect it or choose another input device.",
    ErrorCategory.DEVICE_PERMISSION: "Microphone access is blocked. Please grant microphone permissions and retry.",
    ErrorCategory.CONFIG_INVALID: "Invalid configuration detected. Please verify your settings.",
    ErrorCategory.PROVIDER_LIMIT: "Provider rate limit or quota reached. Please wait or switch provider.",
    ErrorCategory.INTERNAL_BUG: "Recording failed due to an internal error. Please retry.",
}


def classify_error_message(message: str) -> ErrorCategory:
    text = (message or "").lower().strip()
    if not text:
        return ErrorCategory.INTERNAL_BUG

    if any(token in text for token in ("payment required", "plan expired", "subscription")):
        return ErrorCategory.AUTH_EXPIRED
    if any(token in text for token in ("402", "quota exceeded", "credit", "insufficient balance")):
        return ErrorCategory.PROVIDER_LIMIT
    if any(token in text for token in ("401", "unauthorized", "invalid api key", "authentication failed")):
        return ErrorCategory.AUTH_INVALID
    if any(token in text for token in ("403", "forbidden")):
        return ErrorCategory.AUTH_INVALID
    if any(token in text for token in ("permission denied", "access denied", "microphone access")):
        return ErrorCategory.DEVICE_PERMISSION
    if any(token in text for token in ("device unavailable", "no default input", "invalid device")):
        return ErrorCategory.DEVICE_UNAVAILABLE
    if any(token in text for token in ("timeout", "timed out", "connection", "websocket", "handshake", "dns")):
        return ErrorCategory.TRANSIENT_NETWORK
    if any(token in text for token in ("rate limit", "429", "too many requests")):
        return ErrorCategory.PROVIDER_LIMIT
    if any(token in text for token in ("invalid config", "missing api key", "configuration")):
        return ErrorCategory.CONFIG_INVALID
    if any(token in text for token in ("service unavailable", "internal server error", "503", "502", "bad gateway")):
        return ErrorCategory.TRANSIENT_PROVIDER
    return ErrorCategory.INTERNAL_BUG


def classify_exception(exc: Exception) -> ErrorCategory:
    return classify_error_message(str(exc))


def user_message_for_category(category: ErrorCategory) -> str:
    return _CATEGORY_TO_USER_MESSAGE.get(category, _CATEGORY_TO_USER_MESSAGE[ErrorCategory.INTERNAL_BUG])


def user_message_for_exception(exc: Exception) -> str:
    return user_message_for_category(classify_exception(exc))


def is_retryable(category: ErrorCategory) -> bool:
    return category in {ErrorCategory.TRANSIENT_NETWORK, ErrorCategory.TRANSIENT_PROVIDER}

