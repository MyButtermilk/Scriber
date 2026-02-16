import asyncio
import json
import os
import re
import signal
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional
from urllib.parse import urlparse
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from loguru import logger

from src.audio_devices import (
    get_input_hostapi_priorities,
    is_input_device_compatible,
    list_unique_input_microphones,
    normalize_device_name,
    rank_hostapi,
)
from src.config import Config
from src.device_monitor import DeviceMonitor, devices_contain_name
from src.core.provider_capabilities import supports_direct_file_upload
from src.core.error_taxonomy import classify_error_message, is_retryable, user_message_for_category
from src.core.hot_path_tracer import HotPathTracer
from src.core.logging_setup import emit_event, setup_logging
from src.core.provider_circuit_breaker import ProviderCircuitBreaker
from src.core.state_machine import InvalidTransitionError, RecordingState, RecordingStateMachine
from src.core.ws_contracts import (
    audio_level_event,
    error_event,
    history_updated_event,
    input_warning_event,
    session_finished_event,
    session_started_event,
    status_event,
    transcript_event,
    transcribing_event,
    validate_event_payload,
)
from src.data.job_store import JobRecord, JobStore, JobType
from src.data.latency_metrics_store import LatencyMetricsStore
from src.pipeline import ScriberPipeline
from src.runtime.provider_router import ProviderRouter
from src.runtime.retry_scheduler import RetryScheduler
from src.youtube_api import YouTubeApiError, search_youtube_videos, get_video_by_id, extract_youtube_video_id
from src.youtube_download import YouTubeDownloadError, download_youtube_audio
from src.overlay import get_overlay, show_recording_overlay, show_initializing_overlay, show_transcribing_overlay, hide_recording_overlay, update_overlay_audio
from src import database as db

TranscriptStatus = Literal["completed", "processing", "failed", "recording", "stopped"]
TranscriptType = Literal["mic", "youtube", "file"]

_ALLOWED_ORIGINS_ENV = "SCRIBER_ALLOWED_ORIGINS"
_UPLOAD_MAX_BYTES_ENV = "SCRIBER_UPLOAD_MAX_BYTES"
_UPLOAD_MAX_MB_ENV = "SCRIBER_UPLOAD_MAX_MB"
_DEFAULT_UPLOAD_MAX_MB = 200
_DEFAULT_AUDIO_MAX_MB = 200  # Limit for extracted audio files
_DEFAULT_VIDEO_MAX_MB = 2048  # 2GB limit for raw video uploads (audio extracted)

# Video file extensions that require audio extraction
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".flv", ".wmv", ".m4v"}

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


def _safe_upload_filename(name: str) -> str:
    raw = (name or "").strip()
    base = Path(raw).name
    base = _INVALID_FILENAME_CHARS.sub("_", base).rstrip(" .")
    if not base or base in {".", ".."}:
        return "uploaded_file"
    stem = Path(base).stem
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        base = f"_{base}"
    return base


def _parse_allowed_origins() -> list[str]:
    raw = os.getenv(_ALLOWED_ORIGINS_ENV, "")
    if not raw:
        return []
    cleaned: list[str] = []
    for entry in raw.split(","):
        val = entry.strip().rstrip("/")
        if val:
            cleaned.append(val)
    return cleaned


def _origin_allowed(origin: str) -> bool:
    origin = (origin or "").strip()
    if not origin:
        return False
    allowed = _parse_allowed_origins()
    if "*" in allowed:
        return True
    if allowed:
        return origin in allowed
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if not host:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def _get_upload_max_bytes() -> int:
    raw_bytes = os.getenv(_UPLOAD_MAX_BYTES_ENV, "").strip()
    if raw_bytes:
        try:
            value = int(raw_bytes)
            if value > 0:
                return value
        except Exception:
            pass
    raw_mb = os.getenv(_UPLOAD_MAX_MB_ENV, "").strip()
    if raw_mb:
        try:
            value = float(raw_mb)
            if value > 0:
                return int(value * 1024 * 1024)
        except Exception:
            pass
    return _DEFAULT_UPLOAD_MAX_MB * 1024 * 1024


def _format_upload_limit(limit_bytes: int) -> str:
    return f"{limit_bytes / (1024 * 1024):.0f}MB"


def _get_video_max_bytes() -> int:
    """Get maximum bytes allowed for video file uploads (audio will be extracted)."""
    return _DEFAULT_VIDEO_MAX_MB * 1024 * 1024


def _get_audio_max_bytes() -> int:
    """Get maximum bytes allowed for audio files (after extraction from video)."""
    return _DEFAULT_AUDIO_MAX_MB * 1024 * 1024


async def _extract_audio_from_video(video_path: Path, output_dir: Path) -> Path:
    """
    Extract audio from a video file using ffmpeg.
    
    Returns the path to the extracted audio file (WebM/Opus format).
    Raises RuntimeError if extraction fails.
    """
    import shutil
    
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH (required for video audio extraction).")
    
    # Output as audio-only WebM/Opus for efficient upload across STT providers.
    audio_filename = video_path.stem + ".webm"
    audio_path = output_dir / audio_filename
    
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",  # Overwrite output
        "-i", str(video_path),
        "-vn",  # No video
        "-c:a", "libopus",
        "-b:a", "64k",  # Good speech quality with small upload size
        "-ar", "16000",
        "-ac", "1",  # Mono (sufficient for transcription)
        str(audio_path),
    ]
    
    logger.debug(f"Extracting audio from video: {video_path.name}")
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    _, stderr = await proc.communicate()
    
    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
        raise RuntimeError(f"ffmpeg audio extraction failed: {err_msg or f'exit code {proc.returncode}'}")
    
    if not audio_path.exists():
        raise RuntimeError("Audio extraction completed but output file not found.")
    
    logger.debug(f"Audio extracted: {audio_path.name} ({audio_path.stat().st_size / (1024*1024):.1f}MB)")
    return audio_path


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _probe_media_duration_seconds(file_path: Path) -> float | None:
    """Best-effort media duration probe via ffprobe."""
    import math
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        logger.debug(f"ffprobe failed for {file_path.name}: {exc}")
        return None

    if proc.returncode != 0:
        return None

    raw = (proc.stdout or "").strip()
    if not raw:
        return None

    try:
        seconds = float(raw.splitlines()[0])
    except (TypeError, ValueError):
        return None

    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _format_date_label(ts: datetime) -> str:
    now = datetime.now(ts.tzinfo)
    today = now.date()
    if ts.date() == today:
        return f"Today, {ts.strftime('%H:%M')}"
    if ts.date() == (today - timedelta(days=1)):
        return "Yesterday"
    return ts.strftime("%Y-%m-%d")


def _preview_words(text: str, max_words: int = 5) -> list[str]:
    if max_words <= 0:
        return []
    words: list[str] = []
    for match in re.finditer(r"\S+", text or ""):
        words.append(match.group(0))
        if len(words) >= max_words:
            break
    return words


def _preview_from_words(words: list[str], max_words: int = 5, *, has_more: bool = False) -> str:
    if not words:
        return ""
    preview = " ".join(words[:max_words])
    if has_more:
        preview += "..."
    return preview


def _normalize_hotkey_for_backend(display_hotkey: str) -> str:
    # Frontend records like "Ctrl + Shift + S"; keyboard expects "ctrl+shift+s".
    hotkey = (display_hotkey or "").strip()
    if not hotkey:
        return ""
    parts = [p.strip() for p in hotkey.split("+")]
    mapped: list[str] = []
    for part in parts:
        key = part.strip().lower()
        if not key:
            continue
        if key in {"control", "ctrl"}:
            mapped.append("ctrl")
        elif key == "shift":
            mapped.append("shift")
        elif key in {"alt", "option"}:
            mapped.append("alt")
        elif key in {"meta", "cmd", "command", "win", "windows"}:
            mapped.append("windows")
        else:
            mapped.append(key.lower())
    return "+".join(mapped)


def _hotkey_to_display(hotkey: str) -> str:
    # Backend stores like "ctrl+alt+s"; render like "Ctrl + Alt + S".
    parts = [p.strip() for p in (hotkey or "").split("+") if p.strip()]
    out: list[str] = []
    for p in parts:
        if p == "ctrl":
            out.append("Ctrl")
        elif p == "alt":
            out.append("Alt")
        elif p == "shift":
            out.append("Shift")
        elif p in {"windows", "win"}:
            out.append("Meta")
        else:
            out.append(p.upper() if len(p) == 1 else p)
    return " + ".join(out) if out else ""


def _normalize_device_name(name: str) -> str:
    return normalize_device_name(name)


@dataclass
class TranscriptRecord:
    id: str
    title: str
    date: str
    duration: str
    status: TranscriptStatus
    type: TranscriptType
    language: str
    step: str = ""
    source_url: str = ""
    channel: str = ""
    thumbnail_url: str = ""
    content: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    summary: str = ""

    _started_at_monotonic: float | None = None
    _last_segment: str = ""
    _preview: str = ""
    _preview_words: list[str] = field(default_factory=list)
    _preview_has_more: bool = False
    _content_loaded: bool = True
    _summary_loaded: bool = True

    def to_public(self, *, include_content: bool) -> dict[str, Any]:
        # Dynamically calculate date label based on created_at to ensure
        # "Today" and "Yesterday" are always accurate relative to current time
        display_date = self.date
        if self.created_at:
            try:
                created_ts = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
                display_date = _format_date_label(created_ts)
            except (ValueError, TypeError):
                pass  # Fall back to stored date if parsing fails
        
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "date": display_date,
            "duration": self.duration,
            "status": self.status,
            "type": self.type,
            "language": self.language,
            "step": self.step,
            "sourceUrl": self.source_url,
            "channel": self.channel,
            "thumbnailUrl": self.thumbnail_url,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        
        preview = self._preview
        if not preview and self.content:
            sample_words = _preview_words(self.content, max_words=6)
            preview = _preview_from_words(
                sample_words[:5],
                max_words=5,
                has_more=len(sample_words) > 5,
            )
        data["preview"] = preview or self.title

        if include_content:
            data["content"] = self.content
            data["summary"] = self.summary
        return data

    def start(self) -> None:
        self._started_at_monotonic = time.monotonic()

    def finish(self, status: TranscriptStatus) -> None:
        self.status = status
        elapsed = 0.0
        if self._started_at_monotonic is not None:
            elapsed = time.monotonic() - self._started_at_monotonic
        self.duration = _format_duration(elapsed)
        self.updated_at = datetime.now().isoformat()

    def append_final_text(self, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        # Avoid repeats from some providers.
        if self._last_segment == cleaned:
            return
        if self.content:
            self.content = f"{self.content}\n\n{cleaned}"
        else:
            self.content = cleaned
        self._last_segment = cleaned
        segment_words = _preview_words(cleaned, max_words=128)
        if segment_words:
            if len(self._preview_words) >= 5:
                self._preview_has_more = True
            else:
                needed = 5 - len(self._preview_words)
                self._preview_words.extend(segment_words[:needed])
                if len(segment_words) > needed:
                    self._preview_has_more = True
            self._preview = _preview_from_words(
                self._preview_words,
                max_words=5,
                has_more=self._preview_has_more,
            )
        self.updated_at = datetime.now().isoformat()


class ScriberWebController:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        job_store: JobStore | None = None,
        latency_metrics_store: LatencyMetricsStore | None = None,
    ):
        self._loop = loop
        self._clients: set[web.WebSocketResponse] = set()
        self._clients_lock = asyncio.Lock()
        self._clients_snapshot: tuple[web.WebSocketResponse, ...] = ()
        self._clients_dirty = False

        self._pipeline: Optional[ScriberPipeline] = None
        self._pipeline_task: Optional[asyncio.Task] = None
        self._ptt_task: Optional[asyncio.Task] = None
        self._active_provider: str | None = None
        # Track running file/YouTube transcription tasks by transcript ID
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._job_store = job_store or JobStore()
        self._job_ids_by_transcript: dict[str, str] = {}
        self._latency_metrics_store = latency_metrics_store or LatencyMetricsStore()
        self._job_max_attempts = max(1, int(os.getenv("SCRIBER_JOB_MAX_ATTEMPTS", "3")))
        self._job_retry_base_seconds = max(0.1, float(os.getenv("SCRIBER_JOB_RETRY_BASE_SEC", "5")))
        self._job_retry_max_seconds = max(
            self._job_retry_base_seconds,
            float(os.getenv("SCRIBER_JOB_RETRY_MAX_SEC", "120")),
        )
        provider_fallbacks = [
            p.strip()
            for p in os.getenv("SCRIBER_STT_FALLBACKS", "").split(",")
            if p.strip()
        ]
        breaker = ProviderCircuitBreaker(
            failure_threshold=max(1, int(os.getenv("SCRIBER_BREAKER_FAILURE_THRESHOLD", "3"))),
            cooldown_seconds=max(1.0, float(os.getenv("SCRIBER_BREAKER_COOLDOWN_SEC", "30"))),
        )
        self._provider_breaker = breaker
        self._provider_router = ProviderRouter(
            default_provider_getter=lambda: str(getattr(Config, "DEFAULT_STT_SERVICE", "") or ""),
            fallbacks=provider_fallbacks,
            breaker=breaker,
        )
        self._retry_scheduler = RetryScheduler(
            loop=self._loop,
            trigger=lambda: self.resume_pending_jobs(limit=25),
        )
        self._validate_ws_contracts = os.getenv("SCRIBER_VALIDATE_WS_CONTRACTS", "0").strip() in {
            "1",
            "true",
            "True",
        }
        self._keyboard = None

        self._is_listening = False
        self._is_stopping = False  # Track if stop is in progress
        self._listening_lock = asyncio.Lock()  # Prevent race conditions on rapid hotkey presses
        self._status = "Stopped"
        self._session_id: str | None = None
        self._recording_state_machine = RecordingStateMachine()
        self._hot_path_tracers: dict[str, HotPathTracer] = {}
        self._hot_path_reports_emitted: set[str] = set()

        self._current: Optional[TranscriptRecord] = None
        self._current_lock = threading.Lock()
        self._history: list[TranscriptRecord] = []
        self._history_by_id: dict[str, TranscriptRecord] = {}
        self._last_audio_broadcast = 0.0
        self._overlay_audio_enabled = False
        self._mic_low_level_since: float | None = None
        self._mic_input_warning = ""
        try:
            self._mic_low_rms_threshold = max(
                0.0,
                float(os.getenv("SCRIBER_MIC_LOW_RMS_THRESHOLD", "0.001") or 0.001),
            )
        except Exception:
            self._mic_low_rms_threshold = 0.001
        try:
            self._mic_low_rms_clear_threshold = max(
                self._mic_low_rms_threshold,
                float(os.getenv("SCRIBER_MIC_LOW_RMS_CLEAR_THRESHOLD", "0.0025") or 0.0025),
            )
        except Exception:
            self._mic_low_rms_clear_threshold = 0.0025
        try:
            self._mic_low_rms_warn_after_secs = max(
                1.0,
                float(os.getenv("SCRIBER_MIC_LOW_RMS_WARN_AFTER_SECS", "6.0") or 6.0),
            )
        except Exception:
            self._mic_low_rms_warn_after_secs = 6.0
        self._history_broadcast_last = 0.0
        self._history_broadcast_handle: asyncio.TimerHandle | None = None       
        self._history_broadcast_interval = 0.25

        self._downloads_dir = Path(os.getenv("SCRIBER_DOWNLOADS_DIR", "downloads")).resolve()

        # Overlay is initialized in background after server starts (see _prewarm_cache)
        # This avoids blocking app startup while ensuring overlay is ready for first hotkey
        self._overlay = None
        self._overlay_lock = asyncio.Lock()
        
        # Initialize database schema only (transcript loading happens in background)
        db.init_database()
        self._transcripts_loaded = False

        self._device_monitor = DeviceMonitor(
            sample_rate=int(getattr(Config, "SAMPLE_RATE", 16000) or 16000),
            channels=max(1, int(getattr(Config, "CHANNELS", 1) or 1)),
        )
        self._device_monitor.on_devices_changed(self._on_devices_changed)
        self._device_monitor.start()

    @staticmethod
    def _trace_id_for(value: str | None) -> str | None:
        if not value:
            return None
        if value.startswith("tr_"):
            return value
        return f"tr_{value}"

    def _on_devices_changed(self, devices: list[dict[str, str]]) -> None:
        """Bridge device monitor thread callbacks onto the asyncio loop."""
        try:
            self._loop.call_soon_threadsafe(
                lambda d=devices: asyncio.create_task(self._handle_devices_changed(d))
            )
        except Exception as exc:
            logger.warning(f"Failed to schedule devices-changed handler: {exc}")

    async def _handle_devices_changed(self, devices: list[dict[str, str]]) -> None:
        favorite = (getattr(Config, "FAVORITE_MIC", "") or "").strip()
        favorite_restored = False
        restored_device_id = ""
        restored_device_label = ""

        if favorite and favorite != "default":
            favorite_restored, restored_device_id, restored_device_label = devices_contain_name(devices, favorite)
            if favorite_restored and not self._is_listening and restored_device_id:
                if Config.MIC_DEVICE != restored_device_id:
                    Config.set_mic_device(restored_device_id)
                    logger.info(f"[DeviceMonitor] Favorite mic restored: {restored_device_label}")

        payload: dict[str, Any] = {
            "type": "microphones_updated",
            "devices": devices,
            "favoriteMicRestored": favorite_restored,
        }
        if favorite_restored:
            payload["restoredDeviceId"] = restored_device_id
            payload["restoredDeviceLabel"] = restored_device_label
        await self.broadcast(payload)

    def _emit_workflow_event(
        self,
        *,
        message: str,
        event: str,
        workflow: str,
        stage: str,
        level: str = "INFO",
        component: str = "web_api",
        session_id: str | None = None,
        record: TranscriptRecord | None = None,
        provider: str | None = None,
        duration_ms: int | float | None = None,
        outcome: str | None = None,
        milestone: bool = False,
        error_category: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        transcript_id = record.id if record else None
        trace_source = session_id or transcript_id
        job_id = self._job_ids_by_transcript.get(transcript_id or "") if transcript_id else None
        emit_event(
            logger.bind(component=component),
            message,
            level=level,
            event=event,
            workflow=workflow,
            stage=stage,
            trace_id=self._trace_id_for(trace_source),
            session_id=session_id,
            transcript_id=transcript_id,
            job_id=job_id,
            provider=provider,
            duration_ms=duration_ms,
            outcome=outcome,
            milestone=milestone,
            error_category=error_category,
            meta=meta,
        )
    
    def _register_task(self, transcript_id: str, task: asyncio.Task) -> None:
        """Register a background task for a transcript."""
        # Use call_soon_threadsafe to ensure thread safety if called from callbacks
        self._running_tasks[transcript_id] = task
        task.add_done_callback(lambda _: self._loop.call_soon_threadsafe(lambda: self._unregister_task(transcript_id)))

    def _unregister_task(self, transcript_id: str) -> None:
        """Unregister a background task."""
        if transcript_id in self._running_tasks:
            del self._running_tasks[transcript_id]

    def _enqueue_background_job(
        self,
        rec: TranscriptRecord,
        *,
        job_type: JobType,
        payload: dict[str, Any],
    ) -> None:
        try:
            job = self._job_store.enqueue(
                transcript_id=rec.id,
                job_type=job_type,
                payload=payload,
            )
            self._job_ids_by_transcript[rec.id] = job.id
        except Exception as exc:  # pragma: no cover - best effort persistence
            logger.warning(f"Failed to persist queued job for transcript {rec.id}: {exc}")

    def _set_job_running(self, transcript_id: str) -> None:
        job_id = self._job_ids_by_transcript.get(transcript_id)
        if not job_id:
            return
        try:
            self._job_store.mark_running(job_id)
        except Exception as exc:  # pragma: no cover - best effort persistence
            logger.warning(f"Failed to mark job running for transcript {transcript_id}: {exc}")

    def _provider_candidates(self) -> list[str]:
        return self._provider_router.candidates()

    def _select_available_provider(self) -> str:
        return self._provider_router.select()

    def _record_provider_success(self, provider: str) -> None:
        self._provider_router.record_success(provider)

    def _record_provider_failure(self, provider: str, error: Exception | str) -> None:
        self._provider_router.record_failure(provider, error)

    def _retry_delay_seconds(self, attempts: int) -> float:
        exponent = max(0, int(attempts) - 1)
        delay = self._job_retry_base_seconds * (2 ** exponent)
        return min(self._job_retry_max_seconds, delay)

    def _schedule_retry_scan(self, delay_seconds: float) -> None:
        self._retry_scheduler.schedule_in(delay_seconds)

    def _schedule_next_retry_scan_from_store(self) -> None:
        try:
            delay = self._job_store.seconds_until_next_retry()
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning(f"Failed to query next retry delay: {exc}")
            return
        if delay is None:
            self._retry_scheduler.cancel()
            return
        self._schedule_retry_scan(delay)

    def _schedule_retry_if_allowed(self, rec: TranscriptRecord, error: Exception | str) -> bool:
        job_id = self._job_ids_by_transcript.get(rec.id)
        if not job_id:
            return False
        job = self._job_store.get(job_id)
        if not job:
            return False
        category = classify_error_message(str(error))
        if not is_retryable(category):
            return False
        attempts = max(1, int(job.attempts))
        if attempts >= self._job_max_attempts:
            return False

        delay_seconds = self._retry_delay_seconds(attempts)
        retry_at = (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
        retry_label = int(round(delay_seconds))
        rec.status = "processing"
        rec.step = f"Retrying in {retry_label}s ({attempts}/{self._job_max_attempts})"
        rec.updated_at = datetime.now().isoformat()
        try:
            self._job_store.set_retry(job_id, retry_at=retry_at, last_error=str(error))
        except Exception as exc:  # pragma: no cover - best effort persistence
            logger.warning(f"Failed to persist retry state for transcript {rec.id}: {exc}")
            return False
        self._schedule_retry_scan(delay_seconds)
        logger.warning(
            f"Scheduled retry for transcript {rec.id} in {delay_seconds:.1f}s "
            f"(attempt {attempts}/{self._job_max_attempts})"
        )
        return True

    def _sync_job_status(self, rec: TranscriptRecord) -> None:
        job_id = self._job_ids_by_transcript.get(rec.id)
        if not job_id:
            return
        try:
            if rec.status == "completed":
                self._job_store.mark_completed(job_id)
            elif rec.status == "failed":
                self._job_store.mark_failed(job_id, last_error=rec.step or "Transcription failed")
            elif rec.status == "stopped":
                self._job_store.mark_canceled(job_id, last_error=rec.step or "Stopped by user")
        except Exception as exc:  # pragma: no cover - best effort persistence
            logger.warning(f"Failed to sync job status for transcript {rec.id}: {exc}")

    def _schedule_youtube_job(self, rec: TranscriptRecord, *, resumed: bool = False) -> None:
        async def _runner() -> None:
            self._set_job_running(rec.id)
            try:
                provider = self._select_available_provider()
            except Exception as exc:
                if not self._schedule_retry_if_allowed(rec, exc):
                    rec.status = "failed"
                    rec.step = "Failed"
                    rec.append_final_text(f"[Error] {exc}")
                self._sync_job_status(rec)
                rec.updated_at = datetime.now().isoformat()
                self._save_transcript_to_db(rec)
                await self._broadcast_history_updated()
                return
            try:
                await self._run_youtube_transcription(rec, provider=provider)
                if rec.status == "completed":
                    self._record_provider_success(provider)
                elif rec.status == "failed":
                    self._record_provider_failure(provider, rec.step)
            except asyncio.CancelledError:
                if rec.status == "processing":
                    rec.status = "stopped"
                    rec.step = "Stopped by user"
                raise
            finally:
                self._sync_job_status(rec)

        task_name = f"youtube_transcribe_{rec.id}" if not resumed else f"youtube_resume_{rec.id}"
        task = asyncio.create_task(_runner(), name=task_name)
        self._register_task(rec.id, task)

    def _schedule_file_job(self, rec: TranscriptRecord, file_path: Path, *, resumed: bool = False) -> None:
        async def _runner() -> None:
            self._set_job_running(rec.id)
            try:
                provider = self._select_available_provider()
            except Exception as exc:
                if not self._schedule_retry_if_allowed(rec, exc):
                    rec.status = "failed"
                    rec.step = "Failed"
                    rec.append_final_text(f"[Error] {exc}")
                self._sync_job_status(rec)
                rec.updated_at = datetime.now().isoformat()
                self._save_transcript_to_db(rec)
                await self._broadcast_history_updated()
                return
            try:
                await self._run_file_transcription(rec, file_path, provider=provider)
                if rec.status == "completed":
                    self._record_provider_success(provider)
                elif rec.status == "failed":
                    self._record_provider_failure(provider, rec.step)
            except asyncio.CancelledError:
                if rec.status == "processing":
                    rec.status = "stopped"
                    rec.step = "Stopped by user"
                raise
            finally:
                self._sync_job_status(rec)

        task_name = f"file_transcribe_{rec.id}" if not resumed else f"file_resume_{rec.id}"
        task = asyncio.create_task(_runner(), name=task_name)
        self._register_task(rec.id, task)

    def _build_processing_record_from_job(self, job: JobRecord) -> TranscriptRecord:
        payload = job.payload or {}
        created_at = job.created_at or datetime.now().isoformat()
        created_dt = datetime.now()
        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                created_dt = datetime.now()
        title = str(payload.get("title", "") or "").strip()
        if not title:
            title = "YouTube" if job.job_type == JobType.YOUTUBE else "File"

        source_url = str(payload.get("url", "") or "").strip() if job.job_type == JobType.YOUTUBE else str(payload.get("path", "") or "").strip()
        rec = TranscriptRecord(
            id=job.transcript_id,
            title=title,
            date=_format_date_label(created_dt),
            duration=str(payload.get("duration", "") or "--:--"),
            status="processing",
            type="youtube" if job.job_type == JobType.YOUTUBE else "file",
            language=str(payload.get("language", "") or Config.LANGUAGE or "auto"),
            step="Queued (resumed)",
            source_url=source_url,
            channel=str(payload.get("channel", "") or ""),
            thumbnail_url=str(payload.get("thumbnailUrl", "") or ""),
            created_at=created_at,
            updated_at=datetime.now().isoformat(),
            content="",
            summary="",
            _content_loaded=True,
            _summary_loaded=True,
        )
        return rec

    def _fail_resumed_job(self, rec: TranscriptRecord, message: str) -> None:
        rec.status = "failed"
        rec.step = "Failed"
        rec.append_final_text(f"[Error] {message}")
        rec.updated_at = datetime.now().isoformat()
        self._sync_job_status(rec)
        self._save_transcript_to_db(rec)

    @staticmethod
    def _timeout_seconds(env_key: str, default_seconds: float) -> float:
        raw = os.getenv(env_key, "").strip()
        if not raw:
            return default_seconds
        try:
            parsed = float(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
        return default_seconds

    async def _await_with_timeout(
        self,
        operation: Awaitable[Any],
        *,
        timeout_seconds: float,
        timeout_label: str,
    ) -> Any:
        try:
            return await asyncio.wait_for(operation, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{timeout_label} timed out after {timeout_seconds:.1f}s") from exc

    async def resume_pending_jobs(self, *, limit: int = 25) -> int:
        reset_count = self._job_store.reset_running_to_queued()
        pending_jobs = self._job_store.list_pending(limit=limit)
        resumed_count = 0

        for job in pending_jobs:
            if job.transcript_id in self._running_tasks:
                continue

            rec = self._get_history_record(job.transcript_id)
            if rec and rec.status in ("completed", "failed", "stopped"):
                self._sync_job_status(rec)
                continue
            if rec is None:
                rec = self._build_processing_record_from_job(job)
                self._add_to_history(rec)

            self._job_ids_by_transcript[rec.id] = job.id

            if job.job_type == JobType.YOUTUBE:
                if not rec.source_url:
                    self._fail_resumed_job(rec, "Missing source URL for resumed YouTube job.")
                    continue
                rec.step = "Queued (resumed)"
                rec.updated_at = datetime.now().isoformat()
                self._schedule_youtube_job(rec, resumed=True)
                resumed_count += 1
                continue

            file_path_raw = str(job.payload.get("path", "") or "").strip()
            if not file_path_raw:
                self._fail_resumed_job(rec, "Missing source file path for resumed file transcription.")
                continue
            file_path = Path(file_path_raw)
            if not file_path.exists():
                self._fail_resumed_job(rec, "Source file is no longer available for resumed file transcription.")
                continue
            rec.source_url = str(file_path)
            rec.step = "Queued (resumed)"
            rec.updated_at = datetime.now().isoformat()
            self._schedule_file_job(rec, file_path, resumed=True)
            resumed_count += 1

        if reset_count or resumed_count:
            await self._broadcast_history_updated()
            logger.info(
                f"Job resume startup scan: reset_running={reset_count}, resumed={resumed_count}, pending={len(pending_jobs)}"
            )
        self._schedule_next_retry_scan_from_store()
        return resumed_count

    def _set_recording_state(self, target: RecordingState, *, context: str = "") -> None:
        try:
            event = self._recording_state_machine.transition(target)
            if event:
                logger.debug(
                    f"Recording state transition ({context or 'unknown'}): "
                    f"{event.source.value} -> {event.target.value}"
                )
        except InvalidTransitionError as exc:
            logger.debug(f"Ignoring invalid recording state transition ({context or 'unknown'}): {exc}")

    def _start_hot_path_tracer(self, session_id: str) -> None:
        tracer = HotPathTracer(session_id)
        tracer.mark("hotkey_received")
        self._hot_path_tracers[session_id] = tracer
        self._hot_path_reports_emitted.discard(session_id)

    def _mark_hot_path(self, session_id: str | None, marker: str) -> None:
        if not session_id or not marker:
            return
        tracer = self._hot_path_tracers.get(session_id)
        if not tracer or tracer.has_mark(marker):
            return
        tracer.mark(marker)

    def _emit_hot_path_report_once(self, session_id: str | None) -> None:
        if not session_id:
            return
        if session_id in self._hot_path_reports_emitted:
            return
        tracer = self._hot_path_tracers.get(session_id)
        if not tracer:
            return
        if not tracer.has_mark("hotkey_received") or not tracer.has_mark("first_paste"):
            return
        report = tracer.report()
        if report:
            logger.info(f"Hot path timing ({session_id[:8]}): {report}")
            self._emit_workflow_event(
                message=f"Hot path timing captured ({session_id[:8]})",
                event="metrics.hot_path.reported",
                workflow="live_mic",
                stage="hot_path_report",
                component="web_api",
                session_id=session_id,
                record=self._current,
                milestone=True,
                outcome="success",
                duration_ms=report.get("total_ms"),
                meta=report,
            )
            try:
                self._latency_metrics_store.record(session_id, report)
            except Exception as exc:  # pragma: no cover - best effort persistence
                logger.warning(f"Failed to persist hot path timing for {session_id[:8]}: {exc}")
            self._hot_path_reports_emitted.add(session_id)

    def _clear_hot_path_tracer(self, session_id: str | None) -> None:
        if not session_id:
            return
        self._hot_path_tracers.pop(session_id, None)
        self._hot_path_reports_emitted.discard(session_id)

    def _get_overlay(self):
        """Get or create the overlay instance and ensure callback is connected."""
        # get_overlay will create if needed, or update callback if already exists
        on_stop = lambda: self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.stop_listening())
        )
        self._overlay = get_overlay(on_stop=on_stop)
        return self._overlay
    
    def _load_transcripts_from_db(self) -> None:
        """Load transcript metadata from database on startup.

        PERFORMANCE OPTIMIZATION: Uses lazy content loading - only metadata is
        loaded into memory. Content is loaded on-demand via get_transcript().
        This reduces memory usage by 80-90% for large transcript lists.
        """
        try:
            # Use metadata-only loader for reduced memory footprint
            saved = db.load_transcript_metadata()
            for data in saved:
                # Skip processing/recording transcripts (incomplete)
                if data.get("status") in ("processing", "recording"):
                    continue
                rec = TranscriptRecord(
                    id=data.get("id", ""),
                    title=data.get("title", ""),
                    date=data.get("date", ""),
                    duration=data.get("duration", ""),
                    status=data.get("status", "completed"),
                    type=data.get("type", "mic"),
                    language=data.get("language", ""),
                    step=data.get("step", ""),
                    source_url=data.get("sourceUrl", ""),
                    channel=data.get("channel", ""),
                    thumbnail_url=data.get("thumbnailUrl", ""),
                    # Content is NOT loaded - lazy loaded on demand
                    content="",
                    created_at=data.get("createdAt", ""),
                    updated_at=data.get("updatedAt", ""),
                    summary="",  # Summary also lazy loaded
                    _preview=data.get("_previewText", "") or "",
                    _content_loaded=False,
                    _summary_loaded=False,
                )
                self._history.append(rec)
                if rec.id:
                    self._history_by_id[rec.id] = rec
            logger.info(f"Loaded {len(self._history)} transcript metadata (lazy content loading enabled)")
        except Exception as e:
            logger.error(f"Failed to load transcripts from database: {e}")
    
    def _save_transcript_to_db(self, record: TranscriptRecord) -> None:
        """Save a transcript to the database."""
        try:
            db.save_transcript(record)
        except Exception as e:
            logger.error(f"Failed to save transcript to database: {e}")

    def _add_to_history(self, record: TranscriptRecord) -> None:
        """Insert a transcript into history and index it by ID."""
        self._history.insert(0, record)
        if record.id:
            self._history_by_id[record.id] = record

    def _remove_from_history(self, transcript_id: str) -> Optional[TranscriptRecord]:
        """Remove a transcript from history and index; return removed record."""
        rec = self._history_by_id.pop(transcript_id, None)
        if not rec:
            return None
        for i, item in enumerate(self._history):
            if item.id == transcript_id:
                self._history.pop(i)
                break
        return rec

    def _get_history_record(self, transcript_id: str) -> Optional[TranscriptRecord]:
        """Get a transcript by ID from the history index."""
        return self._history_by_id.get(transcript_id)

    def get_state(self) -> dict[str, Any]:
        with self._current_lock:
            current = self._current
        has_background_processing = any(
            task is not None and not task.done()
            for task in self._running_tasks.values()
        )
        return {
            "listening": self._is_listening,
            "status": self._status,
            "inputWarning": self._mic_input_warning,
            "current": current.to_public(include_content=True) if current else None,
            "sessionId": self._session_id,
            "backgroundProcessing": has_background_processing,
            "recordingState": self._recording_state_machine.state.value,
        }

    def get_hot_path_metrics(self, *, limit: int = 50) -> dict[str, Any]:
        query_limit = max(1, min(500, int(limit)))
        summary = self._latency_metrics_store.summarize(limit=query_limit)
        latest = self._latency_metrics_store.latest(limit=query_limit)
        items = [
            {
                "sessionId": metric.session_id,
                "totalMs": metric.total_ms,
                "segments": metric.segments,
                "createdAt": metric.created_at,
            }
            for metric in latest
        ]
        return {"summary": summary, "items": items, "limit": query_limit}

    async def add_client(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.add(ws)
            self._clients_dirty = True

    async def remove_client(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.discard(ws)
            self._clients_dirty = True

    async def broadcast(self, payload: dict[str, Any]) -> None:
        if self._validate_ws_contracts:
            validate_event_payload(payload)
        msg = json.dumps(payload, ensure_ascii=False)
        if self._clients_dirty:
            async with self._clients_lock:
                if self._clients_dirty:
                    self._clients_snapshot = tuple(self._clients)
                    self._clients_dirty = False
        clients = self._clients_snapshot
        if not clients:
            return
        
        async def send_safe(ws: web.WebSocketResponse):
            """Send message to client, return ws if failed or closed."""
            if ws.closed:
                return ws
            try:
                await ws.send_str(msg)
                return None
            except Exception:
                return ws
        
        # Send to all clients in parallel
        results = await asyncio.gather(*[send_safe(ws) for ws in clients], return_exceptions=True)
        dead = [r for r in results if r is not None and isinstance(r, web.WebSocketResponse)]
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)
                self._clients_dirty = True

    def _set_status(self, status: str, *, session_id: str | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            return
        self._status = status
        if session_id is None:
            session_id = self._session_id
        payload = status_event(status, self._is_listening, session_id=session_id)
        payload["inputWarning"] = self._mic_input_warning
        # status changes can happen from non-async callbacks; schedule the broadcast.
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.broadcast(payload))
        )

    def _set_input_warning(self, message: str, *, session_id: str | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            return
        normalized = str(message or "").strip()
        if normalized == self._mic_input_warning:
            return
        self._mic_input_warning = normalized
        if session_id is None:
            session_id = self._session_id
        payload = input_warning_event(
            bool(normalized),
            message=normalized,
            session_id=session_id,
        )
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.broadcast(payload))
        )

    def _clear_input_warning_state(self, *, session_id: str | None = None, broadcast: bool = True) -> None:
        if session_id is not None and session_id != self._session_id:
            return
        self._mic_low_level_since = None
        if broadcast:
            self._set_input_warning("", session_id=session_id)
        else:
            self._mic_input_warning = ""

    def _update_input_warning(self, rms: float, *, session_id: str | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            return

        level = max(0.0, float(rms))
        now = time.monotonic()

        if not self._is_listening:
            self._clear_input_warning_state(session_id=session_id, broadcast=False)
            return

        if level >= self._mic_low_rms_clear_threshold:
            self._mic_low_level_since = None
            if self._mic_input_warning:
                self._set_input_warning("", session_id=session_id)
            return

        if level > self._mic_low_rms_threshold:
            return

        if self._mic_low_level_since is None:
            self._mic_low_level_since = now
            return

        if self._mic_input_warning:
            return

        if now - self._mic_low_level_since >= self._mic_low_rms_warn_after_secs:
            self._set_input_warning(
                "Sehr niedriger Eingangspegel. Bitte Windows-Mikrofonlautstarke und Datenschutzberechtigung prufen.",
                session_id=session_id,
            )

    def _on_audio_level(self, rms: float, *, session_id: str | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            return
        self._update_input_warning(rms, session_id=session_id)
        # Called from the sounddevice callback thread; throttle broadcasts to ~30fps.
        now = time.monotonic()
        if now - self._last_audio_broadcast < 0.033:  # ~30fps
            return
        self._last_audio_broadcast = now
        # Update native overlay waveform only when recording overlay is active
        if self._overlay_audio_enabled:
            update_overlay_audio(rms)
        if session_id is None:
            session_id = self._session_id
        payload = audio_level_event(float(rms), session_id=session_id)
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.broadcast(payload))
        )

    def _on_transcription(self, text: str, is_final: bool, *, session_id: str | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            return
        logger.debug(f"Transcription received: final={is_final}, len={len(text) if text else 0}")
        if is_final:
            self._mark_hot_path(session_id or self._session_id, "first_final_token")
            with self._current_lock:
                if self._current and (session_id is None or self._current.id == session_id):
                    self._current.append_final_text(text)
            self._emit_workflow_event(
                message="Final transcript chunk received",
                event="pipeline.transcript.final",
                workflow="live_mic",
                stage="transcript_done",
                component="pipeline",
                session_id=session_id or self._session_id,
                record=self._current,
                provider=self._active_provider,
                outcome="success",
                meta={"chars": len(text or "")},
            )
        if session_id is None:
            session_id = self._session_id
        payload = transcript_event(text, bool(is_final), session_id=session_id)
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(payload)))

    def _on_pipeline_done(self, task: asyncio.Task, *, session_id: str | None = None) -> None:
        # Ignore completions from tasks that are no longer the active live pipeline.
        # This prevents stale callbacks from clobbering a newer session's state.
        if task is not self._pipeline_task:
            logger.debug("Ignoring completed pipeline task that is no longer active")
            return

        async def _safe_cleanup():
            """Cleanup state with proper lock protection."""
            async with self._listening_lock:
                if task is not self._pipeline_task:
                    return
                self._is_listening = False
                self._is_stopping = False
                self._pipeline = None
                self._pipeline_task = None
                self._active_provider = None
                if session_id is None or session_id == self._session_id:
                    self._session_id = None
                self._set_recording_state(RecordingState.IDLE, context="_on_pipeline_done_cleanup")
                self._clear_hot_path_tracer(session_id)
        
        async def _broadcast_error(error_msg: str):
            """Broadcast error to frontend."""
            await self.broadcast(error_event(error_msg, session_id=session_id))
        
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - runtime dependent        
            logger.error(f"Pipeline error: {exc}")
            self._record_provider_failure(self._active_provider or "", exc)
            self._set_recording_state(RecordingState.FAILED, context="_on_pipeline_done_error")
            self._set_status("Error", session_id=session_id)
            # Hide overlay when pipeline fails to prevent it staying stuck at "Preparing..."
            self._overlay_audio_enabled = False
            hide_recording_overlay()
            
            category = classify_error_message(str(exc))
            user_msg = user_message_for_category(category)
            logger.warning(f"Pipeline task failure category={category.value}: {exc}")
            self._emit_workflow_event(
                message=f"Pipeline task failed: {user_msg}",
                event="pipeline.session.failed",
                workflow="live_mic",
                stage="pipeline_error",
                level="ERROR",
                component="pipeline",
                session_id=session_id,
                record=self._current,
                provider=self._active_provider,
                milestone=True,
                outcome="failure",
                error_category=category.value,
                meta={"error": str(exc)},
            )
            
            # Broadcast error to frontend
            self._loop.call_soon_threadsafe(
                lambda msg=user_msg: asyncio.create_task(_broadcast_error(msg))
            )
            
            failed_current = None
            with self._current_lock:
                if self._current and (session_id is None or self._current.id == session_id):
                    self._current.finish("failed")
                    failed_current = self._current
                    self._current = None
            if failed_current:
                self._add_to_history(failed_current)
                self._save_transcript_to_db(failed_current)
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._broadcast_history_updated())
                )
        finally:
            # Schedule safe cleanup on the event loop
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_safe_cleanup())
            )

    async def _broadcast_history_updated(self, *, force: bool = False) -> None:
        """Broadcast history updates with global throttling to avoid refetch storms."""
        now = time.monotonic()
        if not force and now - self._history_broadcast_last < self._history_broadcast_interval:
            if self._history_broadcast_handle is None:
                delay = self._history_broadcast_interval - (now - self._history_broadcast_last)
                self._history_broadcast_handle = self._loop.call_later(
                    delay,
                    lambda: asyncio.create_task(self._broadcast_history_updated(force=True)),
                )
            return
        self._history_broadcast_last = now
        if self._history_broadcast_handle is not None:
            self._history_broadcast_handle.cancel()
            self._history_broadcast_handle = None
        await self.broadcast(history_updated_event())

    def _touch_history(self) -> None:
        """Thread-safe schedule for history update broadcast."""
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._broadcast_history_updated())
        )

    async def start_youtube_transcription(self, payload: dict[str, Any]) -> TranscriptRecord:
        url = (payload.get("url") if isinstance(payload.get("url"), str) else "") or ""
        url = url.strip()
        if not url:
            raise ValueError("Missing video URL")

        title = (payload.get("title") if isinstance(payload.get("title"), str) else "").strip() or "YouTube"
        channel = (payload.get("channelTitle") if isinstance(payload.get("channelTitle"), str) else "").strip()
        thumbnail = (payload.get("thumbnailUrl") if isinstance(payload.get("thumbnailUrl"), str) else "").strip()
        duration = (payload.get("duration") if isinstance(payload.get("duration"), str) else "").strip() or "00:00"

        started_at = datetime.now()
        rec = TranscriptRecord(
            id=uuid4().hex,
            title=title,
            date=_format_date_label(started_at),
            duration=duration,
            status="processing",
            type="youtube",
            language=Config.LANGUAGE or "auto",
            step="Queued",
            source_url=url,
            channel=channel,
            thumbnail_url=thumbnail,
        )
        self._emit_workflow_event(
            message=f"YouTube job queued: {rec.title}",
            event="api.job.created",
            workflow="youtube",
            stage="job_created",
            record=rec,
            milestone=True,
            outcome="queued",
        )
        self._add_to_history(rec)
        await self._broadcast_history_updated()
        self._enqueue_background_job(
            rec,
            job_type=JobType.YOUTUBE,
            payload={
                "url": rec.source_url,
                "title": rec.title,
                "channel": rec.channel,
                "thumbnailUrl": rec.thumbnail_url,
                "duration": rec.duration,
                "language": rec.language,
            },
        )
        self._schedule_youtube_job(rec)
        return rec

    async def _run_youtube_transcription(self, rec: TranscriptRecord, *, provider: str) -> None:
        workflow_started = time.monotonic()
        out_dir = self._downloads_dir / "youtube" / rec.id
        rec.step = "Downloading audio..."
        rec.updated_at = datetime.now().isoformat()
        await self._broadcast_history_updated()
        self._emit_workflow_event(
            message="YouTube download started",
            event="youtube.download.started",
            workflow="youtube",
            stage="downloading",
            component="youtube_download",
            record=rec,
            provider=provider,
            milestone=True,
            outcome="started",
        )
        try:
            download_started = time.monotonic()
            
            # Track download progress with speed and ETA
            last_broadcast_time = [0.0]  # Use list to allow mutation in closure
            
            def on_download_progress(progress) -> None:
                import time
                now = time.time()
                # Throttle broadcasts to max 4 per second to avoid flooding
                # BUT always allow "finished" status through to show 100%
                if progress.status != "finished" and now - last_broadcast_time[0] < 0.25:
                    return
                last_broadcast_time[0] = now
                
                # Build step message with speed and ETA
                if progress.status == "finished":
                    rec.step = "Download complete"
                elif progress.speed and progress.eta:
                    rec.step = f"Downloading... {progress.percent:.0f}% • {progress.speed} • ETA {progress.eta}"
                elif progress.speed:
                    rec.step = f"Downloading... {progress.percent:.0f}% • {progress.speed}"
                elif progress.percent > 0:
                    rec.step = f"Downloading... {progress.percent:.0f}%"
                else:
                    rec.step = "Downloading audio..."
                rec.updated_at = datetime.now().isoformat()
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._broadcast_history_updated())
                )
            
            download_timeout = self._timeout_seconds("SCRIBER_TIMEOUT_YOUTUBE_DOWNLOAD_SEC", 300.0)
            audio_path = await self._await_with_timeout(
                download_youtube_audio(
                    rec.source_url,
                    output_dir=out_dir,
                    on_progress=on_download_progress,
                ),
                timeout_seconds=download_timeout,
                timeout_label="YouTube download",
            )
            self._emit_workflow_event(
                message="YouTube download completed",
                event="youtube.download.completed",
                workflow="youtube",
                stage="download_done",
                component="youtube_download",
                record=rec,
                provider=provider,
                milestone=True,
                duration_ms=(time.monotonic() - download_started) * 1000,
                outcome="success",
            )

            def on_transcription(text: str, is_final: bool) -> None:
                if not is_final:
                    return
                rec.append_final_text(text)
                logger.debug(f"YouTube transcription received: {len(text)} chars, total: {len(rec.content)} chars")

            def on_progress(step: str) -> None:
                rec.step = step
                rec.updated_at = datetime.now().isoformat()
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._broadcast_history_updated())
                )

            rec.step = "Transcribing..."
            rec.updated_at = datetime.now().isoformat()
            await self._broadcast_history_updated()
            transcribe_started = time.monotonic()
            self._emit_workflow_event(
                message=f"YouTube transcription started ({provider})",
                event="pipeline.transcription.started",
                workflow="youtube",
                stage="transcribing",
                component="pipeline",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="started",
            )

            pipeline = ScriberPipeline(
                service_name=provider,
                on_status_change=None,
                on_audio_level=None,
                on_transcription=on_transcription,
                on_progress=on_progress,
            )
            
            # Use direct file upload for Soniox/Mistral async APIs (more efficient), fallback to pipecat for others
            transcribe_timeout = self._timeout_seconds("SCRIBER_TIMEOUT_YOUTUBE_TRANSCRIBE_SEC", 600.0)
            if supports_direct_file_upload(provider):
                await self._await_with_timeout(
                    pipeline.transcribe_file_direct(str(audio_path)),
                    timeout_seconds=transcribe_timeout,
                    timeout_label="YouTube transcription",
                )
            else:
                await self._await_with_timeout(
                    pipeline.transcribe_file(str(audio_path)),
                    timeout_seconds=transcribe_timeout,
                    timeout_label="YouTube transcription",
                )

            logger.info(f"YouTube transcription completed: {len(rec.content)} chars")
            rec.status = "completed"
            rec.step = "Completed"
            logger.debug(f"YouTube record updated: status={rec.status}, step={rec.step}")
            self._emit_workflow_event(
                message="YouTube transcription completed",
                event="pipeline.transcription.completed",
                workflow="youtube",
                stage="transcript_done",
                component="pipeline",
                record=rec,
                provider=provider,
                milestone=True,
                duration_ms=(time.monotonic() - transcribe_started) * 1000,
                outcome="success",
                meta={"chars": len(rec.content)},
            )

            # Auto-summarize if enabled
            if Config.AUTO_SUMMARIZE and rec.content:
                try:
                    from src.summarization import summarize_text
                    rec.step = "Summarizing..."
                    rec.updated_at = datetime.now().isoformat()
                    await self._broadcast_history_updated()
                    summarize_started = time.monotonic()
                    self._emit_workflow_event(
                        message=f"Summary generation started ({Config.SUMMARIZATION_MODEL})",
                        event="summary.generation.started",
                        workflow="youtube",
                        stage="summarizing",
                        component="summarization",
                        record=rec,
                        provider=provider,
                        milestone=True,
                        outcome="started",
                    )
                    rec.summary = await summarize_text(rec.content, Config.SUMMARIZATION_MODEL)
                    rec.step = "Completed"
                    logger.info(f"YouTube auto-summarization completed: {len(rec.summary)} chars")
                    self._emit_workflow_event(
                        message="Summary generation completed",
                        event="summary.generation.completed",
                        workflow="youtube",
                        stage="summary_done",
                        component="summarization",
                        record=rec,
                        provider=provider,
                        milestone=True,
                        duration_ms=(time.monotonic() - summarize_started) * 1000,
                        outcome="success",
                        meta={"chars": len(rec.summary)},
                    )
                except Exception as sum_err:
                    logger.warning(f"Auto-summarization failed: {sum_err}")
                    rec.step = "Completed"
                    self._emit_workflow_event(
                        message="Summary generation failed",
                        event="summary.generation.failed",
                        workflow="youtube",
                        stage="summarizing",
                        level="WARNING",
                        component="summarization",
                        record=rec,
                        provider=provider,
                        outcome="failure",
                        error_category=classify_error_message(str(sum_err)).value,
                        meta={"error": str(sum_err)},
                    )
        except (ValueError, ImportError) as exc:
            self._record_provider_failure(provider, exc)
            if self._schedule_retry_if_allowed(rec, exc):
                return
            rec.status = "failed"
            rec.step = "Failed"
            rec.append_final_text(f"[Error] {exc}")
            self._emit_workflow_event(
                message="YouTube transcription failed",
                event="api.job.failed",
                workflow="youtube",
                stage="job_failed",
                level="ERROR",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="failure",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
        except TimeoutError as exc:
            self._record_provider_failure(provider, exc)
            if self._schedule_retry_if_allowed(rec, exc):
                return
            rec.status = "failed"
            rec.step = "Failed"
            rec.append_final_text(f"[Timeout] {exc}")
            self._emit_workflow_event(
                message="YouTube transcription timed out",
                event="api.job.failed",
                workflow="youtube",
                stage="job_failed",
                level="ERROR",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="timeout",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
        except YouTubeDownloadError as exc:
            self._record_provider_failure(provider, exc)
            if self._schedule_retry_if_allowed(rec, exc):
                return
            rec.status = "failed"
            rec.step = "Failed"
            rec.append_final_text(f"[Download error] {exc}")
            self._emit_workflow_event(
                message="YouTube download failed",
                event="youtube.download.failed",
                workflow="youtube",
                stage="downloading",
                level="ERROR",
                component="youtube_download",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="failure",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
        except Exception as exc:
            logger.exception("YouTube transcription failed")
            self._record_provider_failure(provider, exc)
            if self._schedule_retry_if_allowed(rec, exc):
                return
            rec.status = "failed"
            rec.step = "Failed"
            rec.append_final_text(f"[Error] {exc}")
            self._emit_workflow_event(
                message="YouTube job failed",
                event="api.job.failed",
                workflow="youtube",
                stage="job_failed",
                level="ERROR",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="failure",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
        finally:
            if rec.status == "completed":
                self._emit_workflow_event(
                    message="YouTube job completed",
                    event="api.job.completed",
                    workflow="youtube",
                    stage="job_done",
                    record=rec,
                    provider=provider,
                    milestone=True,
                    duration_ms=(time.monotonic() - workflow_started) * 1000,
                    outcome="success",
                )
            rec.updated_at = datetime.now().isoformat()
            self._save_transcript_to_db(rec)  # Persist to database
            await self._broadcast_history_updated()
            # Cleanup: delete the downloaded audio file and directory
            try:
                import shutil
                if out_dir.exists():
                    shutil.rmtree(out_dir)
                    logger.debug(f"Cleaned up YouTube download directory: {out_dir}")
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup YouTube download: {cleanup_err}")

    async def start_file_transcription(self, file_path: Path, original_filename: str) -> TranscriptRecord:
        """Start transcription of an uploaded audio/video file."""
        if not file_path.exists():
            raise ValueError("Uploaded file not found")

        title = original_filename or file_path.name
        duration_seconds = await asyncio.to_thread(_probe_media_duration_seconds, file_path)
        duration_label = _format_duration(duration_seconds) if duration_seconds is not None else "--:--"
        # Get file size for display
        try:
            file_size_bytes = file_path.stat().st_size
            if file_size_bytes >= 1_000_000_000:
                file_size = f"{file_size_bytes / 1_000_000_000:.1f}GB"
            elif file_size_bytes >= 1_000_000:
                file_size = f"{file_size_bytes / 1_000_000:.1f}MB"
            elif file_size_bytes >= 1_000:
                file_size = f"{file_size_bytes / 1_000:.1f}KB"
            else:
                file_size = f"{file_size_bytes}B"
        except Exception:
            file_size = ""

        started_at = datetime.now()
        rec = TranscriptRecord(
            id=uuid4().hex,
            title=title,
            date=_format_date_label(started_at),
            duration=duration_label,
            status="processing",
            type="file",
            language=Config.LANGUAGE or "auto",
            step="Queued",
            source_url=str(file_path),
        )
        self._emit_workflow_event(
            message=f"File job queued: {rec.title}",
            event="api.job.created",
            workflow="file",
            stage="job_created",
            record=rec,
            milestone=True,
            outcome="queued",
        )
        # Store file size in content temporarily for display
        if file_size:
            rec.channel = file_size  # Reuse channel field for file size display
        self._add_to_history(rec)
        await self._broadcast_history_updated()
        self._enqueue_background_job(
            rec,
            job_type=JobType.FILE,
            payload={
                "path": str(file_path),
                "title": rec.title,
                "language": rec.language,
                "originalFilename": original_filename,
            },
        )
        self._schedule_file_job(rec, file_path)
        return rec

    async def _run_file_transcription(self, rec: TranscriptRecord, file_path: Path, *, provider: str) -> None:
        """Run transcription on an uploaded file."""
        workflow_started = time.monotonic()
        rec.step = "Preparing audio..."
        rec.updated_at = datetime.now().isoformat()
        await self._broadcast_history_updated()
        self._emit_workflow_event(
            message="File transcription started",
            event="pipeline.transcription.started",
            workflow="file",
            stage="transcribing",
            component="pipeline",
            record=rec,
            provider=provider,
            milestone=True,
            outcome="started",
        )
        try:
            def on_transcription(text: str, is_final: bool) -> None:
                if not is_final:
                    return
                rec.append_final_text(text)
                logger.debug(f"File transcription received: {len(text)} chars, total: {len(rec.content)} chars")

            def on_progress(step: str) -> None:
                rec.step = step
                rec.updated_at = datetime.now().isoformat()
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._broadcast_history_updated())
                )

            rec.step = "Transcribing..."
            rec.updated_at = datetime.now().isoformat()
            await self._broadcast_history_updated()
            transcribe_started = time.monotonic()

            pipeline = ScriberPipeline(
                service_name=provider,
                on_status_change=None,
                on_audio_level=None,
                on_transcription=on_transcription,
                on_progress=on_progress,
            )
            
            # Use direct file upload for Soniox/Mistral async APIs (more efficient), fallback to pipecat for others
            transcribe_timeout = self._timeout_seconds("SCRIBER_TIMEOUT_FILE_TRANSCRIBE_SEC", 600.0)
            if supports_direct_file_upload(provider):
                await self._await_with_timeout(
                    pipeline.transcribe_file_direct(str(file_path)),
                    timeout_seconds=transcribe_timeout,
                    timeout_label="File transcription",
                )
            else:
                await self._await_with_timeout(
                    pipeline.transcribe_file(str(file_path)),
                    timeout_seconds=transcribe_timeout,
                    timeout_label="File transcription",
                )

            logger.info(f"File transcription completed: {len(rec.content)} chars")
            rec.status = "completed"
            rec.step = "Completed"
            self._emit_workflow_event(
                message="File transcription completed",
                event="pipeline.transcription.completed",
                workflow="file",
                stage="transcript_done",
                component="pipeline",
                record=rec,
                provider=provider,
                milestone=True,
                duration_ms=(time.monotonic() - transcribe_started) * 1000,
                outcome="success",
                meta={"chars": len(rec.content)},
            )

            # Auto-summarize if enabled
            if Config.AUTO_SUMMARIZE and rec.content:
                try:
                    from src.summarization import summarize_text
                    rec.step = "Summarizing..."
                    rec.updated_at = datetime.now().isoformat()
                    await self._broadcast_history_updated()
                    summarize_started = time.monotonic()
                    self._emit_workflow_event(
                        message=f"Summary generation started ({Config.SUMMARIZATION_MODEL})",
                        event="summary.generation.started",
                        workflow="file",
                        stage="summarizing",
                        component="summarization",
                        record=rec,
                        provider=provider,
                        milestone=True,
                        outcome="started",
                    )
                    rec.summary = await summarize_text(rec.content, Config.SUMMARIZATION_MODEL)
                    rec.step = "Completed"
                    logger.info(f"File auto-summarization completed: {len(rec.summary)} chars")
                    self._emit_workflow_event(
                        message="Summary generation completed",
                        event="summary.generation.completed",
                        workflow="file",
                        stage="summary_done",
                        component="summarization",
                        record=rec,
                        provider=provider,
                        milestone=True,
                        duration_ms=(time.monotonic() - summarize_started) * 1000,
                        outcome="success",
                        meta={"chars": len(rec.summary)},
                    )
                except Exception as sum_err:
                    logger.warning(f"Auto-summarization failed: {sum_err}")
                    rec.step = "Completed"
                    self._emit_workflow_event(
                        message="Summary generation failed",
                        event="summary.generation.failed",
                        workflow="file",
                        stage="summarizing",
                        level="WARNING",
                        component="summarization",
                        record=rec,
                        provider=provider,
                        outcome="failure",
                        error_category=classify_error_message(str(sum_err)).value,
                        meta={"error": str(sum_err)},
                    )
        except (ValueError, ImportError) as exc:
            self._record_provider_failure(provider, exc)
            if self._schedule_retry_if_allowed(rec, exc):
                return
            rec.status = "failed"
            rec.step = "Failed"
            rec.append_final_text(f"[Error] {exc}")
            self._emit_workflow_event(
                message="File transcription failed",
                event="api.job.failed",
                workflow="file",
                stage="job_failed",
                level="ERROR",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="failure",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
        except TimeoutError as exc:
            self._record_provider_failure(provider, exc)
            if self._schedule_retry_if_allowed(rec, exc):
                return
            rec.status = "failed"
            rec.step = "Failed"
            rec.append_final_text(f"[Timeout] {exc}")
            self._emit_workflow_event(
                message="File transcription timed out",
                event="api.job.failed",
                workflow="file",
                stage="job_failed",
                level="ERROR",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="timeout",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
        except Exception as exc:
            logger.exception("File transcription failed")
            self._record_provider_failure(provider, exc)
            if self._schedule_retry_if_allowed(rec, exc):
                return
            rec.status = "failed"
            rec.step = "Failed"
            rec.append_final_text(f"[Error] {exc}")
            self._emit_workflow_event(
                message="File job failed",
                event="api.job.failed",
                workflow="file",
                stage="job_failed",
                level="ERROR",
                record=rec,
                provider=provider,
                milestone=True,
                outcome="failure",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
        finally:
            if rec.duration.strip() in {"", "--", "--:--", "-:--"}:
                rec.duration = _format_duration(time.monotonic() - workflow_started)
            if rec.status == "completed":
                self._emit_workflow_event(
                    message="File job completed",
                    event="api.job.completed",
                    workflow="file",
                    stage="job_done",
                    record=rec,
                    provider=provider,
                    milestone=True,
                    duration_ms=(time.monotonic() - workflow_started) * 1000,
                    outcome="success",
                )
            rec.updated_at = datetime.now().isoformat()
            self._save_transcript_to_db(rec)  # Persist to database
            await self._broadcast_history_updated()
            # Cleanup: delete the uploaded file and its directory
            try:
                import shutil
                file_dir = file_path.parent
                if rec.status != "processing" and file_dir.exists() and file_dir.name != "files":
                    shutil.rmtree(file_dir)
                    logger.debug(f"Cleaned up uploaded file directory: {file_dir}")
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup uploaded file: {cleanup_err}")

    async def start_listening(self) -> None:
        # Acquire lock for entire operation - no parallel start/stop allowed
        async with self._listening_lock:
            # Don't start if already listening or if stop is in progress
            if self._is_listening or self._is_stopping:
                return

            started_at = datetime.now()
            rec = TranscriptRecord(
                id=uuid4().hex,
                title=f"Live Mic {started_at.strftime('%Y-%m-%d %H:%M')}",
                date=_format_date_label(started_at),
                duration="00:00",
                status="recording",
                type="mic",
                language=Config.LANGUAGE or "auto",
            )
            rec.start()
            session_id = rec.id
            with self._current_lock:
                self._current = rec
            self._session_id = session_id
            self._clear_input_warning_state(session_id=session_id, broadcast=True)
            self._start_hot_path_tracer(session_id)
            self._mark_hot_path(session_id, "controller_accepted")
            self._set_recording_state(RecordingState.INITIALIZING, context="start_listening")
            self._emit_workflow_event(
                message="Live mic session requested",
                event="api.session.start_requested",
                workflow="live_mic",
                stage="session_start",
                session_id=session_id,
                record=rec,
                milestone=True,
                outcome="started",
            )

            # Ensure overlay has stop callback connected before showing
            self._get_overlay()

            # Show initializing overlay immediately for user feedback
            self._overlay_audio_enabled = False
            show_initializing_overlay()

            # Callback to transition overlay when mic is ready
            def on_mic_ready():
                if session_id != self._session_id:
                    return
                logger.debug("on_mic_ready callback triggered - transitioning overlay to recording mode")
                self._mark_hot_path(session_id, "mic_ready")
                self._set_recording_state(RecordingState.RECORDING, context="on_mic_ready")
                self._overlay_audio_enabled = True
                show_recording_overlay()
                logger.info("Microphone ready - recording started")
                self._emit_workflow_event(
                    message="Microphone ready - recording started",
                    event="pipeline.mic.ready",
                    workflow="live_mic",
                    stage="mic_ready",
                    component="pipeline",
                    session_id=session_id,
                    record=rec,
                    provider=self._active_provider,
                    milestone=True,
                    outcome="success",
                )

            # Callback for pipeline errors (e.g., Soniox websocket timeout)
            def on_pipeline_error(error_msg: str):
                if session_id != self._session_id:
                    return
                logger.error(f"Pipeline error callback: {error_msg}")
                self._record_provider_failure(self._active_provider or "", error_msg)
                self._set_recording_state(RecordingState.FAILED, context="pipeline_error")
                self._set_status("Error")
                self._overlay_audio_enabled = False
                hide_recording_overlay()

                category = classify_error_message(error_msg)
                user_msg = user_message_for_category(category)
                logger.warning(f"Pipeline error category={category.value}: {error_msg}")
                self._emit_workflow_event(
                    message=f"Pipeline error: {user_msg}",
                    event="pipeline.provider.failed",
                    workflow="live_mic",
                    stage="pipeline_error",
                    level="ERROR",
                    component="pipeline",
                    session_id=session_id,
                    record=rec,
                    provider=self._active_provider,
                    milestone=True,
                    outcome="failure",
                    error_category=category.value,
                    meta={"error": error_msg},
                )

                # Broadcast error to frontend and stop the pipeline
                def schedule_cleanup():
                    asyncio.create_task(self.broadcast(error_event(user_msg, session_id=session_id)))
                    # Schedule pipeline stop to clean up properly
                    asyncio.create_task(self._emergency_stop_pipeline(session_id=session_id))

                self._loop.call_soon_threadsafe(schedule_cleanup)

            def on_text_injected(_text: str):
                if session_id != self._session_id:
                    return
                self._mark_hot_path(session_id, "first_paste")
                self._emit_hot_path_report_once(session_id)
                self._emit_workflow_event(
                    message="Text injected",
                    event="injector.paste.succeeded",
                    workflow="live_mic",
                    stage="inject_done",
                    component="injector",
                    session_id=session_id,
                    record=rec,
                    provider=self._active_provider,
                    milestone=True,
                    outcome="success",
                    meta={"chars": len(_text or "")},
                )

            try:
                live_provider = self._select_available_provider()
            except Exception as exc:
                self._set_recording_state(RecordingState.FAILED, context="start_listening_provider_select")
                self._set_status("Error")
                self._overlay_audio_enabled = False
                hide_recording_overlay()
                with self._current_lock:
                    failed = self._current
                    self._current = None
                self._is_listening = False
                self._is_stopping = False
                self._session_id = None
                self._clear_hot_path_tracer(session_id)
                if failed:
                    failed.finish("failed")
                    failed.append_final_text(f"[Error] {exc}")
                    self._add_to_history(failed)
                    self._save_transcript_to_db(failed)
                    await self._broadcast_history_updated()
                self._emit_workflow_event(
                    message=f"Live mic session failed before start: {exc}",
                    event="api.session.failed",
                    workflow="live_mic",
                    stage="session_start",
                    level="ERROR",
                    session_id=session_id,
                    record=rec,
                    provider=self._active_provider,
                    milestone=True,
                    outcome="failure",
                    error_category=classify_error_message(str(exc)).value,
                    meta={"error": str(exc)},
                )
                await self.broadcast(error_event(str(exc), session_id=session_id))
                return

            self._active_provider = live_provider
            self._pipeline = ScriberPipeline(
                service_name=live_provider,
                on_status_change=lambda status: self._set_status(status, session_id=session_id),
                on_audio_level=lambda rms: self._on_audio_level(rms, session_id=session_id),
                on_transcription=lambda text, is_final: self._on_transcription(text, is_final, session_id=session_id),
                on_text_injected=on_text_injected,
                on_mic_ready=on_mic_ready,
                on_error=on_pipeline_error,
            )
            self._pipeline_task = asyncio.create_task(self._pipeline.start(), name="scriber_pipeline")
            self._pipeline_task.add_done_callback(lambda task: self._on_pipeline_done(task, session_id=session_id))
            self._is_listening = True
            self._set_status("Listening", session_id=session_id)
            self._emit_workflow_event(
                message=f"Pipeline session started ({live_provider})",
                event="pipeline.session.started",
                workflow="live_mic",
                stage="listening",
                component="pipeline",
                session_id=session_id,
                record=rec,
                provider=live_provider,
                milestone=True,
                outcome="started",
            )
            session_payload = session_started_event(
                rec.to_public(include_content=True),
                session_id=session_id,
            )

        await self.broadcast(session_payload)

    async def _emergency_stop_pipeline(self, *, session_id: str | None = None) -> None:
        """Emergency stop for connection errors - doesn't save transcript."""
        logger.warning("Emergency pipeline stop triggered")
        self._emit_workflow_event(
            message="Emergency pipeline stop triggered",
            event="pipeline.emergency_stop.triggered",
            workflow="live_mic",
            stage="emergency_stop",
            level="WARNING",
            session_id=session_id,
            component="pipeline",
            milestone=True,
            outcome="started",
        )
        pipeline = None
        pipeline_task = None
        try:
            async with self._listening_lock:
                if session_id is not None and session_id != self._session_id:
                    return

                # Cancel the current recording session without saving.
                with self._current_lock:
                    self._current = None

                pipeline = self._pipeline
                pipeline_task = self._pipeline_task
                self._is_listening = False
                self._is_stopping = False
                self._pipeline = None
                self._pipeline_task = None
                self._active_provider = None
                self._clear_input_warning_state(session_id=session_id, broadcast=True)
                self._session_id = None
                self._set_recording_state(RecordingState.FAILED, context="emergency_stop")
                self._set_recording_state(RecordingState.IDLE, context="emergency_stop")
                self._clear_hot_path_tracer(session_id)

            # Stop the previous pipeline instance outside the lock.
            if pipeline:
                try:
                    await asyncio.wait_for(pipeline.stop(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("Emergency stop timeout - forcing cleanup")
                except Exception as e:
                    logger.debug(f"Emergency stop warning: {e}")

            # Cancel previous pipeline task if still running.
            if pipeline_task and not pipeline_task.done():
                pipeline_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(pipeline_task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        except Exception as e:
            logger.error(f"Emergency stop error: {e}")

    async def stop_listening(self) -> None:
        # Acquire lock for entire operation - no parallel start/stop allowed
        async with self._listening_lock:
            if not self._is_listening:
                return
            
            # Mark that we're stopping
            self._is_stopping = True
            self._is_listening = False  # Prevent any new operations
            
            # Capture current pipeline references
            pipeline = self._pipeline
            pipeline_task = self._pipeline_task
            with self._current_lock:
                current = self._current
            session_id = self._session_id
            provider_used = self._active_provider
            self._set_recording_state(RecordingState.FINALIZING, context="stop_listening")
            self._emit_workflow_event(
                message="Live mic stop requested",
                event="api.session.stop_requested",
                workflow="live_mic",
                stage="session_stop",
                session_id=session_id,
                record=current,
                provider=provider_used,
                milestone=True,
                outcome="started",
            )
            
            # Clear pipeline references immediately to prevent double-stop
            # NOTE: We do NOT clear _current here - it must remain set until
            # pipeline.stop() completes so the transcription callback can still
            # append text to it (especially for async STT like Soniox async)
            self._pipeline = None
            self._pipeline_task = None
        
        # Now do the actual stopping work (outside the lock to not block hotkey checks)
        # But we've already cleared _is_listening so no new start will happen
        
        # Check if this is a real-time service (Soniox RT) - text is injected during recording
        # For async services, show "Transcribing..." while processing
        is_realtime_service = (
            pipeline and 
            pipeline.service_name == "soniox" and 
            Config.SONIOX_MODE == "realtime"
        )
        
        if is_realtime_service:
            # For RT services, hide overlay immediately - text is already injected
            self._overlay_audio_enabled = False
            hide_recording_overlay()
        else:
            # Show transcribing state for async services that need processing time
            self._overlay_audio_enabled = False
            show_transcribing_overlay()
            transcribing_payload = transcribing_event(session_id=session_id)
            await self.broadcast(transcribing_payload)

        stop_error: Exception | None = None
        try:
            if pipeline:
                await pipeline.stop()
                self._record_provider_success(self._active_provider or "")
             
            # Now that pipeline has stopped and transcription callback has fired,
            # clear _current to prevent any further modifications
            with self._current_lock:
                if self._current and (session_id is None or self._current.id == session_id):
                    self._current = None
             
            # Hide overlay for async services after processing completes
            if not is_realtime_service:
                self._overlay_audio_enabled = False
                hide_recording_overlay()
            
            if pipeline_task:
                pipeline_task.cancel()
                try:
                    await pipeline_task
                except asyncio.CancelledError:
                    pass
        except Exception as exc:
            stop_error = exc
            self._record_provider_failure(self._active_provider or "", exc)
            logger.exception("Error while stopping live pipeline")
            self._emit_workflow_event(
                message="Live mic stop failed",
                event="api.session.failed",
                workflow="live_mic",
                stage="session_stop",
                level="ERROR",
                session_id=session_id,
                record=current,
                provider=provider_used,
                milestone=True,
                outcome="failure",
                error_category=classify_error_message(str(exc)).value,
                meta={"error": str(exc)},
            )
            self._overlay_audio_enabled = False
            hide_recording_overlay()
            error_payload = error_event(f"Failed to stop recording cleanly: {exc}", session_id=session_id)
            await self.broadcast(error_payload)
        finally:
            async with self._listening_lock:
                self._is_stopping = False
                self._clear_input_warning_state(session_id=session_id, broadcast=True)
                self._set_status("Error" if stop_error else "Stopped", session_id=session_id)
                if session_id is None or self._session_id == session_id:
                    self._session_id = None
                self._active_provider = None
                if stop_error:
                    self._set_recording_state(RecordingState.FAILED, context="stop_listening")
                else:
                    self._set_recording_state(RecordingState.COMPLETED, context="stop_listening")
                self._set_recording_state(RecordingState.IDLE, context="stop_listening")

            if current:
                current.finish("failed" if stop_error else "completed")
                if stop_error:
                    err_line = f"[Error] {stop_error}"
                    current.append_final_text(err_line)
                self._add_to_history(current)
                self._save_transcript_to_db(current)  # Persist to database
                finished_payload = session_finished_event(
                    current.to_public(include_content=True),
                    session_id=session_id,
                )
                await self.broadcast(finished_payload)
                await self._broadcast_history_updated()
                duration_ms = None
                if current._started_at_monotonic is not None:
                    duration_ms = (time.monotonic() - current._started_at_monotonic) * 1000
                self._emit_workflow_event(
                    message="Live mic session completed" if not stop_error else "Live mic session failed",
                    event="api.session.completed" if not stop_error else "api.session.failed",
                    workflow="live_mic",
                    stage="session_done",
                    level="INFO" if not stop_error else "ERROR",
                    session_id=session_id,
                    record=current,
                    provider=provider_used,
                    milestone=True,
                    duration_ms=duration_ms,
                    outcome="success" if not stop_error else "failure",
                    error_category=classify_error_message(str(stop_error)).value if stop_error else None,
                )
            self._clear_hot_path_tracer(session_id)

    async def toggle_listening(self) -> None:
        # Quick check without lock - if operation in progress, ignore
        if self._is_stopping:
            return
        
        if self._is_listening:
            await self.stop_listening()
        else:
            await self.start_listening()

    def register_hotkeys(self) -> None:
        try:
            import keyboard as kb  # type: ignore
        except Exception as exc:  # pragma: no cover - platform/env dependent
            logger.warning(f"Hotkeys disabled (keyboard module missing or headless env): {exc}")
            return

        self._keyboard = kb

        # Some keyboard builds lack internal hotkey sets; create stubs to avoid attribute errors.
        try:
            listener = getattr(kb, "_listener", None)
            if listener:
                if not hasattr(listener, "blocking_hotkeys"):
                    listener.blocking_hotkeys = set()
                if not hasattr(listener, "nonblocking_hotkeys"):
                    listener.nonblocking_hotkeys = set()
                if not hasattr(listener, "nonblocking_keys_pressed"):
                    listener.nonblocking_keys_pressed = set()
        except Exception:
            logger.warning("Keyboard listener is missing; hotkeys may be unavailable.")
            return

        if not hasattr(kb, "add_hotkey") or not hasattr(kb, "clear_all_hotkeys"):
            logger.warning("Keyboard hotkey methods unavailable; skipping hotkey registration.")
            return

        if self._ptt_task:
            self._ptt_task.cancel()
            self._ptt_task = None

        try:
            kb.clear_all_hotkeys()
            if Config.MODE == "push_to_talk":
                self._ptt_task = asyncio.create_task(self._ptt_loop(), name="ptt_loop")
                logger.info(f"Push-to-Talk active: {Config.HOTKEY}")
            else:
                kb.add_hotkey(
                    Config.HOTKEY,
                    lambda: self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.toggle_listening())),
                )
                logger.info(f"Hotkey registered: {Config.HOTKEY} (Toggle)")
        except Exception as exc:
            logger.error(f"Failed to register hotkey: {exc}")

    async def _ptt_loop(self) -> None:
        last_state = False
        while True:
            try:
                kb = self._keyboard
                is_pressed = kb.is_pressed(Config.HOTKEY) if kb else False
                if is_pressed and not last_state:
                    await self.start_listening()
                elif not is_pressed and last_state:
                    await self.stop_listening()
                last_state = is_pressed
            except Exception:
                pass
            await asyncio.sleep(0.05)

    def shutdown(self) -> None:
        if self._ptt_task:
            self._ptt_task.cancel()
            self._ptt_task = None

        kb = self._keyboard
        if kb and hasattr(kb, "clear_all_hotkeys"):
            try:
                kb.clear_all_hotkeys()
            except Exception:
                pass

        try:
            self._device_monitor.stop()
        except Exception as exc:
            logger.debug(f"[DeviceMonitor] stop warning: {exc}")

    def get_settings(self) -> dict[str, Any]:
        # Track favorite mic availability for UI feedback
        _favorite_mic_available = False
        _resolved_favorite = ""

        def resolve_mic_device_for_ui() -> str:
            nonlocal _favorite_mic_available, _resolved_favorite
            selected = Config.MIC_DEVICE or "default"
            favorite = Config.FAVORITE_MIC or ""
            try:
                devices = self.list_microphones()
            except Exception:
                return selected
            available_ids = [d.get("deviceId") for d in devices if d.get("deviceId")]
            available = set(available_ids)
            normalized_to_id: dict[str, str] = {}
            for dev_id in available_ids:
                norm = _normalize_device_name(dev_id)
                if norm and norm not in normalized_to_id:
                    normalized_to_id[norm] = dev_id

            def resolve_device_id(device_id: str) -> str | None:
                if not device_id or device_id == "default":
                    return None
                if device_id in available:
                    return device_id
                norm = _normalize_device_name(device_id)
                if norm in normalized_to_id:
                    return normalized_to_id[norm]
                try:
                    idx = int(device_id)
                except (TypeError, ValueError):
                    return None
                try:
                    import sounddevice as sd  # type: ignore

                    info = sd.query_devices(device=idx, kind="input")
                    name = info.get("name")
                    if name:
                        if name in available:
                            return name
                        name_norm = _normalize_device_name(name)
                        if name_norm in normalized_to_id:
                            return normalized_to_id[name_norm]
                except Exception:
                    return None
                return None

            selected_is_default = selected in ("", "default", None)
            selected_name = resolve_device_id(selected) if not selected_is_default else None
            selected_available = bool(selected_name)

            favorite_name = resolve_device_id(favorite) if favorite and favorite != "default" else None
            _favorite_mic_available = bool(favorite_name)
            _resolved_favorite = favorite_name or ""

            if favorite_name:
                return favorite_name
            if selected_available:
                return selected_name  # type: ignore[return-value]
            first_available = next(
                (
                    dev_id
                    for dev_id in available_ids
                    if dev_id and dev_id != "default"
                ),
                None,
            )
            if first_available:
                return first_available
            return "default"

        resolved_mic = resolve_mic_device_for_ui()

        return {
            "hotkey": _hotkey_to_display(Config.HOTKEY),
            "hotkeyRaw": Config.HOTKEY,
            "mode": Config.MODE,
            "defaultSttService": Config.DEFAULT_STT_SERVICE,
            "sonioxMode": Config.SONIOX_MODE,
            "sonioxAsyncModel": Config.SONIOX_ASYNC_MODEL,
            "language": Config.LANGUAGE,
            "micDevice": resolved_mic,
            "favoriteMic": _resolved_favorite or (Config.FAVORITE_MIC or ""),
            "favoriteMicAvailable": _favorite_mic_available,
            "micAlwaysOn": bool(Config.MIC_ALWAYS_ON),
            "debug": bool(Config.DEBUG),
            "customVocab": Config.CUSTOM_VOCAB or "",
            "summarizationPrompt": Config.SUMMARIZATION_PROMPT or "",
            "summarizationModel": Config.SUMMARIZATION_MODEL or "gemini-3-flash-preview",
            "autoSummarize": bool(Config.AUTO_SUMMARIZE),
            "openaiSttModel": Config.OPENAI_STT_MODEL,
            "onnxModel": Config.ONNX_MODEL,
            "onnxQuantization": Config.ONNX_QUANTIZATION,
            "onnxUseGpu": bool(Config.ONNX_USE_GPU),
            "nemoModel": Config.NEMO_MODEL,
            "visualizerBarCount": Config.VISUALIZER_BAR_COUNT,
            "apiKeys": {
                "soniox": Config.SONIOX_API_KEY or "",
                "mistral": getattr(Config, "MISTRAL_API_KEY", "") or "",
                "assemblyai": Config.ASSEMBLYAI_API_KEY or "",
                "deepgram": Config.DEEPGRAM_API_KEY or "",
                "openai": Config.OPENAI_API_KEY or "",
                "azureSpeechKey": Config.AZURE_SPEECH_KEY or "",
                "azureSpeechRegion": Config.AZURE_SPEECH_REGION or "",
                "gladia": Config.GLADIA_API_KEY or "",
                "groq": Config.GROQ_API_KEY or "",
                "speechmatics": Config.SPEECHMATICS_API_KEY or "",
                "elevenlabs": Config.ELEVENLABS_API_KEY or "",
                "googleApiKey": getattr(Config, "GOOGLE_API_KEY", "") or "",
                "googleApplicationCredentials": Config.GOOGLE_APPLICATION_CREDENTIALS or "",
                "youtubeApiKey": getattr(Config, "YOUTUBE_API_KEY", "") or "",
            },
        }

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        old_hotkey = Config.HOTKEY
        old_mode = Config.MODE

        if "hotkey" in payload and isinstance(payload["hotkey"], str):
            normalized = _normalize_hotkey_for_backend(payload["hotkey"])
            if normalized:
                Config.set_hotkey(normalized)

        if "mode" in payload and isinstance(payload["mode"], str):
            Config.set_mode(payload["mode"])

        if "defaultSttService" in payload and isinstance(payload["defaultSttService"], str):
            service = payload["defaultSttService"].strip().lower()
            if service:
                Config.set_default_service(service)

        if "sonioxMode" in payload and isinstance(payload["sonioxMode"], str):
            Config.set_soniox_mode(payload["sonioxMode"])

        if "sonioxAsyncModel" in payload and isinstance(payload["sonioxAsyncModel"], str):
            Config.SONIOX_ASYNC_MODEL = payload["sonioxAsyncModel"].strip()
            os.environ["SCRIBER_SONIOX_ASYNC_MODEL"] = Config.SONIOX_ASYNC_MODEL

        if "language" in payload and isinstance(payload["language"], str):
            Config.set_language(payload["language"])

        if "micDevice" in payload and isinstance(payload["micDevice"], str):
            Config.set_mic_device(payload["micDevice"])

        if "favoriteMic" in payload and isinstance(payload["favoriteMic"], str):
            Config.set_favorite_mic(payload["favoriteMic"])

        if "micAlwaysOn" in payload:
            Config.set_mic_always_on(bool(payload["micAlwaysOn"]))

        if "debug" in payload:
            Config.set_debug(bool(payload["debug"]))

        if "customVocab" in payload and isinstance(payload["customVocab"], str):
            Config.CUSTOM_VOCAB = payload["customVocab"].strip()
            os.environ["SCRIBER_CUSTOM_VOCAB"] = Config.CUSTOM_VOCAB

        if "summarizationPrompt" in payload and isinstance(payload["summarizationPrompt"], str):
            Config.set_summarization_prompt(payload["summarizationPrompt"])

        if "summarizationModel" in payload and isinstance(payload["summarizationModel"], str):
            Config.SUMMARIZATION_MODEL = payload["summarizationModel"].strip()
            os.environ["SCRIBER_SUMMARIZATION_MODEL"] = Config.SUMMARIZATION_MODEL

        if "autoSummarize" in payload:
            Config.AUTO_SUMMARIZE = bool(payload["autoSummarize"])
            os.environ["SCRIBER_AUTO_SUMMARIZE"] = "1" if Config.AUTO_SUMMARIZE else "0"

        if "openaiSttModel" in payload and isinstance(payload["openaiSttModel"], str):
            Config.set_openai_stt_model(payload["openaiSttModel"])

        if "onnxModel" in payload and isinstance(payload["onnxModel"], str):
            Config.set_onnx_model(payload["onnxModel"])

        if "onnxQuantization" in payload and isinstance(payload["onnxQuantization"], str):
            Config.set_onnx_quantization(payload["onnxQuantization"])

        if "onnxUseGpu" in payload:
            Config.set_onnx_use_gpu(bool(payload["onnxUseGpu"]))

        if "nemoModel" in payload and isinstance(payload["nemoModel"], str):
            Config.set_nemo_model(payload["nemoModel"])

        if "visualizerBarCount" in payload:
            try:
                count = int(payload["visualizerBarCount"])
                Config.set_visualizer_bar_count(count)
            except (ValueError, TypeError):
                pass

        api_keys = payload.get("apiKeys")
        if isinstance(api_keys, dict):
            mapping: dict[str, tuple[str, Callable[[str], None] | None]] = {
                "soniox": ("soniox", lambda v: Config.set_api_key("soniox", v)),
                "mistral": ("mistral", lambda v: Config.set_api_key("mistral", v)),
                "assemblyai": ("assemblyai", lambda v: Config.set_api_key("assemblyai", v)),
                "deepgram": ("deepgram", lambda v: Config.set_api_key("deepgram", v)),
                "openai": ("openai", lambda v: Config.set_api_key("openai", v)),
                "gladia": ("gladia", lambda v: Config.set_api_key("gladia", v)),
                "groq": ("groq", lambda v: Config.set_api_key("groq", v)),
                "speechmatics": ("speechmatics", lambda v: Config.set_api_key("speechmatics", v)),
                "elevenlabs": ("elevenlabs", lambda v: Config.set_api_key("elevenlabs", v)),
            }
            for key, (_, setter) in mapping.items():
                if key in api_keys and isinstance(api_keys[key], str) and setter:
                    setter(api_keys[key])

            if "azureSpeechKey" in api_keys and isinstance(api_keys["azureSpeechKey"], str):
                Config.AZURE_SPEECH_KEY = api_keys["azureSpeechKey"].strip()
                os.environ["AZURE_SPEECH_KEY"] = Config.AZURE_SPEECH_KEY
            if "azureSpeechRegion" in api_keys and isinstance(api_keys["azureSpeechRegion"], str):
                Config.AZURE_SPEECH_REGION = api_keys["azureSpeechRegion"].strip()
                os.environ["AZURE_SPEECH_REGION"] = Config.AZURE_SPEECH_REGION

            if "googleApiKey" in api_keys and isinstance(api_keys["googleApiKey"], str):
                Config.GOOGLE_API_KEY = api_keys["googleApiKey"].strip()
                os.environ["GOOGLE_API_KEY"] = Config.GOOGLE_API_KEY
            if "googleApplicationCredentials" in api_keys and isinstance(api_keys["googleApplicationCredentials"], str):
                Config.GOOGLE_APPLICATION_CREDENTIALS = api_keys["googleApplicationCredentials"].strip()
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = Config.GOOGLE_APPLICATION_CREDENTIALS

            if "youtubeApiKey" in api_keys and isinstance(api_keys["youtubeApiKey"], str):
                Config.YOUTUBE_API_KEY = api_keys["youtubeApiKey"].strip()
                os.environ["YOUTUBE_API_KEY"] = Config.YOUTUBE_API_KEY

        # Persist current settings to .env so they are remembered.
        Config.persist_to_env_file(".env")

        if Config.HOTKEY != old_hotkey or Config.MODE != old_mode:
            self.register_hotkeys()

        await self.broadcast({"type": "settings_updated"})
        return self.get_settings()

    async def cancel_transcript(self, transcript_id: str) -> bool:
        """Cancel a running transcription task."""
        # Find record in history
        rec = self._get_history_record(transcript_id)
        
        if transcript_id in self._running_tasks:
            task = self._running_tasks[transcript_id]
            task.cancel()
            
            if rec and rec.status == "processing":
                 rec.step = "Stopping..."
                 rec.updated_at = datetime.now().isoformat()
                 await self._broadcast_history_updated()
            return True
            
        # Also check if it's stuck in processing but no task running (e.g. restart)
        if rec and rec.status == "processing":
            rec.status = "stopped"
            rec.step = "Stopped"
            rec.updated_at = datetime.now().isoformat()
            self._sync_job_status(rec)
            self._save_transcript_to_db(rec)
            await self._broadcast_history_updated()
            return True
            
        return False

    def list_microphones(self) -> list[dict[str, str]]:
        """List available microphone devices.
        
        Returns devices with:
        - deviceId: The device name (stable across reboots, used for persistence)
        - label: Display label (may include "(Default)" suffix)
        
        Uses a single active host API to avoid cross-host duplicate entries.
        """
        try:
            devices = self._device_monitor.get_devices()
            if devices:
                return devices
        except Exception as exc:
            logger.debug(f"[DeviceMonitor] fallback to direct listing: {exc}")

        try:
            import sounddevice as sd  # type: ignore
        except Exception:  # pragma: no cover - optional runtime dep
            return [{"deviceId": "default", "label": "Default"}]

        devices: list[dict[str, str]] = [{"deviceId": "default", "label": "Default"}]

        sample_rate = int(getattr(Config, "SAMPLE_RATE", 16000) or 16000)
        channels = max(1, int(getattr(Config, "CHANNELS", 1) or 1))
        for entry in list_unique_input_microphones(sd, sample_rate=sample_rate, channels=channels):
            label = f"{entry.name} (Default)" if entry.is_default else entry.name
            devices.append({"deviceId": entry.name, "label": label})

        return devices

    def resolve_microphone_device(self, device_name: str) -> str:
        """Resolve a device name to the current device index.
        
        Args:
            device_name: The saved device name (or "default")
            
        Returns:
            The device index as a string, or "default" if not found.
            Falls back to Windows default if the saved device is unavailable.
        """
        sample_rate = int(getattr(Config, "SAMPLE_RATE", 16000) or 16000)
        channels = max(1, int(getattr(Config, "CHANNELS", 1) or 1))
        if device_name == "default" or not device_name:
            device_name = "default"

        try:
            self._device_monitor.refresh_now()
        except Exception as exc:
            logger.debug(f"[DeviceMonitor] manual refresh failed before resolve: {exc}")
        
        try:
            import sounddevice as sd
        except Exception:
            return "default"
        
        try:
            devices = list(sd.query_devices())
            host_priorities = get_input_hostapi_priorities(
                sd,
                devices,
                sample_rate=sample_rate,
                channels=channels,
            )

            target = device_name.strip()
            target_norm = _normalize_device_name(target)

            def _matches(dev_name: str) -> bool:
                if dev_name == target:
                    return True
                if target_norm:
                    return _normalize_device_name(dev_name) == target_norm
                return False

            matches: list[tuple[int, int, str]] = []
            for idx, dev in enumerate(devices):
                if int(dev.get("max_input_channels", 0) or 0) <= 0:
                    continue
                name = str(dev.get("name", ""))
                if not _matches(name):
                    continue
                try:
                    hostapi_idx = int(dev.get("hostapi", -1))
                except (TypeError, ValueError):
                    hostapi_idx = None
                matches.append((rank_hostapi(hostapi_idx, host_priorities), idx, name))

            if matches:
                matches.sort(key=lambda item: (item[0], item[1]))
                for _, idx, name in matches:
                    if is_input_device_compatible(
                        sd,
                        device_index=idx,
                        device_info=devices[idx],
                        sample_rate=sample_rate,
                        channels=channels,
                    ):
                        logger.info(f"Resolved microphone '{device_name}' to device index {idx}")
                        return str(idx)

            # Selected device not usable: choose curated compatible fallback.
            curated = list_unique_input_microphones(
                sd,
                sample_rate=sample_rate,
                channels=channels,
            )
            if curated:
                preferred = next((entry for entry in curated if entry.is_default), None)
                chosen = preferred or curated[0]
                logger.warning(
                    f"Microphone '{device_name}' unavailable; falling back to '{chosen.name}' (index {chosen.index})"
                )
                return str(chosen.index)

            logger.warning(f"Microphone '{device_name}' not found, falling back to default")
            return "default"
            
        except Exception as e:
            logger.error(f"Error resolving microphone '{device_name}': {e}")
            return "default"

    def list_transcripts(
        self,
        *,
        include_content: bool = False,
        query: str = "",
        transcript_type: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List transcripts with optional search, filtering, and pagination.

        PERFORMANCE OPTIMIZATION: Pagination reduces memory usage and response size
        for large transcript lists (50-100ms improvement for 1000+ transcripts).

        Args:
            include_content: Whether to include full transcript content
            query: Search query (searches title, content, channel)
            transcript_type: Filter by type (live, youtube, file)
            offset: Number of items to skip (for pagination)
            limit: Maximum number of items to return (default 50, max 100)

        Returns:
            Dict with items, total count, and pagination info
        """
        # Clamp limit to reasonable bounds
        limit = max(1, min(100, limit))
        offset = max(0, offset)

        query_lower = query.lower().strip() if query else ""
        if query_lower:
            # Use SQLite FTS for scalable search and keep unsaved active sessions visible.
            live_matches: list[dict[str, Any]] = []
            for rec in self._history:
                if rec.status not in ("processing", "recording"):
                    continue
                if transcript_type and rec.type != transcript_type:
                    continue
                searchable = (
                    f"{rec.title or ''} "
                    f"{rec.channel or ''} "
                    f"{rec._preview or ''}"
                ).lower()
                if query_lower in searchable:
                    if rec.id and db.transcript_exists(rec.id):
                        continue
                    live_matches.append(rec.to_public(include_content=include_content))

            live_count = len(live_matches)
            if offset < live_count:
                live_slice = live_matches[offset:offset + limit]
                remaining = limit - len(live_slice)
                db_offset = 0
            else:
                live_slice = []
                remaining = limit
                db_offset = offset - live_count

            db_result = (
                db.search_transcript_metadata(
                    query_lower,
                    transcript_type=transcript_type,
                    offset=db_offset,
                    limit=remaining,
                )
                if remaining > 0
                else {"items": [], "total": 0}
            )
            items = live_slice + db_result.get("items", [])
            total = live_count + int(db_result.get("total", 0))
            return {
                "items": items,
                "total": total,
                "offset": offset,
                "limit": limit,
                "hasMore": offset + len(items) < total,
            }

        if not query_lower and not transcript_type:
            total = len(self._history)
            paginated = self._history[offset:offset + limit]
            items = [rec.to_public(include_content=include_content) for rec in paginated]
            return {
                "items": items,
                "total": total,
                "offset": offset,
                "limit": limit,
                "hasMore": offset + len(items) < total,
            }

        filtered = []

        for rec in self._history:
            # Type filter
            if transcript_type and rec.type != transcript_type:
                continue

            filtered.append(rec)

        total = len(filtered)
        # Apply pagination
        paginated = filtered[offset:offset + limit]
        items = [rec.to_public(include_content=include_content) for rec in paginated]

        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": offset + len(items) < total,
        }

    def get_transcript(self, transcript_id: str) -> Optional[dict[str, Any]]:
        """Get a transcript by ID with full content.

        PERFORMANCE: Uses lazy content loading. If content was not loaded on
        startup (metadata-only mode), it's fetched from the database on demand.
        """
        rec = self._get_history_record(transcript_id)
        if rec:
            if not rec._content_loaded or not rec._summary_loaded:
                full_data = db.get_transcript(transcript_id)
                if full_data:
                    rec.content = full_data.get("content", rec.content)
                    rec.summary = full_data.get("summary", rec.summary)
                    if not rec._preview:
                        rec._preview = full_data.get("preview", "") or rec._preview
                rec._content_loaded = True
                rec._summary_loaded = True
            return rec.to_public(include_content=True)
        # Not found in memory - try database directly
        return db.get_transcript(transcript_id)


@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin")
    if origin and not _origin_allowed(origin):
        return web.json_response({"message": "Origin not allowed"}, status=403)

    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc

    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
    else:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


def create_app(controller: ScriberWebController) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["controller"] = controller

    async def http_session_ctx(app_: web.Application):
        timeout = ClientTimeout(total=15)
        session = ClientSession(timeout=timeout)
        app_["http_session"] = session
        yield
        await session.close()

    app.cleanup_ctx.append(http_session_ctx)

    async def health(_request: web.Request):
        return web.json_response({"ok": True})

    async def ws_handler(request: web.Request):
        origin = request.headers.get("Origin")
        if origin and not _origin_allowed(origin):
            return web.json_response({"message": "Origin not allowed"}, status=403)

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        ctl: ScriberWebController = request.app["controller"]
        await ctl.add_client(ws)
        await ws.send_str(json.dumps({"type": "state", **ctl.get_state()}))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # Currently server -> client only. Keep the connection alive.
                    if msg.data == "ping":
                        await ws.send_str("pong")
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            await ctl.remove_client(ws)
        return ws

    async def get_state(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        return web.json_response(ctl.get_state())

    async def get_hot_path_metrics(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        return web.json_response(ctl.get_hot_path_metrics(limit=limit))

    async def start_live(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        await ctl.start_listening()
        return web.json_response(ctl.get_state())

    async def stop_live(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        await ctl.stop_listening()
        return web.json_response(ctl.get_state())

    async def toggle_live(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        await ctl.toggle_listening()
        return web.json_response(ctl.get_state())

    async def get_settings(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        return web.json_response(ctl.get_settings())

    async def put_settings(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"message": "Invalid JSON"}, status=400)
        updated = await ctl.update_settings(payload if isinstance(payload, dict) else {})
        return web.json_response(updated)

    async def get_autostart(request: web.Request):
        """Check if autostart is enabled."""
        import sys
        if sys.platform != 'win32':
            return web.json_response({"enabled": False, "available": False})

        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, "Scriber")
                winreg.CloseKey(key)
                return web.json_response({"enabled": True, "available": True})
            except FileNotFoundError:
                winreg.CloseKey(key)
                return web.json_response({"enabled": False, "available": True})
        except Exception:
            return web.json_response({"enabled": False, "available": True})

    async def set_autostart(request: web.Request):
        """Enable or disable autostart."""
        import sys
        if sys.platform != 'win32':
            return web.json_response({"message": "Autostart only available on Windows"}, status=400)

        try:
            payload = await request.json()
            enabled = payload.get("enabled", False)
        except Exception:
            return web.json_response({"message": "Invalid JSON"}, status=400)

        try:
            import winreg
            from pathlib import Path

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)

            if enabled:
                # Enable autostart - find tray.py
                tray_script = Path(__file__).parent / "tray.py"
                if not tray_script.exists():
                    winreg.CloseKey(key)
                    return web.json_response({"message": "tray.py not found"}, status=500)

                python_exe = sys.executable
                startup_command = f'"{python_exe}" "{str(tray_script.resolve())}"'
                winreg.SetValueEx(key, "Scriber", 0, winreg.REG_SZ, startup_command)
                winreg.CloseKey(key)
                return web.json_response({"enabled": True, "message": "Autostart enabled"})
            else:
                # Disable autostart
                try:
                    winreg.DeleteValue(key, "Scriber")
                except FileNotFoundError:
                    pass
                winreg.CloseKey(key)
                return web.json_response({"enabled": False, "message": "Autostart disabled"})

        except Exception as e:
            return web.json_response({"message": f"Error: {str(e)}"}, status=500)

    async def microphones(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        return web.json_response({"devices": ctl.list_microphones()})

    async def transcripts(request: web.Request):
        """List transcripts with optional search, filtering, and pagination.

        Query parameters:
            q: Search query (searches title, content, channel)
            type: Filter by transcript type (mic, youtube, file)
            offset: Number of items to skip (default 0)
            limit: Maximum number of items to return (default 50, max 100)
        """
        ctl: ScriberWebController = request.app["controller"]
        query = request.query.get("q", "")
        transcript_type = request.query.get("type", "")

        # Parse pagination parameters
        try:
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            offset = 0
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50

        return web.json_response(
            ctl.list_transcripts(
                include_content=False,
                query=query,
                transcript_type=transcript_type,
                offset=offset,
                limit=limit,
            )
        )

    async def transcript_detail(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        transcript_id = request.match_info["id"]
        rec = ctl.get_transcript(transcript_id)
        if not rec:
            return web.json_response({"message": "Not found"}, status=404)
        return web.json_response(rec)

    async def youtube_search(request: web.Request):
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response({"message": "Missing query parameter: q"}, status=400)

        api_key = getattr(Config, "YOUTUBE_API_KEY", "") or ""
        if not api_key.strip():
            return web.json_response(
                {"message": "Missing YouTube API key. Set YOUTUBE_API_KEY or save it in Settings."}, status=400
            )

        raw_max = (request.query.get("maxResults") or "").strip()
        try:
            max_results = int(raw_max) if raw_max else 10
        except Exception:
            max_results = 10

        page_token = (request.query.get("pageToken") or "").strip() or None

        session: ClientSession | None = request.app.get("http_session")
        if not session:
            return web.json_response({"message": "HTTP session not initialized"}, status=500)

        try:
            payload = await search_youtube_videos(
                api_key,
                q,
                max_results=max_results,
                page_token=page_token,
                session=session,
            )
        except ValueError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except YouTubeApiError as exc:
            return web.json_response({"message": str(exc), "details": exc.details}, status=exc.status)
        except Exception:
            logger.exception("YouTube search failed")
            return web.json_response({"message": "YouTube search failed"}, status=500)

        return web.json_response(payload)

    async def youtube_video(request: web.Request):
        """Fetch video details by video ID or URL."""
        video_id = (request.query.get("id") or "").strip()
        url_param = (request.query.get("url") or "").strip()
        
        # If URL provided, extract video ID from it
        if url_param and not video_id:
            video_id = extract_youtube_video_id(url_param) or ""
        
        if not video_id:
            return web.json_response({"message": "Missing video ID or URL parameter"}, status=400)

        api_key = getattr(Config, "YOUTUBE_API_KEY", "") or ""
        if not api_key.strip():
            return web.json_response(
                {"message": "Missing YouTube API key. Set YOUTUBE_API_KEY or save it in Settings."}, status=400
            )

        session: ClientSession | None = request.app.get("http_session")
        if not session:
            return web.json_response({"message": "HTTP session not initialized"}, status=500)

        try:
            video = await get_video_by_id(
                api_key,
                video_id,
                session=session,
                timeout=ClientTimeout(total=30),
            )
        except ValueError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except YouTubeApiError as exc:
            return web.json_response({"message": str(exc), "details": exc.details}, status=exc.status)
        except Exception:
            logger.exception("YouTube video fetch failed")
            return web.json_response({"message": "YouTube video fetch failed"}, status=500)

        if not video:
            return web.json_response({"message": "Video not found"}, status=404)

        return web.json_response(video)

    async def youtube_transcribe(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"message": "Invalid JSON"}, status=400)

        try:
            rec = await ctl.start_youtube_transcription(payload if isinstance(payload, dict) else {})
        except ValueError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("Failed to start YouTube transcription")
            return web.json_response({"message": str(exc) or "Failed to start YouTube transcription"}, status=500)

        return web.json_response(rec.to_public(include_content=True))

    async def file_transcribe(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]

        # Check content type for multipart upload
        if not request.content_type.startswith("multipart/"):
            return web.json_response({"message": "Expected multipart/form-data"}, status=400)

        try:
            reader = await request.multipart()
            file_field = None
            original_filename = "uploaded_file"

            async for field in reader:
                if field.name == "file":
                    file_field = field
                    original_filename = field.filename or "uploaded_file"
                    break

            if file_field is None:
                return web.json_response({"message": "No file uploaded"}, status=400)

            # Validate file extension
            allowed_extensions = {".mp3", ".m4a", ".wav", ".mp4", ".mov", ".webm", ".ogg", ".flac", ".aac", ".avi", ".mkv", ".m4v"}
            safe_filename = _safe_upload_filename(original_filename)
            ext = Path(safe_filename).suffix.lower()
            if ext not in allowed_extensions:
                return web.json_response(
                    {"message": f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(allowed_extensions))}"},
                    status=400,
                )

            # Determine if this is a video file (needs audio extraction)
            is_video = ext in _VIDEO_EXTENSIONS
            
            # Use different size limits for video vs audio files
            if is_video:
                max_bytes = _get_video_max_bytes()  # 2GB for videos (audio will be extracted)
            else:
                max_bytes = _get_upload_max_bytes()  # 200MB for audio files
            limit_label = _format_upload_limit(max_bytes)
            
            # Check content-length header if available
            if request.content_length is not None and request.content_length > max_bytes:
                return web.json_response(
                    {"message": f"File too large (max {limit_label})."},
                    status=413,
                )

            # Generate unique ID and save file
            file_id = uuid4().hex
            save_dir = ctl._downloads_dir / "files" / file_id
            save_dir.mkdir(parents=True, exist_ok=True)
            save_path = save_dir / safe_filename

            # Stream file to disk
            bytes_read = 0
            too_large = False
            with open(save_path, "wb") as f:
                while True:
                    chunk = await file_field.read_chunk(size=1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > max_bytes:
                        too_large = True
                        break
                    f.write(chunk)

            if too_large:
                try:
                    import shutil
                    if save_dir.exists():
                        shutil.rmtree(save_dir)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to cleanup oversized upload: {cleanup_err}")
                return web.json_response({"message": f"File too large (max {limit_label})."}, status=413)

            # For video files, extract audio using ffmpeg
            transcribe_path = save_path
            if is_video:
                try:
                    logger.info(f"Extracting audio from video: {safe_filename} ({bytes_read / (1024*1024):.1f}MB)")
                    audio_path = await _extract_audio_from_video(save_path, save_dir)
                    
                    # Check if extracted audio is within size limit
                    audio_size = audio_path.stat().st_size
                    audio_limit = _get_audio_max_bytes()
                    if audio_size > audio_limit:
                        import shutil
                        if save_dir.exists():
                            shutil.rmtree(save_dir)
                        return web.json_response(
                            {"message": f"Extracted audio too large ({audio_size / (1024*1024):.0f}MB, max {audio_limit / (1024*1024):.0f}MB)."},
                            status=413,
                        )
                    
                    # Delete original video file to save space
                    try:
                        save_path.unlink()
                        logger.debug(f"Deleted original video file: {safe_filename}")
                    except Exception as del_err:
                        logger.warning(f"Failed to delete video after extraction: {del_err}")
                    
                    # Use extracted audio for transcription
                    transcribe_path = audio_path
                    # Update filename for display
                    safe_filename = audio_path.name
                    logger.info(f"Audio extracted successfully: {safe_filename} ({audio_size / (1024*1024):.1f}MB)")
                    
                except RuntimeError as extract_err:
                    import shutil
                    if save_dir.exists():
                        shutil.rmtree(save_dir)
                    logger.error(f"Audio extraction failed: {extract_err}")
                    return web.json_response(
                        {"message": f"Failed to extract audio from video: {extract_err}"},
                        status=500,
                    )

            # Start transcription
            rec = await ctl.start_file_transcription(transcribe_path, safe_filename)
            return web.json_response(rec.to_public(include_content=True))

        except ValueError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("Failed to process file upload")
            return web.json_response({"message": str(exc) or "Failed to process file upload"}, status=500)


    async def delete_transcript(request: web.Request):
        ctl: ScriberWebController = request.app["controller"]
        transcript_id = request.match_info.get("id", "")
        if not transcript_id:
            return web.json_response({"message": "Missing transcript ID"}, status=400)

        # Find and remove the transcript from history
        found = ctl._remove_from_history(transcript_id)

        if not found:
            return web.json_response({"message": "Transcript not found"}, status=404)

        # Delete from database
        db.delete_transcript(transcript_id)

        # Broadcast update to clients
        await ctl._broadcast_history_updated()
        logger.info(f"Deleted transcript: {found.title} ({transcript_id})")

        return web.json_response({"success": True, "id": transcript_id})

    async def summarize_transcript(request: web.Request):
        """Summarize a transcript using the configured LLM model."""
        from src.summarization import summarize_text
        
        ctl: ScriberWebController = request.app["controller"]
        transcript_id = request.match_info.get("id", "")
        if not transcript_id:
            return web.json_response({"message": "Missing transcript ID"}, status=400)

        # Ensure full content is loaded (lazy-load safe)
        full_data = ctl.get_transcript(transcript_id)
        rec = ctl._get_history_record(transcript_id)

        if not rec and not full_data:
            return web.json_response({"message": "Transcript not found"}, status=404)

        content = rec.content if rec else (full_data.get("content", "") if isinstance(full_data, dict) else "")
        status = rec.status if rec else (full_data.get("status", "") if isinstance(full_data, dict) else "")

        if not content or not content.strip():
            return web.json_response({"message": "Transcript has no content to summarize"}, status=400)

        if status != "completed":
            return web.json_response({"message": "Transcript is not yet completed"}, status=400)

        try:
            model = getattr(Config, "SUMMARIZATION_MODEL", "gemini-2.0-flash")
            summary = await summarize_text(content, model)
            if rec:
                rec.summary = summary
                rec.updated_at = datetime.now().isoformat()
                ctl._save_transcript_to_db(rec)
                await ctl._broadcast_history_updated()
                logger.info(f"Summarized transcript: {rec.title} ({len(summary)} chars)")
            else:
                db.update_transcript_summary(transcript_id, summary)
                logger.info(f"Summarized transcript: {transcript_id} ({len(summary)} chars)")
            return web.json_response({"success": True, "summary": summary})
        except ValueError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("Summarization failed")
            return web.json_response({"message": str(exc) or "Summarization failed"}, status=500)

    async def stop_transcript(request: web.Request):
        """Cancel a running transcription task."""
        ctl: ScriberWebController = request.app["controller"]
        transcript_id = request.match_info.get("id", "")
        if not transcript_id:
            return web.json_response({"message": "Missing transcript ID"}, status=400)
        
        success = await ctl.cancel_transcript(transcript_id)
        if not success:
             # Check if it exists at all
             found = ctl._get_history_record(transcript_id) is not None
             if not found:
                 return web.json_response({"message": "Transcript not found"}, status=404)
             return web.json_response({"message": "Transcription is not running"}, status=400)
             
        return web.json_response({"success": True})

    async def export_transcript(request: web.Request):
        """Export transcript as PDF or DOCX."""
        from src.export import export_to_pdf, export_to_docx
        
        ctl: ScriberWebController = request.app["controller"]
        transcript_id = request.match_info.get("id", "")
        export_format = request.match_info.get("format", "pdf").lower()
        
        if not transcript_id:
            return web.json_response({"message": "Missing transcript ID"}, status=400)
        
        if export_format not in ("pdf", "docx"):
            return web.json_response({"message": "Invalid format. Use 'pdf' or 'docx'"}, status=400)
        
        # Ensure full content is loaded (lazy-load safe)
        full_data = ctl.get_transcript(transcript_id)
        rec = ctl._get_history_record(transcript_id)
        if not rec and not full_data:
            return web.json_response({"message": "Transcript not found"}, status=404)

        content = rec.content if rec else (full_data.get("content", "") if isinstance(full_data, dict) else "")
        summary = rec.summary if rec else (full_data.get("summary", "") if isinstance(full_data, dict) else "")
        title = rec.title if rec else (full_data.get("title", "") if isinstance(full_data, dict) else "")
        date = rec.date if rec else (full_data.get("date", "") if isinstance(full_data, dict) else "")
        duration = rec.duration if rec else (full_data.get("duration", "") if isinstance(full_data, dict) else "")

        if not content:
            return web.json_response({"message": "Transcript has no content to export"}, status=400)

        try:
            if export_format == "pdf":
                data = export_to_pdf(
                    title=title or "Transcript",
                    content=content,
                    summary=summary,
                    date=date,
                    duration=duration,
                )
                content_type = "application/pdf"
                ext = "pdf"
            else:
                data = export_to_docx(
                    title=title or "Transcript",
                    content=content,
                    summary=summary,
                    date=date,
                    duration=duration,
                )
                content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ext = "docx"

            # Sanitize filename
            safe_title = "".join(c for c in (title or "transcript") if c.isalnum() or c in " -_").strip()[:50]
            filename = f"{safe_title}.{ext}"
            
            return web.Response(
                body=data,
                content_type=content_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        except ImportError as e:
            return web.json_response({"message": str(e)}, status=500)
        except Exception as e:
            logger.exception(f"Export failed: {e}")
            return web.json_response({"message": f"Export failed: {e}"}, status=500)

    app.router.add_get("/api/health", health)
    app.router.add_get("/ws", ws_handler)

    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/metrics/hot-path", get_hot_path_metrics)
    app.router.add_post("/api/live-mic/start", start_live)
    app.router.add_post("/api/live-mic/stop", stop_live)
    app.router.add_post("/api/live-mic/toggle", toggle_live)

    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", put_settings)
    app.router.add_get("/api/autostart", get_autostart)
    app.router.add_post("/api/autostart", set_autostart)
    app.router.add_get("/api/microphones", microphones)

    app.router.add_get("/api/transcripts", transcripts)
    app.router.add_get("/api/transcripts/{id}", transcript_detail)
    app.router.add_delete("/api/transcripts/{id}", delete_transcript)
    app.router.add_post("/api/transcripts/{id}/summarize", summarize_transcript)
    app.router.add_post("/api/transcripts/{id}/cancel", stop_transcript)
    app.router.add_get("/api/transcripts/{id}/export/{format}", export_transcript)

    app.router.add_get("/api/youtube/search", youtube_search)
    app.router.add_get("/api/youtube/video", youtube_video)
    app.router.add_post("/api/youtube/transcribe", youtube_transcribe)
    app.router.add_post("/api/file/transcribe", file_transcribe)


    # ==========================================================================
    # ONNX Local Models API
    # ==========================================================================

    async def onnx_list_models(request: web.Request):
        """List available ONNX models with download status."""
        try:
            def _load_onnx_models() -> dict[str, Any]:
                from src.onnx_stt import list_available_models, is_onnx_available

                if not is_onnx_available():
                    return {
                        "available": False,
                        "message": "onnx-asr library not installed. Run: pip install onnx-asr[cpu,hub]",
                        "models": [],
                    }

                models = list_available_models(quantization=Config.ONNX_QUANTIZATION)
                return {
                    "available": True,
                    "models": models,
                    "currentModel": Config.ONNX_MODEL,
                    "quantization": Config.ONNX_QUANTIZATION,
                }

            payload = await asyncio.to_thread(_load_onnx_models)
            return web.json_response(payload)
        except ImportError as e:
            return web.json_response({
                "available": False,
                "message": str(e),
                "models": [],
            })
        except Exception as e:
            logger.exception("Failed to list ONNX models")
            return web.json_response({"message": str(e)}, status=500)

    async def onnx_model_status(request: web.Request):
        """Get status of a specific ONNX model."""
        model_id = request.match_info.get("model_id", "")
        if not model_id:
            return web.json_response({"message": "Missing model ID"}, status=400)
        
        try:
            from src.onnx_stt import get_model_info, get_model_status
            
            info = get_model_info(model_id)
            if not info:
                return web.json_response({"message": "Unknown model"}, status=404)

            quantization = request.query.get("quantization") or Config.ONNX_QUANTIZATION
            status = get_model_status(model_id, quantization=quantization)
            
            return web.json_response({
                "id": model_id,
                "name": info["name"],
                "description": info["description"],
                "languages": info["languages"],
                "sizeMb": info["size_mb"],
                "sizeMbByQuantization": info.get("size_mb_by_quantization", {}),
                "supportedQuantizations": info.get("supported_quantizations", ["int8", "fp32"]),
                "downloaded": status["downloaded"],
                "status": status["status"],
                "progress": status["progress"],
                "message": status["message"],
            })
        except Exception as e:
            return web.json_response({"message": str(e)}, status=500)

    async def onnx_download_model(request: web.Request):
        """Download an ONNX model from Hugging Face."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        
        model_id = body.get("modelId", "")
        quantization = body.get("quantization") or Config.ONNX_QUANTIZATION
        if not model_id:
            return web.json_response({"message": "Missing modelId"}, status=400)
        
        try:
            from src.onnx_stt import (
                download_model,
                get_model_info,
                get_model_status,
                is_model_downloading,
            )

            info = get_model_info(model_id)
            if not info:
                return web.json_response({"message": "Unknown model"}, status=404)

            status = get_model_status(model_id, quantization=quantization)
            if status.get("downloaded"):
                return web.json_response({
                    "success": True,
                    "message": "Model already downloaded",
                    "modelId": model_id,
                })

            if is_model_downloading(model_id):
                return web.json_response({
                    "success": False,
                    "message": "Download already in progress",
                    "modelId": model_id,
                }, status=409)

            ctl: ScriberWebController = request.app["controller"]
            loop = asyncio.get_running_loop()

            def on_progress(progress: float, message: str) -> None:
                status_value = "downloading"
                if progress < 0:
                    status_value = "error"
                elif progress >= 100:
                    status_value = "ready"

                payload = {
                    "type": "onnx_download_progress",
                    "modelId": model_id,
                    "progress": progress,
                    "status": status_value,
                    "message": message,
                }
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(ctl.broadcast(payload))
                )

            logger.info(f"Starting ONNX model download: {model_id}")
            success = await download_model(model_id, quantization=quantization, on_progress=on_progress)

            final_status = get_model_status(model_id, quantization=quantization)
            await ctl.broadcast({
                "type": "onnx_download_progress",
                "modelId": model_id,
                "progress": final_status.get("progress", 0.0),
                "status": final_status.get("status", "error" if not success else "ready"),
                "message": final_status.get("message", ""),
            })

            if success:
                return web.json_response({
                    "success": True,
                    "message": "Model downloaded successfully",
                    "modelId": model_id,
                })
            return web.json_response({
                "success": False,
                "message": "Download failed",
                "modelId": model_id,
            }, status=500)

        except ValueError as e:
            return web.json_response({"message": str(e)}, status=400)
        except Exception as e:
            logger.exception(f"Failed to download model {model_id}")
            return web.json_response({"message": str(e)}, status=500)

    async def onnx_delete_model(request: web.Request):
        """Delete a downloaded ONNX model from cache."""
        model_id = request.match_info.get("model_id", "")
        if not model_id:
            return web.json_response({"message": "Missing model ID"}, status=400)
        
        try:
            from src.onnx_stt import delete_model, get_model_info
            
            info = get_model_info(model_id)
            if not info:
                return web.json_response({"message": "Unknown model"}, status=404)
            
            quantization = request.query.get("quantization") or Config.ONNX_QUANTIZATION
            success = delete_model(model_id, quantization=quantization)
            
            if success:
                logger.info(f"Deleted ONNX model: {model_id}")
                await request.app["controller"].broadcast({
                    "type": "onnx_models_updated",
                    "modelId": model_id,
                })
                return web.json_response({
                    "success": True,
                    "message": "Model deleted",
                    "modelId": model_id,
                })
            else:
                return web.json_response({
                    "success": False,
                    "message": "Model not found in cache",
                    "modelId": model_id,
                }, status=404)
                
        except Exception as e:
            logger.exception(f"Failed to delete model {model_id}")
            return web.json_response({"message": str(e)}, status=500)

    async def nemo_list_models(request: web.Request):
        """List available NeMo models and their download status."""
        try:
            def _load_nemo_models() -> dict[str, Any]:
                from src.nemo_stt import list_available_models, is_nemo_available

                if not is_nemo_available():
                    return {
                        "available": False,
                        "message": "NeMo toolkit not installed. Run: pip install nemo_toolkit[asr]",
                        "models": [],
                    }

                return {
                    "available": True,
                    "models": list_available_models(),
                    "currentModel": Config.NEMO_MODEL,
                }

            payload = await asyncio.to_thread(_load_nemo_models)
            return web.json_response(payload)
        except Exception as e:
            logger.exception("Failed to list NeMo models")
            return web.json_response({"message": str(e)}, status=500)

    async def nemo_download_model(request: web.Request):
        """Download a NeMo model from Hugging Face."""
        try:
            body = await request.json()
        except Exception:
            body = {}

        model_id = body.get("modelId", "")
        if not model_id:
            return web.json_response({"message": "Missing modelId"}, status=400)

        try:
            from src.nemo_stt import (
                download_model,
                get_model_info,
                get_model_status,
                is_model_downloading,
            )

            info = get_model_info(model_id)
            if not info:
                return web.json_response({"message": "Unknown model"}, status=404)

            status = get_model_status(model_id)
            if status.get("downloaded"):
                return web.json_response({
                    "success": True,
                    "message": "Model already downloaded",
                    "modelId": model_id,
                })

            if is_model_downloading(model_id):
                return web.json_response({
                    "success": False,
                    "message": "Download already in progress",
                    "modelId": model_id,
                }, status=409)

            ctl: ScriberWebController = request.app["controller"]
            loop = asyncio.get_running_loop()

            def on_progress(progress: float, message: str) -> None:
                status_value = "downloading"
                if progress < 0:
                    status_value = "error"
                elif progress >= 100:
                    status_value = "ready"

                payload = {
                    "type": "nemo_download_progress",
                    "modelId": model_id,
                    "progress": progress,
                    "status": status_value,
                    "message": message,
                }
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(ctl.broadcast(payload))
                )

            logger.info(f"Starting NeMo model download: {model_id}")
            success = await download_model(model_id, on_progress=on_progress)

            final_status = get_model_status(model_id)
            await ctl.broadcast({
                "type": "nemo_download_progress",
                "modelId": model_id,
                "progress": final_status.get("progress", 0.0),
                "status": final_status.get("status", "error" if not success else "ready"),
                "message": final_status.get("message", ""),
            })

            if success:
                return web.json_response({
                    "success": True,
                    "message": "Model downloaded successfully",
                    "modelId": model_id,
                })
            return web.json_response({
                "success": False,
                "message": "Download failed",
                "modelId": model_id,
            }, status=500)

        except Exception as e:
            logger.exception(f"Failed to download model {model_id}")
            return web.json_response({"message": str(e)}, status=500)

    async def nemo_delete_model(request: web.Request):
        """Delete a downloaded NeMo model from cache."""
        model_id = request.match_info.get("model_id", "")
        if not model_id:
            return web.json_response({"message": "Missing model ID"}, status=400)

        try:
            from src.nemo_stt import delete_model, get_model_info

            info = get_model_info(model_id)
            if not info:
                return web.json_response({"message": "Unknown model"}, status=404)

            success = delete_model(model_id)

            if success:
                ctl: ScriberWebController = request.app["controller"]
                await ctl.broadcast({
                    "type": "nemo_models_updated",
                })
                return web.json_response({"success": True})

            return web.json_response({"message": "Delete failed"}, status=404)
        except Exception as e:
            logger.exception(f"Failed to delete model {model_id}")
            return web.json_response({"message": str(e)}, status=500)

    app.router.add_get("/api/onnx/models", onnx_list_models)
    app.router.add_get("/api/onnx/models/{model_id}", onnx_model_status)
    app.router.add_post("/api/onnx/download", onnx_download_model)
    app.router.add_delete("/api/onnx/models/{model_id}", onnx_delete_model)
    app.router.add_get("/api/nemo/models", nemo_list_models)
    app.router.add_post("/api/nemo/download", nemo_download_model)
    app.router.add_delete("/api/nemo/models/{model_id}", nemo_delete_model)

    return app



async def run_server(host: str, port: int) -> None:
    loop = asyncio.get_running_loop()
    controller = ScriberWebController(loop)
    controller.register_hotkeys()

    app = create_app(controller)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info(f"Scriber web API listening on http://{host}:{port} (ws://{host}:{port}/ws)")

    # Start background initialization (improves first recording latency)
    asyncio.create_task(_background_init(controller), name="background_init")

    stop_event = asyncio.Event()

    def _request_stop(*_args: Any) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _request_stop)
        except Exception:  # pragma: no cover - platform dependent
            pass

    await stop_event.wait()
    try:
        await controller.stop_listening()
    except Exception:
        pass
    finally:
        controller.shutdown()
        await runner.cleanup()


async def _background_init(controller: ScriberWebController) -> None:
    """Background initialization after server starts.
    
    Runs asynchronously to avoid blocking server startup:
    1. Load transcripts from database
    2. Prewarm Qt overlay
    3. Prewarm ML models (VAD, SmartTurn)
    4. Pre-import configured STT service (4.4 optimization)
    
    Total savings: ~700-1200ms off startup + first recording latency.
    """
    await asyncio.sleep(0.1)  # Yield to let server start accepting connections
    
    # Load transcripts from database first (needed for UI)
    try:
        await asyncio.to_thread(controller._load_transcripts_from_db)
        controller._transcripts_loaded = True
        logger.info(f"Loaded {len(controller._history)} transcripts from database")
    except Exception as e:
        logger.warning(f"Background transcript load failed: {e}")

    try:
        resumed = await controller.resume_pending_jobs(limit=25)
        if resumed:
            logger.info(f"Resumed {resumed} pending background job(s)")
    except Exception as e:
        logger.warning(f"Background job resume failed: {e}")
    
    async def _prewarm_overlay() -> None:
        try:
            from src.overlay import get_overlay
            # get_overlay triggers initialization in a background thread.
            await asyncio.to_thread(lambda: get_overlay(on_stop=None))
            logger.info("Overlay prewarmed (ready for first recording)")
        except Exception as e:
            logger.debug(f"Overlay prewarm skipped: {e}")

    async def _prewarm_models() -> None:
        try:
            from src.pipeline import _AnalyzerCache
            await asyncio.to_thread(_AnalyzerCache.get_vad_analyzer)
            await asyncio.to_thread(_AnalyzerCache.get_smart_turn_analyzer)
            logger.info("ML model cache warmed (first recording will start faster)")
        except Exception as e:
            logger.debug(f"Cache prewarm skipped: {e}")

    async def _prewarm_stt() -> None:
        try:
            await asyncio.to_thread(_prewarm_stt_service, Config.DEFAULT_STT_SERVICE)
            logger.info(f"STT service '{Config.DEFAULT_STT_SERVICE}' preloaded")
        except Exception as e:
            logger.debug(f"STT prewarm skipped: {e}")

    # Run heavyweight warmups in parallel.
    await asyncio.gather(
        _prewarm_overlay(),
        _prewarm_models(),
        _prewarm_stt(),
    )


def _prewarm_stt_service(service_name: str) -> None:
    """Pre-import the configured STT service module.
    
    This avoids the 100-200ms import delay on first hotkey press.
    The actual service instance is created later with proper parameters.
    """
    try:
        if service_name == "assemblyai":
            from pipecat.services.assemblyai.stt import AssemblyAISTTService  # noqa: F401
        elif service_name == "google":
            from pipecat.services.google.stt import GoogleSTTService  # noqa: F401
        elif service_name == "elevenlabs":
            from pipecat.services.elevenlabs.stt import ElevenLabsSTTService  # noqa: F401
        elif service_name == "deepgram":
            from pipecat.services.deepgram.stt import DeepgramSTTService  # noqa: F401
        elif service_name == "openai":
            from pipecat.services.openai.stt import OpenAISTTService  # noqa: F401
        elif service_name == "azure":
            from pipecat.services.azure.stt import AzureSTTService  # noqa: F401
        elif service_name == "gladia":
            from pipecat.services.gladia.stt import GladiaSTTService  # noqa: F401
        elif service_name == "groq":
            from pipecat.services.groq.stt import GroqSTTService  # noqa: F401
        elif service_name == "speechmatics":
            from pipecat.services.speechmatics.stt import SpeechmaticsSTTService  # noqa: F401
        elif service_name == "aws":
            from pipecat.services.aws.stt import AWSTranscribeSTTService  # noqa: F401
        elif service_name in {"mistral", "mistral_async"}:
            from src.mistral_stt import MistralRealtimeSTTService, MistralAsyncProcessor  # noqa: F401
        # soniox is already imported at module level
    except ImportError as e:
        logger.debug(f"Could not prewarm STT service {service_name}: {e}")


def main() -> None:
    add_stderr = os.getenv("SCRIBER_LOG_STDERR", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
    setup_logging(component="web_api", force=True, add_stderr=add_stderr)
    host = os.getenv("SCRIBER_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("SCRIBER_WEB_PORT", "8765"))
    asyncio.run(run_server(host, port))


if __name__ == "__main__":
    main()
