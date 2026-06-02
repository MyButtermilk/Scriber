"""Azure MAI Transcribe helpers and buffered processor."""

from __future__ import annotations

import contextlib
import asyncio
import io
import json
import tempfile
import wave
from pathlib import Path
from typing import Any, BinaryIO, Callable

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
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.config import Config
from src.runtime.media_tools import require_media_tool

_AZURE_MAI_DEFAULT_MODEL = "mai-transcribe-1.5"
_AZURE_MAI_API_VERSION = "2025-10-15"
_AZURE_MAI_DEFAULT_REGION = "northeurope"
_AZURE_MAI_SUPPORTED_REGIONS = {"eastus", "northeurope", "westus"}
_AZURE_MAI_ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac"}
_AZURE_MAI_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
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


def _pcm_to_wav(audio_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with contextlib.closing(wave.open(buf, "wb")) as wf:
        wf.setnchannels(max(1, int(channels or 1)))
        wf.setsampwidth(2)
        wf.setframerate(max(1, int(sample_rate or 16000)))
        wf.writeframes(audio_bytes)
    return buf.getvalue()


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
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(target_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
        raise RuntimeError(f"ffmpeg MAI audio transcode failed: {err or proc.returncode}")
    if not target_path.exists():
        raise RuntimeError("ffmpeg MAI audio transcode completed but output file is missing.")
    return target_path


@contextlib.asynccontextmanager
async def prepared_azure_mai_audio_file(source_path: Path):
    if source_path.suffix.lower() in _AZURE_MAI_ALLOWED_EXTENSIONS:
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


async def transcribe_with_azure_mai(
    *,
    session: aiohttp.ClientSession,
    speech_key: str,
    region: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    language: Language | str | None,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
) -> dict[str, Any]:
    region = validate_azure_mai_region(region)
    url = (
        f"https://{region}.api.cognitive.microsoft.com/"
        f"speechtotext/transcriptions:transcribe?api-version={_AZURE_MAI_API_VERSION}"
    )
    definition = build_azure_mai_definition(language)

    data = aiohttp.FormData()
    data.add_field("audio", audio_source, filename=filename, content_type=content_type)
    data.add_field("definition", json.dumps(definition), content_type="application/json")

    _report_progress(on_progress, "Uploading audio...")
    _report_progress(on_progress, "Processing transcription...")
    async with session.post(
        url,
        data=data,
        headers={"Ocp-Apim-Subscription-Key": speech_key},
        timeout=aiohttp.ClientTimeout(total=timeout_secs),
    ) as resp:
        raw = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"Azure MAI transcription failed ({resp.status}): {raw[:500]}")

    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {"text": raw}
    return payload if isinstance(payload, dict) else {}


class AzureMaiTranscribeProcessor(FrameProcessor):
    """Buffered live/file processor for Azure MAI Transcribe."""

    def __init__(
        self,
        *,
        speech_key: str,
        region: str,
        language: Language | str = "auto",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._speech_key = speech_key
        self._region = validate_azure_mai_region(region)
        self._language = language or "auto"
        self._session = session
        self._on_progress = on_progress
        self._buffer = self._create_buffer()
        self._buffer_size = 0
        self._sample_rate = 16000
        self._channels = 1

    def _create_buffer(self):
        return tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)

    def _reset_buffer(self) -> None:
        try:
            self._buffer.close()
        except Exception:
            pass
        self._buffer = self._create_buffer()
        self._buffer_size = 0

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_bytes = _pcm_to_wav(
            audio_bytes=audio_bytes,
            sample_rate=self._sample_rate,
            channels=self._channels,
        )

        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_azure_mai(
                session=session,
                speech_key=self._speech_key,
                region=self._region,
                audio_source=wav_bytes,
                filename="audio.wav",
                content_type="audio/wav",
                language=self._language,
                on_progress=self._on_progress,
                timeout_secs=900.0,
            )

        if self._session:
            payload = await _call(self._session)
        else:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return azure_mai_transcript_payload_to_text(payload)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame):
            if frame.audio:
                if getattr(frame, "sample_rate", None):
                    self._sample_rate = int(frame.sample_rate or self._sample_rate)
                if getattr(frame, "num_channels", None):
                    self._channels = max(1, int(frame.num_channels or self._channels))
                self._buffer.write(frame.audio)
                self._buffer_size += len(frame.audio)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (EndFrame, StopFrame, CancelFrame)):
            try:
                if self._buffer_size:
                    _report_progress(self._on_progress, "Transcribing...")
                    self._buffer.seek(0)
                    text = (await self._transcribe_bytes(self._buffer.read())).strip()
                    if text:
                        await self.push_frame(
                            TranscriptionFrame(
                                text=text,
                                user_id="user",
                                timestamp=time_now_iso8601(),
                                result=None,
                            ),
                            direction,
                        )
            except Exception as exc:
                logger.error(f"Azure MAI transcription failed: {exc}")
                await self.push_frame(ErrorFrame(error=f"azure mai error: {exc}"), direction)
            finally:
                self._reset_buffer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
