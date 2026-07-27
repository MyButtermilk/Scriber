"""Gladia pre-recorded transcription helpers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, BinaryIO

import aiohttp
from loguru import logger
from pipecat.transcriptions.language import Language

from src.core.provider_errors import provider_transport_error
from src.runtime.http_response import read_response_text_limited

_GLADIA_BASE_URL = "https://api.gladia.io/v2"


def _report_progress(on_progress: Callable[[str], None] | None, message: str) -> None:
    if not on_progress:
        return
    try:
        on_progress(message)
    except Exception:
        pass


def gladia_language_code(language: Language | str | None) -> str:
    if not language:
        return ""
    raw = str(language.value if isinstance(language, Language) else language).strip().lower()
    if not raw or raw == "auto":
        return ""
    return raw.replace("_", "-").split("-", 1)[0]


def _gladia_headers(api_key: str) -> dict[str, str]:
    return {"x-gladia-key": api_key}


def _parse_gladia_object(raw: str, operation: str) -> dict[str, Any]:
    if not raw:
        return {}
    invalid_json = False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = None
        invalid_json = True
    if invalid_json:
        error = provider_transport_error(
            "gladia",
            operation,
            response_body=raw,
        )
        raw = ""
        raise error
    if not isinstance(payload, dict):
        error = provider_transport_error(
            "gladia",
            operation,
            response_body=raw,
        )
        raw = ""
        raise error
    return payload


def _build_pre_recorded_payload(
    *,
    audio_url: str,
    language: Language | str | None,
    custom_vocab: str,
    diarize: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "audio_url": audio_url,
        "diarization": bool(diarize),
        "subtitles": False,
        "callback": False,
    }

    language_code = gladia_language_code(language)
    if language_code:
        payload["language_config"] = {
            "languages": [language_code],
            "code_switching": False,
        }

    terms = [" ".join(term.strip().split()) for term in str(custom_vocab or "").replace("\n", ",").split(",")]
    terms = [term for term in terms if term]
    if terms:
        payload["custom_vocabulary"] = True
        payload["custom_vocabulary_config"] = {"vocabulary": terms[:1000]}

    return payload


def format_gladia_utterances_to_scriber_text(utterances: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    speaker_map: dict[str, int] = {}
    next_index = 1
    for utterance in utterances:
        text = str(utterance.get("text") or "").strip()
        if not text:
            continue
        speaker = utterance.get("speaker")
        if speaker in (None, ""):
            lines.append(text)
        else:
            speaker_key = str(speaker)
            speaker_num = speaker_map.get(speaker_key)
            if speaker_num is None:
                speaker_num = next_index
                speaker_map[speaker_key] = speaker_num
                next_index += 1
            lines.append(f"[Speaker {speaker_num}]: {text}")
    return "\n\n".join(lines).strip()


def gladia_transcript_payload_to_text(
    payload: dict[str, Any],
    *,
    prefer_speaker_labels: bool,
) -> str:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    transcription = result.get("transcription") if isinstance(result.get("transcription"), dict) else {}
    utterances = transcription.get("utterances")
    utterance_list = [u for u in utterances if isinstance(u, dict)] if isinstance(utterances, list) else []

    if prefer_speaker_labels and utterance_list:
        formatted = format_gladia_utterances_to_scriber_text(utterance_list)
        if formatted:
            return formatted

    text = str(transcription.get("full_transcript") or "").strip()
    if text:
        return text

    if utterance_list:
        return format_gladia_utterances_to_scriber_text(utterance_list)
    return ""


async def transcribe_with_gladia_pre_recorded(
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
    """Transcribe complete audio with Gladia's pre-recorded REST API."""
    headers = _gladia_headers(api_key)
    _report_progress(on_progress, "Uploading audio...")

    form = aiohttp.FormData()
    form.add_field(
        "audio",
        audio_source,
        filename=filename,
        content_type=content_type,
    )
    async with session.post(
        f"{_GLADIA_BASE_URL}/upload",
        data=form,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=min(timeout_secs, 300.0)),
    ) as response:
        raw = await read_response_text_limited(response, 64 * 1024 * 1024)
        if response.status >= 400:
            raise provider_transport_error(
                "gladia",
                "upload",
                status=response.status,
                response_body=raw,
            )
        upload_payload = _parse_gladia_object(raw, "upload response")

    audio_url = str(upload_payload.get("audio_url") or "").strip()
    if not audio_url:
        raise provider_transport_error("gladia", "upload response")

    _report_progress(on_progress, "Processing transcription...")
    submit_payload = _build_pre_recorded_payload(
        audio_url=audio_url,
        language=language,
        custom_vocab=custom_vocab,
        diarize=diarize,
    )
    async with session.post(
        f"{_GLADIA_BASE_URL}/pre-recorded",
        json=submit_payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=60),
    ) as response:
        raw = await read_response_text_limited(response, 64 * 1024 * 1024)
        if response.status >= 400:
            raise provider_transport_error(
                "gladia",
                "transcription start",
                status=response.status,
                response_body=raw,
            )
        start_payload = _parse_gladia_object(raw, "transcription start response")

    job_id = str(start_payload.get("id") or "").strip()
    if not job_id:
        raise provider_transport_error("gladia", "transcription start response")

    done_statuses = {"done", "completed", "success", "succeeded"}
    pending_statuses = {"queued", "processing", "running", "pending", "pre-recorded"}
    started_at = asyncio.get_running_loop().time()
    try:
        while True:
            if asyncio.get_running_loop().time() - started_at > timeout_secs:
                raise TimeoutError("Gladia transcription timed out")

            async with session.get(
                f"{_GLADIA_BASE_URL}/pre-recorded/{job_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                raw = await read_response_text_limited(response, 64 * 1024 * 1024)
                if response.status >= 400:
                    raise provider_transport_error(
                        "gladia",
                        "transcription status",
                        status=response.status,
                        response_body=raw,
                    )
                status_payload = _parse_gladia_object(raw, "transcription status response")

            status = str(status_payload.get("status") or "").strip().lower()
            error_code = status_payload.get("error_code")
            if error_code not in (None, "", 0):
                raise provider_transport_error(
                    "gladia",
                    "transcription",
                    code=str(error_code),
                )
            if status in done_statuses or (status_payload.get("completed_at") and status_payload.get("result")):
                _report_progress(on_progress, "Retrieving transcript...")
                return status_payload
            if status and status not in pending_statuses:
                raise provider_transport_error(
                    "gladia",
                    "transcription",
                    code=status,
                )

            await asyncio.sleep(max(0.25, poll_interval_secs))
    finally:
        try:
            async with session.delete(
                f"{_GLADIA_BASE_URL}/pre-recorded/{job_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status not in (200, 202, 204, 404):
                    logger.debug(
                        "Gladia provider-side cleanup returned status {}",
                        response.status,
                    )
        except Exception as exc:
            logger.debug(
                "Gladia provider-side cleanup failed (error_type={})",
                type(exc).__name__,
            )
