"""Issue #47 coverage through controller-owned production Gemini pipelines.

Only the native microphone, provider WebSocket and OS paste are replaced. The
ScriberPipeline, Pipecat worker, Gemini adapter, transcription callbacks,
TextInjector and controller's deferred injection path remain production code.
"""

import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import WSMsgType
from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor

import src.injector as injector_module
import src.pipeline as pipeline_module
from src import web_api
from src.config import Config
from src.pipeline import ScriberPipeline
from src.runtime.office_text_insert import OfficeInsertOutcome

pytest_plugins = ["test_live_mic_continuation"]


class _CleanupBlocksInsertion(Exception):
    """The known whole-finalizer fence still blocks already-complete text."""


class _Socket:
    def __init__(self):
        self.closed = False
        self.incoming = asyncio.Queue()
        self.setup_sent = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.on_end = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def emit(self, content):
        await self.incoming.put(SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(content)))

    async def send_str(self, value):
        message = json.loads(value)
        if "setup" in message:
            await self.emit({"setupComplete": {}})
            self.setup_sent.set()
        if message.get("realtimeInput", {}).get("audioStreamEnd") and self.on_end is not None:
            await self.on_end()

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.incoming.put(None)
            self.closed_event.set()


class _Microphone(FrameProcessor):
    def __init__(self, *, on_ready=None, **_kwargs):
        super().__init__()
        self.on_ready = on_ready
        self.native_stopped = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and self.on_ready is not None:
            self.on_ready()
        await self.push_frame(frame, direction)

    async def stop(self, _frame, *, close_stream=True):
        self.native_stopped = True

    def native_audio_stop_confirmed(self):
        return self.native_stopped


@pytest_asyncio.fixture
async def live(continuation, monkeypatch):
    controller = continuation.controller
    loop = asyncio.get_running_loop()
    pipelines = []
    sockets = []
    connections = asyncio.Queue()
    pasted = []
    paste_events = defaultdict(asyncio.Event)
    observations = {}
    capture_released = {}
    insertion_reached = defaultdict(asyncio.Event)
    cleanup_gates = []

    class Session:
        closed = False

        async def ws_connect(self, *_args, **_kwargs):
            socket = _Socket()
            sockets.append(socket)
            await connections.put(socket)
            return socket

    @asynccontextmanager
    async def provider_session(_self):
        yield Session()

    def paste(text, **_kwargs):
        # This is the OS boundary only. Production TextInjector still decides
        # whether/when to paste and invokes its own success callback.
        normalized = text.strip()
        pasted.append(normalized)
        loop.call_soon_threadsafe(paste_events[normalized].set)
        return True

    def factory(**kwargs):
        received = defaultdict(asyncio.Event)
        released = asyncio.Event()
        original_transcription = kwargs["on_transcription"]
        original_capture_stopped = kwargs["on_capture_stopped"]

        def observe_transcription(text, is_final):
            original_transcription(text, is_final)
            received[text, is_final].set()

        async def observe_capture_stopped():
            await original_capture_stopped()
            released.set()

        kwargs["on_transcription"] = observe_transcription
        kwargs["on_capture_stopped"] = observe_capture_stopped
        pipeline = ScriberPipeline(**kwargs)
        pipelines.append(pipeline)
        observations[pipeline] = received
        capture_released[pipeline] = released
        return pipeline

    class ObservedPredecessors(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            insertion_reached[key].set()
            return value

    monkeypatch.setattr(controller, "_select_available_provider", lambda: "gemini_realtime")
    monkeypatch.setattr(web_api, "ScriberPipeline", factory)
    monkeypatch.setattr(pipeline_module, "MicrophoneInput", _Microphone)
    monkeypatch.setattr(ScriberPipeline, "_provider_session", provider_session)
    monkeypatch.setattr(pipeline_module, "_resolve_live_mic_capture_device", lambda _manager: (None, False))
    monkeypatch.setattr(Config, "GOOGLE_API_KEY", "test-only")
    monkeypatch.setattr(Config, "MIC_AUTO_STOP_ENABLED", False)
    monkeypatch.setattr(Config, "SEGMENT_SPEECH_WITH_VAD", False)
    monkeypatch.setattr(Config, "DISABLE_TEXT_INJECTION", False)
    monkeypatch.setattr(Config, "INJECT_METHOD", "paste")
    monkeypatch.setattr(Config, "INJECT_TARGET_TITLE", "")
    monkeypatch.setenv("SCRIBER_GEMINI_LIVE_FINAL_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("SCRIBER_GEMINI_LIVE_FINAL_QUIET_SECONDS", "0.1")
    monkeypatch.setattr(injector_module, "HAS_GUI", True)
    monkeypatch.setattr(injector_module, "_paste_text", paste)
    monkeypatch.setattr(injector_module, "try_insert_office_text", lambda *_a, **_k: OfficeInsertOutcome.UNAVAILABLE)
    # Undo the shared fixture's high-level insertion mock: deferred successors
    # must reach the same production OS boundary as the first live pipeline.
    monkeypatch.setattr(
        controller,
        "_inject_live_transcript_text",
        web_api.ScriberWebController._inject_live_transcript_text.__get__(controller),
    )
    controller._live_mic_injection_predecessors = ObservedPredecessors(controller._live_mic_injection_predecessors)

    async def start():
        await controller.start_listening()
        pipeline = controller._pipeline
        record = controller._current
        socket = await asyncio.wait_for(connections.get(), 3)
        await asyncio.wait_for(socket.setup_sent.wait(), 3)
        await pipeline.task.queue_frame(InputAudioRawFrame(audio=bytes(3200), sample_rate=16_000, num_channels=1))
        return pipeline, record, socket

    async def emit(pipeline, socket, text, *, final=True):
        key = "inputTranscription" if final else "interimInputTranscription"
        await socket.emit({"serverContent": {key: {"text": text}}})
        await asyncio.wait_for(observations[pipeline][text, final].wait(), 3)

    harness = SimpleNamespace(
        controller=controller,
        start=start,
        stop=continuation.stop,
        emit=emit,
        pasted=pasted,
        paste_events=paste_events,
        capture_released=capture_released,
        insertion_reached=insertion_reached,
        cleanup_gates=cleanup_gates,
    )
    try:
        yield harness
    finally:
        for gate in cleanup_gates:
            gate.set()
        for pipeline in pipelines:
            if pipeline.task is not None and not pipeline.task.has_finished():
                await pipeline.task.cancel(reason="Gemini continuation test cleanup")
        if continuation.stop_tasks:
            await asyncio.wait_for(asyncio.gather(*continuation.stop_tasks, return_exceptions=True), 5)
        for socket in sockets:
            await socket.close()


@pytest.mark.asyncio
async def test_real_pipeline_keeps_late_predecessor_final_ahead_of_ready_successor(live):
    first, first_record, first_socket = await live.start()
    await live.emit(first, first_socket, "Erster Absatz.")
    await asyncio.wait_for(live.paste_events["Erster Absatz."].wait(), 3)
    await live.emit(first, first_socket, "Offen A", final=False)

    async def ambiguous_end():
        await first_socket.emit(
            {
                "serverContent": {
                    "interimInputTranscription": {"text": "Offen A erweitert"},
                    "inputTranscription": {"text": "Weiterer Satz im ersten Absatz."},
                    "generationComplete": True,
                }
            }
        )

    first_socket.on_end = ambiguous_end
    first_stop = live.stop()
    await asyncio.wait_for(live.capture_released[first].wait(), 3)
    await asyncio.wait_for(live.paste_events["Weiterer Satz im ersten Absatz."].wait(), 3)

    second, second_record, second_socket = await live.start()
    await live.emit(second, second_socket, "Zweiter Absatz.")
    second_stop = live.stop()
    await asyncio.wait_for(live.insertion_reached[second_record.id].wait(), 3)

    assert live.pasted == ["Erster Absatz.", "Weiterer Satz im ersten Absatz."]
    assert not first_socket.closed
    assert not second_stop.done()
    await live.emit(first, first_socket, "Spaeter finalisierter Nachsatz.")
    await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 5)
    assert live.pasted == [
        "Erster Absatz.",
        "Weiterer Satz im ersten Absatz.",
        "Spaeter finalisierter Nachsatz.",
        "Zweiter Absatz.",
    ]
    assert "Spaeter finalisierter Nachsatz." in first_record.content_text()
    assert second_record.content_text() == "Zweiter Absatz."
    assert first_socket.closed and second_socket.closed


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    raises=_CleanupBlocksInsertion,
    reason="Issue #47: insertion still waits for predecessor persistence/cleanup; no runtime fix yet",
)
async def test_complete_text_should_not_wait_for_predecessor_persistence(live, monkeypatch):
    first, first_record, first_socket = await live.start()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    live.cleanup_gates.append(release_cleanup)

    async def save(record):
        if record.id == first_record.id:
            cleanup_started.set()
            await release_cleanup.wait()

    monkeypatch.setattr(live.controller, "_save_transcript_to_db_async", save)
    await live.emit(first, first_socket, "Erster Absatz.")
    await asyncio.wait_for(live.paste_events["Erster Absatz."].wait(), 3)
    first_stop = live.stop()
    await asyncio.wait_for(cleanup_started.wait(), 3)
    assert first_socket.closed

    second, second_record, second_socket = await live.start()
    await live.emit(second, second_socket, "Zweiter Absatz.")
    second_stop = live.stop()
    await asyncio.wait_for(live.insertion_reached[second_record.id].wait(), 3)
    assert second_socket.closed
    try:
        try:
            await asyncio.wait_for(live.paste_events["Zweiter Absatz."].wait(), 0.5)
        except TimeoutError as exc:
            raise _CleanupBlocksInsertion from exc
        assert not release_cleanup.is_set()
        assert not first_stop.done()
    finally:
        release_cleanup.set()
        await asyncio.wait_for(asyncio.gather(first_stop, second_stop), 5)
