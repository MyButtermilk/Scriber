from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from src.data.transcript_artifact_store import (
    AlignmentQuality,
    ArtifactConflict,
    AttemptState,
    CanonicalSegmentDraft,
    RouteSnapshotDraft,
    SourceAssetState,
    StageUnit,
    TranscriptArtifactStore,
    UnsafeSnapshotValue,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _create_legacy_database(db_path: Path, *transcript_ids: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE transcripts (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                preview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'processing',
                step TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.executemany(
            "INSERT INTO transcripts (id, title, content, updated_at) VALUES (?, ?, ?, ?)",
            [(item, f"Title {item}", "old projection", "2026-01-01T00:00:00+00:00") for item in transcript_ids],
        )


@pytest.fixture
def artifact_store(tmp_path):
    db_path = tmp_path / "artifacts.db"
    _create_legacy_database(db_path, "transcript-1", "transcript-2")
    store = TranscriptArtifactStore(db_path)
    try:
        yield store
    finally:
        store.close()


def _route(workload: str = "file") -> RouteSnapshotDraft:
    return RouteSnapshotDraft(
        workload=workload,
        source_track="mix",
        provider="soniox_async",
        model="stt-async-v5",
        transport="webm_opus",
        language="de",
        response_shape="provider_segments",
        timestamp_mode="segment",
        diarization_mode="native_if_evidenced_else_local",
        parser_id="soniox-v5",
        parser_version="1",
        request_options={
            "speakerDiarizationRequested": True,
            "customVocabularySha256": SHA_A,
            "customVocabularyCount": 3,
            "apiKeyPresent": True,
            "secretDigest": SHA_B,
            "promptSha256": SHA_A,
            "contextBiasCount": 3,
        },
        local_worker_manifest={
            "workerVersion": "1.0.0",
            "workerSha256": SHA_A,
            "modelSha256": SHA_B,
        },
    )


def _advance_to_transcribing(
    store: TranscriptArtifactStore,
    *,
    transcript_id: str = "transcript-1",
    attempt_id: str | None = None,
):
    attempt = store.create_attempt(
        transcript_id=transcript_id,
        workload="file",
        attempt_id=attempt_id,
    )
    store.persist_route_snapshot(attempt.id, _route())
    attempt = store.transition_attempt(
        attempt.id,
        expected_state=AttemptState.QUEUED,
        expected_version=attempt.state_version,
        new_state=AttemptState.RESOLVING_SOURCE,
    )
    attempt = store.transition_attempt(
        attempt.id,
        expected_state=AttemptState.RESOLVING_SOURCE,
        expected_version=attempt.state_version,
        new_state=AttemptState.SOURCE_READY,
    )
    return store.transition_attempt(
        attempt.id,
        expected_state=AttemptState.SOURCE_READY,
        expected_version=attempt.state_version,
        new_state=AttemptState.TRANSCRIBING,
    )


def _persist_stage(store: TranscriptArtifactStore, attempt):
    return store.persist_stage_result(
        attempt.id,
        expected_version=attempt.state_version,
        transcript_text="Guten Morgen. Nächster Punkt.",
        units=[
            StageUnit(
                source_track="mix",
                start_ms=0,
                end_ms=1200,
                text="Guten Morgen.",
                speaker_key=0,
                timing_origin="provider",
                speaker_origin="provider_native",
                alignment_quality=AlignmentQuality.PROVIDER_SEGMENT,
            ),
            StageUnit(
                source_track="mix",
                start_ms=1500,
                end_ms=2800,
                text="Nächster Punkt.",
                speaker_key=1,
                timing_origin="provider",
                speaker_origin="provider_native",
                alignment_quality=AlignmentQuality.PROVIDER_SEGMENT,
            ),
        ],
        evidence={
            "nativeSpeakerIntervals": 2,
            "wordTimingEvidence": False,
            "parserFixtureDigest": SHA_A,
        },
    )


def _advance_to_committing(store: TranscriptArtifactStore, attempt):
    attempt = store.transition_attempt(
        attempt.id,
        expected_state=AttemptState.PROVIDER_RESULT_READY,
        expected_version=attempt.state_version,
        new_state=AttemptState.CANONICALIZING,
    )
    return store.transition_attempt(
        attempt.id,
        expected_state=AttemptState.CANONICALIZING,
        expected_version=attempt.state_version,
        new_state=AttemptState.COMMITTING,
    )


def _segments(first_text: str = "Guten Morgen."):
    return [
        CanonicalSegmentDraft(
            source_track="mix",
            start_ms=0,
            end_ms=1200,
            text=first_text,
            speaker_key=0,
            timing_origin="provider",
            speaker_origin="provider_native",
            alignment_quality="provider_segment",
        ),
        CanonicalSegmentDraft(
            source_track="mix",
            start_ms=1500,
            end_ms=2800,
            text="Nächster Punkt.",
            speaker_key=1,
            timing_origin="provider",
            speaker_origin="provider_native",
            alignment_quality="provider_segment",
        ),
    ]


def _ready_commit(store: TranscriptArtifactStore, *, transcript_id="transcript-1", attempt_id=None):
    attempt = _advance_to_transcribing(
        store, transcript_id=transcript_id, attempt_id=attempt_id
    )
    _, attempt = _persist_stage(store, attempt)
    return _advance_to_committing(store, attempt)


def test_schema_migrations_are_additive_and_idempotent(tmp_path):
    db_path = tmp_path / "schema.db"
    _create_legacy_database(db_path, "t")
    first = TranscriptArtifactStore(db_path)
    first.init_schema()
    first.close()
    second = TranscriptArtifactStore(db_path)
    second.init_schema()
    second.close()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "transcription_route_snapshots",
            "transcription_attempts",
            "transcription_stage_results",
            "transcription_track_stage_results",
            "transcription_track_derivations",
            "canonical_transcript_artifacts",
            "canonical_transcript_segments",
            "canonical_transcript_heads",
            "canonical_artifact_inputs",
            "canonical_transcript_segments_fts",
            "transcript_source_assets",
        }.issubset(tables)
        assert conn.execute("SELECT content FROM transcripts WHERE id = 't'").fetchone()[0] == "old projection"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("apiKey", "synthetic-secret-value"),
        ("authorization", "Bearer abcdefghijklmnop"),
        ("signedUrl", "https://storage.invalid/source?signature=secret"),
        ("customVocabulary", ["private customer name"]),
        ("phraseList", ["private customer name"]),
        ("contextBias", ["private customer name"]),
        ("terms", ["private customer name"]),
        ("prompt", "Transcribe private customer name accurately"),
        ("promptText", "Transcribe private customer name accurately"),
        ("modelPath", "C:/Users/Alexander/private/model.onnx"),
        ("modelFile", "models/private/model.onnx"),
        ("sourceUrl", "file:///C:/private/source.wav"),
    ],
)
def test_route_snapshot_rejects_secret_url_vocabulary_and_path_fields(
    artifact_store, field, value
):
    attempt = artifact_store.create_attempt(transcript_id="transcript-1", workload="file")
    safe = _route()
    draft = RouteSnapshotDraft(
        **{
            **safe.__dict__,
            "request_options": {**safe.request_options, field: value},
        }
    )

    with pytest.raises(UnsafeSnapshotValue):
        artifact_store.persist_route_snapshot(attempt.id, draft)

    with sqlite3.connect(artifact_store._db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transcription_route_snapshots").fetchone()[0] == 0


def test_route_snapshot_allows_only_digest_presence_and_count_secret_metadata(artifact_store):
    attempt = artifact_store.create_attempt(transcript_id="transcript-1", workload="file")
    snapshot = artifact_store.persist_route_snapshot(attempt.id, _route())
    assert snapshot.request_options["customVocabularySha256"] == SHA_A
    assert snapshot.request_options["apiKeyPresent"] is True
    assert snapshot.request_options["secretDigest"] == SHA_B

    with sqlite3.connect(artifact_store._db_path) as conn:
        persisted = " ".join(
            str(value or "")
            for value in conn.execute(
                "SELECT request_options_json, local_worker_manifest_json "
                "FROM transcription_route_snapshots"
            ).fetchone()
        )
    assert "synthetic-secret" not in persisted
    assert "C:/Users/" not in persisted
    assert "https://" not in persisted


def test_route_snapshot_is_immutable_but_identical_retry_is_idempotent(artifact_store):
    attempt = artifact_store.create_attempt(transcript_id="transcript-1", workload="file")
    first = artifact_store.persist_route_snapshot(attempt.id, _route(), snapshot_id="route-a")
    retry = artifact_store.persist_route_snapshot(attempt.id, _route(), snapshot_id="route-b")
    assert retry == first

    changed = RouteSnapshotDraft(**{**_route().__dict__, "model": "another-model"})
    with pytest.raises(ArtifactConflict, match="immutable"):
        artifact_store.persist_route_snapshot(attempt.id, changed)


def test_transcribing_requires_a_frozen_checksum_valid_route_snapshot(artifact_store):
    attempt = artifact_store.create_attempt(transcript_id="transcript-1", workload="file")
    attempt = artifact_store.transition_attempt(
        attempt.id,
        expected_state=AttemptState.QUEUED,
        expected_version=attempt.state_version,
        new_state=AttemptState.RESOLVING_SOURCE,
    )
    attempt = artifact_store.transition_attempt(
        attempt.id,
        expected_state=AttemptState.RESOLVING_SOURCE,
        expected_version=attempt.state_version,
        new_state=AttemptState.SOURCE_READY,
    )
    with pytest.raises(ArtifactConflict, match="snapshot"):
        artifact_store.transition_attempt(
            attempt.id,
            expected_state=AttemptState.SOURCE_READY,
            expected_version=attempt.state_version,
            new_state=AttemptState.TRANSCRIBING,
        )
    assert artifact_store.require_attempt(attempt.id).state == AttemptState.SOURCE_READY


def test_new_snapshot_after_transcribing_is_rejected_but_existing_retry_is_idempotent(
    artifact_store,
):
    attempt = _advance_to_transcribing(artifact_store, attempt_id="existing-snapshot")
    existing = artifact_store.get_route_snapshot(attempt.id)
    assert artifact_store.persist_route_snapshot(attempt.id, _route()) == existing

    invalid = artifact_store.create_attempt(
        transcript_id="transcript-2", workload="file", attempt_id="late-snapshot"
    )
    # The public state machine can no longer create this invalid state; emulate a
    # legacy/pre-migration row to prove the snapshot writer still refuses it.
    with sqlite3.connect(artifact_store._db_path) as conn:
        conn.execute(
            "UPDATE transcription_attempts SET state = ? WHERE id = ?",
            (AttemptState.TRANSCRIBING.value, invalid.id),
        )
    with pytest.raises(ArtifactConflict, match="before.*transcribing"):
        artifact_store.persist_route_snapshot(invalid.id, _route())


def test_attempt_transition_is_versioned_compare_and_swap_under_concurrency(tmp_path):
    db_path = tmp_path / "attempt-cas.db"
    _create_legacy_database(db_path, "transcript-1")
    first_store = TranscriptArtifactStore(db_path)
    second_store = TranscriptArtifactStore(db_path)
    attempt = first_store.create_attempt(
        transcript_id="transcript-1", workload="file", attempt_id="attempt-cas"
    )
    barrier = Barrier(2)

    def advance(store):
        barrier.wait()
        try:
            return store.transition_attempt(
                attempt.id,
                expected_state=AttemptState.QUEUED,
                expected_version=0,
                new_state=AttemptState.RESOLVING_SOURCE,
            ).state
        except ArtifactConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(advance, (first_store, second_store)))

    assert outcomes.count(AttemptState.RESOLVING_SOURCE) == 1
    assert outcomes.count("conflict") == 1
    assert first_store.require_attempt(attempt.id).state_version == 1
    first_store.close()
    second_store.close()


def test_attempt_lease_blocks_other_owner_and_expired_lease_can_be_taken_over(artifact_store):
    attempt = artifact_store.create_attempt(transcript_id="transcript-1", workload="file")
    leased = artifact_store.acquire_attempt_lease(
        attempt.id, owner="worker-a", expected_version=0, ttl_seconds=60
    )
    with pytest.raises(ArtifactConflict, match="owned"):
        artifact_store.acquire_attempt_lease(
            attempt.id,
            owner="worker-b",
            expected_version=leased.state_version,
            ttl_seconds=60,
        )

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(artifact_store._db_path) as conn:
        conn.execute(
            "UPDATE transcription_attempts SET lease_expires_at = ? WHERE id = ?",
            (past, attempt.id),
        )
    takeover = artifact_store.acquire_attempt_lease(
        attempt.id,
        owner="worker-b",
        expected_version=leased.state_version,
        ttl_seconds=60,
    )
    assert takeover.lease_owner == "worker-b"
    assert takeover.state_version == leased.state_version + 1
    released = artifact_store.release_attempt_lease(
        attempt.id, owner="worker-b", expected_version=takeover.state_version
    )
    assert released.lease_owner == ""
    assert released.lease_expires_at == ""
    assert released.state_version == takeover.state_version + 1


def test_attempt_lease_heartbeat_preserves_state_version(artifact_store):
    attempt = artifact_store.create_attempt(transcript_id="transcript-1", workload="file")
    leased = artifact_store.acquire_attempt_lease(
        attempt.id, owner="worker-a", expected_version=0, ttl_seconds=30
    )
    renewed = artifact_store.renew_attempt_lease(
        attempt.id,
        owner="worker-a",
        expected_version=leased.state_version,
        ttl_seconds=90,
    )
    assert renewed.state_version == leased.state_version
    assert renewed.lease_expires_at > leased.lease_expires_at
    with pytest.raises(ArtifactConflict, match="renewal CAS"):
        artifact_store.renew_attempt_lease(
            attempt.id,
            owner="worker-b",
            expected_version=leased.state_version,
            ttl_seconds=90,
        )


def test_stage_result_and_speaker_zero_survive_restart_without_provider_rerun(tmp_path):
    db_path = tmp_path / "recovery.db"
    _create_legacy_database(db_path, "transcript-1")
    store = TranscriptArtifactStore(db_path)
    attempt = _advance_to_transcribing(store)
    stage, attempt = _persist_stage(store, attempt)
    # Retrying after an ambiguous response returns the immutable row and does not
    # advance the attempt a second time.
    retry, retry_attempt = _persist_stage(store, attempt)
    assert retry == stage
    assert retry_attempt.state_version == attempt.state_version
    store.close()

    reopened = TranscriptArtifactStore(db_path)
    assert [item.attempt.id for item in reopened.list_recoverable_provider_results()] == [
        attempt.id
    ]
    assert reopened.latest_recoverable_for_transcript("transcript-1").attempt.id == attempt.id
    assert reopened.latest_recoverable_for_transcript("transcript-2") is None
    bundle = reopened.claim_recovery_bundle(
        attempt.id,
        owner="recovery-worker",
        expected_version=attempt.state_version,
    )
    assert bundle.attempt.state == AttemptState.PROVIDER_RESULT_READY
    assert bundle.attempt.lease_owner == "recovery-worker"
    assert bundle.stage_result.id == stage.id
    assert bundle.stage_result.units[0].speaker_key == "0"
    assert bundle.route_snapshot.model == "stt-async-v5"
    assert reopened.list_recoverable_provider_results() == ()
    reopened.close()


def test_track_stage_results_checkpoint_each_meeting_track_without_advancing_attempt(
    artifact_store,
):
    attempt = _advance_to_transcribing(artifact_store)
    mic = artifact_store.persist_track_stage_result(
        attempt.id,
        source_track="microphone",
        expected_version=attempt.state_version,
        transcript_text="Ich sende das Protokoll.",
        units=[StageUnit("microphone", 0, 800, "Ich sende das Protokoll.")],
        evidence={"nativeSpeakerEvidence": False},
    )
    assert mic.source_track == "microphone"
    assert artifact_store.require_attempt(attempt.id).state == AttemptState.TRANSCRIBING
    system = artifact_store.persist_track_stage_result(
        attempt.id,
        source_track="system",
        expected_version=attempt.state_version,
        transcript_text="Danke.",
        units=[StageUnit("system", 900, 1_200, "Danke.", speaker_key=0)],
        evidence={"nativeSpeakerEvidence": True},
    )
    assert [item.source_track for item in artifact_store.list_track_stage_results(attempt.id)] == [
        "microphone",
        "system",
    ]
    retry = artifact_store.persist_track_stage_result(
        attempt.id,
        source_track="system",
        expected_version=attempt.state_version,
        transcript_text="Danke.",
        units=[StageUnit("system", 900, 1_200, "Danke.", speaker_key=0)],
        evidence={"nativeSpeakerEvidence": True},
    )
    assert retry == system

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(artifact_store._db_path) as conn:
        conn.execute(
            "UPDATE transcription_attempts SET lease_owner = 'dead', lease_expires_at = ? WHERE id = ?",
            (past, attempt.id),
        )
    resumable = artifact_store.latest_resumable_track_attempt("transcript-1")
    assert resumable is not None
    assert resumable.attempt.id == attempt.id
    assert {item.id for item in resumable.track_results} == {mic.id, system.id}


def test_local_track_derivation_is_immutable_and_recovery_bound_to_parent(
    artifact_store,
):
    attempt = _advance_to_transcribing(artifact_store)
    system = artifact_store.persist_track_stage_result(
        attempt.id,
        source_track="system",
        expected_version=attempt.state_version,
        transcript_text="Guten Morgen.",
        units=[StageUnit("system", 100, 900, "Guten Morgen.")],
        evidence={"nativeSpeakerEvidence": False},
    )
    units = [
        StageUnit(
            "system",
            100,
            900,
            "Guten Morgen.",
            speaker_key="local-1",
            speaker_label="Speaker 1",
            speaker_origin="local_diarization",
            alignment_quality=AlignmentQuality.EXACT_WORD,
        )
    ]
    derived = artifact_store.persist_track_derivation(
        attempt.id,
        parent_stage_result_id=system.id,
        source_track="system",
        derivation_kind="local_speaker_diarization",
        expected_version=attempt.state_version,
        units=units,
        evidence={
            "engine": "sherpa-onnx",
            "parentStageResultSha256": system.result_sha256,
            "segmentCount": 1,
        },
    )
    retry = artifact_store.persist_track_derivation(
        attempt.id,
        parent_stage_result_id=system.id,
        source_track="system",
        derivation_kind="local_speaker_diarization",
        expected_version=attempt.state_version,
        units=units,
        evidence={
            "engine": "sherpa-onnx",
            "parentStageResultSha256": system.result_sha256,
            "segmentCount": 1,
        },
    )
    assert retry == derived
    with pytest.raises(ArtifactConflict, match="immutable"):
        artifact_store.persist_track_derivation(
            attempt.id,
            parent_stage_result_id=system.id,
            source_track="system",
            derivation_kind="local_speaker_diarization",
            expected_version=attempt.state_version,
            units=[StageUnit("system", 100, 900, "Changed")],
            evidence={"engine": "sherpa-onnx"},
        )

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(artifact_store._db_path) as conn:
        conn.execute(
            "UPDATE transcription_attempts SET lease_owner = 'dead', lease_expires_at = ? WHERE id = ?",
            (past, attempt.id),
        )
    resumable = artifact_store.latest_resumable_track_attempt("transcript-1")
    assert resumable is not None
    assert resumable.track_derivations == (derived,)

    with sqlite3.connect(artifact_store._db_path) as conn:
        conn.execute(
            "UPDATE transcription_track_derivations "
            "SET units_json = replace(units_json, 'Guten Morgen.', 'Manipuliert.') "
            "WHERE id = ?",
            (derived.id,),
        )
    with pytest.raises(ArtifactConflict, match="checksum"):
        artifact_store.list_track_derivations(attempt.id)


def test_stage_result_rejects_secret_evidence_before_it_is_persisted(artifact_store):
    attempt = _advance_to_transcribing(artifact_store)
    with pytest.raises(UnsafeSnapshotValue):
        artifact_store.persist_stage_result(
            attempt.id,
            expected_version=attempt.state_version,
            transcript_text="Text",
            units=[StageUnit("mix", 0, 1, "Text")],
            evidence={"apiKey": "synthetic-secret-must-never-enter-sqlite"},
        )
    assert artifact_store.require_attempt(attempt.id).state == AttemptState.TRANSCRIBING


def test_recovery_refuses_a_corrupted_normalized_stage_result(artifact_store):
    attempt = _advance_to_transcribing(artifact_store)
    _, attempt = _persist_stage(artifact_store, attempt)
    with sqlite3.connect(artifact_store._db_path) as conn:
        conn.execute(
            "UPDATE transcription_stage_results SET transcript_text = 'tampered' "
            "WHERE attempt_id = ?",
            (attempt.id,),
        )

    with pytest.raises(ArtifactConflict, match="checksum"):
        artifact_store.get_recovery_bundle(attempt.id)


def test_canonical_commit_updates_head_attempt_and_legacy_projection_atomically(artifact_store):
    attempt = _ready_commit(artifact_store)
    result = artifact_store.commit_canonical_artifact(
        attempt.id,
        expected_attempt_version=attempt.state_version,
        expected_head_generation=0,
        segments=_segments(),
    )
    assert result.committed is True
    assert result.superseded is False
    assert result.head.generation == 1
    assert result.attempt.state == AttemptState.COMPLETED
    assert result.artifact.segments[0].speaker_key == "0"
    assert result.artifact.segments[0].duration_ms == 1200

    with sqlite3.connect(artifact_store._db_path) as conn:
        content, status = conn.execute(
            "SELECT content, status FROM transcripts WHERE id = 'transcript-1'"
        ).fetchone()
        assert "[0:00] Speaker 0: Guten Morgen." in content
        assert status == "completed"
        assert conn.execute("SELECT COUNT(*) FROM canonical_artifact_inputs").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_transcript_segments_fts"
        ).fetchone()[0] == 2

    matches = artifact_store.search_canonical_segments("transcript-1", "nächster")
    assert len(matches) == 1
    assert matches[0].segment_id == result.artifact.segments[1].segment_id
    assert matches[0].start_ms == 1500


def test_canonical_commit_retry_is_idempotent(artifact_store):
    attempt = _ready_commit(artifact_store)
    first = artifact_store.commit_canonical_artifact(
        attempt.id,
        expected_attempt_version=attempt.state_version,
        expected_head_generation=0,
        segments=_segments(),
        artifact_id="artifact-one",
    )
    retry = artifact_store.commit_canonical_artifact(
        attempt.id,
        expected_attempt_version=attempt.state_version,
        expected_head_generation=0,
        segments=_segments(),
        artifact_id="ignored-retry-id",
    )
    assert retry.artifact.id == first.artifact.id
    assert retry.head == first.head
    with sqlite3.connect(artifact_store._db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM canonical_transcript_artifacts").fetchone()[0] == 1


def test_transcript_cascade_removes_canonical_segment_search_rows(artifact_store):
    attempt = _ready_commit(artifact_store)
    artifact_store.commit_canonical_artifact(
        attempt.id,
        expected_attempt_version=attempt.state_version,
        expected_head_generation=0,
        segments=_segments(),
    )
    with sqlite3.connect(artifact_store._db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM transcripts WHERE id = 'transcript-1'")
        assert conn.execute("SELECT COUNT(*) FROM canonical_transcript_artifacts").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_transcript_segments_fts"
        ).fetchone()[0] == 0


def test_concurrent_same_generation_commit_has_one_winner_and_supersedes_stale_loser(tmp_path):
    db_path = tmp_path / "head-cas.db"
    _create_legacy_database(db_path, "transcript-1")
    setup = TranscriptArtifactStore(db_path)
    first = _ready_commit(setup, attempt_id="attempt-a")
    second = _ready_commit(setup, attempt_id="attempt-b")
    assert first.expected_head_generation == second.expected_head_generation == 0
    setup.close()
    first_store = TranscriptArtifactStore(db_path)
    second_store = TranscriptArtifactStore(db_path)
    barrier = Barrier(2)

    def commit(store, attempt, artifact_id):
        barrier.wait()
        return store.commit_canonical_artifact(
            attempt.id,
            expected_attempt_version=attempt.state_version,
            expected_head_generation=0,
            segments=_segments(),
            artifact_id=artifact_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(commit, first_store, first, "artifact-a"),
            pool.submit(commit, second_store, second, "artifact-b"),
        ]
        results = [future.result() for future in futures]

    assert sum(item.committed for item in results) == 1
    assert sum(item.superseded for item in results) == 1
    assert {item.attempt.state for item in results} == {
        AttemptState.COMPLETED,
        AttemptState.SUPERSEDED,
    }
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM canonical_transcript_artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT generation FROM canonical_transcript_heads").fetchone()[0] == 1
    first_store.close()
    second_store.close()


def test_segment_ids_exclude_artifact_version_and_normalize_unicode_whitespace(artifact_store):
    first_attempt = _ready_commit(artifact_store, attempt_id="stable-a")
    first = artifact_store.commit_canonical_artifact(
        first_attempt.id,
        expected_attempt_version=first_attempt.state_version,
        expected_head_generation=0,
        segments=_segments("Café   Morgen."),
    )
    second_attempt = _ready_commit(artifact_store, attempt_id="stable-b")
    second = artifact_store.commit_canonical_artifact(
        second_attempt.id,
        expected_attempt_version=second_attempt.state_version,
        expected_head_generation=1,
        segments=_segments("Cafe\u0301 Morgen."),
    )
    assert first.artifact.generation == 1
    assert second.artifact.generation == 2
    assert [item.segment_id for item in first.artifact.segments] == [
        item.segment_id for item in second.artifact.segments
    ]


def test_duplicate_segment_collision_uses_occurrence_index_not_artifact_version(artifact_store):
    duplicate = _segments()[:1] * 2
    first = TranscriptArtifactStore.build_stable_segments(
        transcript_id="transcript-1", artifact_id="a", segments=duplicate
    )
    second = TranscriptArtifactStore.build_stable_segments(
        transcript_id="transcript-1", artifact_id="b", segments=duplicate
    )
    assert first[0].segment_id != first[1].segment_id
    assert [item.segment_id for item in first] == [item.segment_id for item in second]
    assert [item.occurrence_index for item in first] == [0, 1]


def test_fault_after_artifact_insert_rolls_back_head_projection_and_attempt(tmp_path):
    db_path = tmp_path / "rollback.db"
    _create_legacy_database(db_path, "transcript-1")

    def fail_after_rows(name, _conn):
        if name == "after_artifact_rows":
            raise RuntimeError("injected crash")

    store = TranscriptArtifactStore(db_path, fault_injector=fail_after_rows)
    attempt = _ready_commit(store)
    with pytest.raises(RuntimeError, match="injected crash"):
        store.commit_canonical_artifact(
            attempt.id,
            expected_attempt_version=attempt.state_version,
            expected_head_generation=0,
            segments=_segments(),
        )
    store.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM canonical_transcript_artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM canonical_transcript_heads").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_transcript_segments_fts"
        ).fetchone()[0] == 0
        assert conn.execute("SELECT content FROM transcripts").fetchone()[0] == "old projection"
        assert conn.execute(
            "SELECT state FROM transcription_attempts WHERE id = ?", (attempt.id,)
        ).fetchone()[0] == AttemptState.COMMITTING.value

    recovered = TranscriptArtifactStore(db_path)
    result = recovered.commit_canonical_artifact(
        attempt.id,
        expected_attempt_version=attempt.state_version,
        expected_head_generation=0,
        segments=_segments(),
    )
    assert result.committed
    recovered.close()


def test_source_asset_requires_durable_purge_pending_before_purged_and_hides_path(
    artifact_store,
):
    asset = artifact_store.add_source_asset(
        transcript_id="transcript-1",
        source_track="mix",
        asset_kind="uploaded_audio",
        purpose="processing_only",
        relative_path="jobs/file-1/source/audio.webm",
        sha256=SHA_A,
        byte_count=12345,
        asset_id="asset-1",
    )
    assert asset.state == SourceAssetState.AVAILABLE
    assert artifact_store.list_source_assets("transcript-1") == (asset,)
    assert "relativePath" not in asset.to_public()
    assert "jobs/file-1" not in str(asset.to_public())

    with pytest.raises(ArtifactConflict):
        artifact_store.mark_source_asset_purged(
            asset.id, expected_version=asset.state_version, tombstone_reason="retention"
        )
    pending = artifact_store.mark_source_asset_purge_pending(
        asset.id, expected_version=asset.state_version
    )
    assert artifact_store.list_source_assets_by_state(
        SourceAssetState.PURGE_PENDING, purpose="processing_only"
    ) == (pending,)
    assert artifact_store.list_source_assets_by_state(
        SourceAssetState.PURGE_PENDING, purpose="retained"
    ) == ()
    purged = artifact_store.mark_source_asset_purged(
        asset.id,
        expected_version=pending.state_version,
        tombstone_reason="canonical_commit_archive_verified",
    )
    assert purged.state == SourceAssetState.PURGED
    assert purged.relative_path == ""
    assert purged.tombstone_reason == "canonical_commit_archive_verified"
    assert purged.purged_at
    assert artifact_store.list_source_assets_by_state(SourceAssetState.PURGE_PENDING) == ()


@pytest.mark.parametrize(
    "path",
    [
        "C:/Users/person/source.wav",
        "C:private-source.wav",
        "C:\\Users\\person\\source.wav",
        "/home/person/source.wav",
        "../outside.wav",
        "assets/../../outside.wav",
        "assets/source\x00.wav",
        "https://storage.invalid/source.wav",
    ],
)
def test_source_assets_reject_absolute_or_escaping_local_paths(artifact_store, path):
    with pytest.raises(ValueError, match="relative|normalized|NUL|drive|UNC"):
        artifact_store.add_source_asset(
            transcript_id="transcript-1",
            source_track="mix",
            asset_kind="source",
            purpose="processing_only",
            relative_path=path,
            sha256=SHA_A,
            byte_count=1,
        )
