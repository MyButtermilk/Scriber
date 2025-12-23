import asyncio
import json
import os
import re
import signal
import time
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
from src.overlay import get_overlay, show_recording_overlay, show_transcribing_overlay, hide_recording_overlay, update_overlay_audio
from src import database as db

TranscriptStatus = Literal["completed", "processing", "failed", "recording"]
TranscriptType = Literal["mic", "youtube", "file"]

_ALLOWED_ORIGINS_ENV = "SCRIBER_ALLOWED_ORIGINS"
_UPLOAD_MAX_BYTES_ENV = "SCRIBER_UPLOAD_MAX_BYTES"
_UPLOAD_MAX_MB_ENV = "SCRIBER_UPLOAD_MAX_MB"
_DEFAULT_UPLOAD_MAX_MB = 200

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

        self._pipeline: Optional[ScriberPipeline] = None
        self._pipeline_task: Optional[asyncio.Task] = None
        self._ptt_task: Optional[asyncio.Task] = None
        self._youtube_tasks: dict[str, asyncio.Task] = {}
        self._keyboard = None

        self._is_listening = False
        self._status = "Stopped"

        self._current: Optional[TranscriptRecord] = None
        self._history: list[TranscriptRecord] = []
        self._last_audio_broadcast = 0.0

        self._downloads_dir = Path(os.getenv("SCRIBER_DOWNLOADS_DIR", "downloads")).resolve()

        # Initialize native overlay for system-wide recording popup
        self._overlay = get_overlay(on_stop=lambda: self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.stop_listening())
        ))
        
        # Initialize database and load persisted transcripts
        db.init_database()
        self._load_transcripts_from_db()
    
    def _load_transcripts_from_db(self) -> None:
        """Load transcripts from database on startup."""
        try:
            saved = db.load_all_transcripts()
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
                    content=data.get("content", ""),
                    created_at=data.get("createdAt", ""),
                    updated_at=data.get("updatedAt", ""),
                    summary=data.get("summary", ""),
                )
                self._history.append(rec)
            logger.info(f"Loaded {len(self._history)} transcripts from database")
        except Exception as e:
            logger.error(f"Failed to load transcripts from database: {e}")
    
    def _save_transcript_to_db(self, record: TranscriptRecord) -> None:
        """Save a transcript to the database."""
        try:
            db.save_transcript(record)
        except Exception as e:
            logger.error(f"Failed to save transcript to database: {e}")

    def get_state(self) -> dict[str, Any]:
        return {
            "listening": self._is_listening,
            "status": self._status,
            "current": self._current.to_public(include_content=True) if self._current else None,
        }

    async def add_client(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.add(ws)

    async def remove_client(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        msg = json.dumps(payload, ensure_ascii=False)
        async with self._clients_lock:
            clients = list(self._clients)
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
        # Update native overlay waveform
        update_overlay_audio(rms)
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.broadcast({"type": "audio_level", "rms": float(rms)}))
        )

    def _on_transcription(self, text: str, is_final: bool) -> None:
        if is_final and self._current:
            self._current.append_final_text(text)
        payload: dict[str, Any] = {"type": "transcript", "text": text, "isFinal": bool(is_final)}
        if is_final and self._current:
            payload["content"] = self._current.content
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast(payload)))

    def _on_pipeline_done(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - runtime dependent
            logger.error(f"Pipeline error: {exc}")
            self._set_status("Error")
            if self._current:
                self._current.finish("failed")
                self._history.insert(0, self._current)
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self.broadcast({"type": "history_updated"}))
                )
        finally:
            self._is_listening = False
            self._pipeline = None
            self._pipeline_task = None

    def _touch_history(self) -> None:
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast({"type": "history_updated"})))

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
        self._history.insert(0, rec)
        await self.broadcast({"type": "history_updated"})

        async def _runner() -> None:
            try:
                await self._run_youtube_transcription(rec)
            finally:
                self._youtube_tasks.pop(rec.id, None)

        task = asyncio.create_task(_runner(), name=f"youtube_transcribe_{rec.id}")
        self._youtube_tasks[rec.id] = task
        return rec

    async def _run_youtube_transcription(self, rec: TranscriptRecord) -> None:
        rec.step = "Downloading audio..."
        rec.updated_at = datetime.now().isoformat()
        await self.broadcast({"type": "history_updated"})
        try:
            out_dir = self._downloads_dir / "youtube" / rec.id
            audio_path = await download_youtube_audio(rec.source_url, output_dir=out_dir, audio_format="mp3")

            def on_transcription(text: str, is_final: bool) -> None:
                if not is_final:
                    return
                rec.append_final_text(text)
                logger.debug(f"YouTube transcription received: {len(text)} chars, total: {len(rec.content)} chars")

            def on_progress(step: str) -> None:
                rec.step = step
                rec.updated_at = datetime.now().isoformat()
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self.broadcast({"type": "history_updated"}))
                )

            rec.step = "Transcribing..."
            rec.updated_at = datetime.now().isoformat()
            await self.broadcast({"type": "history_updated"})

            pipeline = ScriberPipeline(
                service_name=Config.DEFAULT_STT_SERVICE,
                on_status_change=None,
                on_audio_level=None,
                on_transcription=on_transcription,
                on_progress=on_progress,
            )
            # Use direct file upload to Soniox (bypasses PCM conversion)
            await pipeline.transcribe_file_direct(str(audio_path))

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
                    await self.broadcast({"type": "history_updated"})
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
            await self.broadcast({"type": "history_updated"})
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
        self._history.insert(0, rec)
        await self.broadcast({"type": "history_updated"})

        async def _runner() -> None:
            try:
                await self._run_file_transcription(rec, file_path)
            finally:
                self._youtube_tasks.pop(rec.id, None)

        task = asyncio.create_task(_runner(), name=f"file_transcribe_{rec.id}")
        self._youtube_tasks[rec.id] = task  # Reuse youtube_tasks dict for file tasks too
        return rec

    async def _run_file_transcription(self, rec: TranscriptRecord, file_path: Path) -> None:
        """Run transcription on an uploaded file."""
        rec.step = "Preparing audio..."
        rec.updated_at = datetime.now().isoformat()
        await self.broadcast({"type": "history_updated"})
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
                    lambda: asyncio.create_task(self.broadcast({"type": "history_updated"}))
                )

            rec.step = "Transcribing..."
            rec.updated_at = datetime.now().isoformat()
            await self.broadcast({"type": "history_updated"})

            pipeline = ScriberPipeline(
                service_name=Config.DEFAULT_STT_SERVICE,
                on_status_change=None,
                on_audio_level=None,
                on_transcription=on_transcription,
                on_progress=on_progress,
            )
            # Use direct file upload to Soniox (bypasses PCM conversion)
            await pipeline.transcribe_file_direct(str(file_path))

            logger.info(f"File transcription completed: {len(rec.content)} chars")
            rec.status = "completed"
            rec.step = "Completed"

            # Auto-summarize if enabled
            if Config.AUTO_SUMMARIZE and rec.content:
                try:
                    from src.summarization import summarize_text
                    rec.step = "Summarizing..."
                    rec.updated_at = datetime.now().isoformat()
                    await self.broadcast({"type": "history_updated"})
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
            await self.broadcast({"type": "history_updated"})
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
        if self._is_listening:
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
        self._current = rec

        self._pipeline = ScriberPipeline(
            service_name=Config.DEFAULT_STT_SERVICE,
            on_status_change=self._set_status,
            on_audio_level=self._on_audio_level,
            on_transcription=self._on_transcription,
        )
        self._pipeline_task = asyncio.create_task(self._pipeline.start(), name="scriber_pipeline")
        self._pipeline_task.add_done_callback(self._on_pipeline_done)
        self._is_listening = True
        self._set_status("Listening")
        # Show native overlay
        show_recording_overlay()
        await self.broadcast({"type": "session_started", "session": rec.to_public(include_content=True)})

    async def stop_listening(self) -> None:
        if not self._is_listening:
            return
        
        # Show transcribing state before stopping pipeline
        show_transcribing_overlay()
        await self.broadcast({"type": "transcribing"})
        
        try:
            if self._pipeline:
                await self._pipeline.stop()
            if self._pipeline_task:
                self._pipeline_task.cancel()
                try:
                    await self._pipeline_task
                except asyncio.CancelledError:
                    pass
        finally:
            self._is_listening = False
            self._pipeline = None
            self._pipeline_task = None
            self._set_status("Stopped")
            # Hide native overlay after transcription complete
            hide_recording_overlay()

            if self._current:
                self._current.finish("completed")
                self._history.insert(0, self._current)
                finished = self._current
                self._current = None
                self._save_transcript_to_db(finished)  # Persist to database
                await self.broadcast({"type": "session_finished", "session": finished.to_public(include_content=True)})
                await self.broadcast({"type": "history_updated"})

    async def toggle_listening(self) -> None:
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

    def list_transcripts(self, *, include_content: bool = False) -> list[dict[str, Any]]:
        out = []
        for rec in self._history:
            out.append(rec.to_public(include_content=include_content))
        return out

    def get_transcript(self, transcript_id: str) -> Optional[dict[str, Any]]:
        for rec in self._history:
            if rec.id == transcript_id:
                return rec.to_public(include_content=True)
        return None


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
        ctl: ScriberWebController = request.app["controller"]
        return web.json_response({"items": ctl.list_transcripts(include_content=False)})

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

        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                video = await get_video_by_id(api_key, video_id, session=session)
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
            max_bytes = _get_upload_max_bytes()
            limit_label = _format_upload_limit(max_bytes)
            if request.content_length is not None and request.content_length > max_bytes:
                return web.json_response(
                    {"message": f"File too large (max {limit_label})."}, status=413
                )

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
            allowed_extensions = {".mp3", ".m4a", ".wav", ".mp4", ".mov", ".webm", ".ogg", ".flac", ".aac"}
            safe_filename = _safe_upload_filename(original_filename)
            ext = Path(safe_filename).suffix.lower()
            if ext not in allowed_extensions:
                return web.json_response(
                    {"message": f"Unsupported file type: {ext}. Allowed: {', '.join(sorted(allowed_extensions))}"},
                    status=400,
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

            # Start transcription
            rec = await ctl.start_file_transcription(save_path, safe_filename)
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
        found = None
        for i, rec in enumerate(ctl._history):
            if rec.id == transcript_id:
                found = ctl._history.pop(i)
                break

        if not found:
            return web.json_response({"message": "Transcript not found"}, status=404)

        # Delete from database
        db.delete_transcript(transcript_id)

        # Broadcast update to clients
        await ctl.broadcast({"type": "history_updated"})
        logger.info(f"Deleted transcript: {found.title} ({transcript_id})")

        return web.json_response({"success": True, "id": transcript_id})

    async def summarize_transcript(request: web.Request):
        """Summarize a transcript using the configured LLM model."""
        from src.summarization import summarize_text
        
        ctl: ScriberWebController = request.app["controller"]
        transcript_id = request.match_info.get("id", "")
        if not transcript_id:
            return web.json_response({"message": "Missing transcript ID"}, status=400)

        # Find the transcript
        rec = None
        for r in ctl._history:
            if r.id == transcript_id:
                rec = r
                break

        if not rec:
            return web.json_response({"message": "Transcript not found"}, status=404)

        if not rec.content or not rec.content.strip():
            return web.json_response({"message": "Transcript has no content to summarize"}, status=400)

        if rec.status != "completed":
            return web.json_response({"message": "Transcript is not yet completed"}, status=400)

        try:
            model = getattr(Config, "SUMMARIZATION_MODEL", "gemini-2.0-flash")
            summary = await summarize_text(rec.content, model)
            rec.summary = summary
            rec.updated_at = datetime.now().isoformat()
            await ctl.broadcast({"type": "history_updated"})
            logger.info(f"Summarized transcript: {rec.title} ({len(summary)} chars)")
            return web.json_response({"success": True, "summary": summary})
        except ValueError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("Summarization failed")
            return web.json_response({"message": str(exc) or "Summarization failed"}, status=500)

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


def main() -> None:
    host = os.getenv("SCRIBER_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("SCRIBER_WEB_PORT", "8765"))
    asyncio.run(run_server(host, port))


if __name__ == "__main__":
    main()
