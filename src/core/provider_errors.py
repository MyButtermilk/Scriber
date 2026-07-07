from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any

from src.config import Config
from src.core.error_taxonomy import ErrorCategory, classify_error_message, is_retryable, user_message_for_category
from src.runtime.provider_dependencies import ProviderRuntimeDependencyError


@dataclass(frozen=True)
class ProviderUserError:
    provider: str
    provider_label: str
    title: str
    message: str
    category: ErrorCategory
    code: str = ""
    retryable: bool = False


_HTTP_STATUS_RE = re.compile(r"(?<!\d)([1-5]\d{2})(?!\d)")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{2,80}$")
_KNOWN_CODES = (
    "model_not_available",
    "invalid_request_error",
    "authentication_error",
    "rate_limit_error",
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
    "DATA-0000",
    "NET-0000",
    "NET-0001",
)


def provider_user_error(provider: str | None, error: Exception | str) -> ProviderUserError:
    dependency_error = error if isinstance(error, ProviderRuntimeDependencyError) else None
    raw = _error_text(error)
    payload = _parse_payload(raw)
    normalized_provider = _normalize_provider(provider or getattr(dependency_error, "provider", None), raw)
    label = _provider_label(normalized_provider)
    combined = _combined_text(raw, payload)
    status = _status_code(raw, payload)
    code = _public_error_code(raw, payload, status=status)

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
    else:
        specific = None

    if specific:
        return specific

    category = classify_error_message(raw)
    return _make_error(
        normalized_provider,
        label,
        category,
        _generic_provider_message(label, category),
        code=code,
    )


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
    if label and label != "STT provider" and category in {
        ErrorCategory.AUTH_INVALID,
        ErrorCategory.AUTH_EXPIRED,
        ErrorCategory.PROVIDER_LIMIT,
        ErrorCategory.TRANSIENT_PROVIDER,
    }:
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
        "onnx_local": "onnx_local",
        "nemo_local": "nemo_local",
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
    if "onnx_local" in text or "onnx local" in text:
        return "onnx_local"
    if "nemo_local" in text or "nemo local" in text:
        return "nemo_local"
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
    return bool(code and _SAFE_CODE_RE.match(str(code)))


def _has(text: str, *tokens: str) -> bool:
    return any(token in text for token in tokens)


def _is_5xx(status: int | None) -> bool:
    return status is not None and 500 <= status <= 599


def _is_audio_error(text: str, status: int | None, code: str) -> bool:
    return (
        status == 422
        or code.lower() in {"data-0000", "asr_unprocessable_entity"}
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
    if _has(text, "websocket", "connection", "timeout", "timed out"):
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
    if _is_5xx(status) or _has(text, "serviceunavailable", "internalservererror", "pipelineerror", "no healthy upstream"):
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


def _classify_assemblyai(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status in {401, 403} or _has(text, "unauthorized", "forbidden", "invalid api key"):
        return _make_error(provider, label, ErrorCategory.AUTH_INVALID, "AssemblyAI rejected the API key. Check it in Settings.", code=code or str(status or ""))
    if status == 402 or _has(text, "payment required", "credit", "insufficient funds"):
        return _make_error(provider, label, ErrorCategory.PROVIDER_LIMIT, "AssemblyAI credits are exhausted. Check billing or switch provider.", code=code or "402", retryable=False)
    if status == 429 or _has(text, "too many requests", "rate limit"):
        return _make_error(provider, label, ErrorCategory.PROVIDER_LIMIT, "AssemblyAI rate limit reached. Wait briefly or lower request volume.", code=code or "429")
    if _is_audio_error(text, status, code):
        return _make_error(provider, label, ErrorCategory.AUDIO_INVALID, "AssemblyAI could not process the recorded audio. Try a clearer or longer recording.", code=code or str(status or ""))
    if _is_5xx(status):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_PROVIDER, "AssemblyAI is temporarily unavailable. Please retry shortly.", code=code or str(status or ""))
    return None


def _classify_mistral(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status in {401, 403} or _has(text, "authentication_error", "unauthorized", "forbidden", "invalid api key"):
        return _make_error(provider, label, ErrorCategory.AUTH_INVALID, "Mistral rejected the API key. Check the Mistral key in Settings.", code=code or str(status or ""))
    if status == 429 or _has(text, "rate_limit_error", "too many requests", "rate limit"):
        return _make_error(provider, label, ErrorCategory.PROVIDER_LIMIT, "Mistral rate limit reached. Wait briefly or lower request volume.", code=code or "429")
    if _is_5xx(status) or _has(text, "server_error", "bad gateway", "service unavailable", "gateway timeout"):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_PROVIDER, "Mistral is temporarily unavailable. Please retry shortly.", code=code or str(status or ""))
    if _has(text, "model_not_found", "model does not exist", "model not found", "unknown model") or (
        "model" in text and "not found" in text
    ):
        return _make_error(provider, label, ErrorCategory.CONFIG_INVALID, "Mistral does not accept the configured model. Check the Mistral model setting.", code=code or str(status or ""))
    if _is_audio_error(text, status, code) or _has(text, "unsupported file", "unsupported format", "duration", "file size"):
        return _make_error(provider, label, ErrorCategory.AUDIO_INVALID, "Mistral could not process this audio. Use a supported format and keep recordings under the model limits.", code=code or str(status or ""))
    if status in {400, 422} or _has(text, "invalid_request_error", "validation error", "bad request"):
        return _make_error(provider, label, ErrorCategory.CONFIG_INVALID, "Mistral rejected the transcription request. Check the model, language, and settings.", code=code or str(status or ""))
    return None


def _classify_smallest(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status == 401 or _has(text, "unauthorized", "missing or invalid authorization", "invalid api key"):
        return _make_error(provider, label, ErrorCategory.AUTH_INVALID, "Smallest AI rejected the API key. Check the Smallest AI key in Settings.", code=code or "401")
    if status == 403 or _has(text, "forbidden", "lacks permission"):
        return _make_error(provider, label, ErrorCategory.AUTH_INVALID, "Smallest AI key is valid but not allowed for this workspace, product, or trial limit. Check the key in Settings.", code=code or "403")
    if status == 429 or _has(text, "too many requests", "rate limit"):
        return _make_error(provider, label, ErrorCategory.PROVIDER_LIMIT, "Smallest AI rate limit reached. Wait briefly before retrying.", code=code or "429")
    if _has(text, "nosuchkey"):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_PROVIDER, "Smallest AI could not find the uploaded audio object. Retry the transcription.", code=code or "NoSuchKey")
    if _is_audio_error(text, status, code):
        return _make_error(provider, label, ErrorCategory.AUDIO_INVALID, "Smallest AI could not process this audio. Use a supported format and retry.", code=code or str(status or ""))
    if _is_5xx(status):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_PROVIDER, "Smallest AI is temporarily unavailable. Please retry shortly.", code=code or str(status or ""))
    return None


def _classify_deepgram(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    code_l = code.lower()
    if status in {401, 403} or _has(text, "invalid_auth", "insufficient_permissions", "invalid credentials"):
        return _make_error(provider, label, ErrorCategory.AUTH_INVALID, "Deepgram rejected the API key or model access. Check the Deepgram key and project permissions in Settings.", code=code or str(status or ""))
    if status == 402 or _has(text, "asr_payment_required", "not have enough credits", "insufficient credits"):
        return _make_error(provider, label, ErrorCategory.PROVIDER_LIMIT, "Deepgram credits are exhausted. Check billing or switch provider.", code=code or "402", retryable=False)
    if status == 429 or _has(text, "too_many_requests", "too many requests", "rate limit"):
        return _make_error(provider, label, ErrorCategory.PROVIDER_LIMIT, "Deepgram rate limit reached. Wait briefly before retrying.", code=code or "429")
    if code_l == "data-0000" or _has(text, "payload cannot be decoded as audio"):
        return _make_error(provider, label, ErrorCategory.AUDIO_INVALID, "Deepgram could not decode the streamed audio. Check the audio format and retry.", code=code or "DATA-0000")
    if code_l == "net-0001":
        return _make_error(provider, label, ErrorCategory.TRANSIENT_NETWORK, "Deepgram stopped because no audio frames arrived for too long. Check mic input and retry.", code=code)
    if code_l == "net-0000":
        return _make_error(provider, label, ErrorCategory.TRANSIENT_PROVIDER, "Deepgram did not return transcript data in time. Please retry shortly.", code=code)
    if _is_audio_error(text, status, code):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_NETWORK, "Deepgram could not read the full audio request. Retry; if it repeats, check the connection and audio input.", code=code or str(status or ""))
    if _is_5xx(status):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_PROVIDER, "Deepgram is temporarily unavailable. Please retry shortly.", code=code or str(status or ""))
    return None


def _classify_openai(provider: str, label: str, text: str, status: int | None, code: str) -> ProviderUserError | None:
    if status in {401, 403} or _has(text, "authentication_error", "invalid_api_key", "incorrect api key", "permissiondeniederror"):
        return _make_error(provider, label, ErrorCategory.AUTH_INVALID, "OpenAI rejected the API key or project access. Check the OpenAI key in Settings.", code=code or str(status or ""))
    if status == 429 or _has(text, "ratelimiterror", "rate_limit_error", "too many requests", "rate limit", "insufficient_quota", "quota"):
        return _make_error(provider, label, ErrorCategory.PROVIDER_LIMIT, "OpenAI rate limit or quota reached. Wait briefly or check billing and limits.", code=code or "429")
    if _is_5xx(status) or _has(text, "internalservererror", "server_error", "service unavailable"):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_PROVIDER, "OpenAI is temporarily unavailable. Please retry shortly.", code=code or str(status or ""))
    if _has(text, "apiconnectionerror", "apitimerouterror", "timeout", "connection"):
        return _make_error(provider, label, ErrorCategory.TRANSIENT_NETWORK, "Could not connect to OpenAI. Check your network and retry.", code=code)
    if _is_audio_error(text, status, code) or _has(text, "unsupported file", "audio", "file format"):
        return _make_error(provider, label, ErrorCategory.AUDIO_INVALID, "OpenAI could not process this audio. Use a supported audio format and retry.", code=code or str(status or ""))
    if status in {400, 422} or _has(text, "badrequesterror", "invalid_request_error"):
        return _make_error(provider, label, ErrorCategory.CONFIG_INVALID, "OpenAI rejected the transcription request. Check the model and settings.", code=code or str(status or ""))
    return None
