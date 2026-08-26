import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from src.config import Config
from src.gemini_realtime_stt import (
    GEMINI_TRANSCRIBE_LIVE_URL,
    GeminiTranscribeLiveSTTService,
    gemini_custom_vocabulary,
    redact_gemini_live_error,
    sanitize_gemini_live_provider_error,
)
from src.pipeline import ScriberPipeline, _live_analyzer_requirements
from src.transcript_artifacts import freeze_provider_route


def test_gemini_live_setup_uses_dedicated_transcribe_contract():
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        language="de",
        custom_vocab="Scriber; Pipecat; Scriber",
    )

    assert service._setup_payload() == {
        "setup": {
            "model": "models/gemini-3.5-transcribe-live",
            "generationConfig": {"responseModalities": ["TEXT"]},
            "inputAudioTranscription": {
                "languageCodes": ["de-DE"],
                "customVocabulary": ["Scriber", "Pipecat"],
                "mode": "SMART",
            },
        }
    }
    assert gemini_custom_vocabulary("a,a;b") == ["a", "b"]

    spanish = GeminiTranscribeLiveSTTService(api_key="secret", language="es")
    assert spanish._setup_payload()["setup"]["inputAudioTranscription"]["languageCodes"] == ["es-419"]


def test_pipeline_builds_gemini_live_with_frozen_exact_route(monkeypatch):
    monkeypatch.setattr(Config, "GOOGLE_API_KEY", "secret")
    route = freeze_provider_route(workload="live", provider="gemini_realtime")
    pipeline = ScriberPipeline(
        service_name="gemini_realtime",
        execution_route=route.execution_route(),
    )

    service = pipeline._create_stt_service(object())
    runtime = pipeline.stt_runtime_configuration()

    assert isinstance(service, GeminiTranscribeLiveSTTService)
    assert service._model == "gemini-3.5-transcribe-live"
    assert runtime["mode"] == "realtime"
    assert runtime["model"] == "gemini-3.5-transcribe-live"
    assert _live_analyzer_requirements("gemini_realtime") == (False, False)


@pytest.mark.asyncio
async def test_gemini_live_maps_interim_and_final_without_provider_metadata():
    service = GeminiTranscribeLiveSTTService(api_key="secret")
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    service._has_audio_in_session = True

    await service._handle_response(
        json.dumps({"serverContent": {"interimInputTranscription": {"text": "Zwischenstand"}}})
    )
    assert service._pending_interim is True

    await service._handle_response(json.dumps({"serverContent": {"inputTranscription": {"text": "Fertig"}}}))

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert isinstance(frames[0], InterimTranscriptionFrame)
    assert frames[0].text == "Zwischenstand"
    assert frames[0].result is None
    assert isinstance(frames[1], TranscriptionFrame)
    assert frames[1].text == "Fertig"
    assert frames[1].result is None
    assert service._final_generation == 1
    assert service._transcription_event.is_set()
    assert service._has_audio_in_session is True
    assert service._pending_interim is False

    service._transcription_event.clear()
    await service._handle_response(
        json.dumps({"goAway": {"timeLeft": "5s"}, "serverContent": {"inputTranscription": {"text": ""}}})
    )
    assert service._rotate_after_final is True
    assert service._final_generation == 2
    assert service._transcription_event.is_set()


@pytest.mark.asyncio
async def test_gemini_live_audio_uses_base64_pcm_contract_and_rotates_first():
    class WebSocket:
        closed = False

        def __init__(self):
            self.messages: list[str] = []

        async def send_str(self, value: str):
            self.messages.append(value)

    websocket = WebSocket()
    service = GeminiTranscribeLiveSTTService(api_key="secret")
    service._ws = websocket  # type: ignore[assignment]
    service._connected_at = time.monotonic()
    service._rotate_after_final = True
    service._connect = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def rotate():
        service._rotate_after_final = False
        return True

    service._rotate = AsyncMock(side_effect=rotate)  # type: ignore[method-assign]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    pieces = [b"\x01\x02" * 512 for _index in range(4)]
    pcm = b"".join(pieces)

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        for piece in pieces:
            await service.process_frame(
                AudioRawFrame(audio=piece, sample_rate=16_000, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )

    service._rotate.assert_awaited_once()  # type: ignore[attr-defined]
    sent = json.loads(websocket.messages[0])
    assert sent == {
        "realtimeInput": {
            "audio": {
                "data": base64.b64encode(pcm[:3_200]).decode("ascii"),
                "mimeType": "audio/pcm;rate=16000",
            }
        }
    }
    assert bytes(service._pcm_buffer) == pcm[3_200:]


@pytest.mark.asyncio
async def test_gemini_live_rotation_keeps_audio_behind_handover_lock():
    class WebSocket:
        closed = False

        def __init__(self):
            self.messages: list[str] = []

        async def send_str(self, value: str):
            self.messages.append(value)

    old_websocket = WebSocket()
    replacement_websocket = WebSocket()
    rotation_started = asyncio.Event()
    allow_rotation = asyncio.Event()
    service = GeminiTranscribeLiveSTTService(api_key="secret")
    service._ws = old_websocket  # type: ignore[assignment]
    service._connected_at = time.monotonic()
    service._rotate_after_final = True
    service._connect = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    async def rotate():
        rotation_started.set()
        await allow_rotation.wait()
        service._ws = replacement_websocket  # type: ignore[assignment]
        service._rotate_after_final = False
        return True

    service._rotate = AsyncMock(side_effect=rotate)  # type: ignore[method-assign]
    first_pcm = b"\x01\x02" * 800
    second_pcm = b"\x03\x04" * 800

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        first = asyncio.create_task(
            service.process_frame(
                AudioRawFrame(audio=first_pcm, sample_rate=16_000, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )
        )
        await asyncio.wait_for(rotation_started.wait(), timeout=1.0)
        second = asyncio.create_task(
            service.process_frame(
                AudioRawFrame(audio=second_pcm, sample_rate=16_000, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )
        )
        await asyncio.sleep(0)

        assert first.done() is False
        assert second.done() is False
        assert old_websocket.messages == []
        assert replacement_websocket.messages == []

        allow_rotation.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)

    assert old_websocket.messages == []
    assert len(replacement_websocket.messages) == 1
    sent = json.loads(replacement_websocket.messages[0])
    assert base64.b64decode(sent["realtimeInput"]["audio"]["data"]) == first_pcm + second_pcm


@pytest.mark.asyncio
async def test_gemini_live_audio_cannot_overtake_setup_complete():
    class WebSocket:
        closed = False

        def __init__(self):
            self.messages: list[dict] = []
            self.setup_sent = asyncio.Event()

        async def send_str(self, value: str):
            payload = json.loads(value)
            self.messages.append(payload)
            if "setup" in payload:
                self.setup_sent.set()

        async def close(self):
            self.closed = True

    class Session:
        def __init__(self, websocket):
            self.websocket = websocket
            self.connect_args = ()
            self.connect_kwargs = {}

        async def ws_connect(self, *args, **kwargs):
            self.connect_args = args
            self.connect_kwargs = kwargs
            return self.websocket

    websocket = WebSocket()
    session = Session(websocket)
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        aiohttp_session=session,  # type: ignore[arg-type]
    )
    service._receive_responses = AsyncMock()  # type: ignore[method-assign]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        start = asyncio.create_task(service.process_frame(StartFrame(), FrameDirection.DOWNSTREAM))
        await asyncio.wait_for(websocket.setup_sent.wait(), timeout=1.0)
        audio = asyncio.create_task(
            service.process_frame(
                AudioRawFrame(audio=b"\x01\x02" * 1_600, sample_rate=16_000, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )
        )
        await asyncio.sleep(0.01)
        assert len(websocket.messages) == 1
        assert "setup" in websocket.messages[0]
        assert audio.done() is False

        await service._handle_response(json.dumps({"setupComplete": {}}))
        await asyncio.wait_for(asyncio.gather(start, audio), timeout=1.0)

    assert websocket.messages[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert session.connect_args == (GEMINI_TRANSCRIBE_LIVE_URL,)
    assert session.connect_kwargs["headers"] == {"x-goog-api-key": "secret"}
    assert "params" not in session.connect_kwargs
    assert "secret" not in session.connect_args[0]
    await service._close_connection()


@pytest.mark.asyncio
async def test_gemini_live_end_signals_audio_stream_end_and_drains_final():
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=1.0,
        final_quiet_seconds=0.01,
    )

    class WebSocket:
        closed = False

        def __init__(self):
            self.messages: list[dict] = []

        async def send_str(self, value: str):
            payload = json.loads(value)
            self.messages.append(payload)
            if payload == {"realtimeInput": {"audioStreamEnd": True}}:
                await service._handle_response(
                    json.dumps({"serverContent": {"inputTranscription": {"text": "Letztes Wort"}}})
                )

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service._pcm_buffer.extend(b"\x01\x02" * 10)
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert any(isinstance(frame, TranscriptionFrame) and frame.text == "Letztes Wort" for frame in frames)
    assert isinstance(frames[-1], EndFrame)
    assert websocket.messages[0]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert websocket.messages[1] == {"realtimeInput": {"audioStreamEnd": True}}
    assert websocket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_final", [False, True], ids=["silence-only", "trailing-silence"])
async def test_gemini_live_end_does_not_require_transcript_for_silence(prior_final):
    class WebSocket:
        closed = False

        def __init__(self):
            self.messages: list[dict] = []

        async def send_str(self, value: str):
            self.messages.append(json.loads(value))

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.01,
        final_quiet_seconds=0.01,
    )
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    if prior_final:
        await service._handle_response(json.dumps({"serverContent": {"inputTranscription": {"text": "Schon final"}}}))
    assert await service._send_pcm_chunk(b"\0" * 3_200) is True

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert sum(isinstance(frame, TranscriptionFrame) for frame in frames) == int(prior_final)
    assert isinstance(frames[-1], EndFrame)
    assert websocket.messages.count({"realtimeInput": {"audioStreamEnd": True}}) == 1
    assert websocket.closed is True
    assert service._terminal_failure is False


@pytest.mark.asyncio
async def test_gemini_live_uses_only_quiet_window_after_provider_final():
    class WebSocket:
        closed = False

        def __init__(self):
            self.messages: list[dict] = []

        async def send_str(self, value: str):
            self.messages.append(json.loads(value))

    websocket = WebSocket()
    service = GeminiTranscribeLiveSTTService(api_key="secret")
    service._ws = websocket  # type: ignore[assignment]
    service._has_audio_in_session = True
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    await service._handle_response(json.dumps({"serverContent": {"inputTranscription": {"text": "Schon final"}}}))
    service._wait_for_final_drain = AsyncMock(return_value=True)  # type: ignore[method-assign]
    service._wait_for_possible_late_final = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert await service._finish_current_stream(wait_for_final=True) is True
    service._wait_for_final_drain.assert_not_awaited()  # type: ignore[attr-defined]
    service._wait_for_possible_late_final.assert_awaited_once_with(1)  # type: ignore[attr-defined]
    assert websocket.messages == [{"realtimeInput": {"audioStreamEnd": True}}]


@pytest.mark.asyncio
async def test_gemini_live_quiet_window_catches_final_for_audio_sent_after_prior_final():
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.2,
        final_quiet_seconds=0.03,
    )

    class WebSocket:
        closed = False

        def __init__(self):
            self.late_task: asyncio.Task[None] | None = None

        async def send_str(self, value: str):
            if json.loads(value) == {"realtimeInput": {"audioStreamEnd": True}}:

                async def emit_late_final():
                    await asyncio.sleep(0.005)
                    await service._handle_response(
                        json.dumps({"serverContent": {"inputTranscription": {"text": "Final B"}}})
                    )

                self.late_task = asyncio.create_task(emit_late_final())

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    assert await service._send_pcm_chunk(b"A" * 3_200) is True
    assert await service._send_pcm_chunk(b"B" * 3_200) is True
    await service._handle_response(json.dumps({"serverContent": {"inputTranscription": {"text": "Final A"}}}))
    assert service._pending_interim is False

    assert await service._finish_current_stream(wait_for_final=True) is True
    if websocket.late_task:
        await websocket.late_task

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert any(isinstance(frame, TranscriptionFrame) and frame.text == "Final B" for frame in frames)
    assert service._final_generation == 2


@pytest.mark.asyncio
async def test_gemini_live_late_interim_promotes_quiet_window_to_strict_drain():
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.2,
        final_quiet_seconds=0.01,
    )
    observer_started = asyncio.Event()
    strict_drain_started = asyncio.Event()

    class WebSocket:
        closed = False

        def __init__(self):
            self.late_task: asyncio.Task[None] | None = None

        async def send_str(self, value: str):
            if json.loads(value) == {"realtimeInput": {"audioStreamEnd": True}}:

                async def emit_late_transcription():
                    await observer_started.wait()
                    await service._handle_response(
                        json.dumps({"serverContent": {"interimInputTranscription": {"text": "Noch offen"}}})
                    )
                    await strict_drain_started.wait()
                    assert self.closed is False
                    await service._handle_response(
                        json.dumps({"serverContent": {"inputTranscription": {"text": "Jetzt final"}}})
                    )

                self.late_task = asyncio.create_task(emit_late_transcription())

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    assert await service._send_pcm_chunk(b"A" * 3_200) is True
    observe_late = service._wait_for_possible_late_final
    wait_for_final = service._wait_for_final_drain

    async def observe_after_end(generation):
        observer_started.set()
        return await observe_late(generation)

    async def signal_strict_drain(generation):
        strict_drain_started.set()
        return await wait_for_final(generation)

    service._wait_for_possible_late_final = observe_after_end  # type: ignore[method-assign]
    service._wait_for_final_drain = signal_strict_drain  # type: ignore[method-assign]

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
    if websocket.late_task:
        await websocket.late_task

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert any(isinstance(frame, TranscriptionFrame) and frame.text == "Jetzt final" for frame in frames)
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert isinstance(frames[-1], EndFrame)
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_gemini_live_drains_interim_that_follows_another_final():
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.2,
        final_quiet_seconds=0.01,
    )

    class WebSocket:
        closed = False

        def __init__(self):
            self.late_task: asyncio.Task[None] | None = None

        async def send_str(self, value: str):
            if json.loads(value) == {"realtimeInput": {"audioStreamEnd": True}}:

                async def emit_two_final_turns():
                    await asyncio.sleep(0.005)
                    await service._handle_response(
                        json.dumps({"serverContent": {"inputTranscription": {"text": "Final A"}}})
                    )
                    await asyncio.sleep(0.005)
                    await service._handle_response(
                        json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen B"}}})
                    )
                    await asyncio.sleep(0.02)
                    assert self.closed is False
                    await service._handle_response(
                        json.dumps({"serverContent": {"inputTranscription": {"text": "Final B"}}})
                    )

                self.late_task = asyncio.create_task(emit_two_final_turns())

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    assert await service._send_pcm_chunk(b"A" * 3_200) is True
    await service._handle_response(json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen A"}}}))

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
    if websocket.late_task:
        await websocket.late_task

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    finals = [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)]
    assert finals == ["Final A", "Final B"]
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert isinstance(frames[-1], EndFrame)
    assert websocket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("same_response", "final_b_inline"),
    [(False, False), (True, False), (False, True)],
    ids=["separate-responses", "same-response", "next-final-before-wakeup"],
)
async def test_gemini_live_preserves_interim_that_precedes_unrelated_final(
    same_response,
    final_b_inline,
):
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.2,
        final_quiet_seconds=0.01,
    )

    class WebSocket:
        closed = False

        def __init__(self):
            self.late_task: asyncio.Task[None] | None = None

        async def send_str(self, value: str):
            payload = json.loads(value)
            if "audio" in payload.get("realtimeInput", {}):
                if same_response:
                    await service._handle_response(
                        json.dumps(
                            {
                                "serverContent": {
                                    "interimInputTranscription": {"text": "Offen B"},
                                    "inputTranscription": {"text": "Final A"},
                                }
                            }
                        )
                    )
                else:
                    await service._handle_response(
                        json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen B"}}})
                    )
                    if final_b_inline:
                        await service._handle_response(
                            json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen B erweitert"}}})
                        )
                    await service._handle_response(
                        json.dumps({"serverContent": {"inputTranscription": {"text": "Final A"}}})
                    )
                    if final_b_inline:
                        await service._handle_response(
                            json.dumps({"serverContent": {"inputTranscription": {"text": "Final B"}}})
                        )
            elif payload == {"realtimeInput": {"audioStreamEnd": True}}:
                if final_b_inline:
                    return

                async def emit_final_b():
                    await asyncio.sleep(0.05)
                    assert self.closed is False
                    await service._handle_response(
                        json.dumps({"serverContent": {"inputTranscription": {"text": "Final B"}}})
                    )

                self.late_task = asyncio.create_task(emit_final_b())

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    await service._handle_response(json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen A"}}}))
    service._pcm_buffer.extend(b"A" * 3_200)

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
    if websocket.late_task:
        await websocket.late_task

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    finals = [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)]
    assert finals == ["Final A", "Final B"]
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert isinstance(frames[-1], EndFrame)
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_gemini_live_preserves_multiple_interims_armed_before_stop():
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.2,
        final_quiet_seconds=0.01,
    )

    class WebSocket:
        closed = False

        def __init__(self):
            self.late_task: asyncio.Task[None] | None = None

        async def send_str(self, value: str):
            payload = json.loads(value)
            if "audio" in payload.get("realtimeInput", {}):
                await service._handle_response(
                    json.dumps({"serverContent": {"inputTranscription": {"text": "Final A"}}})
                )
            elif payload == {"realtimeInput": {"audioStreamEnd": True}}:

                async def emit_final_b():
                    await asyncio.sleep(0.05)
                    assert self.closed is False
                    await service._handle_response(
                        json.dumps({"serverContent": {"inputTranscription": {"text": "Final B"}}})
                    )

                self.late_task = asyncio.create_task(emit_final_b())

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    for text in ("Offen A", "Offen B"):
        await service._handle_response(json.dumps({"serverContent": {"interimInputTranscription": {"text": text}}}))
    service._pcm_buffer.extend(b"A" * 3_200)

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
    if websocket.late_task:
        await websocket.late_task

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)] == ["Final A", "Final B"]
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert isinstance(frames[-1], EndFrame)
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_gemini_live_uses_one_late_observer_for_drained_turns():
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.2,
        final_quiet_seconds=0.01,
    )
    observer_started = asyncio.Event()

    class WebSocket:
        closed = False

        def __init__(self):
            self.late_task: asyncio.Task[None] | None = None

        async def send_str(self, value: str):
            if json.loads(value) != {"realtimeInput": {"audioStreamEnd": True}}:
                return
            await service._handle_response(json.dumps({"serverContent": {"inputTranscription": {"text": "Final A"}}}))

            async def emit_turn_b():
                await observer_started.wait()
                await service._handle_response(
                    json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen B"}}})
                )
                await asyncio.sleep(0.005)
                await service._handle_response(
                    json.dumps({"serverContent": {"inputTranscription": {"text": "Final B"}}})
                )

            self.late_task = asyncio.create_task(emit_turn_b())

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service._has_audio_in_session = True
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    await service._handle_response(json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen A"}}}))
    observe_late = service._wait_for_possible_late_final

    async def observe_once(generation):
        observer_started.set()
        return await observe_late(generation)

    service._wait_for_possible_late_final = AsyncMock(  # type: ignore[method-assign]
        side_effect=observe_once
    )

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
    if websocket.late_task:
        await websocket.late_task

    assert service._wait_for_possible_late_final.await_count == 1  # type: ignore[attr-defined]
    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)] == ["Final A", "Final B"]
    assert isinstance(frames[-1], EndFrame)
    assert websocket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_text", "final_timeout"),
    [("Offen A erweitert heute.", 1.0), ("Final A", 0.02)],
    ids=["confirmed-revision", "ambiguous-timeout"],
)
async def test_gemini_live_interim_revision_stop_is_bounded_and_nonfatal(
    final_text,
    final_timeout,
):
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=final_timeout,
        final_quiet_seconds=0.01,
    )

    class WebSocket:
        closed = False

        async def send_str(self, value: str):
            payload = json.loads(value)
            if "audio" in payload.get("realtimeInput", {}):
                await service._handle_response(
                    json.dumps(
                        {
                            "serverContent": {
                                "interimInputTranscription": {"text": "Offen A erweitert"},
                                "inputTranscription": {"text": final_text},
                            }
                        }
                    )
                )

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    await service._handle_response(json.dumps({"serverContent": {"interimInputTranscription": {"text": "Offen A"}}}))
    service._pcm_buffer.extend(b"A" * 3_200)

    started = time.monotonic()
    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert time.monotonic() - started < 0.25
    assert [frame.text for frame in frames if isinstance(frame, TranscriptionFrame)] == [final_text]
    assert not any(isinstance(frame, ErrorFrame) for frame in frames)
    assert isinstance(frames[-1], EndFrame)
    assert service._terminal_failure is False
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_gemini_live_final_timeout_is_terminal_and_aborts_rotation():
    class WebSocket:
        closed = False

        async def send_str(self, _value: str):
            return None

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service = GeminiTranscribeLiveSTTService(
        api_key="secret",
        final_timeout_seconds=0.01,
        final_quiet_seconds=0.01,
    )
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    service._connect = AsyncMock(return_value=True)  # type: ignore[method-assign]
    await service._handle_response(json.dumps({"serverContent": {"interimInputTranscription": {"text": "Noch offen"}}}))
    assert service._pending_interim is True

    assert await service._rotate() is False

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert sum(isinstance(frame, ErrorFrame) for frame in frames) == 1
    assert service._terminal_failure is True
    assert websocket.closed is True
    service._connect.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gemini_live_provider_error_closes_socket_and_blocks_more_audio():
    class WebSocket:
        closed = False

        def __init__(self):
            self.messages: list[str] = []

        async def send_str(self, value: str):
            self.messages.append(value)

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service = GeminiTranscribeLiveSTTService(api_key="secret")
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    await service._handle_response(
        json.dumps(
            {
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "quota exhausted after secret transcript text",
                }
            }
        )
    )
    await service._emit_error("provider", "duplicate")

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(
            AudioRawFrame(audio=b"\x01\x02" * 1_600, sample_rate=16_000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )

    frames = [call.args[0] for call in service.push_frame.await_args_list]
    assert sum(isinstance(frame, ErrorFrame) for frame in frames) == 1
    assert websocket.messages == []
    assert websocket.closed is True
    assert service._terminal_failure is True
    assert "quota_exceeded" in frames[0].error
    assert "secret transcript text" not in frames[0].error
    assert sanitize_gemini_live_provider_error({"status": "INVALID_ARGUMENT"}) == "invalid_request_error"


@pytest.mark.asyncio
async def test_gemini_live_cancel_blocks_late_system_audio_from_reconnecting():
    class WebSocket:
        closed = False

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service = GeminiTranscribeLiveSTTService(api_key="secret")
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    with patch("src.gemini_realtime_stt.FrameProcessor.process_frame", new=AsyncMock()):
        await service.process_frame(CancelFrame(), FrameDirection.DOWNSTREAM)
        await service.process_frame(
            AudioRawFrame(audio=b"\x01\x02" * 1_600, sample_rate=16_000, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )

    assert websocket.closed is True
    assert service._stopped is True
    assert service._ws is None
    assert await service._connect() is False


@pytest.mark.asyncio
async def test_gemini_live_natural_remote_close_is_terminal():
    class WebSocket:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            self.closed = True

    websocket = WebSocket()
    service = GeminiTranscribeLiveSTTService(api_key="secret")
    service._ws = websocket  # type: ignore[assignment]
    service.push_frame = AsyncMock()  # type: ignore[method-assign]

    await service._receive_responses()

    frame = service.push_frame.await_args.args[0]
    assert isinstance(frame, ErrorFrame)
    assert "provider closed" in frame.error
    assert service._terminal_failure is True
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_gemini_live_redacts_key_from_provider_errors():
    key = "top-secret"
    service = GeminiTranscribeLiveSTTService(api_key=key)
    service.push_frame = AsyncMock()  # type: ignore[method-assign]
    error = f"wss://example.invalid/live?key={key}&alt=ws failed"

    await service._emit_error("connection", error)

    frame = service.push_frame.await_args.args[0]
    assert isinstance(frame, ErrorFrame)
    assert frame.fatal is True
    assert frame.processor is service
    assert key not in frame.error
    assert "key=[REDACTED]" in frame.error
    assert key not in redact_gemini_live_error(error, key)
