from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

from src.config import Config
from src.core.error_taxonomy import ErrorCategory, classify_error_message, is_retryable, user_message_for_category
from src.runtime.provider_dependencies import ProviderRuntimeDependencyError


class ProviderTransportError(RuntimeError):
    """Credential-free provider failure safe for logs and public classifiers.

    Provider response bodies can contain echoed prompts, transcript fragments,
    or other user content.  This exception deliberately retains only bounded
    protocol metadata and never stores the response body.
    """

    def __init__(
        self,
        *,
        provider: str,
        operation: str,
        status: int | None = None,
        code: str = "",
        retryable: bool | None = None,
        response_bytes: int | None = None,
    ) -> None:
        self.provider = _bounded_identifier(provider, fallback="provider")
        self.operation = _bounded_identifier(operation, fallback="request")
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        self.code = code if _is_safe_code(code) else ""
        self.retryable = retryable
        self.response_bytes = (
            max(0, int(response_bytes))
            if isinstance(response_bytes, int) and not isinstance(response_bytes, bool)
            else None
        )

        details: list[str] = []
        if self.status is not None:
            details.append(f"status={self.status}")
        if self.code:
            details.append(f"code={self.code}")
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"{self.provider} {self.operation} failed{suffix}")


@dataclass(frozen=True)
class ProviderUserError:
    provider: str
    provider_label: str
    title: str
    message: str
    category: ErrorCategory
    code: str = ""
    retryable: bool = False


# Do not mistake a URI/host port such as ``example.com:443`` for an HTTP
# status.  Provider exceptions commonly include that form without any response
# having been received at all.
_HTTP_STATUS_RE = re.compile(r"(?<![\d:])([1-5]\d{2})(?!\d)")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{2,80}$")
_KNOWN_CODES = (
    "model_not_available",
    "model_not_found",
    "invalid_request_error",
    "authentication_error",
    "rate_limit_error",
    "quota_exceeded",
    "server_error",
    "ServiceUnavailable",
    "InternalServerError",
    "InvalidRequest",
    "InvalidArgument",
    "PipelineError",
    "Conflict",
    "NotFound",
    "NoSuchKey",
    "ASR_UNPROCESSABLE_ENTITY",
    "TOO_MANY_REQUESTS",
    "INVALID_AUTH",
    "INSUFFICIENT_PERMISSIONS",
    "unsupported_audio",
    "audio_limit_exceeded",
    "audio_error",
    "missing_upload_url",
    "missing_transcript_id",
    "missing_file_uri",
    "missing_job_id",
    "missing_provider_runtime",
    "missing_api_key",
    "provider_error",
    "invalid_json",
    "empty_response",
    "DATA-0000",
    "NET-0000",
    "NET-0001",
)
_SAFE_EXCEPTION_CODES = (
    "APIError",
    "APIConnectionError",
    "APITimeoutError",
    "AuthenticationError",
    "BadRequestError",
    "ClientError",
    "ClientConnectionError",
    "ClientConnectorCertificateError",
    "ClientConnectorError",
    "ClientConnectorSSLError",
    "ClientOSError",
    "ClientPayloadError",
    "ClientResponseError",
    "ConnectionTimeoutError",
    "JSONDecodeError",
    "OSError",
    "PermissionDeniedError",
    "RateLimitError",
    "RuntimeError",
    "ServerConnectionError",
    "ServerDisconnectedError",
    "ServerTimeoutError",
    "SocketTimeoutError",
    "TimeoutError",
    "UnprocessableEntityError",
)
_SAFE_PUBLIC_CODES = frozenset(
    code.casefold() for code in (*_KNOWN_CODES, *_SAFE_EXCEPTION_CODES, "1008", "4001", "4029")
)


def provider_user_error(provider: str | None, error: Exception | str) -> ProviderUserError:
    dependency_error = error if isinstance(error, ProviderRuntimeDependencyError) else None
    transport_error = error if isinstance(error, ProviderTransportError) else None
    raw = _error_text(error)
    payload = _parse_payload(raw)
    normalized_provider = _normalize_provider(
        provider or getattr(dependency_error, "provider", None) or getattr(transport_error, "provider", None),
        raw,
    )
    label = _provider_label(normalized_provider)
    combined = _combined_text(raw, payload)
    status = transport_error.status if transport_error is not None else _status_code(raw, payload)
    code = (
        transport_error.code
        if transport_error is not None and transport_error.code
        else _public_error_code(raw, payload, status=status)
    )

    if dependency_error:
        return _make_error(
            normalized_provider,
            label,
            ErrorCategory.CONFIG_INVALID,
            f"{label} runtime is missing from this Scriber build. Reinstall Scriber or switch transcription provider.",
            code="missing_provider_runtime",
            retryable=False,
        )

    if _has(combined, "api key is missing", "api_key is missing", "missing api key"):
        return _make_error(
            normalized_provider,
            label,
            ErrorCategory.CONFIG_INVALID,
            f"{label} API key is missing. Add it in Settings.",
            code="missing_api_key",
            retryable=False,
        )

    family = _provider_family(normalized_provider)
    if family == "soniox":
        specific = _classify_soniox(normalized_provider, label, combined, status, code)
    elif family == "azure":
        specific = _classify_azure(normalized_provider, label, combined, status, code)
    elif family == "assemblyai":
        specific = _classify_assemblyai(normalized_provider, label, combined, status, code)
    elif family == "mistral":
        specific = _classify_mistral(normalized_provider, label, combined, status, code)
    elif family == "smallest":
        specific = _classify_smallest(normalized_provider, label, combined, status, code)
    elif family == "deepgram":
        specific = _classify_deepgram(normalized_provider, label, combined, status, code)
    elif family == "openai":
        specific = _classify_openai(normalized_provider, label, combined, status, code)
    elif family == "modulate":
        specific = _classify_modulate(normalized_provider, label, combined, status, code)
    else:
        specific = None

    if specific:
        if transport_error is not None and transport_error.status is not None and transport_error.status >= 400:
            authoritative_category = _transport_error_category(
                transport_error,
                fallback=specific.category,
            )
            return _make_error(
                normalized_provider,
                label,
                authoritative_category,
                (
                    specific.message
                    if specific.category is authoritative_category
                    else _generic_provider_message(label, authoritative_category)
                ),
                code=transport_error.code or str(transport_error.status or ""),
                retryable=transport_error.retryable,
            )
        return specific

    category = classify_error_message(raw)
    if transport_error is not None:
        category = _transport_error_category(transport_error, fallback=category)
    return _make_error(
        normalized_provider,
        label,
        category,
        _generic_provider_message(label, category),
        code=code,
        retryable=transport_error.retryable if transport_error is not None else None,
    )


def provider_transport_error(
    provider: str,
    operation: str,
    *,
    status: int | None = None,
    response_body: str | bytes | None = None,
    code: str = "",
    retryable: bool | None = None,
) -> ProviderTransportError:
    """Build a sanitized provider error without retaining response content."""

    extracted_code = code if _is_safe_code(code) else _provider_code_from_body(response_body)
    if isinstance(response_body, bytes):
        response_bytes = len(response_body)
    elif isinstance(response_body, str):
        response_bytes = len(response_body.encode("utf-8", errors="replace"))
    else:
        response_bytes = None
    return ProviderTransportError(
        provider=provider,
        operation=operation,
        status=status,
        code=extracted_code,
        retryable=retryable,
        response_bytes=response_bytes,
    )


def parse_provider_json_response(
    provider: str,
    operation: str,
    response_body: str,
    *,
    empty_value: Any = None,
) -> Any:
    """Parse provider JSON without allowing ``JSONDecodeError.doc`` to escape.

    ``JSONDecodeError`` retains its complete source document. Provider
    responses may echo transcript or prompt content, so callers must receive a
    sanitized transport error instead of the decoder exception.
    """

    raw = str(response_body or "")
    if not raw:
        return empty_value
    invalid_json = False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
        invalid_json = True
    if invalid_json:
        error = provider_transport_error(
            provider,
            operation,
            code="invalid_json",
        )
        raw = ""
        raise error
    return payload


def provider_public_code(value: Any) -> str:
    """Return a public diagnostic code only when it is explicitly allowlisted."""

    candidate = str(value or "").strip()
    return candidate if _is_safe_code(candidate) else ""


def _provider_code_from_body(response_body: str | bytes | None) -> str:
    if response_body is None:
        return ""
    raw = response_body.decode("utf-8", errors="replace") if isinstance(response_body, bytes) else response_body
    if len(raw) > 1_000_000:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""

    candidates: list[Any] = [
        payload.get("code"),
        payload.get("error_code"),
        payload.get("errorCode"),
        payload.get("type"),
        payload.get("status"),
    ]
    nested = payload.get("error")
    if isinstance(nested, dict):
        candidates.extend(
            (
                nested.get("code"),
                nested.get("error_code"),
                nested.get("errorCode"),
                nested.get("type"),
                nested.get("status"),
            )
        )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if _is_safe_code(value):
            return value
    lowered = raw.casefold()
    derived_signals = (
        (
            "model_not_available",
            (
                "model not found",
                "model_not_found",
                "model does not exist",
                "unknown model",
                "model not available",
            ),
        ),
        (
            "quota_exceeded",
            (
                "quota exceeded",
                "quota_exceeded",
                "insufficient quota",
                "insufficient credits",
                "credit balance",
                "payment required",
            ),
        ),
        (
            "rate_limit_error",
            ("rate limit", "rate_limit", "too many requests"),
        ),
        (
            "unsupported_audio",
            (
                "unsupported audio",
                "unsupported format",
                "unsupported codec",
                "corrupt audio",
                "could not decode",
                "cannot decode",
            ),
        ),
        (
            "audio_limit_exceeded",
            (
                "audio duration",
                "duration too long",
                "file too large",
                "file size",
            ),
        ),
        (
            "authentication_error",
            (
                "invalid api key",
                "authentication failed",
                "unauthorized",
            ),
        ),
        (
            "ServiceUnavailable",
            (
                "service unavailable",
                "no healthy upstream",
                "bad gateway",
                "gateway timeout",
            ),
        ),
        (
            "invalid_request_error",
            ("invalid request", "bad request", "validation error"),
        ),
    )
    for safe_code, signals in derived_signals:
        if any(signal in lowered for signal in signals):
            return safe_code
    return ""


def _bounded_identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())[:80]
    return normalized or fallback


def _make_error(
    provider: str,
    label: str,
    category: ErrorCategory,
    message: str,
    *,
    code: str = "",
    retryable: bool | None = None,
) -> ProviderUserError:
    clean_code = code if _is_safe_code(code) else ""
    return ProviderUserError(
        provider=provider,
        provider_label=label,
        title=_title_for(label, category),
        message=message,
        category=category,
        code=clean_code,
        retryable=_is_retryable_for_user(category) if retryable is None else bool(retryable),
    )


def _title_for(label: str, category: ErrorCategory) -> str:
    if category is ErrorCategory.AUTH_INVALID:
        return f"{label} API key issue"
    if category is ErrorCategory.AUTH_EXPIRED:
        return f"{label} billing issue"
    if category is ErrorCategory.PROVIDER_LIMIT:
        return f"{label} limit reached"
    if category is ErrorCategory.CONFIG_INVALID:
        return f"{label} configuration error"
    if category is ErrorCategory.AUDIO_INVALID:
        return f"{label} audio error"
    return f"{label} error"


def _generic_provider_message(label: str, category: ErrorCategory) -> str:
    message = user_message_for_category(category)
    if (
        label
        and label != "STT provider"
        and category
        in {
            ErrorCategory.AUTH_INVALID,
            ErrorCategory.AUTH_EXPIRED,
            ErrorCategory.PROVIDER_LIMIT,
            ErrorCategory.TRANSIENT_PROVIDER,
        }
    ):
        return message.replace("STT service", label).replace("Provider", label)
    return message


def _is_retryable_for_user(category: ErrorCategory) -> bool:
    return is_retryable(category) or category is ErrorCategory.PROVIDER_LIMIT


def _error_text(error: Exception | str) -> str:
    return str(error or "").strip()


def _normalize_provider(provider: str | None, raw: str) -> str:
    normalized = str(provider or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "soniox": "soniox",
        "soniox_async": "soniox_async",
        "mistral": "mistral",
        "mistral_async": "mistral_async",
        "smallest": "smallest",
        "smallest_async": "smallest_async",
        "smallest_ai": "smallest",
        "assembly_ai": "assemblyai",
        "assemblyai": "assemblyai",
        "assemblyai_realtime": "assemblyai_realtime",
        "assembly_ai_realtime": "assemblyai_realtime",
        "azure_mai": "azure_mai",
        "azure_mai_transcribe": "azure_mai",
        "deepgram": "deepgram",
        "openai": "openai",
        "modulate": "modulate",
        "modulate_async": "modulate_async",
        "onnx_local": "onnx_local",
    }
    if normalized in aliases:
        return aliases[normalized]

    text = raw.lower()
    if "soniox" in text:
        return "soniox_async" if "async" in text else "soniox"
    if "azure mai" in text or "mai transcribe" in text:
        return "azure_mai"
    if "assemblyai" in text or "assembly ai" in text:
        return "assemblyai_realtime" if "realtime" in text else "assemblyai"
    if "mistral" in text or "voxtral" in text:
        return "mistral_async" if "async" in text else "mistral"
    if "smallest" in text:
        return "smallest_async" if "async" in text else "smallest"
    if "deepgram" in text:
        return "deepgram"
    if "openai" in text:
        return "openai"
    if "modulate" in text or "velma-2-stt" in text:
        return "modulate_async" if "batch" in text or "async" in text else "modulate"
    if "onnx_local" in text or "onnx local" in text:
        return "onnx_local"
    return normalized


def _provider_family(provider: str) -> str:
    if provider in {"soniox", "soniox_async"}:
        return "soniox"
    if provider == "azure_mai":
        return "azure"
    if provider in {"mistral", "mistral_async"}:
        return "mistral"
    if provider in {"smallest", "smallest_async"}:
        return "smallest"
    if provider in {"modulate", "modulate_async"}:
        return "modulate"
    return provider


def _provider_label(provider: str) -> str:
    if provider:
        label = Config.SERVICE_LABELS.get(provider)
        if label:
            return label
    family = _provider_family(provider)
    if family and family in Config.SERVICE_LABELS:
        return Config.SERVICE_LABELS[family]
    return "STT provider"


def _parse_payload(raw: str) -> dict[str, Any]:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    snippet = raw[start : end + 1]
    try:
        payload = json.loads(snippet)
    except Exception:
        try:
            payload = ast.literal_eval(snippet)
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {}


def _combined_text(raw: str, payload: dict[str, Any]) -> str:
    values = [raw.lower()]
    values.extend(str(value).lower() for value in _walk_values(payload))
    return " ".join(values)


def _walk_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        out: list[Any] = []
        for nested in value.values():
            out.extend(_walk_values(nested))
        return out
    if isinstance(value, list):
        out = []
        for nested in value:
            out.extend(_walk_values(nested))
        return out
    if value is None:
        return []
    return [value]


def _find_payload_value(payload: dict[str, Any], *keys: str) -> Any:
    wanted = {key.lower() for key in keys}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in wanted:
                    return value
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found is not None:
                    return found
        return None

    return walk(payload)


def _status_code(raw: str, payload: dict[str, Any]) -> int | None:
    for key in ("status_code", "statusCode", "status", "error_code"):
        value = _find_payload_value(payload, key)
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status

    match = _HTTP_STATUS_RE.search(raw)
    if match:
        return int(match.group(1))
    return None


def _public_error_code(raw: str, payload: dict[str, Any], *, status: int | None) -> str:
    for key in ("error_type", "err_code", "code", "type"):
        value = _find_payload_value(payload, key)
        text = str(value or "").strip()
        if _is_safe_code(text):
            return text

    lowered = raw.lower()
    for code in _KNOWN_CODES:
        if code.lower() in lowered:
            return code
    return str(status) if status is not None else ""


def _is_safe_code(code: str | None) -> bool:
    value = str(code or "").strip()
    if not _SAFE_CODE_RE.fullmatch(value):
        return False
    if value.casefold() in _SAFE_PUBLIC_CODES:
        return True
    return bool(re.fullmatch(r"[1-5]\d{2}", value))


def _transport_error_category(
    error: ProviderTransportError,
    *,
    fallback: ErrorCategory,
) -> ErrorCategory:
    code = error.code.casefold()
    # Protocol statuses are authoritative. Response text can contain echoed
    # user content, so phrases in a provider body must never override the
    # category established by the HTTP response.
    if error.status in {402, 429}:
        return ErrorCategory.PROVIDER_LIMIT
    if error.status in {401, 403}:
        return ErrorCategory.AUTH_INVALID
    if error.status == 408:
        return ErrorCategory.TRANSIENT_NETWORK
    if error.status is not None and 500 <= error.status <= 599:
        return ErrorCategory.TRANSIENT_PROVIDER
    if error.status in {413, 415, 422}:
        return ErrorCategory.AUDIO_INVALID
    if error.status is not None and 400 <= error.status <= 499:
        return ErrorCategory.CONFIG_INVALID
    if code in {
        "unsupported_audio",
        "audio_limit_exceeded",
        "audio_error",
        "data-0000",
        "asr_unprocessable_entity",
    }:
        return ErrorCategory.AUDIO_INVALID
    if code in {"quota_exceeded", "rate_limit_error", "too_many_requests"}:
        return ErrorCategory.PROVIDER_LIMIT
    if code in {"model_not_available", "model_not_found", "invalid_request_error"}:
        return ErrorCategory.CONFIG_INVALID
    if code in {"authentication_error", "invalid_auth", "insufficient_permissions"}:
        return ErrorCategory.AUTH_INVALID
    return fallback


def _has(text: str, *tokens: str) -> bool:
    return any(token in text for token in tokens)


def _is_5xx(status: int | None) -> bool:
    return status is not None and 500 <= status <= 599


def _is_audio_error(text: str, status: int | None, code: str) -> bool:
    return (
        status == 422
        or code.lower()
        in {
            "data-0000",
            "asr_unprocessable_entity",
            "unsupported_audio",
            "audio_limit_exceeded",
            "audio_error",
        }
        or _has(
            text,
            "unsupported audio",
            "unsupported format",
            "unsupported codec",
            "corrupt or unsupported",
            "could not decode",
            "cannot decode",
            "unable to read the entire client request",
            "audio data was incomplete",
            "audio data is incomplete",
        )
    )


def _classify_soniox(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if _has(text, "model_not_available", "invalid model", "model not available"):
        model_hint = "stt-async-v5" if provider == "soniox_async" else "stt-rt-v5"
        return _make_error(
            provider,
            label,
            ErrorCategory.CONFIG_INVALID,
            f"Soniox rejected the configured model. Use {model_hint} or clear the Soniox model override in Settings.",
            code=code or "model_not_available",
        )
    if status in {401, 403} or _has(text, "unauthorized", "invalid api key", "authentication"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "Soniox rejected the API key. Check the Soniox key in Settings.",
            code=code or str(status or ""),
        )
    if status == 429 or _has(text, "quota", "rate limit", "too many requests"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "Soniox rate limit or quota reached. Wait briefly or check the Soniox plan.",
            code=code or "429",
        )
    if _has(text, "cannot continue request") or status == 503:
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "Soniox realtime ended the stream early. Start recording again; if it repeats, check Soniox status.",
            code=code or "503",
        )
    if _is_audio_error(text, status, code):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            "Soniox could not process the audio. Retry with a clearer or longer recording.",
            code=code,
        )
    if _has(
        text,
        "websocket",
        "connection",
        "cannot connect",
        "could not connect",
        "clientconnectorerror",
        "timeout",
        "timed out",
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_NETWORK,
            "Could not keep the Soniox connection open. Check your network and retry.",
            code=code,
        )
    return None


def _classify_azure(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status in {401, 403} or _has(text, "unauthorized", "forbidden", "invalid subscription", "authentication"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            f"{label} rejected the key or region. Check the key and region in Settings.",
            code=code or str(status or ""),
        )
    if status == 429 or _has(text, "too many requests", "rate limit", "quota"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            f"{label} rate limit or quota reached. Wait briefly or check your Azure limits.",
            code=code or "429",
        )
    if _is_5xx(status) or _has(
        text, "serviceunavailable", "internalservererror", "pipelineerror", "no healthy upstream"
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            f"{label} is temporarily unavailable. Retry shortly; if it repeats, check the Azure region and service status.",
            code=code or str(status or ""),
        )
    if status in {400, 404, 409} or _has(text, "invalidrequest", "invalidargument", "notfound", "conflict"):
        return _make_error(
            provider,
            label,
            ErrorCategory.CONFIG_INVALID,
            f"{label} rejected the request. Check the Azure MAI region, model, and Settings.",
            code=code or str(status or ""),
        )
    if _is_audio_error(text, status, code):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            f"{label} could not process this audio. Retry with a clearer or longer recording.",
            code=code,
        )
    return None


def _classify_assemblyai(
    provider: str, label: str, text: str, status: int | None, code: str
) -> ProviderUserError | None:
    if status in {401, 403} or _has(text, "unauthorized", "forbidden", "invalid api key"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "AssemblyAI rejected the API key. Check it in Settings.",
            code=code or str(status or ""),
        )
    if status == 402 or _has(text, "payment required", "credit", "insufficient funds"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "AssemblyAI credits are exhausted. Check billing or switch provider.",
            code=code or "402",
            retryable=False,
        )
    if status == 429 or _has(text, "too many requests", "rate limit"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "AssemblyAI rate limit reached. Wait briefly or lower request volume.",
            code=code or "429",
        )
    if _is_audio_error(text, status, code):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            "AssemblyAI could not process the recorded audio. Try a clearer or longer recording.",
            code=code or str(status or ""),
        )
    if _is_5xx(status):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "AssemblyAI is temporarily unavailable. Please retry shortly.",
            code=code or str(status or ""),
        )
    return None


def _classify_mistral(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status in {401, 403} or _has(text, "authentication_error", "unauthorized", "forbidden", "invalid api key"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "Mistral rejected the API key. Check the Mistral key in Settings.",
            code=code or str(status or ""),
        )
    if status == 429 or _has(text, "rate_limit_error", "too many requests", "rate limit"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "Mistral rate limit reached. Wait briefly or lower request volume.",
            code=code or "429",
        )
    if _is_5xx(status) or _has(text, "server_error", "bad gateway", "service unavailable", "gateway timeout"):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "Mistral is temporarily unavailable. Please retry shortly.",
            code=code or str(status or ""),
        )
    if _has(
        text,
        "model_not_available",
        "model_not_found",
        "model does not exist",
        "model not found",
        "unknown model",
    ) or ("model" in text and "not found" in text):
        return _make_error(
            provider,
            label,
            ErrorCategory.CONFIG_INVALID,
            "Mistral does not accept the configured model. Check the Mistral model setting.",
            code=code or str(status or ""),
        )
    if _is_audio_error(text, status, code) or _has(
        text, "unsupported file", "unsupported format", "duration", "file size"
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            "Mistral could not process this audio. Use a supported format and keep recordings under the model limits.",
            code=code or str(status or ""),
        )
    if status in {400, 422} or _has(text, "invalid_request_error", "validation error", "bad request"):
        return _make_error(
            provider,
            label,
            ErrorCategory.CONFIG_INVALID,
            "Mistral rejected the transcription request. Check the model, language, and settings.",
            code=code or str(status or ""),
        )
    return None


def _classify_smallest(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status == 401 or _has(text, "unauthorized", "missing or invalid authorization", "invalid api key"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "Smallest AI rejected the API key. Check the Smallest AI key in Settings.",
            code=code or "401",
        )
    if status == 403 or _has(text, "forbidden", "lacks permission"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "Smallest AI key is valid but not allowed for this workspace, product, or trial limit. Check the key in Settings.",
            code=code or "403",
        )
    if status == 429 or _has(text, "too many requests", "rate limit"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "Smallest AI rate limit reached. Wait briefly before retrying.",
            code=code or "429",
        )
    if _has(text, "nosuchkey"):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "Smallest AI could not find the uploaded audio object. Retry the transcription.",
            code=code or "NoSuchKey",
        )
    if _is_audio_error(text, status, code):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            "Smallest AI could not process this audio. Use a supported format and retry.",
            code=code or str(status or ""),
        )
    if _is_5xx(status):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "Smallest AI is temporarily unavailable. Please retry shortly.",
            code=code or str(status or ""),
        )
    return None


def _classify_deepgram(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    code_l = code.lower()
    if status in {401, 403} or _has(text, "invalid_auth", "insufficient_permissions", "invalid credentials"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "Deepgram rejected the API key or model access. Check the Deepgram key and project permissions in Settings.",
            code=code or str(status or ""),
        )
    if status == 402 or _has(text, "asr_payment_required", "not have enough credits", "insufficient credits"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "Deepgram credits are exhausted. Check billing or switch provider.",
            code=code or "402",
            retryable=False,
        )
    if status == 429 or _has(text, "too_many_requests", "too many requests", "rate limit"):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "Deepgram rate limit reached. Wait briefly before retrying.",
            code=code or "429",
        )
    if code_l == "data-0000" or _has(text, "payload cannot be decoded as audio"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            "Deepgram could not decode the streamed audio. Check the audio format and retry.",
            code=code or "DATA-0000",
        )
    if code_l == "net-0001":
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_NETWORK,
            "Deepgram stopped because no audio frames arrived for too long. Check mic input and retry.",
            code=code,
        )
    if code_l == "net-0000":
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "Deepgram did not return transcript data in time. Please retry shortly.",
            code=code,
        )
    if _is_audio_error(text, status, code):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_NETWORK,
            "Deepgram could not read the full audio request. Retry; if it repeats, check the connection and audio input.",
            code=code or str(status or ""),
        )
    if _is_5xx(status):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "Deepgram is temporarily unavailable. Please retry shortly.",
            code=code or str(status or ""),
        )
    return None


def _classify_openai(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status in {401, 403} or _has(
        text, "authentication_error", "invalid_api_key", "incorrect api key", "permissiondeniederror"
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "OpenAI rejected the API key or project access. Check the OpenAI key in Settings.",
            code=code or str(status or ""),
        )
    if status == 429 or _has(
        text, "ratelimiterror", "rate_limit_error", "too many requests", "rate limit", "insufficient_quota", "quota"
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "OpenAI rate limit or quota reached. Wait briefly or check billing and limits.",
            code=code or "429",
        )
    if _is_5xx(status) or _has(text, "internalservererror", "server_error", "service unavailable"):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "OpenAI is temporarily unavailable. Please retry shortly.",
            code=code or str(status or ""),
        )
    if _has(text, "apiconnectionerror", "apitimerouterror", "timeout", "connection"):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_NETWORK,
            "Could not connect to OpenAI. Check your network and retry.",
            code=code,
        )
    if _is_audio_error(text, status, code) or _has(text, "unsupported file", "audio", "file format"):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            "OpenAI could not process this audio. Use a supported audio format and retry.",
            code=code or str(status or ""),
        )
    if status in {400, 422} or _has(text, "badrequesterror", "invalid_request_error"):
        return _make_error(
            provider,
            label,
            ErrorCategory.CONFIG_INVALID,
            "OpenAI rejected the transcription request. Check the model and settings.",
            code=code or str(status or ""),
        )
    return None


def _classify_modulate(
    provider: str,
    label: str,
    text: str,
    status: int | None,
    code: str,
) -> ProviderUserError | None:
    if status in {401, 403} or _has(
        text,
        "4001",
        "4003",
        "auth failure",
        "invalid api key",
        "missing model access",
        "not permitted",
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUTH_INVALID,
            "Modulate rejected the API key or model access. Check the Modulate key in Settings.",
            code=code or str(status or "4001"),
        )
    if status in {402, 429} or _has(
        text,
        "4029",
        "insufficient credits",
        "concurrent-connection limit",
        "concurrent connection limit",
        "rate limit",
        "too many requests",
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.PROVIDER_LIMIT,
            "Modulate credits or connection limits were reached. Wait briefly or check the Modulate plan.",
            code=code or str(status or "4029"),
        )
    if (
        _is_audio_error(text, status, code)
        or status == 422
        or _has(
            text,
            "1003",
            "unsupported audio_format",
            "unsupported format",
            "invalid sample_rate",
            "invalid num_channels",
            "100mb batch upload limit",
        )
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.AUDIO_INVALID,
            "Modulate could not process this audio. Use a supported format and keep batch files at or below 100 MB.",
            code=code or str(status or ""),
        )
    if _is_5xx(status) or _has(
        text,
        "internal server error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_PROVIDER,
            "Modulate is temporarily unavailable. Please retry shortly.",
            code=code or str(status or ""),
        )
    if _has(
        text,
        "websocket",
        "connection",
        "cannot connect",
        "could not connect",
        "clientconnectorerror",
        "timeout",
        "timed out",
    ):
        return _make_error(
            provider,
            label,
            ErrorCategory.TRANSIENT_NETWORK,
            "Could not keep the Modulate connection open. Check your network and retry.",
            code=code,
        )
    return None
