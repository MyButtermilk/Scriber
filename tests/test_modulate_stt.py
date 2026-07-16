from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from pipecat.frames.frames import (
    AudioRawFrame,
    ErrorFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from src.config import Config
from src.core.provider_capabilities import get_capabilities
from src.core.provider_errors import provider_user_error
from src.modulate_stt import (
    MODULATE_BATCH_MODEL,
    MODULATE_BATCH_URL,
    MODULATE_STREAMING_MODEL,
    MODULATE_STREAMING_URL,
    ModulateAsyncProcessor,
    ModulateRealtimeSTTService,
    modulate_transcript_payload_to_text,
    redact_modulate_error,
    transcribe_with_modulate_multilingual,
)
from src.pipeline import ScriberPipeline


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._raw = payload if isinstance(payload, str) else json.dumps(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        del encoding, errors
        return self._raw

    @property
    def content(self):
        response = self

        class _Content:
            async def iter_chunked(self, _size: int):
                yield response._raw.encode("utf-8")

        return _Content()


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


class _FakeWebSocket:
    def __init__(self, messages: list[aiohttp.WSMessage], *, close_code: int | None = None):
        self._messages = messages
        self.close_code = close_code

    def __aiter__(self):
        async def _messages():
            for message in self._messages:
                yield message

        return _messages()


class _ClosableWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent_strings: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.close_count = 0
        self.on_send_eos = None

    async def send_str(self, value: str) -> None:
        self.sent_strings.append(value)
        if self.on_send_eos:
            self.on_send_eos()

    async def send_bytes(self, value: bytes) -> None:
        self.sent_bytes.append(value)

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


def _form_fields(form: aiohttp.FormData) -> dict[str, object]:
    fields: dict[str, object] = {}
    for disposition, _headers, value in form._fields:
        fields[str(disposition["name"])] = value
    return fields


@pytest.mark.asyncio
async def test_modulate_batch_is_final_text_only_and_disables_every_optional_output():
    session = _FakeSession(
        _FakeResponse(
            200,
            {
                "text": "Guten Morgen. Bonjour.",
                "duration_ms": 3200,
                "utterances": [
                    {
                        "text": "must not persist",
                        "speaker": 7,
                        "emotion": "Happy",
                        "accent": "Other",
                        "deepfake_score": 0.9,
                    }
                ],
                "pii_phi_tagging": ["must not persist"],
            },
        )
    )

    payload = await transcribe_with_modulate_multilingual(
        session=session,  # type: ignore[arg-type]
        api_key="secret-key",
        audio_source=b"RIFF-audio",
        filename="audio.wav",
        content_type="audio/wav",
        language="de-DE",
    )

    assert payload == {"text": "Guten Morgen. Bonjour.", "duration_ms": 3200}
    assert session.posts[0][0] == MODULATE_BATCH_URL
    request = session.posts[0][1]
    assert request["headers"] == {"X-API-Key": "secret-key"}
    fields = _form_fields(request["data"])  # type: ignore[arg-type]
    assert fields["upload_file"] == b"RIFF-audio"
    assert fields == {
        "upload_file": b"RIFF-audio",
        "speaker_diarization": "false",
        "emotion_signal": "false",
        "accent_signal": "false",
        "deepfake_signal": "false",
        "pii_phi_tagging": "false",
        "language": "de",
    }


@pytest.mark.asyncio
async def test_modulate_batch_omits_language_when_auto_detecting():
    session = _FakeSession(_FakeResponse(200, {"text": "Bonjour."}))

    await transcribe_with_modulate_multilingual(
        session=session,  # type: ignore[arg-type]
        api_key="secret-key",
        audio_source=b"RIFF-audio",
        filename="audio.wav",
        content_type="audio/wav",
        language="auto",
    )

    fields = _form_fields(session.posts[0][1]["data"])  # type: ignore[arg-type]
    assert "language" not in fields


@pytest.mark.asyncio
async def test_modulate_batch_provider_error_redacts_the_credential():
    session = _FakeSession(
        _FakeResponse(
            403,
            "request wss://platform.modulate.ai/api/x?api_key=secret-key was denied",
        )
    )
    with pytest.raises(RuntimeError) as caught:
        await transcribe_with_modulate_multilingual(
            session=session,  # type: ignore[arg-type]
            api_key="secret-key",
            audio_source=b"audio",
            filename="audio.wav",
            content_type="audio/wav",
        )
    assert "secret-key" not in str(caught.value)
    assert "api_key=[REDACTED]" in str(caught.value)


def test_modulate_top_level_text_parser_never_falls_back_to_utterances():
    assert modulate_transcript_payload_to_text({"text": " final "}) == "final"
    assert (
        modulate_transcript_payload_to_text(
            {"utterances": [{"text": "do not expose this utterance"}]}
        )
        == ""
    )


def test_modulate_stream_url_is_raw_pcm_final_only_with_all_signals_disabled():
    service = ModulateRealtimeSTTService(
        api_key="private key + value",
        language="de-DE",
        sample_rate=16_000,
        channels=1,
    )
    split = urlsplit(service._ws_url())
    assert f"{split.scheme}://{split.netloc}{split.path}" == MODULATE_STREAMING_URL
    assert split.netloc == "modulate-developer-apis.com"
    assert split.path == f"/api/{MODULATE_STREAMING_MODEL}"
    params = parse_qs(split.query)
    assert params == {
        "api_key": ["private key + value"],
        "audio_format": ["s16le"],
        "sample_rate": ["16000"],
        "num_channels": ["1"],
        "speaker_diarization": ["false"],
        "emotion_signal": ["false"],
        "accent_signal": ["false"],
        "deepfake_signal": ["false"],
        "pii_phi_tagging": ["false"],
        "partial_results": ["false"],
        "language": ["de"],
    }


def test_modulate_uses_current_official_velma_api_hosts():
    batch = urlsplit(MODULATE_BATCH_URL)
    streaming = urlsplit(MODULATE_STREAMING_URL)

    assert batch.scheme == "https"
    assert streaming.scheme == "wss"
    assert batch.netloc == streaming.netloc == "modulate-developer-apis.com"
    assert batch.path == f"/api/{MODULATE_BATCH_MODEL}"
    assert streaming.path == f"/api/{MODULATE_STREAMING_MODEL}"


def test_modulate_runtime_diagnostics_name_the_exact_model_and_mode():
    realtime = ScriberPipeline(service_name="modulate").stt_runtime_configuration()
    batch = ScriberPipeline(service_name="modulate_async").stt_runtime_configuration()

    assert realtime["provider"] == "modulate"
    assert realtime["model"] == MODULATE_STREAMING_MODEL
    assert realtime["mode"] == "realtime"
    assert realtime["sampleRateHz"] == Config.SAMPLE_RATE
    assert "api" not in realtime
    assert batch["model"] == MODULATE_BATCH_MODEL
    assert batch["mode"] == "batch"


@pytest.mark.asyncio
async def test_modulate_start_reaches_downstream_before_connection_error():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    observed: list[str] = []

    async def push(frame, _direction):
        observed.append(type(frame).__name__)

    async def fail_connect(direction):
        observed.append("connect")
        await service.push_frame(
            ErrorFrame(error="modulate realtime error: Cannot connect to host"),
            direction,
        )
        return False

    service.push_frame = push  # type: ignore[method-assign]
    service._ensure_connected = fail_connect  # type: ignore[method-assign]

    with patch(
        "src.modulate_stt.FrameProcessor.process_frame",
        new=AsyncMock(),
    ):
        await service.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)

    assert observed == ["StartFrame", "connect", "ErrorFrame"]


@pytest.mark.asyncio
async def test_modulate_terminal_send_failure_is_emitted_once():
    class _FailingWebSocket:
        closed = False

        async def send_bytes(self, _audio: bytes) -> None:
            raise OSError("connection reset")

    service = ModulateRealtimeSTTService(api_key="secret-key")
    service._ws = _FailingWebSocket()  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    frame = AudioRawFrame(audio=b"\x00\x01", sample_rate=16_000, num_channels=1)

    with patch(
        "src.modulate_stt.FrameProcessor.process_frame",
        new=AsyncMock(),
    ):
        await service.process_frame(frame, FrameDirection.DOWNSTREAM)
        await service.process_frame(frame, FrameDirection.DOWNSTREAM)

    emitted = [
        call.args[0]
        for call in service.push_frame.await_args_list
        if isinstance(call.args[0], ErrorFrame)
    ]
    assert len(emitted) == 1
    assert service._terminal_error_emitted is True
    assert service._connect_failed is True
    assert service._terminal_event.is_set()


@pytest.mark.asyncio
async def test_modulate_invalid_json_terminates_receive_processing_once():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    should_stop = await service._handle_response(
        "not-json",
        FrameDirection.DOWNSTREAM,
    )
    await service._handle_response("still-not-json", FrameDirection.DOWNSTREAM)

    assert should_stop is True
    assert service.push_frame.await_count == 1
    assert isinstance(service.push_frame.await_args.args[0], ErrorFrame)
    assert service._terminal_event.is_set()


@pytest.mark.asyncio
async def test_modulate_stream_ignores_partial_and_emits_only_final_text_without_metadata():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    stopped = await service._handle_response(
        json.dumps(
            {
                "type": "partial_utterance",
                "partial_utterance": {"text": "do not emit"},
            }
        ),
        FrameDirection.DOWNSTREAM,
    )
    assert stopped is False
    service.push_frame.assert_not_awaited()

    stopped = await service._handle_response(
        json.dumps(
            {
                "type": "utterance",
                "utterance": {
                    "text": "Final text.",
                    "speaker": 2,
                    "emotion": "Happy",
                    "accent": "Other",
                    "deepfake_score": 0.4,
                },
            }
        ),
        FrameDirection.DOWNSTREAM,
    )
    assert stopped is False
    frame = service.push_frame.await_args.args[0]
    assert isinstance(frame, TranscriptionFrame)
    assert frame.text == "Final text."
    assert frame.result is None
    assert frame.finalized is True


@pytest.mark.asyncio
async def test_modulate_stream_surfaces_premature_close_with_safe_code():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    service._ws = _FakeWebSocket(  # type: ignore[assignment]
        [aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 4001, "secret-key")]
    )

    await service._receive_responses(FrameDirection.DOWNSTREAM)

    frame = service.push_frame.await_args.args[0]
    assert isinstance(frame, ErrorFrame)
    assert "close code 4001" in frame.error
    assert "secret-key" not in frame.error


@pytest.mark.asyncio
async def test_modulate_stream_classifies_1011_as_provider_server_error():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    service._ws = _FakeWebSocket(  # type: ignore[assignment]
        [aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1011, "")]
    )

    await service._receive_responses(FrameDirection.DOWNSTREAM)

    frame = service.push_frame.await_args.args[0]
    assert isinstance(frame, ErrorFrame)
    assert "internal server error" in frame.error
    assert "close code 1011" in frame.error
    assert service.push_frame.await_count == 1
    error = provider_user_error("modulate", frame.error)
    assert error.category.value == "transient_provider"


@pytest.mark.asyncio
async def test_modulate_error_is_forwarded_before_terminal_waiters_are_released():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    release_push = asyncio.Event()

    async def slow_push(_frame, _direction):
        await release_push.wait()

    service.push_frame = slow_push  # type: ignore[method-assign]
    emit_task = asyncio.create_task(
        service._emit_error("provider failure", FrameDirection.DOWNSTREAM)
    )
    await asyncio.sleep(0)

    assert service._terminal_error_emitted is True
    assert service._terminal_event.is_set() is False

    release_push.set()
    assert await emit_task is True
    assert service._terminal_event.is_set() is True


@pytest.mark.asyncio
async def test_modulate_empty_stream_shutdown_is_immediate_and_sends_no_eos():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    websocket = _ClosableWebSocket()
    receiver = asyncio.create_task(asyncio.Event().wait())
    service._ws = websocket  # type: ignore[assignment]
    service._receive_task = receiver
    service._final_timeout_secs = 30.0
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    await asyncio.wait_for(
        service._close_stream(
            FrameDirection.DOWNSTREAM,
            wait_for_final=True,
        ),
        timeout=0.25,
    )

    assert websocket.sent_strings == []
    assert websocket.close_count == 1
    assert receiver.done()
    assert service._stream_closed is True
    service.push_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_modulate_cancel_path_does_not_request_final_transcript():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    websocket = _ClosableWebSocket()
    receiver = asyncio.create_task(asyncio.Event().wait())
    service._ws = websocket  # type: ignore[assignment]
    service._receive_task = receiver
    service._audio_bytes_sent = 640
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    await asyncio.wait_for(
        service._close_stream(
            FrameDirection.DOWNSTREAM,
            wait_for_final=False,
        ),
        timeout=0.25,
    )

    assert websocket.sent_strings == []
    assert websocket.close_count == 1
    assert receiver.done()
    service.push_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_modulate_nonempty_stream_waits_for_done_after_eos():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    websocket = _ClosableWebSocket()
    eos_sent = asyncio.Event()
    websocket.on_send_eos = eos_sent.set
    service._ws = websocket  # type: ignore[assignment]
    service._audio_bytes_sent = 640
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    async def receive_done() -> None:
        await eos_sent.wait()
        await service._handle_response(
            json.dumps({"type": "done", "duration_ms": 20}),
            FrameDirection.DOWNSTREAM,
        )

    receiver = asyncio.create_task(receive_done())
    service._receive_task = receiver

    await service._close_stream(
        FrameDirection.DOWNSTREAM,
        wait_for_final=True,
    )

    assert websocket.sent_strings == [""]
    assert service._done_received is True
    assert receiver.done()
    assert websocket.closed is True
    service.push_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_modulate_final_timeout_emits_once_and_reclaims_receiver():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    websocket = _ClosableWebSocket()
    receiver = asyncio.create_task(asyncio.Event().wait())
    service._ws = websocket  # type: ignore[assignment]
    service._receive_task = receiver
    service._audio_bytes_sent = 640
    service._final_timeout_secs = 0.01
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    await service._close_stream(
        FrameDirection.DOWNSTREAM,
        wait_for_final=True,
    )
    await service._close_stream(
        FrameDirection.DOWNSTREAM,
        wait_for_final=True,
    )

    errors = [
        call.args[0]
        for call in service.push_frame.await_args_list
        if isinstance(call.args[0], ErrorFrame)
    ]
    assert len(errors) == 1
    assert "timed out waiting for the final transcript" in errors[0].error
    assert receiver.done()
    assert websocket.sent_strings == [""]
    assert websocket.close_count == 1


@pytest.mark.asyncio
async def test_modulate_cancelled_final_wait_still_reclaims_websocket():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    websocket = _ClosableWebSocket()
    eos_sent = asyncio.Event()
    websocket.on_send_eos = eos_sent.set
    receiver = asyncio.create_task(asyncio.Event().wait())
    service._ws = websocket  # type: ignore[assignment]
    service._receive_task = receiver
    service._audio_bytes_sent = 640
    service._final_timeout_secs = 30.0

    close_task = asyncio.create_task(
        service._close_stream(
            FrameDirection.DOWNSTREAM,
            wait_for_final=True,
        )
    )
    await asyncio.wait_for(eos_sent.wait(), timeout=0.25)
    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert receiver.done()
    assert websocket.closed is True
    assert service._stream_closed is True


@pytest.mark.asyncio
async def test_modulate_stream_done_does_not_emit_a_close_error():
    service = ModulateRealtimeSTTService(api_key="secret-key")
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    service._ws = _FakeWebSocket(  # type: ignore[assignment]
        [
            aiohttp.WSMessage(
                aiohttp.WSMsgType.TEXT,
                json.dumps({"type": "done"}),
                "",
            )
        ],
        close_code=1000,
    )

    await service._receive_responses(FrameDirection.DOWNSTREAM)

    service.push_frame.assert_not_awaited()
    assert service._done_received is True


def test_modulate_error_redactor_never_keeps_query_credentials():
    raw = "failed api_key=secret-key&partial_results=false secret-key"
    safe = redact_modulate_error(raw, "secret-key")
    assert "secret-key" not in safe
    assert "api_key=[REDACTED]" in safe


def test_modulate_provider_contracts_and_factory(monkeypatch):
    monkeypatch.setattr(Config, "MODULATE_API_KEY", "configured")
    realtime = get_capabilities("modulate")
    batch = get_capabilities("modulate_async")
    assert realtime.supports_live_streaming is True
    assert realtime.injects_immediately_in_live_mode is True
    assert realtime.supports_batch_diarization is False
    assert batch.supports_direct_file_upload is True
    assert batch.supports_batch_diarization is False

    realtime_service = ScriberPipeline(service_name="modulate")._create_stt_service(
        object()  # type: ignore[arg-type]
    )
    async_service = ScriberPipeline(service_name="modulate_async")._create_stt_service(
        object()  # type: ignore[arg-type]
    )
    assert isinstance(realtime_service, ModulateRealtimeSTTService)
    assert isinstance(async_service, ModulateAsyncProcessor)
    assert async_service._language == Config.LANGUAGE


def test_modulate_user_errors_are_provider_specific():
    auth = provider_user_error("modulate", "websocket close 4001 auth failure")
    assert auth.provider == "modulate"
    assert "Modulate" in auth.provider_label
    assert auth.retryable is False

    credits = provider_user_error(
        "modulate_async", "websocket close 4029 insufficient credits"
    )
    assert credits.provider == "modulate_async"
    assert credits.retryable is True
    assert "credits" in credits.message.lower()

    audio = provider_user_error(
        "modulate", "websocket closed before done (close code 1003)"
    )
    assert audio.provider == "modulate"
    assert audio.retryable is False
    assert "audio" in audio.message.lower()


@pytest.mark.asyncio
async def test_pipeline_direct_modulate_uses_batch_adapter(monkeypatch, tmp_path: Path):
    audio_path = tmp_path / "sample.webm"
    audio_path.write_bytes(b"webm")
    monkeypatch.setattr(Config, "MODULATE_API_KEY", "configured")
    calls: list[dict[str, object]] = []

    async def _fake_transcribe(**kwargs):
        calls.append(kwargs)
        return {"text": "Only final text.", "duration_ms": 1000}

    monkeypatch.setattr(
        "src.pipeline.transcribe_with_modulate_multilingual", _fake_transcribe
    )
    received: list[tuple[str, bool]] = []
    pipeline = ScriberPipeline(
        service_name="modulate_async",
        on_transcription=lambda text, final: received.append((text, final)),
    )

    await pipeline.transcribe_file_direct(str(audio_path))

    assert received == [("Only final text.", True)]
    assert pipeline.last_structured_transcript_payload == {
        "text": "Only final text.",
        "duration_ms": 1000,
    }
    assert calls[0]["content_type"] == "audio/webm"
    assert calls[0]["language"] == pipeline._execution_language()
