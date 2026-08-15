"""Optional local voice component and Voice Library routes.

Tenth domain lifted out of ``web_api.create_app``.

Two optional local components live here: the speaker-recognition model behind
the Voice Library, and the local diarization component behind speaker
separation. Both are downloaded on demand, both can be deleted, and neither is
required for the app to run.

What makes this a domain rather than six status endpoints is the opt-in. Voice
Library processing is biometric, so the user's consent gates it -- and the
consent flag is durable and cross-process, which means it can be withdrawn from
another Scriber window while a download is mid-flight. Every mutation here
therefore re-checks consent after the step that could outlive it, and deletes
what it just installed if consent is gone. That is the invariant this module
exists to hold.

Enrollment, profile previews, and profile mutations live beside the optional
model because they share one privacy boundary and one mutation order. Native
audio admission stays behind a narrow collaborator: this module decides *what*
an enrollment does, while composition decides whether this process may own the
microphone and how that ownership is reflected in global application state.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Protocol
from uuid import uuid4

from aiohttp import ClientSession, web
from loguru import logger

from src.api.app_keys import APP_HTTP_SESSION
from src.config import Config
from src.core.rest_contracts import REST_API_VERSION
from src.data.audio_admission_store import AudioAdmissionClaim
from src.data.meeting_store import MeetingNotFound, VoiceLibraryDisabled
from src.runtime.cancellation import (
    await_with_delayed_cancellation,
    to_thread_cancellation_barrier,
)
from src.runtime.media_tools import require_media_tool
from src.runtime.paths import data_dir
from src.runtime.shell_ipc import available as shell_ipc_available
from src.runtime.shell_ipc import call_shell_ipc
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs
from src.runtime.support_bundle import redact_text
from src.speaker_enrollment import VoiceEnrollmentCapture, assess_voice_sample, voice_reference_wav

_SPEAKER_PROFILE_PREVIEW_TTL_SECONDS = 15 * 60
_SPEAKER_PROFILE_PREVIEW_MAX_GRANTS = 256
_SPEAKER_PROFILE_PREVIEW_MAX_BYTES = 384 * 1024
_STORE_FAILURES = (OSError, sqlite3.Error)


class SpeakerModelPort(Protocol):
    """Local model operations consumed by the Voice Library domain."""

    def status(self) -> dict[str, Any]: ...

    async def stage_download(self, session: ClientSession) -> Any: ...

    def promote_staged(self, staged: Any) -> dict[str, Any]: ...

    def discard_staged(self, staged: Any) -> None: ...

    def delete(self) -> None: ...

    async def extract_pcm16(self, pcm: bytes, *, sample_rate: int = 16_000) -> list[float]: ...


class VoiceLibraryStorePort(Protocol):
    """The profile and consent persistence surface used by these routes."""

    def speaker_library_enabled(self) -> bool: ...

    def speaker_profiles(self) -> list[dict[str, Any]]: ...

    def speaker_profile_preview_candidates(self) -> dict[str, dict[str, Any]]: ...

    def speaker_profile_previews(self) -> dict[str, dict[str, Any]]: ...

    def speaker_profile_preview(self, profile_id: str) -> dict[str, Any] | None: ...

    def save_speaker_profile_preview(
        self,
        profile_id: str,
        audio: bytes,
        *,
        duration_ms: int,
        source: str,
        replace: bool = False,
    ) -> bool: ...

    def enroll_speaker_profile(
        self,
        display_name: str,
        embedding: list[float],
        *,
        quality: float = 1.0,
        profile_id: str = "",
        preview_audio: bytes | None = None,
        preview_duration_ms: int = 0,
        preview_source: str = "enrollment",
    ) -> dict[str, Any]: ...

    def delete_speaker_profile(self, profile_id: str) -> bool: ...

    def rename_speaker_profile(self, profile_id: str, display_name: str) -> dict[str, Any]: ...

    def delete_all_speaker_profiles(self) -> int: ...

    def merge_speaker_profiles(self, target_profile_id: str, source_profile_id: str) -> dict[str, Any]: ...

    def split_speaker_profile(self, meeting_id: str, speaker_id: str) -> dict[str, Any]: ...


class DiarizerPort(Protocol):
    """Event-loop-safe local diarizer operations consumed by HTTP routes."""

    async def status_async(self, *, force: bool = False) -> dict[str, Any]: ...

    async def install(
        self,
        session: ClientSession,
        progress: Callable[..., None] | None = None,
    ) -> dict[str, Any]: ...

    async def delete_async(self) -> bool: ...


@dataclass(frozen=True)
class VoiceEnrollmentAdmission:
    """Ownership returned only after composition reserved native audio."""

    claim: AudioAdmissionClaim
    pending_cancellation: asyncio.CancelledError | None = None


class VoiceEnrollmentUnavailable(RuntimeError):
    """A safe, actionable conflict detected at the composition boundary."""


VoiceEnrollmentLossHandler = Callable[[str], Awaitable[None]]


class VoiceEnrollmentAdmissionPort(Protocol):
    """Global audio/application state needed by an enrollment request.

    ``loss_handler`` is request-local. The adapter may release its durable
    admission only after this callback returns; an exception means native
    capture ownership remains unproven and must stay retained.
    """

    async def acquire(
        self,
        *,
        owner_id: str,
        loss_handler: VoiceEnrollmentLossHandler,
    ) -> VoiceEnrollmentAdmission: ...

    async def prepare_capture(self) -> None: ...

    async def release(
        self,
        admission: VoiceEnrollmentAdmission,
        *,
        native_capture_released: bool,
    ) -> None: ...


class VoiceEnrollmentCapturePort(Protocol):
    """Bounded in-memory PCM reader owned by one enrollment request."""

    def start(self, frame_pipe: str) -> None: ...

    def stop(self, timeout: float = 3.0) -> dict[str, Any]: ...

    def expect_native_stop(self) -> None: ...

    def pcm16(self) -> bytes: ...

    def clear(self) -> None: ...


class VoiceShellCall(Protocol):
    def __call__(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class VoiceCaptureFactory(Protocol):
    def __call__(
        self,
        *,
        sample_rate: int,
        max_duration_seconds: float,
    ) -> VoiceEnrollmentCapturePort: ...


class VoiceReferenceWavBuilder(Protocol):
    def __call__(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
    ) -> tuple[bytes, int]: ...


class VoiceCaptureRuntimePort(Protocol):
    """Exact native-capture interface consumed by Voice enrollment."""

    def is_available(self) -> bool: ...

    def call_shell(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...

    def create_capture(
        self,
        *,
        sample_rate: int,
        max_duration_seconds: float,
    ) -> VoiceEnrollmentCapturePort: ...

    async def wait(self, duration_ms: int) -> None: ...

    def build_reference_wav(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
    ) -> tuple[bytes, int]: ...


class VoiceCaptureRuntime:
    """Small exact-callback adapter used by isolated route tests and defaults."""

    def __init__(
        self,
        *,
        available: Callable[[], bool] = shell_ipc_available,
        call: VoiceShellCall = call_shell_ipc,
        capture_factory: VoiceCaptureFactory = VoiceEnrollmentCapture,
        wait: Callable[[int], Awaitable[None]] | None = None,
        reference_wav: VoiceReferenceWavBuilder = voice_reference_wav,
    ) -> None:
        self._available = available
        self._call = call
        self._capture_factory = capture_factory
        self._wait = wait
        self._reference_wav = reference_wav

    def is_available(self) -> bool:
        return self._available()

    def call_shell(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return self._call(command, payload, timeout_seconds=timeout_seconds)

    def create_capture(
        self,
        *,
        sample_rate: int,
        max_duration_seconds: float,
    ) -> VoiceEnrollmentCapturePort:
        return self._capture_factory(
            sample_rate=sample_rate,
            max_duration_seconds=max_duration_seconds,
        )

    async def wait(self, duration_ms: int) -> None:
        if self._wait is None:
            await asyncio.sleep(duration_ms / 1_000)
            return
        await self._wait(duration_ms)

    def build_reference_wav(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
    ) -> tuple[bytes, int]:
        return self._reference_wav(pcm, sample_rate=sample_rate)


class _VoiceEnrollmentCaptureOwnership:
    """Serialize one request's owned-remote native capture lifecycle."""

    def __init__(self, runtime: VoiceCaptureRuntimePort, *, before_stop: Callable[[], None]) -> None:
        self._runtime = runtime
        self._before_stop = before_stop
        self._lock = asyncio.Lock()
        self._native_capture_started = False
        self._stream_id = ""
        self._loss_reason = ""

    async def start(self, payload: dict[str, Any]) -> tuple[Any, asyncio.CancelledError | None]:
        """Start capture while a concurrent loss waits to perform cleanup."""

        async with self._lock:
            self._raise_if_lost()
            response, pending_cancel = await await_with_delayed_cancellation(
                asyncio.to_thread(
                    self._runtime.call_shell,
                    "audioCaptureStart",
                    payload,
                    timeout_seconds=4.0,
                )
            )
            if isinstance(response, dict) and response.get("success") is True:
                self._native_capture_started = True
                raw_payload = response.get("payload")
                if isinstance(raw_payload, dict):
                    self._stream_id = str(raw_payload.get("streamId") or "")
            return response, pending_cancel

    async def stop(self) -> tuple[bool, asyncio.CancelledError | None]:
        """Stop capture once; return false unless shell confirms ownership ended."""

        async with self._lock:
            return await self._stop_locked()

    async def handle_loss(self, reason: str) -> None:
        """Prevent later starts and prove remote capture stopped before returning."""

        async with self._lock:
            self._loss_reason = str(reason or "native-audio ownership lost")
            released, pending_cancel = await self._stop_locked()
            if not released:
                raise RuntimeError("Native microphone capture could not be proven stopped.")
            if pending_cancel is not None:
                raise pending_cancel

    async def raise_if_lost(self) -> None:
        async with self._lock:
            self._raise_if_lost()

    def _raise_if_lost(self) -> None:
        if self._loss_reason:
            raise RuntimeError("Native-audio ownership ended during voice enrollment.")

    async def _stop_locked(self) -> tuple[bool, asyncio.CancelledError | None]:
        if not self._native_capture_started:
            return True, None
        if not self._stream_id:
            return False, None
        try:
            self._before_stop()
        except Exception as exc:
            logger.warning("Voice Library reader stop preparation failed: {}", type(exc).__name__)
        stop_response, pending_cancel = await await_with_delayed_cancellation(
            asyncio.to_thread(
                self._runtime.call_shell,
                "audioCaptureStop",
                {"streamId": self._stream_id},
                timeout_seconds=4.0,
            )
        )
        if not _voice_enrollment_stop_confirmed(stop_response):
            return False, pending_cancel
        self._stream_id = ""
        self._native_capture_started = False
        return True, pending_cancel


@dataclass(frozen=True)
class SpeakerProfilePreviewGrant:
    """Process-local capability for one bounded local speaker sample."""

    profile_id: str
    duration_ms: int
    expires_at: float
    source: str
    meeting_id: str = ""
    start_ms: int = 0


@dataclass(frozen=True)
class SpeakerProfileSummary:
    """The complete public profile projection; biometric vectors never enter it."""

    id: str
    display_name: str
    sample_count: int
    is_named: bool
    enrolled: bool
    enrollment_sample_count: int
    enrolled_at: str
    created_at: str
    updated_at: str

    @classmethod
    def from_store(cls, value: dict[str, Any]) -> SpeakerProfileSummary:
        enrollment_count = max(0, int(value.get("enrollmentSampleCount") or 0))
        return cls(
            id=str(value.get("id") or ""),
            display_name=str(value.get("displayName") or ""),
            sample_count=max(0, int(value.get("sampleCount") or 0)),
            is_named=bool(value.get("isNamed")),
            enrolled=bool(value.get("enrolled") or enrollment_count),
            enrollment_sample_count=enrollment_count,
            enrolled_at=str(value.get("enrolledAt") or ""),
            created_at=str(value.get("createdAt") or ""),
            updated_at=str(value.get("updatedAt") or ""),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "sampleCount": self.sample_count,
            "isNamed": self.is_named,
            "enrolled": self.enrolled,
            "enrollmentSampleCount": self.enrollment_sample_count,
            "enrolledAt": self.enrolled_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass
class VoiceComponentState:
    """Per-application serialization and opaque preview capabilities."""

    download_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    preview_grants: dict[str, SpeakerProfilePreviewGrant] = field(default_factory=dict)


@dataclass(frozen=True)
class VoiceLibraryDeps:
    """What the Voice Library routes mutate, resolved per request.

    Per request rather than at registration because composition supplies live
    controller collaborators that focused suites replace after the app exists.
    Locks and preview capabilities are not dependencies: this module owns them
    in :class:`VoiceComponentState` for the lifetime of one application.
    """

    speaker_model: SpeakerModelPort
    meeting_store: VoiceLibraryStorePort
    persist_settings: Callable[[], None]


VoiceLibraryProvider = Callable[[], VoiceLibraryDeps]
VoiceEnrollmentProvider = Callable[[], VoiceEnrollmentAdmissionPort]
DiarizerProvider = Callable[[], DiarizerPort]

APP_VOICE_LIBRARY_DEPS: web.AppKey[VoiceLibraryProvider] = web.AppKey("voice_library_deps_provider")
APP_VOICE_ENROLLMENT: web.AppKey[VoiceEnrollmentProvider] = web.AppKey("voice_enrollment_provider")
APP_DIARIZER: web.AppKey[DiarizerProvider] = web.AppKey("diarizer_provider")
APP_VOICE_COMPONENT_STATE: web.AppKey[VoiceComponentState] = web.AppKey("voice_component_state")
APP_VOICE_CAPTURE_RUNTIME: web.AppKey[VoiceCaptureRuntimePort] = web.AppKey("voice_capture_runtime")


def _deps(request: web.Request) -> VoiceLibraryDeps:
    return request.app[APP_VOICE_LIBRARY_DEPS]()


def _enrollment(request: web.Request) -> VoiceEnrollmentAdmissionPort:
    return request.app[APP_VOICE_ENROLLMENT]()


def _diarizer(request: web.Request) -> DiarizerPort:
    return request.app[APP_DIARIZER]()


def _state(request: web.Request) -> VoiceComponentState:
    return request.app[APP_VOICE_COMPONENT_STATE]


def _prune_preview_grants(state: VoiceComponentState, now: float) -> None:
    for token in [token for token, grant in state.preview_grants.items() if grant.expires_at <= now]:
        state.preview_grants.pop(token, None)
    overflow = len(state.preview_grants) - _SPEAKER_PROFILE_PREVIEW_MAX_GRANTS
    if overflow > 0:
        oldest = sorted(state.preview_grants.items(), key=lambda item: item[1].expires_at)[:overflow]
        for token, _grant in oldest:
            state.preview_grants.pop(token, None)


def _voice_enrollment_stop_confirmed(response: Any) -> bool:
    # Rust removes the capture from its registry before bounded stop/kill. A
    # successful idempotent stop can therefore report ``stopped=false`` when no
    # active sidecar remains; shell acceptance is the ownership boundary.
    return bool(isinstance(response, dict) and response.get("success") is True)


async def _render_speaker_profile_preview(grant: SpeakerProfilePreviewGrant) -> bytes:
    """Decode only the granted interval; never persist another voice sample."""
    if grant.source not in {"microphone", "system"}:
        raise ValueError("Unsupported speaker preview source.")
    if not re.fullmatch(r"[0-9a-f]{32}", grant.meeting_id):
        raise ValueError("Invalid speaker preview meeting capability.")
    if not (2_000 <= grant.duration_ms <= 4_000) or grant.start_ms < 0:
        raise ValueError("Invalid speaker preview interval.")
    source_name = "microphone.opus" if grant.source == "microphone" else "system.opus"
    meeting_root = (data_dir() / "meetings").resolve()
    source_path = (meeting_root / grant.meeting_id / "final" / source_name).resolve()
    expected_parent = (meeting_root / grant.meeting_id / "final").resolve()
    if (
        expected_parent.parent.parent != meeting_root
        or source_path.parent != expected_parent
        or not source_path.is_file()
    ):
        raise FileNotFoundError("Speaker preview audio is unavailable.")

    ffmpeg = require_media_tool("ffmpeg")
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{grant.start_ms / 1000.0:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{grant.duration_ms / 1000.0:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    stdout, _stderr = await communicate_or_kill_on_cancel(
        process,
        max_stdout_bytes=_SPEAKER_PROFILE_PREVIEW_MAX_BYTES,
        max_stderr_bytes=64 * 1024,
    )
    audio = bytes(stdout or b"")
    if process.returncode != 0 or len(audio) < 44 or not audio.startswith(b"RIFF"):
        raise RuntimeError("Speaker preview decoding failed.")
    return audio


async def _voice_library_enabled(deps: VoiceLibraryDeps) -> bool:
    """Answer the durable, cross-process consent gate.

    ``Config`` alone is this process's view. The store's required flag is the
    one another Scriber window can flip, so both views must still consent.
    """
    if not Config.VOICEPRINT_LIBRARY_OPT_IN:
        return False
    return bool(await asyncio.to_thread(deps.meeting_store.speaker_library_enabled))


async def _diarizer_status(diarizer: DiarizerPort) -> dict[str, Any]:
    return await diarizer.status_async()


def _diarization_payload(status: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "apiVersion": REST_API_VERSION,
        "enabled": bool(Config.SPEAKER_DIARIZATION_FALLBACK_ENABLED),
        **extra,
        **status,
    }


def _redact_unknown_voice_library_failures(
    handler: Callable[[web.Request], Awaitable[web.Response]],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Keep unexpected collaborator failures behind the public REST boundary."""

    @wraps(handler)
    async def guarded(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except Exception as exc:
            logger.warning("Voice Library route failed: {}", type(exc).__name__)
            return web.json_response(
                {"message": "Voice Library is temporarily unavailable."},
                status=503,
            )

    return guarded


@_redact_unknown_voice_library_failures
async def speaker_model_status(request: web.Request) -> web.Response:
    deps = _deps(request)
    try:
        opted_in = await _voice_library_enabled(deps)
    except _STORE_FAILURES as exc:
        logger.warning("Voice Library consent read failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Voice Library consent could not be confirmed."},
            status=503,
        )
    return web.json_response(
        {
            "apiVersion": REST_API_VERSION,
            "optedIn": opted_in,
            **deps.speaker_model.status(),
        }
    )


@_redact_unknown_voice_library_failures
async def download_speaker_model(request: web.Request) -> web.Response:
    """Install the Voice Library model, honouring an opt-out at every step.

    Consent is checked three times on purpose: before starting, after staging
    but before the atomic replace, and again after it. The last check is the one
    that matters -- the replace runs in an executor that cancellation cannot
    interrupt, so another process can withdraw consent while it is in flight.
    The model is then deleted rather than left behind.
    """
    deps = _deps(request)
    if not Config.VOICEPRINT_LIBRARY_OPT_IN:
        return web.json_response(
            {"message": "Confirm the Voice Library biometric-processing opt-in first."}, status=409
        )
    try:
        library_enabled = await _voice_library_enabled(deps)
    except _STORE_FAILURES as exc:
        logger.warning("Voice Library consent read failed before model download: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Voice Library consent could not be confirmed for the local download."},
            status=503,
        )
    if not library_enabled:
        return web.json_response(
            {"message": "Voice Library was turned off before the download started."},
            status=409,
        )
    staged = None
    try:
        async with _state(request).download_lock:
            staged = await deps.speaker_model.stage_download(request.app[APP_HTTP_SESSION])
            async with _state(request).mutation_lock:
                try:
                    library_enabled = await _voice_library_enabled(deps)
                except _STORE_FAILURES as exc:
                    logger.warning(
                        "Voice Library consent recheck failed before model promotion: {}", type(exc).__name__
                    )
                    return web.json_response(
                        {"message": "Voice Library consent could not be confirmed for the local download."},
                        status=503,
                    )
                if not library_enabled:
                    return web.json_response(
                        {"message": "Voice Library was turned off while the local download was running."},
                        status=409,
                    )
                status, promotion_cancel = await await_with_delayed_cancellation(
                    asyncio.to_thread(deps.speaker_model.promote_staged, staged)
                )
                staged = None

                async def post_promotion_consent() -> tuple[bool, Exception | None]:
                    try:
                        return await _voice_library_enabled(deps), None
                    except Exception as exc:
                        return False, exc

                (enabled_after_promotion, consent_error), post_check_cancel = await await_with_delayed_cancellation(
                    post_promotion_consent()
                )
                if consent_error is not None:
                    logger.warning(
                        "Voice Library consent recheck failed after model promotion: {}",
                        type(consent_error).__name__,
                    )
                    _, delete_cancel = await await_with_delayed_cancellation(
                        asyncio.to_thread(deps.speaker_model.delete)
                    )
                    pending_cancel = promotion_cancel or post_check_cancel or delete_cancel
                    if pending_cancel is not None:
                        raise pending_cancel from None
                    return web.json_response(
                        {"message": "Voice Library consent could not be confirmed after the local download."},
                        status=503,
                    )
                pending_cancel = promotion_cancel or post_check_cancel
                if not enabled_after_promotion:
                    _, delete_cancel = await await_with_delayed_cancellation(
                        asyncio.to_thread(deps.speaker_model.delete)
                    )
                    pending_cancel = pending_cancel or delete_cancel
                    if pending_cancel is not None:
                        raise pending_cancel
                    return web.json_response(
                        {"message": "Voice Library was turned off while the local download was finishing."},
                        status=409,
                    )
                if pending_cancel is not None:
                    raise pending_cancel
        return web.json_response({"apiVersion": REST_API_VERSION, **status})
    except ValueError as exc:
        logger.warning("Voice Library model validation failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Local Voice Library model validation failed."},
            status=502,
        )
    finally:
        if staged is not None:
            try:
                await to_thread_cancellation_barrier(deps.speaker_model.discard_staged, staged)
            except Exception as exc:
                logger.warning("Voice Library staged model cleanup failed: {}", type(exc).__name__)


@_redact_unknown_voice_library_failures
async def delete_speaker_library(request: web.Request) -> web.Response:
    """Erase every voiceprint, the model behind them, and the consent flag."""
    deps = _deps(request)

    async def delete_all_voice_data() -> int:
        deleted = await asyncio.to_thread(deps.meeting_store.delete_all_speaker_profiles)
        await asyncio.to_thread(deps.speaker_model.delete)
        Config.set_voiceprint_library_opt_in(False)
        deps.persist_settings()
        return deleted

    try:
        async with _state(request).mutation_lock:
            # Withdrawing consent has to finish even if the caller goes away: a
            # half-erased Voice Library is exactly what the user asked not to keep.
            deleted_profiles, pending_cancel = await await_with_delayed_cancellation(delete_all_voice_data())
            if pending_cancel is not None:
                raise pending_cancel
    except Exception as exc:
        logger.warning("Voice Library erasure failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Voice Library data could not be deleted."},
            status=503,
        )
    return web.json_response({"apiVersion": REST_API_VERSION, "deleted": True, "deletedProfiles": deleted_profiles})


@_redact_unknown_voice_library_failures
async def list_speaker_profiles(request: web.Request) -> web.Response:
    deps = _deps(request)
    state = _state(request)
    try:
        library_enabled = await _voice_library_enabled(deps)
    except _STORE_FAILURES as exc:
        logger.warning("Voice Library consent read failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Voice Library consent could not be confirmed."},
            status=503,
        )
    if not library_enabled:
        state.preview_grants.clear()
        return web.json_response(
            {
                "apiVersion": REST_API_VERSION,
                "enabled": False,
                "items": [],
                "message": "Voice Library is local and opt-in; embeddings are excluded from this response.",
            }
        )
    try:
        items, preview_candidates, stored_previews = await asyncio.gather(
            asyncio.to_thread(deps.meeting_store.speaker_profiles),
            asyncio.to_thread(deps.meeting_store.speaker_profile_preview_candidates),
            asyncio.to_thread(deps.meeting_store.speaker_profile_previews),
        )
    except _STORE_FAILURES as exc:
        state.preview_grants.clear()
        logger.warning("Voice Library profile collection read failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Voice Library profile data could not be read."},
            status=503,
        )
    public_items = [SpeakerProfileSummary.from_store(item).to_payload() for item in items]
    now = time.monotonic()
    _prune_preview_grants(state, now)
    issued_preview_count = 0
    for item in public_items:
        profile_id = str(item.get("id") or "")
        stored_preview = stored_previews.get(profile_id)
        candidate = stored_preview or preview_candidates.get(profile_id)
        if not isinstance(candidate, dict):
            item["preview"] = None
            continue
        source = str(candidate.get("source") or "")
        duration_ms = max(0, min(4_000, int(candidate.get("durationMs") or 0)))
        if duration_ms < 2_000:
            item["preview"] = None
            continue
        meeting_id = ""
        start_ms = 0
        if stored_preview is None:
            meeting_id = str(candidate.get("meetingId") or "")
            source_name = "microphone.opus" if source == "microphone" else "system.opus"
            source_path = data_dir() / "meetings" / meeting_id / "final" / source_name
            if (
                source not in {"microphone", "system"}
                or not re.fullmatch(r"[0-9a-f]{32}", meeting_id)
                or not source_path.is_file()
            ):
                item["preview"] = None
                continue
            start_ms = max(0, int(candidate.get("startMs") or 0))
        if issued_preview_count >= _SPEAKER_PROFILE_PREVIEW_MAX_GRANTS:
            item["preview"] = None
            continue
        if len(state.preview_grants) >= _SPEAKER_PROFILE_PREVIEW_MAX_GRANTS:
            oldest_token = min(
                state.preview_grants,
                key=lambda current: state.preview_grants[current].expires_at,
            )
            state.preview_grants.pop(oldest_token, None)
        token = uuid4().hex
        state.preview_grants[token] = SpeakerProfilePreviewGrant(
            profile_id=profile_id,
            duration_ms=duration_ms,
            expires_at=now + _SPEAKER_PROFILE_PREVIEW_TTL_SECONDS,
            source=source,
            meeting_id=meeting_id,
            start_ms=start_ms,
        )
        issued_preview_count += 1
        item["preview"] = {
            "token": token,
            "url": f"/api/meetings/speaker-profile-preview/{token}",
            "startMs": 0,
            "endMs": duration_ms,
            "durationMs": duration_ms,
            "source": source,
            "expiresInSeconds": _SPEAKER_PROFILE_PREVIEW_TTL_SECONDS,
        }
    _prune_preview_grants(state, now)
    model_status = deps.speaker_model.status()
    try:
        library_enabled = await _voice_library_enabled(deps)
    except _STORE_FAILURES as exc:
        state.preview_grants.clear()
        logger.warning("Voice Library consent recheck failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Voice Library consent could not be confirmed."},
            status=503,
        )
    if not library_enabled:
        state.preview_grants.clear()
        public_items = []
    return web.json_response(
        {
            "apiVersion": REST_API_VERSION,
            "enabled": bool(library_enabled and model_status["installed"]),
            "items": public_items,
            "message": "Voice Library is local and opt-in; embeddings are excluded from this response.",
        }
    )


def _preview_response(audio: bytes) -> web.Response:
    return web.Response(
        body=audio,
        content_type="audio/wav",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _unknown_speaker_preview_response(
    state: VoiceComponentState,
    token: str,
    error: OSError | sqlite3.Error,
) -> web.Response:
    state.preview_grants.pop(token, None)
    logger.warning("Voice Library preview consent/store read failed: {}", type(error).__name__)
    return web.json_response(
        {"message": "Voice Library consent could not be confirmed for this speaker preview."},
        status=503,
    )


@_redact_unknown_voice_library_failures
async def speaker_profile_preview(request: web.Request) -> web.Response:
    deps = _deps(request)
    state = _state(request)
    now = time.monotonic()
    _prune_preview_grants(state, now)
    token = str(request.match_info.get("token") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return web.json_response({"message": "Speaker preview not found"}, status=404)
    grant = state.preview_grants.get(token)
    if grant is None or grant.expires_at <= now:
        state.preview_grants.pop(token, None)
        return web.json_response({"message": "Speaker preview not found"}, status=404)
    try:
        library_enabled = await _voice_library_enabled(deps)
    except _STORE_FAILURES as exc:
        return _unknown_speaker_preview_response(state, token, exc)
    if not library_enabled:
        state.preview_grants.pop(token, None)
        return web.json_response(
            {"message": "Voice Library was turned off before the speaker preview was played."},
            status=409,
        )

    try:
        stored_preview = await asyncio.to_thread(deps.meeting_store.speaker_profile_preview, grant.profile_id)
    except _STORE_FAILURES as exc:
        return _unknown_speaker_preview_response(state, token, exc)
    if isinstance(stored_preview, dict):
        try:
            library_enabled = await _voice_library_enabled(deps)
        except _STORE_FAILURES as exc:
            return _unknown_speaker_preview_response(state, token, exc)
        if not library_enabled:
            state.preview_grants.pop(token, None)
            return web.json_response(
                {"message": "Voice Library was turned off while the speaker preview was loading."},
                status=409,
            )
        audio = bytes(stored_preview.get("audio") or b"")
        if not audio.startswith(b"RIFF") or len(audio) > _SPEAKER_PROFILE_PREVIEW_MAX_BYTES:
            logger.warning("Local Voice Library stored preview failed validation")
            return web.json_response(
                {"message": "The local speaker preview could not be played."},
                status=503,
            )
        return _preview_response(audio)

    try:
        current_candidates = await asyncio.to_thread(deps.meeting_store.speaker_profile_preview_candidates)
    except _STORE_FAILURES as exc:
        return _unknown_speaker_preview_response(state, token, exc)
    if grant.profile_id not in current_candidates:
        state.preview_grants.pop(token, None)
        return web.json_response({"message": "Speaker preview not found"}, status=404)
    try:
        library_enabled = await _voice_library_enabled(deps)
    except _STORE_FAILURES as exc:
        return _unknown_speaker_preview_response(state, token, exc)
    if not library_enabled:
        state.preview_grants.pop(token, None)
        return web.json_response(
            {"message": "Voice Library was turned off before the speaker preview was created."},
            status=409,
        )
    try:
        audio = await _render_speaker_profile_preview(grant)
        try:
            await to_thread_cancellation_barrier(
                deps.meeting_store.save_speaker_profile_preview,
                grant.profile_id,
                audio,
                duration_ms=grant.duration_ms,
                source=grant.source,
                replace=False,
            )
        except VoiceLibraryDisabled:
            state.preview_grants.pop(token, None)
            return web.json_response(
                {"message": "Voice Library was turned off while the speaker preview was created."},
                status=409,
            )
        except Exception as exc:
            state.preview_grants.pop(token, None)
            logger.warning("Local Voice Library preview persistence failed: {}", type(exc).__name__)
            return web.json_response(
                {"message": "Voice Library consent could not be confirmed for this speaker preview."},
                status=503,
            )
    except FileNotFoundError:
        state.preview_grants.pop(token, None)
        return web.json_response({"message": "Speaker preview not found"}, status=404)
    except Exception as exc:
        logger.warning("Local Voice Library preview failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "The local speaker preview could not be played."},
            status=503,
        )
    return _preview_response(audio)


@_redact_unknown_voice_library_failures
async def enroll_speaker_profile(request: web.Request) -> web.Response:
    deps = _deps(request)
    runtime = request.app[APP_VOICE_CAPTURE_RUNTIME]
    if not Config.VOICEPRINT_LIBRARY_OPT_IN:
        return web.json_response(
            {"message": "Turn on Voice Library in Settings before recording a voice."},
            status=409,
        )
    try:
        library_enabled = await _voice_library_enabled(deps)
    except _STORE_FAILURES as exc:
        logger.warning("Voice Library consent read failed before enrollment: {}", type(exc).__name__)
        return web.json_response(
            {"message": "Voice Library consent could not be confirmed."},
            status=503,
        )
    if not library_enabled:
        return web.json_response(
            {"message": "Voice Library was turned off before the sample started."},
            status=409,
        )
    if not deps.speaker_model.status()["installed"]:
        return web.json_response(
            {"message": "Download the local voice recognition model before recording a voice."},
            status=409,
        )
    if not runtime.is_available():
        return web.json_response({"message": "Native microphone capture is unavailable in this copy."}, status=503)
    try:
        raw = await request.json()
    except Exception:
        return web.json_response({"message": "Expected JSON payload"}, status=400)
    if not isinstance(raw, dict):
        return web.json_response({"message": "Expected JSON object"}, status=400)
    display_name = " ".join(str(raw.get("displayName", "")).split()).strip()
    if not display_name:
        return web.json_response({"message": "Enter the speaker's name first."}, status=400)
    if len(display_name) > 120:
        return web.json_response({"message": "Speaker name must be 120 characters or fewer."}, status=400)
    profile_id = str(raw.get("profileId", "") or "").strip()
    if profile_id:
        try:
            profiles = await asyncio.to_thread(deps.meeting_store.speaker_profiles)
        except _STORE_FAILURES as exc:
            logger.warning("Voice Library enrollment profile read failed: {}", type(exc).__name__)
            return web.json_response(
                {"message": "Voice Library profile data could not be read."},
                status=503,
            )
        if not any(str(item.get("id", "")) == profile_id for item in profiles):
            return web.json_response({"message": "Speaker profile not found"}, status=404)
    microphone_hash = str(raw.get("microphoneNativeEndpointIdHash", "") or "").strip()
    if microphone_hash and not re.fullmatch(r"[0-9a-fA-F]{8,128}", microphone_hash):
        return web.json_response({"message": "Choose a valid microphone."}, status=400)
    try:
        duration_ms = max(6_000, min(10_000, int(raw.get("durationMs", 8_000) or 8_000)))
    except TypeError, ValueError:
        return web.json_response({"message": "Invalid voice sample duration."}, status=400)

    enrollment = _enrollment(request)
    admission: VoiceEnrollmentAdmission | None = None
    capture: Any = None

    def expect_native_capture_stop() -> None:
        if capture is None:
            return
        expect_native_stop = getattr(capture, "expect_native_stop", None)
        if callable(expect_native_stop):
            expect_native_stop()

    native_capture = _VoiceEnrollmentCaptureOwnership(
        runtime,
        before_stop=expect_native_capture_stop,
    )
    handler_cancelled = False
    try:
        try:
            admission = await enrollment.acquire(
                owner_id=f"enrollment-{uuid4().hex}",
                loss_handler=native_capture.handle_loss,
            )
        except VoiceEnrollmentUnavailable as exc:
            return web.json_response({"message": str(exc)}, status=409)
        if admission.pending_cancellation is not None:
            raise admission.pending_cancellation
        await enrollment.prepare_capture()
        response, pending_cancel = await native_capture.start(
            {
                "sampleRate": 16_000,
                "channels": 1,
                "blockSize": 512,
                "devicePreference": "default",
                "nativeEndpointIdHash": microphone_hash or None,
                "prebufferMs": 0,
            }
        )
        payload: dict[str, Any] = {}
        if isinstance(response, dict):
            raw_payload = response.get("payload")
            if isinstance(raw_payload, dict):
                payload = raw_payload
        if pending_cancel is not None:
            raise pending_cancel
        if not isinstance(response, dict):
            raise RuntimeError("Native microphone capture returned an invalid response.")
        if not response.get("success"):
            error_code = str(response.get("errorCode") or "")
            if error_code == "transportError":
                message = "Scriber's microphone service was temporarily busy. Wait a moment and try the sample again."
            else:
                message = str(response.get("fallbackReason") or "The selected microphone could not start.")[:240]
            return web.json_response({"message": message}, status=503)
        frame_pipe = str(payload.get("framePipe") or "")
        if not str(payload.get("streamId") or "") or not frame_pipe:
            return web.json_response(
                {"message": "Native microphone capture returned an incomplete response."},
                status=503,
            )
        try:
            returned_sample_rate = int(payload.get("sampleRate") or 0)
            returned_channels = int(payload.get("channels") or 0)
        except TypeError, ValueError:
            returned_sample_rate = 0
            returned_channels = 0
        returned_sample_format = str(payload.get("sampleFormat") or "")
        if returned_sample_rate != 16_000 or returned_channels != 1 or returned_sample_format != "pcm_i16_le":
            return web.json_response(
                {
                    "message": (
                        "Native microphone capture returned an unsupported audio format. Restart Scriber and try again."
                    )
                },
                status=503,
            )
        await native_capture.raise_if_lost()
        capture = runtime.create_capture(
            sample_rate=16_000,
            max_duration_seconds=(duration_ms + 1_000) / 1_000,
        )
        _, pending_cancel = await await_with_delayed_cancellation(asyncio.to_thread(capture.start, frame_pipe))
        if pending_cancel is not None:
            raise pending_cancel
        await native_capture.raise_if_lost()
        await runtime.wait(duration_ms)
        native_capture_released, pending_cancel = await native_capture.stop()
        if not native_capture_released:
            raise RuntimeError("Native microphone capture did not stop cleanly.")
        if pending_cancel is not None:
            raise pending_cancel
        await native_capture.raise_if_lost()
        snapshot, pending_cancel = await await_with_delayed_cancellation(asyncio.to_thread(capture.stop))
        if pending_cancel is not None:
            raise pending_cancel
        await native_capture.raise_if_lost()
        quality = assess_voice_sample(snapshot)
        pcm = capture.pcm16()
        async with _state(request).mutation_lock:
            await native_capture.raise_if_lost()
            if not await _voice_library_enabled(deps):
                return web.json_response(
                    {"message": "Voice Library was turned off before the sample finished."},
                    status=409,
                )
            if not deps.speaker_model.status()["installed"]:
                return web.json_response(
                    {"message": "The local voice recognition model is no longer available."},
                    status=409,
                )
            embedding, pending_cancel = await await_with_delayed_cancellation(
                deps.speaker_model.extract_pcm16(pcm, sample_rate=16_000)
            )
            if pending_cancel is not None:
                raise pending_cancel
            await native_capture.raise_if_lost()
            preview_audio, preview_duration_ms = runtime.build_reference_wav(pcm, sample_rate=16_000)
            pcm = b""
            profile = await to_thread_cancellation_barrier(
                deps.meeting_store.enroll_speaker_profile,
                display_name,
                embedding,
                quality=quality,
                profile_id=profile_id,
                preview_audio=preview_audio,
                preview_duration_ms=preview_duration_ms,
                preview_source="enrollment",
            )
        public_profile = SpeakerProfileSummary.from_store(profile).to_payload()
        public_capture = {
            "durationMs": int(snapshot.get("durationMs", 0) or 0),
            "rms": round(float(snapshot.get("rms", 0.0) or 0.0), 4),
            "peak": round(float(snapshot.get("peak", 0.0) or 0.0), 4),
            "quality": quality,
        }
        return web.json_response(
            {
                "apiVersion": REST_API_VERSION,
                "profile": public_profile,
                "capture": public_capture,
                "audioPersisted": True,
                "audioSentToProvider": False,
            },
            status=201,
        )
    except MeetingNotFound:
        return web.json_response({"message": "Speaker profile not found"}, status=404)
    except VoiceLibraryDisabled as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=422)
    except asyncio.CancelledError:
        handler_cancelled = True
        raise
    except Exception as exc:
        logger.warning("Voice Library enrollment failed: {}", type(exc).__name__)
        return web.json_response({"message": "The voice sample could not be completed. Try again."}, status=503)
    finally:

        async def cleanup_enrollment() -> None:
            native_capture_released = False
            try:
                native_capture_released, native_stop_cancel = await native_capture.stop()
                if native_stop_cancel is not None:
                    raise native_stop_cancel
                if not native_capture_released:
                    logger.error("Voice Library native cleanup was not accepted by the shell")
            except Exception as exc:
                logger.warning("Voice Library native cleanup failed: {}", type(exc).__name__)
            if capture is not None:
                try:
                    await asyncio.to_thread(capture.stop)
                except Exception as exc:
                    logger.warning("Voice Library reader cleanup failed: {}", type(exc).__name__)
                try:
                    capture.clear()
                except Exception as exc:
                    logger.warning("Voice Library buffer cleanup failed: {}", type(exc).__name__)
            if admission is not None:
                try:
                    await enrollment.release(
                        admission,
                        native_capture_released=native_capture_released,
                    )
                except Exception as exc:
                    logger.warning("Voice Library admission cleanup failed: {}", type(exc).__name__)
            if not native_capture_released:
                logger.error("Voice Library retained native-audio ownership after unconfirmed cleanup")

        _, cleanup_cancel = await await_with_delayed_cancellation(cleanup_enrollment())
        if cleanup_cancel is not None and not handler_cancelled:
            raise cleanup_cancel


@_redact_unknown_voice_library_failures
async def delete_speaker_profile(request: web.Request) -> web.Response:
    deps = _deps(request)
    try:
        async with _state(request).mutation_lock:
            deleted = await to_thread_cancellation_barrier(
                deps.meeting_store.delete_speaker_profile,
                request.match_info.get("profileId", ""),
            )
    except Exception as exc:
        logger.warning("Voice Library profile deletion failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "The speaker profile could not be deleted."},
            status=503,
        )
    if not deleted:
        return web.json_response({"message": "Speaker profile not found"}, status=404)
    return web.json_response({"apiVersion": REST_API_VERSION, "success": True})


@_redact_unknown_voice_library_failures
async def patch_speaker_profile(request: web.Request) -> web.Response:
    deps = _deps(request)
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("Expected JSON object")
        async with _state(request).mutation_lock:
            result = await to_thread_cancellation_barrier(
                deps.meeting_store.rename_speaker_profile,
                request.match_info.get("profileId", ""),
                str(raw.get("displayName", "")),
            )
        return web.json_response({"apiVersion": REST_API_VERSION, **result})
    except MeetingNotFound as exc:
        return web.json_response({"message": str(exc)}, status=404)
    except VoiceLibraryDisabled as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except Exception as exc:
        logger.warning("Voice Library profile rename failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "The speaker profile could not be updated."},
            status=503,
        )


@_redact_unknown_voice_library_failures
async def merge_speaker_profiles(request: web.Request) -> web.Response:
    deps = _deps(request)
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("Expected JSON object")
        async with _state(request).mutation_lock:
            result = await to_thread_cancellation_barrier(
                deps.meeting_store.merge_speaker_profiles,
                str(raw.get("targetProfileId", "")),
                str(raw.get("sourceProfileId", "")),
            )
        return web.json_response({"apiVersion": REST_API_VERSION, **result})
    except MeetingNotFound as exc:
        return web.json_response({"message": str(exc)}, status=404)
    except VoiceLibraryDisabled as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except Exception as exc:
        logger.warning("Voice Library profile merge failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "The speaker profiles could not be merged."},
            status=503,
        )


@_redact_unknown_voice_library_failures
async def split_speaker_profile(request: web.Request) -> web.Response:
    deps = _deps(request)
    try:
        async with _state(request).mutation_lock:
            result = await to_thread_cancellation_barrier(
                deps.meeting_store.split_speaker_profile,
                request.match_info.get("id", ""),
                request.match_info.get("speakerId", ""),
            )
        return web.json_response({"apiVersion": REST_API_VERSION, **result})
    except MeetingNotFound as exc:
        return web.json_response({"message": str(exc)}, status=404)
    except VoiceLibraryDisabled as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except Exception as exc:
        logger.warning("Voice Library profile split failed: {}", type(exc).__name__)
        return web.json_response(
            {"message": "The speaker profile could not be split."},
            status=503,
        )


async def diarization_component_status(request: web.Request) -> web.Response:
    return web.json_response(_diarization_payload(await _diarizer_status(_diarizer(request))))


async def install_diarization_component(request: web.Request) -> web.Response:
    diarizer = _diarizer(request)
    try:
        status = await diarizer.install(request.app[APP_HTTP_SESSION])
    except (OSError, RuntimeError, ValueError) as exc:
        return web.json_response(
            {"message": redact_text(str(exc))[:240] or "Local diarization install failed."},
            status=502,
        )
    return web.json_response(_diarization_payload(status))


async def delete_diarization_component(request: web.Request) -> web.Response:
    diarizer = _diarizer(request)
    deleted = await diarizer.delete_async()
    if not deleted:
        return web.json_response(
            {
                "apiVersion": REST_API_VERSION,
                "deleted": False,
                "message": "Local speaker separation is currently in use.",
            },
            status=409,
        )
    return web.json_response(_diarization_payload(await _diarizer_status(diarizer), deleted=True))


def register_voice_component_routes(
    app: web.Application,
    *,
    voice_library: VoiceLibraryProvider,
    enrollment: VoiceEnrollmentProvider,
    diarizer: DiarizerProvider,
    capture_runtime: VoiceCaptureRuntimePort | None = None,
) -> None:
    """Register the optional voice component domain.

    Three providers rather than one bundle: Voice Library data, native
    enrollment admission, and diarization share no operational collaborator.
    A single bundle would make model status materialize global audio state or a
    diarizer. Each route resolves only what it actually uses.
    """

    app[APP_VOICE_LIBRARY_DEPS] = voice_library
    app[APP_VOICE_ENROLLMENT] = enrollment
    app[APP_DIARIZER] = diarizer
    app[APP_VOICE_COMPONENT_STATE] = VoiceComponentState()
    resolved_capture_runtime: VoiceCaptureRuntimePort = capture_runtime or VoiceCaptureRuntime()
    app[APP_VOICE_CAPTURE_RUNTIME] = resolved_capture_runtime

    app.router.add_get("/api/meetings/speaker-model", speaker_model_status)
    app.router.add_post("/api/meetings/speaker-model", download_speaker_model)
    app.router.add_delete("/api/meetings/speaker-library", delete_speaker_library)
    app.router.add_get("/api/meetings/speaker-profiles", list_speaker_profiles)
    app.router.add_get("/api/meetings/speaker-profile-preview/{token}", speaker_profile_preview)
    app.router.add_post("/api/meetings/speaker-profiles/enroll", enroll_speaker_profile)
    app.router.add_post("/api/meetings/speaker-profiles/merge", merge_speaker_profiles)
    app.router.add_delete("/api/meetings/speaker-profiles/{profileId}", delete_speaker_profile)
    app.router.add_patch("/api/meetings/speaker-profiles/{profileId}", patch_speaker_profile)
    app.router.add_post("/api/meetings/{id}/speakers/{speakerId}/split-profile", split_speaker_profile)
    app.router.add_get("/api/meetings/diarization-component", diarization_component_status)
    app.router.add_post("/api/meetings/diarization-component", install_diarization_component)
    app.router.add_delete("/api/meetings/diarization-component", delete_diarization_component)
