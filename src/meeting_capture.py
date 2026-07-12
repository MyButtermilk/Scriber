"""Meeting frame-pipe consumers and crash-safe PCM chunk persistence."""
from __future__ import annotations

import errno
import hashlib
import math
import os
import threading
import time
import wave
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from loguru import logger

from src.data.meeting_store import MeetingStore
from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_HEADER_LEN,
    AUDIO_FRAME_FLAG_END_OF_STREAM,
    AudioFrameSequenceGuard,
    decode_audio_frame_header,
)


@dataclass
class MeetingCaptureStats:
    frames: int = 0
    audio_frames: int = 0
    payload_bytes: int = 0
    chunks: int = 0
    error_code: str = ""


@dataclass
class MeetingProbeStats:
    frames: int = 0
    audio_frames: int = 0
    sample_count: int = 0
    sum_squares: int = 0
    peak: int = 0
    error_code: str = ""


class MeetingDeviceLevelProbe:
    """Reads native meeting pipes briefly without persisting or forwarding audio."""

    def __init__(self, *, reader_factory: Callable[..., BinaryIO] = open) -> None:
        self.reader_factory = reader_factory
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._stats: dict[str, MeetingProbeStats] = {}
        self._lock = threading.Lock()

    def start(self, sources: list[dict[str, Any]]) -> None:
        self._stop.clear()
        for item in sources:
            source = str(item.get("source", ""))
            pipe = str(item.get("framePipe", ""))
            if source not in {"microphone", "system", "mic_clean"} or not pipe:
                raise ValueError("Native meeting device test omitted a required frame pipe.")
            with self._lock:
                self._stats.setdefault(source, MeetingProbeStats())
            thread = threading.Thread(
                target=self._consume,
                args=(source, pipe),
                name=f"meeting-device-test-{source}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 3.0) -> dict[str, Any]:
        self._stop.set()
        threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(timeout=timeout)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                source: {
                    "frames": stats.frames,
                    "audioFrames": stats.audio_frames,
                    "rms": min(
                        1.0,
                        math.sqrt(stats.sum_squares / max(1, stats.sample_count)) / 32768.0,
                    ),
                    "peak": min(1.0, stats.peak / 32768.0),
                    "active": stats.audio_frames > 0 and not stats.error_code,
                    "errorCode": stats.error_code,
                }
                for source, stats in self._stats.items()
            }

    @staticmethod
    def _read_exact(reader: BinaryIO, size: int) -> bytes:
        return MeetingAudioRecorder._read_exact(reader, size)

    def _consume(self, source: str, pipe: str) -> None:
        guard = AudioFrameSequenceGuard()
        try:
            with self.reader_factory(pipe, "rb", buffering=0) as reader:
                while not self._stop.is_set():
                    header = decode_audio_frame_header(
                        self._read_exact(reader, AUDIO_FRAME_HEADER_LEN)
                    )
                    guard.verify_and_advance(header)
                    payload = self._read_exact(reader, header.payload_len)
                    sample_count = len(payload) // 2
                    sum_squares = 0
                    peak = 0
                    for offset in range(0, sample_count * 2, 2):
                        sample = int.from_bytes(
                            payload[offset:offset + 2], "little", signed=True
                        )
                        magnitude = abs(sample)
                        peak = max(peak, magnitude)
                        sum_squares += sample * sample
                    with self._lock:
                        stats = self._stats[source]
                        stats.frames += 1
                        stats.audio_frames += header.frame_count
                        stats.sample_count += sample_count
                        stats.sum_squares += sum_squares
                        stats.peak = max(stats.peak, peak)
                    if header.flags & AUDIO_FRAME_FLAG_END_OF_STREAM:
                        break
        except (EOFError, OSError, BrokenPipeError) as exc:
            # The native sidecar closes the frame pipe as part of a successful
            # device-test stop. Keep already observed frames valid; only expose
            # a transport error when the pipe failed before the first frame.
            with self._lock:
                stats = self._stats[source]
                if stats.frames == 0:
                    stats.error_code = type(exc).__name__
        except Exception as exc:
            with self._lock:
                self._stats[source].error_code = type(exc).__name__


class MeetingAudioRecorder:
    """Consumes private Rust frame pipes into independently durable WAV chunks."""

    def __init__(
        self,
        meeting_id: str,
        root: Path,
        store: MeetingStore,
        *,
        sample_rate: int = 16_000,
        chunk_seconds: int = 30,
        reader_factory: Callable[..., BinaryIO] = open,
        on_pcm: Callable[[str, bytes, Any], None] | None = None,
        on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
        reader_open_timeout_seconds: float = 2.0,
    ) -> None:
        self.meeting_id = meeting_id
        self.root = root / meeting_id / "audio"
        self.store = store
        self.sample_rate = sample_rate
        self.chunk_audio_frames = sample_rate * chunk_seconds
        self.reader_factory = reader_factory
        self.on_pcm = on_pcm
        self.on_checkpoint = on_checkpoint
        self.reader_open_timeout_seconds = max(0.05, min(5.0, float(reader_open_timeout_seconds)))
        self._threads: list[threading.Thread] = []
        self._thread_sources: dict[threading.Thread, str] = {}
        self._lifecycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._stats: dict[str, MeetingCaptureStats] = {}
        self._lock = threading.Lock()

    def start(self, sources: list[dict[str, Any]]) -> None:
        resolved_sources: list[tuple[str, str, int]] = []
        seen_sources: set[str] = set()
        for item in sources:
            source = str(item.get("source", ""))
            pipe = str(item.get("framePipe", ""))
            timeline_offset_ms = max(0, int(item.get("timelineOffsetMs", 0) or 0))
            if source not in {"microphone", "system", "mic_clean"} or not pipe:
                raise ValueError("Native meeting capture omitted a required frame pipe.")
            if source in seen_sources:
                raise ValueError("Native meeting capture duplicated an audio source.")
            seen_sources.add(source)
            resolved_sources.append((source, pipe, timeline_offset_ms))
        with self._lifecycle_lock:
            alive = [thread for thread in self._threads if thread.is_alive()]
            if alive:
                raise RuntimeError(
                    "Meeting audio reader is still active; capture cannot restart safely."
                )
            self._threads.clear()
            self._thread_sources.clear()
            self.root.mkdir(parents=True, exist_ok=True)
            self._stop.clear()
            settled_events: dict[str, threading.Event] = {}
            opened_events: dict[str, threading.Event] = {}
            for source, pipe, timeline_offset_ms in resolved_sources:
                with self._lock:
                    self._stats.setdefault(source, MeetingCaptureStats())
                settled = threading.Event()
                opened = threading.Event()
                settled_events[source] = settled
                opened_events[source] = opened
                thread = threading.Thread(
                    target=self._consume,
                    args=(source, pipe, timeline_offset_ms, settled, opened),
                    name=f"meeting-{source}-frames",
                    daemon=True,
                )
                self._threads.append(thread)
                self._thread_sources[thread] = source
                thread.start()
        deadline = time.monotonic() + self.reader_open_timeout_seconds + 0.25
        for settled in settled_events.values():
            settled.wait(timeout=max(0.0, deadline - time.monotonic()))
        snapshot = self.snapshot()
        failed_sources = {
            source: "pipe_unavailable"
            for source, stats in snapshot.items()
            if not opened_events[source].is_set()
        }
        if failed_sources:
            self.stop()
            summary = ", ".join(f"{source}:{code}" for source, code in failed_sources.items())
            raise RuntimeError(f"Meeting audio reader startup failed ({summary}).")

    @staticmethod
    def quarantine_orphaned_partials(meetings_root: Path) -> int:
        # Kept under its historical name because startup already calls it. It
        # now first reconciles checksum-bound prepared rows and only quarantines
        # files that cannot be safely associated with one.
        return MeetingStore().reconcile_audio_chunks(meetings_root)["quarantined"]

    def stop(
        self, timeout: float = 5.0, *, expected_disconnect: bool = False
    ) -> dict[str, Any]:
        with self._lifecycle_lock:
            self._stop.set()
            threads = list(self._threads)
            deadline = time.monotonic() + max(0.0, float(timeout))
            for thread in threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            alive = [thread for thread in threads if thread.is_alive()]
            self._threads = alive
            self._thread_sources = {
                thread: self._thread_sources[thread]
                for thread in alive
                if thread in self._thread_sources
            }
            if alive:
                with self._lock:
                    for thread in alive:
                        source = self._thread_sources.get(thread)
                        if source and source in self._stats:
                            self._stats[source].error_code = "reader_stop_timeout"
                raise RuntimeError(
                    "Meeting audio reader did not stop before the timeout."
                )
            if expected_disconnect:
                with self._lock:
                    for stats in self._stats.values():
                        if stats.error_code in {"OSError", "BrokenPipeError"}:
                            stats.error_code = ""
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                source: {
                    "frames": stats.frames,
                    "audioFrames": stats.audio_frames,
                    "payloadBytes": stats.payload_bytes,
                    "chunks": stats.chunks,
                    "errorCode": stats.error_code,
                }
                for source, stats in self._stats.items()
            }

    @staticmethod
    def _read_exact(reader: BinaryIO, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            data = reader.read(remaining)
            if not data:
                raise EOFError("meeting frame pipe closed")
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def _open_reader(self, pipe: str) -> BinaryIO:
        deadline = time.monotonic() + self.reader_open_timeout_seconds
        while True:
            try:
                return self.reader_factory(pipe, "rb", buffering=0)
            except OSError:
                if self._stop.is_set() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.025)

    def _consume(
        self,
        source: str,
        pipe: str,
        timeline_offset_ms: int,
        settled: threading.Event,
        opened: threading.Event,
    ) -> None:
        guard = AudioFrameSequenceGuard()
        writer: wave.Wave_write | None = None
        writer_handle: BinaryIO | None = None
        path: Path | None = None
        sequence = self.store.next_audio_chunk_sequence(self.meeting_id, source)
        persisted_offset_ms = self.store.next_audio_offset_ms(self.meeting_id, source)
        chunk_start_frame = round(max(persisted_offset_ms, timeline_offset_ms) * self.sample_rate / 1000)
        chunk_frames = 0
        failed = False
        try:
            with self._open_reader(pipe) as reader:
                opened.set()
                settled.set()
                while not self._stop.is_set():
                    header = decode_audio_frame_header(self._read_exact(reader, AUDIO_FRAME_HEADER_LEN))
                    guard.verify_and_advance(header)
                    payload = self._read_exact(reader, header.payload_len)
                    if writer is None:
                        path = self.root / f"{source}-{sequence:06d}.partial.wav"
                        final_path = path.with_name(
                            path.name.removesuffix(".partial.wav") + ".wav"
                        )
                        if final_path.exists():
                            raise FileExistsError(
                                f"Meeting audio destination already exists: {final_path.name}"
                            )
                        writer_handle = path.open("xb")
                        try:
                            writer = wave.open(writer_handle, "wb")
                            writer.setnchannels(header.channels)
                            writer.setsampwidth(2)
                            writer.setframerate(self.sample_rate)
                        except Exception:
                            writer_handle.close()
                            writer_handle = None
                            path.unlink(missing_ok=True)
                            raise
                    writer.writeframesraw(payload)
                    if self.on_pcm is not None:
                        try:
                            self.on_pcm(source, payload, header)
                        except Exception:
                            # Live preview is best-effort; the durable writer above is authoritative.
                            pass
                    chunk_frames += header.frame_count
                    with self._lock:
                        stats = self._stats[source]
                        stats.frames += 1
                        stats.audio_frames += header.frame_count
                        stats.payload_bytes += len(payload)
                    if chunk_frames >= self.chunk_audio_frames:
                        self._close_chunk(
                            source, sequence, path, writer, writer_handle,
                            chunk_start_frame, chunk_frames,
                        )
                        writer = None
                        writer_handle = None
                        path = None
                        sequence += 1
                        chunk_start_frame += chunk_frames
                        chunk_frames = 0
                    if header.flags & AUDIO_FRAME_FLAG_END_OF_STREAM:
                        break
        except EOFError:
            pass
        except Exception as exc:
            failed = True
            self._record_error(source, exc)
            logger.warning(
                "Meeting {} frame reader stopped: {} errno={} winerror={}",
                source,
                type(exc).__name__,
                getattr(exc, "errno", None),
                getattr(exc, "winerror", None),
            )
        finally:
            settled.set()
            if writer is not None and path is not None:
                if failed:
                    with suppress(Exception):
                        writer.close()
                    if writer_handle is not None:
                        with suppress(Exception):
                            writer_handle.close()
                else:
                    try:
                        self._close_chunk(
                            source, sequence, path, writer, writer_handle,
                            chunk_start_frame, chunk_frames,
                        )
                    except Exception as exc:
                        self._record_error(source, exc)
                        with suppress(Exception):
                            writer.close()
                        if writer_handle is not None:
                            with suppress(Exception):
                                writer_handle.close()

    def _record_error(self, source: str, exc: BaseException) -> None:
        error_code = (
            "disk_full"
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC
            else type(exc).__name__
        )
        with self._lock:
            self._stats[source].error_code = error_code

    def _close_chunk(
        self,
        source: str,
        sequence: int,
        path: Path,
        writer: wave.Wave_write,
        writer_handle: BinaryIO | None,
        start_frame: int,
        frame_count: int,
    ) -> None:
        try:
            writer.close()
            if writer_handle is not None and not writer_handle.closed:
                writer_handle.flush()
                os.fsync(writer_handle.fileno())
        finally:
            if writer_handle is not None and not writer_handle.closed:
                writer_handle.close()
        if frame_count <= 0:
            path.unlink(missing_ok=True)
            return
        final_path = path.with_name(
            path.name.removesuffix(".partial.wav") + ".wav"
        )
        digest_state = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest_state.update(block)
        digest = digest_state.hexdigest()
        relative_path = final_path.relative_to(self.root.parent.parent).as_posix()
        self.store.prepare_audio_chunk(
            self.meeting_id,
            source=source,
            sequence=sequence,
            relative_path=relative_path,
            started_at_ms=round(start_frame * 1000 / self.sample_rate),
            ended_at_ms=round((start_frame + frame_count) * 1000 / self.sample_rate),
            sha256=digest,
        )
        if final_path.exists():
            raise FileExistsError(
                f"Meeting audio destination already exists: {final_path.name}"
            )
        # On Windows, Path.rename is an atomic no-replace publication. The
        # explicit existence check is also required for test/dev platforms
        # where rename may otherwise replace the destination.
        path.rename(final_path)
        completed = self.store.complete_audio_chunk(
            self.meeting_id,
            source=source,
            sequence=sequence,
            expected_sha256=digest,
        )
        checkpoint = completed.get("transcriptCheckpoint")
        if self.on_checkpoint is not None and isinstance(checkpoint, dict):
            self.on_checkpoint(checkpoint)
        with self._lock:
            self._stats[source].chunks += 1
