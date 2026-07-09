import asyncio
import hashlib
import os
import queue as _queue
import threading
import time
import numpy as np
from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, StartFrame, EndFrame

from src.device_monitor import get_device_guard_lock, mark_stream_started, mark_stream_stopped
from src.audio_devices import (
    build_input_endpoint_mappings,
    collect_native_capture_endpoint_inventory,
    normalize_device_name,
    normalize_native_endpoint_inventory,
)
from src.config import Config
from src.runtime.audio_frame_pipe import (
    AUDIO_FRAME_FLAG_END_OF_STREAM,
    AUDIO_FRAME_FLAG_PREBUFFER,
    AUDIO_FRAME_HEADER_LEN,
    AUDIO_FRAME_VERSION,
    AudioFrameProtocolError,
    AudioFrameSequenceGuard,
    decode_audio_frame_header,
)
from src.runtime.shell_ipc import call_shell_ipc

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except Exception:
    sd = None
    HAS_SOUNDDEVICE = False
    logger.warning("Sounddevice not available. Microphone input will be disabled.")

try:
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.base_input import BaseInputTransport
except ImportError as exc:  # pragma: no cover - defensive fallback
    raise ImportError(
        "MicrophoneInput requires pipecat.transports.base_input.BaseInputTransport. "
        "Upgrade pipecat to a version that includes BaseInputTransport."
    ) from exc


def _select_best_mono_channel(
    indata: np.ndarray,
    previous_channel: int | None = None,
) -> tuple[np.ndarray, int | None]:
    """Pick the strongest channel and convert multichannel input to mono int16.

    Some Windows endpoints expose a silent/noisy channel next to the real mic
    signal. Averaging channels can cancel speech (phase issues), so we keep the
    strongest channel with mild hysteresis to avoid frequent channel flips.
    """
    if not isinstance(indata, np.ndarray) or indata.ndim != 2 or indata.shape[1] <= 1:
        return indata, previous_channel

    try:
        channel_energy = np.mean(np.square(indata.astype(np.float32)), axis=0)
    except Exception:
        channel_energy = np.zeros(indata.shape[1], dtype=np.float32)

    best_channel = int(np.argmax(channel_energy))
    chosen_channel = best_channel

    if previous_channel is not None and 0 <= previous_channel < indata.shape[1]:
        prev_energy = float(channel_energy[previous_channel])
        best_energy = float(channel_energy[best_channel])
        # Keep previous channel unless another one is clearly stronger.
        if prev_energy > 0.0 and best_energy < (prev_energy * 1.35):
            chosen_channel = previous_channel

    mono = indata[:, chosen_channel]
    if mono.dtype != np.int16:
        mono = np.clip(mono, -32768, 32767).astype(np.int16)
    else:
        mono = np.ascontiguousarray(mono)

    return mono.reshape(-1, 1), chosen_channel


def _determine_capture_channels(output_channels: int, max_channels: int) -> int:
    """Choose capture channel count for robust mono transcription.

    For mono output we still capture several channels on mic arrays, then pick
    the strongest channel in the callback. This avoids silent-channel devices.
    """
    safe_max = max(1, int(max_channels))
    safe_output = max(1, min(int(output_channels), safe_max))
    if safe_output == 1 and safe_max >= 2:
        return min(8, safe_max)
    return safe_output


def _requested_audio_engine() -> str:
    """Live microphone capture is Rust/WASAPI-only.

    ``SCRIBER_AUDIO_ENGINE`` is kept as a backwards-compatible diagnostic input,
    but it no longer selects a Python capture implementation.
    """
    requested = (os.getenv("SCRIBER_AUDIO_ENGINE", "rust-wasapi") or "").strip().lower()
    if requested in {"", "rust", "rust-wasapi", "rust-prototype", "python"}:
        return "rust-wasapi"
    logger.warning(f"Ignoring unsupported SCRIBER_AUDIO_ENGINE={requested!r}; using Rust/WASAPI")
    return "rust-wasapi"


_RUST_AUDIO_FALLBACK_LOCK = threading.Lock()
_RUST_AUDIO_FALLBACK_UNTIL = 0.0
_RUST_AUDIO_FALLBACK_REASON = ""


def _rust_audio_failure_cooldown_seconds() -> float:
    raw = os.getenv("SCRIBER_RUST_AUDIO_FAILURE_COOLDOWN_SEC", "60")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 60.0


def _rust_audio_fallback_circuit_state() -> dict:
    now = time.monotonic()
    with _RUST_AUDIO_FALLBACK_LOCK:
        remaining = max(0.0, _RUST_AUDIO_FALLBACK_UNTIL - now)
        open_ = remaining > 0.0
        reason = _RUST_AUDIO_FALLBACK_REASON if open_ else ""
    return {
        "open": open_,
        "reason": reason,
        "remainingSeconds": round(remaining, 3) if open_ else None,
    }


def rust_audio_fallback_circuit_diagnostics() -> dict:
    state = _rust_audio_fallback_circuit_state()
    return {
        "available": True,
        "open": bool(state.get("open")),
        "reason": str(state.get("reason") or ""),
        "remainingSeconds": state.get("remainingSeconds"),
        "cooldownSeconds": _rust_audio_failure_cooldown_seconds(),
    }


def _record_rust_audio_mid_session_failure(reason: str) -> None:
    reason = str(reason or "rustMidSessionFailure")
    cooldown = _rust_audio_failure_cooldown_seconds()
    if cooldown <= 0:
        return
    until = time.monotonic() + cooldown
    with _RUST_AUDIO_FALLBACK_LOCK:
        global _RUST_AUDIO_FALLBACK_UNTIL, _RUST_AUDIO_FALLBACK_REASON
        _RUST_AUDIO_FALLBACK_UNTIL = max(_RUST_AUDIO_FALLBACK_UNTIL, until)
        _RUST_AUDIO_FALLBACK_REASON = reason


def _reset_rust_audio_fallback_circuit() -> None:
    with _RUST_AUDIO_FALLBACK_LOCK:
        global _RUST_AUDIO_FALLBACK_UNTIL, _RUST_AUDIO_FALLBACK_REASON
        _RUST_AUDIO_FALLBACK_UNTIL = 0.0
        _RUST_AUDIO_FALLBACK_REASON = ""


class AudioFrameSource:
    """Internal capture boundary for Rust/WASAPI microphone frames."""

    engine = "unknown"
    name = "unknown"

    @property
    def stream(self):
        return None

    @property
    def is_active(self) -> bool:
        return False

    def open(self, callback):
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self, *, close: bool) -> None:
        raise NotImplementedError

    def diagnostic_snapshot(self) -> dict:
        return {
            "engine": self.engine,
            "frameSource": self.name,
            "hasStream": False,
            "streamActive": False,
        }


def _hash_private_hint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _read_exact(reader, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = int(byte_count)
    while remaining > 0:
        chunk = reader.read(remaining)
        if not chunk:
            raise EOFError(f"audio frame pipe closed while reading {byte_count} bytes")
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)


class _RustPrototypeStreamHandle:
    def __init__(self) -> None:
        self.active = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.active = False


class RustPrototypeFrameSource(AudioFrameSource):
    """Reader for the Rust WASAPI capture sidecar.

    Python owns the Pipecat transport queue, but microphone capture itself is
    supplied by the Rust sidecar through private shell IPC and the versioned
    frame pipe.
    """

    engine = "rust-wasapi"
    name = "rust-frame-pipe"

    def __init__(
        self,
        *,
        sample_rate: int,
        target_channels: int,
        block_size: int,
        device,
        shell_call=None,
        reader_factory=None,
        first_frame_timeout_seconds: float | None = None,
        prewarm_id: str = "",
    ):
        self.sample_rate = int(sample_rate)
        self.target_channels = int(target_channels)
        self.capture_channels = int(target_channels)
        self.block_size = int(block_size)
        self.device = device
        self.fallback_reason = ""
        self.stream_id = ""
        self.native_endpoint_id_hash = None
        self.endpoint_selection = None
        self.sidecar_pid = None
        self.sidecar_exit_status = None
        self.sidecar_uptime_ms = None
        self.sidecar_killed_after_timeout = None
        self.sidecar_wait_error = None
        self.sidecar_connected = None
        self.sidecar_frames_written = None
        self.sidecar_prebuffer_frames_written = None
        self.sidecar_live_frames_written = None
        self.sidecar_bytes_written = None
        self.sidecar_writer_error = None
        self.sidecar_stop_reason = ""
        self.sidecar_start_count = 0
        self.callback_count = 0
        self.dropped_frame_count = 0
        self.frame_pipe_frames_read = 0
        self.frame_pipe_audio_frames_read = 0
        self.frame_pipe_payload_bytes_read = 0
        self.frame_pipe_total_bytes_read = 0
        self.frame_pipe_sequence_error_count = 0
        self.frame_pipe_protocol_error_count = 0
        self.frame_pipe_last_sequence = None
        self.frame_pipe_last_timestamp_micros = None
        self.frame_pipe_last_flags = None
        self.frame_pipe_prebuffer_frames_read = 0
        self.frame_pipe_prebuffer_audio_frames_read = 0
        self.frame_pipe_live_frames_read = 0
        self.frame_pipe_live_audio_frames_read = 0
        self.frame_pipe_prebuffer_after_live_count = 0
        self.frame_pipe_first_live_sequence = None
        self.requested_prebuffer_ms = 0
        self.requested_prewarm_id = str(prewarm_id or "")
        self.adopted_prewarm: dict | None = None
        self.frame_pipe_reader_end_reason = "notStarted"
        self.frame_pipe_first_frame_read_ms = None
        self.mid_session_failure_reason = ""
        self._shell_call = shell_call or call_shell_ipc
        self._reader_factory = reader_factory or open
        self._first_frame_timeout_seconds = (
            _rust_first_frame_timeout_seconds()
            if first_frame_timeout_seconds is None
            else max(0.05, float(first_frame_timeout_seconds))
        )
        self._stream = _RustPrototypeStreamHandle()
        self._frame_pipe = ""
        self._frame_pipe_hash = None
        self._closed = False
        self._callback = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._first_frame_event = threading.Event()
        self._sequence_guard = AudioFrameSequenceGuard()
        self._reader_started_at = 0.0
        self._last_callback_at = 0.0
        self._last_error = ""

    @property
    def stream(self):
        if self._closed:
            return None
        return self._stream

    @property
    def is_active(self) -> bool:
        return bool(self._stream and self._stream.active)

    def open(self, callback):
        self._callback = callback
        selection = _rust_audio_device_selection_payload(
            self.device,
            sample_rate=self.sample_rate,
            channels=self.target_channels,
            shell_call=self._shell_call,
        )
        self.requested_prebuffer_ms = _rust_audio_prebuffer_ms()
        payload = {
            "sampleRate": self.sample_rate,
            "channels": self.target_channels,
            "blockSize": self.block_size,
            "devicePreference": selection.get("devicePreference") or str(self.device or "default"),
            "portAudioLabel": selection.get("portAudioLabel") or "",
            "nativeEndpointIdHash": selection.get("nativeEndpointIdHash") or None,
            "prebufferMs": self.requested_prebuffer_ms,
            "prewarmId": self.requested_prewarm_id,
            "frameProtocol": {
                "magic": "SAF1",
                "version": AUDIO_FRAME_VERSION,
                "headerBytes": AUDIO_FRAME_HEADER_LEN,
                "sampleFormat": "pcm_i16_le",
            },
        }
        response = self._shell_call("audioCaptureStart", payload, timeout_seconds=2.0)
        response_payload = response.get("payload") if isinstance(response, dict) else None
        if not isinstance(response_payload, dict):
            response_payload = {}
        if not bool(response.get("success")):
            error_code = str(response.get("errorCode") or "audioCaptureStartFailed")
            fallback_reason = str(response.get("fallbackReason") or error_code)
            self.fallback_reason = error_code
            raise RuntimeError(f"Rust audio capture start failed: {fallback_reason}")

        self._last_error = ""
        self.stream_id = str(response_payload.get("streamId") or "")
        self._frame_pipe = str(response_payload.get("framePipe") or "")
        self._frame_pipe_hash = _hash_private_hint(self._frame_pipe)
        self.native_endpoint_id_hash = response_payload.get("nativeEndpointIdHash")
        endpoint_selection = response_payload.get("endpointSelection")
        self.endpoint_selection = endpoint_selection if isinstance(endpoint_selection, dict) else None
        adopted_prewarm = response_payload.get("adoptedPrewarm")
        self.adopted_prewarm = adopted_prewarm if isinstance(adopted_prewarm, dict) else None
        self.sidecar_pid = response_payload.get("sidecarPid")
        if self.stream_id:
            self.sidecar_start_count += 1
        self.capture_channels = max(
            1,
            int(response_payload.get("captureChannels") or self.target_channels),
        )
        self.target_channels = max(
            1,
            int(response_payload.get("channels") or self.target_channels),
        )
        returned_rate = int(response_payload.get("sampleRate") or self.sample_rate)
        sample_format = str(response_payload.get("sampleFormat") or "pcm_i16_le")
        if returned_rate != self.sample_rate:
            self.fallback_reason = "rustSampleRateMismatch"
            raise RuntimeError(
                f"Rust audio capture returned sample rate {returned_rate}, expected {self.sample_rate}"
            )
        if sample_format != "pcm_i16_le":
            self.fallback_reason = "rustSampleFormatMismatch"
            raise RuntimeError(
                f"Rust audio capture returned unsupported sample format {sample_format}"
            )
        if not self._frame_pipe:
            self.fallback_reason = "rustFramePipeMissing"
            raise RuntimeError("Rust audio capture did not return a frame pipe")
        return self

    def start(self) -> None:
        if self._reader_thread and self._reader_thread.is_alive():
            return
        if not self._frame_pipe:
            if self._closed:
                raise RuntimeError("Rust audio frame source is closed")
            if not callable(self._callback):
                raise RuntimeError("Rust audio frame source callback is not configured")
            self.open(self._callback)
        self._stop_event.clear()
        self._first_frame_event.clear()
        self._sequence_guard = AudioFrameSequenceGuard()
        self._reader_started_at = time.monotonic()
        self.frame_pipe_reader_end_reason = "running"
        self.frame_pipe_first_frame_read_ms = None
        self._stream.start()
        self._reader_thread = threading.Thread(
            target=self._read_frame_pipe,
            name="scriber-rust-audio-frame-pipe",
            daemon=True,
        )
        self._reader_thread.start()
        if not self._first_frame_event.wait(self._first_frame_timeout_seconds):
            self.fallback_reason = "rustFirstFrameTimeout"
            self.stop(close=True)
            raise RuntimeError("Rust audio capture did not deliver a first frame in time")
        if self._last_error and self.callback_count <= 0:
            error = self._last_error
            self.stop(close=True)
            raise RuntimeError(f"Rust audio capture failed before first frame: {error}")

    def stop(self, *, close: bool) -> None:
        self._stop_event.set()
        if self.stream_id:
            try:
                response = self._shell_call(
                    "audioCaptureStop",
                    {"streamId": self.stream_id},
                    timeout_seconds=0.75,
                )
                response_payload = response.get("payload") if isinstance(response, dict) else None
                if isinstance(response_payload, dict):
                    self._record_sidecar_stop(response_payload)
            except Exception as exc:
                self._last_error = str(exc)
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=0.5)
        self._stream.stop()
        self.stream_id = ""
        self._frame_pipe = ""
        self._frame_pipe_hash = None
        self.sidecar_pid = None
        if close:
            self._stream.close()
            self._closed = True

    def _record_sidecar_stop(self, payload: dict) -> None:
        self.sidecar_exit_status = payload.get("exitStatus")
        self.sidecar_uptime_ms = payload.get("sidecarUptimeMs")
        self.sidecar_killed_after_timeout = payload.get("sidecarKilledAfterTimeout")
        self.sidecar_wait_error = payload.get("sidecarWaitError")
        self.sidecar_connected = payload.get("connected")
        self.sidecar_frames_written = payload.get("framesWritten")
        self.sidecar_prebuffer_frames_written = payload.get("prebufferFramesWritten")
        self.sidecar_live_frames_written = payload.get("liveFramesWritten")
        self.sidecar_bytes_written = payload.get("bytesWritten")
        self.sidecar_writer_error = payload.get("writerError")
        self.sidecar_stop_reason = str(payload.get("reason") or "")
        if self.sidecar_writer_error:
            self._last_error = str(self.sidecar_writer_error)

    def diagnostic_snapshot(self) -> dict:
        return {
            "engine": self.engine,
            "frameSource": self.name,
            "hasStream": bool(self._frame_pipe_hash or self.stream_id),
            "streamActive": self.is_active,
            "sampleRate": self.sample_rate,
            "targetChannels": self.target_channels,
            "captureChannels": self.capture_channels,
            "blockSize": self.block_size,
            "device": str(self.device),
            "requestedPrebufferMs": self.requested_prebuffer_ms,
            "requestedPrewarmIdHash": _hash_private_hint(self.requested_prewarm_id),
            "adoptedPrewarm": self._redacted_adopted_prewarm(),
            "streamId": self.stream_id or None,
            "framePipeHash": self._frame_pipe_hash,
            "nativeEndpointIdHash": self.native_endpoint_id_hash,
            "endpointSelection": self.endpoint_selection,
            "sidecarPid": self.sidecar_pid,
            "sidecarExitStatus": self.sidecar_exit_status,
            "sidecarUptimeMs": self.sidecar_uptime_ms,
            "sidecarKilledAfterTimeout": self.sidecar_killed_after_timeout,
            "sidecarWaitError": self.sidecar_wait_error,
            "sidecarConnected": self.sidecar_connected,
            "sidecarFramesWritten": self.sidecar_frames_written,
            "sidecarPrebufferFramesWritten": self.sidecar_prebuffer_frames_written,
            "sidecarLiveFramesWritten": self.sidecar_live_frames_written,
            "sidecarBytesWritten": self.sidecar_bytes_written,
            "sidecarWriterError": self.sidecar_writer_error,
            "sidecarStopReason": self.sidecar_stop_reason,
            "sidecarStartCount": self.sidecar_start_count,
            "sidecarRestartCount": max(0, int(self.sidecar_start_count) - 1),
            "readerThreadAlive": bool(self._reader_thread and self._reader_thread.is_alive()),
            "callbackCount": self.callback_count,
            "droppedFrameCount": self.dropped_frame_count,
            "framePipeFramesRead": self.frame_pipe_frames_read,
            "framePipeAudioFramesRead": self.frame_pipe_audio_frames_read,
            "framePipePayloadBytesRead": self.frame_pipe_payload_bytes_read,
            "framePipeTotalBytesRead": self.frame_pipe_total_bytes_read,
            "framePipeSequenceErrorCount": self.frame_pipe_sequence_error_count,
            "framePipeProtocolErrorCount": self.frame_pipe_protocol_error_count,
            "framePipeLastSequence": self.frame_pipe_last_sequence,
            "framePipeLastTimestampMicros": self.frame_pipe_last_timestamp_micros,
            "framePipeLastFlags": self.frame_pipe_last_flags,
            "framePipePrebufferFramesRead": self.frame_pipe_prebuffer_frames_read,
            "framePipePrebufferAudioFramesRead": self.frame_pipe_prebuffer_audio_frames_read,
            "framePipeLiveFramesRead": self.frame_pipe_live_frames_read,
            "framePipeLiveAudioFramesRead": self.frame_pipe_live_audio_frames_read,
            "framePipePrebufferAfterLiveCount": self.frame_pipe_prebuffer_after_live_count,
            "framePipeFirstLiveSequence": self.frame_pipe_first_live_sequence,
            "framePipeReaderEndReason": self.frame_pipe_reader_end_reason,
            "framePipeFirstFrameReadMs": self.frame_pipe_first_frame_read_ms,
            "midSessionFailureReason": self.mid_session_failure_reason,
            "fallbackReason": self.fallback_reason,
            "lastError": self._last_error,
            "lastCallbackAgoSeconds": (
                round(time.monotonic() - self._last_callback_at, 3)
                if self._last_callback_at > 0
                else None
            ),
        }

    def _redacted_adopted_prewarm(self) -> dict | None:
        if not isinstance(self.adopted_prewarm, dict):
            return None
        payload = dict(self.adopted_prewarm)
        if "prewarmId" in payload:
            payload["prewarmIdHash"] = _hash_private_hint(str(payload.pop("prewarmId") or ""))
        stop_payload = payload.get("stop")
        if isinstance(stop_payload, dict) and "prewarmId" in stop_payload:
            stop_payload = dict(stop_payload)
            stop_payload["prewarmIdHash"] = _hash_private_hint(
                str(stop_payload.pop("prewarmId") or "")
            )
            payload["stop"] = stop_payload
        return payload

    def _read_frame_pipe(self) -> None:
        try:
            with self._reader_factory(self._frame_pipe, "rb", buffering=0) as reader:
                while not self._stop_event.is_set():
                    header_bytes = _read_exact(reader, AUDIO_FRAME_HEADER_LEN)
                    self.frame_pipe_total_bytes_read += len(header_bytes)
                    header = decode_audio_frame_header(header_bytes)
                    payload = _read_exact(reader, header.payload_len)
                    self.frame_pipe_payload_bytes_read += len(payload)
                    self.frame_pipe_total_bytes_read += len(payload)
                    self._sequence_guard.verify_and_advance(header)
                    if int(header.channels) != int(self.capture_channels):
                        raise AudioFrameProtocolError(
                            f"Rust audio frame channel mismatch: expected {self.capture_channels}, got {header.channels}"
                        )
                    is_prebuffer = bool(header.flags & AUDIO_FRAME_FLAG_PREBUFFER)
                    if is_prebuffer and self.frame_pipe_live_frames_read > 0:
                        self.frame_pipe_prebuffer_after_live_count += 1
                        raise AudioFrameProtocolError(
                            "Rust audio prebuffer frame arrived after live frame"
                        )
                    self.frame_pipe_frames_read += 1
                    self.frame_pipe_audio_frames_read += int(header.frame_count)
                    self.frame_pipe_last_sequence = int(header.sequence)
                    self.frame_pipe_last_timestamp_micros = int(header.timestamp_micros)
                    self.frame_pipe_last_flags = int(header.flags)
                    if is_prebuffer:
                        self.frame_pipe_prebuffer_frames_read += 1
                        self.frame_pipe_prebuffer_audio_frames_read += int(header.frame_count)
                    else:
                        self.frame_pipe_live_frames_read += 1
                        self.frame_pipe_live_audio_frames_read += int(header.frame_count)
                        if self.frame_pipe_first_live_sequence is None:
                            self.frame_pipe_first_live_sequence = int(header.sequence)
                    if self.frame_pipe_first_frame_read_ms is None and self._reader_started_at > 0:
                        self.frame_pipe_first_frame_read_ms = round(
                            (time.monotonic() - self._reader_started_at) * 1000.0,
                            3,
                        )
                    samples = np.frombuffer(payload, dtype="<i2")
                    audio = samples.reshape((header.frame_count, header.channels))
                    callback = self._callback
                    if callable(callback):
                        try:
                            callback(
                                audio,
                                header.frame_count,
                                {
                                    "timestamp_micros": header.timestamp_micros,
                                    "engine": self.engine,
                                },
                                None,
                            )
                        except Exception:
                            self.dropped_frame_count += 1
                            raise
                    else:
                        self.dropped_frame_count += 1
                    self.callback_count += 1
                    self._last_callback_at = time.monotonic()
                    self._first_frame_event.set()
                    if header.flags & AUDIO_FRAME_FLAG_END_OF_STREAM:
                        self.frame_pipe_reader_end_reason = "endOfStream"
                        break
                if self.frame_pipe_reader_end_reason == "running":
                    self.frame_pipe_reader_end_reason = "stopRequested"
        except EOFError as exc:
            if self._stop_event.is_set():
                self.frame_pipe_reader_end_reason = "stopRequested"
            else:
                self.frame_pipe_reader_end_reason = "pipeClosed"
                self._last_error = str(exc)
                if self.callback_count <= 0 and not self.fallback_reason:
                    self.fallback_reason = "rustFramePipeClosedBeforeFirstFrame"
                if self.callback_count > 0:
                    self.mid_session_failure_reason = "pipeClosed"
                    logger.warning(
                        f"Rust audio frame pipe stopped after {self.callback_count} frame(s): {exc}"
                    )
            self._first_frame_event.set()
        except AudioFrameProtocolError as exc:
            message = str(exc)
            if "sequence out of order" in message:
                self.frame_pipe_sequence_error_count += 1
                if not self.fallback_reason:
                    self.fallback_reason = "rustFramePipeSequenceError"
            elif "prebuffer frame arrived after live frame" in message:
                self.frame_pipe_protocol_error_count += 1
                if not self.fallback_reason:
                    self.fallback_reason = "rustFramePipePrebufferInterleaving"
            else:
                self.frame_pipe_protocol_error_count += 1
                if not self.fallback_reason:
                    self.fallback_reason = "rustFramePipeProtocolError"
            self.frame_pipe_reader_end_reason = "protocolError"
            self._last_error = message
            self._first_frame_event.set()
            if self.callback_count > 0:
                self.mid_session_failure_reason = self.fallback_reason or "protocolError"
                logger.warning(
                    f"Rust audio frame pipe stopped after {self.callback_count} frame(s): {exc}"
                )
        except Exception as exc:
            if self._stop_event.is_set():
                self.frame_pipe_reader_end_reason = "stopRequested"
            else:
                self.frame_pipe_reader_end_reason = type(exc).__name__
                self._last_error = str(exc)
                if self.callback_count <= 0 and not self.fallback_reason:
                    self.fallback_reason = "rustFramePipeReadError"
            self._first_frame_event.set()
            if self.callback_count > 0 and not self._stop_event.is_set():
                self.mid_session_failure_reason = self.frame_pipe_reader_end_reason
                logger.warning(
                    f"Rust audio frame pipe stopped after {self.callback_count} frame(s): {exc}"
                )
        finally:
            self._stream.stop()


def _rust_first_frame_timeout_seconds() -> float:
    raw = os.getenv("SCRIBER_RUST_AUDIO_FIRST_FRAME_TIMEOUT_SEC", "0.5")
    try:
        return max(0.05, float(raw))
    except (TypeError, ValueError):
        return 0.5


def _rust_audio_prebuffer_ms() -> int:
    try:
        return max(0, min(2000, int(getattr(Config, "MIC_PREBUFFER_MS", 0) or 0)))
    except (TypeError, ValueError):
        return 0


def _rust_audio_device_selection_payload(
    device,
    *,
    sample_rate: int,
    channels: int,
    shell_call=None,
) -> dict:
    """Return redacted native endpoint hints for the opt-in Rust audio prototype."""

    configured_device = str(getattr(Config, "MIC_DEVICE", "default") or "default").strip() or "default"
    favorite_mic = str(getattr(Config, "FAVORITE_MIC", "") or "").strip()
    raw_device = str(device or "default").strip()
    resolved_non_default = raw_device not in {"", "default", "None"}
    result = {
        "devicePreference": str(device or "default"),
        "portAudioLabel": "",
        "nativeEndpointIdHash": None,
        "nativeDefaultInputEndpointIdHash": None,
        "nativeEndpointMatchConfidence": "none",
        "nativeEndpointMatchReason": "notResolved",
    }
    if configured_device in {"default", "None"} and not favorite_mic:
        result.update(
            {
                "devicePreference": "default",
                "nativeEndpointMatchConfidence": "default",
                "nativeEndpointMatchReason": "windowsDefaultEndpoint",
            }
        )
        return result
    if not HAS_SOUNDDEVICE or sd is None:
        result["nativeEndpointMatchReason"] = "sounddeviceUnavailable"
        return result

    try:
        native_endpoints = _collect_rust_audio_native_capture_endpoint_inventory(
            shell_call=shell_call,
        )
        mappings = build_input_endpoint_mappings(
            sd,
            native_endpoints=native_endpoints,
            sample_rate=sample_rate,
            channels=channels,
        )
    except Exception as exc:
        result["nativeEndpointMatchReason"] = f"mappingFailed:{type(exc).__name__}"
        return result

    match = None
    if raw_device and raw_device not in {"default", "None"}:
        try:
            wanted_index = int(raw_device)
            match = next(
                (mapping for mapping in mappings if mapping.portaudio_index == wanted_index),
                None,
            )
        except ValueError:
            wanted_normalized = normalize_device_name(raw_device)
            match = next(
                (
                    mapping
                    for mapping in mappings
                    if mapping.portaudio_name == raw_device
                    or (
                        wanted_normalized
                        and mapping.normalized_name == wanted_normalized
                    )
                ),
                None,
            )
    else:
        match = next((mapping for mapping in mappings if mapping.is_default), None)

    if match is None:
        endpoint = (
            _match_rust_audio_native_endpoint_by_label(
                native_endpoints,
                favorite_mic or configured_device or raw_device,
            )
            if resolved_non_default
            else None
        )
        if endpoint is not None:
            result.update(
                {
                    "portAudioLabel": favorite_mic or configured_device or raw_device,
                    "nativeEndpointIdHash": endpoint.endpoint_id_hash,
                    "nativeEndpointMatchConfidence": "name",
                    "nativeEndpointMatchReason": "nativeInventoryLabel",
                }
            )
            return result
        result["nativeEndpointMatchReason"] = (
            "nativeEndpointNotFound" if native_endpoints else "nativeInventoryUnavailable"
        )
        return result

    result.update(
        {
            "portAudioLabel": match.portaudio_name,
            "nativeEndpointIdHash": match.native_endpoint_id_hash,
            "nativeDefaultInputEndpointIdHash": match.native_default_input_endpoint_id_hash,
            "nativeEndpointMatchConfidence": match.match_confidence,
            "nativeEndpointMatchReason": match.match_reason,
        }
    )
    return result


def _collect_rust_audio_native_capture_endpoint_inventory(*, shell_call=None) -> list[dict]:
    if shell_call is not None:
        try:
            response = shell_call("audioEndpointInventory", {}, timeout_seconds=2.0)
        except Exception:
            response = None
        if isinstance(response, dict) and response.get("success"):
            payload = response.get("payload")
            endpoints = payload.get("endpoints") if isinstance(payload, dict) else None
            if isinstance(endpoints, list):
                shell_endpoints = [item for item in endpoints if isinstance(item, dict)]
                if shell_endpoints:
                    return shell_endpoints
    return collect_native_capture_endpoint_inventory()


def _match_rust_audio_native_endpoint_by_label(
    native_endpoints: list[dict],
    label: str,
):
    normalized_label = normalize_device_name(str(label or ""))
    if not normalized_label:
        return None
    for endpoint in normalize_native_endpoint_inventory(native_endpoints):
        if endpoint.normalized_name == normalized_label:
            return endpoint
    return None


class MicrophoneInput(BaseInputTransport):
    def __init__(
        self,
        sample_rate=16000,
        channels=1,
        block_size=512,
        turn_analyzer=None,
        vad_analyzer=None,
        device="default",
        keep_alive=False,
        prewarm_manager=None,
        on_audio_level=None,
        on_ready=None,
        on_last_audio_chunk_sent=None,
    ):
        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=sample_rate,
            audio_in_channels=channels,
            audio_in_passthrough=True,
            turn_analyzer=turn_analyzer,
            vad_analyzer=vad_analyzer,
        )
        super().__init__(params=params)
        self._target_sample_rate = sample_rate
        self._target_channels = channels
        self._capture_channels = channels
        self.block_size = block_size
        self.device = device
        self.keep_alive = keep_alive
        self.prewarm_manager = prewarm_manager
        self.on_audio_level = on_audio_level
        self.on_ready = on_ready
        self.on_last_audio_chunk_sent = on_last_audio_chunk_sent
        self.stream = None
        self._frame_source: AudioFrameSource | None = None
        self._requested_audio_engine = _requested_audio_engine()
        self._audio_engine = "rust-wasapi"
        self._frame_source_name = "rust-frame-pipe"
        self._audio_engine_fallback_reason = ""
        self._prewarm_adoption_skipped_reason = ""
        self._rust_prewarm_adoption: dict | None = None
        self._rust_prewarm_id = ""
        self._running = False
        self._queue = _queue.Queue(maxsize=512)
        self._dropped_chunks = 0
        self._drained_chunks = 0
        self._max_queue_depth = 0
        self._last_drop_log_at = 0.0
        self._consumer_error = ""
        self._consumer_last_state = "not_started"
        self._consumer_loop: asyncio.AbstractEventLoop | None = None
        self._queue_wakeup: asyncio.Event | None = None
        self._consumer_task = None
        # Serializes _audio_callback against itself. The prewarm path can invoke
        # this callback from the PortAudio thread (live frames) and the event loop
        # thread (prebuffer replay) concurrently; reentrant so the replay can hold
        # the lock while calling back into _audio_callback.
        self._callback_lock = threading.RLock()
        self._stopped = asyncio.Event()
        # Visualizer gating (reduce noise-triggered movement)
        self._noise_floor_db = -70.0
        self._speech_active = False
        self._speech_hold_until = 0.0
        self._visual_level = 0.0
        self._active_capture_channel = None
        self._channel_selection_counter = 0
        self._channel_selection_interval_frames = 10
        self._last_audio_level_at = 0.0
        self._audio_level_interval = 1.0 / 60.0
        self._last_observed_rms = 0.0
        self._max_observed_rms = 0.0
        self._audio_level_sample_count = 0
        self._speech_observed = False
        self._speech_observed_count = 0
        self._last_speech_at = 0.0
        self._stream_claimed = False
        self._last_audio_chunk_sent_notified = False
        self._callback_count = 0
        self._last_callback_at = 0.0
        self._stream_started_at = 0.0
        self._last_status = ""
        self._last_callback_exception = ""
        self._last_health_restart_at = 0.0
        self._health_restart_count = 0
        self._health_restart_throttle_count = 0
        self._last_health_check_reason = ""
        self._last_health_failure_reason = ""
        self._last_health_restart_reason = ""
        self._last_health_restart_error = ""
        self._last_health_restart_throttled_reason = ""
        self._last_health_restart_throttle_remaining_seconds = None
        self._last_rust_audio_mid_session_failure_reason = ""

    def _claim_active_stream(self) -> None:
        if self._stream_claimed:
            return
        mark_stream_started()
        self._stream_claimed = True

    def _release_active_stream(self) -> None:
        if not self._stream_claimed:
            return
        mark_stream_stopped()
        self._stream_claimed = False

    def _create_frame_source(self) -> AudioFrameSource:
        requested = _requested_audio_engine()
        self._requested_audio_engine = requested
        self._audio_engine_fallback_reason = ""
        circuit = _rust_audio_fallback_circuit_state()
        if circuit["open"]:
            reason = str(circuit.get("reason") or "rustMidSessionFailure")
            self._audio_engine_fallback_reason = f"rustCircuitOpen:{reason}"
            raise RuntimeError(
                "Rust audio fallback circuit is open; Python capture fallback has been removed "
                f"(reason={reason}, remaining={circuit.get('remainingSeconds')}s)"
            )
        return RustPrototypeFrameSource(
            sample_rate=self._target_sample_rate,
            target_channels=self._target_channels,
            block_size=self.block_size,
            device=self.device,
            prewarm_id=self._rust_prewarm_id,
        )

    def _open_and_start_frame_source(self) -> None:
        source = self._frame_source
        try:
            if source is None or source.stream is None:
                source = self._create_frame_source()
                self._frame_source = source
                source.open(self._audio_callback)
            self._sync_frame_source_state()
            if not self.stream:
                raise RuntimeError("Microphone frame source did not expose a stream handle")
            if not getattr(self.stream, "active", False):
                if self._source_owns_stream():
                    source.start()
                else:
                    self.stream.start()
            self._sync_frame_source_state()
            return
        except Exception as exc:
            reason = str(getattr(source, "fallback_reason", "") or type(exc).__name__)
            self._audio_engine_fallback_reason = f"rustCaptureFailed:{reason}"
            logger.error(f"Rust audio capture failed; no Python fallback is available ({exc})")
            try:
                if source is not None:
                    source.stop(close=True)
            except Exception:
                pass
            raise

    def _sync_frame_source_state(self) -> None:
        source = self._frame_source
        if source is None:
            return
        self.stream = source.stream
        self._audio_engine = source.engine
        self._frame_source_name = source.name
        self._target_channels = int(getattr(source, "target_channels", self._target_channels))
        self._capture_channels = int(getattr(source, "capture_channels", self._capture_channels))
        fallback_reason = str(getattr(source, "fallback_reason", "") or "")
        if fallback_reason:
            if (
                self._audio_engine_fallback_reason
                and self._audio_engine_fallback_reason != fallback_reason
            ):
                self._audio_engine_fallback_reason = (
                    f"{self._audio_engine_fallback_reason};{fallback_reason}"
                )
            else:
                self._audio_engine_fallback_reason = fallback_reason

    def _source_owns_stream(self) -> bool:
        return bool(
            self._frame_source is not None
            and self.stream is not None
            and self._frame_source.stream is self.stream
        )

    async def start(self, frame: StartFrame):
        """Start audio capture and feed frames into the transport queue."""
        logger.debug(f"MicrophoneInput.start() called, device={self.device}")
        await super().start(frame)
        self._consumer_loop = asyncio.get_running_loop()
        self._queue_wakeup = asyncio.Event()
        self._queue = _queue.Queue(maxsize=512)
        self._dropped_chunks = 0
        self._drained_chunks = 0
        self._max_queue_depth = 0
        self._last_drop_log_at = 0.0
        self._consumer_error = ""
        self._consumer_last_state = "not_started"
        self._running = True
        self._active_capture_channel = None
        self._channel_selection_counter = 0
        self._last_audio_level_at = 0.0
        self._last_observed_rms = 0.0
        self._max_observed_rms = 0.0
        self._audio_level_sample_count = 0
        self._speech_observed = False
        self._speech_observed_count = 0
        self._last_speech_at = 0.0
        self._last_audio_chunk_sent_notified = False
        self._callback_count = 0
        self._last_callback_at = 0.0
        self._stream_started_at = 0.0
        self._last_status = ""
        self._last_callback_exception = ""
        self._last_health_restart_at = 0.0
        self._health_restart_count = 0
        self._health_restart_throttle_count = 0
        self._last_health_check_reason = ""
        self._last_health_failure_reason = ""
        self._last_health_restart_reason = ""
        self._last_health_restart_error = ""
        self._last_health_restart_throttled_reason = ""
        self._last_health_restart_throttle_remaining_seconds = None
        self._last_rust_audio_mid_session_failure_reason = ""
        self._prewarm_adoption_skipped_reason = ""
        self._rust_prewarm_adoption = None
        self._rust_prewarm_id = ""
        self._requested_audio_engine = _requested_audio_engine()
        self._create_audio_task()
        self._consumer_task = asyncio.create_task(self._drain_queue(), name="microphone_drain")
        self._consumer_last_state = "running"
        self._consumer_task.add_done_callback(self._on_consumer_task_done)

        try:
            if self.keep_alive and self.prewarm_manager is not None:
                adopted = None
                self._prewarm_adoption_skipped_reason = ""
                if (
                    self._requested_audio_engine == "rust-wasapi"
                    and getattr(self.prewarm_manager, "engine", "") == "rust-wasapi"
                ):
                    try:
                        adopted = self.prewarm_manager.attach_active_capture(
                            None,
                            sample_rate=self._target_sample_rate,
                            target_channels=self._target_channels,
                            block_size=self.block_size,
                            device=self.device,
                        )
                    except Exception as exc:
                        logger.debug(f"Could not attach Rust prewarm session: {exc}")
                        adopted = None
                    prewarm_id = str(
                        (adopted or {}).get("prewarmId")
                        or (adopted or {}).get("prewarm_id")
                        or ""
                    )
                    if prewarm_id:
                        self._rust_prewarm_adoption = adopted
                        self._rust_prewarm_id = prewarm_id
                        logger.info("Rust mic prewarm session will be adopted by capture")
                    else:
                        self._prewarm_adoption_skipped_reason = "rustPrewarmUnavailable"
                else:
                    self._prewarm_adoption_skipped_reason = (
                        f"engine:{self._requested_audio_engine}"
                    )

                if not self._rust_prewarm_id:
                    try:
                        self.prewarm_manager.pause_for_active_capture()
                    except Exception as exc:
                        logger.debug(f"Could not pause idle mic prewarm before Rust capture: {exc}")
            # Device enumeration/open is guarded against concurrent PortAudio refresh.
            with get_device_guard_lock():
                self._open_and_start_frame_source()
                self._stream_started_at = time.monotonic()
                self._claim_active_stream()
            device_index = getattr(self._frame_source, "device_index", None)
            logger.info(f"Microphone stream started (device={'default' if device_index is None else device_index})")
            # Signal that microphone is ready and capturing audio
            if self.on_ready:
                try:
                    self.on_ready()
                except Exception as e:
                    logger.warning(f"on_ready callback error: {e}")
        except Exception as e:
            logger.error(f"Microphone error: {e}")
            await self.stop(frame=EndFrame())
            # Re-raise to notify the pipeline that microphone initialization failed
            raise RuntimeError(f"Microphone initialization failed: {e}") from e

    def _audio_callback(self, indata, frames, time_info, status):
        # Serialized via reentrant lock: the prewarm path can drive this callback
        # from the PortAudio thread (live frames) and the event loop thread
        # (prebuffer replay) at the same time. The lock keeps channel-selection and
        # visualizer state consistent and preserves frame ordering into the queue.
        try:
            with self._callback_lock:
                self._process_audio_callback(indata, frames, time_info, status)
        except Exception as exc:
            # Never raise from PortAudio callback threads.
            self._last_callback_exception = str(exc)
            logger.debug(f"Audio callback exception ignored: {exc}")

    def _process_audio_callback(self, indata, frames, time_info, status):
        self._callback_count += 1
        if status:
            self._last_status = str(status)
            logger.warning(f"Audio status: {status}")
        if not self._running:
            return

        output_data = indata
        if (
            isinstance(indata, np.ndarray)
            and indata.ndim == 2
            and indata.shape[1] > self._target_channels
            and self._target_channels == 1
        ):
            self._channel_selection_counter += 1
            previous_channel = self._active_capture_channel
            should_rescan_channel = (
                previous_channel is None
                or previous_channel < 0
                or previous_channel >= indata.shape[1]
                or self._channel_selection_counter >= self._channel_selection_interval_frames
            )
            if should_rescan_channel:
                self._channel_selection_counter = 0
                output_data, chosen_channel = _select_best_mono_channel(
                    indata,
                    previous_channel,
                )
                if chosen_channel != previous_channel and chosen_channel is not None:
                    logger.debug(
                        "Microphone channel selection changed: {} -> {}",
                        previous_channel,
                        chosen_channel,
                    )
                self._active_capture_channel = chosen_channel
            else:
                mono = indata[:, previous_channel]
                if mono.dtype != np.int16:
                    mono = np.clip(mono, -32768, 32767).astype(np.int16)
                else:
                    mono = np.ascontiguousarray(mono)
                output_data = mono.reshape(-1, 1)

        audio_bytes = output_data.tobytes()
        # PortAudio is alive whenever it invokes us, regardless of whether the
        # frame is queued or dropped. Mark liveness for the watchdog here so a
        # full queue (slow consumer) is not misdiagnosed as a dead capture
        # stream — that would trigger a pointless stream restart. Real capture
        # stalls show up as the callback no longer firing at all.
        self._last_callback_at = time.monotonic()
        try:
            self._queue.put_nowait(audio_bytes)
            queue_depth = self._queue.qsize()
            self._max_queue_depth = max(self._max_queue_depth, queue_depth)
            self._signal_queue_wakeup()
        except _queue.Full:
            self._dropped_chunks += 1
            now = time.monotonic()
            if self._dropped_chunks == 1 or now - self._last_drop_log_at >= 1.0:
                self._last_drop_log_at = now
                logger.warning(
                    "Mic queue full; dropped {} audio chunk(s) (depth={}/{})",
                    self._dropped_chunks,
                    self._queue.qsize(),
                    self._queue.maxsize,
                )

        # Visualizer/input-warning calculation is capped to UI frame rate. The
        # raw audio still flows downstream on every callback.
        if self.on_audio_level:
            try:
                now = time.monotonic()
                if now - self._last_audio_level_at < self._audio_level_interval:
                    return
                self._last_audio_level_at = now
                # Optimized RMS: use int16 view directly, compute in float32 for speed
                # Use the exact frame we send downstream (after channel selection/downmix).
                samples = np.asarray(output_data).astype(np.int16, copy=False).ravel()
                # Use float32 for faster computation than float64
                rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0

                # Speech-focused gating (dynamic noise floor + hysteresis)
                db = 20.0 * float(np.log10(rms + 1e-6))

                # Update noise floor: quick to drop, very slow to rise (avoid "locking out" speech)
                if (not self._speech_active) or (db < self._noise_floor_db + 3.0):
                    if db < self._noise_floor_db:
                        self._noise_floor_db = self._noise_floor_db * 0.8 + db * 0.2
                    elif db <= self._noise_floor_db + 1.0:
                        self._noise_floor_db = self._noise_floor_db * 0.98 + db * 0.02

                # Lower thresholds for responsive visualization
                threshold_high = max(self._noise_floor_db + 6.0, -58.0)
                threshold_low = threshold_high - 8.0
                abs_on_rms = 0.0007
                abs_off_rms = 0.00025

                if db >= threshold_high or rms >= abs_on_rms:
                    self._speech_active = True
                    self._speech_hold_until = now + 0.45
                elif (
                    (db <= threshold_low and rms <= abs_off_rms)
                    and now >= self._speech_hold_until
                ):
                    self._speech_active = False

                # Keep visualization continuous across syllables.
                rms_value = float(rms)
                self._last_observed_rms = rms_value
                self._max_observed_rms = max(self._max_observed_rms, rms_value)
                self._audio_level_sample_count += 1
                if self._speech_active:
                    self._speech_observed = True
                    self._speech_observed_count += 1
                    self._last_speech_at = now
                vis_target = float(rms) if self._speech_active else max(0.0, rms * 0.10)
                if vis_target > self._visual_level:
                    self._visual_level = self._visual_level * 0.25 + vis_target * 0.75
                else:
                    self._visual_level = self._visual_level * 0.70 + vis_target * 0.30
                self.on_audio_level(self._visual_level)
            except Exception:
                pass

    def diagnostic_snapshot(self) -> dict:
        stream = self.stream
        source_snapshot = (
            self._frame_source.diagnostic_snapshot()
            if self._frame_source is not None
            else {
                "engine": self._audio_engine,
                "frameSource": self._frame_source_name,
                "hasStream": bool(stream),
                "streamActive": bool(stream and getattr(stream, "active", False)),
            }
        )
        rust_circuit = _rust_audio_fallback_circuit_state()
        return {
            "running": bool(self._running),
            "engine": self._audio_engine,
            "requestedEngine": self._requested_audio_engine,
            "frameSource": self._frame_source_name,
            "engineFallbackReason": self._audio_engine_fallback_reason,
            "hasStream": bool(stream),
            "streamActive": bool(stream and getattr(stream, "active", False)),
            "usingPrewarmStream": False,
            "prewarmAdoptionSkippedReason": self._prewarm_adoption_skipped_reason,
            "rustPrewarmAdoption": self._redacted_rust_prewarm_adoption(),
            "streamClaimed": bool(self._stream_claimed),
            "sampleRate": int(self._target_sample_rate),
            "targetChannels": int(self._target_channels),
            "captureChannels": int(self._capture_channels),
            "blockSize": int(self.block_size),
            "device": str(self.device),
            "callbackCount": int(self._callback_count),
            "droppedFrameCount": int(self._dropped_chunks),
            "drainedFrameCount": int(self._drained_chunks),
            "audioQueueDepth": int(self._queue.qsize()),
            "audioQueueCapacity": int(self._queue.maxsize),
            "audioQueueMaxDepth": int(self._max_queue_depth),
            "consumerTaskState": self._consumer_task_state(),
            "consumerError": self._consumer_error,
            "lastObservedRms": float(self._last_observed_rms),
            "maxObservedRms": float(self._max_observed_rms),
            "audioLevelSampleCount": int(self._audio_level_sample_count),
            "speechObserved": bool(self._speech_observed),
            "speechObservedCount": int(self._speech_observed_count),
            "lastSpeechAgoSeconds": (
                round(time.monotonic() - self._last_speech_at, 3)
                if self._last_speech_at > 0
                else None
            ),
            "source": source_snapshot,
            "streamStartedAgoSeconds": (
                round(time.monotonic() - self._stream_started_at, 3)
                if self._stream_started_at > 0
                else None
            ),
            "lastCallbackAgoSeconds": (
                round(time.monotonic() - self._last_callback_at, 3)
                if self._last_callback_at > 0
                else None
            ),
            "lastStatus": self._last_status,
            "lastCallbackException": self._last_callback_exception,
            "healthRestartCount": int(self._health_restart_count),
            "healthRestartThrottleCount": int(self._health_restart_throttle_count),
            "lastHealthCheckReason": self._last_health_check_reason,
            "lastHealthFailureReason": self._last_health_failure_reason,
            "lastHealthRestartReason": self._last_health_restart_reason,
            "lastHealthRestartError": self._last_health_restart_error,
            "lastHealthRestartThrottledReason": self._last_health_restart_throttled_reason,
            "lastHealthRestartThrottleRemainingSeconds": self._last_health_restart_throttle_remaining_seconds,
            "lastRustAudioMidSessionFailureReason": self._last_rust_audio_mid_session_failure_reason,
            "rustAudioFallbackCircuitOpen": bool(rust_circuit.get("open")),
            "rustAudioFallbackCircuitReason": str(rust_circuit.get("reason") or ""),
            "rustAudioFallbackCircuitRemainingSeconds": rust_circuit.get("remainingSeconds"),
        }

    def _redacted_rust_prewarm_adoption(self) -> dict | None:
        if not isinstance(self._rust_prewarm_adoption, dict):
            return None
        payload = dict(self._rust_prewarm_adoption)
        payload.setdefault("adopted", True)
        for key in ("prewarmId", "prewarm_id"):
            if key in payload:
                payload[f"{key}Hash"] = _hash_private_hint(str(payload.pop(key) or ""))
        return payload

    def ensure_stream_health(
        self,
        *,
        reason: str = "watchdog",
        max_callback_gap_seconds: float | None = None,
        min_restart_interval_seconds: float = 15.0,
    ) -> bool:
        if not self._running:
            return True
        self._last_health_check_reason = str(reason or "watchdog")

        stream = self.stream
        active = bool(stream and getattr(stream, "active", False))
        if stream and not active:
            self._release_active_stream()
        now = time.monotonic()
        callback_stale = (
            max_callback_gap_seconds is not None
            and (
                (
                    self._last_callback_at > 0
                    and now - self._last_callback_at > max_callback_gap_seconds
                )
                or (
                    self._last_callback_at <= 0
                    and self._stream_started_at > 0
                    and now - self._stream_started_at > max_callback_gap_seconds
                )
            )
        )

        if active and not callback_stale:
            return True
        if not stream:
            self._last_health_failure_reason = "missingStream"
            logger.warning(f"Mic watchdog found missing capture stream while recording ({reason})")
            return False
        if not active:
            health_failure_reason = "inactiveStream"
        elif self._last_callback_at <= 0:
            health_failure_reason = "noCallbacksAfterStart"
        else:
            health_failure_reason = "staleCallbacks"
        self._last_health_failure_reason = health_failure_reason
        if now - self._last_health_restart_at < min_restart_interval_seconds:
            elapsed = max(0.0, now - self._last_health_restart_at)
            remaining = max(0.0, float(min_restart_interval_seconds) - elapsed)
            self._health_restart_throttle_count += 1
            self._last_health_restart_throttled_reason = (
                f"{reason or 'watchdog'}:{health_failure_reason}"
            )
            self._last_health_restart_throttle_remaining_seconds = round(remaining, 3)
            logger.debug(
                "Microphone capture stream restart throttled "
                f"({reason}, failure={health_failure_reason}, remaining={remaining:.3f}s)"
            )
            return False

        self._last_health_restart_at = now
        source_owns_stream = self._source_owns_stream()
        try:
            with get_device_guard_lock():
                if source_owns_stream and (callback_stale or not active):
                    mid_session_failure = str(
                        getattr(self._frame_source, "mid_session_failure_reason", "") or ""
                    )
                    if (
                        mid_session_failure
                        and getattr(self._frame_source, "engine", "") == "rust-wasapi"
                    ):
                        self._last_rust_audio_mid_session_failure_reason = mid_session_failure
                        _record_rust_audio_mid_session_failure(mid_session_failure)
                        self._audio_engine_fallback_reason = (
                            f"rustWasapiMidSessionFailure:{mid_session_failure}"
                        )
                        logger.warning(
                            "Rust audio mid-session failure recorded; "
                            f"next recording will fail fast during cooldown ({mid_session_failure})"
                        )
                    try:
                        self._frame_source.stop(close=False)
                    except Exception:
                        pass
                elif active and callback_stale:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                if source_owns_stream:
                    self._frame_source.start()
                    self._sync_frame_source_state()
                    stream = self.stream
                else:
                    stream.start()
                self._stream_started_at = time.monotonic()
                self._last_callback_at = 0.0
                self._claim_active_stream()
            self._health_restart_count += 1
            self._last_health_restart_reason = str(reason or "watchdog")
            self._last_health_restart_error = ""
            self._last_health_restart_throttled_reason = ""
            self._last_health_restart_throttle_remaining_seconds = None
            logger.warning(
                "Microphone capture stream restarted "
                f"({reason}, failure={health_failure_reason}, was_active={active}, "
                f"stale_callbacks={callback_stale})"
            )
            return bool(getattr(stream, "active", False))
        except Exception as exc:
            self._last_callback_exception = str(exc)
            self._last_health_restart_error = str(exc)
            if not bool(getattr(stream, "active", False)):
                self._release_active_stream()
            logger.warning(f"Microphone capture stream restart failed ({reason}): {exc}")
            return False

    def force_stop_from_external_error(self, *, reason: str = "external_error") -> None:
        logger.warning(f"Microphone capture stopped by pipeline error ({reason})")
        self._running = False
        with get_device_guard_lock():
            if self.stream:
                try:
                    if self._source_owns_stream():
                        self._frame_source.stop(close=False)
                        self._sync_frame_source_state()
                    else:
                        self.stream.stop()
                except Exception as exc:
                    logger.debug(f"Microphone stream stop after pipeline error failed: {exc}")
            self._release_active_stream()

    def _signal_queue_wakeup(self) -> None:
        loop = self._consumer_loop
        wakeup = self._queue_wakeup
        if loop is None or wakeup is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wakeup.set)
        except RuntimeError:
            pass

    def _consumer_task_state(self) -> str:
        task = self._consumer_task
        if task is None:
            return self._consumer_last_state
        if task.cancelled():
            return "cancelled"
        if not task.done():
            return "running"
        try:
            return "failed" if task.exception() is not None else "completed"
        except asyncio.CancelledError:
            return "cancelled"

    def _on_consumer_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            self._consumer_last_state = "cancelled"
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            self._consumer_last_state = "cancelled"
            return
        if error is None:
            self._consumer_last_state = "completed"
            return
        self._consumer_last_state = "failed"
        self._consumer_error = f"{type(error).__name__}: {error}"
        logger.error("Microphone audio queue consumer failed: {}", self._consumer_error)

    async def _next_queued_audio(self):
        while True:
            try:
                return self._queue.get_nowait()
            except _queue.Empty:
                if not self._running:
                    return None

                wakeup = self._queue_wakeup
                if wakeup is None:
                    await asyncio.sleep(0)
                    continue

                # Clear first, then check the queue again so a producer cannot
                # race between the empty check and the wait.
                wakeup.clear()
                try:
                    return self._queue.get_nowait()
                except _queue.Empty:
                    await wakeup.wait()

    async def _drain_queue(self):
        # Ensure audio queue exists (BaseInputTransport creates it in _create_audio_task)
        if not hasattr(self, "_audio_in_queue") or self._audio_in_queue is None:
            self._create_audio_task()
        # Wait for queue to be available and start frame delivered downstream
        while not hasattr(self, "_audio_in_queue") or self._audio_in_queue is None:
            await asyncio.sleep(0.01)

        try:
            while True:
                data = await self._next_queued_audio()
                if data is None:
                    break
                try:
                    frame = InputAudioRawFrame(
                        audio=data,
                        sample_rate=self._target_sample_rate,
                        num_channels=self._target_channels,
                    )
                    await self.push_audio_frame(frame)
                    self._drained_chunks += 1
                finally:
                    self._queue.task_done()
            self._notify_last_audio_chunk_sent()
        except asyncio.CancelledError:
            # Clean up audio stream on cancellation
            self._running = False
            with get_device_guard_lock():
                if self.stream:
                    try:
                        if self._source_owns_stream():
                            self._frame_source.stop(close=True)
                            self._sync_frame_source_state()
                        else:
                            self.stream.stop()
                            self.stream.close()
                            self.stream = None
                    except Exception:
                        pass
                self._release_active_stream()
            raise  # Re-raise to properly complete cancellation

    def _notify_last_audio_chunk_sent(self) -> None:
        if self._last_audio_chunk_sent_notified:
            return
        self._last_audio_chunk_sent_notified = True
        if not self.on_last_audio_chunk_sent:
            return
        try:
            self.on_last_audio_chunk_sent()
        except Exception as exc:
            logger.debug(f"Microphone last-chunk callback failed: {exc}")

    async def stop(self, frame: EndFrame, *, close_stream: bool | None = None):
        await self.stop_capture_for_finalization(close_stream=close_stream)
        await super().stop(frame)

    async def stop_capture_for_finalization(self, *, close_stream: bool | None = None):
        """Stop the physical capture source while leaving downstream frames alive.

        Segmented STT providers need a clean "no more microphone frames" point
        before Scriber emits the synthetic UserStoppedSpeakingFrame that triggers
        upload/transcription. Calling BaseInputTransport.stop() here would cancel
        Pipecat's audio task and can race the final STT frame, so final pipeline
        termination remains the caller's responsibility.
        """
        self._running = False
        self._signal_queue_wakeup()

        # OPTIMIZED: Always stop stream to prevent CPU usage and buffer overflow
        # With keep_alive: pause stream (fast restart via stream.start())
        # Without keep_alive: close stream entirely
        should_close_stream = (not self.keep_alive) if close_stream is None else bool(close_stream)
        with get_device_guard_lock():
            if self.stream:
                try:
                    if self._source_owns_stream():
                        self._frame_source.stop(close=should_close_stream)
                        self._sync_frame_source_state()
                    else:
                        self.stream.stop()  # Stops callbacks, saves CPU, prevents overflow
                        if should_close_stream:
                            self.stream.close()
                            self.stream = None
                except Exception:
                    pass
            self._release_active_stream()

        # Wait for consumer task with timeout
        if self._consumer_task:
            task, self._consumer_task = self._consumer_task, None
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception as exc:
                if not self._consumer_error:
                    self._consumer_error = f"{type(exc).__name__}: {exc}"
                logger.debug(f"Microphone queue consumer cleanup warning: {exc}")

        self._stopped.set()
