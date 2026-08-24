import asyncio
import inspect
import json
import sys
import traceback
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import ClientPayloadError

from src import summarization
from src.celeris import CelerisOutputLimitError
from src.summary_html import normalize_summary_html


def _words(count: int) -> str:
    return " ".join(f"w{i}" for i in range(count))


def _structured_summary(body: str, title: str = "Overview") -> str:
    return f"<section><h2>{title}</h2><p>{body}</p></section>"


def test_summary_provider_module_never_logs_diagnostic_tracebacks_with_request_locals():
    assert "logger.exception(" not in inspect.getsource(summarization)


@pytest.mark.asyncio
async def test_meeting_analysis_uses_a_longer_nested_provider_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCRIBER_SUMMARY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("SCRIBER_MEETING_ANALYSIS_TIMEOUT_SEC", raising=False)
    observed: list[tuple[float, int | None]] = []

    async def fake_generate(prompt, model=None, *, max_output_tokens=2048):
        observed.append((summarization._summary_timeout_seconds(), max_output_tokens))
        return "ok"

    monkeypatch.setattr(summarization, "generate_text_with_model", fake_generate)

    assert summarization._summary_timeout_seconds() == 240.0
    assert summarization._meeting_analysis_timeout_seconds() == 900.0
    assert await summarization.generate_meeting_analysis_text("prompt") == "ok"
    assert observed == [(900.0, None)]
    assert summarization._summary_timeout_seconds() == 240.0


def test_summary_budget_scales_with_input_size():
    short_input = _words(700)
    long_input = _words(12_000)

    short_words, short_target, short_tokens = summarization._summary_budget_for_text(short_input, "gpt-5-mini")
    long_words, long_target, long_tokens = summarization._summary_budget_for_text(long_input, "gpt-5-mini")

    assert short_words == 700
    assert long_words == 12_000
    assert long_target > short_target
    assert long_tokens > short_tokens
    assert short_target >= 320
    assert short_tokens >= 900
    assert long_target <= 2200


def test_summary_budget_short_floor_is_configurable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_SHORT_INPUT_MAX_WORDS", "3000")
    monkeypatch.setenv("SCRIBER_SUMMARY_SHORT_MIN_WORDS", "480")
    monkeypatch.setenv("SCRIBER_SUMMARY_SHORT_MIN_OUTPUT_TOKENS", "1300")

    _, target_words, output_tokens = summarization._summary_budget_for_text(_words(900), "gpt-5-mini")

    assert target_words >= 480
    assert output_tokens >= 1300


def test_summary_budget_tolerates_invalid_and_non_finite_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_MIN_WORDS", "invalid")
    monkeypatch.setenv("SCRIBER_SUMMARY_TOKEN_MULTIPLIER", "nan")
    monkeypatch.setenv("SCRIBER_SUMMARY_MAX_OUTPUT_TOKENS", "invalid")

    _, target_words, output_tokens = summarization._summary_budget_for_text(
        _words(1000),
        "gpt-5-mini",
    )

    assert target_words >= 180
    assert output_tokens >= 1024


def test_summary_budget_long_video_gets_token_bonus(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_MIN_SECONDS", "1800")
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_TOKEN_BONUS", "700")

    _, _, normal_tokens = summarization._summary_budget_for_text(
        _words(3500),
        "gpt-5-mini",
        duration_seconds=1700,
    )
    _, _, boosted_tokens = summarization._summary_budget_for_text(
        _words(3500),
        "gpt-5-mini",
        duration_seconds=1900,
    )

    assert boosted_tokens > normal_tokens


def test_summary_budget_gemini_includes_thinking_reserve(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_THINKING_RESERVE_TOKENS", "2400")

    _, _, gpt_tokens = summarization._summary_budget_for_text(_words(1800), "gpt-5-mini")
    _, _, gemini_tokens = summarization._summary_budget_for_text(_words(1800), "gemini-flash-latest")

    assert gemini_tokens > gpt_tokens


def test_summary_budget_openrouter_reasoning_models_include_reasoning_reserve(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_RESERVE_TOKENS", "2400")

    _, _, minimax_tokens = summarization._summary_budget_for_text(_words(300), "minimax/minimax-m3:nitro")
    _, _, glm_tokens = summarization._summary_budget_for_text(_words(300), "z-ai/glm-5.2:nitro")

    assert minimax_tokens == 6144
    assert glm_tokens == 6144


def test_openrouter_payload_normalizes_models_to_nitro():
    payload = summarization._build_openrouter_payload(
        "prompt",
        ["minimax/minimax-m3", "z-ai/glm-5.2:floor"],
        4096,
    )

    assert payload["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]
    assert payload["max_tokens"] == 4096
    assert payload["messages"] == [{"role": "user", "content": "prompt"}]
    assert payload["reasoning"] == {"exclude": True, "effort": "medium"}


def test_openrouter_summary_payload_can_omit_scriber_output_limit():
    payload = summarization._build_openrouter_payload(
        "prompt",
        ["minimax/minimax-m3", "z-ai/glm-5.2"],
        None,
    )

    assert "max_tokens" not in payload


def test_openrouter_payload_routes_gpt_oss_120b_to_baseten_first():
    payload = summarization._build_openrouter_payload(
        "prompt",
        "openai/gpt-oss-120b",
        2048,
    )

    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["provider"] == {"order": ["baseten", "cerebras"], "allow_fallbacks": True}
    assert ":nitro" not in payload["model"]


def test_openrouter_payload_routes_gpt_oss_120b_to_cerebras_only():
    payload = summarization._build_openrouter_payload(
        "prompt",
        "openai/gpt-oss-120b:cerebras",
        2048,
    )

    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["provider"] == {"order": ["cerebras"], "allow_fallbacks": False}
    assert ":cerebras" not in payload["model"]
    assert ":nitro" not in payload["model"]


def test_openrouter_payload_allows_gpt_oss_provider_order_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_OPENROUTER_GPT_OSS_120B_PROVIDERS", "cerebras,baseten")

    payload = summarization._build_openrouter_payload(
        "prompt",
        "openai/gpt-oss-120b",
        2048,
    )

    assert payload["provider"] == {"order": ["cerebras", "baseten"], "allow_fallbacks": True}


def test_cerebras_direct_model_is_not_openrouter():
    assert not summarization._is_openrouter_model("cerebras/gemma-4-31b")
    assert summarization._is_cerebras_model("cerebras/gemma-4-31b")
    assert summarization._cerebras_model_id("cerebras/gemma-4-31b") == "gemma-4-31b"


def test_cerebras_summary_payload_can_omit_scriber_output_limit():
    payload = summarization._build_cerebras_payload(
        "prompt",
        "cerebras/gemma-4-31b",
        None,
    )

    assert payload == {
        "model": "gemma-4-31b",
        "messages": [{"role": "user", "content": "prompt"}],
    }


def test_meta_muse_models_are_direct_models_with_large_output_caps():
    for model in ("muse-spark-1.2", "muse-spark-1.2-contributor"):
        assert summarization._is_meta_model(model)
        assert not summarization._is_openrouter_model(model)
        assert summarization._MODEL_OUTPUT_TOKEN_CAPS[model] == 131_072


@pytest.mark.asyncio
async def test_meta_muse_chat_completion_uses_official_contract(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[dict[str, object], dict[str, str]]] = []
    monkeypatch.setattr(summarization.Config, "MODEL_API_KEY", "meta-test-key", raising=False)

    async def _fake_post(payload, headers, _session):
        calls.append((payload, headers))
        return {
            "model": "muse-spark-1.2",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Meta summary"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_meta_chat_completion", _fake_post)

    result = await summarization._summarize_meta("Summarize this", "muse-spark-1.2", None)

    assert result == "Meta summary"
    payload, headers = calls[0]
    assert payload == {
        "model": "muse-spark-1.2",
        "messages": [{"role": "user", "content": "Summarize this"}],
    }
    assert headers == {"Authorization": "Bearer meta-test-key", "Content-Type": "application/json"}


@pytest.mark.asyncio
async def test_meta_muse_discards_output_marked_incomplete(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(summarization.Config, "MODEL_API_KEY", "meta-test-key", raising=False)

    async def _fake_post(_payload, _headers, _session):
        return {
            "model": "muse-spark-1.2-contributor",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "partial"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_meta_chat_completion", _fake_post)

    with pytest.raises(summarization.SummaryOutputLimitError, match="output limit"):
        await summarization._summarize_meta("prompt", "muse-spark-1.2-contributor", 2048)


@pytest.mark.asyncio
async def test_meta_muse_routes_both_youtube_and_meeting_summary_workflows(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, int | None]] = []

    async def _fake_meta(_prompt: str, model: str, max_output_tokens: int | None) -> str:
        calls.append((model, max_output_tokens))
        if model.endswith("contributor"):
            return '{"overview":"Meeting result"}'
        return "<section><h2>Video</h2><p>YouTube result</p></section>"

    monkeypatch.setattr(summarization, "_summarize_meta", _fake_meta)
    monkeypatch.setattr(summarization.Config, "SUMMARIZATION_PROMPT", "Summarize")
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "0")

    youtube = await summarization.summarize_text(
        "A sufficiently detailed YouTube transcript about product planning.",
        model="muse-spark-1.2",
    )
    meeting = await summarization.generate_meeting_analysis_text(
        "Return meeting JSON",
        model="muse-spark-1.2-contributor",
        max_output_tokens=2048,
    )

    assert youtube == "<section><h2>Video</h2><p>YouTube result</p></section>"
    assert meeting == '{"overview":"Meeting result"}'
    assert calls == [
        ("muse-spark-1.2", None),
        ("muse-spark-1.2-contributor", None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["muse-spark-1.2", "muse-spark-1.2-contributor"])
@pytest.mark.parametrize(
    ("failure_kind", "expected_error", "expected_message"),
    [
        ("timeout", RuntimeError, "timed out"),
        ("rate_limit", summarization.ProviderTransportError, "meta summarization failed"),
        ("service_unavailable", summarization.ProviderTransportError, "meta summarization failed"),
        ("invalid_output", RuntimeError, "no displayable structured HTML summary"),
        ("native_length", summarization.SummaryOutputLimitError, "output limit"),
    ],
)
async def test_direct_meta_summary_never_sends_transcript_to_openrouter_fallback(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    failure_kind: str,
    expected_error: type[Exception],
    expected_message: str,
):
    openrouter_calls: list[str] = []
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _failing_meta(_prompt: str, _model: str, _max_output_tokens: int | None) -> str:
        if failure_kind == "timeout":
            raise TimeoutError
        if failure_kind == "rate_limit":
            raise summarization.provider_transport_error("meta", "summarization", status=429)
        if failure_kind == "service_unavailable":
            raise summarization.provider_transport_error("meta", "summarization", status=503)
        if failure_kind == "native_length":
            raise summarization.SummaryOutputLimitError("Meta reached its native output limit.")
        return "<script>PRIVATE_META_TRANSCRIPT_SENTINEL</script>"

    async def _unexpected_openrouter(prompt: str, *_args, **_kwargs) -> str:
        openrouter_calls.append(prompt)
        return _structured_summary("must not be used")

    monkeypatch.setattr(summarization, "_summarize_meta", _failing_meta)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _unexpected_openrouter)

    with pytest.raises(expected_error, match=expected_message):
        await summarization.summarize_text(
            "PRIVATE_META_TRANSCRIPT_SENTINEL",
            model=model,
        )

    assert openrouter_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "primary_error", "expected_error"),
    [
        ("muse-spark-1.2", TimeoutError(), RuntimeError),
        (
            "muse-spark-1.2",
            summarization.provider_transport_error("meta", "summarization", status=503),
            summarization.ProviderTransportError,
        ),
        (
            "muse-spark-1.2-contributor",
            summarization.SummaryOutputLimitError("Meta reached its native output limit."),
            summarization.SummaryOutputLimitError,
        ),
        (
            "muse-spark-1.2-contributor",
            summarization.MetaContributorAccessError("Contributor access is unavailable."),
            summarization.MetaContributorAccessError,
        ),
    ],
)
async def test_direct_meta_meeting_analysis_never_sends_prompt_to_openrouter_fallback(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    primary_error: Exception,
    expected_error: type[Exception],
):
    openrouter_calls: list[str] = []
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _failing_meta(_prompt: str, _model: str, _max_output_tokens: int | None) -> str:
        raise primary_error

    async def _unexpected_openrouter(prompt: str, *_args, **_kwargs) -> str:
        openrouter_calls.append(prompt)
        return "must not be used"

    monkeypatch.setattr(summarization, "_summarize_meta", _failing_meta)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _unexpected_openrouter)

    with pytest.raises(expected_error):
        await summarization.generate_meeting_analysis_text(
            "PRIVATE_META_MEETING_PROMPT_SENTINEL",
            model=model,
        )

    assert openrouter_calls == []


def test_openrouter_reasoning_effort_is_only_sent_when_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_EFFORT", raising=False)
    assert summarization._openrouter_reasoning_config() == {"exclude": True, "effort": "medium"}

    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_EFFORT", "high")
    assert summarization._openrouter_reasoning_config() == {"exclude": True, "effort": "high"}

    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_EFFORT", "invalid")
    assert summarization._openrouter_reasoning_config() == {"exclude": True, "effort": "medium"}


@pytest.mark.asyncio
async def test_live_mic_generation_uses_context_local_low_reasoning(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    monkeypatch.delenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _session):
        calls.append(payload)
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)
    routing_diagnostics: dict[str, object] = {}

    result = await summarization.generate_live_mic_text(
        "clean this dictation",
        "minimax/minimax-m3:nitro",
        max_output_tokens=768,
        routing_diagnostics=routing_diagnostics,
    )

    assert result == "cleaned dictation"
    assert calls[0]["model"] == "minimax/minimax-m3:nitro"
    assert "models" not in calls[0]
    assert calls[0]["reasoning"] == {"exclude": True, "effort": "low"}
    assert routing_diagnostics["primaryModel"] == "minimax/minimax-m3:nitro"
    assert routing_diagnostics["usedModel"] == "minimax/minimax-m3"
    assert routing_diagnostics["fallbackUsed"] is False
    assert summarization._openrouter_reasoning_config() == {"exclude": True, "effort": "medium"}


@pytest.mark.asyncio
async def test_live_mic_generation_temporarily_bypasses_rejected_primary_per_credential(
    monkeypatch: pytest.MonkeyPatch,
):
    primary_calls: list[str] = []
    fallback_calls: list[dict[str, object]] = []
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_FALLBACK_MODELS", "minimax/minimax-m3:nitro")
    monkeypatch.setattr(summarization.Config, "CEREBRAS_API_KEY", "cerebras-key-a", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(
        summarization.Config,
        "POST_PROCESSING_FALLBACK_MODEL",
        "z-ai/glm-5.2:nitro",
        raising=False,
    )
    monkeypatch.setattr(summarization, "_live_mic_rejected_primary_keys", set())
    monkeypatch.setattr(
        summarization,
        "_live_mic_primary_circuit",
        summarization.ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=60.0),
    )

    async def _rejected_primary(_prompt, model, _max_output_tokens):
        primary_calls.append(model)
        raise summarization.ProviderTransportError(
            provider="cerebras",
            operation="text_generation",
            status=401,
            code="",
            retryable=False,
        )

    async def _accepted_fallback(payload, _headers, _session):
        fallback_calls.append(payload)
        return {
            "model": "z-ai/glm-5.2",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_summarize_cerebras", _rejected_primary)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _accepted_fallback)
    first_diagnostics: dict[str, object] = {}
    second_diagnostics: dict[str, object] = {}

    first = await summarization.generate_live_mic_text(
        "clean this dictation",
        "cerebras/gemma-4-31b",
        max_output_tokens=768,
        routing_diagnostics=first_diagnostics,
    )
    second = await summarization.generate_live_mic_text(
        "clean this dictation",
        "cerebras/gemma-4-31b",
        max_output_tokens=768,
        routing_diagnostics=second_diagnostics,
    )

    assert first == second == "cleaned dictation"
    assert len(primary_calls) == 1
    assert len(fallback_calls) == 2
    assert all(call["model"] == "z-ai/glm-5.2:nitro" for call in fallback_calls)
    assert all(call["reasoning"] == {"exclude": True, "effort": "low"} for call in fallback_calls)
    assert first_diagnostics["primaryBypassed"] is False
    assert first_diagnostics["primaryFailureCode"] == "provider_error"
    assert first_diagnostics["primaryFailureStatus"] == 401
    assert first_diagnostics["primaryCircuitCooldownMs"] is None
    assert first_diagnostics["primaryModel"] == "cerebras/gemma-4-31b"
    assert first_diagnostics["configuredFallbackModel"] == "z-ai/glm-5.2:nitro"
    assert first_diagnostics["fallbackUsed"] is True
    assert first_diagnostics["fallbackModel"] == "z-ai/glm-5.2"
    assert first_diagnostics["usedModel"] == "z-ai/glm-5.2"
    assert second_diagnostics["primaryBypassed"] is True
    assert second_diagnostics["primaryBypassReason"] == "credential_rejected"
    assert second_diagnostics["primaryCircuitCooldownMs"] is None
    assert second_diagnostics["fallbackUsed"] is True
    assert second_diagnostics["fallbackModel"] == "z-ai/glm-5.2"

    monkeypatch.setattr(summarization.Config, "CEREBRAS_API_KEY", "cerebras-key-b", raising=False)
    changed_key_diagnostics: dict[str, object] = {}
    await summarization.generate_live_mic_text(
        "clean this dictation",
        "cerebras/gemma-4-31b",
        max_output_tokens=768,
        routing_diagnostics=changed_key_diagnostics,
    )

    assert len(primary_calls) == 2
    assert changed_key_diagnostics["primaryBypassed"] is False


@pytest.mark.asyncio
async def test_live_mic_generation_does_not_cache_transient_primary_failure(monkeypatch: pytest.MonkeyPatch):
    primary_calls = 0
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "CEREBRAS_API_KEY", "cerebras-key", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(summarization, "_live_mic_rejected_primary_keys", set())
    monkeypatch.setattr(
        summarization,
        "_live_mic_primary_circuit",
        summarization.ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=60.0),
    )

    async def _transient_primary(_prompt, _model, _max_output_tokens):
        nonlocal primary_calls
        primary_calls += 1
        raise summarization.ProviderTransportError(
            provider="cerebras",
            operation="text_generation",
            status=503,
            code="server_error",
            retryable=True,
        )

    async def _accepted_fallback(_payload, _headers, _session):
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_summarize_cerebras", _transient_primary)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _accepted_fallback)

    for _ in range(2):
        await summarization.generate_live_mic_text(
            "clean this dictation",
            "cerebras/gemma-4-31b",
            max_output_tokens=768,
        )

    assert primary_calls == 2


@pytest.mark.asyncio
async def test_live_mic_generation_latches_celeris_status_only_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    primary_calls = 0
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "CELERIS_API_KEY", "celeris-key", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(summarization, "_live_mic_rejected_primary_keys", set())
    monkeypatch.setattr(
        summarization,
        "_live_mic_primary_circuit",
        summarization.ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=60.0),
    )

    async def _rejected_primary(*_args, **_kwargs):
        nonlocal primary_calls
        primary_calls += 1
        raise summarization.ProviderTransportError(
            provider="celeris",
            operation="text_generation",
            status=403,
            code="permission_denied",
            retryable=False,
        )

    async def _accepted_fallback(_payload, _headers, _session):
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "celeris_chat_completion", _rejected_primary)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _accepted_fallback)

    first_diagnostics: dict[str, object] = {}
    second_diagnostics: dict[str, object] = {}
    for diagnostics in (first_diagnostics, second_diagnostics):
        await summarization.generate_live_mic_text(
            "clean this dictation",
            "celeris-1",
            max_output_tokens=768,
            routing_diagnostics=diagnostics,
        )

    assert primary_calls == 1
    assert first_diagnostics["primaryFailureStatus"] == 403
    assert first_diagnostics["primaryCircuitCooldownMs"] is None
    assert second_diagnostics["primaryBypassed"] is True
    assert second_diagnostics["primaryBypassReason"] == "credential_rejected"


@pytest.mark.asyncio
async def test_live_mic_generation_does_not_cache_request_specific_400(monkeypatch: pytest.MonkeyPatch):
    primary_calls = 0
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "CEREBRAS_API_KEY", "cerebras-key", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(summarization, "_live_mic_rejected_primary_keys", set())
    monkeypatch.setattr(
        summarization,
        "_live_mic_primary_circuit",
        summarization.ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=60.0),
    )

    async def _rejected_primary(_prompt, _model, _max_output_tokens):
        nonlocal primary_calls
        primary_calls += 1
        raise summarization.ProviderTransportError(
            provider="cerebras",
            operation="text_generation",
            status=400,
            code="invalid_request_error",
            retryable=False,
        )

    async def _accepted_fallback(_payload, _headers, _session):
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_summarize_cerebras", _rejected_primary)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _accepted_fallback)

    for _ in range(2):
        await summarization.generate_live_mic_text(
            "clean this dictation",
            "cerebras/gemma-4-31b",
            max_output_tokens=768,
        )

    assert primary_calls == 2


@pytest.mark.asyncio
async def test_live_mic_generation_temporarily_bypasses_quota_limited_primary(
    monkeypatch: pytest.MonkeyPatch,
):
    primary_calls = 0
    clock = [0.0]
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "CEREBRAS_API_KEY", "cerebras-key", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(summarization, "_live_mic_rejected_primary_keys", set())
    monkeypatch.setattr(
        summarization,
        "_live_mic_primary_circuit",
        summarization.ProviderCircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=60.0,
            clock=lambda: clock[0],
        ),
    )

    async def _rejected_primary(_prompt, _model, _max_output_tokens):
        nonlocal primary_calls
        primary_calls += 1
        raise summarization.ProviderTransportError(
            provider="cerebras",
            operation="text_generation",
            status=402,
            code="",
            retryable=False,
        )

    async def _accepted_fallback(_payload, _headers, _session):
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_summarize_cerebras", _rejected_primary)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _accepted_fallback)
    first_diagnostics: dict[str, object] = {}
    second_diagnostics: dict[str, object] = {}

    await summarization.generate_live_mic_text(
        "clean this dictation",
        "cerebras/gemma-4-31b",
        max_output_tokens=768,
        routing_diagnostics=first_diagnostics,
    )
    await summarization.generate_live_mic_text(
        "clean this dictation",
        "cerebras/gemma-4-31b",
        max_output_tokens=768,
        routing_diagnostics=second_diagnostics,
    )

    assert primary_calls == 1
    assert first_diagnostics["primaryFailureStatus"] == 402
    assert second_diagnostics["primaryBypassed"] is True
    assert second_diagnostics["primaryBypassReason"] == "quota_cooldown"

    clock[0] = 61.0
    after_cooldown_diagnostics: dict[str, object] = {}
    await summarization.generate_live_mic_text(
        "clean this dictation",
        "cerebras/gemma-4-31b",
        max_output_tokens=768,
        routing_diagnostics=after_cooldown_diagnostics,
    )

    assert primary_calls == 2
    assert after_cooldown_diagnostics["primaryBypassed"] is False


@pytest.mark.asyncio
async def test_live_mic_generation_disables_transport_payload_replay(monkeypatch: pytest.MonkeyPatch):
    post_calls: list[str] = []
    read_calls = 0
    mode = "live"
    monkeypatch.setenv("SCRIBER_SUMMARY_PAYLOAD_RETRIES", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def post(self, _url, *, json, headers):
            del json, headers
            post_calls.append(mode)
            return _Response()

    async def _read_response(_response, _limit):
        nonlocal read_calls
        read_calls += 1
        if mode == "live" or read_calls == 1:
            raise ClientPayloadError("interrupted")
        return json.dumps(
            {
                "model": "minimax/minimax-m3",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": "cleaned dictation"},
                    }
                ],
            }
        )

    async def _post_with_fake_session(payload, headers, _session):
        return await summarization._post_chat_completion_json(
            provider="openrouter",
            url="https://openrouter.invalid/v1/chat/completions",
            payload=payload,
            headers=headers,
            session=_Session(),
        )

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(summarization, "read_response_text_limited", _read_response)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _post_with_fake_session)
    monkeypatch.setattr(summarization.asyncio, "sleep", _no_sleep)

    with pytest.raises(summarization.ProviderTransportError):
        await summarization.generate_live_mic_text(
            "clean this dictation",
            "minimax/minimax-m3:nitro",
            max_output_tokens=768,
        )
    assert post_calls == ["live"]

    mode = "normal"
    read_calls = 0
    result = await summarization.generate_text_with_model(
        "clean this dictation",
        "minimax/minimax-m3:nitro",
        max_output_tokens=768,
    )

    assert result == "cleaned dictation"
    assert post_calls == ["live", "normal", "normal"]


@pytest.mark.asyncio
async def test_live_mic_context_overrides_are_isolated_from_parallel_summary(monkeypatch: pytest.MonkeyPatch):
    observed: dict[str, dict[str, object]] = {}
    both_entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(
        summarization.Config,
        "POST_PROCESSING_FALLBACK_MODEL",
        "minimax/minimax-m3:nitro",
        raising=False,
    )

    class _Transport:
        @asynccontextmanager
        async def borrow(self):
            yield self

    provider_http_transport = _Transport()

    async def _observe_context(prompt, _model, _max_output_tokens):
        observed[prompt] = {
            "reasoning": summarization._openrouter_reasoning_config(),
            "attemptLimit": summarization._openrouter_semantic_attempt_limit_override.get(),
            "fallbackModels": summarization._openrouter_fallback_models_override.get(),
            "payloadRetries": summarization._summary_payload_retries_override.get(),
            "providerHttpTransport": summarization._live_mic_provider_http_transport_override.get(),
        }
        if len(observed) == 2:
            both_entered.set()
        await release.wait()
        return "cleaned dictation"

    monkeypatch.setattr(summarization, "_summarize_with_model", _observe_context)
    live = asyncio.create_task(
        summarization.generate_live_mic_text(
            "live",
            "minimax/minimax-m3:nitro",
            max_output_tokens=768,
            provider_http_transport=provider_http_transport,  # type: ignore[arg-type]
        )
    )
    normal = asyncio.create_task(
        summarization.generate_text_with_model(
            "normal",
            "minimax/minimax-m3:nitro",
            max_output_tokens=768,
        )
    )
    await asyncio.wait_for(both_entered.wait(), timeout=1.0)
    release.set()
    await asyncio.gather(live, normal)

    assert observed["live"] == {
        "reasoning": {"exclude": True, "effort": "low"},
        "attemptLimit": 1,
        "fallbackModels": ("minimax/minimax-m3:nitro",),
        "payloadRetries": 0,
        "providerHttpTransport": provider_http_transport,
    }
    assert observed["normal"] == {
        "reasoning": {"exclude": True, "effort": "medium"},
        "attemptLimit": None,
        "fallbackModels": None,
        "payloadRetries": None,
        "providerHttpTransport": None,
    }


@pytest.mark.asyncio
async def test_live_mic_context_overrides_reset_after_cancellation(monkeypatch: pytest.MonkeyPatch):
    entered = asyncio.Event()

    async def _hang(_prompt, _model, _max_output_tokens):
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    async def _worker():
        try:
            await summarization.generate_live_mic_text(
                "live",
                "minimax/minimax-m3:nitro",
                max_output_tokens=768,
            )
        except asyncio.CancelledError:
            return {
                "reasoning": summarization._openrouter_reasoning_config(),
                "attemptLimit": summarization._openrouter_semantic_attempt_limit_override.get(),
                "fallbackModels": summarization._openrouter_fallback_models_override.get(),
                "payloadRetries": summarization._summary_payload_retries_override.get(),
                "providerHttpTransport": summarization._live_mic_provider_http_transport_override.get(),
            }

    monkeypatch.setattr(summarization, "_summarize_with_model", _hang)
    task = asyncio.create_task(_worker())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    observed = await task

    assert observed == {
        "reasoning": {"exclude": True, "effort": "medium"},
        "attemptLimit": None,
        "fallbackModels": None,
        "payloadRetries": None,
        "providerHttpTransport": None,
    }


@pytest.mark.asyncio
async def test_live_mic_shared_transport_views_follow_primary_then_fallback(monkeypatch: pytest.MonkeyPatch):
    providers: list[str] = []

    class _Transport:
        @asynccontextmanager
        async def borrow(self):
            yield self

        async def session_view(self, *, provider, marker=None):
            del marker
            providers.append(provider)
            return object()

    transport = _Transport()
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "CEREBRAS_API_KEY", "cerebras-key", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _failing_primary(_prompt, _model, _max_output_tokens):
        active = summarization._live_mic_provider_http_transport_override.get()
        await active.session_view(provider="cerebras")
        raise summarization.ProviderTransportError(
            provider="cerebras",
            operation="text_generation",
            status=401,
        )

    async def _successful_fallback(_prompt, _models, _max_output_tokens):
        active = summarization._live_mic_provider_http_transport_override.get()
        await active.session_view(provider="openrouter")
        return "cleaned dictation"

    monkeypatch.setattr(summarization, "_summarize_cerebras", _failing_primary)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _successful_fallback)

    result = await summarization.generate_live_mic_text(
        "clean this dictation",
        "cerebras/gemma-4-31b",
        max_output_tokens=768,
        provider_http_transport=transport,  # type: ignore[arg-type]
    )

    assert result == "cleaned dictation"
    assert providers == ["cerebras", "openrouter"]
    assert summarization._live_mic_provider_http_transport_override.get() is None


@pytest.mark.asyncio
async def test_live_mic_does_not_report_failed_fallback_as_used(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "CEREBRAS_API_KEY", "cerebras-key", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(
        summarization.Config,
        "POST_PROCESSING_FALLBACK_MODEL",
        "z-ai/glm-5.2:nitro",
        raising=False,
    )
    monkeypatch.setattr(summarization, "_live_mic_rejected_primary_keys", set())
    monkeypatch.setattr(
        summarization,
        "_live_mic_primary_circuit",
        summarization.ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=60.0),
    )

    async def _failing_primary(_prompt, _model, _max_output_tokens):
        raise summarization.ProviderTransportError(
            provider="cerebras",
            operation="text_generation",
            status=503,
            code="server_error",
            retryable=True,
        )

    async def _failing_fallback(_prompt, models, _max_output_tokens):
        assert models == ("z-ai/glm-5.2:nitro",)
        raise RuntimeError("fallback unavailable")

    monkeypatch.setattr(summarization, "_summarize_cerebras", _failing_primary)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _failing_fallback)
    diagnostics: dict[str, object] = {}

    with pytest.raises(RuntimeError):
        await summarization.generate_live_mic_text(
            "clean this dictation",
            "cerebras/gemma-4-31b",
            max_output_tokens=768,
            routing_diagnostics=diagnostics,
        )

    assert diagnostics["primaryModel"] == "cerebras/gemma-4-31b"
    assert diagnostics["configuredFallbackModel"] == "z-ai/glm-5.2:nitro"
    assert diagnostics["fallbackReason"] == "server_error"
    assert diagnostics["fallbackUsed"] is False
    assert diagnostics["usedModel"] is None


@pytest.mark.asyncio
async def test_live_mic_openrouter_borrows_bounded_session_without_closing_it(monkeypatch: pytest.MonkeyPatch):
    borrowed = object()
    providers: list[str] = []
    observed_sessions: list[object] = []

    class _Transport:
        @asynccontextmanager
        async def borrow(self):
            yield self

        async def session_view(self, *, provider, marker=None):
            del marker
            providers.append(provider)
            return borrowed

    async def _fake_post(_payload, _headers, session):
        observed_sessions.append(session)
        assert isinstance(session, summarization._RequestTimeoutSessionView)
        assert session._session is borrowed
        assert session._timeout.total == summarization._summary_timeout_seconds()
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    result = await summarization.generate_live_mic_text(
        "clean this dictation",
        "minimax/minimax-m3:nitro",
        max_output_tokens=768,
        provider_http_transport=_Transport(),  # type: ignore[arg-type]
    )

    assert result == "cleaned dictation"
    assert providers == ["openrouter"]
    assert len(observed_sessions) == 1


@pytest.mark.asyncio
async def test_live_mic_generation_caps_openrouter_attempts(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    monkeypatch.delenv("SCRIBER_SUMMARY_OPENROUTER_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _session):
        calls.append(payload)
        max_tokens = payload["max_tokens"]
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "length",
                    "native_finish_reason": "length",
                    "message": {"role": "assistant", "content": None},
                }
            ],
            "usage": {
                "completion_tokens": max_tokens,
                "reasoning_tokens": max_tokens,
                "total_tokens": max_tokens + 100,
            },
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    with pytest.raises(RuntimeError, match="bounded attempt limit"):
        await summarization.generate_live_mic_text(
            "clean this dictation",
            "minimax/minimax-m3:nitro",
            max_output_tokens=768,
            openrouter_max_semantic_attempts=1,
        )

    assert len(calls) == 1
    assert [call["max_tokens"] for call in calls] == [768]
    assert all(call["reasoning"] == {"exclude": True, "effort": "low"} for call in calls)
    assert summarization._openrouter_reasoning_config() == {"exclude": True, "effort": "medium"}


@pytest.mark.asyncio
async def test_live_mic_openrouter_retries_remain_single_model(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _session):
        calls.append(payload)
        if len(calls) == 1:
            max_tokens = payload["max_tokens"]
            return {
                "model": "google/gemini-2.5-flash-lite",
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "length",
                        "message": {"role": "assistant", "content": None},
                    }
                ],
                "usage": {
                    "completion_tokens": max_tokens,
                    "reasoning_tokens": max_tokens,
                    "total_tokens": max_tokens + 100,
                },
            }
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "cleaned dictation"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    result = await summarization.generate_live_mic_text(
        "clean this dictation",
        "google/gemini-2.5-flash-lite:nitro",
        max_output_tokens=768,
        openrouter_max_semantic_attempts=2,
    )

    assert result == "cleaned dictation"
    assert len(calls) == 2
    assert calls[0]["model"] == "google/gemini-2.5-flash-lite:nitro"
    assert calls[1]["model"] == "minimax/minimax-m3:nitro"
    assert all("models" not in call for call in calls)


def test_openrouter_model_family_matches_dated_response_model():
    assert summarization._same_openrouter_model("z-ai/glm-5.2:nitro", "z-ai/glm-5.2-20260616")
    assert not summarization._same_openrouter_model("minimax/minimax-m3:nitro", "z-ai/glm-5.2-20260616")


def test_openrouter_response_extractor_uses_first_non_empty_choice():
    data = {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": ""}},
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "summary"}]},
            },
        ]
    }

    assert summarization._extract_openrouter_response_text(data) == "summary"


def test_openrouter_empty_response_detail_includes_provider_diagnostics():
    data = {
        "model": "minimax/minimax-m3:nitro",
        "choices": [
            {
                "finish_reason": "stop",
                "native_finish_reason": "stop",
                "message": {"role": "assistant", "content": "", "reasoning": "internal notes"},
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 12,
            "total_tokens": 132,
            "completion_tokens_details": {"reasoning_tokens": 12},
        },
    }

    detail = json.loads(summarization._openrouter_empty_response_detail(data))

    assert detail["model"] == "minimax/minimax-m3:nitro"
    assert detail["choice"]["finish_reason"] == "stop"
    assert detail["choice"]["native_finish_reason"] == "stop"
    assert detail["choice"]["content_chars"] == 0
    assert detail["choice"]["reasoning_chars"] == 14
    assert detail["usage"]["completion_tokens"] == 12
    assert detail["usage"]["reasoning_tokens"] == 12


def test_openrouter_empty_response_detail_excludes_provider_error_message():
    private_marker = "private transcript fragment echoed by provider"
    data = {
        "model": "minimax/minimax-m3:nitro",
        "choices": [
            {
                "finish_reason": "error",
                "error": {
                    "code": "provider_error",
                    "message": private_marker,
                },
                "message": {"content": ""},
            }
        ],
    }

    detail = summarization._openrouter_empty_response_detail(data)

    assert private_marker not in detail
    assert json.loads(detail)["choice"]["error"] == {"code": "provider_error"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("post", "args"),
    [
        (
            summarization._post_openrouter_chat_completion,
            (
                {"messages": []},
                {},
            ),
        ),
        (
            summarization._post_gemini_generate_content,
            ("https://gemini.example/generate", {"contents": []}),
        ),
    ],
)
async def test_summary_http_errors_discard_private_provider_response(
    post,
    args,
):
    private_marker = "private transcript fragment echoed by provider"

    class Response:
        status = 429

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return json.dumps(
                {
                    "error": {
                        "code": "rate_limit_error",
                        "message": private_marker,
                    }
                }
            )

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(RuntimeError) as captured:
        if post is summarization._post_gemini_generate_content:
            await post(Session(), *args, retries=0)
        else:
            await post(*args, Session())

    assert private_marker not in str(captured.value)
    assert private_marker not in repr(captured.value)
    assert "rate_limit_error" in str(captured.value)


@pytest.mark.asyncio
async def test_invalid_summary_json_is_absent_from_formatted_exception_chain():
    private_marker = "PRIVATE_TRANSCRIPT_91a5e7"

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return "{invalid " + private_marker

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(RuntimeError) as captured:
        await summarization._post_openrouter_chat_completion(
            {"messages": []},
            {},
            Session(),
        )

    formatted = "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )
    assert private_marker not in formatted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post",
    [
        summarization._post_openrouter_chat_completion,
        summarization._post_meta_chat_completion,
    ],
)
async def test_summary_chat_completion_retries_incomplete_response_payload(post):
    calls = 0

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ClientPayloadError("response payload is not completed")
            return json.dumps(
                {
                    "model": "muse-spark-1.2",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "complete summary"},
                        }
                    ],
                }
            )

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    result = await post({"messages": []}, {}, Session())

    assert result["choices"][0]["message"]["content"] == "complete summary"
    assert calls == 2


def test_summary_diagnostics_discard_unknown_identifier_shaped_values():
    private_marker = "patient_Alice_2026"
    data = {
        "model": private_marker,
        "choices": [
            {
                "finish_reason": private_marker,
                "native_finish_reason": "550e8400-e29b-41d4-a716-446655440000",
                "error": {"code": private_marker},
                "message": {"content": ""},
            }
        ],
    }

    detail = summarization._openrouter_empty_response_detail(data)

    assert private_marker not in detail
    assert "550e8400-e29b-41d4-a716-446655440000" not in detail


def test_gemini_payload_sets_explicit_thinking_level_without_output_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL", "low")

    payload = summarization._build_gemini_payload("prompt", "gemini-flash-latest", 4096)

    generation_config = payload["generationConfig"]
    assert "maxOutputTokens" not in generation_config
    assert generation_config["thinkingConfig"] == {"thinkingLevel": "LOW"}


def test_gemini_summary_payload_can_omit_scriber_output_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCRIBER_SUMMARY_GEMINI_TEMPERATURE", raising=False)

    payload = summarization._build_gemini_payload("prompt", "gemini-flash-latest", None)

    assert "maxOutputTokens" not in payload["generationConfig"]


def test_gemini_35_flash_payload_uses_medium_thinking_level_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL", raising=False)

    payload = summarization._build_gemini_payload("prompt", "gemini-3.5-flash", 4096)

    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}


def test_dynamic_length_instruction_keeps_word_count_advisory():
    instruction = summarization._dynamic_length_instruction(1500, 270)
    assert "1500" in instruction
    assert "270" in instruction
    assert "Längenregel" in instruction
    assert "kein hartes Limit" in instruction
    assert "Vollständigkeit" in instruction
    assert "kürze keine relevanten Inhalte" in instruction


@pytest.mark.asyncio
async def test_openai_summary_request_can_omit_scriber_output_limit(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(summarization.Config, "OPENAI_API_KEY", "openai-test-key")

    class _FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text="complete summary",
                status="completed",
                incomplete_details=None,
                output=[],
            )

    class _FakeClient:
        def __init__(self, **_kwargs):
            self.responses = _FakeResponses()

    class _FakeApiError(Exception):
        pass

    fake_openai = SimpleNamespace(AsyncOpenAI=_FakeClient, APIError=_FakeApiError)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = await summarization._summarize_openai("prompt", "gpt-5-mini", None)

    assert result == "complete summary"
    assert captured == {"model": "gpt-5-mini", "input": "prompt"}


def test_normalize_summary_html_converts_markdown_drift():
    raw = "Titel\n\n•\tErster Punkt\n• Zweiter Punkt"
    normalized = normalize_summary_html(raw)
    assert normalized == "<p>Titel</p><ul><li>Erster Punkt</li><li>Zweiter Punkt</li></ul>"


@pytest.mark.asyncio
async def test_summarize_text_lets_provider_choose_output_length(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    async def _fake_openai(prompt: str, model: str, max_output_tokens: int | None) -> str:
        captured["prompt"] = prompt
        captured["model"] = model
        captured["max_output_tokens"] = max_output_tokens
        return _structured_summary("ok")

    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)
    monkeypatch.setattr(
        summarization.Config,
        "SUMMARIZATION_PROMPT",
        "Custom instruction: return the answer as Markdown.",
    )

    out = await summarization.summarize_text(_words(4000), model="gpt-5-mini")

    assert out == _structured_summary("ok")
    assert captured["model"] == "gpt-5-mini"
    assert captured["max_output_tokens"] is None
    prompt = str(captured["prompt"])
    assert "Zusätzliche Längenregel" in prompt
    assert "Return only one well-formed, semantic, static HTML fragment" in prompt
    assert "Do not return Markdown" in prompt
    assert "no fixed item count" in prompt
    assert "Do not force a card count" in prompt
    assert "Scriber owns typography, spacing, colors, and interaction" in prompt
    assert "Infer the dominant natural language from the transcript itself" in prompt
    assert "UNTRUSTED_TRANSCRIPT_TEXT:" in prompt
    assert (
        prompt.index("Custom instruction: return the answer as Markdown.")
        < prompt.index("Output contract (mandatory")
        < prompt.index("UNTRUSTED_TRANSCRIPT_TEXT:")
    )


@pytest.mark.asyncio
async def test_summarize_text_uses_configured_language_only_as_ambiguous_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, str] = {}

    async def _fake_openai(prompt: str, _model: str, _max_output_tokens: int) -> str:
        captured["prompt"] = prompt
        return _structured_summary("Deutsche Zusammenfassung", "Überblick")

    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)
    monkeypatch.setattr(summarization.Config, "LANGUAGE", "de-DE", raising=False)

    result = await summarization.summarize_text("Wir besprechen heute die nächsten Schritte.", model="gpt-5-mini")

    assert result == _structured_summary("Deutsche Zusammenfassung", "Überblick")
    assert "Only if the transcript is too short or language-neutral to decide, use de-DE" in captured["prompt"]
    assert "do not translate the transcript" in captured["prompt"]


@pytest.mark.asyncio
async def test_summarize_text_empty_input_returns_empty():
    out = await summarization.summarize_text("   ", model="gpt-5-mini")
    assert out == ""


@pytest.mark.asyncio
async def test_summarize_with_model_rejects_empty_provider_result(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _empty_openai(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        return "  "

    monkeypatch.setattr(summarization, "_summarize_openai", _empty_openai)

    with pytest.raises(RuntimeError, match="empty text response"):
        await summarization._summarize_with_model("prompt", "gpt-5-mini", 1024)


@pytest.mark.asyncio
async def test_summarize_text_never_adds_provider_token_cap_for_long_video(monkeypatch: pytest.MonkeyPatch):
    captured_tokens: list[int | None] = []

    async def _fake_openai(_prompt: str, _model: str, max_output_tokens: int | None) -> str:
        captured_tokens.append(max_output_tokens)
        return _structured_summary("ok")

    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_MIN_SECONDS", "1800")
    monkeypatch.setenv("SCRIBER_SUMMARY_LONG_VIDEO_TOKEN_BONUS", "700")

    text = _words(3500)
    await summarization.summarize_text(text, model="gpt-5-mini", duration="29:59")
    await summarization.summarize_text(text, model="gpt-5-mini", duration="30:01")

    assert len(captured_tokens) == 2
    assert captured_tokens == [None, None]


@pytest.mark.asyncio
async def test_summarize_text_projects_markdown_model_drift_to_html(monkeypatch: pytest.MonkeyPatch):
    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        return "# Zusammenfassung\n\nKurzer Überblick.\n\n•\tPunkt A\n• Punkt B"

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)

    out = await summarization.summarize_text("x y z", model="gemini-flash-latest")

    assert out == (
        "<section><h2>Zusammenfassung</h2><p>Kurzer Überblick.</p><ul><li>Punkt A</li><li>Punkt B</li></ul></section>"
    )


@pytest.mark.asyncio
async def test_summarize_text_rejects_sanitized_empty_or_unstructured_output(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_openai(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        return "<script>not displayable</script>"

    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "", raising=False)

    with pytest.raises(RuntimeError, match="no displayable structured HTML summary"):
        await summarization.summarize_text("x y z", model="gpt-5-mini")


@pytest.mark.asyncio
async def test_summarize_text_openrouter_model_uses_nitro(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_openrouter(
        _prompt: str,
        models,
        _max_output_tokens: int,
        *,
        require_structured_html: bool = False,
    ) -> str:
        captured["models"] = models
        captured["require_structured_html"] = require_structured_html
        return _structured_summary("openrouter summary")

    monkeypatch.setattr(summarization, "_summarize_openrouter", _fake_openrouter)

    out = await summarization.summarize_text("x y z", model="minimax/minimax-m3")

    assert out == _structured_summary("openrouter summary")
    assert captured["models"] == "minimax/minimax-m3:nitro"
    assert captured["require_structured_html"] is True


@pytest.mark.asyncio
async def test_summarize_text_gpt_oss_120b_keeps_provider_routed_model(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_openrouter(
        _prompt: str,
        models,
        _max_output_tokens: int,
        *,
        require_structured_html: bool = False,
    ) -> str:
        captured["models"] = models
        captured["require_structured_html"] = require_structured_html
        return _structured_summary("openrouter summary")

    monkeypatch.setattr(summarization, "_summarize_openrouter", _fake_openrouter)

    out = await summarization.summarize_text("x y z", model="openai/gpt-oss-120b")

    assert out == _structured_summary("openrouter summary")
    assert captured["models"] == "openai/gpt-oss-120b"
    assert captured["require_structured_html"] is True


@pytest.mark.asyncio
async def test_generate_text_with_model_routes_cerebras_directly(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    async def _fake_cerebras(_prompt: str, model: str, max_output_tokens: int) -> str:
        captured["model"] = model
        captured["max_output_tokens"] = max_output_tokens
        return "cleaned text"

    monkeypatch.setattr(summarization, "_summarize_cerebras", _fake_cerebras)

    out = await summarization.generate_text_with_model(
        "raw prompt",
        "cerebras/gemma-4-31b",
        max_output_tokens=2048,
    )

    assert out == "cleaned text"
    assert captured == {"model": "cerebras/gemma-4-31b", "max_output_tokens": 2048}


@pytest.mark.asyncio
async def test_summarize_text_retries_next_openrouter_model_after_invalid_html_without_logging_content(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    log_records: list[tuple[str, tuple[object, ...]]] = []
    invalid_content = "<script>PRIVATE_RAW_SUMMARY_SENTINEL</script>"
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    class _CapturingLogger:
        def info(self, message: str, *args: object) -> None:
            log_records.append((message, args))

        def warning(self, message: str, *args: object) -> None:
            log_records.append((message, args))

        def error(self, message: str, *args: object) -> None:
            log_records.append((message, args))

    async def _fake_post(payload, _headers, _session):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "model": "minimax/minimax-m3",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": invalid_content},
                    }
                ],
            }
        return {
            "model": "z-ai/glm-5.2-20260616",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": _structured_summary("Fallback summary"),
                    },
                }
            ],
        }

    monkeypatch.setattr(summarization, "logger", _CapturingLogger())
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization.summarize_text("x y z", model="minimax/minimax-m3:nitro")

    assert out == _structured_summary("Fallback summary")
    assert calls[0]["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]
    assert calls[1]["model"] == "z-ai/glm-5.2:nitro"
    assert invalid_content not in repr(log_records)
    assert any("locally invalid structured HTML" in message for message, _args in log_records)


@pytest.mark.asyncio
async def test_summarize_text_stops_after_all_openrouter_models_return_invalid_html(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _session):
        calls.append(payload)
        response_model = "minimax/minimax-m3" if len(calls) == 1 else "z-ai/glm-5.2-20260616"
        return {
            "model": response_model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "<section><h2>Truncated</h2><p>Missing close",
                    },
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    with pytest.raises(RuntimeError, match="after trying the configured model candidates"):
        await summarization.summarize_text("x y z", model="minimax/minimax-m3:nitro")

    assert len(calls) == 2
    assert calls[1]["model"] == "z-ai/glm-5.2:nitro"


@pytest.mark.asyncio
async def test_generate_text_with_openrouter_preserves_raw_text_contract(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    raw_text = "RAW_TEXT_WITHOUT_SUMMARY_HTML"
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _session):
        calls.append(payload)
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": raw_text},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization.generate_text_with_model(
        "raw prompt",
        "minimax/minimax-m3:nitro",
        max_output_tokens=1024,
    )

    assert out == raw_text
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_summarize_openrouter_retries_empty_selected_model_with_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    sessions: list[object] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, session):
        calls.append(payload)
        sessions.append(session)
        if len(calls) == 1:
            return {
                "model": "minimax/minimax-m3:nitro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
                "usage": {"completion_tokens": 0, "total_tokens": 120},
            }
        return {
            "model": "z-ai/glm-5.2:nitro",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "fallback summary"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization._summarize_openrouter("prompt", "minimax/minimax-m3:nitro", 900)

    assert out == "fallback summary"
    assert calls[0]["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]
    assert calls[0]["max_tokens"] == 900
    assert calls[0]["reasoning"] == {"exclude": True, "effort": "medium"}
    assert calls[1]["model"] == "z-ai/glm-5.2:nitro"
    assert sessions[0] is sessions[1]


@pytest.mark.asyncio
async def test_summarize_openrouter_uses_gpt_oss_provider_route_before_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": ""},
                    }
                ],
            }
        return {
            "model": "minimax/minimax-m3:nitro",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "fallback summary"},
                }
            ],
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization._summarize_openrouter("prompt", "openai/gpt-oss-120b", 900)

    assert out == "fallback summary"
    assert calls[0]["model"] == "openai/gpt-oss-120b"
    assert calls[0]["provider"] == {"order": ["baseten", "cerebras"], "allow_fallbacks": True}
    assert calls[1]["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]
    assert "provider" not in calls[1]


@pytest.mark.asyncio
async def test_summarize_openrouter_retries_empty_length_response_with_next_model_and_larger_budget(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "model": "minimax/minimax-m3",
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "length",
                        "message": {"role": "assistant", "content": None},
                    }
                ],
                "usage": {
                    "completion_tokens": 900,
                    "prompt_tokens": 1989,
                    "total_tokens": 2889,
                    "completion_tokens_details": {"reasoning_tokens": 876},
                },
            }
        return {
            "model": "z-ai/glm-5.2-20260616",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "glm summary"},
                }
            ],
            "usage": {"completion_tokens": 420, "total_tokens": 2400},
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization._summarize_openrouter("prompt", "minimax/minimax-m3:nitro", 900)

    assert out == "glm summary"
    assert calls[0]["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]
    assert calls[0]["max_tokens"] == 900
    assert calls[0]["reasoning"] == {"exclude": True, "effort": "medium"}
    assert calls[1]["model"] == "z-ai/glm-5.2:nitro"
    assert calls[1]["max_tokens"] == 1800


@pytest.mark.asyncio
async def test_summarize_openrouter_retries_partial_length_response_instead_of_saving_it(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_post(payload, _headers, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "model": "z-ai/glm-5.2-20260616",
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "length",
                        "message": {
                            "role": "assistant",
                            "content": "## Zusammenfassung\n\n- Erbschaftsteuer-Falle: Ohne Geme",
                        },
                    }
                ],
                "usage": {
                    "completion_tokens": 900,
                    "prompt_tokens": 1989,
                    "total_tokens": 2889,
                    "completion_tokens_details": {"reasoning_tokens": 300},
                },
            }
        return {
            "model": "z-ai/glm-5.2-20260616",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "vollstaendige summary"},
                }
            ],
            "usage": {"completion_tokens": 420, "total_tokens": 2400},
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization._summarize_openrouter("prompt", "z-ai/glm-5.2:nitro", 900)

    assert out == "vollstaendige summary"
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"]


@pytest.mark.asyncio
async def test_summarize_openrouter_discards_partial_length_response_at_retry_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_RETRY_MAX_TOKENS", "900")

    async def _fake_post(_payload, _headers, _timeout):
        return {
            "model": "z-ai/glm-5.2-20260616",
            "choices": [
                {
                    "finish_reason": "length",
                    "native_finish_reason": "length",
                    "message": {"role": "assistant", "content": "partial darf nicht zurueckkommen"},
                }
            ],
            "usage": {"completion_tokens": 900, "total_tokens": 2400},
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    with pytest.raises(summarization.SummaryOutputLimitError, match="partial summary was discarded"):
        await summarization._summarize_openrouter("prompt", ["z-ai/glm-5.2:nitro"], 900)


@pytest.mark.asyncio
async def test_summarize_openrouter_stops_after_larger_partial_retry_without_model_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_RETRY_MAX_TOKENS", "8192")

    async def _fake_post(payload, _headers, _timeout):
        calls.append(payload)
        return {
            "model": "minimax/minimax-m3",
            "choices": [
                {
                    "finish_reason": "length",
                    "native_finish_reason": "length",
                    "message": {"role": "assistant", "content": "large but truncated response"},
                }
            ],
            "usage": {
                "completion_tokens": payload["max_tokens"],
                "prompt_tokens": 16_000,
                "total_tokens": 16_000 + payload["max_tokens"],
            },
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    with pytest.raises(RuntimeError, match="larger-budget retry"):
        await summarization._summarize_openrouter(
            "large meeting reduce prompt",
            ["minimax/minimax-m3:nitro"],
            4096,
        )

    assert [call["max_tokens"] for call in calls] == [4096, 8192]


@pytest.mark.asyncio
async def test_summarize_openrouter_tries_alternate_model_after_larger_partial_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_RETRY_MAX_TOKENS", "8192")

    async def _fake_post(payload, _headers, _timeout):
        calls.append(payload)
        if len(calls) < 3:
            return {
                "model": "minimax/minimax-m3",
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "length",
                        "message": {"role": "assistant", "content": "large but truncated response"},
                    }
                ],
                "usage": {
                    "completion_tokens": payload["max_tokens"],
                    "prompt_tokens": 16_000,
                    "total_tokens": 16_000 + payload["max_tokens"],
                },
            }
        return {
            "model": "z-ai/glm-5.2-20260616",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "complete alternate-model summary"},
                }
            ],
            "usage": {"completion_tokens": 1_200, "prompt_tokens": 16_000, "total_tokens": 17_200},
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization._summarize_openrouter(
        "large meeting reduce prompt",
        ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"],
        4096,
    )

    assert out == "complete alternate-model summary"
    assert [call["max_tokens"] for call in calls] == [4096, 8192, 8192]
    assert calls[2]["model"] == "z-ai/glm-5.2:nitro"


@pytest.mark.asyncio
async def test_summarize_openrouter_retries_initial_model_at_full_budget_after_other_model_hits_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)
    monkeypatch.setenv("SCRIBER_SUMMARY_OPENROUTER_RETRY_MAX_TOKENS", "8192")

    async def _fake_post(payload, _headers, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            model = "minimax/minimax-m3"
        elif len(calls) == 2:
            model = "z-ai/glm-5.2-20260616"
        else:
            return {
                "model": "minimax/minimax-m3",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                        "message": {"role": "assistant", "content": "complete first-model summary"},
                    }
                ],
            }
        return {
            "model": model,
            "choices": [
                {
                    "finish_reason": "length",
                    "native_finish_reason": "length",
                    "message": {"role": "assistant", "content": "truncated"},
                }
            ],
            "usage": {"completion_tokens": payload["max_tokens"]},
        }

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    out = await summarization._summarize_openrouter(
        "large meeting reduce prompt",
        ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"],
        4096,
    )

    assert out == "complete first-model summary"
    assert [call["max_tokens"] for call in calls] == [4096, 8192, 8192]
    assert calls[2]["model"] == "minimax/minimax-m3:nitro"


def test_openrouter_used_model_accepts_any_requested_custom_candidate():
    assert (
        summarization._openrouter_used_model(
            {"model": "vendor/custom-two-20260801"},
            ["vendor/custom-one:nitro", "vendor/custom-two:nitro"],
        )
        == "vendor/custom-two-20260801"
    )


@pytest.mark.asyncio
async def test_meeting_analysis_tries_complete_alternate_without_scriber_token_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        raise RuntimeError("Gemini API error 503: UNAVAILABLE")

    async def _fake_post(payload, _headers, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "model": "minimax/minimax-m3",
                "choices": [
                    {
                        "finish_reason": "length",
                        "native_finish_reason": "length",
                        "message": {"role": "assistant", "content": "truncated map JSON"},
                    }
                ],
                "usage": {
                    "completion_tokens": 8_192,
                    "prompt_tokens": 8_000,
                    "total_tokens": 16_192,
                },
            }
        return {
            "model": "z-ai/glm-5.2-20260616",
            "choices": [
                {
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                    "message": {"role": "assistant", "content": "VALID COMPLETE MAP JSON"},
                }
            ],
            "usage": {"completion_tokens": 1_100, "prompt_tokens": 8_000, "total_tokens": 9_100},
        }

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)
    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", _fake_post)

    result = await summarization.generate_meeting_analysis_text(
        "large meeting map prompt",
        "gemini-3.1-pro-preview",
        max_output_tokens=3072,
    )

    assert result == "VALID COMPLETE MAP JSON"
    assert len(calls) == 2
    assert all("max_tokens" not in call for call in calls)
    assert calls[1]["model"] == "z-ai/glm-5.2:nitro"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "primary_target", "primary_error"),
    [
        (
            "celeris-1",
            "celeris_chat_completion",
            CelerisOutputLimitError("Celeris reached its native output limit."),
        ),
        (
            "gemini-flash-latest",
            "_summarize_gemini",
            summarization.SummaryOutputLimitError("Gemini finish_reason=MAX_TOKENS."),
        ),
    ],
)
async def test_meeting_output_limit_classification_survives_other_fallback_failure(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    primary_target: str,
    primary_error: RuntimeError,
):
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _output_limited(*_args, **_kwargs):
        raise primary_error

    async def _different_fallback_failure(*_args, **_kwargs):
        raise RuntimeError("Fallback transport failed for an unrelated reason.")

    monkeypatch.setattr(summarization, primary_target, _output_limited)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _different_fallback_failure)

    with pytest.raises(type(primary_error)) as caught:
        await summarization.generate_meeting_analysis_text(
            "large meeting analysis prompt",
            model,
            max_output_tokens=3072,
        )

    assert caught.value is primary_error
    assert caught.value.meeting_analysis_error_code == "meeting_analysis_incomplete_response"


@pytest.mark.asyncio
async def test_summarize_text_falls_back_to_openrouter_when_primary_fails(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        raise RuntimeError("Gemini API error 503: UNAVAILABLE")

    async def _fake_openrouter(
        _prompt: str,
        models,
        _max_output_tokens: int,
        *,
        require_structured_html: bool = False,
    ) -> str:
        captured["models"] = models
        captured["require_structured_html"] = require_structured_html
        return _structured_summary("openrouter fallback")

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _fake_openrouter)

    out = await summarization.summarize_text("x y z", model="gemini-3.5-flash")

    assert out == _structured_summary("openrouter fallback")
    assert captured["models"] == ["minimax/minimax-m3:nitro", "z-ai/glm-5.2:nitro"]
    assert captured["require_structured_html"] is True


@pytest.mark.asyncio
async def test_gemini_summary_does_not_fallback_to_openai_by_default(monkeypatch: pytest.MonkeyPatch):
    openai_calls = 0
    monkeypatch.delenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENAI", raising=False)
    monkeypatch.setattr(summarization.Config, "OPENAI_API_KEY", "openai-key")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "", raising=False)

    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        raise RuntimeError("Gemini API error 429: RESOURCE_EXHAUSTED")

    async def _fake_openai(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        nonlocal openai_calls
        openai_calls += 1
        return "openai summary"

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)
    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)

    with pytest.raises(RuntimeError, match="Gemini API error 429"):
        await summarization.summarize_text("x y z", model="gemini-flash-latest")

    assert openai_calls == 0


@pytest.mark.asyncio
async def test_gemini_summary_openai_fallback_is_explicit_and_contextual(monkeypatch: pytest.MonkeyPatch):
    openai_calls = 0
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENAI", "1")
    monkeypatch.setattr(summarization.Config, "OPENAI_API_KEY", "openai-key")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "", raising=False)

    async def _fake_gemini(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        raise RuntimeError("Gemini API error 429: RESOURCE_EXHAUSTED")

    async def _fake_openai(_prompt: str, _model: str, _max_output_tokens: int) -> str:
        nonlocal openai_calls
        openai_calls += 1
        raise RuntimeError("OpenAI API error: insufficient_quota")

    monkeypatch.setattr(summarization, "_summarize_gemini", _fake_gemini)
    monkeypatch.setattr(summarization, "_summarize_openai", _fake_openai)

    with pytest.raises(
        RuntimeError, match="Gemini summarization failed and the configured OpenAI fallback also failed"
    ):
        await summarization.summarize_text("x y z", model="gemini-flash-latest")

    assert openai_calls == 1


@pytest.mark.asyncio
async def test_summarize_gemini_never_sends_a_scriber_output_token_limit(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("SCRIBER_SUMMARY_GEMINI_THINKING_LEVEL", "high")

    async def _fake_post(_session, _url, payload, *, retries):
        calls.append(payload)
        assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "HIGH"}
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "vollstaendige zusammenfassung"}]},
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 900, "totalTokenCount": 1200},
        }

    monkeypatch.setattr(summarization, "_post_gemini_generate_content", _fake_post)

    out = await summarization._summarize_gemini("prompt", "gemini-flash-latest", 3000)

    assert out == "vollstaendige zusammenfassung"
    assert len(calls) == 1
    assert "maxOutputTokens" not in calls[0]["generationConfig"]


@pytest.mark.asyncio
async def test_summarize_gemini_discards_partial_at_native_max_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(summarization.Config, "GOOGLE_API_KEY", "test-key")

    async def _fake_post(_session, _url, payload, *, retries):
        assert "maxOutputTokens" not in payload["generationConfig"]
        return {
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": "partial darf nicht zurueckkommen"}]},
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 65_536, "totalTokenCount": 66_000},
        }

    monkeypatch.setattr(summarization, "_post_gemini_generate_content", _fake_post)

    with pytest.raises(summarization.SummaryOutputLimitError, match="MAX_TOKENS"):
        await summarization._summarize_gemini("prompt", "gemini-flash-latest", 3000)


@pytest.mark.asyncio
async def test_gemini_meeting_fallback_has_no_output_limit_and_reports_rejected_key(
    monkeypatch: pytest.MonkeyPatch,
):
    observed: dict[str, object] = {}
    notices: list[tuple[str, str]] = []
    monkeypatch.setenv("SCRIBER_SUMMARY_FALLBACK_TO_OPENROUTER", "1")
    monkeypatch.setattr(summarization.Config, "OPENROUTER_API_KEY", "openrouter-key", raising=False)

    async def _rejected_gemini(_prompt: str, _model: str, max_output_tokens: int | None) -> str:
        observed["gemini_tokens"] = max_output_tokens
        raise summarization.provider_transport_error(
            "gemini",
            "summarization",
            status=400,
            code="authentication_error",
        )

    async def _fallback(_prompt: str, _models, max_output_tokens: int | None) -> str:
        observed["fallback_tokens"] = max_output_tokens
        return '{"schemaVersion":"1"}'

    async def _notice(model: str, reason: str) -> None:
        notices.append((model, reason))

    monkeypatch.setattr(summarization, "_summarize_gemini", _rejected_gemini)
    monkeypatch.setattr(summarization, "_summarize_openrouter", _fallback)

    result = await summarization.generate_meeting_analysis_text(
        "meeting prompt",
        "gemini-flash-latest",
        max_output_tokens=3072,
        on_fallback=_notice,
    )

    assert result == '{"schemaVersion":"1"}'
    assert observed == {"gemini_tokens": None, "fallback_tokens": None}
    assert notices == [("gemini-flash-latest", "authentication_error")]


@pytest.mark.asyncio
async def test_gemini_invalid_key_response_gets_authentication_code():
    class Response:
        status = 400

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return json.dumps(
                {
                    "error": {
                        "code": 400,
                        "message": "API key not valid. Please pass a valid API key.",
                        "status": "INVALID_ARGUMENT",
                    }
                }
            )

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(summarization.ProviderTransportError) as captured:
        await summarization._post_gemini_generate_content(
            Session(),
            "https://gemini.example/generate",
            {"contents": []},
            retries=0,
        )

    assert captured.value.status == 400
    assert captured.value.code == "authentication_error"
    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_summarize_gemini_without_scriber_cap_still_discards_native_truncation(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(summarization.Config, "GOOGLE_API_KEY", "test-key")

    async def _fake_post(_session, _url, payload, *, retries):
        calls.append(payload)
        return {
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": "partial darf nicht zurueckkommen"}]},
                }
            ],
            "usageMetadata": {"candidatesTokenCount": 65_536, "totalTokenCount": 66_000},
        }

    monkeypatch.setattr(summarization, "_post_gemini_generate_content", _fake_post)

    with pytest.raises(summarization.SummaryOutputLimitError, match="MAX_TOKENS"):
        await summarization._summarize_gemini("prompt", "gemini-flash-latest", None)

    assert len(calls) == 1
    assert "maxOutputTokens" not in calls[0]["generationConfig"]
