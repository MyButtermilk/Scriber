"""AssemblyAI Universal-3-Pro async transcription helpers and processor."""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
import tempfile
import wave
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
from pipecat.utils.time import time_now_iso8601

_ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com/v2"
_ASSEMBLYAI_U3PRO_MODEL = "universal-3-pro"
_ASSEMBLYAI_U3PRO_SUPPORTED_LANGUAGES = {"en", "es", "pt", "fr", "de", "it"}
_VOCAB_SPLIT_RE = re.compile(r"[,\n;]+")
_MULTISPACE_RE = re.compile(r"\s+")
_MAX_KEYTERMS = 1000
_MAX_KEYTERM_WORDS = 6


def _pcm_to_wav(audio_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with contextlib.closing(wave.open(buf, "wb")) as wf:
        wf.setnchannels(max(1, int(channels or 1)))
        wf.setsampwidth(2)  # PCM int16
        wf.setframerate(max(1, int(sample_rate or 16000)))
        wf.writeframes(audio_bytes)
    return buf.getvalue()


def _report_progress(on_progress: Callable[[str], None] | None, message: str) -> None:
    if not on_progress:
        return
    try:
        on_progress(message)
    except Exception:
        pass


def build_keyterms_from_vocab(custom_vocab: str) -> list[str]:
    """Convert comma/newline separated custom vocab into AssemblyAI keyterms."""
    if not custom_vocab:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for raw in _VOCAB_SPLIT_RE.split(str(custom_vocab)):
        term = _MULTISPACE_RE.sub(" ", raw.strip())
        if not term:
            continue
        if len(term.split(" ")) > _MAX_KEYTERM_WORDS:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= _MAX_KEYTERMS:
            break
    return out


def build_u3pro_language_fields(config_lang: str | None) -> dict[str, Any]:
    """Build language fields for Universal-3-Pro payloads.

    Rules:
    - auto/empty -> language_detection=True
    - supported manual language -> language_code=<lang>
    - unsupported manual language -> language_detection=True
    """
    lang = (config_lang or "").strip().lower()
    if not lang or lang == "auto":
        return {"language_detection": True}

    lang_base = lang.replace("_", "-").split("-", 1)[0]
    if lang_base in _ASSEMBLYAI_U3PRO_SUPPORTED_LANGUAGES:
        return {"language_code": lang_base}

    logger.warning(
        f"AssemblyAI U3-Pro does not support manual language '{lang}'; "
        "falling back to language_detection=true"
    )
    return {"language_detection": True}


def format_assemblyai_utterances_to_scriber_text(utterances: list[dict[str, Any]]) -> str:
    """Format AssemblyAI utterances to Scriber speaker transcript format."""
    if not utterances:
        return ""

    speaker_map: dict[str, int] = {}
    lines: list[str] = []
    next_index = 1

    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        text = str(utterance.get("text", "") or "").strip()
        if not text:
            continue
        speaker_key = str(utterance.get("speaker", "") or "").strip() or "_unknown"
        speaker_num = speaker_map.get(speaker_key)
        if speaker_num is None:
            speaker_num = next_index
            speaker_map[speaker_key] = speaker_num
            next_index += 1
        lines.append(f"[Speaker {speaker_num}]: {text}")
    return "\n\n".join(lines)


def assemblyai_transcript_payload_to_text(
    payload: dict[str, Any],
    *,
    prefer_speaker_labels: bool,
) -> str:
    utterances = payload.get("utterances")
    utterance_list = utterances if isinstance(utterances, list) else []
    if prefer_speaker_labels and utterance_list:
        formatted = format_assemblyai_utterances_to_scriber_text(
            [u for u in utterance_list if isinstance(u, dict)]
        )
        if formatted:
            return formatted

    text = str(payload.get("text", "") or "").strip()
    if text:
        return text

    if utterance_list:
        return format_assemblyai_utterances_to_scriber_text(
            [u for u in utterance_list if isinstance(u, dict)]
        )
    return ""


async def transcribe_with_assemblyai_pre_recorded(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    language: str | None,
    custom_vocab: str = "",
    speaker_labels: bool = False,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
) -> dict[str, Any]:
    """Upload + transcribe with AssemblyAI Universal-3-Pro pre-recorded API."""
    headers = {"authorization": api_key}

    _report_progress(on_progress, "Uploading audio...")
    upload_timeout = min(max(30.0, timeout_secs * 0.4), 300.0)
    async with session.post(
        f"{_ASSEMBLYAI_BASE_URL}/upload",
        data=audio_source,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=upload_timeout),
    ) as upload_resp:
        if upload_resp.status not in (200, 201):
            raw = await upload_resp.text()
            raise RuntimeError(f"AssemblyAI upload failed ({upload_resp.status}): {raw[:500]}")
        upload_payload = await upload_resp.json()

    upload_url = str(upload_payload.get("upload_url") or "").strip()
    if not upload_url:
        raise RuntimeError("AssemblyAI upload response missing upload_url")

    submit_payload: dict[str, Any] = {
        "audio_url": upload_url,
        "speech_models": [_ASSEMBLYAI_U3PRO_MODEL],
        "speaker_labels": bool(speaker_labels),
    }
    submit_payload.update(build_u3pro_language_fields(language))
    keyterms = build_keyterms_from_vocab(custom_vocab)
    if keyterms:
        submit_payload["keyterms_prompt"] = keyterms
    elif str(custom_vocab or "").strip():
        logger.warning(
            "AssemblyAI keyterms_prompt omitted because no valid keyterms remained after sanitization"
        )

    _report_progress(on_progress, "Processing transcription...")
    async with session.post(
        f"{_ASSEMBLYAI_BASE_URL}/transcript",
        json=submit_payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as submit_resp:
        if submit_resp.status not in (200, 201):
            raw = await submit_resp.text()
            raise RuntimeError(f"AssemblyAI transcript submit failed ({submit_resp.status}): {raw[:500]}")
        submit_data = await submit_resp.json()

    transcript_id = str(submit_data.get("id") or "").strip()
    if not transcript_id:
        raise RuntimeError("AssemblyAI transcript submit response missing id")

    done_statuses = {"completed", "done", "succeeded", "success"}
    error_statuses = {"error", "failed", "canceled", "cancelled"}
    poll_start = asyncio.get_running_loop().time()
    poll_delay = 0.75

    while True:
        elapsed = asyncio.get_running_loop().time() - poll_start
        if elapsed > timeout_secs:
            raise TimeoutError("AssemblyAI transcription timed out")

        async with session.get(
            f"{_ASSEMBLYAI_BASE_URL}/transcript/{transcript_id}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as poll_resp:
            if poll_resp.status not in (200, 201):
                raw = await poll_resp.text()
                raise RuntimeError(
                    f"AssemblyAI transcript polling failed ({poll_resp.status}): {raw[:500]}"
                )
            poll_payload = await poll_resp.json()

        status = str(poll_payload.get("status") or "").lower()
        if status in done_statuses:
            _report_progress(on_progress, "Retrieving transcript...")
            return poll_payload if isinstance(poll_payload, dict) else {}
        if status in error_statuses:
            raise RuntimeError(
                f"AssemblyAI transcription failed: {poll_payload.get('error') or 'unknown error'}"
            )

        if elapsed >= 120:
            poll_delay = 5.0
        elif elapsed >= 30:
            poll_delay = 2.0
        else:
            poll_delay = 0.75
        await asyncio.sleep(poll_delay)


class AssemblyAIUniversal3ProAsyncProcessor(FrameProcessor):
    """Buffered async STT via AssemblyAI Universal-3-Pro pre-recorded API."""

    def __init__(
        self,
        *,
        api_key: str,
        language: str = "auto",
        custom_vocab: str = "",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        speaker_labels: bool = False,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._language = language or "auto"
        self._custom_vocab = custom_vocab or ""
        self._session = session
        self._on_progress = on_progress
        self._speaker_labels = bool(speaker_labels)
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
            return await transcribe_with_assemblyai_pre_recorded(
                session=session,
                api_key=self._api_key,
                audio_source=wav_bytes,
                language=self._language,
                custom_vocab=self._custom_vocab,
                speaker_labels=self._speaker_labels,
                on_progress=self._on_progress,
                timeout_secs=900.0,
            )

        if self._session:
            payload = await _call(self._session)
        else:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)

        return assemblyai_transcript_payload_to_text(
            payload,
            prefer_speaker_labels=self._speaker_labels,
        )

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
                logger.error(f"AssemblyAI async transcription failed: {exc}")
                await self.push_frame(ErrorFrame(error=f"assemblyai async error: {exc}"), direction)
            finally:
                self._reset_buffer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
