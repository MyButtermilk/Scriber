"""Direct async/batch STT adapters for cloud providers.

These adapters back Scriber's final-only live mode and file/YouTube direct
upload paths when Pipecat only provides the provider's realtime service.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
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
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from src.gladia_stt import (
    gladia_transcript_payload_to_text,
    transcribe_with_gladia_pre_recorded,
)


def provider_language_code(language: Language | str | None) -> str:
    if not language:
        return ""
    raw = str(language.value if isinstance(language, Language) else language).strip().lower()
    if not raw or raw == "auto":
        return ""
    return raw.replace("_", "-").split("-", 1)[0]


def _pcm_to_wav(audio_bytes: bytes, sample_rate: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with contextlib.closing(wave.open(buf, "wb")) as wf:
        wf.setnchannels(max(1, int(channels or 1)))
        wf.setsampwidth(2)
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


def _terms_from_vocab(custom_vocab: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in str(custom_vocab or "").replace("\n", ",").split(","):
        term = " ".join(raw.strip().split())
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def _deepgram_custom_vocab_param(model: str) -> str:
    normalized = str(model or "").strip().lower()
    return "keyterm" if normalized.startswith("nova-3") else "keywords"


def _format_speaker_segments(segments: list[dict[str, Any]]) -> str:
    speaker_map: dict[str, int] = {}
    next_speaker = 1
    blocks: list[tuple[int, list[str]]] = []

    for segment in segments:
        text = str(
            segment.get("text")
            or segment.get("transcript")
            or segment.get("word")
            or segment.get("punctuated_word")
            or ""
        ).strip()
        if not text:
            continue
        speaker_raw = None
        for speaker_key in ("speaker", "speaker_id", "speaker_label", "attendee"):
            value = segment.get(speaker_key)
            if value not in (None, ""):
                speaker_raw = value
                break
        if speaker_raw in (None, ""):
            if not blocks or blocks[-1][0] != 0:
                blocks.append((0, [text]))
            else:
                blocks[-1][1].append(text)
            continue

        speaker_key = str(speaker_raw).strip()
        speaker_num = speaker_map.get(speaker_key)
        if speaker_num is None:
            speaker_num = next_speaker
            speaker_map[speaker_key] = speaker_num
            next_speaker += 1
        if not blocks or blocks[-1][0] != speaker_num:
            blocks.append((speaker_num, [text]))
        else:
            blocks[-1][1].append(text)

    lines: list[str] = []
    for speaker_num, parts in blocks:
        text = " ".join(part.strip() for part in parts if part.strip()).strip()
        if not text:
            continue
        lines.append(text if speaker_num == 0 else f"[Speaker {speaker_num}]: {text}")
    return "\n\n".join(lines).strip()


def deepgram_transcript_payload_to_text(
    payload: dict[str, Any],
    *,
    prefer_speaker_labels: bool,
) -> str:
    results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    channels = results.get("channels") if isinstance(results.get("channels"), list) else []
    if not channels:
        return ""
    alternatives = channels[0].get("alternatives") if isinstance(channels[0], dict) else []
    if not alternatives:
        return ""
    alternative = alternatives[0] if isinstance(alternatives[0], dict) else {}
    words = alternative.get("words") if isinstance(alternative.get("words"), list) else []
    if prefer_speaker_labels and words and any("speaker" in word for word in words if isinstance(word, dict)):
        formatted = _format_speaker_segments([word for word in words if isinstance(word, dict)])
        if formatted:
            return formatted
    return str(alternative.get("transcript") or "").strip()


async def transcribe_with_deepgram_pre_recorded(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    language: Language | str | None,
    custom_vocab: str = "",
    diarize: bool = True,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
) -> dict[str, Any]:
    _report_progress(on_progress, "Uploading audio...")
    _report_progress(on_progress, "Processing transcription...")

    model = os.getenv("SCRIBER_DEEPGRAM_MODEL", "nova-3")
    params: list[tuple[str, str]] = [
        ("model", model),
        ("smart_format", "true"),
        ("punctuate", "true"),
        ("diarize", "true" if diarize else "false"),
    ]
    language_code = provider_language_code(language)
    if language_code:
        params.append(("language", language_code))
    vocab_param = _deepgram_custom_vocab_param(model)
    for term in _terms_from_vocab(custom_vocab)[:100]:
        params.append((vocab_param, term))

    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": content_type,
    }
    async with session.post(
        "https://api.deepgram.com/v1/listen",
        params=params,
        data=audio_source,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout_secs),
    ) as response:
        raw = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Deepgram transcription failed ({response.status}): {raw[:500]}")
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}


def openai_transcript_payload_to_text(
    payload: dict[str, Any],
    *,
    prefer_speaker_labels: bool,
) -> str:
    for key in ("segments", "words"):
        segments = payload.get(key)
        segment_list = [segment for segment in segments if isinstance(segment, dict)] if isinstance(segments, list) else []
        if prefer_speaker_labels and segment_list:
            formatted = _format_speaker_segments(segment_list)
            if formatted:
                return formatted
    return str(payload.get("text") or "").strip()


def gemini_transcript_payload_to_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    if not candidates:
        return ""
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") if isinstance(first.get("content"), dict) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    return "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ).strip()


async def _delete_gemini_file(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    file_name: str,
) -> None:
    if not file_name:
        return
    try:
        async with session.delete(
            f"https://generativelanguage.googleapis.com/v1beta/{file_name}",
            params={"key": api_key},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            if response.status >= 400:
                logger.debug("Gemini file cleanup returned status {}", response.status)
    except Exception as exc:
        logger.debug("Gemini file cleanup failed: {}", exc)


async def _upload_gemini_file(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_bytes: bytes,
    filename: str,
    content_type: str,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(audio_bytes)),
        "X-Goog-Upload-Header-Content-Type": content_type,
    }
    start_payload = {"file": {"displayName": filename or "scriber-audio"}}
    async with session.post(
        "https://generativelanguage.googleapis.com/upload/v1beta/files",
        params={"key": api_key},
        json=start_payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as response:
        raw = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Gemini file upload start failed ({response.status}): {raw[:500]}")
        upload_url = response.headers.get("X-Goog-Upload-URL", "")
        if not upload_url:
            raise RuntimeError("Gemini file upload did not return an upload URL.")

    async with session.post(
        upload_url,
        data=audio_bytes,
        headers={
            "Content-Length": str(len(audio_bytes)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        timeout=aiohttp.ClientTimeout(total=900),
    ) as response:
        raw = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Gemini file upload failed ({response.status}): {raw[:500]}")
        payload = json.loads(raw) if raw else {}

    file_info = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    if not file_info.get("uri"):
        raise RuntimeError(f"Gemini file upload response did not include a file URI: {payload}")
    return file_info


async def transcribe_with_gemini_audio(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    language: Language | str | None,
    custom_vocab: str = "",
    diarize: bool = False,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
) -> dict[str, Any]:
    audio_bytes = audio_source if isinstance(audio_source, bytes) else audio_source.read()
    if not audio_bytes:
        return {}

    model = os.getenv("SCRIBER_GEMINI_STT_MODEL", "gemini-2.5-flash")
    inline_limit_mb = float(os.getenv("SCRIBER_GEMINI_STT_INLINE_LIMIT_MB", "18") or 18)
    inline_limit_bytes = max(1, int(inline_limit_mb * 1024 * 1024))
    mime_type = content_type or "audio/wav"
    terms = _terms_from_vocab(custom_vocab)[:100]
    language_code = provider_language_code(language)
    language_hint = f" The expected spoken language is {language_code}." if language_code else ""
    vocab_hint = f" Prefer these domain terms when audible: {', '.join(terms)}." if terms else ""
    speaker_hint = (
        " If multiple speakers are clearly present, keep the order and label turns as [Speaker 1]:, [Speaker 2]:."
        if diarize
        else " Do not add speaker labels for single-speaker dictation."
    )
    prompt = (
        "Transcribe the attached audio exactly and output only the transcript. "
        "Preserve meaning, names, numbers, punctuation, and paragraph breaks. "
        "Do not summarize, translate, explain, or add commentary."
        f"{language_hint}{vocab_hint}{speaker_hint}"
    )
    generation_config = {
        "temperature": 0,
        "maxOutputTokens": int(os.getenv("SCRIBER_GEMINI_STT_MAX_OUTPUT_TOKENS", "16384") or 16384),
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    params = {"key": api_key}

    file_name = ""
    try:
        _report_progress(on_progress, "Uploading audio...")
        if len(audio_bytes) <= inline_limit_bytes:
            audio_part = {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": base64.b64encode(audio_bytes).decode("ascii"),
                }
            }
        else:
            file_info = await _upload_gemini_file(
                session=session,
                api_key=api_key,
                audio_bytes=audio_bytes,
                filename=filename,
                content_type=mime_type,
            )
            file_name = str(file_info.get("name") or "")
            audio_part = {
                "fileData": {
                    "mimeType": str(file_info.get("mimeType") or mime_type),
                    "fileUri": str(file_info.get("uri") or ""),
                }
            }

        _report_progress(on_progress, "Processing transcription...")
        payload = {
            "contents": [{"parts": [{"text": prompt}, audio_part]}],
            "generationConfig": generation_config,
        }
        async with session.post(
            url,
            params=params,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_secs),
        ) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Gemini transcription failed ({response.status}): {raw[:500]}")
            parsed = json.loads(raw) if raw else {}
            return parsed if isinstance(parsed, dict) else {}
    finally:
        if file_name:
            await _delete_gemini_file(session=session, api_key=api_key, file_name=file_name)


async def transcribe_with_openai_audio_transcription(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    model: str,
    language: Language | str | None,
    custom_vocab: str = "",
    diarize: bool = False,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
) -> dict[str, Any]:
    _report_progress(on_progress, "Uploading audio...")
    _report_progress(on_progress, "Processing transcription...")

    form = aiohttp.FormData()
    form.add_field("file", audio_source, filename=filename, content_type=content_type)
    form.add_field("model", model)
    response_format = "diarized_json" if diarize and "diarize" in model.lower() else "json"
    form.add_field("response_format", response_format)
    language_code = provider_language_code(language)
    if language_code:
        form.add_field("language", language_code)
    prompt = ", ".join(_terms_from_vocab(custom_vocab)[:200])
    if prompt:
        form.add_field("prompt", prompt)

    async with session.post(
        "https://api.openai.com/v1/audio/transcriptions",
        data=form,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=aiohttp.ClientTimeout(total=timeout_secs),
    ) as response:
        raw = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"OpenAI transcription failed ({response.status}): {raw[:500]}")
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"text": raw}


def speechmatics_transcript_payload_to_text(
    payload: dict[str, Any],
    *,
    prefer_speaker_labels: bool,
) -> str:
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    if prefer_speaker_labels and results:
        segments: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            alternatives = item.get("alternatives") if isinstance(item.get("alternatives"), list) else []
            if not alternatives:
                continue
            alternative = alternatives[0] if isinstance(alternatives[0], dict) else {}
            content = str(alternative.get("content") or "").strip()
            if not content:
                continue
            segments.append(
                {
                    "text": content,
                    "speaker": alternative.get("speaker") or item.get("speaker"),
                }
            )
        formatted = _format_speaker_segments(segments)
        if formatted:
            return formatted

    if isinstance(payload.get("transcript"), str):
        return str(payload.get("transcript") or "").strip()

    words: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        result_type = str(item.get("type") or "").lower()
        if result_type not in {"word", "punctuation"}:
            continue
        alternatives = item.get("alternatives") if isinstance(item.get("alternatives"), list) else []
        if not alternatives:
            continue
        content = str(alternatives[0].get("content") or "").strip()
        if not content:
            continue
        if result_type == "punctuation" and words:
            words[-1] += content
        else:
            words.append(content)
    return " ".join(words).strip()


async def transcribe_with_speechmatics_batch(
    *,
    session: aiohttp.ClientSession,
    api_key: str,
    audio_source: bytes | BinaryIO,
    filename: str,
    content_type: str,
    language: Language | str | None,
    custom_vocab: str = "",
    diarize: bool = True,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 900.0,
    poll_interval_secs: float = 1.0,
) -> dict[str, Any]:
    base_url = os.getenv("SCRIBER_SPEECHMATICS_BATCH_BASE_URL", "https://asr.api.speechmatics.com/v2").rstrip("/")
    language_code = provider_language_code(language) or os.getenv("SCRIBER_SPEECHMATICS_DEFAULT_LANGUAGE", "en")
    transcription_config: dict[str, Any] = {
        "language": language_code,
        "operating_point": "enhanced",
    }
    terms = _terms_from_vocab(custom_vocab)[:1000]
    if terms:
        transcription_config["additional_vocab"] = [{"content": term} for term in terms]
    if diarize:
        transcription_config["diarization"] = "speaker"
    config = {"type": "transcription", "transcription_config": transcription_config}

    form = aiohttp.FormData()
    form.add_field("config", json.dumps(config), content_type="application/json")
    form.add_field("data_file", audio_source, filename=filename, content_type=content_type)

    _report_progress(on_progress, "Uploading audio...")
    async with session.post(
        f"{base_url}/jobs/",
        data=form,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=aiohttp.ClientTimeout(total=min(timeout_secs, 300.0)),
    ) as response:
        raw = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Speechmatics batch job start failed ({response.status}): {raw[:500]}")
        start_payload = json.loads(raw) if raw else {}

    job_id = str(
        start_payload.get("id")
        or (start_payload.get("job") or {}).get("id")
        or ""
    ).strip()
    if not job_id:
        raise RuntimeError(f"Speechmatics batch response did not include a job id: {start_payload}")

    _report_progress(on_progress, "Processing transcription...")
    started_at = asyncio.get_running_loop().time()
    while True:
        if asyncio.get_running_loop().time() - started_at > timeout_secs:
            raise TimeoutError("Speechmatics batch transcription timed out")
        async with session.get(
            f"{base_url}/jobs/{job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"Speechmatics batch status failed ({response.status}): {raw[:500]}")
            status_payload = json.loads(raw) if raw else {}
        job_payload = status_payload.get("job") if isinstance(status_payload.get("job"), dict) else status_payload
        status = str(job_payload.get("status") or "").strip().lower()
        if status in {"done", "completed", "success", "succeeded"}:
            break
        if status in {"rejected", "failed", "error"}:
            raise RuntimeError(f"Speechmatics batch transcription failed: {status_payload}")
        await asyncio.sleep(max(0.25, poll_interval_secs))

    _report_progress(on_progress, "Retrieving transcript...")
    async with session.get(
        f"{base_url}/jobs/{job_id}/transcript",
        params={"format": "json-v2"},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=aiohttp.ClientTimeout(total=60),
    ) as response:
        raw = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"Speechmatics batch transcript failed ({response.status}): {raw[:500]}")
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}


class _BufferedAsyncProcessor(FrameProcessor):
    provider_name = "cloud"

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None,
        on_progress: Callable[[str], None] | None,
        diarize: bool,
    ) -> None:
        super().__init__()
        self._session = session
        self._on_progress = on_progress
        self._diarize = bool(diarize)
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
        raise NotImplementedError

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
                if getattr(self, "_skip_terminal_transcription", False):
                    logger.info(f"{self.provider_name} async: skipping terminal transcription for silent recording")
                    self._reset_buffer()
                    await self.push_frame(frame, direction)
                    return
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
                logger.error(f"{self.provider_name} async transcription failed: {exc}")
                await self.push_frame(ErrorFrame(error=f"{self.provider_name} async error: {exc}"), direction)
            finally:
                self._reset_buffer()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


class DeepgramAsyncProcessor(_BufferedAsyncProcessor):
    provider_name = "Deepgram"

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str | None,
        custom_vocab: str = "",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        diarize: bool = False,
    ) -> None:
        super().__init__(session=session, on_progress=on_progress, diarize=diarize)
        self._api_key = api_key
        self._language = language
        self._custom_vocab = custom_vocab

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_bytes = _pcm_to_wav(audio_bytes, self._sample_rate, self._channels)

        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_deepgram_pre_recorded(
                session=session,
                api_key=self._api_key,
                audio_source=wav_bytes,
                filename="audio.wav",
                content_type="audio/wav",
                language=self._language,
                custom_vocab=self._custom_vocab,
                diarize=self._diarize,
                on_progress=self._on_progress,
            )

        payload = await _call(self._session) if self._session else None
        if payload is None:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return deepgram_transcript_payload_to_text(payload, prefer_speaker_labels=self._diarize)


class OpenAIAsyncProcessor(_BufferedAsyncProcessor):
    provider_name = "OpenAI"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        language: Language | str | None,
        custom_vocab: str = "",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        diarize: bool = False,
    ) -> None:
        super().__init__(session=session, on_progress=on_progress, diarize=diarize)
        self._api_key = api_key
        self._model = model
        self._language = language
        self._custom_vocab = custom_vocab

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_bytes = _pcm_to_wav(audio_bytes, self._sample_rate, self._channels)

        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_openai_audio_transcription(
                session=session,
                api_key=self._api_key,
                audio_source=wav_bytes,
                filename="audio.wav",
                content_type="audio/wav",
                model=self._model,
                language=self._language,
                custom_vocab=self._custom_vocab,
                diarize=self._diarize,
                on_progress=self._on_progress,
            )

        payload = await _call(self._session) if self._session else None
        if payload is None:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return openai_transcript_payload_to_text(payload, prefer_speaker_labels=self._diarize)


class GeminiAsyncProcessor(_BufferedAsyncProcessor):
    provider_name = "Gemini"

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str | None,
        custom_vocab: str = "",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        diarize: bool = False,
    ) -> None:
        super().__init__(session=session, on_progress=on_progress, diarize=diarize)
        self._api_key = api_key
        self._language = language
        self._custom_vocab = custom_vocab

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_bytes = _pcm_to_wav(audio_bytes, self._sample_rate, self._channels)

        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_gemini_audio(
                session=session,
                api_key=self._api_key,
                audio_source=wav_bytes,
                filename="audio.wav",
                content_type="audio/wav",
                language=self._language,
                custom_vocab=self._custom_vocab,
                diarize=self._diarize,
                on_progress=self._on_progress,
            )

        payload = await _call(self._session) if self._session else None
        if payload is None:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return gemini_transcript_payload_to_text(payload)


class GladiaAsyncProcessor(_BufferedAsyncProcessor):
    provider_name = "Gladia"

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str | None,
        custom_vocab: str = "",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        diarize: bool = False,
    ) -> None:
        super().__init__(session=session, on_progress=on_progress, diarize=diarize)
        self._api_key = api_key
        self._language = language
        self._custom_vocab = custom_vocab

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_bytes = _pcm_to_wav(audio_bytes, self._sample_rate, self._channels)

        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_gladia_pre_recorded(
                session=session,
                api_key=self._api_key,
                audio_source=wav_bytes,
                filename="audio.wav",
                content_type="audio/wav",
                language=self._language,
                custom_vocab=self._custom_vocab,
                diarize=self._diarize,
                on_progress=self._on_progress,
            )

        payload = await _call(self._session) if self._session else None
        if payload is None:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return gladia_transcript_payload_to_text(payload, prefer_speaker_labels=self._diarize)


class SpeechmaticsAsyncProcessor(_BufferedAsyncProcessor):
    provider_name = "Speechmatics"

    def __init__(
        self,
        *,
        api_key: str,
        language: Language | str | None,
        custom_vocab: str = "",
        session: aiohttp.ClientSession | None = None,
        on_progress: Callable[[str], None] | None = None,
        diarize: bool = False,
    ) -> None:
        super().__init__(session=session, on_progress=on_progress, diarize=diarize)
        self._api_key = api_key
        self._language = language
        self._custom_vocab = custom_vocab

    async def _transcribe_bytes(self, audio_bytes: bytes) -> str:
        wav_bytes = _pcm_to_wav(audio_bytes, self._sample_rate, self._channels)

        async def _call(session: aiohttp.ClientSession) -> dict[str, Any]:
            return await transcribe_with_speechmatics_batch(
                session=session,
                api_key=self._api_key,
                audio_source=wav_bytes,
                filename="audio.wav",
                content_type="audio/wav",
                language=self._language,
                custom_vocab=self._custom_vocab,
                diarize=self._diarize,
                on_progress=self._on_progress,
            )

        payload = await _call(self._session) if self._session else None
        if payload is None:
            async with aiohttp.ClientSession() as session:
                payload = await _call(session)
        return speechmatics_transcript_payload_to_text(payload, prefer_speaker_labels=self._diarize)
