from __future__ import annotations

import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import scripts.smoke_rust_audio_sidecar as smoke
from scripts.smoke_rust_audio_sidecar import (
    effective_max_frames,
    observed_duration_sec,
    run_capture,
    validate_capture_metrics,
)
from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_FLAG_END_OF_STREAM,
    AudioFrameHeader,
    encode_audio_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_rust_audio_sidecar_smoke_script_documents_capture_contract() -> None:
    script = (REPO_ROOT / "scripts" / "smoke_rust_audio_sidecar.py").read_text(encoding="utf-8")

    assert "SCRIBER_RUST_AUDIO_WASAPI_CAPTURE" in script
    assert "SCRIBER_RUST_AUDIO_SYNTHETIC_CAPTURE" in script
    assert "captureStart" in script
    assert "captureStop" in script
    assert "nativeEndpointIdHash" in script
    assert "selected-native-endpoint-hash" in script
    assert "firstFrameReadMs" in script
    assert "observedDurationSec" in script
    assert "framesRead" in script
    assert "prebufferFramesRead" in script
    assert "prebufferAfterLiveCount" in script
    assert "totalPrebufferFramesWritten" in script
    assert "totalLiveFramesWritten" in script
    assert "--prebuffer-ms" in script
    assert "--prewarm-before-capture" in script
    assert "adoptedPrewarm" in script
    assert "totalAdoptedPrewarmBlocks" in script
    assert "sequenceGapCount" in script
    assert "selectedHashVerified" in script


def test_rust_audio_sidecar_smoke_plan_only_writes_artifact(tmp_path: Path) -> None:
    output = tmp_path / "rust-audio-sidecar-plan.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/smoke_rust_audio_sidecar.py",
            "--plan-only",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    stdout_payload = json.loads(result.stdout)
    written_payload = json.loads(output.read_text(encoding="utf-8"))

    assert stdout_payload == written_payload
    assert stdout_payload["apiVersion"] == "1"
    assert stdout_payload["ok"] is True
    assert stdout_payload["planOnly"] is True
    assert stdout_payload["mode"] == "wasapi"
    assert stdout_payload["requested"]["maxFrames"] == 0
    assert stdout_payload["requested"]["effectiveDefaultMaxFrames"] > 0
    assert stdout_payload["requested"]["prebufferMs"] == 0
    assert stdout_payload["requested"]["prewarmBeforeCapture"] is False
    assert stdout_payload["requested"]["prewarmDurationSec"] == 0.5
    assert "--duration-sec 10" in stdout_payload["exampleCommand"]
    assert any("10-minute" in item for item in stdout_payload["requirements"])


def test_rust_audio_sidecar_observed_duration_uses_frame_span_not_absolute_timestamp() -> None:
    assert observed_duration_sec(650_000, 3_650_000) == 3.0
    assert observed_duration_sec(3_650_000, 650_000) == 0.0
    assert observed_duration_sec(None, 650_000) is None


def test_rust_audio_sidecar_effective_max_frames_scales_with_duration_and_prebuffer() -> None:
    args = types.SimpleNamespace(
        max_frames=0,
        sample_rate=16_000,
        block_size=160,
        prebuffer_ms=400,
    )

    assert effective_max_frames(args, 600) >= 60_090

    args.max_frames = 123
    assert effective_max_frames(args, 600) == 123


def test_rust_audio_sidecar_smoke_validation_accepts_consistent_prebuffer_metrics() -> None:
    errors = validate_capture_metrics(
        {
            "frames": {
                "framesRead": 10,
                "prebufferFramesRead": 4,
                "liveFramesRead": 6,
                "prebufferAfterLiveCount": 0,
                "observedDurationSec": 10.0,
                "endOfStreamRead": True,
                "sequenceGapCount": 0,
            },
            "stop": {
                "stopped": True,
                "framesWritten": 10,
                "prebufferFramesWritten": 4,
                "liveFramesWritten": 6,
                "eosWritten": True,
                "writerError": None,
            },
        },
        require_prebuffer=True,
        min_observed_duration_sec=10.0,
    )

    assert errors == []


def test_rust_audio_sidecar_smoke_validation_rejects_inconsistent_writer_metrics() -> None:
    errors = validate_capture_metrics(
        {
            "frames": {
                "framesRead": 10,
                "prebufferFramesRead": 4,
                "liveFramesRead": 6,
                "prebufferAfterLiveCount": 0,
                "observedDurationSec": 10.0,
                "endOfStreamRead": True,
                "sequenceGapCount": 0,
            },
            "stop": {
                "stopped": True,
                "framesWritten": 10,
                "prebufferFramesWritten": 2,
                "liveFramesWritten": 6,
                "eosWritten": True,
                "writerError": None,
            },
        },
        require_prebuffer=True,
        min_observed_duration_sec=10.0,
    )

    assert "prebufferFramesWritten must be at least prebufferFramesRead" in errors


def test_rust_audio_sidecar_smoke_validation_rejects_short_observed_duration() -> None:
    errors = validate_capture_metrics(
        {
            "frames": {
                "framesRead": 10,
                "prebufferFramesRead": 4,
                "liveFramesRead": 6,
                "prebufferAfterLiveCount": 0,
                "observedDurationSec": 3.0,
                "endOfStreamRead": True,
                "sequenceGapCount": 0,
            },
            "stop": {
                "stopped": True,
                "framesWritten": 10,
                "prebufferFramesWritten": 4,
                "liveFramesWritten": 6,
                "eosWritten": True,
                "writerError": None,
            },
        },
        require_prebuffer=True,
        min_observed_duration_sec=10.0,
    )

    assert "observedDurationSec must be at least 10" in errors


def test_rust_audio_sidecar_smoke_keeps_reader_alive_until_stop_and_eos(monkeypatch) -> None:
    stop_requested = threading.Event()
    eos_read = threading.Event()
    audio_payload = b"\x01\x00" * 160
    audio_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(audio_payload),
            sequence=0,
            timestamp_micros=100_000,
            frame_count=160,
            channels=1,
        ),
        audio_payload,
    )
    eos_frame = encode_audio_frame(
        AudioFrameHeader(
            payload_len=0,
            sequence=1,
            timestamp_micros=110_000,
            frame_count=0,
            channels=1,
            flags=AUDIO_FRAME_FLAG_END_OF_STREAM,
        ),
        b"",
    )

    class BlockingFrameReader:
        def __init__(self) -> None:
            self.buffer = bytearray(audio_frame)
            self.eos_loaded = False

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self, length: int) -> bytes:
            if not self.buffer and not self.eos_loaded:
                assert stop_requested.wait(timeout=1.0), "reader closed before captureStop"
                self.buffer.extend(eos_frame)
                self.eos_loaded = True
            if not self.buffer:
                return b""
            chunk = bytes(self.buffer[:length])
            del self.buffer[:length]
            if self.eos_loaded and not self.buffer:
                eos_read.set()
            return chunk

    class FakeClient:
        def call(self, command, payload, *, timeout=6.0):
            if command == "captureStart":
                return {
                    "success": True,
                    "payload": {
                        "streamId": "stream-regression",
                        "framePipe": "fake-frame-pipe",
                    },
                }
            assert command == "captureStop"
            assert payload == {"streamId": "stream-regression"}
            stop_requested.set()
            assert eos_read.wait(timeout=1.0), "captureStop returned before EOS was drained"
            return {
                "success": True,
                "payload": {
                    "stopped": True,
                    "framesWritten": 1,
                    "prebufferFramesWritten": 0,
                    "liveFramesWritten": 1,
                    "eosWritten": True,
                    "writerError": None,
                },
            }

    monkeypatch.setattr(smoke, "open_pipe_for_read", lambda _path: BlockingFrameReader())

    result = run_capture(
        FakeClient(),
        name="reader-stop-eos-regression",
        request_payload={"prebufferMs": 0},
        duration_sec=0.0,
        max_frames=1,
    )

    assert result["ok"] is True
    assert result["frames"]["framesRead"] == 1
    assert result["frames"]["endOfStreamRead"] is True
    assert result["frames"]["endOfStreamSequence"] == 1
    assert result["stop"]["eosWritten"] is True
