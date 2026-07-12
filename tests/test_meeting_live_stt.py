from __future__ import annotations

import asyncio
import json

import pytest

from src.meeting_live_stt import SonioxMeetingStream


_CLOSED = object()


class FakeWebSocket:
    def __init__(self, *, fail_binary_once: bool = False):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.fail_binary_once = fail_binary_once

    async def send(self, value):
        if isinstance(value, bytes) and self.fail_binary_once:
            self.fail_binary_once = False
            raise ConnectionError("synthetic network loss")
        self.sent.append(value)
        if value == "":
            await self.incoming.put(json.dumps({"tokens": [], "finished": True}))

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.incoming.put(_CLOSED)

    async def push(self, payload: dict):
        await self.incoming.put(json.dumps(payload))

    async def disconnect(self):
        await self.incoming.put(ConnectionError("synthetic receive loss"))

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is _CLOSED:
            raise StopAsyncIteration
        if isinstance(value, BaseException):
            raise value
        return value


class GatedFirstAudioWebSocket(FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.first_audio_started = asyncio.Event()
        self.release_first_audio = asyncio.Event()
        self._gated = False

    async def send(self, value):
        if isinstance(value, bytes) and not self._gated:
            self._gated = True
            self.first_audio_started.set()
            await self.release_first_audio.wait()
        await super().send(value)


async def _eventually(predicate, *, timeout: float = 1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _append(items, value):
    items.append(value)


class FakeTurnState:
    def __init__(self, name: str):
        self.name = name


class FakeTurnMetrics:
    probability = 0.42
    e2e_processing_time_ms = 12.5


class FakeSmartTurn:
    def __init__(self, states: list[str]):
        self.states = list(states)
        self.audio = []
        self.clear_count = 0

    def append_audio(self, pcm: bytes, is_speech: bool):
        self.audio.append((pcm, is_speech))

    async def analyze_end_of_turn(self):
        return FakeTurnState(self.states.pop(0)), FakeTurnMetrics()

    def clear(self):
        self.clear_count += 1


@pytest.mark.asyncio
async def test_soniox_meeting_stream_uses_stable_upserts_and_persists_final_turn():
    websocket = FakeWebSocket()
    segments = []
    gaps = []

    async def connect(_url):
        return websocket

    stream = SonioxMeetingStream(
        meeting_id="meeting-1",
        source="system",
        api_key="secret",
        model="stt-rt-v5",
        language="en",
        diarization=True,
        on_segment=lambda segment: _append(segments, segment),
        on_gap=lambda source, reason: _append(gaps, (source, reason)),
        connect_factory=connect,
        session_id="session-1",
        timeline_offset_ms=1_000,
    )
    await stream.start()
    stream.enqueue(b"\0\0" * 160)
    await websocket.push({
        "tokens": [
            {"text": "Hello", "is_final": False, "start_ms": 100, "end_ms": 300, "speaker": "1"}
        ]
    })
    await websocket.push({
        "tokens": [
            {"text": "Hello", "is_final": True, "start_ms": 100, "end_ms": 300, "speaker": "1"},
            {"text": " world", "is_final": True, "start_ms": 300, "end_ms": 500, "speaker": "1"},
            {"text": "<end>", "is_final": True},
        ]
    })
    await _eventually(lambda: any(segment.is_final for segment in segments))
    await stream.stop()

    config = json.loads(websocket.sent[0])
    assert config["model"] == "stt-rt-v5"
    assert config["enable_speaker_diarization"] is True
    assert config["api_key"] == "secret"
    assert any(value == b"\0\0" * 160 for value in websocket.sent)
    interim = next(segment for segment in segments if not segment.is_final)
    final = next(segment for segment in segments if segment.is_final)
    assert interim.id == final.id == "live-system-session--0"
    assert final.text == "Hello world"
    assert final.speaker_label == "Speaker 1"
    assert (final.start_ms, final.end_ms) == (1_100, 1_500)
    assert gaps == []


@pytest.mark.asyncio
async def test_soniox_meeting_stream_reconnects_after_send_failure_and_marks_one_gap():
    first = FakeWebSocket(fail_binary_once=True)
    second = FakeWebSocket()
    sockets = [first, second]
    segments = []
    gaps = []
    statuses = []

    async def connect(_url):
        return sockets.pop(0)

    stream = SonioxMeetingStream(
        meeting_id="meeting-2",
        source="microphone",
        api_key="secret",
        model="stt-rt-v5",
        language="en",
        diarization=False,
        on_segment=lambda segment: _append(segments, segment),
        on_gap=lambda source, reason: _append(gaps, (source, reason)),
        on_status=lambda source, status, count: _append(statuses, (source, status, count)),
        connect_factory=connect,
        reconnect_initial_delay_s=0.01,
        reconnect_max_delay_s=0.02,
    )
    await stream.start()
    stream.enqueue(b"\0\0" * 160)
    await _eventually(lambda: len(second.sent) >= 1)
    stream.enqueue(b"\0\0" * 160)
    await second.push({
        "tokens": [
            {"text": "Recovered", "is_final": True, "start_ms": 0, "end_ms": 10},
            {"text": "<end>", "is_final": True},
        ]
    })
    await _eventually(lambda: any(segment.is_final for segment in segments))
    await stream.stop()

    assert gaps == [("microphone", "live_stt_reconnect")]
    assert stream.reconnect_count == 1
    assert stream.reconnect_attempts == 1
    assert statuses == [
        ("microphone", "reconnecting", 1),
        ("microphone", "recovered", 1),
    ]
    assert segments[-1].text == "Recovered"
    assert segments[-1].speaker_label == "You"
    assert segments[-1].start_ms >= 10


@pytest.mark.asyncio
async def test_soniox_meeting_stream_retries_connect_without_duplicate_gap_markers():
    first = FakeWebSocket()
    recovered = FakeWebSocket()
    attempts = 0
    gaps = []

    async def connect(_url):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return first
        if attempts < 4:
            raise OSError("provider unavailable")
        return recovered

    stream = SonioxMeetingStream(
        meeting_id="meeting-3",
        source="system",
        api_key="secret",
        model="stt-rt-v5",
        language="auto",
        diarization=True,
        on_segment=lambda segment: _append([], segment),
        on_gap=lambda source, reason: _append(gaps, (source, reason)),
        connect_factory=connect,
        reconnect_initial_delay_s=0.01,
        reconnect_max_delay_s=0.02,
    )
    await stream.start()
    await first.disconnect()
    await _eventually(lambda: len(recovered.sent) >= 1)
    await stream.stop()

    assert attempts == 4
    assert stream.reconnect_attempts == 3
    assert stream.reconnect_count == 1
    assert gaps == [("system", "live_stt_reconnect")]


@pytest.mark.asyncio
async def test_soniox_meeting_stream_reports_live_queue_backpressure_immediately_once():
    gaps = []
    statuses = []
    stream = SonioxMeetingStream(
        meeting_id="meeting-4",
        source="system",
        api_key="secret",
        model="stt-rt-v5",
        language="en",
        diarization=True,
        on_segment=lambda segment: _append([], segment),
        on_gap=lambda source, reason: _append(gaps, (source, reason)),
        on_status=lambda source, status, count: _append(statuses, (source, status, count)),
        queue_frames=16,
    )

    for _ in range(40):
        stream.enqueue(b"\0\0" * 160)
    await _eventually(lambda: bool(statuses))
    await stream.stop()

    assert stream.dropped_frames == 24
    assert gaps == [("system", "live_stt_backpressure")]
    assert statuses == [("system", "degraded", 0)]


@pytest.mark.asyncio
async def test_live_timestamps_preserve_dropped_frame_gap():
    websocket = GatedFirstAudioWebSocket()
    segments = []

    async def connect(_url):
        return websocket

    stream = SonioxMeetingStream(
        meeting_id="meeting-backpressure-timeline",
        source="system",
        api_key="secret",
        model="stt-rt-v5",
        language="en",
        diarization=True,
        on_segment=lambda segment: _append(segments, segment),
        on_gap=lambda source, reason: _append([], (source, reason)),
        connect_factory=connect,
        queue_frames=16,
    )
    await stream.start()
    stream.enqueue(b"\0\0" * 160)
    await websocket.first_audio_started.wait()
    for _ in range(20):
        stream.enqueue(b"\0\0" * 160)
    assert stream.dropped_frames == 4
    websocket.release_first_audio.set()
    await _eventually(
        lambda: sum(isinstance(item, bytes) for item in websocket.sent) == 17
    )
    await websocket.push({
        "tokens": [
            {"text": "After gap", "is_final": True, "start_ms": 10, "end_ms": 30},
            {"text": "<end>", "is_final": True},
        ]
    })
    await _eventually(lambda: any(segment.is_final for segment in segments))
    await stream.stop()

    final = next(segment for segment in segments if segment.is_final)
    assert (final.start_ms, final.end_ms) == (50, 70)


@pytest.mark.asyncio
async def test_soniox_meeting_stream_reports_redacted_interim_latency_snapshot():
    websocket = FakeWebSocket()
    segments = []

    async def connect(_url):
        return websocket

    stream = SonioxMeetingStream(
        meeting_id="meeting-latency",
        source="system",
        api_key="secret",
        model="stt-rt-v5",
        language="en",
        diarization=True,
        on_segment=lambda segment: _append(segments, segment),
        on_gap=lambda source, reason: _append([], (source, reason)),
        connect_factory=connect,
    )
    await stream.start()
    for _ in range(100):
        stream.enqueue(b"\0\0" * 160)
    await websocket.push({
        "tokens": [
            {"text": "Measured", "is_final": False, "start_ms": 0, "end_ms": 100}
        ]
    })
    await _eventually(lambda: bool(segments))
    snapshot = stream.snapshot()
    await stream.stop()

    assert snapshot["interimLatencySampleCount"] == 1
    assert snapshot["interimLatencyP95Ms"] == 900
    assert snapshot["timelineCursorMs"] == 1000
    assert "secret" not in str(snapshot)
    assert "Measured" not in str(snapshot)


@pytest.mark.asyncio
async def test_smart_turn_merges_incomplete_provider_endpoints_for_microphone():
    websocket = FakeWebSocket()
    segments = []
    analyzer = FakeSmartTurn(["INCOMPLETE", "COMPLETE"])

    async def connect(_url):
        return websocket

    stream = SonioxMeetingStream(
        meeting_id="meeting-smart-turn",
        source="microphone",
        api_key="secret",
        model="stt-rt-v5",
        language="en",
        diarization=False,
        on_segment=lambda segment: _append(segments, segment),
        on_gap=lambda source, reason: _append([], (source, reason)),
        connect_factory=connect,
        session_id="smart-session",
        smart_turn_analyzer=analyzer,
    )
    await stream.start()
    stream.enqueue((2_000).to_bytes(2, "little", signed=True) * 160)
    await websocket.push({"tokens": [
        {"text": "I think we should", "is_final": True, "start_ms": 0, "end_ms": 600},
        {"text": "<end>", "is_final": True},
    ]})
    await _eventually(lambda: bool(segments))
    assert not any(segment.is_final for segment in segments)

    stream.enqueue((2_000).to_bytes(2, "little", signed=True) * 160)
    await websocket.push({"tokens": [
        {"text": " ship Friday.", "is_final": True, "start_ms": 700, "end_ms": 1_100},
        {"text": "<end>", "is_final": True},
    ]})
    await _eventually(lambda: any(segment.is_final for segment in segments))
    snapshot = stream.snapshot()
    await stream.stop()

    final = next(segment for segment in segments if segment.is_final)
    assert final.id == "live-microphone-smart-se-0"
    assert final.text == "I think we should ship Friday."
    assert analyzer.audio
    assert snapshot["smartTurn"] == {
        "enabled": True,
        "analyses": 2,
        "incompleteTurns": 1,
        "failures": 0,
        "lastProbability": 0.42,
        "lastLatencyMs": 12.5,
    }
