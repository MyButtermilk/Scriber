from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable
from uuid import UUID, uuid4

from src.core.provider_audio_formats import SPEECHMATICS_BATCH_DEFAULT_BASE_URL
from src.runtime.ffmpeg_commands import mp3_encode_pcm_pipe_args
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import hidden_subprocess_kwargs, read_stream_limited


PROVIDER_REPLAY_RUN_ID_ENV = "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID"
PROVIDER_REPLAY_FIXTURE_DURATION_MS_ENV = (
    "SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_DURATION_MS"
)
PROVIDER_REPLAY_FIXTURE_PCM_SHA256_ENV = (
    "SCRIBER_B7_PROVIDER_REPLAY_FIXTURE_PCM_SHA256"
)
PROVIDER_REPLAY_FIXTURE_PCM_PATH_ENV = (
    "SCRIBER_RUST_AUDIO_SYNTHETIC_MIC_PCM_S16LE_48000_MONO_PATH"
)
PROVIDER_REPLAY_RUNTIME_MODE = "tauri-supervised"
PROVIDER_REPLAY_LAUNCH_KIND = "sidecar"
PROVIDER_REPLAY_PARENT_EXE = "scriber-desktop.exe"
PROVIDER_REPLAY_CONTRACT_VERSION = 1
PROVIDER_REPLAY_DEFAULT_TTL_SECONDS = 60.0
PROVIDER_REPLAY_MAX_ENTRIES = 256
PROVIDER_REPLAY_PROVIDERS = frozenset({"microsoft", "soniox", "speechmatics"})
PROVIDER_REPLAY_DEFAULT_FIXTURE_DURATION_MS = 350
PROVIDER_REPLAY_MAX_FIXTURE_DURATION_MS = 600_000
PROVIDER_REPLAY_AZURE_REGION = "northeurope"
_AZURE_REPLAY_INPUT_SAMPLE_RATE = 48_000
_AZURE_REPLAY_OUTPUT_SAMPLE_RATE = 16_000
_AZURE_REPLAY_REFERENCE_CACHE_MAX_BYTES = 48 * 1024 * 1024
_AZURE_REPLAY_REFERENCE_CACHE: dict[
    tuple[str, int, str, int], bytes
] = {}
_AZURE_REPLAY_REFERENCE_CACHE_BYTES = 0
_AZURE_REPLAY_REFERENCE_CACHE_LOCK = threading.Lock()
_AZURE_REPLAY_URL = (
    f"https://{PROVIDER_REPLAY_AZURE_REGION}.api.cognitive.microsoft.com/"
    "speechtotext/transcriptions:transcribe?api-version=2025-10-15"
)
_AZURE_REPLAY_DEFINITION = {
    "enhancedMode": {
        "enabled": True,
        "model": "mai-transcribe-1.5",
    },
    "locales": ["en-US"],
}

# These phrases are product-owned, immutable benchmark fixtures. The control
# plane never accepts transcript text from a caller. Keeping one fixed phrase
# per provider also lets the external observer prove the visible suffix without
# sending user content through the installed backend.
_PROVIDER_REPLAY_TEXT = {
    "microsoft": "Scriber deterministic Microsoft provider replay.",
    "soniox": "Scriber deterministic Soniox provider replay.",
    "speechmatics": "Scriber deterministic Speechmatics provider replay.",
}

_MARKER_SOURCES = {
    "activation_received": "tauri_activation_boundary",
    "hotkey_received": "tauri_global_shortcut",
    "button_received": "tauri_ui_command",
    "recording_state_visible": "installed_backend_state_event",
    "stop_requested": "installed_backend_stop_event",
    "provider_response_complete": "installed_backend_provider_event",
    "last_final_token_received": "installed_backend_provider_event",
    "recording_state_transcribing_emitted": "installed_backend_state_event",
    "session_finished_emitted": "installed_backend_session_event",
    "clipboard_set": "installed_backend_injector_event",
    "paste": "installed_backend_injector_event",
    "injection_callback_completed": "installed_backend_injector_event",
}

_PROVIDER_REPLAY_ERROR_CODES = frozenset(
    {
        "arm_failed",
        "capture_timeout",
        "expired",
        "pipeline_failed",
        "provider_failed",
        "shutdown",
        "target_mismatch",
    }
)

_CAPTURE_ATTESTATION_READER_FIELDS = (
    "fixturePcmSha256",
    "capturedPcmSha256",
    "sampleRate",
    "channels",
    "sampleWidthBytes",
    "fixturePayloadBytesRead",
    "fixtureAudioFramesRead",
    "payloadBytesRead",
    "audioFramesRead",
    "trailingZeroFrames",
    "expectedTrailingZeroFrames",
    "captureBlockSizeFrames",
    "exactFixtureEndAccepted",
    "eosFramesRead",
    "eosObserved",
    "sidecarEosWritten",
    "droppedFrameCount",
    "sequenceErrorCount",
    "protocolErrorCount",
    "prebufferAfterLiveCount",
    "readerEndReason",
    "tailKind",
    "fixturePrefixMatched",
    "tailAllZero",
)

if sys.platform == "win32":
    _QPC_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _QPC_COUNTER = _QPC_KERNEL32.QueryPerformanceCounter
    _QPC_COUNTER.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
    _QPC_COUNTER.restype = ctypes.c_int
    _QPC_FREQUENCY = _QPC_KERNEL32.QueryPerformanceFrequency
    _QPC_FREQUENCY.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
    _QPC_FREQUENCY.restype = ctypes.c_int
else:
    _QPC_COUNTER = None
    _QPC_FREQUENCY = None


class ProviderReplayError(RuntimeError):
    """Base class for fail-closed benchmark replay state errors."""


class ProviderReplayDisabled(ProviderReplayError):
    pass


class ProviderReplayNotFound(ProviderReplayError):
    pass


class ProviderReplayConflict(ProviderReplayError):
    pass


class ProviderReplayCapacityError(ProviderReplayError):
    pass


def canonical_replay_uuid(value: str | None) -> str | None:
    try:
        parsed = UUID(str(value or "").strip())
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.int == 0:
        return None
    return parsed.hex


def _process_generation_fingerprint(
    *,
    backend_pid: int,
    backend_creation_time_100ns: int,
    parent_pid: int,
    parent_creation_time_100ns: int,
) -> str:
    material = (
        "scriber-provider-replay-process-v1\0"
        f"{backend_pid}\0{backend_creation_time_100ns}\0"
        f"{parent_pid}\0{parent_creation_time_100ns}"
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _windows_process_identity(process_id: int) -> tuple[str, int] | None:
    """Return basename and creation FILETIME for a live Windows process."""

    if sys.platform != "win32" or process_id <= 0:
        return None

    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32_768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        creation_100ns = (int(creation.dwHighDateTime) << 32) | int(
            creation.dwLowDateTime
        )
        if creation_100ns <= 0:
            return None
        return Path(buffer.value).name.casefold(), creation_100ns
    finally:
        kernel32.CloseHandle(handle)


@dataclass(frozen=True)
class ProviderReplayRuntimeGate:
    enabled: bool
    reason: str
    run_id: str | None = None
    process_generation_fingerprint: str | None = None

    @classmethod
    def disabled(cls, reason: str) -> "ProviderReplayRuntimeGate":
        return cls(enabled=False, reason=reason)

    @classmethod
    def evaluate(
        cls,
        *,
        raw_run_id: str | None,
        frozen: bool,
        runtime_mode: str | None,
        launch_kind: str | None,
        platform: str,
        backend_pid: int,
        backend_creation_time_100ns: int | None,
        parent_pid: int,
        parent_executable_name: str | None,
        parent_creation_time_100ns: int | None,
    ) -> "ProviderReplayRuntimeGate":
        run_id = canonical_replay_uuid(raw_run_id)
        if run_id is None:
            return cls.disabled("run_id_missing_or_invalid")
        if not frozen:
            return cls.disabled("backend_not_frozen")
        if runtime_mode != PROVIDER_REPLAY_RUNTIME_MODE:
            return cls.disabled("runtime_mode_not_tauri_supervised")
        if launch_kind != PROVIDER_REPLAY_LAUNCH_KIND:
            return cls.disabled("launch_kind_not_sidecar")
        if platform != "win32":
            return cls.disabled("platform_not_windows")
        if parent_pid <= 0 or backend_pid <= 0:
            return cls.disabled("process_identity_unavailable")
        if str(parent_executable_name or "").casefold() != PROVIDER_REPLAY_PARENT_EXE:
            return cls.disabled("direct_parent_not_scriber_desktop")
        if not backend_creation_time_100ns or not parent_creation_time_100ns:
            return cls.disabled("process_generation_unavailable")
        fingerprint = _process_generation_fingerprint(
            backend_pid=backend_pid,
            backend_creation_time_100ns=backend_creation_time_100ns,
            parent_pid=parent_pid,
            parent_creation_time_100ns=parent_creation_time_100ns,
        )
        return cls(
            enabled=True,
            reason="enabled",
            run_id=run_id,
            process_generation_fingerprint=fingerprint,
        )

    @classmethod
    def from_environment(cls) -> "ProviderReplayRuntimeGate":
        raw_run_id = os.getenv(PROVIDER_REPLAY_RUN_ID_ENV)
        if canonical_replay_uuid(raw_run_id) is None:
            return cls.disabled("run_id_missing_or_invalid")
        if not bool(getattr(sys, "frozen", False)):
            return cls.disabled("backend_not_frozen")
        if os.getenv("SCRIBER_RUNTIME_MODE") != PROVIDER_REPLAY_RUNTIME_MODE:
            return cls.disabled("runtime_mode_not_tauri_supervised")
        if os.getenv("SCRIBER_BACKEND_LAUNCH_KIND") != PROVIDER_REPLAY_LAUNCH_KIND:
            return cls.disabled("launch_kind_not_sidecar")
        if sys.platform != "win32":
            return cls.disabled("platform_not_windows")
        backend_pid = os.getpid()
        parent_pid = os.getppid()
        backend_identity = _windows_process_identity(backend_pid)
        parent_identity = _windows_process_identity(parent_pid)
        return cls.evaluate(
            raw_run_id=raw_run_id,
            frozen=bool(getattr(sys, "frozen", False)),
            runtime_mode=os.getenv("SCRIBER_RUNTIME_MODE"),
            launch_kind=os.getenv("SCRIBER_BACKEND_LAUNCH_KIND"),
            platform=sys.platform,
            backend_pid=backend_pid,
            backend_creation_time_100ns=(
                backend_identity[1] if backend_identity is not None else None
            ),
            parent_pid=parent_pid,
            parent_executable_name=(
                parent_identity[0] if parent_identity is not None else None
            ),
            parent_creation_time_100ns=(
                parent_identity[1] if parent_identity is not None else None
            ),
        )


def windows_qpc_snapshot() -> tuple[int, int]:
    """Read the system-wide Windows QPC clock used by external B7 observers."""

    if _QPC_COUNTER is None or _QPC_FREQUENCY is None:
        raise ProviderReplayDisabled("windows_qpc_unavailable")
    ticks = ctypes.c_longlong()
    frequency = ctypes.c_longlong()
    if not _QPC_COUNTER(ctypes.byref(ticks)):
        raise ProviderReplayDisabled("windows_qpc_counter_failed")
    if not _QPC_FREQUENCY(ctypes.byref(frequency)):
        raise ProviderReplayDisabled("windows_qpc_frequency_failed")
    if ticks.value <= 0 or frequency.value <= 0:
        raise ProviderReplayDisabled("windows_qpc_invalid")
    return int(ticks.value), int(frequency.value)


def provider_replay_fixture_text(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    try:
        return _PROVIDER_REPLAY_TEXT[normalized]
    except KeyError as exc:
        raise ValueError("unsupported provider replay provider") from exc


def provider_replay_fixture_sha256(provider: str) -> str:
    return hashlib.sha256(provider_replay_fixture_text(provider).encode("utf-8")).hexdigest()


def provider_replay_fixture_duration_ms_from_environment() -> int:
    """Return bounded, non-content metadata for the installed replay fixture."""

    raw = os.getenv(PROVIDER_REPLAY_FIXTURE_DURATION_MS_ENV, "").strip()
    if not raw:
        return PROVIDER_REPLAY_DEFAULT_FIXTURE_DURATION_MS
    try:
        duration_ms = int(raw)
    except ValueError:
        return PROVIDER_REPLAY_DEFAULT_FIXTURE_DURATION_MS
    if not 100 <= duration_ms <= PROVIDER_REPLAY_MAX_FIXTURE_DURATION_MS:
        return PROVIDER_REPLAY_DEFAULT_FIXTURE_DURATION_MS
    return duration_ms


def create_azure_mai_replay_transport(
    *,
    provider: str = "microsoft",
    authoritative_fixture_duration_ms: int,
    expected_fixture_pcm_sha256: str,
    authoritative_fixture_pcm_path: str | Path,
    capture_block_size_frames: int,
    expected_audio_preparation_implementation: str,
    on_audio_preparation_validated: Callable[[str], None] | None = None,
) -> Callable[..., Any]:
    """Return a deterministic local raw transport for the real MAI parser.

    The transport consumes the MP3 produced by ``AzureMaiTranscribeSTTService``
    and returns a realistic raw provider payload. It never performs network I/O
    and intentionally does not emit the provider-complete marker; that marker
    belongs to the MAI adapter boundary after this await returns.
    """

    duration_ms = int(authoritative_fixture_duration_ms)
    if not 100 <= duration_ms <= PROVIDER_REPLAY_MAX_FIXTURE_DURATION_MS:
        raise ValueError("provider replay fixture duration is out of bounds")
    expected_fixture_pcm_sha256 = str(expected_fixture_pcm_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_fixture_pcm_sha256):
        raise ValueError("provider replay fixture PCM digest is invalid")
    raw_fixture_pcm_path = str(authoritative_fixture_pcm_path or "").strip()
    if not raw_fixture_pcm_path:
        raise ValueError("provider replay fixture PCM path is missing")
    fixture_pcm_path = Path(raw_fixture_pcm_path)
    capture_block_size = _provider_replay_capture_block_size_frames(
        capture_block_size_frames
    )
    expected_audio_preparation_implementation = str(
        expected_audio_preparation_implementation or ""
    ).strip()
    if expected_audio_preparation_implementation not in {
        "post_stop_ffmpeg_mp3_v1",
        "capture_time_ffmpeg_mp3_v1",
    }:
        raise ValueError("provider replay MAI preparation is invalid")

    text = provider_replay_fixture_text(provider)
    raw_payload = json.dumps(
        {
            "combinedPhrases": [{"text": text}],
            "phrases": [
                {
                    "text": text,
                    "locale": "en-US",
                    "confidence": 1.0,
                }
            ],
        },
        separators=(",", ":"),
    )
    used = False

    async def _transport(
        *,
        session: Any,
        url: str,
        audio_source: bytes | BinaryIO,
        filename: str,
        content_type: str,
        definition: dict[str, Any],
        speech_key: str,
        timeout_secs: float,
        audio_preparation_implementation: str | None = None,
    ) -> tuple[int, str]:
        nonlocal used
        if used:
            raise RuntimeError("provider replay MAI transport is one-shot")
        used = True
        del session
        if url != _AZURE_REPLAY_URL:
            raise RuntimeError("provider replay MAI adapter URL mismatch")
        if speech_key != "local-replay":
            raise RuntimeError("provider replay MAI credential sentinel mismatch")
        if filename != "audio.mp3" or content_type != "audio/mpeg":
            raise RuntimeError("provider replay MAI audio contract mismatch")
        if definition != _AZURE_REPLAY_DEFINITION:
            raise RuntimeError("provider replay MAI definition mismatch")
        if not isinstance(timeout_secs, (int, float)) or timeout_secs <= 0:
            raise RuntimeError("provider replay MAI timeout is invalid")
        if audio_preparation_implementation != expected_audio_preparation_implementation:
            raise RuntimeError("provider replay MAI audio preparation mismatch")
        fixture_pcm = await asyncio.to_thread(
            _load_authoritative_azure_replay_pcm,
            fixture_pcm_path,
            authoritative_fixture_duration_ms=duration_ms,
            expected_fixture_pcm_sha256=expected_fixture_pcm_sha256,
        )
        padded_duration_ms = duration_ms + _azure_replay_block_tail_duration_ms(
            duration_ms,
            capture_block_size,
        )
        maximum_mp3_bytes = max(
            256 * 1024,
            int(
                (padded_duration_ms / 1000.0)
                * 32
                * 1024
            )
            + 64 * 1024,
        )
        audio = await asyncio.to_thread(
            _read_azure_replay_mp3_fully,
            audio_source,
            maximum_bytes=maximum_mp3_bytes,
        )
        ffmpeg = await asyncio.to_thread(require_media_tool, "ffmpeg")
        await _validate_azure_replay_mp3(
            ffmpeg,
            audio,
            fixture_pcm=fixture_pcm,
            authoritative_fixture_duration_ms=duration_ms,
            expected_fixture_pcm_sha256=expected_fixture_pcm_sha256,
            capture_block_size_frames=capture_block_size,
            maximum_mp3_bytes=maximum_mp3_bytes,
        )
        if on_audio_preparation_validated is not None:
            on_audio_preparation_validated(audio_preparation_implementation)
        return 200, raw_payload

    return _transport


def _load_authoritative_azure_replay_pcm(
    path: Path,
    *,
    authoritative_fixture_duration_ms: int,
    expected_fixture_pcm_sha256: str,
) -> bytes:
    expected_frames = (
        _AZURE_REPLAY_INPUT_SAMPLE_RATE * authoritative_fixture_duration_ms // 1000
    )
    expected_bytes = expected_frames * 2
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_bytes:
            raise RuntimeError("provider replay MAI fixture PCM length mismatch")
        with path.open("rb") as source:
            payload = source.read(expected_bytes + 1)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("provider replay MAI fixture PCM is unavailable") from exc
    if len(payload) != expected_bytes:
        raise RuntimeError("provider replay MAI fixture PCM length mismatch")
    if not hmac.compare_digest(
        hashlib.sha256(payload).hexdigest(),
        expected_fixture_pcm_sha256,
    ):
        raise RuntimeError("provider replay MAI fixture PCM digest mismatch")
    return payload


def _provider_replay_capture_block_size_frames(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("provider replay capture block size is invalid")
    try:
        block_size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider replay capture block size is invalid") from exc
    if not 16 <= block_size <= 16_384:
        raise ValueError("provider replay capture block size is out of bounds")
    return block_size


def _azure_replay_block_tail_frames(
    authoritative_fixture_duration_ms: int,
    capture_block_size_frames: int,
) -> int:
    fixture_frames = (
        _AZURE_REPLAY_INPUT_SAMPLE_RATE
        * int(authoritative_fixture_duration_ms)
        // 1000
    )
    return (-fixture_frames) % int(capture_block_size_frames)


def _azure_replay_block_tail_duration_ms(
    authoritative_fixture_duration_ms: int,
    capture_block_size_frames: int,
) -> int:
    tail_frames = _azure_replay_block_tail_frames(
        authoritative_fixture_duration_ms,
        capture_block_size_frames,
    )
    return (
        tail_frames * 1000 + _AZURE_REPLAY_INPUT_SAMPLE_RATE - 1
    ) // _AZURE_REPLAY_INPUT_SAMPLE_RATE


def _read_azure_replay_mp3_fully(
    audio_source: bytes | BinaryIO,
    *,
    maximum_bytes: int,
) -> bytes:
    if isinstance(audio_source, (bytes, bytearray, memoryview)):
        payload = bytes(audio_source)
        if len(payload) > maximum_bytes:
            raise RuntimeError("provider replay MAI MP3 exceeds the bound")
    else:
        read = getattr(audio_source, "read", None)
        if not callable(read):
            raise RuntimeError("provider replay MAI audio source is invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = read(min(1024 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise RuntimeError("provider replay MAI audio read is invalid")
            rendered = bytes(chunk)
            total += len(rendered)
            if total > maximum_bytes:
                raise RuntimeError("provider replay MAI MP3 exceeds the bound")
            chunks.append(rendered)
        payload = b"".join(chunks)
    if not payload:
        raise RuntimeError("provider replay MAI audio is empty")
    return payload


async def _run_azure_replay_ffmpeg_pipe(
    command: list[str],
    input_payload: bytes,
    *,
    maximum_output_bytes: int,
    timeout_seconds: float,
    operation: str,
) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
    except OSError as exc:
        raise RuntimeError(
            f"provider replay MAI {operation} could not start"
        ) from exc
    if process.stdin is None or process.stdout is None or process.stderr is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise RuntimeError(f"provider replay MAI {operation} pipes are unavailable")

    output = bytearray()

    async def feed_input() -> None:
        assert process.stdin is not None
        for offset in range(0, len(input_payload), 1024 * 1024):
            process.stdin.write(input_payload[offset : offset + 1024 * 1024])
            await process.stdin.drain()
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()

    async def drain_output() -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(64 * 1024):
            output.extend(chunk)
            if len(output) > maximum_output_bytes:
                raise RuntimeError(
                    f"provider replay MAI {operation} output exceeds the bound"
                )

    feed_task = asyncio.create_task(feed_input())
    output_task = asyncio.create_task(drain_output())
    stderr_task = asyncio.create_task(
        read_stream_limited(process.stderr, max_bytes=1024 * 1024)
    )
    wait_task = asyncio.create_task(process.wait())
    tasks = (feed_task, output_task, stderr_task, wait_task)
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=max(1.0, min(120.0, float(timeout_seconds))),
        )
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise RuntimeError(f"provider replay MAI {operation} timed out") from exc
    except Exception as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"provider replay MAI {operation} failed") from exc
    if process.returncode != 0:
        raise RuntimeError(f"provider replay MAI {operation} failed")
    if not output:
        raise RuntimeError(f"provider replay MAI {operation} output is empty")
    return bytes(output)


def _azure_replay_reference_cache_get(
    key: tuple[str, int, str, int],
) -> bytes | None:
    with _AZURE_REPLAY_REFERENCE_CACHE_LOCK:
        return _AZURE_REPLAY_REFERENCE_CACHE.get(key)


def _azure_replay_reference_cache_put(
    key: tuple[str, int, str, int],
    value: bytes,
) -> None:
    global _AZURE_REPLAY_REFERENCE_CACHE_BYTES
    value_bytes = len(value)
    if value_bytes > _AZURE_REPLAY_REFERENCE_CACHE_MAX_BYTES:
        return
    with _AZURE_REPLAY_REFERENCE_CACHE_LOCK:
        if key in _AZURE_REPLAY_REFERENCE_CACHE:
            return
        if (
            _AZURE_REPLAY_REFERENCE_CACHE_BYTES + value_bytes
            > _AZURE_REPLAY_REFERENCE_CACHE_MAX_BYTES
        ):
            _AZURE_REPLAY_REFERENCE_CACHE.clear()
            _AZURE_REPLAY_REFERENCE_CACHE_BYTES = 0
        _AZURE_REPLAY_REFERENCE_CACHE[key] = value
        _AZURE_REPLAY_REFERENCE_CACHE_BYTES += value_bytes


async def _validate_azure_replay_mp3(
    ffmpeg: str,
    payload: bytes,
    *,
    fixture_pcm: bytes,
    authoritative_fixture_duration_ms: int,
    expected_fixture_pcm_sha256: str,
    capture_block_size_frames: int,
    maximum_mp3_bytes: int,
) -> None:
    duration_seconds = authoritative_fixture_duration_ms / 1000.0
    timeout_seconds = max(15.0, min(120.0, duration_seconds * 0.25 + 10.0))
    padded_duration_ms = (
        authoritative_fixture_duration_ms
        + _azure_replay_block_tail_duration_ms(
            authoritative_fixture_duration_ms,
            capture_block_size_frames,
        )
    )
    maximum_decoded_bytes = (
        int(
            _AZURE_REPLAY_OUTPUT_SAMPLE_RATE
            * padded_duration_ms
            / 1000
        )
        + 4096
    ) * 2
    decode_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mp3",
        "-i",
        "pipe:0",
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(_AZURE_REPLAY_OUTPUT_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    actual_decoded = await _run_azure_replay_ffmpeg_pipe(
        decode_command,
        payload,
        maximum_output_bytes=maximum_decoded_bytes,
        timeout_seconds=timeout_seconds,
        operation="MP3 decode",
    )
    if len(actual_decoded) % 2:
        raise RuntimeError("provider replay MAI decoded PCM is misaligned")

    expected_decoded = await _azure_replay_reference_decode(
        ffmpeg,
        fixture_pcm=fixture_pcm,
        authoritative_fixture_duration_ms=authoritative_fixture_duration_ms,
        expected_fixture_pcm_sha256=expected_fixture_pcm_sha256,
        capture_block_size_frames=capture_block_size_frames,
        maximum_mp3_bytes=maximum_mp3_bytes,
        maximum_decoded_bytes=maximum_decoded_bytes,
        timeout_seconds=timeout_seconds,
        decode_command=decode_command,
        allow_create=False,
    )
    if not hmac.compare_digest(actual_decoded, expected_decoded):
        shared_bytes = min(len(actual_decoded), len(expected_decoded))
        first_different_byte = next(
            (
                index
                for index in range(shared_bytes)
                if actual_decoded[index] != expected_decoded[index]
            ),
            shared_bytes,
        )
        # This branch is reachable only behind the private, frozen-runtime
        # provider-replay gate and only for its generated synthetic fixture.
        # Retain bounded digests and shape information so an installed replay
        # failure can distinguish truncation from an encoder regression without
        # persisting or logging audio bytes.
        raise RuntimeError(
            "provider replay MAI decoded fixture mismatch "
            f"(actualBytes={len(actual_decoded)}, "
            f"expectedBytes={len(expected_decoded)}, "
            f"firstDifferentByte={first_different_byte}, "
            f"actualSha256={hashlib.sha256(actual_decoded).hexdigest()}, "
            f"expectedSha256={hashlib.sha256(expected_decoded).hexdigest()})"
        )


async def _azure_replay_reference_decode(
    ffmpeg: str,
    *,
    fixture_pcm: bytes,
    authoritative_fixture_duration_ms: int,
    expected_fixture_pcm_sha256: str,
    capture_block_size_frames: int,
    maximum_mp3_bytes: int,
    maximum_decoded_bytes: int,
    timeout_seconds: float,
    decode_command: list[str],
    allow_create: bool,
) -> bytes:
    cache_key = (
        str(Path(ffmpeg).resolve()).casefold(),
        authoritative_fixture_duration_ms,
        expected_fixture_pcm_sha256,
        capture_block_size_frames,
    )
    references = _azure_replay_reference_cache_get(cache_key)
    if references is None and not allow_create:
        raise RuntimeError("provider replay MAI reference is not prewarmed")
    if references is None:
        encode_command = mp3_encode_pcm_pipe_args(
            ffmpeg,
            input_sample_rate=_AZURE_REPLAY_INPUT_SAMPLE_RATE,
            input_channels=1,
            bitrate="64k",
        )
        exact_tail_frames = _azure_replay_block_tail_frames(
            authoritative_fixture_duration_ms,
            capture_block_size_frames,
        )
        exact_mp3 = await _run_azure_replay_ffmpeg_pipe(
            encode_command,
            fixture_pcm + b"\0\0" * exact_tail_frames,
            maximum_output_bytes=maximum_mp3_bytes,
            timeout_seconds=timeout_seconds,
            operation="reference MP3 encode",
        )
        references = await _run_azure_replay_ffmpeg_pipe(
            decode_command,
            exact_mp3,
            maximum_output_bytes=maximum_decoded_bytes,
            timeout_seconds=timeout_seconds,
            operation="reference MP3 decode",
        )
        _azure_replay_reference_cache_put(cache_key, references)
    return references


async def prewarm_azure_mai_replay_validation(
    *,
    authoritative_fixture_duration_ms: int,
    expected_fixture_pcm_sha256: str,
    authoritative_fixture_pcm_path: str | Path,
    capture_block_size_frames: int,
) -> None:
    """Prepare deterministic MP3 references before the measured activation."""

    duration_ms = int(authoritative_fixture_duration_ms)
    if not 100 <= duration_ms <= PROVIDER_REPLAY_MAX_FIXTURE_DURATION_MS:
        raise ValueError("provider replay fixture duration is out of bounds")
    expected_sha256 = str(expected_fixture_pcm_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("provider replay fixture PCM digest is invalid")
    raw_fixture_path = str(authoritative_fixture_pcm_path or "").strip()
    if not raw_fixture_path:
        raise ValueError("provider replay fixture PCM path is missing")
    capture_block_size = _provider_replay_capture_block_size_frames(
        capture_block_size_frames
    )
    fixture_pcm = await asyncio.to_thread(
        _load_authoritative_azure_replay_pcm,
        Path(raw_fixture_path),
        authoritative_fixture_duration_ms=duration_ms,
        expected_fixture_pcm_sha256=expected_sha256,
    )
    ffmpeg = await asyncio.to_thread(require_media_tool, "ffmpeg")
    padded_duration_ms = duration_ms + _azure_replay_block_tail_duration_ms(
        duration_ms,
        capture_block_size,
    )
    maximum_mp3_bytes = max(
        256 * 1024,
        int(
            (padded_duration_ms / 1000.0)
            * 32
            * 1024
        )
        + 64 * 1024,
    )
    maximum_decoded_bytes = (
        int(
            _AZURE_REPLAY_OUTPUT_SAMPLE_RATE
            * padded_duration_ms
            / 1000
        )
        + 4096
    ) * 2
    timeout_seconds = max(
        15.0,
        min(120.0, (duration_ms / 1000.0) * 0.25 + 10.0),
    )
    decode_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "mp3",
        "-i",
        "pipe:0",
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(_AZURE_REPLAY_OUTPUT_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    await _azure_replay_reference_decode(
        ffmpeg,
        fixture_pcm=fixture_pcm,
        authoritative_fixture_duration_ms=duration_ms,
        expected_fixture_pcm_sha256=expected_sha256,
        capture_block_size_frames=capture_block_size,
        maximum_mp3_bytes=maximum_mp3_bytes,
        maximum_decoded_bytes=maximum_decoded_bytes,
        timeout_seconds=timeout_seconds,
        decode_command=decode_command,
        allow_create=True,
    )


def _read_binary_source_fully(
    audio_source: bytes | BinaryIO,
    *,
    maximum_bytes: int = 64 * 1024 * 1024 + 4096,
) -> bytes:
    if isinstance(audio_source, (bytes, bytearray, memoryview)):
        payload = bytes(audio_source)
        if len(payload) > maximum_bytes:
            raise RuntimeError("provider replay Speechmatics WAV exceeds the bound")
        return payload
    read = getattr(audio_source, "read", None)
    if not callable(read):
        raise RuntimeError("provider replay Speechmatics audio source is invalid")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = read(1024 * 1024)
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise RuntimeError("provider replay Speechmatics audio read is invalid")
        rendered = bytes(chunk)
        total += len(rendered)
        if total > maximum_bytes:
            raise RuntimeError("provider replay Speechmatics WAV exceeds the bound")
        chunks.append(rendered)
    return b"".join(chunks)


def _validate_speechmatics_replay_wav(
    payload: bytes,
    *,
    authoritative_fixture_duration_ms: int,
    expected_fixture_pcm_sha256: str,
    capture_block_size_frames: int,
) -> None:
    """Validate the complete capture artifact before returning provider JSON."""

    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise RuntimeError("provider replay Speechmatics WAV header is invalid")
    if struct.unpack_from("<I", payload, 4)[0] + 8 != len(payload):
        raise RuntimeError("provider replay Speechmatics RIFF length is invalid")

    fmt_chunk: bytes | None = None
    data_chunk: bytes | None = None
    position = 12
    while position < len(payload):
        if position + 8 > len(payload):
            raise RuntimeError("provider replay Speechmatics WAV chunk header is truncated")
        chunk_id = payload[position : position + 4]
        chunk_size = struct.unpack_from("<I", payload, position + 4)[0]
        chunk_start = position + 8
        chunk_end = chunk_start + chunk_size
        padded_end = chunk_end + (chunk_size & 1)
        if chunk_end > len(payload) or padded_end > len(payload):
            raise RuntimeError("provider replay Speechmatics WAV chunk is truncated")
        if chunk_id == b"fmt ":
            if fmt_chunk is not None:
                raise RuntimeError("provider replay Speechmatics WAV fmt is duplicated")
            fmt_chunk = payload[chunk_start:chunk_end]
        elif chunk_id == b"data":
            if data_chunk is not None:
                raise RuntimeError("provider replay Speechmatics WAV data is duplicated")
            data_chunk = payload[chunk_start:chunk_end]
        position = padded_end
    if position != len(payload) or fmt_chunk is None or data_chunk is None:
        raise RuntimeError("provider replay Speechmatics WAV chunks are incomplete")
    if len(fmt_chunk) < 16:
        raise RuntimeError("provider replay Speechmatics WAV fmt is truncated")
    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = (
        struct.unpack_from("<HHIIHH", fmt_chunk, 0)
    )
    if (
        audio_format != 1
        or channels != 1
        or sample_rate != 48_000
        or byte_rate != 96_000
        or block_align != 2
        or bits_per_sample != 16
    ):
        raise RuntimeError("provider replay Speechmatics PCM contract mismatch")
    if not data_chunk or len(data_chunk) % block_align:
        raise RuntimeError("provider replay Speechmatics PCM payload is invalid")

    fixture_byte_length = int(
        sample_rate * authoritative_fixture_duration_ms / 1000
    ) * block_align
    fixture_prefix = data_chunk[:fixture_byte_length]
    if len(fixture_prefix) != fixture_byte_length or not hmac.compare_digest(
        hashlib.sha256(fixture_prefix).hexdigest(),
        expected_fixture_pcm_sha256,
    ):
        raise RuntimeError("provider replay Speechmatics fixture prefix mismatch")
    trailing = data_chunk[fixture_byte_length:]
    expected_trailing_frames = (
        -(fixture_byte_length // block_align)
    ) % capture_block_size_frames
    if (
        len(trailing) != expected_trailing_frames * block_align
        or any(trailing)
    ):
        raise RuntimeError("provider replay Speechmatics fixture tail is invalid")


def _speechmatics_replay_json_v2(text: str, *, duration_ms: int) -> dict[str, Any]:
    words = text.removesuffix(".").split()
    duration_seconds = float(duration_ms) / 1000.0
    word_duration = duration_seconds / max(1, len(words))
    results: list[dict[str, Any]] = []
    for index, word in enumerate(words):
        start_time = round(index * word_duration, 3)
        end_time = round((index + 1) * word_duration, 3)
        results.append(
            {
                "type": "word",
                "start_time": start_time,
                "end_time": end_time,
                "alternatives": [
                    {
                        "content": word,
                        "confidence": 1.0,
                        "language": "en",
                    }
                ],
            }
        )
    results.append(
        {
            "type": "punctuation",
            "start_time": round(duration_seconds, 3),
            "end_time": round(duration_seconds, 3),
            "alternatives": [
                {"content": ".", "confidence": 1.0, "language": "en"}
            ],
        }
    )
    return {
        "format": "2.9",
        "job": {"id": "scriber-local-provider-replay", "status": "done"},
        "metadata": {
            "created_at": "2026-01-01T00:00:00.000Z",
            "transcription_config": {
                "language": "en",
                "operating_point": "enhanced",
            },
        },
        "results": results,
    }


def create_speechmatics_batch_replay_transport(
    *,
    authoritative_fixture_duration_ms: int,
    expected_fixture_pcm_sha256: str,
    capture_block_size_frames: int,
    expected_audio_preparation_implementation: str,
    on_audio_preparation_validated: Callable[[str], None] | None = None,
) -> Callable[..., Any]:
    """Return a one-shot, network-free transport for the real Batch-v2 adapter."""

    duration_ms = int(authoritative_fixture_duration_ms)
    if not 100 <= duration_ms <= PROVIDER_REPLAY_MAX_FIXTURE_DURATION_MS:
        raise ValueError("provider replay fixture duration is out of bounds")
    expected_fixture_pcm_sha256 = str(expected_fixture_pcm_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_fixture_pcm_sha256):
        raise ValueError("provider replay fixture PCM digest is invalid")
    capture_block_size = _provider_replay_capture_block_size_frames(
        capture_block_size_frames
    )
    expected_audio_preparation_implementation = str(
        expected_audio_preparation_implementation or ""
    ).strip()
    if expected_audio_preparation_implementation not in {
        "python_reserved_wav_header_v1",
        "wav_pcm16_file_v1",
    }:
        raise ValueError("provider replay Speechmatics preparation is invalid")
    response_payload = _speechmatics_replay_json_v2(
        provider_replay_fixture_text("speechmatics"),
        duration_ms=duration_ms,
    )
    used = False

    async def _transport(
        *,
        session: Any,
        base_url: str,
        api_key: str,
        audio_source: bytes | BinaryIO,
        filename: str,
        content_type: str,
        config: dict[str, Any],
        timeout_secs: float,
        poll_interval_secs: float,
        audio_preparation_implementation: str | None = None,
    ) -> dict[str, Any]:
        nonlocal used
        if used:
            raise RuntimeError("provider replay Speechmatics transport is one-shot")
        used = True
        del session
        if str(base_url).rstrip("/") != SPEECHMATICS_BATCH_DEFAULT_BASE_URL.rstrip("/"):
            raise RuntimeError("provider replay Speechmatics endpoint mismatch")
        if api_key != "local-replay":
            raise RuntimeError("provider replay Speechmatics credential sentinel mismatch")
        if filename != "audio.wav" or content_type != "audio/wav":
            raise RuntimeError("provider replay Speechmatics audio contract mismatch")
        if not isinstance(timeout_secs, (int, float)) or timeout_secs <= 0:
            raise RuntimeError("provider replay Speechmatics timeout is invalid")
        if not isinstance(poll_interval_secs, (int, float)) or poll_interval_secs <= 0:
            raise RuntimeError("provider replay Speechmatics poll interval is invalid")
        expected_config = {
            "type": "transcription",
            "transcription_config": {
                "language": "en",
                "operating_point": "enhanced",
            },
        }
        if config != expected_config:
            raise RuntimeError("provider replay Speechmatics config mismatch")
        if (
            audio_preparation_implementation
            != expected_audio_preparation_implementation
        ):
            raise RuntimeError(
                "provider replay Speechmatics audio preparation mismatch"
            )
        payload = await asyncio.to_thread(_read_binary_source_fully, audio_source)
        await asyncio.to_thread(
            _validate_speechmatics_replay_wav,
            payload,
            authoritative_fixture_duration_ms=duration_ms,
            expected_fixture_pcm_sha256=expected_fixture_pcm_sha256,
            capture_block_size_frames=capture_block_size,
        )
        if on_audio_preparation_validated is not None:
            on_audio_preparation_validated(audio_preparation_implementation)
        return response_payload

    return _transport


class _ObservedSonioxMessages:
    """Transparent receive-boundary observer around the real WebSocket."""

    def __init__(
        self,
        source: Any,
        *,
        final_message_sha256: str,
        on_last_final_token_received: Callable[[], None],
    ) -> None:
        self._iterator = source.__aiter__()
        self._final_message_sha256 = final_message_sha256
        self._callback = on_last_final_token_received
        self._observed = False

    def __aiter__(self) -> "_ObservedSonioxMessages":
        return self

    async def __anext__(self) -> Any:
        message = await self._iterator.__anext__()
        raw = message.encode("utf-8") if isinstance(message, str) else bytes(message)
        digest = hashlib.sha256(raw).hexdigest()
        if not self._observed and hmac.compare_digest(
            digest,
            self._final_message_sha256,
        ):
            # This executes in the installed client immediately after the
            # WebSocket yielded the final message and before Pipecat parses it.
            self._callback()
            self._observed = True
        return message


def install_soniox_replay_receive_observer(
    service: Any,
    *,
    final_message_sha256: str,
    on_last_final_token_received: Callable[[], None],
) -> None:
    """Observe a pinned final message without replacing Pipecat's parser."""

    if not re.fullmatch(r"[0-9a-f]{64}", str(final_message_sha256 or "")):
        raise ValueError("provider replay Soniox final message digest is invalid")
    if not callable(on_last_final_token_received):
        raise ValueError("provider replay Soniox receive callback is required")
    original_get_websocket = getattr(service, "_get_websocket", None)
    if not callable(original_get_websocket):
        raise RuntimeError("Soniox replay requires Pipecat's WebSocket service")

    def _observed_websocket() -> _ObservedSonioxMessages:
        return _ObservedSonioxMessages(
            original_get_websocket(),
            final_message_sha256=final_message_sha256,
            on_last_final_token_received=on_last_final_token_received,
        )

    # Keep the real service instance and parser. Only the async receive iterator
    # is decorated, so all provider messages still traverse Pipecat unchanged.
    service._get_websocket = _observed_websocket
    service._scriber_soniox_replay = True


class LocalSonioxReplayServer:
    """One-shot loopback implementation of Soniox's pinned WebSocket protocol."""

    def __init__(self) -> None:
        self.text = provider_replay_fixture_text("soniox")
        self._final_message = self._build_final_message(self.text)
        self.final_message_sha256 = hashlib.sha256(
            self._final_message.encode("utf-8")
        ).hexdigest()
        self.url: str | None = None
        self._server: Any | None = None
        self._connected = False
        self._closed = False
        self.error_code: str | None = None

    @staticmethod
    def _build_final_message(text: str) -> str:
        tokens: list[dict[str, Any]] = []
        offset_ms = 0
        words = text.split(" ")
        for index, word in enumerate(words):
            rendered = word if index == len(words) - 1 else f"{word} "
            end_ms = offset_ms + 100
            tokens.append(
                {
                    "text": rendered,
                    "is_final": True,
                    "start_ms": offset_ms,
                    "end_ms": end_ms,
                    "speaker": "1",
                    "language": "en",
                }
            )
            offset_ms = end_ms
        tokens.append({"text": "<end>", "is_final": True})
        return json.dumps(
            {"tokens": tokens, "finished": True},
            separators=(",", ":"),
        )

    async def start(self) -> "LocalSonioxReplayServer":
        if self._server is not None or self._closed:
            raise RuntimeError("provider replay Soniox server is not startable")
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handle_connection,
            "127.0.0.1",
            0,
            max_size=1 << 20,
            compression=None,
        )
        sockets = list(getattr(self._server, "sockets", ()) or ())
        if len(sockets) != 1:
            await self.close()
            raise RuntimeError("provider replay Soniox loopback bind failed")
        port = int(sockets[0].getsockname()[1])
        self.url = f"ws://127.0.0.1:{port}/transcribe-websocket"
        return self

    async def _handle_connection(self, websocket: Any) -> None:
        if self._connected:
            await websocket.close(code=1008, reason="one-shot replay")
            return
        self._connected = True
        try:
            raw_config = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            if not isinstance(raw_config, str):
                raise RuntimeError("provider replay Soniox configuration is not JSON")
            config = json.loads(raw_config)
            if not isinstance(config, dict):
                raise RuntimeError("provider replay Soniox configuration is invalid")
            if config.get("api_key") != "local-replay":
                raise RuntimeError("provider replay Soniox credential sentinel mismatch")
            if config.get("model") != "stt-rt-v5":
                raise RuntimeError("provider replay Soniox model mismatch")
            if config.get("audio_format") != "pcm_s16le":
                raise RuntimeError("provider replay Soniox audio format mismatch")

            audio_bytes = 0
            while True:
                message = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                if isinstance(message, (bytes, bytearray, memoryview)):
                    audio_bytes += len(message)
                    continue
                if message == "":
                    break
                if message in {
                    '{"type": "keepalive"}',
                    '{"type": "finalize"}',
                }:
                    continue
                raise RuntimeError("provider replay Soniox client message is invalid")
            if audio_bytes <= 0:
                raise RuntimeError("provider replay Soniox audio is empty")
            await websocket.send(self._final_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error_code = str(exc)[:160] or type(exc).__name__
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="replay protocol failure")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()


@dataclass
class _ProviderReplaySample:
    run_id: str
    sample_id: str
    provider: str
    state: str
    created_at_monotonic: float
    expires_at_monotonic: float
    session_id: str | None = None
    target_generation_sha256: str | None = None
    activation_kind: str | None = None
    markers: dict[str, dict[str, Any]] = field(default_factory=dict)
    capture_attestation: dict[str, Any] | None = None
    expected_audio_preparation_implementation: str | None = None
    actual_audio_preparation_implementation: str | None = None
    error_code: str | None = None


@dataclass
class ProviderReplayExecution:
    """Private installed-runtime context passed through the real controller."""

    registry: "ProviderReplayRegistry"
    run_id: str
    sample_id: str
    provider: str
    injection_target_guard: Any
    expected_audio_preparation_implementation: str | None = None
    azure_raw_transport: Callable[..., Any] | None = None
    speechmatics_batch_raw_transport: Callable[..., Any] | None = None
    soniox_server: LocalSonioxReplayServer | None = None
    session_id: str | None = None
    watchdog_task: asyncio.Task | None = None
    auto_stop_task: asyncio.Task | None = None
    authoritative_fixture_duration_ms: int = (
        PROVIDER_REPLAY_DEFAULT_FIXTURE_DURATION_MS
    )
    _closed: bool = False
    _pending_markers: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def soniox_url(self) -> str | None:
        return self.soniox_server.url if self.soniox_server is not None else None

    @property
    def soniox_final_message_sha256(self) -> str | None:
        return (
            self.soniox_server.final_message_sha256
            if self.soniox_server is not None
            else None
        )

    def bind_session(self, session_id: str) -> dict[str, Any]:
        result = self.registry.bind_session(
            run_id=self.run_id,
            sample_id=self.sample_id,
            session_id=session_id,
        )
        self.session_id = str(result["sessionId"])
        pending, self._pending_markers = self._pending_markers, []
        for marker, qpc_ticks, qpc_frequency in pending:
            self.registry.record_marker(
                run_id=self.run_id,
                sample_id=self.sample_id,
                session_id=self.session_id,
                marker=marker,
                qpc_ticks=qpc_ticks,
                qpc_frequency=qpc_frequency,
            )
        return result

    def marker(
        self,
        marker: str,
        *,
        qpc_snapshot: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        if self.session_id is None:
            if any(name == marker for name, _ticks, _frequency in self._pending_markers):
                raise ProviderReplayConflict("provider replay marker already recorded")
            ticks, frequency = (
                qpc_snapshot
                if qpc_snapshot is not None
                else self.registry.capture_marker_timestamp(marker)
            )
            self._pending_markers.append((marker, int(ticks), int(frequency)))
            return {
                "marker": marker,
                "qpcTicks": int(ticks),
                "qpcFrequency": int(frequency),
            }
        return self.registry.record_marker(
            run_id=self.run_id,
            sample_id=self.sample_id,
            session_id=self.session_id,
            marker=marker,
            qpc_ticks=(qpc_snapshot[0] if qpc_snapshot is not None else None),
            qpc_frequency=(qpc_snapshot[1] if qpc_snapshot is not None else None),
        )

    def fail(self, error_code: str) -> dict[str, Any] | None:
        with contextlib.suppress(ProviderReplayError):
            return self.registry.fail(
                run_id=self.run_id,
                sample_id=self.sample_id,
                error_code=error_code,
            )
        return None

    def attach_capture_attestation(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        if self.session_id is None:
            raise ProviderReplayConflict(
                "provider replay capture attestation requires a bound session"
            )
        return self.registry.record_capture_attestation(
            run_id=self.run_id,
            sample_id=self.sample_id,
            session_id=self.session_id,
            payload=payload,
        )

    def attach_audio_preparation_attestation(
        self,
        implementation: str,
    ) -> dict[str, Any]:
        if self.session_id is None:
            raise ProviderReplayConflict(
                "provider replay audio preparation requires a bound session"
            )
        return self.registry.record_audio_preparation_attestation(
            run_id=self.run_id,
            sample_id=self.sample_id,
            session_id=self.session_id,
            implementation=implementation,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        watchdog, self.watchdog_task = self.watchdog_task, None
        current = asyncio.current_task()
        if watchdog is not None and watchdog is not current and not watchdog.done():
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        auto_stop, self.auto_stop_task = self.auto_stop_task, None
        if auto_stop is not None and auto_stop is not current and not auto_stop.done():
            auto_stop.cancel()
            await asyncio.gather(auto_stop, return_exceptions=True)
        if self.soniox_server is not None:
            await self.soniox_server.close()


class ProviderReplayRegistry:
    """Bounded, one-shot state for the installed-provider B7 harness.

    The registry contains no transcript payload and performs no provider work.
    It only owns the bounded one-shot state and QPC evidence binding used by the
    installed replay execution context.
    """

    def __init__(
        self,
        gate: ProviderReplayRuntimeGate,
        *,
        ttl_seconds: float = PROVIDER_REPLAY_DEFAULT_TTL_SECONDS,
        max_entries: int = PROVIDER_REPLAY_MAX_ENTRIES,
        monotonic: Callable[[], float] = time.monotonic,
        uuid_factory: Callable[[], UUID] = uuid4,
        qpc_clock: Callable[[], tuple[int, int]] = windows_qpc_snapshot,
        authoritative_fixture_duration_ms: int = (
            PROVIDER_REPLAY_DEFAULT_FIXTURE_DURATION_MS
        ),
    ) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 1_200:
            raise ValueError("provider replay TTL must be in (0, 1200]")
        if max_entries < 1 or max_entries > 4096:
            raise ValueError("provider replay max_entries must be in [1, 4096]")
        self.gate = gate
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._monotonic = monotonic
        self._uuid_factory = uuid_factory
        self._qpc_clock = qpc_clock
        duration_ms = int(authoritative_fixture_duration_ms)
        if not 100 <= duration_ms <= PROVIDER_REPLAY_MAX_FIXTURE_DURATION_MS:
            raise ValueError("provider replay fixture duration is out of bounds")
        self.authoritative_fixture_duration_ms = duration_ms
        self._samples: dict[str, _ProviderReplaySample] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.gate.enabled)

    def _require_enabled(self) -> tuple[str, str]:
        if (
            not self.gate.enabled
            or self.gate.run_id is None
            or self.gate.process_generation_fingerprint is None
        ):
            raise ProviderReplayDisabled("provider replay is not enabled")
        return self.gate.run_id, self.gate.process_generation_fingerprint

    def _canonical_matching_run(self, run_id: str) -> str:
        expected, _fingerprint = self._require_enabled()
        canonical = canonical_replay_uuid(run_id)
        if canonical is None or not hmac.compare_digest(canonical, expected):
            raise ProviderReplayNotFound("provider replay sample not found")
        return canonical

    def _expire_locked(self, now: float) -> None:
        for sample in self._samples.values():
            if sample.state in {
                "prepared",
                "activation_armed",
                "starting",
                "armed",
            } and now >= sample.expires_at_monotonic:
                sample.state = "expired"

    def _trim_locked(self) -> None:
        if len(self._samples) < self._max_entries:
            return
        terminal = sorted(
            (
                sample
                for sample in self._samples.values()
                if sample.state
                not in {"prepared", "activation_armed", "starting", "armed"}
            ),
            key=lambda item: item.created_at_monotonic,
        )
        while terminal and len(self._samples) >= self._max_entries:
            stale = terminal.pop(0)
            self._samples.pop(stale.sample_id, None)
        if len(self._samples) >= self._max_entries:
            raise ProviderReplayCapacityError("provider replay registry is full")

    def _sample_locked(
        self,
        *,
        run_id: str,
        sample_id: str,
        now: float,
    ) -> _ProviderReplaySample:
        canonical_run = self._canonical_matching_run(run_id)
        canonical_sample = canonical_replay_uuid(sample_id)
        sample = self._samples.get(canonical_sample or "")
        if (
            sample is None
            or not hmac.compare_digest(sample.run_id, canonical_run)
            or sample.state == "expired"
        ):
            raise ProviderReplayNotFound("provider replay sample not found")
        if now >= sample.expires_at_monotonic:
            sample.state = "expired"
            raise ProviderReplayNotFound("provider replay sample not found")
        return sample

    def prepare(
        self,
        *,
        run_id: str,
        provider: str,
        expected_audio_preparation_implementation: str | None = None,
    ) -> dict[str, Any]:
        canonical_run = self._canonical_matching_run(run_id)
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider not in PROVIDER_REPLAY_PROVIDERS:
            raise ValueError("unsupported provider replay provider")
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            if any(
                sample.state
                in {"prepared", "activation_armed", "starting", "armed"}
                for sample in self._samples.values()
            ):
                raise ProviderReplayConflict("another provider replay sample is active")
            self._trim_locked()
            sample_id = canonical_replay_uuid(str(self._uuid_factory()))
            if sample_id is None or sample_id in self._samples:
                raise ProviderReplayConflict("provider replay sample id collision")
            sample = _ProviderReplaySample(
                run_id=canonical_run,
                sample_id=sample_id,
                provider=normalized_provider,
                state="prepared",
                created_at_monotonic=now,
                expires_at_monotonic=now + self._ttl_seconds,
                expected_audio_preparation_implementation=(
                    str(expected_audio_preparation_implementation or "").strip()
                    or None
                ),
            )
            self._samples[sample_id] = sample
            return self._public_locked(sample, now=now)

    def status(self, *, run_id: str, sample_id: str) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            return self._public_locked(sample, now=now)

    def arm_unsupported(
        self,
        *,
        run_id: str,
        sample_id: str,
        target_process_id: int,
        target_creation_time_100ns: int,
    ) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            if sample.state != "prepared":
                raise ProviderReplayConflict("provider replay sample is not prepared")
            target_material = (
                f"provider-replay-target-v1\0{target_process_id}\0"
                f"{target_creation_time_100ns}"
            ).encode("ascii")
            sample.target_generation_sha256 = hashlib.sha256(target_material).hexdigest()
            sample.state = "unsupported"
            return self._public_locked(sample, now=now)

    def begin_arm(
        self,
        *,
        run_id: str,
        sample_id: str,
        target_process_id: int,
        target_creation_time_100ns: int,
        activation_kind: str,
    ) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            if sample.state != "prepared":
                raise ProviderReplayConflict("provider replay sample is not prepared")
            normalized_activation = str(activation_kind or "").strip().lower()
            if normalized_activation not in {"hotkey", "button"}:
                raise ProviderReplayConflict(
                    "provider replay activation kind is invalid"
                )
            target_material = (
                f"provider-replay-target-v1\0{target_process_id}\0"
                f"{target_creation_time_100ns}"
            ).encode("ascii")
            sample.target_generation_sha256 = hashlib.sha256(target_material).hexdigest()
            sample.activation_kind = normalized_activation
            sample.state = "activation_armed"
            return self._public_locked(sample, now=now)

    def claim_activation(
        self,
        *,
        run_id: str,
        sample_id: str,
        activation_kind: str,
    ) -> dict[str, Any]:
        """Consume one native activation for an explicitly armed sample.

        The state transition is the at-most-once fence between the shell's
        native QPC marker and construction of a billable-capable pipeline. A
        duplicate or mismatched activation therefore cannot start a second
        provider execution.
        """

        normalized_activation = str(activation_kind or "").strip().lower()
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            if (
                sample.state != "activation_armed"
                or sample.activation_kind != normalized_activation
            ):
                raise ProviderReplayConflict(
                    "provider replay activation binding failed"
                )
            sample.state = "starting"
            return self._public_locked(sample, now=now)

    def bind_session(
        self,
        *,
        run_id: str,
        sample_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Internal future provider hook; never exposed by the Phase 1 arm route."""

        canonical_session = canonical_replay_uuid(session_id)
        if canonical_session is None:
            raise ProviderReplayConflict("provider replay session id is invalid")
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            # Accepting ``prepared`` here keeps the internal marker unit helper
            # usable without a Windows target. The HTTP execution path always
            # calls begin_arm first and therefore binds only from ``starting``.
            if sample.state not in {"prepared", "starting"} or sample.session_id is not None:
                raise ProviderReplayConflict("provider replay sample is not bindable")
            sample.session_id = canonical_session
            sample.state = "armed"
            return self._public_locked(sample, now=now)

    def fail(
        self,
        *,
        run_id: str,
        sample_id: str,
        error_code: str,
    ) -> dict[str, Any]:
        normalized_error = str(error_code or "").strip().lower()
        if normalized_error not in _PROVIDER_REPLAY_ERROR_CODES:
            normalized_error = "arm_failed"
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            if sample.state in {"completed", "failed", "unsupported"}:
                return self._public_locked(sample, now=now)
            sample.state = "failed"
            sample.error_code = normalized_error
            return self._public_locked(sample, now=now)

    def record_marker(
        self,
        *,
        run_id: str,
        sample_id: str,
        session_id: str,
        marker: str,
        qpc_ticks: int | None = None,
        qpc_frequency: int | None = None,
    ) -> dict[str, Any]:
        source = _MARKER_SOURCES.get(marker)
        if source is None:
            raise ProviderReplayConflict("provider replay marker is not allowed")
        canonical_session = canonical_replay_uuid(session_id)
        if canonical_session is None:
            raise ProviderReplayConflict("provider replay session id is invalid")
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            if (
                sample.state != "armed"
                or sample.session_id is None
                or not hmac.compare_digest(sample.session_id, canonical_session)
            ):
                raise ProviderReplayConflict("provider replay session binding failed")
            if marker in sample.markers:
                raise ProviderReplayConflict("provider replay marker already recorded")
            if (qpc_ticks is None) != (qpc_frequency is None):
                raise ProviderReplayDisabled("windows_qpc_invalid")
            ticks, frequency = (
                self._qpc_clock()
                if qpc_ticks is None
                else (int(qpc_ticks), int(qpc_frequency))
            )
            if ticks <= 0 or frequency <= 0:
                raise ProviderReplayDisabled("windows_qpc_invalid")
            artifact = {
                "ok": True,
                "apiVersion": PROVIDER_REPLAY_CONTRACT_VERSION,
                "runId": sample.run_id,
                "sampleId": sample.sample_id,
                "sessionId": sample.session_id,
                "processGenerationFingerprint": (
                    self.gate.process_generation_fingerprint
                ),
                "source": source,
                "marker": marker,
                "qpcTicks": int(ticks),
                "qpcFrequency": int(frequency),
            }
            sample.markers[marker] = artifact
            if marker == "session_finished_emitted":
                sample.state = "completed"
            return dict(artifact)

    def record_capture_attestation(
        self,
        *,
        run_id: str,
        sample_id: str,
        session_id: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Bind private frame-reader evidence to one armed replay sample."""

        if not isinstance(payload, dict):
            raise ProviderReplayConflict(
                "provider replay capture attestation is unavailable"
            )
        canonical_session = canonical_replay_uuid(session_id)
        if canonical_session is None:
            raise ProviderReplayConflict("provider replay session id is invalid")
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            if (
                sample.state != "armed"
                or sample.session_id is None
                or not hmac.compare_digest(sample.session_id, canonical_session)
            ):
                raise ProviderReplayConflict(
                    "provider replay capture attestation binding failed"
                )
            if sample.capture_attestation is not None:
                raise ProviderReplayConflict(
                    "provider replay capture attestation already recorded"
                )
            artifact: dict[str, Any] = {
                "contractVersion": PROVIDER_REPLAY_CONTRACT_VERSION,
                "source": "rust_audio_frame_pipe_reader",
                "runId": sample.run_id,
                "sampleId": sample.sample_id,
                "sessionId": sample.session_id,
                "processGenerationFingerprint": (
                    self.gate.process_generation_fingerprint
                ),
            }
            # Copy only the bounded reader contract. In particular, a fixture
            # path or any arbitrary diagnostic context can never escape through
            # this status endpoint.
            artifact.update(
                {
                    field_name: payload.get(field_name)
                    for field_name in _CAPTURE_ATTESTATION_READER_FIELDS
                }
            )
            sample.capture_attestation = artifact
            return dict(artifact)

    def record_audio_preparation_attestation(
        self,
        *,
        run_id: str,
        sample_id: str,
        session_id: str,
        implementation: str,
    ) -> dict[str, Any]:
        canonical_session = canonical_replay_uuid(session_id)
        if canonical_session is None:
            raise ProviderReplayConflict("provider replay session id is invalid")
        actual = str(implementation or "").strip()
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            sample = self._sample_locked(
                run_id=run_id,
                sample_id=sample_id,
                now=now,
            )
            if (
                sample.state != "armed"
                or sample.session_id is None
                or not hmac.compare_digest(sample.session_id, canonical_session)
            ):
                raise ProviderReplayConflict(
                    "provider replay audio preparation binding failed"
                )
            if sample.actual_audio_preparation_implementation is not None:
                raise ProviderReplayConflict(
                    "provider replay audio preparation already recorded"
                )
            if (
                not actual
                or sample.expected_audio_preparation_implementation is None
                or actual != sample.expected_audio_preparation_implementation
            ):
                raise ProviderReplayConflict(
                    "provider replay audio preparation mismatch"
                )
            sample.actual_audio_preparation_implementation = actual
            return {
                "expected": sample.expected_audio_preparation_implementation,
                "actual": actual,
            }

    def capture_marker_timestamp(self, marker: str) -> tuple[int, int]:
        """Capture an allowlisted QPC boundary before a session id exists."""

        if marker not in _MARKER_SOURCES:
            raise ProviderReplayConflict("provider replay marker is not allowed")
        ticks, frequency = self._qpc_clock()
        if ticks <= 0 or frequency <= 0:
            raise ProviderReplayDisabled("windows_qpc_invalid")
        return int(ticks), int(frequency)

    def _public_locked(
        self,
        sample: _ProviderReplaySample,
        *,
        now: float,
    ) -> dict[str, Any]:
        _run_id, fingerprint = self._require_enabled()
        return {
            "contractVersion": PROVIDER_REPLAY_CONTRACT_VERSION,
            "runId": sample.run_id,
            "sampleId": sample.sample_id,
            "provider": sample.provider,
            "fixtureText": provider_replay_fixture_text(sample.provider),
            "fixtureTextSha256": provider_replay_fixture_sha256(sample.provider),
            "fixtureTextLength": len(provider_replay_fixture_text(sample.provider)),
            "authoritativeFixtureDurationMs": (
                self.authoritative_fixture_duration_ms
            ),
            "state": sample.state,
            "expiresInMs": max(
                0,
                int(round((sample.expires_at_monotonic - now) * 1000)),
            ),
            "sessionId": sample.session_id,
            "processGenerationFingerprint": fingerprint,
            "targetGenerationSha256": sample.target_generation_sha256,
            "activationKind": sample.activation_kind,
            "errorCode": sample.error_code,
            "captureAttestation": (
                dict(sample.capture_attestation)
                if sample.capture_attestation is not None
                else None
            ),
            "audioPreparationImplementationExpected": (
                sample.expected_audio_preparation_implementation
            ),
            "audioPreparationImplementationActual": (
                sample.actual_audio_preparation_implementation
            ),
            "markers": [dict(item) for item in sample.markers.values()],
        }
