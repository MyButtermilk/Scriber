"""Canonical meeting transcript and analysis orchestration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from loguru import logger

from src import database
from src.config import Config
from src.core.provider_capabilities import (
    meeting_max_duration_seconds,
    supports_direct_file_upload,
)
from src.data.meeting_store import MeetingStore
from src.data.transcript_artifact_store import (
    ArtifactInputDraft,
    AttemptRecord,
    AttemptState,
    CanonicalSegment,
    StageUnit,
    TranscriptArtifactStore,
)
from src.meeting_analysis import MEETING_ANALYSIS_SCHEMA_VERSION, analyze_meeting
from src.provider_transcript import has_speaker_evidence, normalize_provider_segments
from src.runtime.ffmpeg_commands import (
    lossless_flac_track_args,
    meeting_lossless_archive_args,
    meeting_opus_playback_args,
    wav_pcm_transcode_args,
    webm_opus_transcode_args,
)
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import (
    communicate_or_kill_on_cancel,
    hidden_subprocess_kwargs,
    read_stream_limited,
)
from src.speaker_diarization import DiarizationIneligibleError, diarization_component_installed
from src.transcript_artifacts import (
    canonical_drafts,
    freeze_provider_route,
    stage_units_from_local_segments,
    stage_units_from_provider,
)


ProgressCallback = Callable[[str, float], Awaitable[None]]


_ATTEMPT_LEASE_TTL_SECONDS = 1_800.0
_ATTEMPT_LEASE_HEARTBEAT_SECONDS = 300.0
_ATTEMPT_LEASE_RETRY_DELAYS_SECONDS = (0.0, 0.5, 2.0)


@dataclass(frozen=True)
class PreparedMeetingTrack:
    path: Path
    duration_ms: int
    timeline_origin_ms: int
    sample_count: int
    pcm_sha256: str


class MeetingFinalizer:
    def __init__(
        self,
        store: MeetingStore,
        audio_root: Path,
        pipeline_factory: Callable[..., Any],
        text_generator: Callable[..., Awaitable[str]],
        speaker_model: Any | None = None,
        speaker_diarizer: Any | None = None,
        artifact_store: TranscriptArtifactStore | None = None,
    ) -> None:
        self.store = store
        self.audio_root = audio_root
        self.pipeline_factory = pipeline_factory
        self.text_generator = text_generator
        self.speaker_model = speaker_model
        self.speaker_diarizer = speaker_diarizer
        self.artifact_store = artifact_store or TranscriptArtifactStore(
            Path(database._DB_PATH)
        )
        self._active_attempt_lease: tuple[str, str] | None = None
        self._attempt_lease_heartbeat_task: asyncio.Task[None] | None = None

    @staticmethod
    def _ensure_transcript_parent(meeting: dict[str, Any]) -> None:
        if database.get_transcript(str(meeting["id"])) is not None:
            return
        created_at = str(meeting.get("createdAt") or meeting.get("startedAt") or "")
        database.save_transcript({
            "id": meeting["id"], "title": meeting["title"], "date": created_at,
            "duration": "00:00", "status": "processing", "type": "meeting",
            "language": meeting.get("language", "auto"), "step": "Finalizing",
            "sourceUrl": "", "channel": "Meeting", "thumbnailUrl": "",
            "content": "", "createdAt": created_at, "updatedAt": created_at,
        })

    def _frozen_meeting_route(self, meeting: dict[str, Any]):
        status = self.speaker_diarizer.status() if self.speaker_diarizer is not None else {}
        provider = str(meeting["finalProvider"])
        return freeze_provider_route(
            workload="meeting",
            provider=provider,
            source_track="meeting_tracks",
            language=str(meeting.get("language") or "auto"),
            diarization_requested=True,
            local_worker_manifest={
                "enabled": self.speaker_diarizer is not None,
                "engine": "sherpa-onnx",
                "componentPresent": bool(status.get("installed")),
                "workerVersion": str(status.get("workerVersion") or "unknown"),
            },
            transport=(
                "webm_opus_task_derivative"
                if provider.strip().lower() in {"soniox", "soniox_async"}
                else None
            ),
        )

    @staticmethod
    def _track_audio_evidence(track: PreparedMeetingTrack) -> dict[str, Any]:
        return {
            "pcmSha256": track.pcm_sha256,
            "sampleCount": track.sample_count,
            "durationMs": track.duration_ms,
            "timelineOriginMs": track.timeline_origin_ms,
        }

    @classmethod
    def _track_result_matches_audio(
        cls, track_result: Any, track: PreparedMeetingTrack
    ) -> bool:
        evidence = getattr(track_result, "evidence", {})
        actual = evidence.get("sourceAudio") if isinstance(evidence, dict) else None
        return actual == cls._track_audio_evidence(track)

    def _begin_artifact_attempt(
        self, meeting: dict[str, Any]
    ) -> tuple[AttemptRecord, str, Any | None, dict[str, Any]]:
        owner = f"meeting-{uuid4().hex}"
        selected_provider = str(meeting.get("finalProvider") or "").strip().lower()
        recovered = self.artifact_store.latest_recoverable_for_transcript(meeting["id"])
        if (
            recovered is not None
            and recovered.route_snapshot.provider.strip().lower() == selected_provider
        ):
            bundle = self.artifact_store.claim_recovery_bundle(
                recovered.attempt.id,
                owner=owner,
                expected_version=recovered.attempt.state_version,
                ttl_seconds=_ATTEMPT_LEASE_TTL_SECONDS,
            )
            return bundle.attempt, owner, bundle, self._execution_route_for_snapshot(
                bundle.route_snapshot
            )

        partial = self.artifact_store.latest_resumable_track_attempt(meeting["id"])
        if (
            partial is not None
            and partial.route_snapshot.provider.strip().lower() == selected_provider
        ):
            attempt = self.artifact_store.acquire_attempt_lease(
                partial.attempt.id,
                owner=owner,
                expected_version=partial.attempt.state_version,
                ttl_seconds=_ATTEMPT_LEASE_TTL_SECONDS,
            )
            return attempt, owner, partial, self._execution_route_for_snapshot(
                partial.route_snapshot
            )

        route = self._frozen_meeting_route(meeting)
        attempt = self.artifact_store.create_attempt(
            transcript_id=meeting["id"], workload="meeting"
        )
        self.artifact_store.persist_route_snapshot(attempt.id, route.snapshot_draft())
        attempt = self.artifact_store.acquire_attempt_lease(
            attempt.id,
            owner=owner,
            expected_version=attempt.state_version,
            ttl_seconds=_ATTEMPT_LEASE_TTL_SECONDS,
        )
        for expected, target in (
            (AttemptState.QUEUED, AttemptState.RESOLVING_SOURCE),
            (AttemptState.RESOLVING_SOURCE, AttemptState.SOURCE_READY),
            (AttemptState.SOURCE_READY, AttemptState.TRANSCRIBING),
        ):
            attempt = self.artifact_store.transition_attempt(
                attempt.id,
                expected_state=expected,
                expected_version=attempt.state_version,
                new_state=target,
                lease_owner=owner,
            )
        return attempt, owner, None, route.execution_route()

    async def _attempt_lease_heartbeat(self, attempt_id: str, owner: str) -> None:
        """Keep a long-running provider attempt owned without changing its CAS version."""
        terminal_states = {
            AttemptState.COMPLETED,
            AttemptState.SUPERSEDED,
            AttemptState.FAILED,
            AttemptState.CANCELED,
        }
        while True:
            await asyncio.sleep(_ATTEMPT_LEASE_HEARTBEAT_SECONDS)
            renewed = False
            for retry_index, delay_seconds in enumerate(
                _ATTEMPT_LEASE_RETRY_DELAYS_SECONDS
            ):
                if delay_seconds:
                    await asyncio.sleep(delay_seconds)
                try:
                    current = await asyncio.to_thread(
                        self.artifact_store.require_attempt, attempt_id
                    )
                    if (
                        current.state in terminal_states
                        or current.lease_owner != owner
                    ):
                        return
                    await asyncio.to_thread(
                        self.artifact_store.renew_attempt_lease,
                        attempt_id,
                        owner=owner,
                        expected_version=current.state_version,
                        ttl_seconds=_ATTEMPT_LEASE_TTL_SECONDS,
                    )
                    renewed = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    remaining = len(_ATTEMPT_LEASE_RETRY_DELAYS_SECONDS) - retry_index - 1
                    log = logger.warning if remaining else logger.error
                    log(
                        "Meeting artifact lease renewal failed "
                        f"({remaining} retries remain): {type(exc).__name__}: {exc}"
                    )
            if not renewed:
                # The next regular heartbeat remains well inside the 30-minute
                # lease. This avoids an unbounded tight retry loop during a
                # transient SQLite or shutdown failure.
                continue

    async def _start_attempt_lease_heartbeat(
        self, attempt_id: str, owner: str
    ) -> None:
        await self._stop_attempt_lease_heartbeat()
        self._active_attempt_lease = (attempt_id, owner)
        self._attempt_lease_heartbeat_task = asyncio.create_task(
            self._attempt_lease_heartbeat(attempt_id, owner),
            name=f"meeting-attempt-lease-{attempt_id[-8:]}",
        )

    async def _stop_attempt_lease_heartbeat(self) -> None:
        task = self._attempt_lease_heartbeat_task
        self._attempt_lease_heartbeat_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "Meeting artifact lease heartbeat stopped with an error: "
                    f"{type(exc).__name__}: {exc}"
                )

    @staticmethod
    def _execution_route_for_snapshot(snapshot: Any) -> dict[str, Any]:
        vocab = str(Config.CUSTOM_VOCAB or "")
        expected = str(snapshot.request_options.get("customVocabularySha256") or "")
        actual = hashlib.sha256(vocab.encode("utf-8")).hexdigest() if vocab else ""
        if expected and actual != expected:
            raise RuntimeError(
                "Meeting recovery needs the same private custom vocabulary used by its frozen route."
            )
        return {
            "model": snapshot.model,
            "language": snapshot.language,
            "custom_vocab": vocab,
            "transport": snapshot.transport,
        }

    def _commit_artifact(
        self,
        *,
        attempt: AttemptRecord,
        owner: str,
        units: list[StageUnit],
        track_results: list[Any],
        track_derivations: list[Any],
    ) -> tuple[CanonicalSegment, ...]:
        if attempt.state == AttemptState.TRANSCRIBING:
            combined_text = " ".join(unit.text for unit in units)
            _stage, attempt = self.artifact_store.persist_stage_result(
                attempt.id,
                expected_version=attempt.state_version,
                transcript_text=combined_text,
                units=units,
                evidence={
                    "trackCount": len(track_results),
                    "normalizedIntervalCount": len(units),
                    "nativeSpeakerIntervals": sum(
                        1 for unit in units if unit.speaker_origin == "provider_native"
                    ),
                },
                lease_owner=owner,
            )
        if attempt.state == AttemptState.PROVIDER_RESULT_READY:
            attempt = self.artifact_store.transition_attempt(
                attempt.id,
                expected_state=AttemptState.PROVIDER_RESULT_READY,
                expected_version=attempt.state_version,
                new_state=AttemptState.CANONICALIZING,
                lease_owner=owner,
            )
        if attempt.state == AttemptState.CANONICALIZING:
            attempt = self.artifact_store.transition_attempt(
                attempt.id,
                expected_state=AttemptState.CANONICALIZING,
                expected_version=attempt.state_version,
                new_state=AttemptState.COMMITTING,
                lease_owner=owner,
            )
        result = self.artifact_store.commit_canonical_artifact(
            attempt.id,
            expected_attempt_version=attempt.state_version,
            expected_head_generation=attempt.expected_head_generation,
            segments=canonical_drafts(units),
            inputs=[
                ArtifactInputDraft(
                    "track_stage_result",
                    item.id,
                    item.result_sha256,
                    {"sourceTrack": item.source_track},
                )
                for item in track_results
            ] + [
                ArtifactInputDraft(
                    "track_derivation",
                    item.id,
                    item.result_sha256,
                    {
                        "sourceTrack": item.source_track,
                        "derivationKind": item.derivation_kind,
                        "parentStageResultId": item.parent_stage_result_id,
                    },
                )
                for item in track_derivations
            ],
            lease_owner=owner,
        )
        artifact = result.artifact
        if artifact is None and result.head is not None:
            artifact = self.artifact_store.get_artifact(result.head.artifact_id)
        if artifact is None:
            raise RuntimeError("Meeting canonical artifact commit produced no artifact.")
        return artifact.segments

    async def run(self, meeting_id: str, progress: ProgressCallback) -> dict[str, Any]:
        self._active_attempt_lease: tuple[str, str] | None = None
        try:
            return await self._run_impl(meeting_id, progress)
        finally:
            lease = self._active_attempt_lease
            await self._stop_attempt_lease_heartbeat()
            self._active_attempt_lease = None
            if lease is not None:
                attempt_id, owner = lease
                try:
                    current = self.artifact_store.require_attempt(attempt_id)
                    if current.lease_owner == owner and current.state not in {
                        AttemptState.COMPLETED,
                        AttemptState.SUPERSEDED,
                        AttemptState.FAILED,
                        AttemptState.CANCELED,
                    }:
                        self.artifact_store.release_attempt_lease(
                            attempt_id,
                            owner=owner,
                            expected_version=current.state_version,
                        )
                except Exception:
                    # Recovery also handles expiry; never mask the finalization
                    # result with best-effort lease cleanup.
                    pass

    async def _run_impl(self, meeting_id: str, progress: ProgressCallback) -> dict[str, Any]:
        meeting = self.store.get(meeting_id)
        await progress("Preparing durable audio", 0.05)
        tracks: dict[str, PreparedMeetingTrack] = {}
        for source in ("microphone", "mic_clean", "system"):
            # Verification/concatenation are proportional to meeting length.
            # Only one full PCM working track exists at a time; it is converted
            # to verified lossless FLAC before the next source is prepared.
            chunks = await asyncio.to_thread(self._validated_chunks, meeting_id, source)
            if chunks:
                tracks[source] = await self._prepare_lossless_track(
                    meeting_id,
                    source,
                    chunks,
                )
        if not tracks:
            raise ValueError("No durable meeting audio chunks are available for finalization.")
        await self._consolidate_audio_assets(meeting_id, tracks)

        await progress("Creating canonical transcript", 0.2)
        await asyncio.to_thread(self._ensure_transcript_parent, meeting)
        attempt, owner, recovery, execution_route = self._begin_artifact_attempt(meeting)
        await self._start_attempt_lease_heartbeat(attempt.id, owner)
        recovered_stage = getattr(recovery, "stage_result", None)
        track_results = list(getattr(recovery, "track_results", ()))
        if not track_results:
            track_results = list(self.artifact_store.list_track_stage_results(attempt.id))
        track_derivations = list(getattr(recovery, "track_derivations", ()))
        if not track_derivations:
            track_derivations = list(
                self.artifact_store.list_track_derivations(attempt.id)
            )
        canonical_units: list[StageUnit] = []
        transcription_tracks = {
            "microphone": tracks.get("mic_clean") or tracks.get("microphone"),
            "system": tracks.get("system"),
        }
        transcription_tracks = {key: value for key, value in transcription_tracks.items() if value}
        recovered_sources = {item.source_track for item in track_results}
        audio_identity_changed = bool(
            recovered_stage is not None and not track_results
        ) or any(
            source not in transcription_tracks
            or not self._track_result_matches_audio(result, transcription_tracks[source])
            for source, result in ((item.source_track, item) for item in track_results)
        ) or any(
            source not in recovered_sources
            for source in transcription_tracks
            if recovered_stage is not None
        )
        if audio_identity_changed:
            await self._stop_attempt_lease_heartbeat()
            self.artifact_store.transition_attempt(
                attempt.id,
                expected_state=attempt.state,
                expected_version=attempt.state_version,
                new_state=AttemptState.FAILED,
                lease_owner=owner,
                error_code="source_audio_identity_changed",
                error_message=(
                    "Prepared Meeting audio changed after the persisted provider result."
                ),
            )
            self._active_attempt_lease = None
            attempt, owner, recovery, execution_route = self._begin_artifact_attempt(meeting)
            await self._start_attempt_lease_heartbeat(attempt.id, owner)
            recovered_stage = getattr(recovery, "stage_result", None)
            track_results = list(getattr(recovery, "track_results", ()))
            if not track_results:
                track_results = list(
                    self.artifact_store.list_track_stage_results(attempt.id)
                )
            track_derivations = list(getattr(recovery, "track_derivations", ()))
            if not track_derivations:
                track_derivations = list(
                    self.artifact_store.list_track_derivations(attempt.id)
                )
        empty_sources: list[str] = []
        if recovered_stage is not None:
            canonical_units = list(recovered_stage.units)
        else:
            by_source = {item.source_track: item for item in track_results}
            derivation_by_source = {
                item.source_track: item
                for item in track_derivations
                if item.derivation_kind == "local_speaker_diarization"
            }
            for index, (source, track) in enumerate(
                transcription_tracks.items()
            ):
                path = track.path
                duration_ms = track.duration_ms
                timeline_origin_ms = track.timeline_origin_ms
                track_result = by_source.get(source)
                pipeline = None
                payload = None
                if track_result is None:
                    provider_duration_limit = meeting_max_duration_seconds(
                        meeting["finalProvider"],
                        str(execution_route.get("model") or ""),
                    )
                    if (
                        provider_duration_limit is not None
                        and duration_ms > provider_duration_limit * 1_000
                    ):
                        raise ValueError(
                            f"{meeting['finalProvider']} accepts Meeting tracks up to "
                            f"{provider_duration_limit // 60} minutes; choose a compatible "
                            "final transcription model for this recording."
                        )
                    parts: list[str] = []

                    def on_transcription(text: str, is_final: bool) -> None:
                        if is_final and text.strip():
                            parts.append(text.strip())

                    pipeline = self.pipeline_factory(
                        service_name=meeting["finalProvider"],
                        on_status_change=None,
                        on_audio_level=None,
                        on_transcription=on_transcription,
                        on_progress=None,
                        enable_speaker_diarization=source == "system",
                        direct_file_speaker_diarization=source == "system",
                        execution_route=execution_route,
                        direct_file_expected_duration_seconds=duration_ms / 1_000.0,
                    )
                    provider_input = path
                    provider_derivative: Path | None = None
                    try:
                        if (
                            supports_direct_file_upload(meeting["finalProvider"])
                            and execution_route.get("transport")
                            == "webm_opus_task_derivative"
                        ):
                            provider_derivative = await self._create_webm_provider_derivative(
                                path, source=source
                            )
                            provider_input = provider_derivative
                        if supports_direct_file_upload(meeting["finalProvider"]):
                            await pipeline.transcribe_file_direct(str(provider_input))
                        else:
                            await pipeline.transcribe_file(str(provider_input))
                    finally:
                        if provider_derivative is not None:
                            provider_derivative.unlink(missing_ok=True)
                    text = "\n".join(parts).strip()
                    payload = getattr(pipeline, "last_structured_transcript_payload", None)
                    provider_units, evidence = stage_units_from_provider(
                        provider=meeting["finalProvider"],
                        payload=payload,
                        text=text,
                        duration_ms=duration_ms,
                        source_track=source,
                        origin_ms=timeline_origin_ms,
                    )
                    if not provider_units:
                        if not text:
                            # A canonical Meeting track may legitimately be
                            # silent (for example, no remote participant audio
                            # on the system loopback). Do not fabricate a
                            # segment or fail the other track's valid speech.
                            empty_sources.append(source)
                            await progress(
                                f"No {source} speech detected; continuing with other tracks",
                                0.25 + (index + 1) * 0.25,
                            )
                            continue
                        raise ValueError(
                            "The final transcription provider returned text but no usable "
                            f"{source} transcript segments."
                        )
                    if not text:
                        text = "\n".join(
                            unit.text.strip()
                            for unit in provider_units
                            if unit.text.strip()
                        ).strip()
                    if not text:
                        raise ValueError(
                            "The final transcription provider returned unusable empty "
                            f"{source} transcript segments."
                        )
                    track_result = self.artifact_store.persist_track_stage_result(
                        attempt.id,
                        source_track=source,
                        expected_version=attempt.state_version,
                        transcript_text=text,
                        units=provider_units,
                        evidence={
                            **evidence,
                            "sourceAudio": self._track_audio_evidence(track),
                        },
                        lease_owner=owner,
                    )
                    track_results.append(track_result)
                    by_source[source] = track_result

                selected_units = list(track_result.units)
                native_speaker_evidence = bool(
                    track_result.evidence.get("nativeSpeakerEvidence")
                )
                derivation = derivation_by_source.get(source)
                if derivation is not None:
                    selected_units = list(derivation.units)
                elif (
                    source == "system"
                    and not native_speaker_evidence
                    and self.speaker_diarizer is not None
                    and await diarization_component_installed(self.speaker_diarizer)
                ):
                    await progress("Separating remote speakers locally", 0.25 + index * 0.25)
                    normalized_words = None
                    if pipeline is None:
                        normalized_words = [
                            {
                                "text": unit.text,
                                "startMs": unit.start_ms,
                                "endMs": unit.end_ms,
                                "speaker": "",
                                "confidence": None,
                                "alignmentQuality": str(
                                    getattr(unit.alignment_quality, "value", unit.alignment_quality)
                                ),
                                "concatenate": False,
                            }
                            for unit in track_result.units
                        ]
                    try:
                        fallback_segments, _turns = (
                            await self.speaker_diarizer.transcribe_with_fallback_speakers(
                                audio_path=path,
                                provider=meeting["finalProvider"],
                                payload=payload,
                                text=track_result.transcript_text,
                                source=source,
                                timeline_origin_ms=timeline_origin_ms,
                                normalized_words=normalized_words,
                            )
                        )
                    except DiarizationIneligibleError:
                        fallback_segments = []
                        await progress(
                            "Local speaker separation skipped above 60 minutes; use native diarization",
                            0.25 + index * 0.25,
                        )
                    if fallback_segments:
                        selected_units = list(
                            stage_units_from_local_segments(
                                fallback_segments, source_track=source
                            )
                        )
                        derivation = self.artifact_store.persist_track_derivation(
                            attempt.id,
                            parent_stage_result_id=track_result.id,
                            source_track=source,
                            derivation_kind="local_speaker_diarization",
                            expected_version=attempt.state_version,
                            units=selected_units,
                            evidence={
                                "engine": "sherpa-onnx",
                                "parentStageResultSha256": track_result.result_sha256,
                                "segmentCount": len(selected_units),
                            },
                            lease_owner=owner,
                        )
                        track_derivations.append(derivation)
                        derivation_by_source[source] = derivation
                canonical_units.extend(selected_units)
                await progress(
                    f"Transcribed {source} audio", 0.25 + (index + 1) * 0.25
                )

        if not canonical_units:
            empty_label = ", ".join(empty_sources) if empty_sources else "all available"
            raise ValueError(
                "The final transcription provider returned no speech on any canonical "
                f"Meeting track ({empty_label})."
            )
        echo_candidates = [
            {
                "id": unit.provider_native_id or f"stage-{index}",
                "source": unit.source_track,
                "providerSegmentId": unit.provider_native_id,
                "speakerKey": unit.speaker_key,
                "speakerLabel": unit.speaker_label,
                "startMs": unit.start_ms,
                "endMs": unit.end_ms,
                "text": unit.text,
                "timingOrigin": unit.timing_origin,
                "speakerOrigin": unit.speaker_origin,
                "alignmentQuality": str(
                    getattr(unit.alignment_quality, "value", unit.alignment_quality)
                ),
            }
            for index, unit in enumerate(canonical_units)
        ]
        echo_candidates = self._remove_cross_track_echoes(echo_candidates)
        echo_candidates.sort(
            key=lambda item: (
                item["startMs"], 0 if item["source"] == "microphone" else 1
            )
        )
        canonical_units = [
            StageUnit(
                source_track=item["source"],
                start_ms=item["startMs"],
                end_ms=item["endMs"],
                text=item["text"],
                speaker_key=item.get("speakerKey"),
                speaker_label=item.get("speakerLabel", ""),
                timing_origin=item.get("timingOrigin", "provider"),
                speaker_origin=item.get("speakerOrigin", "none"),
                alignment_quality=item.get("alignmentQuality", "estimated"),
                provider_native_id=item.get("providerSegmentId", ""),
            )
            for item in echo_candidates
        ]
        artifact_segments = self._commit_artifact(
            attempt=attempt,
            owner=owner,
            units=canonical_units,
            track_results=track_results,
            track_derivations=track_derivations,
        )
        projected = [
            {
                "id": segment.segment_id,
                "revision": "canonical",
                "source": segment.source_track,
                "providerSegmentId": segment.provider_native_id,
                # MeetingStore speaker ids are globally keyed entities; the
                # artifact's provider-local key must not be reused as that id.
                "speakerId": None,
                "speakerLabel": segment.speaker_label,
                "startMs": segment.start_ms,
                "endMs": segment.end_ms,
                "text": segment.text,
                "alignmentQuality": segment.alignment_quality.value,
                "isFinal": True,
                "sequence": segment.order_index,
            }
            for segment in artifact_segments
        ]
        self.store.replace_segments(meeting_id, "canonical", projected)

        if meeting.get("voiceLibraryEnabled") and self.speaker_model is not None:
            await progress("Building local speaker profiles", 0.76)
            await self._apply_speaker_intelligence(meeting_id, tracks)

        if not meeting.get("autoAnalyze", True):
            detail = self.store.detail(meeting_id)
            await asyncio.to_thread(self._publish_global_transcript, meeting, detail, {})
            ready = self.store.transition(meeting_id, "ready")
            await self._purge_redundant_pcm_after_ready(meeting_id, tracks)
            await progress("Transcript ready; analysis is available on demand", 1.0)
            return ready

        self.store.transition(meeting_id, "analyzing")
        await progress("Generating cited decisions and action items", 0.8)
        detail = self.store.detail(meeting_id)

        async def cache_get(stage: str, digest: str) -> dict[str, Any] | None:
            return await asyncio.to_thread(
                self.store.get_analysis_chunk,
                meeting_id,
                stage=stage,
                input_sha256=digest,
                model=meeting["analysisModel"],
                schema_version=MEETING_ANALYSIS_SCHEMA_VERSION,
            )

        async def cache_put(
            stage: str, digest: str, payload: dict[str, Any]
        ) -> None:
            await asyncio.to_thread(
                self.store.put_analysis_chunk,
                meeting_id,
                stage=stage,
                input_sha256=digest,
                model=meeting["analysisModel"],
                schema_version=MEETING_ANALYSIS_SCHEMA_VERSION,
                payload=payload,
            )

        async def analysis_progress(status: str, fraction: float) -> None:
            await progress(status, 0.8 + 0.18 * fraction)

        analysis = await analyze_meeting(
            meeting["title"],
            detail["segments"],
            detail["notes"],
            model=meeting["analysisModel"],
            generate=self.text_generator,
            cache_get=cache_get,
            cache_put=cache_put,
            on_progress=analysis_progress,
        )
        self.store.save_output(
            meeting_id,
            kind="analysis",
            schema_version="1",
            payload=analysis,
            transcript_revision="canonical",
            provider=meeting["analysisModel"],
        )
        await asyncio.to_thread(
            self._publish_global_transcript, meeting, self.store.detail(meeting_id), analysis
        )
        ready = self.store.transition(meeting_id, "ready")
        await self._purge_redundant_pcm_after_ready(meeting_id, tracks)
        await progress("Meeting ready", 1.0)
        return ready

    async def _purge_redundant_pcm_after_ready(
        self,
        meeting_id: str,
        tracks: dict[str, PreparedMeetingTrack],
    ) -> None:
        """Remove redundant WAVs only after durable canonical ownership exists."""
        try:
            head = self.artifact_store.get_head(meeting_id)
            assets = {item["kind"]: item for item in self.store.audio_assets(meeting_id)}
            archive = assets.get("multitrack_flac")
            required = {"playback_system"} if "system" in tracks else set()
            if "microphone" in tracks or "mic_clean" in tracks:
                required.add("playback_microphone")
            if (
                head is None
                or archive is None
                or not archive.get("equalityVerified")
                or not required.issubset(assets)
            ):
                return
            root = self.audio_root.resolve()
            verified_paths: list[Path] = []
            for kind in {"multitrack_flac", "playback_mix", *required}:
                asset = assets.get(kind)
                if asset is None:
                    return
                path = (root / str(asset["relativePath"])).resolve()
                if root not in path.parents or not path.is_file():
                    return
                digest = await asyncio.to_thread(self._sha256_file, path)
                if digest != str(asset.get("sha256") or ""):
                    return
                verified_paths.append(path)

            self.store.mark_audio_chunks_purge_pending(meeting_id)
            pending = self.store.pending_audio_chunk_purges(meeting_id)
            removal_paths: set[Path] = {
                track.path.resolve() for track in tracks.values()
            }
            for chunk in pending:
                path = (root / str(chunk["relativePath"])).resolve()
                if root in path.parents:
                    removal_paths.add(path)
            for path in removal_paths:
                if path in verified_paths:
                    raise RuntimeError("Refusing to purge a verified Meeting archive asset.")
                await asyncio.to_thread(path.unlink, True)
            if any(path.exists() for path in removal_paths):
                raise RuntimeError("Redundant Meeting PCM could not be fully removed.")
            self.store.mark_audio_chunks_purged(meeting_id)
        except Exception as exc:
            # Meeting readiness and its verified archive are already durable.
            # Leave purge_pending tombstones for startup/maintenance recovery.
            logger.warning("Meeting PCM cleanup deferred for {}: {}", meeting_id, exc)

    async def resume_pending_pcm_purge(self, meeting_id: str) -> bool:
        """Finish an interrupted post-ready PCM purge from durable tombstones."""
        pending = self.store.pending_audio_chunk_purges(meeting_id)
        if not pending or self.artifact_store.get_head(meeting_id) is None:
            return False
        assets = {item["kind"]: item for item in self.store.audio_assets(meeting_id)}
        archive = assets.get("multitrack_flac")
        sources = {str(item["source"]) for item in pending}
        required = {"playback_system"} if "system" in sources else set()
        if sources.intersection({"microphone", "mic_clean"}):
            required.add("playback_microphone")
        if (
            archive is None
            or not archive.get("equalityVerified")
            or not required.issubset(assets)
        ):
            return False
        root = self.audio_root.resolve()
        for kind in {"multitrack_flac", "playback_mix", *required}:
            asset = assets.get(kind)
            if asset is None:
                return False
            path = (root / str(asset["relativePath"])).resolve()
            if (
                root not in path.parents
                or not path.is_file()
                or await asyncio.to_thread(self._sha256_file, path)
                != str(asset.get("sha256") or "")
            ):
                return False
        removal_paths: set[Path] = set()
        for item in pending:
            path = (root / str(item["relativePath"])).resolve()
            if root in path.parents:
                removal_paths.add(path)
        final_dir = (root / meeting_id / "final").resolve()
        if final_dir.parent == (root / meeting_id).resolve() and final_dir.is_dir():
            removal_paths.update(final_dir.glob("*.wav"))
            removal_paths.update(final_dir.glob("*.work.flac"))
        for path in removal_paths:
            await asyncio.to_thread(path.unlink, True)
        if any(path.exists() for path in removal_paths):
            return False
        self.store.mark_audio_chunks_purged(meeting_id)
        return True

    @staticmethod
    def _publish_global_transcript(
        meeting: dict[str, Any], detail: dict[str, Any], analysis: dict[str, Any]
    ) -> None:
        summary = str(analysis.get("executiveSummary") or "")
        existing = database.get_transcript(str(meeting["id"]))
        if existing is not None and str(existing.get("content") or "").strip():
            # CanonicalArtifactStore already owns content, preview, duration,
            # status, and timestamps. Analysis may update only its independent
            # summary lifecycle; rewriting content here would create a second
            # renderer and break the one-truth contract.
            database.update_transcript_summary_state(
                meeting["id"],
                status="completed" if summary else "idle",
                error="",
                summary=summary,
                step="Ready",
            )
            return
        segments = detail.get("segments", [])
        content = "\n\n".join(
            f"[{int(item['startMs']) // 60000}:{(int(item['startMs']) // 1000) % 60:02d}] "
            f"{item.get('speakerLabel') or item.get('source', 'Meeting')}: {item.get('text', '')}"
            for item in segments
        )
        duration_ms = max((int(item.get("endMs", 0)) for item in segments), default=0)
        duration = f"{duration_ms // 3_600_000:d}:{(duration_ms // 60_000) % 60:02d}:{(duration_ms // 1000) % 60:02d}"
        created_at = str(meeting.get("createdAt") or meeting.get("startedAt") or "")
        database.save_transcript({
            "id": meeting["id"], "title": meeting["title"], "date": created_at,
            "duration": duration, "status": "completed", "type": "meeting",
            "language": meeting.get("language", "auto"), "step": "Ready",
            "sourceUrl": "", "channel": "Meeting", "thumbnailUrl": "",
            "content": content, "createdAt": created_at,
            "updatedAt": detail.get("updatedAt") or created_at,
            "summary": summary,
            "summaryStatus": "completed" if summary else "idle", "summaryError": "",
            "summaryUpdatedAt": (detail.get("updatedAt") or created_at) if summary else "",
        })

    def _validated_chunks(self, meeting_id: str, source: str) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        root = self.audio_root.resolve()
        for chunk in self.store.audio_chunks(meeting_id, source):
            path = (root / str(chunk["relativePath"])).resolve()
            reason = ""
            try:
                if root not in path.parents or not path.is_file():
                    raise ValueError("unsafe-or-missing")
                if chunk.get("sha256") and self._sha256_file(path) != chunk["sha256"]:
                    raise ValueError("checksum-mismatch")
                with wave.open(str(path), "rb") as handle:
                    if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (1, 2, 16_000):
                        raise ValueError("invalid-pcm-format")
                    if handle.getnframes() <= 0:
                        raise ValueError("empty-wav")
            except (EOFError, wave.Error, ValueError) as exc:
                reason = str(exc) or type(exc).__name__
            except OSError as exc:
                # Access-denied/sharing violations can be transient on Windows
                # (scanner/indexer/AV). Preserve the complete chunk so retry can
                # validate it; only deterministic content/path failures are
                # quarantined as corrupt audio.
                raise RuntimeError(
                    f"Meeting audio chunk could not be validated ({type(exc).__name__})."
                ) from exc
            if not reason:
                valid.append(chunk)
                continue
            if path.is_file() and root in path.parents:
                quarantine = path.parent / "quarantine"
                quarantine.mkdir(parents=True, exist_ok=True)
                destination = quarantine / path.name
                if destination.exists():
                    destination = quarantine / f"{path.stem}-{chunk['id'][:8]}{path.suffix}"
                shutil.move(str(path), str(destination))
            self.store.quarantine_audio_chunk(
                meeting_id, str(chunk["id"]), reason=f"corrupt-chunk:{reason}"
            )
        return valid

    async def _apply_speaker_intelligence(
        self, meeting_id: str, tracks: dict[str, PreparedMeetingTrack]
    ) -> None:
        library_enabled = getattr(self.store, "speaker_library_enabled", None)
        if callable(library_enabled) and not await asyncio.to_thread(library_enabled):
            return
        detail = self.store.detail(meeting_id)
        scheduled_per_speaker: dict[str, int] = {}
        candidates_by_track: dict[str, list[dict[str, Any]]] = {}
        for segment in detail["segments"]:
            speaker_id = str(segment.get("speakerId") or "")
            if not speaker_id or int(segment["endMs"]) - int(segment["startMs"]) < 2_000:
                continue
            if scheduled_per_speaker.get(speaker_id, 0) >= 3:
                continue
            track_key = "system" if segment["source"] == "system" else (
                "mic_clean" if "mic_clean" in tracks else "microphone"
            )
            if track_key not in tracks:
                continue
            candidates_by_track.setdefault(track_key, []).append(segment)
            scheduled_per_speaker[speaker_id] = scheduled_per_speaker.get(speaker_id, 0) + 1

        for track_key, segments in candidates_by_track.items():
            track = tracks.get(track_key)
            if track is None:
                continue
            extraction_path = track.path
            temporary_wav: Path | None = None
            try:
                if extraction_path.suffix.lower() != ".wav":
                    temporary_wav = extraction_path.with_name(
                        f".{track_key}.{uuid4().hex}.voice.wav"
                    )
                    extraction_path = await self._materialize_pcm_wav(
                        track.path,
                        temporary_wav,
                    )
                for segment in segments:
                    speaker_id = str(segment.get("speakerId") or "")
                    try:
                        embedding = await self.speaker_model.extract(
                            extraction_path,
                            max(
                                0,
                                int(segment["startMs"]) - track.timeline_origin_ms,
                            ),
                            max(
                                0,
                                int(segment["endMs"]) - track.timeline_origin_ms,
                            ),
                        )
                        self.store.register_speaker_embedding(
                            meeting_id,
                            speaker_id,
                            str(segment["id"]),
                            embedding,
                            quality=1.0,
                        )
                    except Exception:
                        # Voiceprints are optional; transcript finalization remains authoritative.
                        continue
            except Exception:
                # A track-level decode failure cannot invalidate the transcript.
                continue
            finally:
                if temporary_wav is not None:
                    temporary_wav.unlink(missing_ok=True)

    async def _create_webm_provider_derivative(
        self, source_path: Path, *, source: str
    ) -> Path:
        """Create a task-owned Soniox upload derivative from lossless meeting PCM."""
        ffmpeg = require_media_tool("ffmpeg")
        destination = source_path.with_name(
            f".{source}.{uuid4().hex}.provider.webm"
        )
        args = webm_opus_transcode_args(
            ffmpeg,
            source_path,
            destination,
            bitrate="64k",
            sample_rate=16_000,
            channels=1,
        )
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        try:
            _, stderr = await communicate_or_kill_on_cancel(
                process,
                max_stderr_bytes=1024 * 1024,
            )
        except asyncio.CancelledError:
            destination.unlink(missing_ok=True)
            raise
        if process.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 0:
            destination.unlink(missing_ok=True)
            reason = stderr.decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"Meeting provider WebM preparation failed: {reason}")
        return destination

    async def _prepare_lossless_track(
        self,
        meeting_id: str,
        source: str,
        chunks: list[dict[str, Any]],
    ) -> PreparedMeetingTrack:
        """Bound peak PCM to one source, then publish a verified FLAC working track."""
        wav_path, duration_ms, timeline_origin_ms = await asyncio.to_thread(
            self._concatenate_wav,
            meeting_id,
            source,
            chunks,
        )
        expected = await asyncio.to_thread(self._pcm_wav_fingerprint, wav_path)
        ffmpeg = require_media_tool("ffmpeg")
        ffprobe = require_media_tool("ffprobe")
        destination = wav_path.with_name(f"{source}.work.flac")
        temporary = destination.with_name(
            f".{destination.stem}.{uuid4().hex}.partial{destination.suffix}"
        )
        process = await asyncio.create_subprocess_exec(
            *lossless_flac_track_args(ffmpeg, wav_path, temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        replaced = False
        try:
            _, stderr = await communicate_or_kill_on_cancel(
                process,
                max_stderr_bytes=1024 * 1024,
            )
            if process.returncode != 0 or not temporary.is_file():
                reason = (stderr or b"").decode("utf-8", errors="replace")[-800:]
                raise RuntimeError(f"Meeting lossless working-track encode failed: {reason}")
            await self._verify_audio_asset(
                ffprobe,
                temporary,
                expected_codec="flac",
                expected_streams=1,
            )
            decoded = await self._decoded_pcm_fingerprint(
                ffmpeg,
                temporary,
                stream_index=0,
            )
            if (
                decoded["sampleCount"] != expected["sampleCount"]
                or decoded["pcmSha256"] != expected["pcmSha256"]
            ):
                raise RuntimeError(
                    "Meeting lossless working track does not reproduce its source PCM."
                )
            temporary.replace(destination)
            replaced = True
            # The verified FLAC now owns this temporary source representation.
            # Checkpoint WAVs remain untouched until canonical readiness.
            await asyncio.to_thread(wav_path.unlink)
            return PreparedMeetingTrack(
                path=destination,
                duration_ms=duration_ms,
                timeline_origin_ms=timeline_origin_ms,
                sample_count=int(expected["sampleCount"]),
                pcm_sha256=str(expected["pcmSha256"]),
            )
        except BaseException:
            if not replaced:
                temporary.unlink(missing_ok=True)
            raise

    async def _materialize_pcm_wav(self, source_path: Path, destination: Path) -> Path:
        """Create a task-scoped canonical WAV for a WAV-only optional consumer."""
        ffmpeg = require_media_tool("ffmpeg")
        temporary = destination.with_name(
            f".{destination.stem}.{uuid4().hex}.partial{destination.suffix}"
        )
        process = await asyncio.create_subprocess_exec(
            *wav_pcm_transcode_args(ffmpeg, source_path, temporary),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        replaced = False
        try:
            _, stderr = await communicate_or_kill_on_cancel(
                process,
                max_stderr_bytes=1024 * 1024,
            )
            if process.returncode != 0 or not temporary.is_file():
                reason = (stderr or b"").decode("utf-8", errors="replace")[-800:]
                raise RuntimeError(f"Meeting PCM working derivative failed: {reason}")
            await asyncio.to_thread(self._pcm_wav_fingerprint, temporary)
            temporary.replace(destination)
            replaced = True
            return destination
        except BaseException:
            if not replaced:
                temporary.unlink(missing_ok=True)
            raise

    async def _coerce_prepared_track(
        self,
        value: PreparedMeetingTrack | tuple[Path, int, int],
    ) -> PreparedMeetingTrack:
        """Keep focused archive tests/backward callers compatible with WAV tuples."""
        if isinstance(value, PreparedMeetingTrack):
            return value
        path, duration_ms, timeline_origin_ms = value
        fingerprint = await asyncio.to_thread(self._pcm_wav_fingerprint, path)
        return PreparedMeetingTrack(
            path=path,
            duration_ms=int(duration_ms),
            timeline_origin_ms=int(timeline_origin_ms),
            sample_count=int(fingerprint["sampleCount"]),
            pcm_sha256=str(fingerprint["pcmSha256"]),
        )

    async def _consolidate_audio_assets(
        self,
        meeting_id: str,
        tracks: dict[str, PreparedMeetingTrack | tuple[Path, int, int]],
    ) -> None:
        ffmpeg = require_media_tool("ffmpeg")
        ffprobe = require_media_tool("ffprobe")
        destination_dir = self.audio_root / meeting_id / "final"
        destination_dir.mkdir(parents=True, exist_ok=True)

        prepared_tracks = {
            source: await self._coerce_prepared_track(track)
            for source, track in tracks.items()
        }

        archive_tracks: list[tuple[str, str, PreparedMeetingTrack]] = []
        # The map order is a durable contract. Raw microphone is present only
        # when a distinct clean microphone track exists; otherwise the sole
        # microphone track must not be duplicated as both raw and clean.
        if "microphone" in prepared_tracks and "mic_clean" in prepared_tracks:
            archive_tracks.append(
                ("microphone", "Microphone raw", prepared_tracks["microphone"])
            )
        primary_microphone_source = "mic_clean" if "mic_clean" in prepared_tracks else (
            "microphone" if "microphone" in prepared_tracks else ""
        )
        if primary_microphone_source:
            title = "Microphone clean" if primary_microphone_source == "mic_clean" else "Microphone"
            archive_tracks.append(
                (
                    primary_microphone_source,
                    title,
                    prepared_tracks[primary_microphone_source],
                )
            )
        if "system" in prepared_tracks:
            archive_tracks.append(("system", "System audio", prepared_tracks["system"]))
        if not archive_tracks:
            raise ValueError("No supported meeting audio tracks are available for archiving.")

        playback_tracks: list[tuple[str, PreparedMeetingTrack]] = []
        if primary_microphone_source:
            playback_tracks.append(
                (primary_microphone_source, prepared_tracks[primary_microphone_source])
            )
        if "system" in prepared_tracks:
            playback_tracks.append(("system", prepared_tracks["system"]))

        archive_sources: list[dict[str, Any]] = []
        for source, _title, track in archive_tracks:
            archive_sources.append({
                "source": source,
                "timelineOriginMs": track.timeline_origin_ms,
                "durationMs": track.duration_ms,
                "sampleCount": track.sample_count,
                "pcmSha256": track.pcm_sha256,
            })

        outputs = [
            {
                "kind": "multitrack_flac",
                "destination": destination_dir / "meeting-tracks.mka",
                "codec": "flac",
                "command": lambda temporary: meeting_lossless_archive_args(
                    ffmpeg,
                    [(track.path, title) for _source, title, track in archive_tracks],
                    temporary,
                    stream_copy=all(
                        track.path.suffix.lower() == ".flac"
                        for _, _, track in archive_tracks
                    ),
                ),
                "sources": archive_sources,
            },
            {
                "kind": "playback_mix",
                "destination": destination_dir / "playback.opus",
                "codec": "opus",
                "command": lambda temporary: meeting_opus_playback_args(
                    ffmpeg,
                    [track.path for _source, track in playback_tracks],
                    temporary,
                    timeline_origins_ms=[
                        track.timeline_origin_ms for _source, track in playback_tracks
                    ],
                ),
                "sources": [{
                    "source": (
                        "mixed" if len(playback_tracks) > 1 else playback_tracks[0][0]
                    ),
                    # Playback media is padded onto the meeting clock, so seek
                    # time zero always means meeting time zero.
                    "timelineOriginMs": 0,
                    # Replaced with the verified encoded duration below.
                    "durationMs": max(
                        track.duration_ms for _source, track in playback_tracks
                    ),
                }],
                "minimumDurationMs": max(
                    track.timeline_origin_ms + track.duration_ms
                    for _source, track in playback_tracks
                ),
            },
        ]
        for playback_source, track in playback_tracks:
            public_kind = (
                "playback_microphone"
                if playback_source in {"microphone", "mic_clean"}
                else "playback_system"
            )
            outputs.append({
                "kind": public_kind,
                "destination": destination_dir / (
                    "microphone.opus" if public_kind == "playback_microphone" else "system.opus"
                ),
                "codec": "opus",
                "command": lambda temporary, selected=track: meeting_opus_playback_args(
                    ffmpeg,
                    [selected.path],
                    temporary,
                    timeline_origins_ms=[selected.timeline_origin_ms],
                ),
                "sources": [{
                    "source": playback_source,
                    "timelineOriginMs": 0,
                    "durationMs": track.duration_ms,
                }],
                "minimumDurationMs": track.timeline_origin_ms + track.duration_ms,
            })
        for output in outputs:
            kind = str(output["kind"])
            destination = Path(output["destination"])
            codec = str(output["codec"])
            temporary = destination.with_name(
                f".{destination.stem}.{uuid4().hex}.partial{destination.suffix}"
            )
            args = output["command"](temporary)
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **hidden_subprocess_kwargs(),
            )
            try:
                _, stderr = await communicate_or_kill_on_cancel(
                    process,
                    max_stderr_bytes=1024 * 1024,
                )
            except asyncio.CancelledError:
                temporary.unlink(missing_ok=True)
                raise
            if process.returncode != 0 or not temporary.is_file():
                temporary.unlink(missing_ok=True)
                reason = stderr.decode("utf-8", errors="replace")[-800:]
                raise RuntimeError(f"Meeting audio consolidation failed ({kind}): {reason}")
            replaced = False
            try:
                probe = await self._verify_audio_asset(
                    ffprobe,
                    temporary,
                    expected_codec=codec,
                    expected_streams=len(output["sources"]),
                )
                if (
                    kind.startswith("playback_")
                    and int(probe["durationMs"]) + 40 < int(output["minimumDurationMs"])
                ):
                    raise RuntimeError(
                        "Meeting playback does not cover the durable meeting timeline."
                    )
                decoded_fingerprints = [
                    await self._decoded_pcm_fingerprint(
                        ffmpeg,
                        temporary,
                        stream_index=stream_index,
                    )
                    for stream_index in range(len(output["sources"]))
                ]
                if kind == "multitrack_flac":
                    for source, decoded in zip(
                        output["sources"], decoded_fingerprints, strict=True
                    ):
                        if (
                            decoded["sampleCount"] != source["sampleCount"]
                            or decoded["pcmSha256"] != source["pcmSha256"]
                        ):
                            raise RuntimeError(
                                "Meeting lossless archive PCM does not match its source track."
                            )
                digest = await asyncio.to_thread(self._sha256_file, temporary)
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise RuntimeError(f"Meeting audio consolidation produced no valid hash ({kind}).")
                manifest = []
                for stream_index, (source, stream) in enumerate(
                    zip(output["sources"], probe["streams"], strict=True)
                ):
                    manifest.append({
                        "source": source["source"],
                        "streamIndex": stream_index,
                        "codec": stream["codec"],
                        "sampleRate": stream["sampleRate"],
                        "channels": stream["channels"],
                        "timelineOriginMs": int(source["timelineOriginMs"]),
                        "durationMs": (
                            int(probe["durationMs"])
                            if kind.startswith("playback_")
                            else int(source["durationMs"])
                        ),
                        "sampleCount": (
                            int(source["sampleCount"])
                            if kind == "multitrack_flac"
                            else int(decoded_fingerprints[stream_index]["sampleCount"])
                        ),
                        "pcmSha256": (
                            str(source["pcmSha256"])
                            if kind == "multitrack_flac"
                            else str(decoded_fingerprints[stream_index]["pcmSha256"])
                        ),
                        "equalityVerified": kind == "multitrack_flac",
                    })
                temporary.replace(destination)
                replaced = True
                relative = destination.relative_to(self.audio_root).as_posix()
                self.store.add_audio_asset(
                    meeting_id,
                    kind=kind,
                    relative_path=relative,
                    codec=codec,
                    sample_rate=int(probe["streams"][0]["sampleRate"]),
                    # This is the per-stream channel count, not the number of
                    # independently addressable streams in the container.
                    channels=int(probe["streams"][0]["channels"]),
                    duration_ms=int(probe["durationMs"]),
                    byte_size=destination.stat().st_size,
                    sha256=digest,
                    track_manifest=manifest,
                    equality_verified=kind == "multitrack_flac",
                )
            except BaseException:
                # Before atomic replacement every failure leaves a prior good
                # destination and its DB row untouched. If the DB upsert fails
                # after replacement, retain the verified file for retry/recovery.
                if not replaced:
                    temporary.unlink(missing_ok=True)
                raise

    @staticmethod
    def _pcm_wav_fingerprint(path: Path) -> dict[str, Any]:
        digest = hashlib.sha256()
        with wave.open(str(path), "rb") as source:
            if (
                source.getcomptype() != "NONE"
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != 16_000
            ):
                raise RuntimeError(
                    "Meeting archive source must be canonical 16-kHz mono s16le WAV."
                )
            sample_count = source.getnframes()
            if sample_count <= 0:
                raise RuntimeError("Meeting archive source contains no PCM samples.")
            remaining = sample_count
            while remaining > 0:
                frames = source.readframes(min(remaining, 64 * 1024))
                if not frames:
                    raise RuntimeError("Meeting archive source ended before its declared sample count.")
                digest.update(frames)
                remaining -= len(frames) // 2
        return {"sampleCount": sample_count, "pcmSha256": digest.hexdigest()}

    async def _decoded_pcm_fingerprint(
        self,
        ffmpeg: str,
        path: Path,
        *,
        stream_index: int,
    ) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(path),
            "-map", f"0:a:{stream_index}",
            "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        digest = hashlib.sha256()
        byte_count = 0

        async def drain_pcm() -> None:
            nonlocal byte_count
            if process.stdout is None:
                raise RuntimeError("Meeting PCM verification has no decoder output pipe.")
            while True:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    return
                digest.update(chunk)
                byte_count += len(chunk)

        pcm_task = asyncio.create_task(drain_pcm())
        stderr_task = asyncio.create_task(
            read_stream_limited(process.stderr, max_bytes=1024 * 1024)
        )
        try:
            _, stderr, _ = await asyncio.gather(
                pcm_task,
                stderr_task,
                process.wait(),
            )
        except BaseException:
            try:
                process.kill()
            except (ProcessLookupError, AttributeError):
                pass
            try:
                await process.wait()
            except Exception:
                pass
            for task in (pcm_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(pcm_task, stderr_task, return_exceptions=True)
            raise
        if process.returncode != 0:
            reason = (stderr or b"").decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"Meeting PCM equality decode failed: {reason}")
        if byte_count <= 0 or byte_count % 2:
            raise RuntimeError("Meeting PCM equality decode returned invalid s16le data.")
        return {"sampleCount": byte_count // 2, "pcmSha256": digest.hexdigest()}

    @staticmethod
    def _positive_seconds(value: Any) -> float | None:
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return seconds if seconds > 0.0 and seconds < float("inf") else None

    async def _verify_audio_asset(
        self,
        ffprobe: str,
        path: Path,
        *,
        expected_codec: str,
        expected_streams: int,
    ) -> dict[str, Any]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("Meeting audio consolidation produced an empty output.")
        process = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v", "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,sample_rate,channels,duration:format=duration",
            "-of", "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        stdout, stderr = await communicate_or_kill_on_cancel(
            process,
            max_stdout_bytes=1024 * 1024,
            max_stderr_bytes=1024 * 1024,
        )
        if process.returncode != 0:
            reason = (stderr or b"").decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"Meeting audio verification failed: {reason}")
        try:
            payload = json.loads((stdout or b"").decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Meeting audio verification returned invalid ffprobe JSON.") from exc
        raw_streams = payload.get("streams") if isinstance(payload, dict) else None
        audio_streams = [
            item for item in (raw_streams if isinstance(raw_streams, list) else [])
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ]
        if len(audio_streams) != expected_streams:
            raise RuntimeError(
                f"Meeting audio verification expected {expected_streams} stream(s), "
                f"found {len(audio_streams)}."
            )
        normalized_streams: list[dict[str, Any]] = []
        for expected_index, stream in enumerate(audio_streams):
            try:
                stream_index = int(stream.get("index"))
                sample_rate = int(stream.get("sample_rate"))
                channels = int(stream.get("channels"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("Meeting audio verification found invalid stream metadata.") from exc
            codec = str(stream.get("codec_name") or "").strip().lower()
            if stream_index != expected_index:
                raise RuntimeError("Meeting audio verification found an unexpected stream map order.")
            if codec != expected_codec:
                raise RuntimeError(
                    f"Meeting audio verification expected {expected_codec}, found {codec or 'unknown'}."
                )
            if channels != 1:
                raise RuntimeError("Meeting audio archive tracks must remain mono.")
            if codec == "flac" and sample_rate != 16_000:
                raise RuntimeError("Meeting FLAC archive tracks must remain at 16 kHz.")
            if codec == "opus" and sample_rate not in {16_000, 48_000}:
                raise RuntimeError("Meeting Opus playback reported an unsupported sample rate.")
            normalized_streams.append({
                "codec": codec,
                "sampleRate": sample_rate,
                "channels": channels,
            })
        format_info = payload.get("format") if isinstance(payload, dict) else None
        duration_seconds = self._positive_seconds(
            format_info.get("duration") if isinstance(format_info, dict) else None
        )
        if duration_seconds is None:
            stream_durations = [
                self._positive_seconds(item.get("duration")) for item in audio_streams
            ]
            duration_seconds = max(
                (value for value in stream_durations if value is not None),
                default=None,
            )
        if duration_seconds is None:
            raise RuntimeError("Meeting audio verification found no positive duration.")
        return {
            "durationMs": max(1, round(duration_seconds * 1000)),
            "streams": normalized_streams,
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _concatenate_wav(
        self,
        meeting_id: str,
        source: str,
        chunks: list[dict[str, Any]],
    ) -> tuple[Path, int, int]:
        destination_dir = self.audio_root / meeting_id / "final"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{source}.wav"
        temporary = destination.with_name(
            f".{destination.name}.{uuid4().hex}.part"
        )
        params: tuple[int, int, int] | None = None
        total_frames = 0
        timeline_origin_ms = min(int(chunk.get("startedAtMs", 0)) for chunk in chunks)
        timeline_cursor_ms = timeline_origin_ms
        try:
            with wave.open(str(temporary), "wb") as output:
                for chunk in chunks:
                    path = self.audio_root / str(chunk["relativePath"])
                    with wave.open(str(path), "rb") as source_wave:
                        current = (source_wave.getnchannels(), source_wave.getsampwidth(), source_wave.getframerate())
                        if params is None:
                            params = current
                            output.setnchannels(current[0])
                            output.setsampwidth(current[1])
                            output.setframerate(current[2])
                        elif params != current:
                            raise ValueError(f"Meeting {source} chunks do not share one PCM format.")
                        chunk_start_ms = int(chunk.get("startedAtMs", timeline_cursor_ms))
                        if chunk_start_ms > timeline_cursor_ms:
                            silence_frames = round((chunk_start_ms - timeline_cursor_ms) * current[2] / 1000)
                            output.writeframes(b"\0" * silence_frames * current[0] * current[1])
                            total_frames += silence_frames
                        frame_count = source_wave.getnframes()
                        output.writeframes(source_wave.readframes(frame_count))
                        total_frames += frame_count
                        timeline_cursor_ms = max(
                            int(chunk.get("endedAtMs", chunk_start_ms)),
                            chunk_start_ms + round(frame_count * 1000 / current[2]),
                        )
            if params is None or total_frames == 0:
                raise ValueError(f"Meeting {source} audio is empty.")
            temporary.replace(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return destination, round(total_frames * 1000 / params[2]), timeline_origin_ms

    @staticmethod
    def _segments_from_text(
        source: str, text: str, duration_ms: int, timeline_origin_ms: int = 0
    ) -> list[dict[str, Any]]:
        blocks: list[tuple[str, str]] = []
        pattern = re.compile(r"^\s*\[?(Speaker\s+\w+|[^\]\n:]{1,48})\]?\s*:\s*(.+)$", re.IGNORECASE)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = pattern.match(line)
            if match:
                blocks.append((match.group(1).strip(), match.group(2).strip()))
            else:
                blocks.append(("You" if source == "microphone" else "Meeting audio", line))
        if not blocks:
            return []
        weights = [max(1, len(block_text)) for _, block_text in blocks]
        total_weight = sum(weights)
        cursor = max(0, timeline_origin_ms)
        timeline_end_ms = cursor + duration_ms
        segments = []
        for index, ((label, block_text), weight) in enumerate(zip(blocks, weights, strict=True)):
            end = timeline_end_ms if index == len(blocks) - 1 else cursor + round(duration_ms * weight / total_weight)
            segments.append({
                "revision": "canonical", "source": source, "speakerLabel": label,
                "providerSegmentId": f"fallback-estimated-{index}",
                "startMs": cursor, "endMs": max(cursor, end), "text": block_text,
                "confidence": None, "alignmentQuality": "estimated", "isFinal": True,
            })
            cursor = end
        return segments

    @staticmethod
    def _cross_track_segments_are_echoes(
        item: dict[str, Any],
        candidate: dict[str, Any],
        *,
        normalized_item_text: str | None = None,
    ) -> bool:
        """Apply the original overlap and text-similarity echo thresholds."""
        normalized = normalized_item_text
        if normalized is None:
            normalized = re.sub(
                r"[^\w]+", " ", str(item.get("text", "")).lower()
            ).strip()
        item_start = int(item.get("startMs", 0))
        item_end = int(item.get("endMs", 0))
        candidate_start = int(candidate.get("startMs", 0))
        candidate_end = int(candidate.get("endMs", 0))
        overlap = max(
            0,
            min(item_end, candidate_end) - max(item_start, candidate_start),
        )
        item_duration = max(1, item_end - item_start)
        candidate_duration = max(1, candidate_end - candidate_start)
        if overlap / min(item_duration, candidate_duration) < 0.65:
            return False
        other = re.sub(
            r"[^\w]+", " ", str(candidate.get("text", "")).lower()
        ).strip()
        return SequenceMatcher(None, normalized, other).ratio() >= 0.92

    @staticmethod
    def _remove_cross_track_echoes(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop overlapping mic echoes with a timeline sweep; preserve input order."""
        # Event kind 0 (end) sorts before kind 1 (start), so touching but
        # non-overlapping intervals never enter the candidate comparison path.
        events: list[
            tuple[int, int, int, str, dict[str, Any], str]
        ] = []
        for index, item in enumerate(segments):
            source = str(item.get("source") or "")
            if source not in {"microphone", "system"}:
                continue
            start_ms = int(item.get("startMs", 0))
            end_ms = int(item.get("endMs", 0))
            if end_ms <= start_ms:
                continue
            normalized = ""
            if source == "microphone":
                normalized = re.sub(
                    r"[^\w]+", " ", str(item.get("text", "")).lower()
                ).strip()
                if len(normalized) < 8:
                    continue
            events.append((start_ms, 1, index, source, item, normalized))
            events.append((end_ms, 0, index, source, item, normalized))
        events.sort(key=lambda event: (event[0], event[1], event[2]))

        active_system: dict[int, dict[str, Any]] = {}
        active_microphones: dict[int, tuple[dict[str, Any], str]] = {}
        echo_indices: set[int] = set()
        for _timestamp, event_kind, item_index, source, item, normalized in events:
            if event_kind == 0:
                if source == "system":
                    active_system.pop(item_index, None)
                else:
                    active_microphones.pop(item_index, None)
                continue

            if source == "system":
                for microphone_index, (microphone, microphone_text) in (
                    active_microphones.items()
                ):
                    if (
                        microphone_index not in echo_indices
                        and MeetingFinalizer._cross_track_segments_are_echoes(
                            microphone,
                            item,
                            normalized_item_text=microphone_text,
                        )
                    ):
                        echo_indices.add(microphone_index)
                active_system[item_index] = item
                continue

            for candidate in active_system.values():
                if MeetingFinalizer._cross_track_segments_are_echoes(
                    item,
                    candidate,
                    normalized_item_text=normalized,
                ):
                    echo_indices.add(item_index)
                    break
            active_microphones[item_index] = (item, normalized)

        return [item for index, item in enumerate(segments) if index not in echo_indices]
