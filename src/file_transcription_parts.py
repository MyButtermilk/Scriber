"""Sequential, restart-safe provider work for a bounded MP3 part manifest."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.provider_errors import ProviderTransportError, provider_transport_error

if TYPE_CHECKING:
    from src.data.transcription_part_store import DurableTranscriptionCheckpoint
    from src.openrouter_audio import OpenRouterAudioPart


def _validate_result(provider: str, payload: Any) -> None:
    """An explicitly empty transcript is silence; a missing result is not."""
    valid = isinstance(payload, dict) and isinstance(payload.get("text"), str)
    if provider == "azure_mai" and isinstance(payload, dict):
        valid |= any(isinstance(payload.get(alias), str) for alias in ("displayText", "transcription", "transcript"))
        valid |= any(
            isinstance(payload.get(key), list)
            and all(
                isinstance(item, dict)
                and any(isinstance(item.get(alias), str) for alias in ("text", "displayText", "display", "lexical"))
                for item in payload[key]
            )
            for key in ("combinedPhrases", "phrases", "recognizedPhrases")
        )
    if not valid:
        raise provider_transport_error(provider, "transcription_response", code="invalid_response", retryable=False)


async def transcribe_mp3_parts(
    *,
    source: Path,
    provider: str,
    max_audio_bytes: int,
    request_shape: dict[str, Any],
    transcribe: Callable[[OpenRouterAudioPart], Awaitable[dict[str, Any]]],
    max_duration_ms: int | None = None,
    target_audio_bytes: int | None = None,
    checkpoint: DurableTranscriptionCheckpoint | None = None,
    request_start_managed: bool = False,
    timeout_secs: float = 900.0,
) -> dict[str, Any]:
    """Persist each paid result before advancing; never publish partial success.

    Persisted in-flight requests are deliberately not retried: synchronous STT
    has no documented idempotent operation for reconciling a lost response.
    """
    from src.openrouter_audio import AudioPartsManifest, mp3_audio_parts, plan_mp3_audio_parts
    from src.transcription_merge import merge_part_transcripts, part_transcript_from_payload

    async with asyncio.timeout(timeout_secs):
        saved = await checkpoint.load_manifest(request_shape=request_shape) if checkpoint else None
        manifest = (
            AudioPartsManifest.from_dict(saved)
            if saved is not None
            else await plan_mp3_audio_parts(
                source,
                max_audio_bytes=max_audio_bytes,
                max_duration_ms=max_duration_ms,
                target_audio_bytes=target_audio_bytes,
            )
        )
        if checkpoint:
            manifest = AudioPartsManifest.from_dict(
                await checkpoint.bind(manifest=manifest.to_dict(), request_shape=request_shape)
            )
        transcripts = []
        usage: dict[str, float] = {}
        fallback = False
        async with mp3_audio_parts(
            source,
            max_audio_bytes=max_audio_bytes,
            max_duration_ms=max_duration_ms,
            manifest=manifest,
        ) as parts:
            async for part in parts:
                payload = await checkpoint.lookup(part.index) if checkpoint else None
                if payload is None:
                    if checkpoint and not request_start_managed:
                        await checkpoint.mark_started(part.index)
                    try:
                        payload = await transcribe(part)
                        _validate_result(provider, payload)
                    except ProviderTransportError as exc:
                        # A 5xx may hide completed upstream work; only explicit
                        # request rejection can make this part safely resumable.
                        if checkpoint and exc.status in {400, 401, 402, 403, 404, 413, 415, 422, 429}:
                            await checkpoint.mark_rejected(part.index, exc.status)
                        raise
                    if checkpoint:
                        await checkpoint.save_success(part.index, payload)
                else:
                    _validate_result(provider, payload)
                if part.count == 1:
                    return {**payload, "_scriberChunkCount": 1}
                transcripts.append(
                    part_transcript_from_payload(
                        provider,
                        index=part.index,
                        start_ms=part.start_ms,
                        end_ms=part.end_ms,
                        core_start_ms=part.core_start_ms,
                        core_end_ms=part.core_end_ms,
                        payload=payload,
                    )
                )
                fallback |= payload.get("_scriberDiarizationFallback") == "diarization_unavailable"
                part_usage = payload.get("usage")
                if isinstance(part_usage, dict):
                    for key, value in part_usage.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            usage[key] = usage.get(key, 0) + value
        result = merge_part_transcripts(transcripts).to_dict()
        # Word precision is provider-reported; MP3 packet-copy rebasing has a
        # separately measured frame/encoder-delay tolerance on the source clock.
        result["_scriberMerged"]["audioBoundaryToleranceMs"] = manifest.timestamp_tolerance_ms
        result.update(usage=usage, _scriberChunkCount=len(transcripts))
        if fallback:
            result["_scriberDiarizationFallback"] = "diarization_unavailable"
        return result
