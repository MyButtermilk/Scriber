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
from typing import Any, Callable, Literal, Optional
from urllib.parse import urlparse
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from loguru import logger

from src.config import Config
from src.pipeline import ScriberPipeline
from src.youtube_api import YouTubeApiError, search_youtube_videos, get_video_by_id, extract_youtube_video_id
from src.youtube_download import YouTubeDownloadError, download_youtube_audio
from src.overlay import get_overlay, show_recording_overlay, show_initializing_overlay, show_transcribing_overlay, hide_recording_overlay, update_overlay_audio
from src import database as db

TranscriptStatus = Literal["completed", "processing", "failed", "recording"]
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
    
    Returns the path to the extracted audio file (MP3 format).
    Raises RuntimeError if extraction fails.
    """
    import shutil
    
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH (required for video audio extraction).")
    
    # Output as MP3 for good compression and compatibility
    audio_filename = video_path.stem + ".mp3"
    audio_path = output_dir / audio_filename
    
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",  # Overwrite output
        "-i", str(video_path),
        "-vn",  # No video
        "-acodec", "libmp3lame",
        "-ab", "128k",  # 128kbps for good quality/size balance
        "-ar", "16000",  # 16kHz sample rate (good for speech)
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


def _format_date_label(ts: datetime) -> str:
    now = datetime.now(ts.tzinfo)
    today = now.date()
    if ts.date() == today:
        return f"Today, {ts.strftime('%H:%M')}"
    if ts.date() == (today - timedelta(days=1)):
        return "Yesterday"
    return ts.strftime("%Y-%m-%d")


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
    _segments: list[str] = field(default_factory=list)
    _content_loaded: bool = True
    _summary_loaded: bool = True

    def to_public(self, *, include_content: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "date": self.date,
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
        
        # Generate 5-word preview
        words = (self.content or "").strip().split()
        preview = " ".join(words[:5])
        if len(words) > 5:
            preview += "..."
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
        if self._segments and self._segments[-1] == cleaned:
            return
        self._segments.append(cleaned)
        self.content = "\n\n".join(self._segments).strip()
        self.updated_at = datetime.now().isoformat()


class ScriberWebController:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._clients: set[web.WebSocketResponse] = set()
        self._clients_lock = asyncio.Lock()
        self._clients_snapshot: tuple[web.WebSocketResponse, ...] = ()
        self._clients_dirty = False

        self._pipeline: Optional[ScriberPipeline] = None
        self._pipeline_task: Optional[asyncio.Task] = None
        self._ptt_task: Optional[asyncio.Task] = None
        # Track running file/YouTube transcription tasks by transcript ID
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._keyboard = None

        self._is_listening = False
        self._is_stopping = False  # Track if stop is in progress
        self._listening_lock = asyncio.Lock()  # Prevent race conditions on rapid hotkey presses
        self._status = "Stopped"

        self._current: Optional[TranscriptRecord] = None
        self._current_lock = threading.Lock()
        self._history: list[TranscriptRecord] = []
        self._history_by_id: dict[str, TranscriptRecord] = {}
        self._last_audio_broadcast = 0.0
        self._overlay_audio_enabled = False
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
    
    def _register_task(self, transcript_id: str, task: asyncio.Task) -> None:
        """Register a background task for a transcript."""
        # Use call_soon_threadsafe to ensure thread safety if called from callbacks
        self._running_tasks[transcript_id] = task
        task.add_done_callback(lambda _: self._loop.call_soon_threadsafe(lambda: self._unregister_task(transcript_id)))

    def _unregister_task(self, transcript_id: str) -> None:
        """Unregister a background task."""
        if transcript_id in self._running_tasks:
            del self._running_tasks[transcript_id]

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
                    content=data.get("_previewText", ""),  # Only preview for list display
                    created_at=data.get("createdAt", ""),
                    updated_at=data.get("updatedAt", ""),
                    summary="",  # Summary also lazy loaded
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
        return {
            "listening": self._is_listening,
            "status": self._status,
            "current": current.to_public(include_content=True) if current else None,
        }

    async def add_client(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.add(ws)
            self._clients_dirty = True

    async def remove_client(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.discard(ws)
            self._clients_dirty = True

    async def broadcast(self, payload: dict[str, Any]) -> None:
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

    def _set_status(self, status: str) -> None:
        self._status = status
        # status changes can happen from non-async callbacks; schedule the broadcast.
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self.broadcast({"type": "status", "status": status, "listening": self._is_listening})
            )
        )

    def _on_audio_level(self, rms: float) -> None:
        # Called from the sounddevice callback thread; throttle broadcasts to ~60fps.
        now = time.monotonic()
        if now - self._last_audio_broadcast < 0.016:  # ~60fps
            return
        self._last_audio_broadcast = now
        # Update native overlay waveform only when recording overlay is active
        if self._overlay_audio_enabled:
            update_overlay_audio(rms)
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.broadcast({"type": "audio_level", "rms": float(rms)}))
        )

    def _on_transcription(self, text: str, is_final: bool) -> None:
        if is_final:
            with self._current_lock:
                if self._current:
                    self._current.append_final_text(text)
        payload: dict[str, Any] = {"type": "transcript", "text": text, "isFinal": bool(is_final)}
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(payload)))

    def _on_pipeline_done(self, task: asyncio.Task) -> None:
        async def _safe_cleanup():
            """Cleanup state with proper lock protection."""
            async with self._listening_lock:
                self._is_listening = False
                self._is_stopping = False
                self._pipeline = None
                self._pipeline_task = None
        
        async def _broadcast_error(error_msg: str):
            """Broadcast error to frontend."""
            await self.broadcast({"type": "error", "message": error_msg})
        
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - runtime dependent        
            logger.error(f"Pipeline error: {exc}")
            self._set_status("Error")
            # Hide overlay when pipeline fails to prevent it staying stuck at "Preparing..."
            self._overlay_audio_enabled = False
            hide_recording_overlay()
            
            # Parse error and create user-friendly message
            error_str = str(exc).lower()
            if "402" in error_str or "payment required" in error_str:
                user_msg = "STT service requires payment. Please check your account credits or subscription."
            elif "401" in error_str or "unauthorized" in error_str or "invalid api key" in error_str:
                user_msg = "Invalid API key. Please check your STT service credentials in Settings."
            elif "403" in error_str or "forbidden" in error_str:
                user_msg = "Access denied. Please check your STT service permissions."
            elif "unable to connect" in error_str or "connection" in error_str:
                user_msg = "Could not connect to STT service. Please check your internet connection."
            elif "timeout" in error_str:
                user_msg = "Connection to STT service timed out. Please try again."
            else:
                user_msg = f"Recording failed: {exc}"
            
            # Broadcast error to frontend
            self._loop.call_soon_threadsafe(
                lambda msg=user_msg: asyncio.create_task(_broadcast_error(msg))
            )
            
            failed_current = None
            with self._current_lock:
                if self._current:
                    self._current.finish("failed")
                    failed_current = self._current
            if failed_current:
                self._add_to_history(failed_current)
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
        await self.broadcast({"type": "history_updated"})

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
        self._add_to_history(rec)
        await self._broadcast_history_updated()

        async def _runner() -> None:
            try:
                await self._run_youtube_transcription(rec)
            except asyncio.CancelledError:
                # Ensure status is updated if cancelled
                if rec.status == "processing":
                    rec.status = "stopped"
                    rec.step = "Stopped by user"
                raise
            finally:
                pass  # Task cleanup handled by done callback

        task = asyncio.create_task(_runner(), name=f"youtube_transcribe_{rec.id}")
        self._register_task(rec.id, task)
        return rec

    async def _run_youtube_transcription(self, rec: TranscriptRecord) -> None:
        rec.step = "Downloading audio..."
        rec.updated_at = datetime.now().isoformat()
        await self._broadcast_history_updated()
        try:
            out_dir = self._downloads_dir / "youtube" / rec.id
            
            # Track download progress with speed and ETA
            last_broadcast_time = [0.0]  # Use list to allow mutation in closure
            
            def on_download_progress(progress) -> None:
                import time
                now = time.time()
                # Throttle broadcasts to max 4 per second to avoid flooding
                if now - last_broadcast_time[0] < 0.25:
                    return
                last_broadcast_time[0] = now
                
                # Build step message with speed and ETA
                if progress.speed and progress.eta:
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
            
            audio_path = await download_youtube_audio(
                rec.source_url, 
                output_dir=out_dir, 
                audio_format="mp3",
                on_progress=on_download_progress
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

            pipeline = ScriberPipeline(
                service_name=Config.DEFAULT_STT_SERVICE,
                on_status_change=None,
                on_audio_level=None,
                on_transcription=on_transcription,
                on_progress=on_progress,
            )
            
            # Use direct file upload for Soniox (more efficient), fallback to pipecat for others
            if Config.DEFAULT_STT_SERVICE in ("soniox", "soniox_async"):
                await pipeline.transcribe_file_direct(str(audio_path))
            else:
                await pipeline.transcribe_file(str(audio_path))

            logger.info(f"YouTube transcription completed: {len(rec.content)} chars")
            rec.status = "completed"
            rec.step = "Completed"
            logger.debug(f"YouTube record updated: status={rec.status}, step={rec.step}")

            # Auto-summarize if enabled
            if Config.AUTO_SUMMARIZE and rec.content:
                try:
                    from src.summarization import summarize_text
                    rec.step = "Summarizing..."
                    rec.updated_at = datetime.now().isoformat()
                    await self._broadcast_history_updated()
                    rec.summary = await summarize_text(rec.content, Config.SUMMARIZATION_MODEL)
                    rec.step = "Completed"
                    logger.info(f"YouTube auto-summarization completed: {len(rec.summary)} chars")
                except Exception as sum_err:
                    logger.warning(f"Auto-summarization failed: {sum_err}")
                    rec.step = "Completed"
        except (ValueError, ImportError) as exc:
            rec.status = "failed"
            rec.step = "Failed"
            rec.content = (rec.content + "\n" if rec.content else "") + f"[Error] {exc}"
        except YouTubeDownloadError as exc:
            rec.status = "failed"
            rec.step = "Failed"
            rec.content = (rec.content + "\n" if rec.content else "") + f"[Download error] {exc}"
        except Exception as exc:
            logger.exception("YouTube transcription failed")
            rec.status = "failed"
            rec.step = "Failed"
            rec.content = (rec.content + "\n" if rec.content else "") + f"[Error] {exc}"
        finally:
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
            duration="--:--",
            status="processing",
            type="file",
            language=Config.LANGUAGE or "auto",
            step="Queued",
            source_url=str(file_path),
        )
        # Store file size in content temporarily for display
        if file_size:
            rec.channel = file_size  # Reuse channel field for file size display
        self._add_to_history(rec)
        await self._broadcast_history_updated()

        async def _runner() -> None:
            try:
                await self._run_file_transcription(rec, file_path)
            except asyncio.CancelledError:
                # Ensure status is updated if cancelled
                if rec.status == "processing":
                    rec.status = "stopped"
                    rec.step = "Stopped by user"
                raise
            finally:
                pass  # Task cleanup handled by done callback

        task = asyncio.create_task(_runner(), name=f"file_transcribe_{rec.id}")
        self._register_task(rec.id, task)
        return rec

    async def _run_file_transcription(self, rec: TranscriptRecord, file_path: Path) -> None:
        """Run transcription on an uploaded file."""
        rec.step = "Preparing audio..."
        rec.updated_at = datetime.now().isoformat()
        await self._broadcast_history_updated()
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

            pipeline = ScriberPipeline(
                service_name=Config.DEFAULT_STT_SERVICE,
                on_status_change=None,
                on_audio_level=None,
                on_transcription=on_transcription,
                on_progress=on_progress,
            )
            
            # Use direct file upload for Soniox (more efficient), fallback to pipecat for others
            if Config.DEFAULT_STT_SERVICE in ("soniox", "soniox_async"):
                await pipeline.transcribe_file_direct(str(file_path))
            else:
                await pipeline.transcribe_file(str(file_path))

            logger.info(f"File transcription completed: {len(rec.content)} chars")
            rec.status = "completed"
            rec.step = "Completed"

            # Auto-summarize if enabled
            if Config.AUTO_SUMMARIZE and rec.content:
                try:
                    from src.summarization import summarize_text
                    rec.step = "Summarizing..."
                    rec.updated_at = datetime.now().isoformat()
                    await self._broadcast_history_updated()
                    rec.summary = await summarize_text(rec.content, Config.SUMMARIZATION_MODEL)
                    rec.step = "Completed"
                    logger.info(f"File auto-summarization completed: {len(rec.summary)} chars")
                except Exception as sum_err:
                    logger.warning(f"Auto-summarization failed: {sum_err}")
                    rec.step = "Completed"
        except (ValueError, ImportError) as exc:
            rec.status = "failed"
            rec.step = "Failed"
            rec.content = (rec.content + "\n" if rec.content else "") + f"[Error] {exc}"
        except Exception as exc:
            logger.exception("File transcription failed")
            rec.status = "failed"
            rec.step = "Failed"
            rec.content = (rec.content + "\n" if rec.content else "") + f"[Error] {exc}"
        finally:
            rec.updated_at = datetime.now().isoformat()
            self._save_transcript_to_db(rec)  # Persist to database
            await self._broadcast_history_updated()
            # Cleanup: delete the uploaded file and its directory
            try:
                import shutil
                file_dir = file_path.parent
                if file_dir.exists() and file_dir.name != "files":  # Don't delete the root files dir
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
            with self._current_lock:
                self._current = rec

            # Ensure overlay has stop callback connected before showing
            self._get_overlay()

            # Show initializing overlay immediately for user feedback
            self._overlay_audio_enabled = False
            show_initializing_overlay()

            # Callback to transition overlay when mic is ready
            def on_mic_ready():
                logger.debug("on_mic_ready callback triggered - transitioning overlay to recording mode")
                self._overlay_audio_enabled = True
                show_recording_overlay()
                logger.info("Microphone ready - recording started")

            self._pipeline = ScriberPipeline(
                service_name=Config.DEFAULT_STT_SERVICE,
                on_status_change=self._set_status,
                on_audio_level=self._on_audio_level,
                on_transcription=self._on_transcription,
                on_mic_ready=on_mic_ready,
            )
            self._pipeline_task = asyncio.create_task(self._pipeline.start(), name="scriber_pipeline")
            self._pipeline_task.add_done_callback(self._on_pipeline_done)
            self._is_listening = True
            self._set_status("Listening")
            await self.broadcast({"type": "session_started", "session": rec.to_public(include_content=True)})

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
            await self.broadcast({"type": "transcribing"})
        
        try:
            if pipeline:
                await pipeline.stop()
            
            # Now that pipeline has stopped and transcription callback has fired,
            # clear _current to prevent any further modifications
            with self._current_lock:
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
        finally:
            async with self._listening_lock:
                self._is_stopping = False
            
            self._set_status("Stopped")

            if current:
                current.finish("completed")
                self._add_to_history(current)
                self._save_transcript_to_db(current)  # Persist to database
                await self.broadcast({"type": "session_finished", "session": current.to_public(include_content=True)})
                await self._broadcast_history_updated()

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

    def get_settings(self) -> dict[str, Any]:
        return {
            "hotkey": _hotkey_to_display(Config.HOTKEY),
            "hotkeyRaw": Config.HOTKEY,
            "mode": Config.MODE,
            "defaultSttService": Config.DEFAULT_STT_SERVICE,
            "sonioxMode": Config.SONIOX_MODE,
            "sonioxAsyncModel": Config.SONIOX_ASYNC_MODEL,
            "language": Config.LANGUAGE,
            "micDevice": Config.MIC_DEVICE,
            "favoriteMic": Config.FAVORITE_MIC or "",
            "micAlwaysOn": bool(Config.MIC_ALWAYS_ON),
            "debug": bool(Config.DEBUG),
            "customVocab": Config.CUSTOM_VOCAB or "",
            "summarizationPrompt": Config.SUMMARIZATION_PROMPT or "",
            "summarizationModel": Config.SUMMARIZATION_MODEL or "gemini-flash-latest",
            "autoSummarize": bool(Config.AUTO_SUMMARIZE),
            "openaiSttModel": Config.OPENAI_STT_MODEL,
            "visualizerBarCount": Config.VISUALIZER_BAR_COUNT,
            "apiKeys": {
                "soniox": Config.SONIOX_API_KEY or "",
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
            self._save_transcript_to_db(rec)
            await self._broadcast_history_updated()
            return True
            
        return False

    def list_microphones(self) -> list[dict[str, str]]:
        """List available microphone devices.
        
        Returns devices with:
        - deviceId: The device name (stable across reboots, used for persistence)
        - label: Display label (may include "(Default)" suffix)
        
        Uses MME Host API as primary (most compatible with USB devices).
        """
        try:
            import sounddevice as sd  # type: ignore
        except Exception:  # pragma: no cover - optional runtime dep
            return [{"deviceId": "default", "label": "Default"}]

        devices: list[dict[str, str]] = [{"deviceId": "default", "label": "Default"}]
        
        try:
            # Get host APIs - prefer MME (most compatible, especially for USB devices)
            host_apis = sd.query_hostapis()
            mme_idx = next((i for i, h in enumerate(host_apis) if h.get('name', '') == 'MME'), None)
            wasapi_idx = next((i for i, h in enumerate(host_apis) if 'WASAPI' in h.get('name', '')), None)
            
            # Use MME for best USB device compatibility
            preferred_hostapi = mme_idx if mme_idx is not None else wasapi_idx
        except Exception:
            preferred_hostapi = None

        try:
            default = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        except Exception:
            default = None

        # Virtual/system devices to exclude
        exclude_patterns = [
            'soundmapper', 'stereo mix', 'stereomix', 'what u hear',
            'loopback', 'primary sound',
        ]
        
        seen_names: set[str] = set()
        
        try:
            for idx, dev in enumerate(sd.query_devices()):
                # Skip if no input channels
                if int(dev.get("max_input_channels", 0) or 0) <= 0:
                    continue
                
                # Only include devices from the preferred host API (MME)
                hostapi_idx = dev.get('hostapi', 0)
                if preferred_hostapi is not None and hostapi_idx != preferred_hostapi:
                    continue
                
                name = str(dev.get("name", f"Device {idx}"))
                name_lower = name.lower()
                
                # Skip virtual/system devices
                if any(pattern in name_lower for pattern in exclude_patterns):
                    continue
                
                # Skip devices that look like outputs
                if any(word in name_lower for word in ['output', 'speaker', 'lautsprecher', 'headphone']):
                    continue
                
                # Skip duplicates (same name)
                if name in seen_names:
                    continue
                seen_names.add(name)
                
                is_default = (default is not None and idx == default)
                label = f"{name} (Default)" if is_default else name
                # Use device NAME as the ID (stable across reboots)
                devices.append({"deviceId": name, "label": label})
                
        except Exception:
            pass

        return devices

    def resolve_microphone_device(self, device_name: str) -> str:
        """Resolve a device name to the current device index.
        
        Args:
            device_name: The saved device name (or "default")
            
        Returns:
            The device index as a string, or "default" if not found.
            Falls back to Windows default if the saved device is unavailable.
        """
        if device_name == "default" or not device_name:
            return "default"
        
        try:
            import sounddevice as sd
        except Exception:
            return "default"
        
        try:
            # Get preferred host API
            host_apis = sd.query_hostapis()
            wasapi_idx = next((i for i, h in enumerate(host_apis) if 'WASAPI' in h.get('name', '')), None)
            mme_idx = next((i for i, h in enumerate(host_apis) if h.get('name', '') == 'MME'), None)
            preferred_hostapi = wasapi_idx if wasapi_idx is not None else mme_idx
            
            # Search for the device by name
            for idx, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels", 0) or 0) <= 0:
                    continue
                
                # Prefer devices from the same host API
                hostapi_idx = dev.get('hostapi', 0)
                if preferred_hostapi is not None and hostapi_idx != preferred_hostapi:
                    continue
                
                name = str(dev.get("name", ""))
                if name == device_name:
                    logger.info(f"Resolved microphone '{device_name}' to device index {idx}")
                    return str(idx)
            
            # If not found in preferred host API, try any host API
            for idx, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels", 0) or 0) <= 0:
                    continue
                name = str(dev.get("name", ""))
                if name == device_name:
                    logger.info(f"Resolved microphone '{device_name}' to device index {idx} (different host API)")
                    return str(idx)
            
            # Device not found - fall back to default
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

            # Search filter
            if query_lower:
                searchable = (
                    (rec.title or "").lower() +
                    (rec.content or "").lower() +
                    (rec.channel or "").lower() +
                    (rec.summary or "").lower()
                )
                if query_lower not in searchable:
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
    await runner.cleanup()


async def _background_init(controller: ScriberWebController) -> None:
    """Background initialization after server starts.
    
    Runs in parallel to avoid blocking server startup:
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
    
    # Wait a bit more before heavy initialization
    await asyncio.sleep(0.5)
    
    # Prewarm overlay (runs in its own thread)
    try:
        from src.overlay import get_overlay
        # Get overlay triggers its initialization in a background thread
        # Pass None for on_stop since this is just prewarming
        await asyncio.to_thread(lambda: get_overlay(on_stop=None))
        logger.info("Overlay prewarmed (ready for first recording)")
    except Exception as e:
        logger.debug(f"Overlay prewarm skipped: {e}")
    
    # Then prewarm ML models
    try:
        from src.pipeline import _AnalyzerCache
        # Load in thread to avoid blocking event loop
        await asyncio.to_thread(_AnalyzerCache.get_vad_analyzer)
        await asyncio.to_thread(_AnalyzerCache.get_smart_turn_analyzer)
        logger.info("ML model cache warmed (first recording will start faster)")
    except Exception as e:
        logger.debug(f"Cache prewarm skipped: {e}")
    
    # Pre-import the configured STT service (4.4 optimization)
    # This ensures the module is already loaded when user presses hotkey
    try:
        await asyncio.to_thread(_prewarm_stt_service, Config.DEFAULT_STT_SERVICE)
        logger.info(f"STT service '{Config.DEFAULT_STT_SERVICE}' preloaded")
    except Exception as e:
        logger.debug(f"STT prewarm skipped: {e}")


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
        # soniox is already imported at module level
    except ImportError as e:
        logger.debug(f"Could not prewarm STT service {service_name}: {e}")


def main() -> None:
    host = os.getenv("SCRIBER_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("SCRIBER_WEB_PORT", "8765"))
    asyncio.run(run_server(host, port))


if __name__ == "__main__":
    main()
