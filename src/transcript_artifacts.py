"""Shared route freezing and normalization for durable transcript artifacts.

Provider APIs intentionally remain in :mod:`src.pipeline`.  This module is the
small deterministic boundary between provider/caption output and the durable
artifact store used by File, YouTube, and Meetings.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from src.config import Config
from src.data.transcript_artifact_store import (
    AlignmentQuality,
    CanonicalSegmentDraft,
    RouteSnapshotDraft,
    StageUnit,
)
from src.provider_transcript import has_speaker_evidence, normalize_provider_segments
from src.youtube_download import YouTubeCaptionCue


PARSER_ID = "scriber-provider-transcript"
PARSER_VERSION = "1"
CAPTION_PARSER_ID = "youtube-caption-cues"
CAPTION_PARSER_VERSION = "1"


@dataclass(frozen=True)
class FrozenTranscriptionRoute:
    workload: str
    source_track: str
    provider: str
    model: str
    transport: str
    language: str
    response_shape: str
    timestamp_mode: str
    diarization_mode: str
    parser_id: str
    parser_version: str
    custom_vocab: str = ""
    local_worker_manifest: Mapping[str, Any] | None = None

    def execution_route(self) -> dict[str, Any]:
        """Private in-memory values consumed by one concrete pipeline run."""
        return {
            "model": self.model,
            "language": self.language,
            "custom_vocab": self.custom_vocab,
            "transport": self.transport,
        }

    def snapshot_draft(self) -> RouteSnapshotDraft:
        terms = [item.strip() for item in self.custom_vocab.split(",") if item.strip()]
        options: dict[str, Any] = {
            "speakerDiarizationRequested": self.diarization_mode != "disabled",
            "customVocabularyPresent": bool(terms),
            "customVocabularyCount": len(terms),
        }
        if terms:
            options["customVocabularySha256"] = hashlib.sha256(
                self.custom_vocab.encode("utf-8")
            ).hexdigest()
        return RouteSnapshotDraft(
            workload=self.workload,
            source_track=self.source_track,
            provider=self.provider,
            model=self.model,
            transport=self.transport,
            language=self.language or "auto",
            response_shape=self.response_shape,
            timestamp_mode=self.timestamp_mode,
            diarization_mode=self.diarization_mode,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            request_options=options,
            local_worker_manifest=dict(self.local_worker_manifest or {}),
        )


def provider_batch_model(provider: str) -> str:
    key = str(provider or "").strip().lower()
    configured = {
        "soniox": Config.SONIOX_ASYNC_MODEL,
        "soniox_async": Config.SONIOX_ASYNC_MODEL,
        "assemblyai": Config.ASSEMBLYAI_ASYNC_MODEL,
        "mistral": Config.MISTRAL_ASYNC_MODEL,
        "mistral_async": Config.MISTRAL_ASYNC_MODEL,
        "openai_async": Config.OPENAI_STT_MODEL,
        "onnx_local": Config.ONNX_MODEL,
        "deepgram_async": Config.DEEPGRAM_MODEL,
        "gladia": "pre-recorded-v2",
        "gladia_async": "pre-recorded-v2",
        "speechmatics_async": "batch-v2",
        "smallest": "pulse",
        "smallest_async": "pulse",
        "azure_mai": getattr(Config, "AZURE_MAI_MODEL", "mai-transcribe-1.5"),
        "gemini_stt": Config.GEMINI_STT_MODEL,
    }
    return str(configured.get(key) or key or "unknown")


def freeze_provider_route(
    *,
    workload: str,
    provider: str,
    source_track: str = "mix",
    language: str | None = None,
    custom_vocab: str | None = None,
    diarization_requested: bool = True,
    local_worker_manifest: Mapping[str, Any] | None = None,
    transport: str | None = None,
) -> FrozenTranscriptionRoute:
    key = str(provider or "").strip().lower()
    direct = key in {
        "soniox", "soniox_async", "assemblyai", "mistral", "mistral_async",
        "smallest", "smallest_async", "deepgram_async", "openai_async",
        "gemini_stt", "azure_mai", "gladia", "gladia_async", "speechmatics_async",
    }
    return FrozenTranscriptionRoute(
        workload=workload,
        source_track=source_track,
        provider=key,
        model=provider_batch_model(key),
        transport=str(transport or ("direct_upload" if direct else "decoded_pcm")),
        language=str(Config.LANGUAGE if language is None else language) or "auto",
        response_shape="provider_segments_or_words",
        timestamp_mode="word_or_segment",
        diarization_mode=(
            "native_if_evidenced_else_local" if diarization_requested else "disabled"
        ),
        parser_id=PARSER_ID,
        parser_version=PARSER_VERSION,
        custom_vocab=str(Config.CUSTOM_VOCAB if custom_vocab is None else custom_vocab),
        local_worker_manifest=local_worker_manifest,
    )


def freeze_caption_route(
    *, workload: str, language: str, automatic: bool
) -> FrozenTranscriptionRoute:
    return FrozenTranscriptionRoute(
        workload=workload,
        source_track="captions",
        provider="youtube_captions_auto" if automatic else "youtube_captions",
        model="youtube-json3-vtt",
        transport="caption_track",
        language=language or "auto",
        response_shape="timed_caption_cues",
        timestamp_mode="segment",
        diarization_mode="disabled",
        parser_id=CAPTION_PARSER_ID,
        parser_version=CAPTION_PARSER_VERSION,
    )


def duration_label_to_ms(value: str, *, fallback_ms: int = 1) -> int:
    parts = str(value or "").strip().split(":")
    if not parts or any(not re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts):
        return max(1, int(fallback_ms))
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    except ValueError:
        return max(1, int(fallback_ms))
    return max(1, round(seconds * 1000))


def _estimated_units(text: str, *, duration_ms: int, source_track: str) -> tuple[StageUnit, ...]:
    blocks = [item.strip() for item in re.split(r"\n\s*\n|(?<=[.!?])\s+", text) if item.strip()]
    if not blocks:
        return ()
    weights = [max(1, len(re.findall(r"\w+", block))) for block in blocks]
    total = sum(weights)
    cursor = 0
    units: list[StageUnit] = []
    for index, (block, weight) in enumerate(zip(blocks, weights)):
        end = duration_ms if index == len(blocks) - 1 else round(duration_ms * sum(weights[: index + 1]) / total)
        end = max(cursor + 1, end)
        units.append(
            StageUnit(
                source_track=source_track,
                start_ms=cursor,
                end_ms=end,
                text=block,
                timing_origin="duration_weighted_text",
                speaker_origin="none",
                alignment_quality=AlignmentQuality.ESTIMATED,
            )
        )
        cursor = end
    return tuple(units)


def stage_units_from_provider(
    *,
    provider: str,
    payload: Any,
    text: str,
    duration_ms: int,
    source_track: str = "mix",
    origin_ms: int = 0,
) -> tuple[tuple[StageUnit, ...], dict[str, Any]]:
    normalized = normalize_provider_segments(provider, payload, source_track, origin_ms)
    units: list[StageUnit] = []
    for segment in normalized:
        label = str(segment.get("speakerLabel") or "").strip()
        key = segment.get("speakerKey")
        has_speaker = bool(label and label not in {"Meeting audio", "You"})
        units.append(
            StageUnit(
                source_track=source_track,
                start_ms=int(segment["startMs"]),
                end_ms=int(segment["endMs"]),
                text=str(segment.get("text") or "").strip(),
                speaker_key=key if has_speaker else None,
                speaker_label=label if has_speaker else "",
                timing_origin="provider",
                speaker_origin="provider_native" if has_speaker else "none",
                alignment_quality=str(segment.get("alignmentQuality") or "provider_segment"),
                provider_native_id=str(segment.get("providerSegmentId") or ""),
            )
        )
    if not units:
        units = [
            StageUnit(
                **{
                    **unit.__dict__,
                    "start_ms": unit.start_ms + max(0, int(origin_ms)),
                    "end_ms": unit.end_ms + max(0, int(origin_ms)),
                }
            )
            for unit in _estimated_units(
                text, duration_ms=duration_ms, source_track=source_track
            )
        ]
    native_speakers = has_speaker_evidence(normalized)
    exact_count = sum(
        1 for unit in units if str(getattr(unit.alignment_quality, "value", unit.alignment_quality)) == "exact_word"
    )
    evidence = {
        "normalizedIntervalCount": len(units),
        "nativeSpeakerIntervals": sum(1 for unit in units if unit.speaker_origin == "provider_native"),
        "nativeSpeakerEvidence": native_speakers,
        "exactWordIntervalCount": exact_count,
        "estimatedTiming": all(
            str(getattr(unit.alignment_quality, "value", unit.alignment_quality)) == "estimated"
            for unit in units
        ),
    }
    return tuple(units), evidence


def stage_units_from_captions(
    cues: Sequence[YouTubeCaptionCue],
) -> tuple[tuple[StageUnit, ...], dict[str, Any]]:
    units = tuple(
        StageUnit(
            source_track="captions",
            start_ms=max(0, int(cue.start_ms)),
            end_ms=max(int(cue.start_ms) + 1, int(cue.end_ms)),
            text=cue.text,
            timing_origin="youtube_caption",
            speaker_origin="none",
            alignment_quality=AlignmentQuality.PROVIDER_SEGMENT,
            provider_native_id=f"caption-{index}",
        )
        for index, cue in enumerate(cues)
        if str(cue.text or "").strip()
    )
    return units, {
        "normalizedIntervalCount": len(units),
        "nativeSpeakerIntervals": 0,
        "nativeSpeakerEvidence": False,
        "exactWordIntervalCount": 0,
        "estimatedTiming": False,
    }


def stage_units_from_local_segments(
    segments: Sequence[Mapping[str, Any]],
    *,
    source_track: str = "mix",
) -> tuple[StageUnit, ...]:
    """Normalize the shared Sherpa-ONNX fallback result without overstating timing."""
    units: list[StageUnit] = []
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = max(0, int(segment.get("startMs") or 0))
        end = max(start + 1, int(segment.get("endMs") or start + 1))
        label = str(segment.get("speakerLabel") or segment.get("speaker") or "").strip()
        key = segment.get("speakerKey", segment.get("speakerId"))
        units.append(
            StageUnit(
                source_track=str(segment.get("source") or source_track),
                start_ms=start,
                end_ms=end,
                text=text,
                speaker_key=key if key not in (None, "") else label or None,
                speaker_label=label,
                timing_origin=str(segment.get("timingOrigin") or "provider_aligned_local_turn"),
                speaker_origin="local_diarization",
                alignment_quality=str(segment.get("alignmentQuality") or "estimated"),
                provider_native_id=f"local-diarization-{index}",
            )
        )
    return tuple(units)


def canonical_drafts(units: Iterable[StageUnit]) -> tuple[CanonicalSegmentDraft, ...]:
    return tuple(CanonicalSegmentDraft(**unit.__dict__) for unit in units)
