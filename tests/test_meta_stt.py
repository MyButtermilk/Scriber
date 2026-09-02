from __future__ import annotations

import asyncio
import io
import wave
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from src.audio_prepare import ProbedAudioInput, ProviderAudioPreparationError, resolve_provider_audio_selection
from src.config import Config
from src.core.provider_audio_formats import AudioInputFormat, AudioSelectionMode, UnsupportedProviderAudioRoute
from src.core.provider_capabilities import get_capabilities
from src.core.provider_errors import ProviderTransportError, provider_user_error
from src.meta_stt import (
    META_STT_MODEL,
    MetaAsyncProcessor,
    MetaRealtimeSTTService,
    meta_request_options,
    transcribe_with_meta,
    validate_meta_wav,
)
from src.pipeline import ScriberPipeline, _live_analyzer_requirements, _live_service_uses_async_finalization
from src.provider_transcript import normalize_provider_segments, normalize_provider_words
from src.transcript_artifacts import freeze_provider_route

DOWN = FrameDirection.DOWNSTREAM


def wav_bytes(*, rate=16000, channels=1, seconds=0.02):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * int(rate * seconds) * channels)
    return stream.getvalue()


def final_payload():
    return {
        "sessionId": "session",
        "transcript": "Hallo Welt.",
        "audioDurationMs": 2000,
        "turns": [{"turnId": 1, "startMs": 0, "endMs": 2000, "transcript": "Hallo Welt.", "speaker": "A"}],
    }


@asynccontextmanager
async def server(handler, *, websocket=False):
    app = web.Application()
    app.router.add_route("GET" if websocket else "POST", "/asr", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"{'ws' if websocket else 'http'}://127.0.0.1:{port}/asr"
    finally:
        await runner.cleanup()


def test_model_language_and_vocabulary_are_exact():
    assert meta_request_options(model=META_STT_MODEL, language="de-DE", custom_vocab=" Scriber, Muse, Scriber ") == {
        "model": META_STT_MODEL,
        "languageBias": ["German"],
        "keywords": ["Scriber", "Muse"],
    }
    assert meta_request_options(model=META_STT_MODEL, language="auto") == {"model": META_STT_MODEL}
    with pytest.raises(ValueError, match="not verified"):
        meta_request_options(model="muse-voice-transcribe-async")
    with pytest.raises(ValueError, match="not supported"):
        meta_request_options(model=META_STT_MODEL, language="xx")


@pytest.mark.asyncio
async def test_direct_file_uses_frozen_model_language_and_vocabulary(monkeypatch, tmp_path):
    from src import audio_prepare

    source = tmp_path / "input.wav"
    source.write_bytes(wav_bytes())
    monkeypatch.setattr(Config, "MODEL_API_KEY", "test-key")
    monkeypatch.setattr(
        audio_prepare,
        "probe_audio_input_file",
        lambda path: ProbedAudioInput(
            AudioInputFormat.WAV_PCM16, "wav", "pcm_s16le", 16000, 1, 20, path.stat().st_size
        ),
    )
    route = freeze_provider_route(workload="file", provider="meta_stt_async", language="de", custom_vocab="Scriber")
    monkeypatch.setattr(Config, "META_STT_MODEL", "not-the-frozen-model")
    monkeypatch.setattr(Config, "LANGUAGE", "fr")
    transcribe = AsyncMock(return_value=final_payload())
    monkeypatch.setattr("src.meta_stt.transcribe_with_meta", transcribe)

    class Transport:
        async def session_view(self, **kwargs):
            return object()

    pipeline = ScriberPipeline(
        service_name="meta_stt_async", execution_route=route.execution_route(), provider_http_transport=Transport()
    )
    results = []
    pipeline.on_transcription = lambda text, final: results.append((text, final))
    await pipeline.transcribe_file_direct(str(source))
    assert transcribe.call_args.kwargs["model"] == META_STT_MODEL
    assert transcribe.call_args.kwargs["language"] == "de"
    assert transcribe.call_args.kwargs["custom_vocab"] == "Scriber"
    assert transcribe.call_args.kwargs["endpoint"] == "https://api.meta.ai/v1/asr/transcribe"
    assert results == [("Hallo Welt.", True)]
    assert pipeline.last_structured_transcript_payload == final_payload()
    assert source.read_bytes() == wav_bytes()


@pytest.mark.parametrize("rate,channels", [(44100, 1), (16000, 2)])
def test_invalid_wav_is_rejected(rate, channels):
    with pytest.raises(ValueError, match="mono PCM16"):
        validate_meta_wav(io.BytesIO(wav_bytes(rate=rate, channels=channels)))


def test_wav_size_duration_and_truncation_are_checked():
    stream = io.BytesIO(wav_bytes())
    stream.seek(3)
    validate_meta_wav(stream)
    assert stream.tell() == 3 and not stream.closed
    with pytest.raises(ValueError, match="truncated"):
        validate_meta_wav(io.BytesIO(wav_bytes()[:-2]))
    with pytest.raises(ValueError, match="10 minutes"):
        validate_meta_wav(io.BytesIO(wav_bytes(seconds=601)))
    with pytest.raises(ValueError, match="32 MB"):
        validate_meta_wav(io.BytesIO(b"0" * 32_000_000))


@pytest.mark.asyncio
async def test_http_contract_real_multipart_and_normalized_output():
    seen = {}

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.headers["Accept"] == "application/json"
        reader = await request.multipart()
        settings = await reader.next()
        assert settings.name == "request" and settings.headers["Content-Type"] == "application/json"
        seen.update(await settings.json())
        audio = await reader.next()
        assert audio.name == "audio"
        validate_meta_wav(io.BytesIO(await audio.read()))
        return web.json_response({**final_payload(), "unexpected": "private"})

    async with server(handler) as endpoint, aiohttp.ClientSession() as session:
        payload = await transcribe_with_meta(
            session=session,
            api_key="test-secret",
            audio_source=wav_bytes(),
            language="de",
            custom_vocab="Scriber",
            mode="DIARIZATION",
            endpoint=endpoint,
        )
    assert seen == {
        "model": META_STT_MODEL,
        "audioEncoding": "WAV",
        "mode": "DIARIZATION",
        "languageBias": ["German"],
        "keywords": ["Scriber"],
    }
    assert set(payload) == {"transcript", "turns", "audioDurationMs"}
    segments = normalize_provider_segments("meta_stt_async", payload, "mix")
    assert segments[0]["speakerKey"] == "A" and segments[0]["alignmentQuality"] == "provider_segment"
    assert normalize_provider_words("meta_stt_async", payload) == []


@pytest.mark.parametrize("status", [400, 401, 403, 413, 429, 500])
@pytest.mark.asyncio
async def test_http_errors_are_safe_and_never_retried(status):
    calls = []

    async def handler(request):
        calls.append(1)
        await request.read()
        return web.Response(status=status, text="test-secret transcript-private")

    async with server(handler) as endpoint, aiohttp.ClientSession() as session:
        with pytest.raises(ProviderTransportError) as raised:
            await transcribe_with_meta(
                session=session, api_key="test-secret", audio_source=wav_bytes(), endpoint=endpoint
            )
    assert calls == [1]
    public = provider_user_error("meta_stt_async", raised.value)
    assert "test-secret" not in str(raised.value) and "transcript-private" not in str(public)


@pytest.mark.asyncio
async def test_http_rejects_invalid_success():
    async def handler(request):
        await request.read()
        return web.json_response({"text": "wrong schema"})

    async with server(handler) as endpoint, aiohttp.ClientSession() as session:
        with pytest.raises(RuntimeError, match="invalid final"):
            await transcribe_with_meta(session=session, api_key="test", audio_source=wav_bytes(), endpoint=endpoint)


@pytest.mark.asyncio
async def test_realtime_handshake_pcm_drain_and_final_turns():
    seen = {}

    async def handler(request):
        assert "Authorization" not in request.headers
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        seen.update(await ws.receive_json())
        await ws.send_json({"sessionId": "test"})
        audio = await ws.receive()
        assert audio.type == aiohttp.WSMsgType.BINARY and audio.data == b"\0\0" * 160
        await ws.send_json({"type": "speechStart", "turnId": 4, "audioProcessedMs": 0})
        await ws.send_json({"type": "transcript", "transcript": "H", "final": False, "audioProcessedMs": 5})
        await ws.send_json({"type": "transcript", "transcript": "Hallo", "final": True, "audioProcessedMs": 10})
        assert await ws.receive_json() == {"type": "endStream"}
        await ws.send_json({"type": "speechComplete", "turnId": 4, "transcript": "Hallo!", "audioProcessedMs": 10})
        await ws.close(code=1000)
        return ws

    async with server(handler, websocket=True) as endpoint, aiohttp.ClientSession() as session:
        service = MetaRealtimeSTTService(session=session, api_key="test-secret", language="de", endpoint=endpoint)
        service.push_frame = AsyncMock()
        await service._connect(DOWN)
        await service.process_frame(AudioRawFrame(audio=b"\0\0" * 160, sample_rate=16000, num_channels=1), DOWN)
        await asyncio.sleep(0.02)
        await service._close(finalize=True, direction=DOWN)
        frames = [call.args[0] for call in service.push_frame.call_args_list]
        assert not any(isinstance(frame, ErrorFrame) for frame in frames)
        assert [frame.text for frame in frames if type(frame) is TranscriptionFrame] == ["Hallo!"]
        assert [frame.text for frame in frames if isinstance(frame, InterimTranscriptionFrame)] == ["H", "Hallo"]
        assert service._tasks.pending_count == 0
    assert seen["authorization"] == {"accessToken": "Bearer test-secret"}
    assert seen["audioEncoding"] == "PCM_16KHZ" and seen["mode"] == "ENDPOINTING"
    assert seen["partialMode"] == "CUMULATIVE" and seen["model"] == META_STT_MODEL


@asynccontextmanager
async def delayed_realtime_server(stage):
    paused = asyncio.Event()
    resume = asyncio.Event()
    received = asyncio.Queue()

    async def pause_at(boundary):
        if stage == boundary:
            paused.set()
            await resume.wait()

    async def handler(request):
        await pause_at("upgrade")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await pause_at("acknowledgement")
        await ws.send_json({"sessionId": "test"})
        async for message in ws:
            if message.type == aiohttp.WSMsgType.BINARY:
                received.put_nowait(message.data)
        return ws

    async with server(handler, websocket=True) as endpoint:
        yield endpoint, paused, resume, received


@pytest.mark.parametrize("stage", ["upgrade", "acknowledgement"])
@pytest.mark.asyncio
async def test_audio_waits_for_authenticated_startup(stage):
    async with (
        delayed_realtime_server(stage) as (endpoint, paused, resume, received),
        aiohttp.ClientSession() as session,
    ):
        service = MetaRealtimeSTTService(session=session, api_key="test-secret", endpoint=endpoint)
        service.push_frame = AsyncMock()
        await service.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
        start = asyncio.create_task(service.process_frame(StartFrame(), DOWN))
        audio = None
        try:
            await asyncio.wait_for(paused.wait(), 2)
            audio = asyncio.create_task(
                service.process_frame(AudioRawFrame(audio=b"\0\0" * 160, sample_rate=16000, num_channels=1), DOWN)
            )
            await asyncio.sleep(0)  # let audio enter while startup is held at the barrier
            assert not audio.done(), "Audio must wait for the authenticated socket, not return during startup"
            assert service._audio_bytes == 0
            resume.set()
            await asyncio.wait_for(asyncio.gather(start, audio), 2)
            assert await asyncio.wait_for(received.get(), 2) == b"\0\0" * 160
            assert service._audio_bytes == 320 and received.empty()
        finally:
            resume.set()
            await asyncio.gather(start, *([audio] if audio is not None else []), return_exceptions=True)
            await service._close(finalize=False, direction=DOWN)
            await service.cleanup()
        assert not any(isinstance(call.args[0], ErrorFrame) for call in service.push_frame.call_args_list)


@pytest.mark.parametrize("stage", ["upgrade", "acknowledgement"])
@pytest.mark.asyncio
async def test_cancel_during_startup_leaves_no_socket_or_receiver(stage):
    async with (
        delayed_realtime_server(stage) as (endpoint, paused, resume, received),
        aiohttp.ClientSession() as session,
    ):
        service = MetaRealtimeSTTService(session=session, api_key="test-secret", endpoint=endpoint)
        service.push_frame = AsyncMock()
        await service.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
        start = asyncio.create_task(service.process_frame(StartFrame(), DOWN))
        cancel = None
        try:
            await asyncio.wait_for(paused.wait(), 2)
            cancel = asyncio.create_task(service.process_frame(CancelFrame(), DOWN))
            await asyncio.sleep(0)
            resume.set()
            await asyncio.wait_for(asyncio.gather(start, cancel), 2)
            # Assert before ClientSession.__aexit__ can conceal a leaked socket.
            assert not session.closed
            assert service._closed and (service._ws is None or service._ws.closed)
            assert service._tasks.pending_count == 0 and received.empty()
        finally:
            resume.set()
            await asyncio.gather(start, *([cancel] if cancel is not None else []), return_exceptions=True)
            await service._tasks.close(timeout_seconds=2, cancel=True)
            if service._ws is not None:
                await service._ws.close()
            await service.cleanup()


@pytest.mark.asyncio
async def test_overlapping_turns_are_ordered_and_deduplicated():
    service = MetaRealtimeSTTService(session=AsyncMock(), api_key="test")
    service.push_frame = AsyncMock()
    for turn_id in [8, 3]:
        await service._handle_event({"type": "speechStart", "turnId": turn_id}, DOWN)
    await service._handle_event({"type": "speechComplete", "turnId": 3, "transcript": "Second."}, DOWN)
    service.push_frame.assert_not_called()
    await service._handle_event({"type": "speechComplete", "turnId": 8, "transcript": "First."}, DOWN)
    await service._handle_event({"type": "speechComplete", "turnId": 8, "transcript": "Duplicate."}, DOWN)
    assert [call.args[0].text for call in service.push_frame.call_args_list] == ["First.", "Second."]


@pytest.mark.parametrize("stage", ["upgrade", "acknowledgement"])
@pytest.mark.asyncio
async def test_cancel_discards_audio_already_waiting_for_startup(stage):
    async with (
        delayed_realtime_server(stage) as (endpoint, paused, resume, received),
        aiohttp.ClientSession() as session,
    ):
        service = MetaRealtimeSTTService(session=session, api_key="test-secret", endpoint=endpoint)
        service.push_frame = AsyncMock()
        await service.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
        start = asyncio.create_task(service.process_frame(StartFrame(), DOWN))
        pending = [start]
        try:
            await asyncio.wait_for(paused.wait(), 2)
            pending.append(
                asyncio.create_task(
                    service.process_frame(AudioRawFrame(audio=b"\0\0" * 160, sample_rate=16000, num_channels=1), DOWN)
                )
            )
            await asyncio.sleep(0)
            pending.append(asyncio.create_task(service.process_frame(CancelFrame(), DOWN)))
            async with asyncio.timeout(2):
                while not service._ending:
                    await asyncio.sleep(0)
            resume.set()
            await asyncio.wait_for(asyncio.gather(*pending), 2)
            assert service._audio_bytes == 0 and received.empty(), "Canceled queued audio must never be sent"
            assert service._ws.closed and service._tasks.pending_count == 0
            assert not any(isinstance(call.args[0], ErrorFrame) for call in service.push_frame.call_args_list)
        finally:
            resume.set()
            await asyncio.gather(*pending, return_exceptions=True)
            await service.cleanup()


@pytest.mark.parametrize("mode", ["error", "disconnect", "timeout", "bad_handshake"])
@pytest.mark.asyncio
async def test_realtime_failure_never_becomes_success(mode):
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        if mode == "bad_handshake":
            await ws.send_json({"type": "error", "message": "test-secret"})
        else:
            await ws.send_json({"sessionId": "test"})
            await ws.receive_json()
            if mode == "timeout":
                await asyncio.sleep(0.1)
            elif mode == "error":
                await ws.send_json({"type": "error", "message": "test-secret transcript-private"})
        await ws.close(code=1008)
        return ws

    async with server(handler, websocket=True) as endpoint, aiohttp.ClientSession() as session:
        service = MetaRealtimeSTTService(session=session, api_key="test-secret", endpoint=endpoint)
        service.push_frame = AsyncMock()
        service._final_timeout = 0.03
        await service._connect(DOWN)
        await service._close(finalize=True, direction=DOWN)
        errors = [
            call.args[0].error for call in service.push_frame.call_args_list if isinstance(call.args[0], ErrorFrame)
        ]
        assert len(errors) == 1 and "test-secret" not in errors[0] and "transcript-private" not in errors[0]
        assert service._tasks.pending_count == 0 and not session.closed


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_async_uploads_once_on_stop_never_on_cancel(monkeypatch, cancel):
    transcribe = AsyncMock(return_value=final_payload())
    monkeypatch.setattr("src.meta_stt.transcribe_with_meta", transcribe)
    processor = MetaAsyncProcessor(session=AsyncMock(), api_key="test")
    processor.push_frame = AsyncMock()
    await processor.process_frame(AudioRawFrame(audio=b"\0\0" * 160, sample_rate=16000, num_channels=1), DOWN)
    await processor.process_frame(CancelFrame() if cancel else EndFrame(), DOWN)
    await processor.process_frame(EndFrame(), DOWN)
    assert transcribe.await_count == (0 if cancel else 1)
    if not cancel:
        assert transcribe.call_args.kwargs["mode"] == "PUSH_TO_TALK"
    assert processor._buffer.closed


def test_settings_capabilities_routes_and_native_analyzers(monkeypatch):
    monkeypatch.setattr(Config, "MODEL_API_KEY", "test-meta")
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", True)
    for provider in ("meta_stt", "meta_stt_async"):
        assert Config.get_api_key(provider) == "test-meta"
        assert get_capabilities(provider).supports_batch_diarization
        assert not get_capabilities(provider).supports_word_timestamps
        assert get_capabilities(provider).meeting_max_duration_seconds == 600
        route = freeze_provider_route(workload="file", provider=provider)
        assert route.model == META_STT_MODEL and route.provider_route == "asr_transcribe"
        assert route.transport == "direct_upload"
        service = ScriberPipeline(service_name=provider)._create_stt_service(AsyncMock())
        assert isinstance(service, MetaAsyncProcessor if provider.endswith("async") else MetaRealtimeSTTService)
    assert _live_analyzer_requirements("meta_stt") == (False, False)
    assert _live_service_uses_async_finalization("meta_stt_async")


@pytest.mark.parametrize(
    "rate,channels,expected",
    [(16000, 1, "original_passthrough"), (24000, 1, "original_passthrough"), (44100, 2, "generated")],
)
def test_audio_selection_checks_rate_channels_and_model(rate, channels, expected):
    probe = ProbedAudioInput(AudioInputFormat.WAV_PCM16, "wav", "pcm_s16le", rate, channels, 1000, 32044)
    _, selection = resolve_provider_audio_selection(provider="meta_stt_async", model=META_STT_MODEL, probe=probe)
    assert selection.mode == AudioSelectionMode(expected)
    with pytest.raises(UnsupportedProviderAudioRoute):
        resolve_provider_audio_selection(provider="meta_stt_async", model="unknown", probe=probe)
    with pytest.raises(ProviderAudioPreparationError, match="10 minutes"):
        resolve_provider_audio_selection(
            provider="meta_stt_async",
            model=META_STT_MODEL,
            probe=ProbedAudioInput(AudioInputFormat.WAV_PCM16, "wav", "pcm_s16le", rate, channels, 600001, 32044),
        )
