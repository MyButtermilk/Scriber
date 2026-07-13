"""Bounded, local-only microphone capture for Voice Library enrollment."""
from __future__ import annotations

import array
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable

from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_FLAG_END_OF_STREAM,
    AUDIO_FRAME_HEADER_LEN,
    AudioFrameSequenceGuard,
    decode_audio_frame_header,
)


_ACTIVITY_FRAME_MS = 25
_ACTIVITY_RMS_FLOOR = 0.006
_ACTIVITY_DYNAMIC_PEAK_FLOOR = 0.018
_MIN_ACTIVE_SPEECH_MS = 1_200
_EMBEDDING_WINDOW_MS = 4_000
_MIN_ACTIVE_MS_PER_EMBEDDING_WINDOW = 500
_MIN_USABLE_EMBEDDING_WINDOWS = 2


@dataclass
class VoiceEnrollmentStats:
    frames: int = 0
    audio_frames: int = 0
    sample_count: int = 0
    sum_squares: int = 0
    peak: int = 0
    clipped_samples: int = 0
    error_code: str = ""


class VoiceEnrollmentCapture:
    """Read one Rust/WASAPI frame pipe into a bounded in-memory PCM sample.

    The capture never writes audio to disk and never forwards it to a provider.
    Callers must stop the native sidecar before calling :meth:`stop` so a blocked
    named-pipe reader is released promptly.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        max_duration_seconds: float = 12.0,
        reader_factory: Callable[..., BinaryIO] = open,
        reader_open_timeout_seconds: float = 2.0,
    ) -> None:
        self.sample_rate = max(8_000, min(192_000, int(sample_rate)))
        self.max_audio_frames = max(
            self.sample_rate,
            round(self.sample_rate * max(1.0, min(30.0, float(max_duration_seconds)))),
        )
        self.reader_factory = reader_factory
        self.reader_open_timeout_seconds = max(
            0.05, min(5.0, float(reader_open_timeout_seconds))
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._expected_native_stop = threading.Event()
        self._opened = threading.Event()
        self._settled = threading.Event()
        self._lock = threading.Lock()
        self._stats = VoiceEnrollmentStats()
        self._pcm = bytearray()

    def start(self, frame_pipe: str) -> None:
        pipe = str(frame_pipe or "").strip()
        if not pipe:
            raise ValueError("Native microphone capture omitted its frame pipe.")
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("A Voice Library sample is already being captured.")
        self._stop.clear()
        self._expected_native_stop.clear()
        self._opened.clear()
        self._settled.clear()
        with self._lock:
            self._stats = VoiceEnrollmentStats()
            self._pcm.clear()
        self._thread = threading.Thread(
            target=self._consume,
            args=(pipe,),
            name="voice-library-enrollment",
            daemon=True,
        )
        self._thread.start()
        self._settled.wait(timeout=self.reader_open_timeout_seconds + 0.25)
        if not self._opened.is_set():
            self.stop(timeout=0.25)
            raise RuntimeError("Native microphone audio did not become available.")

    def stop(self, timeout: float = 3.0) -> dict[str, Any]:
        self._expected_native_stop.set()
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
            if thread.is_alive():
                with self._lock:
                    self._stats.error_code = "reader_stop_timeout"
                raise RuntimeError("Voice Library microphone capture did not stop in time.")
            if self._thread is thread:
                self._thread = None
        return self.snapshot()

    def expect_native_stop(self) -> None:
        """Mark the imminent sidecar stop so its pipe close is not a capture error."""

        self._expected_native_stop.set()

    def pcm16(self) -> bytes:
        with self._lock:
            return bytes(self._pcm)

    def clear(self) -> None:
        """Best-effort overwrite of the bounded raw sample after inference."""
        with self._lock:
            self._pcm[:] = b"\0" * len(self._pcm)
            self._pcm.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stats = VoiceEnrollmentStats(**vars(self._stats))
            pcm = bytes(self._pcm)
        rms = math.sqrt(stats.sum_squares / max(1, stats.sample_count)) / 32768.0
        duration_ms = round(stats.audio_frames * 1000 / self.sample_rate)
        activity = _analyze_voice_activity(pcm, sample_rate=self.sample_rate)
        return {
            "frames": stats.frames,
            "audioFrames": stats.audio_frames,
            "durationMs": duration_ms,
            "rms": min(1.0, rms),
            "peak": min(1.0, stats.peak / 32768.0),
            "clippingRatio": stats.clipped_samples / max(1, stats.sample_count),
            "activeSpeechMs": activity["activeSpeechMs"],
            "usableVoiceWindows": activity["usableVoiceWindows"],
            "voiceWindowActiveMs": activity["voiceWindowActiveMs"],
            "active": stats.audio_frames > 0 and not stats.error_code,
            "errorCode": stats.error_code,
        }

    def _open_reader(self, pipe: str) -> BinaryIO:
        deadline = time.monotonic() + self.reader_open_timeout_seconds
        while True:
            try:
                return self.reader_factory(pipe, "rb", buffering=0)
            except OSError:
                if self._stop.is_set() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.025)

    @staticmethod
    def _read_exact(reader: BinaryIO, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            data = reader.read(remaining)
            if not data:
                raise EOFError("voice enrollment frame pipe closed")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def _consume(self, pipe: str) -> None:
        guard = AudioFrameSequenceGuard()
        try:
            with self._open_reader(pipe) as reader:
                self._opened.set()
                self._settled.set()
                while not self._stop.is_set():
                    header = decode_audio_frame_header(
                        self._read_exact(reader, AUDIO_FRAME_HEADER_LEN)
                    )
                    guard.verify_and_advance(header)
                    payload = self._read_exact(reader, header.payload_len)
                    if header.channels != 1:
                        raise ValueError("Voice Library capture must be mono PCM.")
                    if self._stop.is_set():
                        break
                    with self._lock:
                        # ``stop`` may be requested after the check above while
                        # this reader is waiting for the buffer lock. Recheck
                        # under the lock so a timed-out reader can never append
                        # fresh PCM after cleanup has overwritten the sample.
                        if self._stop.is_set():
                            break
                        remaining_frames = max(
                            0, self.max_audio_frames - self._stats.audio_frames
                        )
                        accepted_frames = min(int(header.frame_count), remaining_frames)
                        accepted = payload[: accepted_frames * 2]
                        sum_squares = 0
                        peak = 0
                        clipped = 0
                        for offset in range(0, len(accepted), 2):
                            sample = int.from_bytes(
                                accepted[offset : offset + 2], "little", signed=True
                            )
                            magnitude = abs(sample)
                            peak = max(peak, magnitude)
                            sum_squares += sample * sample
                            if magnitude >= 32_760:
                                clipped += 1
                        self._pcm.extend(accepted)
                        self._stats.frames += 1
                        self._stats.audio_frames += accepted_frames
                        self._stats.sample_count += accepted_frames
                        self._stats.sum_squares += sum_squares
                        self._stats.peak = max(self._stats.peak, peak)
                        self._stats.clipped_samples += clipped
                    if header.flags & AUDIO_FRAME_FLAG_END_OF_STREAM:
                        break
        except (EOFError, OSError, BrokenPipeError) as exc:
            with self._lock:
                if not self._stop.is_set() and not self._expected_native_stop.is_set():
                    self._stats.error_code = type(exc).__name__
        except Exception as exc:
            with self._lock:
                self._stats.error_code = type(exc).__name__
        finally:
            self._settled.set()


def _analyze_voice_activity(pcm: bytes, *, sample_rate: int) -> dict[str, Any]:
    """Return bounded energy/activity facts for the in-memory PCM sample.

    This is intentionally a small quality gate, not a speaker-independent VAD.
    Twenty-five millisecond frames prevent a single loud click or clap from
    making an otherwise silent enrollment look useful. Removing the per-frame
    DC component also avoids treating a biased input signal as speech.
    """

    rate = max(1, int(sample_rate))
    if not pcm or len(pcm) % 2:
        return {
            "activeSpeechMs": 0,
            "usableVoiceWindows": 0,
            "voiceWindowActiveMs": [],
        }

    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    total_samples = len(samples)
    if total_samples <= 0:
        return {
            "activeSpeechMs": 0,
            "usableVoiceWindows": 0,
            "voiceWindowActiveMs": [],
        }

    frame_samples = max(1, round(rate * _ACTIVITY_FRAME_MS / 1_000))
    activity_frames: list[tuple[int, int, bool]] = []
    active_samples = 0
    for start in range(0, total_samples, frame_samples):
        end = min(total_samples, start + frame_samples)
        count = end - start
        sample_sum = 0
        sum_squares = 0
        minimum = 32_767
        maximum = -32_768
        for index in range(start, end):
            sample = int(samples[index])
            sample_sum += sample
            sum_squares += sample * sample
            minimum = min(minimum, sample)
            maximum = max(maximum, sample)
        mean = sample_sum / count
        variance = max(0.0, sum_squares / count - mean * mean)
        ac_rms = math.sqrt(variance) / 32768.0
        dynamic_peak = max(maximum - mean, mean - minimum) / 32768.0
        active = (
            ac_rms >= _ACTIVITY_RMS_FLOOR
            and dynamic_peak >= _ACTIVITY_DYNAMIC_PEAK_FLOOR
        )
        activity_frames.append((start, end, active))
        if active:
            active_samples += count

    embedding_window_samples = min(
        total_samples, round(rate * _EMBEDDING_WINDOW_MS / 1_000)
    )
    final_start = max(0, total_samples - embedding_window_samples)
    middle_start = max(0, final_start // 2)
    # A short sample can make start/middle/end identical. Count unique windows
    # so duplicated inference slices do not masquerade as broad speech coverage.
    window_starts = tuple(dict.fromkeys((0, middle_start, final_start)))
    window_active_ms: list[int] = []
    for window_start in window_starts:
        window_end = window_start + embedding_window_samples
        window_active_samples = 0
        for frame_start, frame_end, active in activity_frames:
            if not active or frame_end <= window_start or frame_start >= window_end:
                continue
            window_active_samples += max(
                0, min(frame_end, window_end) - max(frame_start, window_start)
            )
        window_active_ms.append(round(window_active_samples * 1_000 / rate))

    return {
        "activeSpeechMs": round(active_samples * 1_000 / rate),
        "usableVoiceWindows": sum(
            value >= _MIN_ACTIVE_MS_PER_EMBEDDING_WINDOW
            for value in window_active_ms
        ),
        "voiceWindowActiveMs": window_active_ms,
    }


def assess_voice_sample(snapshot: dict[str, Any]) -> float:
    """Validate a captured sample and return a bounded persistence quality."""

    if not snapshot.get("active") or snapshot.get("errorCode"):
        raise ValueError("Scriber could not read microphone audio. Check the selected microphone and try again.")
    duration_ms = int(snapshot.get("durationMs", 0) or 0)
    rms = float(snapshot.get("rms", 0.0) or 0.0)
    peak = float(snapshot.get("peak", 0.0) or 0.0)
    clipping_ratio = float(snapshot.get("clippingRatio", 0.0) or 0.0)
    if duration_ms < 4_000:
        raise ValueError("The voice sample was too short. Speak for the full recording time and try again.")
    if rms < 0.008 or peak < 0.025:
        raise ValueError("The voice sample was too quiet. Move closer to the microphone and try again.")
    if clipping_ratio > 0.02:
        raise ValueError("The voice sample was distorted. Move slightly away from the microphone and try again.")
    # Production snapshots always include the local activity facts. Keeping the
    # check conditional preserves compatibility with older test/runtime payloads
    # while every capture made by this implementation receives the stronger gate.
    if "activeSpeechMs" in snapshot:
        active_speech_ms = int(snapshot.get("activeSpeechMs", 0) or 0)
        usable_windows = int(snapshot.get("usableVoiceWindows", 0) or 0)
        if (
            active_speech_ms < _MIN_ACTIVE_SPEECH_MS
            or usable_windows < _MIN_USABLE_EMBEDDING_WINDOWS
        ):
            raise ValueError(
                "Scriber heard too little clear speech. Keep speaking naturally "
                "throughout the recording and try again."
            )
    level_score = min(1.0, max(0.0, (rms - 0.008) / 0.10))
    duration_score = min(1.0, duration_ms / 8_000)
    return round(max(0.35, min(1.0, 0.55 * level_score + 0.45 * duration_score)), 4)
