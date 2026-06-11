import threading
import types

import numpy as np
import pytest

import src.microphone as microphone
from src.microphone import RustPrototypeFrameSource
from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_FLAG_PREBUFFER,
    AudioFrameHeader,
    encode_audio_frame,
)


class _FakeInputStream:
    instances: list["_FakeInputStream"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.closed = False
        self.stop_calls = 0
        _FakeInputStream.instances.append(self)

    def start(self):
        self.active = True

    def stop(self):
        self.stop_calls += 1
        self.active = False

    def close(self):
        self.closed = True


class _FakeStreamHandle:
    def __init__(self) -> None:
        self.active = False
        self.closed = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True
        self.active = False


class _FakeRustFrameSource:
    engine = "rust-wasapi"
    name = "fake-rust-frame-source"

    def __init__(self) -> None:
        self.stream = _FakeStreamHandle()
        self.target_channels = 1
        self.capture_channels = 1
        self.fallback_reason = ""
        self.callback_count = 0
        self.open_calls = 0
        self.start_calls = 0
        self.stop_calls = 0

    def open(self, callback):
        self.callback = callback
        self.open_calls += 1
        return self

    def start(self) -> None:
        self.start_calls += 1
        self.stream.start()

    def stop(self, *, close: bool) -> None:
        self.stop_calls += 1
        self.stream.stop()
        if close:
            self.stream.close()

    def diagnostic_snapshot(self) -> dict:
        return {
            "engine": self.engine,
            "frameSource": self.name,
            "streamActive": self.stream.active,
        }

def test_rust_audio_default_without_favorite_uses_windows_default(monkeypatch):
    monkeypatch.setattr(microphone.Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(microphone.Config, "FAVORITE_MIC", "", raising=False)

    payload = microphone._rust_audio_device_selection_payload(
        "7",
        sample_rate=16000,
        channels=1,
    )

    assert payload["devicePreference"] == "default"
    assert payload["nativeEndpointIdHash"] is None
    assert payload["nativeEndpointMatchReason"] == "windowsDefaultEndpoint"


def test_rust_audio_selection_uses_shell_inventory_for_favorite_label(monkeypatch):
    commands: list[str] = []
    monkeypatch.setattr(microphone.Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(
        microphone.Config,
        "FAVORITE_MIC",
        "Mikrofon (4- Insta360 Link)",
        raising=False,
    )
    monkeypatch.setattr(microphone, "HAS_SOUNDDEVICE", True, raising=False)
    monkeypatch.setattr(microphone, "sd", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(microphone, "collect_native_capture_endpoint_inventory", lambda: [])
    monkeypatch.setattr(microphone, "build_input_endpoint_mappings", lambda *_args, **_kwargs: [])

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        assert command == "audioEndpointInventory"
        return {
            "success": True,
            "payload": {
                "endpoints": [
                    {
                        "endpointIdHash": "insta-hash",
                        "friendlyName": "Mikrofon (4- Insta360 Link)",
                        "flow": "capture",
                        "state": "active",
                        "isDefault": False,
                    }
                ]
            },
        }

    payload = microphone._rust_audio_device_selection_payload(
        "11",
        sample_rate=16000,
        channels=1,
        shell_call=shell_call,
    )

    assert commands == ["audioEndpointInventory"]
    assert payload["devicePreference"] == "11"
    assert payload["portAudioLabel"] == "Mikrofon (4- Insta360 Link)"
    assert payload["nativeEndpointIdHash"] == "insta-hash"
    assert payload["nativeEndpointMatchReason"] == "nativeInventoryLabel"


def test_rust_audio_selection_prefers_shell_inventory_hash(monkeypatch):
    commands: list[str] = []
    monkeypatch.setattr(microphone.Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(
        microphone.Config,
        "FAVORITE_MIC",
        "Mikrofon (4- Insta360 Link)",
        raising=False,
    )
    monkeypatch.setattr(microphone, "HAS_SOUNDDEVICE", True, raising=False)
    monkeypatch.setattr(microphone, "sd", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(
        microphone,
        "collect_native_capture_endpoint_inventory",
        lambda: [
            {
                "endpointIdHash": "python-local-hash",
                "friendlyName": "Mikrofon (4- Insta360 Link)",
                "flow": "capture",
                "state": "active",
            }
        ],
    )
    monkeypatch.setattr(microphone, "build_input_endpoint_mappings", lambda *_args, **_kwargs: [])

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        assert command == "audioEndpointInventory"
        return {
            "success": True,
            "payload": {
                "endpoints": [
                    {
                        "endpointIdHash": "rust-shell-hash",
                        "friendlyName": "Mikrofon (4- Insta360 Link)",
                        "flow": "capture",
                        "state": "active",
                    }
                ]
            },
        }

    payload = microphone._rust_audio_device_selection_payload(
        "11",
        sample_rate=16000,
        channels=1,
        shell_call=shell_call,
    )

    assert commands == ["audioEndpointInventory"]
    assert payload["nativeEndpointIdHash"] == "rust-shell-hash"


def test_rust_prototype_frame_source_honors_selection_device_preference(monkeypatch):
    commands: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-default",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "windows-default-hash",
                    "endpointSelection": {
                        "mode": "default",
                        "usedDefaultEndpoint": True,
                        "requestedNativeEndpointIdHash": None,
                        "selectedNativeEndpointIdHash": "windows-default-hash",
                    },
                },
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="7",
        shell_call=shell_call,
    )

    source.open(lambda *_args: None)
    snapshot = source.diagnostic_snapshot()

    assert commands[0][1]["devicePreference"] == "default"
    assert commands[0][1]["nativeEndpointIdHash"] is None
    assert snapshot["nativeEndpointIdHash"] == "windows-default-hash"
    assert snapshot["endpointSelection"]["mode"] == "default"
    assert snapshot["endpointSelection"]["usedDefaultEndpoint"] is True


def test_rust_prototype_frame_source_passes_shell_inventory_hash(monkeypatch):
    audio = np.full((512, 1), 321, dtype=np.int16)
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(audio.tobytes()),
            sequence=0,
            timestamp_micros=123_456,
            frame_count=512,
            channels=1,
        ),
        audio.tobytes(),
    )
    commands: list[tuple[str, dict]] = []
    monkeypatch.setattr(microphone.Config, "MIC_DEVICE", "default", raising=False)
    monkeypatch.setattr(
        microphone.Config,
        "FAVORITE_MIC",
        "Mikrofon (4- Insta360 Link)",
        raising=False,
    )
    monkeypatch.setattr(microphone, "HAS_SOUNDDEVICE", True, raising=False)
    monkeypatch.setattr(microphone, "sd", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(microphone, "collect_native_capture_endpoint_inventory", lambda: [])
    monkeypatch.setattr(microphone, "build_input_endpoint_mappings", lambda *_args, **_kwargs: [])

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioEndpointInventory":
            return {
                "success": True,
                "payload": {
                    "endpoints": [
                        {
                            "endpointIdHash": "insta-hash",
                            "friendlyName": "Mikrofon (4- Insta360 Link)",
                            "flow": "capture",
                            "state": "active",
                            "isDefault": False,
                        }
                    ]
                },
            }
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-insta",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "insta-hash",
                    "endpointSelection": {
                        "mode": "nativeEndpointHash",
                        "usedDefaultEndpoint": False,
                        "requestedNativeEndpointIdHash": "insta-hash",
                        "selectedNativeEndpointIdHash": "insta-hash",
                    },
                },
            }
        raise AssertionError(command)

    def reader_factory(_path, *_args, **_kwargs):
        import io

        return io.BytesIO(frame)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="11",
        shell_call=shell_call,
        reader_factory=reader_factory,
        first_frame_timeout_seconds=1.0,
    )

    source.open(lambda *_args: None)
    source.stop(close=True)

    assert commands[0][0] == "audioEndpointInventory"
    assert commands[1][0] == "audioCaptureStart"
    assert commands[1][1]["devicePreference"] == "11"
    assert commands[1][1]["portAudioLabel"] == "Mikrofon (4- Insta360 Link)"
    assert commands[1][1]["nativeEndpointIdHash"] == "insta-hash"


def test_rust_prototype_frame_source_reads_binary_frame_pipe(monkeypatch):
    audio = np.full((512, 1), 321, dtype=np.int16)
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(audio.tobytes()),
            sequence=0,
            timestamp_micros=123_456,
            frame_count=512,
            channels=1,
        ),
        audio.tobytes(),
    )
    calls: list[tuple[np.ndarray, int, dict, object]] = []
    commands: list[tuple[str, dict]] = []
    monkeypatch.setattr(microphone.Config, "MIC_PREBUFFER_MS", 400, raising=False)
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-1",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "endpoint-hash",
                    "sidecarPid": 1234,
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "captureStop",
                    "connected": True,
                    "framesWritten": 3,
                    "prebufferFramesWritten": 1,
                    "liveFramesWritten": 2,
                    "bytesWritten": 3072,
                    "writerError": None,
                    "sidecarUptimeMs": 55,
                    "exitStatus": 0,
                    "sidecarKilledAfterTimeout": False,
                    "sidecarWaitError": None,
                },
            }
        raise AssertionError(command)

    def reader_factory(_path, *_args, **_kwargs):
        import io

        return io.BytesIO(frame)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="default",
        shell_call=shell_call,
        reader_factory=reader_factory,
        first_frame_timeout_seconds=1.0,
        prewarm_id="prewarm-1",
    )

    source.open(lambda *args: calls.append(args))
    source.start()
    active_snapshot = source.diagnostic_snapshot()
    source.stop(close=True)

    assert commands[0][0] == "audioCaptureStart"
    assert commands[0][1]["frameProtocol"]["sampleFormat"] == "pcm_i16_le"
    assert commands[0][1]["portAudioLabel"] == "Default Mic, Windows WASAPI"
    assert commands[0][1]["nativeEndpointIdHash"] == "endpoint-hash"
    assert commands[0][1]["prebufferMs"] == 400
    assert commands[0][1]["prewarmId"] == "prewarm-1"
    assert commands[-1][0] == "audioCaptureStop"
    assert source.engine == "rust-wasapi"
    assert source.name == "rust-frame-pipe"
    assert source.callback_count == 1
    assert calls[0][1] == 512
    np.testing.assert_array_equal(calls[0][0], audio)
    assert active_snapshot["framePipeHash"]
    snapshot = source.diagnostic_snapshot()
    assert snapshot["requestedPrebufferMs"] == 400
    assert snapshot["requestedPrewarmIdHash"]
    assert snapshot["framePipeHash"] is None
    assert snapshot["nativeEndpointIdHash"] == "endpoint-hash"
    assert snapshot["sidecarExitStatus"] == 0
    assert snapshot["sidecarConnected"] is True
    assert snapshot["sidecarFramesWritten"] == 3
    assert snapshot["sidecarPrebufferFramesWritten"] == 1
    assert snapshot["sidecarLiveFramesWritten"] == 2
    assert snapshot["sidecarBytesWritten"] == 3072
    assert snapshot["sidecarUptimeMs"] == 55
    assert snapshot["sidecarKilledAfterTimeout"] is False
    assert snapshot["sidecarWaitError"] is None
    assert snapshot["sidecarStopReason"] == "captureStop"
    assert snapshot["framePipeFramesRead"] == 1
    assert snapshot["framePipeAudioFramesRead"] == 512
    assert snapshot["framePipePayloadBytesRead"] == len(audio.tobytes())
    assert snapshot["framePipeTotalBytesRead"] == len(frame)
    assert snapshot["framePipeSequenceErrorCount"] == 0
    assert snapshot["framePipeProtocolErrorCount"] == 0
    assert snapshot["framePipeLastSequence"] == 0
    assert snapshot["framePipeLastTimestampMicros"] == 123_456
    assert snapshot["framePipeLastFlags"] == 0
    assert snapshot["framePipeReaderEndReason"] in {"pipeClosed", "stopRequested"}
    assert snapshot["framePipeFirstFrameReadMs"] is not None
    assert "framePipe" not in snapshot


def test_rust_prototype_frame_source_reports_sequence_error_before_first_frame(monkeypatch):
    audio = np.zeros((16, 1), dtype=np.int16)
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(audio.tobytes()),
            sequence=1,
            timestamp_micros=1,
            frame_count=16,
            channels=1,
        ),
        audio.tobytes(),
    )
    commands: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-1",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "endpoint-hash",
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "protocolError",
                    "connected": True,
                    "framesWritten": 1,
                    "bytesWritten": len(frame),
                    "writerError": None,
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    def reader_factory(_path, *_args, **_kwargs):
        import io

        return io.BytesIO(frame)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=reader_factory,
        first_frame_timeout_seconds=1.0,
    )

    source.open(lambda *_args: None)
    with pytest.raises(RuntimeError, match="failed before first frame"):
        source.start()

    snapshot = source.diagnostic_snapshot()
    assert commands[-1][0] == "audioCaptureStop"
    assert snapshot["callbackCount"] == 0
    assert snapshot["framePipeFramesRead"] == 0
    assert snapshot["framePipePayloadBytesRead"] == len(audio.tobytes())
    assert snapshot["framePipeTotalBytesRead"] == len(frame)
    assert snapshot["framePipeSequenceErrorCount"] == 1
    assert snapshot["framePipeProtocolErrorCount"] == 0
    assert snapshot["framePipeReaderEndReason"] == "protocolError"
    assert snapshot["fallbackReason"] == "rustFramePipeSequenceError"
    assert "sequence out of order" in snapshot["lastError"]


def test_rust_prototype_frame_source_records_pipe_closed_mid_session_failure(monkeypatch):
    audio = np.zeros((16, 1), dtype=np.int16)
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(audio.tobytes()),
            sequence=0,
            timestamp_micros=1,
            frame_count=16,
            channels=1,
        ),
        audio.tobytes(),
    )
    got_frame = threading.Event()
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-mid-session",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "endpoint-hash",
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "captureStop",
                    "connected": False,
                    "framesWritten": 1,
                    "bytesWritten": len(frame),
                    "writerError": "pipe closed",
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    def reader_factory(_path, *_args, **_kwargs):
        import io

        return io.BytesIO(frame)

    def callback(*_args):
        got_frame.set()

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=reader_factory,
        first_frame_timeout_seconds=1.0,
    )

    source.open(callback)
    source.start()
    assert got_frame.wait(1.0)
    if source._reader_thread is not None:
        source._reader_thread.join(timeout=1.0)
    snapshot = source.diagnostic_snapshot()
    source.stop(close=True)

    assert snapshot["callbackCount"] == 1
    assert snapshot["framePipeReaderEndReason"] == "pipeClosed"
    assert snapshot["midSessionFailureReason"] == "pipeClosed"


def test_rust_prototype_frame_source_tracks_prebuffer_before_live_frames(monkeypatch):
    prebuffer_audio = np.full((16, 1), 100, dtype=np.int16)
    live_audio = np.full((16, 1), 200, dtype=np.int16)
    frames = b"".join(
        [
            encode_audio_frame(
                AudioFrameHeader(
                    payload_len=len(prebuffer_audio.tobytes()),
                    sequence=0,
                    timestamp_micros=1,
                    frame_count=16,
                    channels=1,
                    flags=AUDIO_FRAME_FLAG_PREBUFFER,
                ),
                prebuffer_audio.tobytes(),
            ),
            encode_audio_frame(
                AudioFrameHeader(
                    payload_len=len(live_audio.tobytes()),
                    sequence=1,
                    timestamp_micros=2,
                    frame_count=16,
                    channels=1,
                ),
                live_audio.tobytes(),
            ),
        ]
    )
    calls: list[tuple[np.ndarray, int, dict, object]] = []
    got_two_frames = threading.Event()
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-prebuffer",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "endpoint-hash",
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "captureStop",
                    "connected": True,
                    "framesWritten": 2,
                    "prebufferFramesWritten": 1,
                    "liveFramesWritten": 1,
                    "bytesWritten": len(frames),
                    "writerError": None,
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    def reader_factory(_path, *_args, **_kwargs):
        import io

        return io.BytesIO(frames)

    def callback(*args):
        calls.append(args)
        if len(calls) >= 2:
            got_two_frames.set()

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=reader_factory,
        first_frame_timeout_seconds=1.0,
    )

    source.open(callback)
    source.start()
    assert got_two_frames.wait(1.0)
    source.stop(close=True)

    snapshot = source.diagnostic_snapshot()
    assert snapshot["callbackCount"] == 2
    assert snapshot["framePipeFramesRead"] == 2
    assert snapshot["framePipePrebufferFramesRead"] == 1
    assert snapshot["framePipePrebufferAudioFramesRead"] == 16
    assert snapshot["framePipeLiveFramesRead"] == 1
    assert snapshot["framePipeLiveAudioFramesRead"] == 16
    assert snapshot["framePipePrebufferAfterLiveCount"] == 0
    assert snapshot["framePipeFirstLiveSequence"] == 1
    assert snapshot["sidecarPrebufferFramesWritten"] == 1
    assert snapshot["sidecarLiveFramesWritten"] == 1
    np.testing.assert_array_equal(calls[0][0], prebuffer_audio)
    np.testing.assert_array_equal(calls[1][0], live_audio)


def test_rust_prototype_frame_source_rejects_prebuffer_after_live_frame(monkeypatch):
    live_audio = np.full((16, 1), 200, dtype=np.int16)
    late_prebuffer_audio = np.full((16, 1), 100, dtype=np.int16)
    frames = b"".join(
        [
            encode_audio_frame(
                AudioFrameHeader(
                    payload_len=len(live_audio.tobytes()),
                    sequence=0,
                    timestamp_micros=1,
                    frame_count=16,
                    channels=1,
                ),
                live_audio.tobytes(),
            ),
            encode_audio_frame(
                AudioFrameHeader(
                    payload_len=len(late_prebuffer_audio.tobytes()),
                    sequence=1,
                    timestamp_micros=2,
                    frame_count=16,
                    channels=1,
                    flags=AUDIO_FRAME_FLAG_PREBUFFER,
                ),
                late_prebuffer_audio.tobytes(),
            ),
        ]
    )
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-interleaved",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "endpoint-hash",
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "protocolError",
                    "connected": True,
                    "framesWritten": 2,
                    "bytesWritten": len(frames),
                    "writerError": None,
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    def reader_factory(_path, *_args, **_kwargs):
        import io

        return io.BytesIO(frames)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=reader_factory,
        first_frame_timeout_seconds=1.0,
    )

    source.open(lambda *_args: None)
    source.start()
    if source._reader_thread is not None:
        source._reader_thread.join(timeout=1.0)
    snapshot = source.diagnostic_snapshot()
    source.stop(close=True)

    assert snapshot["callbackCount"] == 1
    assert snapshot["framePipeLiveFramesRead"] == 1
    assert snapshot["framePipePrebufferFramesRead"] == 0
    assert snapshot["framePipePrebufferAfterLiveCount"] == 1
    assert snapshot["framePipeProtocolErrorCount"] == 1
    assert snapshot["framePipeReaderEndReason"] == "protocolError"
    assert snapshot["fallbackReason"] == "rustFramePipePrebufferInterleaving"
    assert "prebuffer frame arrived after live frame" in snapshot["lastError"]


def test_rust_prototype_frame_source_can_reopen_after_watchdog_pause(monkeypatch):
    audio = np.zeros((16, 1), dtype=np.int16)
    frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(audio.tobytes()),
            sequence=0,
            timestamp_micros=1,
            frame_count=16,
            channels=1,
        ),
        audio.tobytes(),
    )
    commands: list[tuple[str, dict]] = []
    starts = 0
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        nonlocal starts
        commands.append((command, payload or {}))
        if command == "audioCaptureStart":
            starts += 1
            return {
                "success": True,
                "payload": {
                    "streamId": f"stream-{starts}",
                    "framePipe": f"memory-pipe-{starts}",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "endpoint-hash",
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "watchdog",
                    "connected": True,
                    "framesWritten": 1,
                    "bytesWritten": len(frame),
                    "writerError": None,
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    def reader_factory(_path, *_args, **_kwargs):
        import io

        return io.BytesIO(frame)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=reader_factory,
        first_frame_timeout_seconds=1.0,
    )
    source.open(lambda *_args: None)
    source.start()
    source.stop(close=False)
    source.start()
    source.stop(close=True)

    assert [command for command, _payload in commands].count("audioCaptureStart") == 2
    assert [command for command, _payload in commands].count("audioCaptureStop") == 2
    snapshot = source.diagnostic_snapshot()
    assert snapshot["sidecarStartCount"] == 2
    assert snapshot["sidecarRestartCount"] == 1
    assert snapshot["sidecarExitStatus"] == 0


def test_rust_audio_device_selection_payload_maps_portaudio_index_to_native_hash(monkeypatch):
    devices = [
        {"name": "Built-in Mic, Windows WASAPI", "max_input_channels": 1, "hostapi": 1},
        {"name": "Dock Mic, Windows WASAPI", "max_input_channels": 2, "hostapi": 1},
    ]
    fake_sd = types.SimpleNamespace(
        default=types.SimpleNamespace(device=(0, None), hostapi=1),
        query_hostapis=lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}],
        query_devices=lambda device=None, kind=None: (
            devices
            if device is None and kind is None
            else devices[0 if device is None else int(device)]
        ),
        check_input_settings=lambda **_kwargs: None,
    )
    monkeypatch.setattr(microphone, "HAS_SOUNDDEVICE", True)
    monkeypatch.setattr(microphone, "sd", fake_sd)
    monkeypatch.setattr(
        microphone,
        "collect_native_capture_endpoint_inventory",
        lambda: [
            {
                "endpointId": r"SWD\MMDEVAPI\{0.0.1.00000000}.{built-in}",
                "friendlyName": "Built-in Mic",
                "flow": "capture",
                "isDefault": True,
            },
            {
                "endpointId": r"SWD\MMDEVAPI\{0.0.1.00000000}.{dock}",
                "friendlyName": "Dock Mic",
                "flow": "capture",
                "isDefault": False,
            },
        ],
    )

    payload = microphone._rust_audio_device_selection_payload(
        "1",
        sample_rate=16000,
        channels=1,
    )

    assert payload["portAudioLabel"] == "Dock Mic, Windows WASAPI"
    assert payload["nativeEndpointIdHash"]
    assert payload["nativeEndpointMatchConfidence"] == "name"
    assert "SWD" not in str(payload)


@pytest.mark.asyncio
async def test_microphone_input_raises_when_rust_capture_unavailable(monkeypatch):
    _FakeInputStream.instances.clear()
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    monkeypatch.setattr(
        microphone,
        "call_shell_ipc",
        lambda command, payload=None, **_kwargs: {
            "success": False,
            "errorCode": "audioCaptureUnavailable",
            "fallbackReason": "Rust audio capture sidecar is not implemented",
            "payload": {},
        },
    )

    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue

    with pytest.raises(RuntimeError, match="Rust audio capture start failed"):
        await mic.start(microphone.StartFrame())

    snapshot = mic.diagnostic_snapshot()
    assert snapshot["requestedEngine"] == "rust-wasapi"
    assert snapshot["engine"] != "python"
    assert snapshot["frameSource"] != "sounddevice"
    assert snapshot["engineFallbackReason"].startswith("rustCaptureFailed:")
    assert _FakeInputStream.instances == []


@pytest.mark.asyncio
async def test_rust_prototype_does_not_adopt_python_prewarm_when_always_on(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    monkeypatch.setattr(microphone, "HAS_SOUNDDEVICE", True)
    fake_source = _FakeRustFrameSource()

    class FakePrewarmManager:
        def __init__(self) -> None:
            self.attach_calls = 0
            self.pause_calls = 0

        def attach_active_capture(self, *_args, **_kwargs):
            self.attach_calls += 1
            raise AssertionError("Rust WASAPI must not adopt Python prewarm")

        def pause_for_active_capture(self) -> None:
            self.pause_calls += 1

    prewarm = FakePrewarmManager()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=prewarm,
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source

    await mic.start(microphone.StartFrame())
    snapshot = mic.diagnostic_snapshot()
    await mic.stop(microphone.EndFrame())

    assert prewarm.attach_calls == 0
    assert prewarm.pause_calls == 1
    assert fake_source.open_calls == 1
    assert fake_source.start_calls == 1
    assert snapshot["requestedEngine"] == "rust-wasapi"
    assert snapshot["engine"] == "rust-wasapi"
    assert snapshot["usingPrewarmStream"] is False
    assert snapshot["prewarmAdoptionSkippedReason"] == "engine:rust-wasapi"


@pytest.mark.asyncio
async def test_rust_prototype_adopts_rust_prewarm_id_when_always_on(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    monkeypatch.setattr(microphone, "HAS_SOUNDDEVICE", True)
    fake_source = _FakeRustFrameSource()
    created_prewarm_ids: list[str] = []

    class FakeRustPrewarmManager:
        engine = "rust-wasapi"

        def __init__(self) -> None:
            self.attach_calls = 0
            self.pause_calls = 0

        def attach_active_capture(self, *_args, **_kwargs):
            self.attach_calls += 1
            return {
                "engine": "rust-wasapi",
                "prewarmId": "prewarm-rust-1",
                "signature": {
                    "sample_rate": 16000,
                    "target_channels": 1,
                    "block_size": 512,
                },
            }

        def pause_for_active_capture(self) -> None:
            self.pause_calls += 1

    prewarm = FakeRustPrewarmManager()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=prewarm,
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue

    def create_frame_source():
        created_prewarm_ids.append(mic._rust_prewarm_id)
        return fake_source

    mic._create_frame_source = create_frame_source

    await mic.start(microphone.StartFrame())
    snapshot = mic.diagnostic_snapshot()
    await mic.stop(microphone.EndFrame())

    assert prewarm.attach_calls == 1
    assert prewarm.pause_calls == 0
    assert created_prewarm_ids == ["prewarm-rust-1"]
    assert snapshot["requestedEngine"] == "rust-wasapi"
    assert snapshot["engine"] == "rust-wasapi"
    assert snapshot["usingPrewarmStream"] is False
    assert snapshot["prewarmAdoptionSkippedReason"] == ""
    assert snapshot["rustPrewarmAdoption"]["adopted"] is True
    assert snapshot["rustPrewarmAdoption"]["prewarmIdHash"]
    assert "prewarm-rust-1" not in str(snapshot)
