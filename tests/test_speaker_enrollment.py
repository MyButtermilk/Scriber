from __future__ import annotations

import io
import math
import struct
import threading

import pytest

from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_FLAG_END_OF_STREAM,
    AudioFrameHeader,
    encode_audio_frame,
)
from src.speaker_enrollment import VoiceEnrollmentCapture, assess_voice_sample


def _frame(samples: list[int], *, sequence: int = 0, flags: int = 0) -> bytes:
    payload = struct.pack(f"<{len(samples)}h", *samples)
    return encode_audio_frame(
        AudioFrameHeader(
            payload_len=len(payload),
            sequence=sequence,
            timestamp_micros=sequence * 1_000_000,
            frame_count=len(samples),
            channels=1,
            flags=flags,
        ),
        payload,
    )


def _tone(
    duration_seconds: float,
    *,
    sample_rate: int = 16_000,
    amplitude: int = 4_000,
    frequency: float = 190.0,
) -> list[int]:
    return [
        round(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
        for index in range(round(duration_seconds * sample_rate))
    ]


def test_voice_enrollment_capture_is_bounded_and_clearable() -> None:
    # The constructor deliberately enforces a one-second minimum capacity.
    first = [3_000] * 8_000
    second = [32_767] * 8_000
    stream = io.BytesIO(
        _frame(first)
        + _frame(second, sequence=1, flags=AUDIO_FRAME_FLAG_END_OF_STREAM)
    )
    capture = VoiceEnrollmentCapture(
        sample_rate=8_000,
        max_duration_seconds=1,
        reader_factory=lambda *_args, **_kwargs: stream,
    )

    capture.start("test-pipe")
    snapshot = capture.stop()

    assert snapshot["frames"] == 2
    assert snapshot["audioFrames"] == 8_000
    assert snapshot["durationMs"] == 1_000
    assert snapshot["rms"] == pytest.approx(3_000 / 32_768)
    assert snapshot["peak"] == pytest.approx(3_000 / 32_768)
    assert snapshot["clippingRatio"] == 0
    assert snapshot["active"] is True
    assert len(capture.pcm16()) == 16_000

    capture.clear()
    assert capture.pcm16() == b""


def test_voice_enrollment_activity_accepts_speech_across_embedding_windows() -> None:
    samples = _tone(8.0)
    stream = io.BytesIO(
        _frame(samples, flags=AUDIO_FRAME_FLAG_END_OF_STREAM)
    )
    capture = VoiceEnrollmentCapture(
        sample_rate=16_000,
        max_duration_seconds=8,
        reader_factory=lambda *_args, **_kwargs: stream,
    )

    capture.start("test-pipe")
    assert capture._thread is not None
    capture._thread.join(timeout=1)
    snapshot = capture.stop()

    assert snapshot["activeSpeechMs"] == 8_000
    assert snapshot["usableVoiceWindows"] == 3
    assert snapshot["voiceWindowActiveMs"] == [4_000, 4_000, 4_000]
    assert assess_voice_sample(snapshot) > 0.35


def test_short_loud_clap_does_not_pass_voice_enrollment_quality() -> None:
    sample_rate = 16_000
    samples = [0] * (8 * sample_rate)
    clap_start = 4 * sample_rate
    clap = [24_000, -24_000] * 200  # One loud 25-ms impulse frame.
    samples[clap_start : clap_start + len(clap)] = clap
    stream = io.BytesIO(
        _frame(samples, flags=AUDIO_FRAME_FLAG_END_OF_STREAM)
    )
    capture = VoiceEnrollmentCapture(
        sample_rate=sample_rate,
        max_duration_seconds=8,
        reader_factory=lambda *_args, **_kwargs: stream,
    )

    capture.start("test-pipe")
    assert capture._thread is not None
    capture._thread.join(timeout=1)
    snapshot = capture.stop()

    # These aggregate checks all passed before activity analysis was added.
    assert snapshot["durationMs"] == 8_000
    assert snapshot["rms"] > 0.008
    assert snapshot["peak"] > 0.025
    assert snapshot["clippingRatio"] < 0.02
    assert snapshot["activeSpeechMs"] == 25
    with pytest.raises(ValueError, match="too little clear speech"):
        assess_voice_sample(snapshot)


def test_voice_activity_must_cover_two_embedding_windows() -> None:
    sample_rate = 16_000
    # Two seconds of clear signal only at the very beginning provide enough
    # aggregate activity, but not enough coverage for robust start/middle/end
    # enrollment embeddings.
    samples = _tone(2.0) + [0] * (6 * sample_rate)
    stream = io.BytesIO(
        _frame(samples, flags=AUDIO_FRAME_FLAG_END_OF_STREAM)
    )
    capture = VoiceEnrollmentCapture(
        sample_rate=sample_rate,
        max_duration_seconds=8,
        reader_factory=lambda *_args, **_kwargs: stream,
    )

    capture.start("test-pipe")
    assert capture._thread is not None
    capture._thread.join(timeout=1)
    snapshot = capture.stop()

    assert snapshot["activeSpeechMs"] == 2_000
    assert snapshot["usableVoiceWindows"] == 1
    assert snapshot["voiceWindowActiveMs"] == [2_000, 0, 0]
    with pytest.raises(ValueError, match="too little clear speech"):
        assess_voice_sample(snapshot)


def test_unexpected_pipe_close_after_partial_audio_is_not_accepted() -> None:
    stream = io.BytesIO(_frame([3_000] * 8_000))
    capture = VoiceEnrollmentCapture(
        sample_rate=8_000,
        max_duration_seconds=2,
        reader_factory=lambda *_args, **_kwargs: stream,
    )

    capture.start("test-pipe")
    assert capture._thread is not None
    capture._thread.join(timeout=1)
    snapshot = capture.stop()

    assert snapshot["audioFrames"] == 8_000
    assert snapshot["active"] is False
    assert snapshot["errorCode"] == "EOFError"


def test_reader_handle_is_retained_after_stop_timeout_for_safe_retry() -> None:
    unblock = threading.Event()

    class BlockingReader:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            unblock.wait(timeout=2)
            return b""

    capture = VoiceEnrollmentCapture(
        sample_rate=8_000,
        reader_factory=lambda *_args, **_kwargs: BlockingReader(),
    )
    capture.start("test-pipe")
    reader_thread = capture._thread

    with pytest.raises(RuntimeError, match="did not stop in time"):
        capture.stop(timeout=0.01)
    assert capture._thread is reader_thread

    unblock.set()
    snapshot = capture.stop(timeout=1)
    assert capture._thread is None
    assert snapshot["errorCode"] == "reader_stop_timeout"


def test_stop_requested_while_reader_waits_for_buffer_lock_appends_no_pcm() -> None:
    reader_waiting = threading.Event()
    allow_reader = threading.Event()

    class GatedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "voice-library-enrollment":
                reader_waiting.set()
                allow_reader.wait(timeout=1)
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()
            return False

    stream = io.BytesIO(
        _frame([3_000] * 8_000, flags=AUDIO_FRAME_FLAG_END_OF_STREAM)
    )
    capture = VoiceEnrollmentCapture(
        sample_rate=8_000,
        max_duration_seconds=1,
        reader_factory=lambda *_args, **_kwargs: stream,
    )
    capture._lock = GatedLock()
    capture.start("test-pipe")

    assert reader_waiting.wait(timeout=1)
    capture._stop.set()
    allow_reader.set()
    assert capture._thread is not None
    capture._thread.join(timeout=1)

    assert capture.pcm16() == b""
    capture.stop(timeout=1)


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        (
            {
                "active": False,
                "errorCode": "EOFError",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.2,
                "clippingRatio": 0,
            },
            "could not read microphone audio",
        ),
        (
            {
                "active": True,
                "errorCode": "",
                "durationMs": 3_999,
                "rms": 0.1,
                "peak": 0.2,
                "clippingRatio": 0,
            },
            "too short",
        ),
        (
            {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.007,
                "peak": 0.2,
                "clippingRatio": 0,
            },
            "too quiet",
        ),
        (
            {
                "active": True,
                "errorCode": "",
                "durationMs": 8_000,
                "rms": 0.1,
                "peak": 0.2,
                "clippingRatio": 0.021,
            },
            "distorted",
        ),
    ],
)
def test_voice_enrollment_quality_rejects_unusable_samples(
    snapshot: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        assess_voice_sample(snapshot)


def test_voice_enrollment_quality_is_bounded() -> None:
    assert assess_voice_sample(
        {
            "active": True,
            "errorCode": "",
            "durationMs": 8_000,
            "rms": 0.3,
            "peak": 0.7,
            "clippingRatio": 0,
        }
    ) == 1.0
    assert assess_voice_sample(
        {
            "active": True,
            "errorCode": "",
            "durationMs": 4_000,
            "rms": 0.008,
            "peak": 0.025,
            "clippingRatio": 0,
        }
    ) == 0.35
