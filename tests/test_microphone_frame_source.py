import asyncio
import threading
import types
import hashlib

import numpy as np
import pytest

import src.microphone as microphone
from src.microphone import RustCaptureWavArtifact, RustPrototypeFrameSource
from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_FLAG_END_OF_STREAM,
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


def _write_test_pcm16_wav(path, pcm: bytes = b"\x01\x00\x02\x00") -> None:
    header = bytearray(44)
    header[0:4] = b"RIFF"
    header[4:8] = (len(pcm) + 36).to_bytes(4, "little")
    header[8:12] = b"WAVE"
    header[12:16] = b"fmt "
    header[16:20] = (16).to_bytes(4, "little")
    header[20:22] = (1).to_bytes(2, "little")
    header[22:24] = (1).to_bytes(2, "little")
    header[24:28] = (16_000).to_bytes(4, "little")
    header[28:32] = (32_000).to_bytes(4, "little")
    header[32:34] = (2).to_bytes(2, "little")
    header[34:36] = (16).to_bytes(2, "little")
    header[36:40] = b"data"
    header[40:44] = len(pcm).to_bytes(4, "little")
    path.write_bytes(bytes(header) + pcm)


def test_rust_capture_wav_artifact_validates_opens_and_releases(tmp_path):
    lease_id = "123456781234423481234567890abcde"
    path = tmp_path / f"{lease_id}.wav"
    _write_test_pcm16_wav(path)
    calls = []

    def shell_call(command, payload, **_kwargs):
        calls.append((command, payload))
        return {
            "success": True,
            "payload": {"released": True},
        }

    artifact = RustCaptureWavArtifact(
        {
            "schemaVersion": "1",
            "leaseId": lease_id,
            "path": str(path),
            "format": "wav_pcm16",
            "contentType": "audio/wav",
            "owner": "pythonBackendLease",
            "cleanupCommand": "audioCaptureArtifactRelease",
            "byteLength": 48,
            "pcmBytes": 4,
            "sampleRate": 16_000,
            "channels": 1,
            "bitsPerSample": 16,
        },
        shell_call=shell_call,
    )

    assert artifact.matches_pcm(sample_rate=16_000, channels=1, pcm_bytes=4)
    with artifact.open() as wav:
        assert wav.read() == path.read_bytes()
    snapshot = artifact.diagnostic_snapshot()
    assert "path" not in snapshot
    assert lease_id not in str(snapshot)
    assert artifact.release() is True
    assert artifact.released is True
    assert calls == [
        ("audioCaptureArtifactRelease", {"leaseId": lease_id})
    ]


@pytest.mark.asyncio
async def test_rust_capture_wav_artifact_release_async_joins_shell_ack_before_propagating_cancellation(
    tmp_path,
):
    lease_id = "133456781234423481234567890abcde"
    path = tmp_path / f"{lease_id}.wav"
    _write_test_pcm16_wav(path)
    release_started = threading.Event()
    allow_release = threading.Event()
    calls = []

    def shell_call(command, payload, **_kwargs):
        calls.append((command, payload))
        release_started.set()
        if not allow_release.wait(timeout=2.0):
            raise TimeoutError("test did not release shell acknowledgement")
        return {
            "success": True,
            "payload": {"released": True},
        }

    artifact = RustCaptureWavArtifact(
        {
            "schemaVersion": "1",
            "leaseId": lease_id,
            "path": str(path),
            "format": "wav_pcm16",
            "contentType": "audio/wav",
            "owner": "pythonBackendLease",
            "cleanupCommand": "audioCaptureArtifactRelease",
            "byteLength": 48,
            "pcmBytes": 4,
            "sampleRate": 16_000,
            "channels": 1,
            "bitsPerSample": 16,
        },
        shell_call=shell_call,
    )

    release_task = asyncio.create_task(artifact.release_async())
    try:
        assert await asyncio.to_thread(release_started.wait, 1.0)
        release_task.cancel()
        await asyncio.sleep(0)
        assert release_task.done() is False
    finally:
        allow_release.set()

    with pytest.raises(asyncio.CancelledError):
        await release_task

    assert artifact.released is True
    assert calls == [
        ("audioCaptureArtifactRelease", {"leaseId": lease_id})
    ]


def test_rust_capture_wav_artifact_rejects_inconsistent_pcm_header(tmp_path):
    lease_id = "223456781234423481234567890abcde"
    path = tmp_path / f"{lease_id}.wav"
    _write_test_pcm16_wav(path)
    bytes_ = bytearray(path.read_bytes())
    bytes_[28:32] = (31_998).to_bytes(4, "little")
    path.write_bytes(bytes_)
    artifact = RustCaptureWavArtifact(
        {
            "schemaVersion": "1",
            "leaseId": lease_id,
            "path": str(path),
            "format": "wav_pcm16",
            "contentType": "audio/wav",
            "owner": "pythonBackendLease",
            "cleanupCommand": "audioCaptureArtifactRelease",
            "byteLength": 48,
            "pcmBytes": 4,
            "sampleRate": 16_000,
            "channels": 1,
            "bitsPerSample": 16,
        },
        shell_call=lambda *_args, **_kwargs: {
            "success": True,
            "payload": {"released": True},
        },
    )

    with pytest.raises(RuntimeError, match="header is invalid"):
        artifact.open()


def test_invalid_python_artifact_contract_releases_tauri_lease():
    lease_id = "323456781234423481234567890abcde"
    calls = []

    def shell_call(command, payload=None, **_kwargs):
        calls.append((command, payload or {}))
        return {"success": True, "payload": {"released": True}}

    source = RustPrototypeFrameSource(
        sample_rate=16_000,
        target_channels=1,
        block_size=512,
        device="default",
        shell_call=shell_call,
    )
    source._record_sidecar_stop(
        {
            "captureArtifact": {
                "schemaVersion": "1",
                "leaseId": lease_id,
                "path": f"C:\\Temp\\{lease_id}.wav",
                "format": "wav_pcm16",
                "contentType": "audio/wav",
                "owner": "invalid-owner",
                "cleanupCommand": "audioCaptureArtifactRelease",
                "byteLength": 48,
                "pcmBytes": 4,
                "sampleRate": 16_000,
                "channels": 1,
                "bitsPerSample": 16,
            }
        }
    )

    assert source.take_capture_wav_artifact() is None
    assert calls == [
        ("audioCaptureArtifactRelease", {"leaseId": lease_id})
    ]

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
    preparation = {
        "schemaVersion": "1",
        "format": "wav_pcm16",
        "implementation": "wav_pcm16_file_v1",
        "sampleRate": 16_000,
        "channels": 1,
        "bitsPerSample": 16,
        "queueCapacityFrames": 64,
        "maxPcmBytes": 64 * 1024 * 1024,
    }
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
        audio_preparation=preparation,
    )

    source.open(lambda *_args: None)
    snapshot = source.diagnostic_snapshot()

    assert commands[0][1]["devicePreference"] == "default"
    assert commands[0][1]["nativeEndpointIdHash"] is None
    assert commands[0][1]["audioPreparation"] == preparation
    assert snapshot["nativeEndpointIdHash"] == "windows-default-hash"
    assert snapshot["endpointSelection"]["mode"] == "default"
    assert snapshot["endpointSelection"]["usedDefaultEndpoint"] is True


def test_rust_prototype_frame_source_reuses_leased_prewarm_route(monkeypatch):
    commands: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("leased prewarm route must skip fresh device inventory")
        ),
    )

    def shell_call(command, payload=None, **_kwargs):
        commands.append((command, payload or {}))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-warm-route",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "warm-endpoint-hash",
                },
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="17",
        prewarm_id="prewarm-warm-route",
        capture_route={
            "devicePreference": "17",
            "portAudioLabel": "Warm Mic, Windows WASAPI",
            "nativeEndpointIdHash": "warm-endpoint-hash",
        },
        shell_call=shell_call,
    )

    source.open(lambda *_args: None)

    assert [command for command, _payload in commands] == ["audioCaptureStart"]
    assert commands[0][1]["devicePreference"] == "17"
    assert commands[0][1]["portAudioLabel"] == "Warm Mic, Windows WASAPI"
    assert commands[0][1]["nativeEndpointIdHash"] == "warm-endpoint-hash"
    assert commands[0][1]["prewarmId"] == "prewarm-warm-route"
    snapshot = source.diagnostic_snapshot()
    assert snapshot["captureRouteSource"] == "prewarmLease"
    assert snapshot["deviceSelectionMs"] is not None


@pytest.mark.parametrize(
    ("response_override", "error_match"),
    [
        ({"sampleRate": 48000}, "sample rate"),
        ({"sampleFormat": "float32"}, "sample format"),
        ({"framePipe": ""}, "frame pipe"),
        (
            {
                "audioFrameProtocol": {
                    "magic": "SAF1",
                    "version": 1,
                    "headerBytes": microphone.AUDIO_FRAME_HEADER_LEN,
                    "sampleFormat": "pcm_i16_le",
                    "zeroLengthEndOfStream": False,
                }
            },
            "frame protocol",
        ),
    ],
)
def test_rust_frame_source_stops_started_sidecar_on_contract_mismatch(
    monkeypatch,
    response_override,
    error_match,
):
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
            response_payload = {
                "streamId": "stream-contract-mismatch",
                "framePipe": "memory-pipe",
                "sampleRate": 16000,
                "channels": 1,
                "captureChannels": 1,
                "sampleFormat": "pcm_i16_le",
                **response_override,
            }
            return {"success": True, "payload": response_payload}
        if command == "audioCaptureStop":
            return {"success": True, "payload": {"stopped": True}}
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="default",
        shell_call=shell_call,
    )

    with pytest.raises(RuntimeError, match=error_match):
        source.open(lambda *_args: None)

    assert [command for command, _payload in commands] == [
        "audioCaptureStart",
        "audioCaptureStop",
    ]
    assert commands[-1][1] == {"streamId": "stream-contract-mismatch"}
    assert source.stream_id == ""
    assert source.diagnostic_snapshot()["framePipeHash"] is None


def test_rust_frame_source_retries_unconfirmed_sidecar_stop():
    calls: list[tuple[str, dict, float]] = []

    def shell_call(command, payload, *, timeout_seconds):
        calls.append((command, payload, timeout_seconds))
        if len(calls) == 1:
            return {
                "success": False,
                "errorCode": "transportError",
                "fallbackReason": "audio shell IPC timed out",
                "payload": {},
            }
        return {
            "success": True,
            "payload": {"stopped": True, "reason": "stopped"},
        }

    source = RustPrototypeFrameSource(
        sample_rate=16_000,
        target_channels=1,
        block_size=512,
        device=None,
        shell_call=shell_call,
    )
    source.stream_id = "stream-retry-stop"
    source._frame_pipe = r"\\.\pipe\private-test-pipe"
    source._frame_pipe_hash = "redacted-hash"

    source.stop(close=True)

    assert [call[0] for call in calls] == ["audioCaptureStop", "audioCaptureStop"]
    assert [call[2] for call in calls] == [0.75, 2.0]
    assert source.stream_id == ""
    assert source._pending_stop_stream_id == ""
    assert source.diagnostic_snapshot()["sidecarStopConfirmed"] is True


def test_rust_frame_source_retains_redacted_owner_after_unconfirmed_stop():
    calls: list[tuple[str, dict, float]] = []

    def shell_call(command, payload, *, timeout_seconds):
        calls.append((command, payload, timeout_seconds))
        return {
            "success": False,
            "errorCode": "transportError",
            "fallbackReason": "audio shell IPC timed out",
            "payload": {},
        }

    source = RustPrototypeFrameSource(
        sample_rate=16_000,
        target_channels=1,
        block_size=512,
        device=None,
        shell_call=shell_call,
    )
    source.stream_id = "stream-deferred-stop"
    source._frame_pipe = r"\\.\pipe\private-test-pipe"
    source._frame_pipe_hash = "redacted-hash"

    source.stop(close=False)

    snapshot = source.diagnostic_snapshot()
    assert len(calls) == 2
    assert source.stream_id == ""
    assert source._pending_stop_stream_id == "stream-deferred-stop"
    assert snapshot["pendingStopStreamIdHash"]
    assert "stream-deferred-stop" not in str(snapshot)
    assert snapshot["sidecarStopConfirmed"] is False
    assert snapshot["framePipeHash"] is None


def test_rust_frame_source_accepts_no_active_retry_only_after_observed_v2_eos():
    calls: list[str] = []
    source = None

    def shell_call(command, _payload, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return {
                "success": False,
                "errorCode": "transportError",
                "fallbackReason": "audio shell IPC timed out",
                "payload": {},
            }
        source._end_of_stream_event.set()
        return {
            "success": True,
            "payload": {"stopped": False, "reason": "noActiveCapture"},
        }

    source = RustPrototypeFrameSource(
        sample_rate=16_000,
        target_channels=1,
        block_size=512,
        device=None,
        shell_call=shell_call,
    )
    source.stream_id = "stream-reconciled-eos"
    source._requires_terminal_eos = True
    source._end_of_stream_wait_seconds = 0.01

    source.stop(close=True)

    assert calls == ["audioCaptureStop", "audioCaptureStop"]
    assert source.diagnostic_snapshot()["terminalIntegrityError"] == ""


def test_rust_frame_source_rejects_no_active_retry_without_observed_v2_eos():
    calls = 0

    def shell_call(_command, _payload, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "success": False,
                "errorCode": "transportError",
                "fallbackReason": "audio shell IPC timed out",
                "payload": {},
            }
        return {
            "success": True,
            "payload": {"stopped": False, "reason": "noActiveCapture"},
        }

    source = RustPrototypeFrameSource(
        sample_rate=16_000,
        target_channels=1,
        block_size=512,
        device=None,
        shell_call=shell_call,
    )
    source.stream_id = "stream-missing-eos"
    source._requires_terminal_eos = True
    source._end_of_stream_wait_seconds = 0.01

    with pytest.raises(RuntimeError, match="rustFramePipeMissingEndOfStream"):
        source.stop(close=True)

    snapshot = source.diagnostic_snapshot()
    assert snapshot["terminalIntegrityError"] == "rustFramePipeMissingEndOfStream"
    assert snapshot["midSessionFailureReason"] == "captureIntegrityFailure"


@pytest.mark.parametrize(
    ("stop_payload", "expected_error"),
    [
        (
            {"stopped": True, "eosWritten": False, "writerError": None},
            "rustSidecarDidNotWriteEndOfStream",
        ),
        (
            {"stopped": True, "eosWritten": True, "writerError": "pipeFailed"},
            "rustAudioSidecarWriterFailed",
        ),
    ],
)
def test_rust_frame_source_terminal_stop_failure_is_fail_closed(
    stop_payload,
    expected_error,
):
    source = RustPrototypeFrameSource(
        sample_rate=16_000,
        target_channels=1,
        block_size=512,
        device=None,
        shell_call=lambda *_args, **_kwargs: {
            "success": True,
            "payload": stop_payload,
        },
    )
    source.stream_id = "stream-terminal-failure"
    source._requires_terminal_eos = True
    source._end_of_stream_wait_seconds = 0.01
    if stop_payload.get("eosWritten"):
        source._end_of_stream_event.set()

    with pytest.raises(RuntimeError, match=expected_error):
        source.stop(close=True)

    assert source.diagnostic_snapshot()["terminalIntegrityError"] == expected_error


def test_rust_frame_source_rejects_success_without_stream_id(monkeypatch):
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )
    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=512,
        device="default",
        shell_call=lambda *_args, **_kwargs: {
            "success": True,
            "payload": {
                "framePipe": "memory-pipe",
                "sampleRate": 16000,
                "channels": 1,
                "sampleFormat": "pcm_i16_le",
            },
        },
    )

    with pytest.raises(RuntimeError, match="stream ID"):
        source.open(lambda *_args: None)

    assert source.fallback_reason == "rustStreamIdMissing"


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
    assert active_snapshot["streamIdHash"]
    assert "streamId" not in active_snapshot
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


def test_provider_replay_capture_attests_consumed_fixture_tail_and_eos(
    monkeypatch,
    tmp_path,
):
    import io

    fixture = np.arange(1, 7, dtype="<i2").tobytes()
    fixture_path = tmp_path / "fixture.pcm"
    fixture_path.write_bytes(fixture)
    monkeypatch.setenv(
        "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID",
        "11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setenv(
        "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH",
        str(fixture_path),
    )
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )

    first_payload = fixture[:8]
    second_payload = fixture[8:] + b"\0" * 4
    frames = b"".join(
        (
            encode_audio_frame(
                AudioFrameHeader(
                    payload_len=len(first_payload),
                    sequence=0,
                    timestamp_micros=1,
                    frame_count=4,
                    channels=1,
                ),
                first_payload,
            ),
            encode_audio_frame(
                AudioFrameHeader(
                    payload_len=len(second_payload),
                    sequence=1,
                    timestamp_micros=2,
                    frame_count=4,
                    channels=1,
                ),
                second_payload,
            ),
            encode_audio_frame(
                AudioFrameHeader(
                    payload_len=0,
                    sequence=2,
                    timestamp_micros=3,
                    frame_count=0,
                    channels=1,
                    flags=AUDIO_FRAME_FLAG_END_OF_STREAM,
                ),
                b"",
            ),
        )
    )
    fixture_consumed = threading.Event()
    source = RustPrototypeFrameSource(
        sample_rate=48_000,
        target_channels=1,
        block_size=4,
        device="default",
        shell_call=lambda *_args, **_kwargs: None,
        reader_factory=lambda *_args, **_kwargs: io.BytesIO(frames),
        on_provider_replay_fixture_consumed=fixture_consumed.set,
    )
    source._frame_pipe = "memory-pipe"
    source._callback = lambda *_args: None
    source._provider_replay_exact_end_accepted = True
    source._read_frame_pipe()
    source.sidecar_eos_written = True

    attestation = source.provider_replay_capture_attestation()

    assert fixture_consumed.is_set()
    assert attestation is not None
    assert attestation["fixturePcmSha256"] == hashlib.sha256(fixture).hexdigest()
    assert attestation["capturedPcmSha256"] == hashlib.sha256(
        fixture + b"\0" * 4
    ).hexdigest()
    assert attestation["fixturePayloadBytesRead"] == len(fixture)
    assert attestation["fixtureAudioFramesRead"] == 6
    assert attestation["payloadBytesRead"] == 16
    assert attestation["audioFramesRead"] == 8
    assert attestation["trailingZeroFrames"] == 2
    assert attestation["expectedTrailingZeroFrames"] == 2
    assert attestation["captureBlockSizeFrames"] == 4
    assert attestation["exactFixtureEndAccepted"] is True
    assert attestation["eosFramesRead"] == 1
    assert attestation["eosObserved"] is True
    assert attestation["readerEndReason"] == "endOfStream"
    assert attestation["tailKind"] == "zero_pcm_s16le"
    assert attestation["fixturePrefixMatched"] is True
    assert attestation["tailAllZero"] is True
    assert "capturedPcmSha256" not in source.diagnostic_snapshot()


def test_provider_replay_capture_hashing_is_disabled_without_valid_private_gate(
    monkeypatch,
    tmp_path,
):
    fixture_path = tmp_path / "fixture.pcm"
    fixture_path.write_bytes(b"\1\0" * 4)
    monkeypatch.setenv("SCRIBER_B7_PROVIDER_REPLAY_RUN_ID", "not-a-uuid")
    monkeypatch.setenv(
        "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH",
        str(fixture_path),
    )

    source = RustPrototypeFrameSource(
        sample_rate=48_000,
        target_channels=1,
        block_size=4,
        device="default",
        shell_call=lambda *_args, **_kwargs: None,
    )

    assert source.provider_replay_capture_attestation() is None
    assert source._provider_replay_capture_fixture is None


def test_provider_replay_capture_requests_and_attests_exact_fixture_end(
    monkeypatch,
    tmp_path,
):
    fixture_path = tmp_path / "fixture.pcm"
    fixture_path.write_bytes(b"\1\0" * 4)
    monkeypatch.setenv(
        "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID",
        "11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setenv(
        "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH",
        str(fixture_path),
    )
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )
    commands: list[tuple[str, dict]] = []

    def shell_call(command, payload=None, **_kwargs):
        rendered = payload or {}
        commands.append((command, rendered))
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-replay-exact",
                    "framePipe": "memory-pipe",
                    "sampleRate": 48_000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "providerReplayFixtureExactEndAccepted": True,
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "captureStop",
                    "writerError": None,
                },
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=48_000,
        target_channels=1,
        block_size=4,
        device="default",
        shell_call=shell_call,
        prewarm_id="must-not-be-adopted",
        capture_route={
            "devicePreference": "leased-route",
            "nativeEndpointIdHash": "leased-endpoint",
        },
    )

    source.open(lambda *_args: None)
    source.stop(close=True)

    assert commands[0][0] == "audioCaptureStart"
    assert commands[0][1]["providerReplayFixtureExactEnd"] is True
    assert commands[0][1]["prebufferMs"] == 0
    assert commands[0][1]["prewarmId"] == ""
    assert commands[0][1]["devicePreference"] == "default"
    assert source.capture_route_source == "freshResolution"
    assert source._provider_replay_exact_end_accepted is True


def test_provider_replay_capture_rejects_unattested_exact_fixture_end(
    monkeypatch,
    tmp_path,
):
    fixture_path = tmp_path / "fixture.pcm"
    fixture_path.write_bytes(b"\1\0" * 4)
    monkeypatch.setenv(
        "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID",
        "11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setenv(
        "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH",
        str(fixture_path),
    )
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "devicePreference": "default",
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        },
    )
    commands: list[str] = []

    def shell_call(command, _payload=None, **_kwargs):
        commands.append(command)
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-replay-unattested",
                    "framePipe": "memory-pipe",
                    "sampleRate": 48_000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                },
            }
        if command == "audioCaptureStop":
            return {
                "success": True,
                "payload": {"stopped": True, "reason": "captureStop"},
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=48_000,
        target_channels=1,
        block_size=4,
        device="default",
        shell_call=shell_call,
    )

    with pytest.raises(RuntimeError, match="exact provider replay fixture boundary"):
        source.open(lambda *_args: None)

    assert commands == ["audioCaptureStart", "audioCaptureStop"]
    assert source.fallback_reason == "rustProviderReplayFixtureExactEndRejected"


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
    with pytest.raises(RuntimeError, match="rustAudioSidecarWriterFailed"):
        source.stop(close=True)

    assert snapshot["callbackCount"] == 1
    assert snapshot["framePipeReaderEndReason"] == "pipeClosed"
    assert snapshot["midSessionFailureReason"] == "pipeClosed"


def test_rust_frame_source_classifies_confirmed_external_handoff_as_expected():
    import io

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=lambda *_args, **_kwargs: None,
        reader_factory=lambda *_args, **_kwargs: io.BytesIO(b""),
    )
    source._frame_pipe = "memory-pipe"
    source._live_capture_ready = True
    source.callback_count = 5

    assert source.prepare_external_stop() is True
    source._read_frame_pipe()
    pending = source.diagnostic_snapshot()
    source.confirm_external_stop()
    confirmed = source.diagnostic_snapshot()

    assert pending["framePipeReaderEndReason"] == "externalHandoffPending"
    assert pending["midSessionFailureReason"] == ""
    assert confirmed["framePipeReaderEndReason"] == "externalHandoff"
    assert confirmed["externalStopState"] == "confirmed"
    assert confirmed["midSessionFailureReason"] == ""


def test_rust_frame_source_restores_failure_when_external_handoff_is_rejected():
    import io

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=lambda *_args, **_kwargs: None,
        reader_factory=lambda *_args, **_kwargs: io.BytesIO(b""),
    )
    source._frame_pipe = "memory-pipe"
    source._live_capture_ready = True
    source.callback_count = 5

    assert source.prepare_external_stop() is True
    source._read_frame_pipe()
    source.cancel_external_stop()
    snapshot = source.diagnostic_snapshot()

    assert snapshot["framePipeReaderEndReason"] == "pipeClosed"
    assert snapshot["externalStopState"] == "idle"
    assert snapshot["midSessionFailureReason"] == "pipeClosed"


class _InvalidArgumentPipeReader:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        raise OSError(22, "Invalid argument")


class _RuntimeFailurePipeReader:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        raise RuntimeError("callback bug")


def test_rust_frame_source_treats_oserror_as_confirmed_external_handoff():
    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=lambda *_args, **_kwargs: None,
        reader_factory=lambda *_args, **_kwargs: _InvalidArgumentPipeReader(),
    )
    source._frame_pipe = "memory-pipe"
    source._live_capture_ready = True
    source.callback_count = 5

    assert source.prepare_external_stop() is True
    source._read_frame_pipe()
    source.confirm_external_stop()
    snapshot = source.diagnostic_snapshot()

    assert snapshot["framePipeReaderEndReason"] == "externalHandoff"
    assert snapshot["externalStopState"] == "confirmed"
    assert snapshot["midSessionFailureReason"] == ""


def test_rust_frame_source_restores_oserror_when_external_handoff_is_rejected():
    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=lambda *_args, **_kwargs: None,
        reader_factory=lambda *_args, **_kwargs: _InvalidArgumentPipeReader(),
    )
    source._frame_pipe = "memory-pipe"
    source._live_capture_ready = True
    source.callback_count = 5

    assert source.prepare_external_stop() is True
    source._read_frame_pipe()
    source.cancel_external_stop()
    snapshot = source.diagnostic_snapshot()

    assert snapshot["framePipeReaderEndReason"] == "pipeClosed"
    assert snapshot["externalStopState"] == "idle"
    assert snapshot["midSessionFailureReason"] == "pipeClosed"
    assert "Invalid argument" in snapshot["lastError"]


def test_rust_frame_source_does_not_mask_programming_error_during_external_handoff():
    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=lambda *_args, **_kwargs: None,
        reader_factory=lambda *_args, **_kwargs: _RuntimeFailurePipeReader(),
    )
    source._frame_pipe = "memory-pipe"
    source._live_capture_ready = True
    source.callback_count = 5

    assert source.prepare_external_stop() is True
    source._read_frame_pipe()
    snapshot = source.diagnostic_snapshot()

    assert snapshot["framePipeReaderEndReason"] == "RuntimeError"
    assert snapshot["externalStopState"] == "pending"
    assert snapshot["midSessionFailureReason"] == "RuntimeError"
    assert snapshot["lastError"] == "callback bug"


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


def test_rust_prototype_stop_keeps_reader_alive_until_sidecar_eos(monkeypatch):
    live_audio = np.full((16, 1), 321, dtype=np.int16)
    live_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(live_audio.tobytes()),
            sequence=0,
            timestamp_micros=1,
            frame_count=16,
            channels=1,
        ),
        live_audio.tobytes(),
    )
    eos_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=0,
            sequence=1,
            timestamp_micros=2,
            frame_count=0,
            channels=1,
            flags=AUDIO_FRAME_FLAG_END_OF_STREAM,
        ),
        b"",
    )

    class BlockingReader:
        def __init__(self, initial: bytes):
            self._buffer = bytearray(initial)
            self._condition = threading.Condition()
            self._closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            with self._condition:
                self._closed = True
                self._condition.notify_all()

        def feed(self, data: bytes) -> None:
            with self._condition:
                self._buffer.extend(data)
                self._condition.notify_all()

        def read(self, size: int) -> bytes:
            if size == 0:
                return b""
            with self._condition:
                while len(self._buffer) < size and not self._closed:
                    self._condition.wait(timeout=1.0)
                if len(self._buffer) < size:
                    return b""
                result = bytes(self._buffer[:size])
                del self._buffer[:size]
                return result

    reader = BlockingReader(live_frame)
    commands: list[str] = []
    markers: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-eos",
                    "framePipe": "memory-pipe",
                    "sampleRate": 16000,
                    "channels": 1,
                    "captureChannels": 1,
                    "sampleFormat": "pcm_i16_le",
                    "nativeEndpointIdHash": "endpoint-hash",
                    "audioFrameProtocol": {
                        "magic": "SAF1",
                        "version": microphone.AUDIO_FRAME_VERSION,
                        "headerBytes": microphone.AUDIO_FRAME_HEADER_LEN,
                        "sampleFormat": "pcm_i16_le",
                        "zeroLengthEndOfStream": True,
                    },
                },
            }
        if command == "audioCaptureStop":
            reader.feed(eos_frame)
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "reason": "captureStop",
                    "connected": True,
                    "framesWritten": 1,
                    "prebufferFramesWritten": 0,
                    "liveFramesWritten": 1,
                    "bytesWritten": len(live_frame) + len(eos_frame),
                    "eosWritten": True,
                    "writerError": None,
                    "timingMarkers": {
                        "last_audio_frame_captured": 100,
                        "capture_stopped": 200,
                        "encoder_tail_started": 300,
                        "encoder_tail_completed": 400,
                    },
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=lambda *_args, **_kwargs: reader,
        first_frame_timeout_seconds=1.0,
        on_start_marker=lambda marker, timestamp_ns=None: markers.append(
            (marker, timestamp_ns)
        ),
    )
    callbacks: list[np.ndarray] = []
    source.open(lambda audio, *_args: callbacks.append(audio.copy()))
    source.start()

    source.stop(close=True)

    snapshot = source.diagnostic_snapshot()
    assert commands[-1] == "audioCaptureStop"
    assert len(callbacks) == 1
    assert snapshot["framePipeFramesRead"] == 2
    assert snapshot["framePipeEosFramesRead"] == 1
    assert snapshot["framePipeLiveFramesRead"] == 1
    assert snapshot["framePipeReaderEndReason"] == "endOfStream"
    assert snapshot["sidecarEosWritten"] is True
    assert snapshot["terminalEosRequired"] is True
    assert snapshot["midSessionFailureReason"] == ""
    assert markers[-4:] == [
        ("last_audio_frame_captured", 100),
        ("capture_stopped", 200),
        ("encoder_tail_started", 300),
        ("encoder_tail_completed", 400),
    ]


def test_rust_prototype_start_waits_for_live_frame_after_callbacking_prebuffer(monkeypatch):
    prebuffer_audio = np.full((16, 1), 100, dtype=np.int16)
    live_audio = np.full((16, 1), 200, dtype=np.int16)
    prebuffer_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(prebuffer_audio.tobytes()),
            sequence=0,
            timestamp_micros=1,
            frame_count=16,
            channels=1,
            flags=AUDIO_FRAME_FLAG_PREBUFFER,
        ),
        prebuffer_audio.tobytes(),
    )
    live_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(live_audio.tobytes()),
            sequence=1,
            timestamp_micros=2,
            frame_count=16,
            channels=1,
        ),
        live_audio.tobytes(),
    )
    waiting_for_live = threading.Event()
    release_live = threading.Event()
    calls: list[tuple[np.ndarray, int, dict, object]] = []
    commands: list[str] = []
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    class GatedReader:
        def __init__(self) -> None:
            self._payload = prebuffer_frame + live_frame
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size: int) -> bytes:
            if self._offset >= len(prebuffer_frame) and self._offset < len(self._payload):
                waiting_for_live.set()
                if not release_live.wait(timeout=2.0):
                    return b""
            if self._offset >= len(self._payload):
                return b""
            end = min(len(self._payload), self._offset + size)
            chunk = self._payload[self._offset:end]
            self._offset = end
            return chunk

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-live-ready-gate",
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
                    "bytesWritten": len(prebuffer_frame) + len(live_frame),
                    "writerError": None,
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=lambda *_args, **_kwargs: GatedReader(),
        first_frame_timeout_seconds=1.0,
    )
    source.open(lambda *args: calls.append(args))
    start_result: list[object] = []

    def start_source() -> None:
        try:
            source.start()
            start_result.append("ready")
        except Exception as exc:  # pragma: no cover - assertion reports the captured exception
            start_result.append(exc)

    start_thread = threading.Thread(target=start_source)
    start_thread.start()
    assert waiting_for_live.wait(timeout=1.0)
    assert len(calls) == 1
    np.testing.assert_array_equal(calls[0][0], prebuffer_audio)
    assert start_thread.is_alive(), "PREBUFFER must not make native capture live-ready"

    release_live.set()
    start_thread.join(timeout=1.0)
    assert not start_thread.is_alive()
    assert start_result == ["ready"]
    assert len(calls) == 2
    np.testing.assert_array_equal(calls[1][0], live_audio)
    source.stop(close=True)
    assert commands[-1] == "audioCaptureStop"


def test_rust_prototype_prebuffer_only_eof_wakes_start_and_fails(monkeypatch):
    import io
    import time

    prebuffer_audio = np.full((16, 1), 100, dtype=np.int16)
    prebuffer_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(prebuffer_audio.tobytes()),
            sequence=0,
            timestamp_micros=1,
            frame_count=16,
            channels=1,
            flags=AUDIO_FRAME_FLAG_PREBUFFER,
        ),
        prebuffer_audio.tobytes(),
    )
    calls: list[tuple[np.ndarray, int, dict, object]] = []
    commands: list[str] = []
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-prebuffer-only",
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
                    "framesWritten": 1,
                    "prebufferFramesWritten": 1,
                    "liveFramesWritten": 0,
                    "bytesWritten": len(prebuffer_frame),
                    "writerError": None,
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=lambda *_args, **_kwargs: io.BytesIO(prebuffer_frame),
        first_frame_timeout_seconds=1.0,
    )
    source.open(lambda *args: calls.append(args))
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="failed before first frame became live-ready"):
        source.start()

    assert time.monotonic() - started < 0.5
    assert len(calls) == 1, "PREBUFFER remains durable and must still reach the callback"
    snapshot = source.diagnostic_snapshot()
    assert snapshot["framePipePrebufferFramesRead"] == 1
    assert snapshot["framePipeLiveFramesRead"] == 0
    assert snapshot["fallbackReason"] == "rustFramePipeClosedBeforeFirstLiveFrame"
    assert commands[-1] == "audioCaptureStop"


def test_rust_prototype_first_live_callback_failure_does_not_report_ready(monkeypatch):
    import io

    live_audio = np.full((16, 1), 200, dtype=np.int16)
    live_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(live_audio.tobytes()),
            sequence=0,
            timestamp_micros=1,
            frame_count=16,
            channels=1,
        ),
        live_audio.tobytes(),
    )
    commands: list[str] = []
    monkeypatch.setattr(
        microphone,
        "_rust_audio_device_selection_payload",
        lambda *_args, **_kwargs: {
            "portAudioLabel": "Default Mic, Windows WASAPI",
            "nativeEndpointIdHash": "endpoint-hash",
        },
    )

    def shell_call(command, payload=None, **_kwargs):
        commands.append(command)
        if command == "audioCaptureStart":
            return {
                "success": True,
                "payload": {
                    "streamId": "stream-live-callback-failure",
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
                    "framesWritten": 1,
                    "prebufferFramesWritten": 0,
                    "liveFramesWritten": 1,
                    "bytesWritten": len(live_frame),
                    "writerError": None,
                    "exitStatus": 0,
                },
            }
        raise AssertionError(command)

    source = RustPrototypeFrameSource(
        sample_rate=16000,
        target_channels=1,
        block_size=16,
        device="default",
        shell_call=shell_call,
        reader_factory=lambda *_args, **_kwargs: io.BytesIO(live_frame),
        first_frame_timeout_seconds=1.0,
    )

    def failing_callback(*_args):
        raise RuntimeError("downstream callback failed")

    source.open(failing_callback)
    with pytest.raises(RuntimeError, match="failed before first frame became live-ready"):
        source.start()

    snapshot = source.diagnostic_snapshot()
    assert snapshot["framePipeLiveFramesRead"] == 1
    assert snapshot["callbackCount"] == 0
    assert snapshot["liveCaptureReady"] is False
    assert snapshot["fallbackReason"] == "rustFramePipeReadError"
    assert "downstream callback failed" in snapshot["lastError"]
    assert commands[-1] == "audioCaptureStop"


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


def test_microphone_input_passes_leased_route_to_rust_frame_source(monkeypatch):
    captured: dict[str, object] = {}
    sentinel = object()

    def frame_source_factory(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(microphone, "RustPrototypeFrameSource", frame_source_factory)
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        device="17",
    )
    mic._rust_prewarm_id = "prewarm-route-pass"
    mic._rust_prewarm_adoption = {
        "captureRoute": {
            "devicePreference": "17",
            "portAudioLabel": "Warm Mic, Windows WASAPI",
            "nativeEndpointIdHash": "warm-endpoint-hash",
        }
    }

    assert mic._create_frame_source() is sentinel
    assert captured["prewarm_id"] == "prewarm-route-pass"
    assert captured["capture_route"] == mic._rust_prewarm_adoption["captureRoute"]


@pytest.mark.asyncio
async def test_microphone_input_resolves_current_device_when_warm_hint_turns_stale(
    monkeypatch,
):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    fake_source = _FakeRustFrameSource()
    fresh_resolution_calls: list[str] = []
    opened_devices: list[str] = []

    class ReplacedPrewarmManager:
        engine = "rust-wasapi"

        def __init__(self) -> None:
            self.pause_calls = 0

        def attach_active_capture(self, *_args, **_kwargs):
            # Simulate DeviceMonitor replacing the route between the read-only
            # pipeline hint and the authoritative attach boundary.
            return None

        def pause_for_active_capture(self) -> None:
            self.pause_calls += 1

    prewarm = ReplacedPrewarmManager()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        device="7",
        keep_alive=True,
        prewarm_manager=prewarm,
        fresh_device_resolver=lambda: (
            fresh_resolution_calls.append("resolved") or "12"
        ),
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue

    def create_frame_source():
        opened_devices.append(str(mic.device))
        return fake_source

    mic._create_frame_source = create_frame_source

    await mic.start(microphone.StartFrame())
    await mic.stop(microphone.EndFrame())

    assert prewarm.pause_calls == 1
    assert fresh_resolution_calls == ["resolved"]
    assert opened_devices == ["12"]


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


@pytest.mark.asyncio
async def test_microphone_input_opens_native_frame_source_off_event_loop(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    open_entered = threading.Event()
    release_open = threading.Event()
    events: list[str] = []

    class BlockingFrameSource(_FakeRustFrameSource):
        def open(self, callback):
            events.append("open-entered")
            open_entered.set()
            release_open.wait(timeout=1.5)
            events.append("open-returned")
            return super().open(callback)

    fake_source = BlockingFrameSource()
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source

    # The timer prevents a regressed implementation from hanging the suite. If
    # open() runs on the asyncio thread, it fires before the heartbeat can run
    # and the event ordering assertion below fails deterministically.
    fail_safe = threading.Timer(1.0, release_open.set)
    fail_safe.start()
    start_task = asyncio.create_task(mic.start(microphone.StartFrame()))
    try:
        while not open_entered.is_set():
            await asyncio.sleep(0.005)
        events.append("event-loop-heartbeat")
        release_open.set()
        await asyncio.wait_for(start_task, timeout=1.0)
    finally:
        fail_safe.cancel()

    await mic.stop(microphone.EndFrame())

    assert events.index("event-loop-heartbeat") < events.index("open-returned")
    assert fake_source.open_calls == 1
    assert fake_source.start_calls == 1


@pytest.mark.asyncio
async def test_rust_prewarm_adoption_commits_only_after_native_capture_opens(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    events: list[str] = []
    commit_ids: list[str] = []

    class OrderedFrameSource(_FakeRustFrameSource):
        def open(self, callback):
            events.append("open")
            assert commit_ids == []
            return super().open(callback)

        def start(self) -> None:
            events.append("start")
            assert commit_ids == []
            super().start()

    class FakeRustPrewarmManager:
        engine = "rust-wasapi"

        def attach_active_capture(self, *_args, **_kwargs):
            events.append("attach")
            return {"prewarmId": "prewarm-rust-commit"}

        def commit_active_capture(self, prewarm_id: str) -> bool:
            events.append("commit")
            commit_ids.append(prewarm_id)
            return True

    fake_source = OrderedFrameSource()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=FakeRustPrewarmManager(),
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source

    await mic.start(microphone.StartFrame())
    await mic.stop(microphone.EndFrame())

    assert events[:4] == ["attach", "open", "start", "commit"]
    assert commit_ids == ["prewarm-rust-commit"]


@pytest.mark.asyncio
async def test_failed_native_open_rolls_back_same_prewarm_after_source_cleanup(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    events: list[str] = []
    commit_ids: list[str] = []
    rollback_ids: list[str] = []

    class FailingFrameSource(_FakeRustFrameSource):
        def open(self, callback):
            events.append("open")
            raise OSError("native open failed")

        def stop(self, *, close: bool) -> None:
            events.append(f"cleanup:{close}")
            super().stop(close=close)

    class FakeRustPrewarmManager:
        engine = "rust-wasapi"

        def attach_active_capture(self, *_args, **_kwargs):
            events.append("attach")
            return {"prewarmId": "prewarm-rust-rollback"}

        def commit_active_capture(self, prewarm_id: str) -> bool:
            events.append("commit")
            commit_ids.append(prewarm_id)
            return True

        def rollback_active_capture(self, prewarm_id: str) -> bool:
            events.append("rollback")
            rollback_ids.append(prewarm_id)
            return True

    fake_source = FailingFrameSource()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=FakeRustPrewarmManager(),
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source

    with pytest.raises(RuntimeError, match="Microphone initialization failed"):
        await mic.start(microphone.StartFrame())

    assert commit_ids == []
    assert rollback_ids == ["prewarm-rust-rollback"]
    assert events == ["attach", "open", "cleanup:True", "rollback"]


@pytest.mark.asyncio
async def test_failed_live_ready_does_not_commit_or_publish_ready_and_rolls_back(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    events: list[str] = []
    commit_ids: list[str] = []
    rollback_ids: list[str] = []
    ready_events: list[str] = []

    class FailingLiveReadyFrameSource(_FakeRustFrameSource):
        def open(self, callback):
            events.append("open")
            return super().open(callback)

        def start(self) -> None:
            events.append("start")
            self.stream.start()
            raise RuntimeError("first live callback failed")

        def stop(self, *, close: bool) -> None:
            events.append(f"cleanup:{close}")
            super().stop(close=close)

    class FakeRustPrewarmManager:
        engine = "rust-wasapi"

        def attach_active_capture(self, *_args, **_kwargs):
            events.append("attach")
            return {"prewarmId": "prewarm-rust-live-ready-failure"}

        def commit_active_capture(self, prewarm_id: str) -> bool:
            events.append("commit")
            commit_ids.append(prewarm_id)
            return True

        def rollback_active_capture(self, prewarm_id: str) -> bool:
            events.append("rollback")
            rollback_ids.append(prewarm_id)
            return True

    fake_source = FailingLiveReadyFrameSource()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=FakeRustPrewarmManager(),
        on_ready=lambda: ready_events.append("ready"),
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source

    with pytest.raises(RuntimeError, match="Microphone initialization failed"):
        await mic.start(microphone.StartFrame())

    assert commit_ids == []
    assert ready_events == []
    assert rollback_ids == ["prewarm-rust-live-ready-failure"]
    assert events == ["attach", "open", "start", "cleanup:True", "rollback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["attach", "open", "commit"])
async def test_cancelled_start_waits_for_native_ownership_transition_and_rolls_back(
    monkeypatch,
    blocked_phase,
):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    transition_entered = threading.Event()
    release_transition = threading.Event()
    events: list[str] = []
    rollback_ids: list[str] = []

    def block_if_selected(phase: str) -> None:
        if blocked_phase != phase:
            return
        events.append(f"{phase}-blocked")
        transition_entered.set()
        release_transition.wait(timeout=1.5)
        events.append(f"{phase}-returned")

    class BlockingFrameSource(_FakeRustFrameSource):
        def open(self, callback):
            events.append("open")
            block_if_selected("open")
            return super().open(callback)

        def start(self) -> None:
            events.append("start")
            super().start()

        def stop(self, *, close: bool) -> None:
            events.append(f"stop:{close}")
            super().stop(close=close)

    class BlockingPrewarmManager:
        engine = "rust-wasapi"

        def attach_active_capture(self, *_args, **_kwargs):
            events.append("attach")
            block_if_selected("attach")
            return {"prewarmId": "prewarm-rust-cancel"}

        def commit_active_capture(self, prewarm_id: str) -> bool:
            assert prewarm_id == "prewarm-rust-cancel"
            events.append("commit")
            block_if_selected("commit")
            return True

        def rollback_active_capture(self, prewarm_id: str) -> bool:
            events.append("rollback")
            rollback_ids.append(prewarm_id)
            return True

    fake_source = BlockingFrameSource()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=BlockingPrewarmManager(),
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source

    fail_safe = threading.Timer(1.0, release_transition.set)
    fail_safe.start()
    start_task = asyncio.create_task(mic.start(microphone.StartFrame()))
    try:
        while not transition_entered.is_set():
            await asyncio.sleep(0.005)
        start_task.cancel()
        await asyncio.sleep(0.02)
        assert not start_task.done()
        release_transition.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(start_task, timeout=1.0)
    finally:
        fail_safe.cancel()

    assert rollback_ids == ["prewarm-rust-cancel"]
    assert events.index(f"{blocked_phase}-returned") < events.index("rollback")
    assert fake_source.stream.active is False
    if blocked_phase in {"open", "commit"}:
        assert fake_source.stream.closed is True
        assert "stop:True" in events
    else:
        assert fake_source.open_calls == 0


@pytest.mark.asyncio
async def test_false_prewarm_commit_fails_start_closes_capture_and_rolls_back(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    events: list[str] = []
    rollback_ids: list[str] = []

    class OrderedFrameSource(_FakeRustFrameSource):
        def open(self, callback):
            events.append("open")
            return super().open(callback)

        def start(self) -> None:
            events.append("start")
            super().start()

        def stop(self, *, close: bool) -> None:
            events.append(f"stop:{close}")
            super().stop(close=close)

    class RejectingPrewarmManager:
        engine = "rust-wasapi"

        def attach_active_capture(self, *_args, **_kwargs):
            events.append("attach")
            return {"prewarmId": "prewarm-rust-rejected"}

        def commit_active_capture(self, prewarm_id: str) -> bool:
            events.append("commit")
            assert prewarm_id == "prewarm-rust-rejected"
            return False

        def rollback_active_capture(self, prewarm_id: str) -> bool:
            events.append("rollback")
            rollback_ids.append(prewarm_id)
            return True

    fake_source = OrderedFrameSource()
    mic = microphone.MicrophoneInput(
        sample_rate=16000,
        channels=1,
        block_size=512,
        keep_alive=True,
        prewarm_manager=RejectingPrewarmManager(),
    )
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source

    with pytest.raises(RuntimeError, match="could not be committed"):
        await mic.start(microphone.StartFrame())

    assert events == ["attach", "open", "start", "commit", "stop:True", "rollback"]
    assert rollback_ids == ["prewarm-rust-rejected"]
    assert fake_source.stream.active is False
    assert fake_source.stream.closed is True


@pytest.mark.asyncio
async def test_microphone_stop_releases_native_source_off_event_loop(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    stop_entered = threading.Event()
    release_stop = threading.Event()
    events: list[str] = []

    class BlockingStopFrameSource(_FakeRustFrameSource):
        def stop(self, *, close: bool) -> None:
            events.append("stop-entered")
            stop_entered.set()
            release_stop.wait(timeout=1.5)
            events.append("stop-returned")
            super().stop(close=close)

    fake_source = BlockingStopFrameSource()
    mic = microphone.MicrophoneInput(sample_rate=16000, channels=1, block_size=512)
    mic._create_audio_task = lambda: None

    async def fake_drain_queue():
        return None

    mic._drain_queue = fake_drain_queue
    mic._create_frame_source = lambda: fake_source
    await mic.start(microphone.StartFrame())

    fail_safe = threading.Timer(1.0, release_stop.set)
    fail_safe.start()
    stop_task = asyncio.create_task(mic.stop(microphone.EndFrame()))
    try:
        while not stop_entered.is_set():
            await asyncio.sleep(0.005)
        events.append("event-loop-heartbeat")
        release_stop.set()
        await asyncio.wait_for(stop_task, timeout=1.0)
    finally:
        fail_safe.cancel()

    assert events.index("event-loop-heartbeat") < events.index("stop-returned")
    assert fake_source.stream.active is False
    assert fake_source.stream.closed is True


@pytest.mark.asyncio
async def test_microphone_stop_accepts_rust_tail_until_eos_before_success_marker(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    tail_audio = np.full((16, 1), 777, dtype=np.int16)
    events: list[str] = []
    pushed: list[bytes] = []
    markers: list[tuple[str, int | None]] = []

    class TailOnStopFrameSource(_FakeRustFrameSource):
        sidecar_timing_markers: dict[str, int] = {}

        def stop(self, *, close: bool) -> None:
            events.append("source-stop-entered")
            self.callback(tail_audio, len(tail_audio), None, None)
            events.append("tail-callback-returned")
            super().stop(close=close)
            events.append("source-stop-returned")

    fake_source = TailOnStopFrameSource()
    mic = microphone.MicrophoneInput(
        sample_rate=16_000,
        channels=1,
        block_size=16,
        on_last_audio_chunk_sent=lambda: events.append("last-chunk-sent"),
        on_start_marker=lambda marker, timestamp_ns=None: markers.append(
            (marker, timestamp_ns)
        ),
    )
    mic._audio_in_queue = object()
    mic._create_audio_task = lambda: None
    mic._create_frame_source = lambda: fake_source

    async def fake_push_audio_frame(frame):
        pushed.append(frame.audio)
        events.append("tail-pushed")

    mic.push_audio_frame = fake_push_audio_frame

    await mic.start(microphone.StartFrame())
    await mic.stop_capture_for_finalization(close_stream=True)

    assert pushed == [tail_audio.tobytes()]
    assert events.index("source-stop-returned") < events.index("last-chunk-sent")
    assert events.index("tail-pushed") < events.index("last-chunk-sent")
    marker_map = dict(markers)
    assert marker_map["last_audio_frame_captured"] is not None
    assert marker_map["capture_stopped"] >= marker_map["last_audio_frame_captured"]
    assert mic.diagnostic_snapshot()["acceptingAudio"] is False


@pytest.mark.asyncio
async def test_microphone_terminal_eos_failure_suppresses_last_chunk_marker(monkeypatch):
    monkeypatch.setenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi")
    completion_markers: list[str] = []

    class MissingEosFrameSource(_FakeRustFrameSource):
        sidecar_timing_markers: dict[str, int] = {}
        terminal_integrity_error = "rustFramePipeMissingEndOfStream"

        def stop(self, *, close: bool) -> None:
            super().stop(close=close)
            raise RuntimeError(self.terminal_integrity_error)

    fake_source = MissingEosFrameSource()
    mic = microphone.MicrophoneInput(
        sample_rate=16_000,
        channels=1,
        block_size=16,
        on_last_audio_chunk_sent=lambda: completion_markers.append("last-chunk-sent"),
    )
    mic._audio_in_queue = object()
    mic._create_audio_task = lambda: None
    mic._create_frame_source = lambda: fake_source
    mic.push_audio_frame = lambda *_args, **_kwargs: None

    await mic.start(microphone.StartFrame())
    with pytest.raises(RuntimeError, match="rustFramePipeMissingEndOfStream"):
        await mic.stop_capture_for_finalization(close_stream=True)

    snapshot = mic.diagnostic_snapshot()
    assert completion_markers == []
    assert snapshot["captureIntegrityError"] == "rustFramePipeMissingEndOfStream"
    assert snapshot["acceptingAudio"] is False


def test_rust_audio_timeout_configuration_is_finite_and_bounded(monkeypatch):
    monkeypatch.setenv("SCRIBER_RUST_AUDIO_FIRST_FRAME_TIMEOUT_SEC", "inf")
    monkeypatch.setenv("SCRIBER_RUST_AUDIO_FAILURE_COOLDOWN_SEC", "inf")
    assert microphone._rust_first_frame_timeout_seconds() == 0.5
    assert microphone._rust_audio_failure_cooldown_seconds() == 60.0

    monkeypatch.setenv("SCRIBER_RUST_AUDIO_FIRST_FRAME_TIMEOUT_SEC", "999")
    monkeypatch.setenv("SCRIBER_RUST_AUDIO_FAILURE_COOLDOWN_SEC", "99999")
    assert microphone._rust_first_frame_timeout_seconds() == 10.0
    assert microphone._rust_audio_failure_cooldown_seconds() == 3600.0
