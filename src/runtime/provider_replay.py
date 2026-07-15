from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable
from uuid import UUID, uuid4


PROVIDER_REPLAY_RUN_ID_ENV = "SCRIBER_B7_PROVIDER_REPLAY_RUN_ID"
PROVIDER_REPLAY_RUNTIME_MODE = "tauri-supervised"
PROVIDER_REPLAY_LAUNCH_KIND = "sidecar"
PROVIDER_REPLAY_PARENT_EXE = "scriber-desktop.exe"
PROVIDER_REPLAY_CONTRACT_VERSION = 1
PROVIDER_REPLAY_DEFAULT_TTL_SECONDS = 60.0
PROVIDER_REPLAY_MAX_ENTRIES = 256
PROVIDER_REPLAY_PROVIDERS = frozenset({"microsoft", "soniox"})

# These phrases are product-owned, immutable benchmark fixtures. The control
# plane never accepts transcript text from a caller. Keeping one fixed phrase
# per provider also lets the external observer prove the visible suffix without
# sending user content through the installed backend.
_PROVIDER_REPLAY_TEXT = {
    "microsoft": "Scriber deterministic Microsoft provider replay.",
    "soniox": "Scriber deterministic Soniox provider replay.",
}

_MARKER_SOURCES = {
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
        "expired",
        "pipeline_failed",
        "provider_failed",
        "shutdown",
        "target_mismatch",
    }
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


def create_azure_mai_replay_transport(
    *,
    provider: str = "microsoft",
) -> Callable[..., Any]:
    """Return a deterministic local raw transport for the real MAI parser.

    The transport consumes the MP3 produced by ``AzureMaiTranscribeSTTService``
    and returns a realistic raw provider payload. It never performs network I/O
    and intentionally does not emit the provider-complete marker; that marker
    belongs to the MAI adapter boundary after this await returns.
    """

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
    ) -> tuple[int, str]:
        nonlocal used
        if used:
            raise RuntimeError("provider replay MAI transport is one-shot")
        used = True
        del session, speech_key
        if not str(url).startswith("https://"):
            raise RuntimeError("provider replay MAI adapter URL is invalid")
        if filename != "audio.mp3" or content_type != "audio/mpeg":
            raise RuntimeError("provider replay MAI audio contract mismatch")
        if not isinstance(definition, dict) or not definition:
            raise RuntimeError("provider replay MAI definition is missing")
        if not isinstance(timeout_secs, (int, float)) or timeout_secs <= 0:
            raise RuntimeError("provider replay MAI timeout is invalid")
        if isinstance(audio_source, (bytes, bytearray, memoryview)):
            audio = bytes(audio_source)
        elif callable(getattr(audio_source, "read", None)):
            audio = audio_source.read()
        else:
            raise RuntimeError("provider replay MAI audio source is invalid")
        if not audio:
            raise RuntimeError("provider replay MAI audio is empty")
        return 200, raw_payload

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
    markers: dict[str, dict[str, Any]] = field(default_factory=dict)
    error_code: str | None = None


@dataclass
class ProviderReplayExecution:
    """Private installed-runtime context passed through the real controller."""

    registry: "ProviderReplayRegistry"
    run_id: str
    sample_id: str
    provider: str
    injection_target_guard: Any
    azure_raw_transport: Callable[..., Any] | None = None
    soniox_server: LocalSonioxReplayServer | None = None
    session_id: str | None = None
    watchdog_task: asyncio.Task | None = None
    auto_stop_task: asyncio.Task | None = None
    _closed: bool = False

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
        return result

    def marker(self, marker: str) -> dict[str, Any]:
        if self.session_id is None:
            raise ProviderReplayConflict("provider replay session is not bound")
        return self.registry.record_marker(
            run_id=self.run_id,
            sample_id=self.sample_id,
            session_id=self.session_id,
            marker=marker,
        )

    def fail(self, error_code: str) -> dict[str, Any] | None:
        with contextlib.suppress(ProviderReplayError):
            return self.registry.fail(
                run_id=self.run_id,
                sample_id=self.sample_id,
                error_code=error_code,
            )
        return None

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
    ) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 300:
            raise ValueError("provider replay TTL must be in (0, 300]")
        if max_entries < 1 or max_entries > 4096:
            raise ValueError("provider replay max_entries must be in [1, 4096]")
        self.gate = gate
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._monotonic = monotonic
        self._uuid_factory = uuid_factory
        self._qpc_clock = qpc_clock
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
            if sample.state in {"prepared", "starting", "armed"} and now >= sample.expires_at_monotonic:
                sample.state = "expired"

    def _trim_locked(self) -> None:
        if len(self._samples) < self._max_entries:
            return
        terminal = sorted(
            (
                sample
                for sample in self._samples.values()
                if sample.state not in {"prepared", "starting", "armed"}
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

    def prepare(self, *, run_id: str, provider: str) -> dict[str, Any]:
        canonical_run = self._canonical_matching_run(run_id)
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider not in PROVIDER_REPLAY_PROVIDERS:
            raise ValueError("unsupported provider replay provider")
        now = self._monotonic()
        with self._lock:
            self._expire_locked(now)
            if any(
                sample.state in {"prepared", "starting", "armed"}
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
            ticks, frequency = self._qpc_clock()
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
            "state": sample.state,
            "expiresInMs": max(
                0,
                int(round((sample.expires_at_monotonic - now) * 1000)),
            ),
            "sessionId": sample.session_id,
            "processGenerationFingerprint": fingerprint,
            "targetGenerationSha256": sample.target_generation_sha256,
            "errorCode": sample.error_code,
            "markers": [dict(item) for item in sample.markers.values()],
        }
