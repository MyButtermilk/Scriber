from __future__ import annotations

import json

import pytest

from src import summarization
from src.cloud_async_stt import OpenRouterSTTProcessor, transcribe_with_openrouter_audio_transcription
from src.config import Config
from src.core.error_taxonomy import ErrorCategory
from src.core.provider_errors import ProviderTransportError, provider_transport_error, provider_user_error
from src.openrouter_region import normalize_openrouter_region, openrouter_api_base_url

REGIONS = [
    ("eu", "https://eu.openrouter.ai/api/v1"),
    ("us", "https://us.openrouter.ai/api/v1"),
    ("global", "https://openrouter.ai/api/v1"),
]


class Response:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {"text": "Synthetic transcript"}
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return json.dumps(self.payload)


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, **kwargs):
        if "data" in kwargs:
            kwargs["body"] = json.loads(kwargs["data"].read())
        self.calls.append((url, kwargs))
        return next(self.responses)

    async def close(self):
        pass


def test_missing_and_legacy_region_defaults_to_eu():
    assert Config.DEFAULT_OPENROUTER_REGION == "eu"
    assert normalize_openrouter_region(None) == "eu"
    assert normalize_openrouter_region("") == "eu"
    assert normalize_openrouter_region(" US ", strict=True) == "us"


@pytest.mark.parametrize("value", ["jp", "https://openrouter.ai", False, 1, [], {}])
def test_invalid_region_never_selects_a_different_origin(value):
    with pytest.raises(ValueError, match="Invalid OpenRouter region"):
        openrouter_api_base_url(value)


@pytest.mark.parametrize("region,base_url", REGIONS)
def test_setting_round_trips_independently_of_api_key_and_soniox(monkeypatch, tmp_path, region, base_url):
    monkeypatch.setattr(Config, "OPENROUTER_REGION", "eu")
    monkeypatch.setenv("SCRIBER_OPENROUTER_REGION", "eu")
    key, soniox_region = Config.OPENROUTER_API_KEY, Config.SONIOX_REGION
    Config.set_openrouter_region(region)
    target = tmp_path / ".env"
    Config.persist_to_env_file(str(target))
    assert f"SCRIBER_OPENROUTER_REGION={region}" in target.read_text()
    assert openrouter_api_base_url(Config.OPENROUTER_REGION) == base_url
    assert key == Config.OPENROUTER_API_KEY
    assert soniox_region == Config.SONIOX_REGION


@pytest.mark.asyncio
@pytest.mark.parametrize("region,base_url", REGIONS)
async def test_stt_and_chat_use_the_same_region_key_and_model_contract(monkeypatch, region, base_url):
    monkeypatch.setattr(Config, "OPENROUTER_REGION", region)
    session = Session([Response(), Response()])
    await transcribe_with_openrouter_audio_transcription(
        session=session,
        api_key="synthetic-key",
        audio_source=b"test-audio",
        filename="audio.mp3",
        content_type="audio/mpeg",
        language="de",
    )
    payload = {"model": "openai/gpt-oss-120b", "messages": []}
    await summarization._post_openrouter_chat_completion(payload, {"Authorization": "Bearer synthetic-key"}, session)
    assert [url for url, _ in session.calls] == [f"{base_url}/audio/transcriptions", f"{base_url}/chat/completions"]
    for _, options in session.calls:
        assert options["allow_redirects"] is False
        assert options["headers"]["Authorization"] == "Bearer synthetic-key"
    assert session.calls[0][1]["body"]["model"] == "microsoft/mai-transcribe-2"
    assert session.calls[1][1]["json"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["eu", "us"])
@pytest.mark.parametrize("workload", ["stt", "summary", "meeting", "post_processing", "fallback"])
async def test_regional_404_is_actionable_and_never_replayed_or_sent_global(monkeypatch, region, workload):
    monkeypatch.setattr(Config, "OPENROUTER_REGION", region)
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "synthetic-key")
    session = Session(
        [Response(404, {"error": {"message": "No endpoints found supporting your data region. PRIVATE-ECHO"}})]
    )
    monkeypatch.setattr(summarization.aiohttp, "ClientSession", lambda **_kwargs: session)
    model = "openai/gpt-oss-120b"
    with pytest.raises(ProviderTransportError) as caught:
        if workload == "stt":
            await transcribe_with_openrouter_audio_transcription(
                session=session,
                api_key="synthetic-key",
                audio_source=b"audio",
                filename="audio.mp3",
                content_type="audio/mpeg",
                language=None,
            )
        elif workload == "summary":
            await summarization.summarize_text("Synthetic transcript", model)
        elif workload == "meeting":
            from src.meeting_analysis import analyze_meeting

            await analyze_meeting(
                "Synthetic meeting",
                [{"id": "segment-1", "startMs": 0, "endMs": 1000, "text": "Synthetic speech"}],
                [],
                model=model,
                generate=summarization.generate_text_with_model,
            )
        elif workload == "post_processing":
            from src.post_processing import post_process_live_transcript

            await post_process_live_transcript("Synthetic speech", model=model, engine="cloud")
        else:
            await summarization._try_openrouter_summary_fallback(
                "Synthetic prompt",
                primary_model="cerebras/gemma-4-31b",
                primary_error=RuntimeError("synthetic primary failure"),
                max_output_tokens=128,
                timeout_seconds=10,
            )
    error = caught.value
    assert error.status == 404
    assert error.code == "region_unavailable"
    assert error.region == region
    assert error.retryable is False
    assert "PRIVATE-ECHO" not in str(error)
    assert "PRIVATE-ECHO" not in str(error.diagnostic_metadata())
    public = provider_user_error(None, error)
    assert public.category == ErrorCategory.CONFIG_INVALID
    assert f"{region.upper()} region" in public.message
    assert "did not switch to the global endpoint" in public.message
    # Live Mic's string-only ErrorFrame must retain the same guidance.
    assert (
        provider_user_error("openrouter_stt", f"openrouter_stt async error: {public.message}").message == public.message
    )
    assert len(session.calls) == 1
    assert session.calls[0][0].startswith(f"https://{region}.openrouter.ai/")


def test_global_404_keeps_generic_model_error():
    error = provider_transport_error("openrouter", "summarization", status=404, region="global")
    assert error.code != "region_unavailable"
    assert "EU" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("workload", ["stt", "chat"])
async def test_redirect_is_rejected_instead_of_followed(monkeypatch, workload):
    monkeypatch.setattr(Config, "OPENROUTER_REGION", "eu")
    response = Response(307)
    response.headers = {"Location": "https://openrouter.ai/api/v1/chat/completions"}
    session = Session([response])
    with pytest.raises(ProviderTransportError) as caught:
        if workload == "stt":
            await transcribe_with_openrouter_audio_transcription(
                session=session,
                api_key="test",
                audio_source=b"audio",
                filename="audio.mp3",
                content_type="audio/mpeg",
                language=None,
            )
        else:
            await summarization._post_openrouter_chat_completion({}, {}, session)
    assert caught.value.status == 307
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_chat_semantic_retries_pin_region_even_if_settings_change(monkeypatch):
    monkeypatch.setattr(Config, "OPENROUTER_REGION", "eu")
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "test")
    captured_regions = []

    async def post(_payload, _headers, _session, *, region):
        captured_regions.append(region)
        Config.OPENROUTER_REGION = "global"
        content, reason = ("partial", "length") if len(captured_regions) == 1 else ("complete", "stop")
        return {"model": "openai/gpt-oss-120b", "choices": [{"message": {"content": content}, "finish_reason": reason}]}

    monkeypatch.setattr(summarization, "_post_openrouter_chat_completion", post)
    assert await summarization._summarize_openrouter("Synthetic prompt", "openai/gpt-oss-120b", 128) == "complete"
    assert captured_regions == ["eu", "eu"]


def test_buffered_stt_captures_region_at_construction(monkeypatch):
    monkeypatch.setattr(Config, "OPENROUTER_REGION", "eu")
    processor = OpenRouterSTTProcessor(api_key="test", language=None)
    Config.OPENROUTER_REGION = "global"
    assert processor._region == "eu"


@pytest.mark.asyncio
async def test_stt_rate_limit_retry_keeps_region_after_settings_change(monkeypatch):
    from src import cloud_async_stt

    monkeypatch.setattr(Config, "OPENROUTER_REGION", "eu")
    monkeypatch.setattr(cloud_async_stt, "_openrouter_retry_delay", lambda *_args: 0)

    class RateLimit(Response):
        async def text(self):
            Config.OPENROUTER_REGION = "global"
            return await super().text()

    session = Session([RateLimit(429), Response()])
    await transcribe_with_openrouter_audio_transcription(
        session=session,
        api_key="test",
        audio_source=b"audio",
        filename="audio.mp3",
        content_type="audio/mpeg",
        language=None,
    )
    assert [url for url, _ in session.calls] == ["https://eu.openrouter.ai/api/v1/audio/transcriptions"] * 2


def test_meeting_recovery_and_post_processing_keep_safe_region_guidance():
    from src.web_api import (
        ScriberWebController,
        _meeting_analysis_failure_details,
        _persisted_meeting_analysis_failure_details,
    )

    error = provider_transport_error(
        "openrouter", "summarization", status=404, region="eu", response_body="PRIVATE-ECHO"
    )
    code, message = _meeting_analysis_failure_details(error)
    assert code == "meeting_analysis_region_unavailable"
    assert "EU region" in message
    assert "PRIVATE-ECHO" not in message
    assert _persisted_meeting_analysis_failure_details({"errorCode": code, "errorMessage": message}) == (code, message)
    assert ScriberWebController._post_processing_error_summary(error) == message
