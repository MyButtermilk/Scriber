"""Azure MAI Transcribe helpers and buffered processor."""

from __future__ import annotations

import contextlib
import asyncio
import io
import json
import os
import tempfile
from pathlib import Path
from collections.abc import AsyncGenerator
from typing import Any, Awaitable, BinaryIO, Callable

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StopFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_service import AIService
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.config import Config
from src.runtime.ffmpeg_commands import classify_ffmpeg_stderr, mp3_encode_pcm_pipe_args, mp3_transcode_args
from src.runtime.http_response import read_response_text_limited
from src.runtime.media_tools import require_media_tool
from src.runtime.audio_spool import append_pcm_frame, close_pcm_spool, create_pcm_spool
from src.runtime.capture_time_encoder import (
    CaptureTimeEncoderError,
    CaptureTimeFfmpegEncoder,
)
from src.runtime.subprocess_utils import (
    communicate_or_kill_on_cancel,
    hidden_subprocess_kwargs,
    read_stream_limited,
)

_AZURE_MAI_DEFAULT_MODEL = "mai-transcribe-1.5"
_AZURE_MAI_API_VERSION = "2025-10-15"
_AZURE_MAI_DEFAULT_REGION = "northeurope"
_AZURE_MAI_SUPPORTED_REGIONS = {"eastus", "northeurope", "westus"}
_AZURE_MAI_DIRECT_UPLOAD_EXTENSIONS = {".mp3"}
_AZURE_MAI_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
}

AzureMaiRawTransport = Callable[..., Awaitable[tuple[int, str]]]


def _capture_time_mp3_enabled() -> bool:
    # The matched installed 5/15/30/60-second no-regression matrix did not
    # promote this path, so it remains an explicit opt-in.
    return os.getenv("SCRIBER_AZURE_MAI_CAPTURE_TIME_MP3", "0").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def azure_mai_region(region: str | None) -> str:
    return (region or _AZURE_MAI_DEFAULT_REGION).strip().lower() or _AZURE_MAI_DEFAULT_REGION


def validate_azure_mai_region(region: str | None) -> str:
    resolved = azure_mai_region(region)
    if resolved not in _AZURE_MAI_SUPPORTED_REGIONS:
        allowed = ", ".join(sorted(_AZURE_MAI_SUPPORTED_REGIONS))
        raise ValueError(
            "Azure MAI Transcribe is not supported in region "
            f"'{resolved}'. Use a Speech/Foundry resource in one of: {allowed}. "
            "The nearest supported Europe region is northeurope."
        )
    return resolved


def azure_mai_language_locales(language: Language | str | None) -> list[str]:
    if not language:
        return []
    raw = str(language.value if isinstance(language, Language) else language).strip()
    if not raw or raw.lower() == "auto":
        return []
    return [raw]


def azure_mai_model(model: str | None = None) -> str:
    selected = model or getattr(Config, "AZURE_MAI_MODEL", "") or _AZURE_MAI_DEFAULT_MODEL
    return selected.strip() or _AZURE_MAI_DEFAULT_MODEL


def azure_mai_phrase_list(custom_vocab: str | None = None) -> list[str]:
    raw = custom_vocab if custom_vocab is not None else getattr(Config, "CUSTOM_VOCAB", "")
    return [term.strip() for term in str(raw or "").split(",") if term.strip()]


def build_azure_mai_definition(
    language: Language | str | None,
    *,
    model: str | None = None,
    custom_vocab: str | None = None,
) -> dict[str, Any]:
    selected_model = azure_mai_model(model)
    definition: dict[str, Any] = {
        "enhancedMode": {
            "enabled": True,
            "model": selected_model,
        }
    }
    locales = azure_mai_language_locales(language)
    if locales:
        definition["locales"] = locales
    phrases = azure_mai_phrase_list(custom_vocab)
    if selected_model == "mai-transcribe-1.5" and phrases:
        definition["phraseList"] = {"phrases": phrases}
    return definition


def azure_mai_content_type(path: Path) -> str:
    return _AZURE_MAI_CONTENT_TYPES.get(path.suffix.lower(), "audio/mpeg")


def _phrase_text(phrase: dict[str, Any]) -> str:
    for key in ("text", "displayText", "display", "lexical"):
        value = str(phrase.get(key) or "").strip()
        if value:
            return value
    return ""


def azure_mai_transcript_payload_to_text(payload: dict[str, Any]) -> str:
    combined = payload.get("combinedPhrases")
    if isinstance(combined, list):
        text = "\n\n".join(
            _phrase_text(item)
            for item in combined
            if isinstance(item, dict) and _phrase_text(item)
        ).strip()
        if text:
            return text

    phrases = payload.get("phrases") or payload.get("recognizedPhrases")
    if isinstance(phrases, list):
        text = " ".join(
            _phrase_text(item)
            for item in phrases
            if isinstance(item, dict) and _phrase_text(item)
        ).strip()
        if text:
            return text

    for key in ("text", "displayText", "transcription", "transcript"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


async def _transcode_to_mp3(source_path: Path, target_path: Path) -> Path:
    ffmpeg = require_media_tool("ffmpeg")

    proc = await asyncio.create_subprocess_exec(
        *mp3_transcode_args(ffmpeg, source_path, target_path, bitrate="64k"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    _, stderr = await communicate_or_kill_on_cancel(
        proc,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=1024 * 1024,
    )
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
        friendly = classify_ffmpeg_stderr(err)
        raise RuntimeError(f"ffmpeg MAI audio transcode failed: {friendly or proc.returncode}")
    if not target_path.exists():
        raise RuntimeError("ffmpeg MAI audio transcode completed but output file is missing.")
    return target_path


async def _pcm_to_mp3(audio_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    mp3_source = await _pcm_stream_to_mp3(
        io.BytesIO(audio_bytes),
        sample_rate=sample_rate,
        channels=channels,
    )
    try:
        return mp3_source.read()
    finally:
        mp3_source.close()


async def _pcm_stream_to_mp3(
    audio_source: BinaryIO,
    *,
    sample_rate: int,
    channels: int,
) -> BinaryIO:
    ffmpeg = require_media_tool("ffmpeg")
    proc = await asyncio.create_subprocess_exec(
        *mp3_encode_pcm_pipe_args(
            ffmpeg,
            input_sample_rate=max(1, int(sample_rate or 16000)),
            input_channels=max(1, int(channels or 1)),
            bitrate="64k",
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        raise RuntimeError("ffmpeg MAI PCM encode pipes were not created.")

    mp3_file = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode="w+b")

    async def feed_pcm() -> None:
        audio_source.seek(0)
        try:
            while chunk := await asyncio.to_thread(audio_source.read, 1024 * 1024):
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await proc.stdin.wait_closed()

    async def capture_mp3() -> None:
        while chunk := await proc.stdout.read(64 * 1024):
            mp3_file.write(chunk)

    feed_task = asyncio.create_task(feed_pcm(), name="azure-mai-ffmpeg-feed")
    capture_task = asyncio.create_task(capture_mp3(), name="azure-mai-ffmpeg-capture")
    stderr_task = asyncio.create_task(
        read_stream_limited(proc.stderr),
        name="azure-mai-ffmpeg-stderr",
    )
    try:
        await asyncio.gather(feed_task, capture_task)
        stderr = await stderr_task
        return_code = await proc.wait()
        if return_code != 0:
            err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            friendly = classify_ffmpeg_stderr(err)
            raise RuntimeError(f"ffmpeg MAI PCM encode failed: {friendly or return_code}")
        if mp3_file.tell() <= 0:
            raise RuntimeError("ffmpeg MAI PCM encode completed but output audio is empty.")
        mp3_file.seek(0)
        return mp3_file
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        for task in (feed_task, capture_task, stderr_task):
            task.cancel()
        await asyncio.gather(feed_task, capture_task, stderr_task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await proc.wait()
        mp3_file.close()
        raise


@contextlib.asynccontextmanager
async def prepared_azure_mai_audio_file(source_path: Path):
    if source_path.suffix.lower() in _AZURE_MAI_DIRECT_UPLOAD_EXTENSIONS:
        yield source_path
        return

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        await _transcode_to_mp3(source_path, tmp_path)
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)


def _report_progress(on_progress: Callable[[str], None] | None, message: str) -> None:
    if not on_progress:
        return
    try:
        on_progress(message)
    except Exception:
        pass


async def _azure_mai_http_raw_transport(
    *,
    session: aiohttp.ClientSession,
    url: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    definition: dict[str, Any],
    speech_key: str,
    timeout_secs: float,
    audio_preparation_implementation: str | None = None,
) -> tuple[int, str]:
    del audio_preparation_implementation
    data = aiohttp.FormData()
    data.add_field("audio", audio_source, filename=filename, content_type=content_type)
    data.add_field("definition", json.dumps(definition), content_type="application/json")
    async with session.post(
        url,
        data=data,
        headers={"Ocp-Apim-Subscription-Key": speech_key},
        timeout=aiohttp.ClientTimeout(total=timeout_secs),
    ) as resp:
        raw = await read_response_text_limited(resp, 64 * 1024 * 1024)
        return int(resp.status), raw


async def transcribe_with_azure_mai(
    *,
    session: aiohttp.ClientSession,
    speech_key: str,
    region: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    language: Language | str | None,
    model: str | None = None,
    custom_vocab: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
    raw_transport: AzureMaiRawTransport | None = None,
    on_response_complete: Callable[[], None] | None = None,
    audio_preparation_implementation: str | None = None,
) -> dict[str, Any]:
    region = validate_azure_mai_region(region)
    url = (
        f"https://{region}.api.cognitive.microsoft.com/"
        f"speechtotext/transcriptions:transcribe?api-version={_AZURE_MAI_API_VERSION}"
    )
    definition = build_azure_mai_definition(
        language,
        model=model,
        custom_vocab=custom_vocab,
    )

    _report_progress(on_progress, "Uploading audio...")
    _report_progress(on_progress, "Processing transcription...")
    transport = raw_transport or _azure_mai_http_raw_transport
    status, raw = await transport(
        session=session,
        url=url,
        audio_source=audio_source,
        filename=filename,
        content_type=content_type,
        definition=definition,
        speech_key=speech_key,
        timeout_secs=timeout_secs,
        audio_preparation_implementation=audio_preparation_implementation,
    )
    # This boundary is intentionally before status handling and JSON parsing:
    # installed performance evidence measures controllable local tail latency
    # from the instant the complete raw provider response is available.
    if on_response_complete is not None:
        on_response_complete()
    if status >= 400:
        raise RuntimeError(f"Azure MAI transcription failed ({status}): {raw[:500]}")

    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {"text": raw}
    return payload if isinstance(payload, dict) else {}


class AzureMaiTranscribeSTTService(STTService):
    """Pipecat STT service for Azure MAI Transcribe's batch REST API.

    MAI Transcribe 1.5 is currently exposed through Azure's LLM Speech REST API,
    not the streaming Azure Speech SDK path used by Pipecat's AzureSTTService.
    This service keeps the app on Pipecat's STT boundary while buffering audio
    until a terminal frame before calling MAI once.
    """

    def __init__(
        self,
        *,
        speech_key: str,
        region: str,
        language: Language | str = "auto",
        model: str | None = None,
        custom_vocab: str | None = None,
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        audio_passthrough: bool = True,
        raw_transport: AzureMaiRawTransport | None = None,
        on_response_complete: Callable[[], None] | None = None,
        on_encoder_marker: Callable[[str], None] | None = None,
        capture_time_mp3_enabled: bool | None = None,
    ) -> None:
        language_locales = azure_mai_language_locales(language)
        selected_model = azure_mai_model(model)
        super().__init__(
            audio_passthrough=audio_passthrough,
            settings=STTSettings(
                model=selected_model,
                language=language_locales[0] if language_locales else None,
            ),
        )
        self._speech_key = speech_key
        self._region = validate_azure_mai_region(region)
        self._language = language or "auto"
        self._model = selected_model
        self._custom_vocab = (
            str(custom_vocab)
            if custom_vocab is not None
            else str(getattr(Config, "CUSTOM_VOCAB", "") or "")
        )
        self._session = session
        self._on_progress = on_progress
        self._raw_transport = raw_transport
        self._on_response_complete = on_response_complete
        self._on_encoder_marker = on_encoder_marker
        self._buffer = self._create_buffer()
        self._buffer_size = 0
        self._sample_rate = 16000
        self._channels = 1
        self._capture_encoder: CaptureTimeFfmpegEncoder | None = None
        self._capture_encoder_enabled = (
            _capture_time_mp3_enabled()
            if capture_time_mp3_enabled is None
            else bool(capture_time_mp3_enabled)
        )
        self._capture_encoder_disabled = not self._capture_encoder_enabled
        self._audio_preparation_implementation: str | None = None

    def _create_buffer(self):
        return create_pcm_spool()

    def _reset_buffer(self) -> None:
        capture_encoder = getattr(self, "_capture_encoder", None)
        if capture_encoder is not None:
            capture_encoder.close_nowait()
        self._capture_encoder = None
        self._capture_encoder_disabled = not self._capture_encoder_enabled
        self._audio_preparation_implementation = None
        close_pcm_spool(getattr(self, "_buffer", None))
        self._buffer = self._create_buffer()
        self._buffer_size = 0

    def __del__(self) -> None:
        capture_encoder = getattr(self, "_capture_encoder", None)
        if capture_encoder is not None:
            capture_encoder.close_nowait()
        close_pcm_spool(getattr(self, "_buffer", None))

    def _create_capture_encoder(
        self,
        *,
        sample_rate: int,
        channels: int,
    ) -> CaptureTimeFfmpegEncoder:
        ffmpeg = require_media_tool("ffmpeg")
        return CaptureTimeFfmpegEncoder(
            mp3_encode_pcm_pipe_args(
                ffmpeg,
                input_sample_rate=max(1, int(sample_rate)),
                input_channels=max(1, int(channels)),
                bitrate="64k",
            ),
            sample_rate=sample_rate,
            channels=channels,
        )

    def _offer_capture_encoded_frame(self, audio: bytes) -> None:
        if self._capture_encoder_disabled or not audio:
            return
        if self._capture_encoder is None:
            try:
                self._capture_encoder = self._create_capture_encoder(
                    sample_rate=self._sample_rate,
                    channels=self._channels,
                )
            except Exception as exc:
                self._capture_encoder_disabled = True
                logger.warning(
                    "Azure MAI capture-time MP3 unavailable; retaining PCM fallback ({})",
                    type(exc).__name__,
                )
                return
        if not self._capture_encoder.offer(
            audio,
            sample_rate=self._sample_rate,
            channels=self._channels,
        ):
            self._capture_encoder_disabled = True
            logger.warning(
                "Azure MAI capture-time MP3 invalidated; retaining PCM fallback ({})",
                self._capture_encoder.error_code or "localCandidateUnavailable",
            )

    async def _finish_capture_encoder(self) -> BinaryIO | None:
        encoder = self._capture_encoder
        self._capture_encoder = None
        if encoder is None or self._capture_encoder_disabled:
            if encoder is not None:
                await encoder.abort()
            return None
        try:
            return await encoder.finish()
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, CaptureTimeEncoderError)
                else type(exc).__name__
            )
            logger.warning(
                "Azure MAI capture-time MP3 failed before upload; using PCM fallback ({})",
                reason,
            )
            return None

    async def _abort_capture_encoder(self) -> None:
        encoder = self._capture_encoder
        self._capture_encoder = None
        if encoder is not None:
            await encoder.abort()

    def _emit_encoder_marker(self, marker: str) -> None:
        callback = self._on_encoder_marker
        if callback is None:
            return
        try:
            callback(marker)
        except Exception:
            logger.debug("Azure MAI encoder marker callback failed")

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        mp3_bytes = await _pcm_to_mp3(
            audio_bytes,
            sample_rate=self._sample_rate,
            channels=self._channels,
        )
        return await self._transcribe_mp3(mp3_bytes)

    async def _transcribe_mp3(self, mp3_source: bytes | BinaryIO) -> str:

        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_azure_mai(
                session=session,
                speech_key=self._speech_key,
                region=self._region,
                audio_source=mp3_source,
                filename="audio.mp3",
                content_type="audio/mpeg",
                language=self._language,
                model=self._model,
                custom_vocab=self._custom_vocab,
                on_progress=self._on_progress,
                timeout_secs=900.0,
                raw_transport=self._raw_transport,
                on_response_complete=self._on_response_complete,
                audio_preparation_implementation=(
                    self._audio_preparation_implementation
                ),
            )

        if self._session:
            payload = await _call(self._session)
        else:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return azure_mai_transcript_payload_to_text(payload)

    def _transcription_frame(self, text: str) -> TranscriptionFrame:
        return TranscriptionFrame(
            text=text,
            user_id=self._user_id or "user",
            timestamp=time_now_iso8601(),
            result=None,
        )

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        text = (await self._transcribe_bytes(audio)).strip()
        if text:
            yield self._transcription_frame(text)

    async def process_audio_frame(self, frame: AudioRawFrame, direction: FrameDirection):
        if self._muted:
            return

        self._user_id = str(getattr(frame, "user_id", "") or "")
        if not frame.audio:
            logger.warning(f"Empty audio frame received for STT service: {self.name} {frame.num_frames}")
            return

        if getattr(frame, "sample_rate", None):
            self._sample_rate = int(frame.sample_rate or self._sample_rate)
        if getattr(frame, "num_channels", None):
            self._channels = max(1, int(frame.num_channels or self._channels))
        self._buffer_size = await append_pcm_frame(
            self._buffer,
            self._buffer_size,
            frame.audio,
        )
        self._offer_capture_encoded_frame(frame.audio)

    async def _flush_transcription(self, direction: FrameDirection) -> None:
        if not self._buffer_size:
            return

        _report_progress(self._on_progress, "Transcribing...")
        self._emit_encoder_marker("encoder_tail_started")
        try:
            mp3_source = await self._finish_capture_encoder()
            if mp3_source is None:
                self._audio_preparation_implementation = (
                    "post_stop_ffmpeg_mp3_v1"
                )
                mp3_source = await _pcm_stream_to_mp3(
                    self._buffer,
                    sample_rate=self._sample_rate,
                    channels=self._channels,
                )
            else:
                self._audio_preparation_implementation = (
                    "capture_time_ffmpeg_mp3_v1"
                )
        finally:
            self._emit_encoder_marker("encoder_tail_completed")
        try:
            text = (await self._transcribe_mp3(mp3_source)).strip()
        finally:
            mp3_source.close()
            self._audio_preparation_implementation = None
        if text:
            await self.push_frame(self._transcription_frame(text), direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if not isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            await super().process_frame(frame, direction)
            return

        await AIService.process_frame(self, frame, direction)
        if isinstance(frame, CancelFrame):
            await self._abort_capture_encoder()
            self._reset_buffer()
            await self.push_frame(frame, direction)
            return
        try:
            if getattr(self, "_skip_terminal_transcription", False):
                logger.info("Azure MAI: skipping terminal transcription for silent recording")
                await self._abort_capture_encoder()
                await self.push_frame(frame, direction)
                return
            await self._flush_transcription(direction)
        except Exception as exc:
            logger.error(f"Azure MAI transcription failed: {exc}")
            await self.push_frame(ErrorFrame(error=f"azure mai error: {exc}"), direction)
        finally:
            self._reset_buffer()
        await self.push_frame(frame, direction)
