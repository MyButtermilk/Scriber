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
from src.core.provider_audio_formats import (
    AudioInputFormat,
    AudioSelectionMode,
    ProviderAudioRouteKind,
    UnsupportedProviderAudioRoute,
    batch_route_for_provider,
    realtime_pcm_preparation_implementation,
    realtime_route_for_provider,
    require_exact_audio_input_format,
    resolve_provider_audio_capabilities,
    supports_exact_audio_input_format,
    try_resolve_batch_provider_audio_capabilities,
)
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
    provider_route: str = ""
    audio_input_format: AudioInputFormat | None = None
    provider_audio_capability_id: str = ""
    provider_audio_capability_revision: str = ""
    audio_input_format_verified: bool | None = None
    audio_selection_mode: AudioSelectionMode | None = None
    audio_preparation_implementation: str = ""
    provider_region: str = ""
    provider_endpoint_sha256: str = ""

    def execution_route(self) -> dict[str, Any]:
        """Private in-memory values consumed by one concrete pipeline run."""
        return {
            "model": self.model,
            "language": self.language,
            "custom_vocab": self.custom_vocab,
            "transport": self.transport,
            "provider_route": self.provider_route,
            "audio_input_format": (
                self.audio_input_format.value if self.audio_input_format else None
            ),
            "provider_audio_capability_id": self.provider_audio_capability_id,
            "provider_audio_capability_revision": (
                self.provider_audio_capability_revision
            ),
            "audio_input_format_verified": self.audio_input_format_verified,
            "audio_selection_mode": (
                self.audio_selection_mode.value
                if self.audio_selection_mode is not None
                else None
            ),
            "audio_preparation_implementation": (
                self.audio_preparation_implementation or None
            ),
            "provider_region": self.provider_region or None,
            "provider_endpoint_sha256": self.provider_endpoint_sha256 or None,
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
        if self.provider_route:
            # These bounded identifiers are safe to persist in the existing
            # request_options_json column.  Evidence URLs and custom endpoint
            # values deliberately stay in the static registry only.
            options.update(
                {
                    "providerRoute": self.provider_route,
                    "audioInputFormat": (
                        self.audio_input_format.value
                        if self.audio_input_format
                        else None
                    ),
                    "providerAudioCapabilityId": (
                        self.provider_audio_capability_id or None
                    ),
                    "providerAudioCapabilityRevision": (
                        self.provider_audio_capability_revision or None
                    ),
                    "audioInputFormatVerified": self.audio_input_format_verified,
                    "audioSelectionMode": (
                        self.audio_selection_mode.value
                        if self.audio_selection_mode is not None
                        else None
                    ),
                    "audioPreparationImplementation": (
                        self.audio_preparation_implementation or None
                    ),
                }
            )
        if self.provider_region:
            options["providerRegion"] = self.provider_region
        if self.provider_endpoint_sha256:
            options["providerEndpointSha256"] = self.provider_endpoint_sha256
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
        "assemblyai_realtime": Config.ASSEMBLYAI_RT_MODEL,
        "mistral": Config.MISTRAL_ASYNC_MODEL,
        "mistral_async": Config.MISTRAL_ASYNC_MODEL,
        "openai_async": Config.OPENAI_STT_MODEL,
        "onnx_local": Config.ONNX_MODEL,
        "deepgram_async": Config.DEEPGRAM_MODEL,
        "deepgram": Config.DEEPGRAM_MODEL,
        "openai": Config.OPENAI_REALTIME_STT_MODEL,
        "gladia": "pre-recorded-v2",
        "gladia_async": "pre-recorded-v2",
        "speechmatics_async": "batch-v2",
        "modulate": "velma-2-stt-batch",
        "modulate_async": "velma-2-stt-batch",
        "smallest": "pulse",
        "smallest_async": "pulse",
        "azure_mai": getattr(Config, "AZURE_MAI_MODEL", "mai-transcribe-1.5"),
        "gemini_stt": Config.GEMINI_STT_MODEL,
        "groq": "whisper-large-v3-turbo",
        "google": "latest_long",
        "elevenlabs": "scribe_v2_realtime",
        "speechmatics": "enhanced",
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
    model: str | None = None,
    provider_route: str | None = None,
    audio_input_format: AudioInputFormat | str | None = None,
    audio_selection_mode: AudioSelectionMode | str | None = None,
    audio_preparation_implementation: str | None = None,
    custom_endpoint: bool = False,
    provider_region: str | None = None,
    provider_endpoint_sha256: str | None = None,
) -> FrozenTranscriptionRoute:
    key = str(provider or "").strip().lower()
    direct = key in {
        "soniox", "soniox_async", "assemblyai", "mistral", "mistral_async",
        "smallest", "smallest_async", "deepgram_async", "openai_async",
        "gemini_stt", "azure_mai", "gladia", "gladia_async", "speechmatics_async",
        "modulate", "modulate_async",
    }
    final_text_only = key in {"modulate", "modulate_async"}
    resolved_model = str(model or provider_batch_model(key)).strip()
    streaming_only = key in {
        "assemblyai_realtime",
        "deepgram",
        "openai",
        "google",
        "elevenlabs",
        "speechmatics",
    }
    route_kind = (
        ProviderAudioRouteKind.REALTIME
        if streaming_only
        else ProviderAudioRouteKind.BATCH
    )
    default_provider_route = (
        realtime_route_for_provider(key)
        if route_kind == ProviderAudioRouteKind.REALTIME
        else batch_route_for_provider(key)
    )
    requested_provider_route = str(
        provider_route or default_provider_route or ""
    ).strip()
    if route_kind == ProviderAudioRouteKind.REALTIME:
        try:
            capability = resolve_provider_audio_capabilities(
                key,
                requested_provider_route,
                resolved_model,
                custom_endpoint=custom_endpoint,
            )
        except UnsupportedProviderAudioRoute:
            capability = None
    else:
        capability = try_resolve_batch_provider_audio_capabilities(
            key,
            resolved_model,
            custom_endpoint=custom_endpoint,
        )
    if capability is not None and (
        requested_provider_route and requested_provider_route != capability.route
    ):
        capability = None

    resolved_transport = str(
        transport or ("direct_upload" if direct else "decoded_pcm")
    )
    realtime_pcm_implementation = realtime_pcm_preparation_implementation(key)
    if streaming_only and realtime_pcm_implementation:
        # Each streaming-only key consumes decoded signed 16-bit mono PCM.
        # OpenAI's Pipecat service resamples that stream to its required 24 kHz
        # wire representation; the implementation marker records that detail.
        runtime_audio_contract = (
            AudioInputFormat.RAW_PCM16,
            realtime_pcm_implementation,
        )
    else:
        runtime_audio_contract = {
            # Pipecat's segmented STT boundary wraps every PCM16 utterance in a
            # WAV container before Groq's OpenAI-compatible v1 multipart request.
            "groq": (
                AudioInputFormat.WAV_PCM16,
                "pipecat_segmented_wav_pcm16",
            ),
        }.get(key)
    if (
        audio_input_format is None
        and resolved_transport == "decoded_pcm"
        and capability is not None
        and runtime_audio_contract is not None
    ):
        audio_input_format = runtime_audio_contract[0]
        if audio_selection_mode is None:
            audio_selection_mode = AudioSelectionMode.GENERATED
        if audio_preparation_implementation is None:
            audio_preparation_implementation = runtime_audio_contract[1]
    transport_audio_format = {
        "webm_opus_task_derivative": AudioInputFormat.WEBM_OPUS,
        "mp3_task_derivative": AudioInputFormat.MP3,
    }.get(resolved_transport)
    resolved_audio_format: AudioInputFormat | None
    format_verified: bool | None
    if audio_input_format is not None:
        if capability is None:
            # An explicit selection must never inherit capabilities from an
            # unknown model, route, or custom endpoint.
            raise UnsupportedProviderAudioRoute(
                "Cannot freeze an audio format without an exact provider "
                "route/model capability."
            )
        resolved_audio_format = require_exact_audio_input_format(
            capability,
            audio_input_format,
            route_kind=route_kind,
        )
        format_verified = True
    elif transport_audio_format is not None:
        # Record an already-existing task transport honestly.  A false marker
        # exposes a legacy route/capability mismatch without granting it to the
        # new selector or disrupting the current worker in this metadata-only
        # stage.
        resolved_audio_format = transport_audio_format
        format_verified = bool(
            capability
            and supports_exact_audio_input_format(
                capability,
                transport_audio_format,
                route_kind=route_kind,
            )
        )
    else:
        # Source bytes are not inspected at this early route-freezing boundary.
        # Keep the value unknown rather than claiming an unobserved format.
        resolved_audio_format = None
        format_verified = None

    resolved_selection_mode: AudioSelectionMode | None = None
    if audio_selection_mode is not None:
        if resolved_audio_format is None or format_verified is not True:
            raise UnsupportedProviderAudioRoute(
                "Cannot freeze audio selection metadata without an exact verified format."
            )
        try:
            resolved_selection_mode = (
                audio_selection_mode
                if isinstance(audio_selection_mode, AudioSelectionMode)
                else AudioSelectionMode(str(audio_selection_mode).strip().lower())
            )
        except ValueError as exc:
            raise UnsupportedProviderAudioRoute(
                "Audio selection mode is not recognized."
            ) from exc
    resolved_implementation = str(audio_preparation_implementation or "").strip()
    if resolved_implementation and (
        resolved_selection_mode is None
        or len(resolved_implementation) > 160
        or not all(
            char.isalnum() or char in "._-" for char in resolved_implementation
        )
    ):
        raise UnsupportedProviderAudioRoute(
            "Audio preparation implementation metadata is invalid."
        )
    resolved_region = str(provider_region or "").strip().lower()
    if resolved_region and (
        len(resolved_region) > 64
        or not all(char.isalnum() or char in "._-" for char in resolved_region)
    ):
        raise UnsupportedProviderAudioRoute("Provider region metadata is invalid.")
    resolved_endpoint_sha256 = str(provider_endpoint_sha256 or "").strip().lower()
    if resolved_endpoint_sha256 and not re.fullmatch(
        r"[0-9a-f]{64}",
        resolved_endpoint_sha256,
    ):
        raise UnsupportedProviderAudioRoute(
            "Provider endpoint fingerprint is invalid."
        )

    return FrozenTranscriptionRoute(
        workload=workload,
        source_track=source_track,
        provider=key,
        model=resolved_model,
        transport=resolved_transport,
        language=str(Config.LANGUAGE if language is None else language) or "auto",
        response_shape=("final_text" if final_text_only else "provider_segments_or_words"),
        timestamp_mode=("estimated" if final_text_only else "word_or_segment"),
        diarization_mode=(
            (
                "local_fallback_if_enabled"
                if final_text_only
                else "native_if_evidenced_else_local"
            )
            if diarization_requested
            else "disabled"
        ),
        parser_id=PARSER_ID,
        parser_version=PARSER_VERSION,
        custom_vocab=str(Config.CUSTOM_VOCAB if custom_vocab is None else custom_vocab),
        local_worker_manifest=local_worker_manifest,
        provider_route=(capability.route if capability else requested_provider_route),
        audio_input_format=resolved_audio_format,
        provider_audio_capability_id=(
            capability.capability_id if capability else ""
        ),
        provider_audio_capability_revision=(
            capability.revision if capability else ""
        ),
        audio_input_format_verified=format_verified,
        audio_selection_mode=resolved_selection_mode,
        audio_preparation_implementation=resolved_implementation,
        provider_region=resolved_region,
        provider_endpoint_sha256=resolved_endpoint_sha256,
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
