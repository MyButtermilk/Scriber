from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.data.meeting_import_store import (
    InvalidMeetingImportTransition,
    MAX_MEETING_IMPORT_INBOX_ITEMS,
    MeetingImportConflict,
    MeetingImportStatus,
    MeetingImportStore,
)


ORIGINAL_SHA = "a" * 64
NORMALIZED_SHA = "b" * 64


def _create(store: MeetingImportStore, import_id: str = "import-1"):
    return store.create(
        import_id=import_id,
        source_filename="Quarterly review.m4a",
        expected_bytes=1234,
        profile_snapshot={
            "id": "balanced",
            "finalProvider": "openai_async",
            "diarizationRoute": "sherpa_onnx_local",
        },
        metadata={"title": "Quarterly review"},
    )


def _receive(store: MeetingImportStore, import_id: str = "import-1"):
    store.begin_receiving(import_id)
    store.update_receive_progress(import_id, 600)
    return store.mark_received(
        import_id,
        relative_path=f"meetings/imports/{import_id}/original/Quarterly-review.m4a",
        byte_count=1234,
        sha256=ORIGINAL_SHA,
    )


def _prepare(store: MeetingImportStore, import_id: str = "import-1"):
    store.transition(import_id, MeetingImportStatus.PROBING)
    store.transition(import_id, MeetingImportStatus.PREPARING)
    return store.mark_prepared(
        import_id,
        relative_path=f"meetings/imports/{import_id}/prepared/system.wav",
        byte_count=48000,
        sha256=NORMALIZED_SHA,
        probe={"durationMs": 1000, "sampleRate": 16000, "channels": 1},
    )


def test_meeting_import_store_uses_runtime_database_path(monkeypatch, tmp_path):
    data_dir = tmp_path / "runtime"
    monkeypatch.setenv("SCRIBER_DATA_DIR", str(data_dir))
    monkeypatch.delenv("SCRIBER_DATABASE_PATH", raising=False)

    store = MeetingImportStore()

    assert store._db_path == data_dir / "transcripts.db"
    assert store._db_path.is_file()


def test_meeting_import_store_persists_complete_two_phase_lifecycle(tmp_path):
    db_path = tmp_path / "imports.db"
    store = MeetingImportStore(db_path=db_path)
    created = _create(store)

    assert created.status == MeetingImportStatus.CREATED
    assert created.profile_snapshot["finalProvider"] == "openai_async"
    received = _receive(store)
    assert received.status == MeetingImportStatus.RECEIVED
    assert received.received_bytes == 1234
    assert received.original_relative_path.startswith("meetings/imports/")
    assert received.original_sha256 == ORIGINAL_SHA

    waiting = _prepare(store)
    assert waiting.status == MeetingImportStatus.WAITING_FOR_WORKSPACE
    assert waiting.normalized_bytes == 48000
    assert waiting.probe["durationMs"] == 1000

    committing = store.transition(
        created.id,
        MeetingImportStatus.COMMITTING,
        meeting_id="meeting-42",
    )
    assert committing.meeting_id == "meeting-42"
    assert store.transition(created.id, MeetingImportStatus.FINALIZING).status == MeetingImportStatus.FINALIZING
    completed = store.transition(created.id, MeetingImportStatus.COMPLETED)
    assert completed.status == MeetingImportStatus.COMPLETED
    assert completed.finished_at

    store.close()
    reopened = MeetingImportStore(db_path=db_path)
    persisted = reopened.require(created.id)
    assert persisted == completed
    assert reopened.list_unfinished() == []


def test_public_serializer_uses_camel_case_and_detached_json_objects(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)

    payload = record.to_public()

    assert payload["sourceFilename"] == "Quarterly review.m4a"
    assert payload["expectedBytes"] == 1234
    assert payload["receivedBytes"] == 0
    assert payload["profileSnapshot"]["finalProvider"] == "openai_async"
    assert payload["meetingId"] is None
    assert payload["cancelRequested"] is False
    payload["profileSnapshot"]["finalProvider"] = "changed"
    assert record.profile_snapshot["finalProvider"] == "openai_async"


def test_meeting_import_store_requires_verified_artifacts_at_phase_boundaries(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    created = _create(store)
    store.begin_receiving(created.id)

    with pytest.raises(MeetingImportConflict, match="verified original"):
        store.transition(created.id, MeetingImportStatus.RECEIVED)

    _receive_after_begin(store, created.id)
    store.transition(created.id, MeetingImportStatus.PROBING)
    store.transition(created.id, MeetingImportStatus.PREPARING)
    with pytest.raises(MeetingImportConflict, match="normalized audio"):
        store.transition(created.id, MeetingImportStatus.WAITING_FOR_WORKSPACE)

    waiting = store.mark_prepared(
        created.id,
        relative_path="meetings/imports/import-1/prepared/system.wav",
        byte_count=42,
        sha256=NORMALIZED_SHA,
        probe={"durationMs": 1},
    )
    assert waiting.status == MeetingImportStatus.WAITING_FOR_WORKSPACE
    with pytest.raises(MeetingImportConflict, match="workspace ID"):
        store.transition(created.id, MeetingImportStatus.COMMITTING)


def _receive_after_begin(store: MeetingImportStore, import_id: str):
    return store.mark_received(
        import_id,
        relative_path=f"meetings/imports/{import_id}/original/source.m4a",
        byte_count=1234,
        sha256=ORIGINAL_SHA,
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "C:/Users/person/private.wav",
        "C:private.wav",
        "C:\\Users\\person\\private.wav",
        "/home/person/private.wav",
        "../outside.wav",
        "imports/../../outside.wav",
    ],
)
def test_meeting_import_store_rejects_non_relative_artifact_paths(tmp_path, relative_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    store.begin_receiving(record.id)

    with pytest.raises(ValueError, match="relative|normalized"):
        store.mark_received(
            record.id,
            relative_path=relative_path,
            byte_count=1234,
            sha256=ORIGINAL_SHA,
        )


def test_meeting_import_store_validates_hashes_filenames_and_json(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    with pytest.raises(ValueError, match="filename"):
        store.create(source_filename="folder/source.wav", profile_snapshot={"id": "x"})
    with pytest.raises(ValueError, match="JSON serializable"):
        store.create(
            source_filename="source.wav",
            profile_snapshot={"bad": object()},
        )

    record = _create(store)
    store.begin_receiving(record.id)
    with pytest.raises(ValueError, match="SHA-256"):
        store.mark_received(
            record.id,
            relative_path="meetings/imports/import-1/original/source.wav",
            byte_count=1234,
            sha256="not-a-hash",
        )


def test_receive_progress_is_monotonic_and_matches_declared_size(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    store.begin_receiving(record.id)
    assert store.update_receive_progress(record.id, 800).received_bytes == 800

    with pytest.raises(MeetingImportConflict, match="monotonic"):
        store.update_receive_progress(record.id, 700)
    with pytest.raises(MeetingImportConflict, match="exceeds"):
        store.update_receive_progress(record.id, 1235)
    with pytest.raises(MeetingImportConflict, match="does not match"):
        store.mark_received(
            record.id,
            relative_path="meetings/imports/import-1/original/source.wav",
            byte_count=1200,
            sha256=ORIGINAL_SHA,
        )


def test_cancel_is_durable_idempotent_and_blocks_forward_work(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    _receive(store)

    requested = store.request_cancel(record.id)
    assert requested.status == MeetingImportStatus.CANCEL_REQUESTED
    assert requested.cancel_requested is True
    assert requested.cancel_requested_at
    assert store.request_cancel(record.id) == requested
    assert store.list_cancel_requested() == [requested]

    with pytest.raises(InvalidMeetingImportTransition):
        store.transition(record.id, MeetingImportStatus.PROBING)

    canceled = store.mark_canceled(record.id)
    assert canceled.status == MeetingImportStatus.CANCELED
    assert canceled.finished_at
    assert store.request_cancel(record.id) == canceled


def test_cancel_and_worker_transition_are_serialized_without_lost_cancel(tmp_path):
    db_path = tmp_path / "imports.db"
    first = MeetingImportStore(db_path=db_path)
    second = MeetingImportStore(db_path=db_path)
    record = _create(first)
    _receive(first)
    barrier = Barrier(2)

    def advance():
        barrier.wait(timeout=2)
        try:
            return first.transition(
                record.id,
                MeetingImportStatus.PROBING,
                expected_status=MeetingImportStatus.RECEIVED,
            ).status
        except (MeetingImportConflict, InvalidMeetingImportTransition):
            return None

    def cancel():
        barrier.wait(timeout=2)
        return second.request_cancel(record.id).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        advance_result = executor.submit(advance)
        cancel_result = executor.submit(cancel)
        advance_result.result(timeout=5)
        assert cancel_result.result(timeout=5) == MeetingImportStatus.CANCEL_REQUESTED

    persisted = first.require(record.id)
    assert persisted.status == MeetingImportStatus.CANCEL_REQUESTED
    assert persisted.cancel_requested is True


@pytest.mark.parametrize(
    "status",
    [MeetingImportStatus.COMMITTING, MeetingImportStatus.FINALIZING],
)
def test_cancel_is_rejected_after_workspace_claim(tmp_path, status):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    _receive(store)
    _prepare(store)
    store.transition(
        record.id, MeetingImportStatus.COMMITTING, meeting_id="meeting-claimed"
    )
    if status == MeetingImportStatus.FINALIZING:
        store.transition(record.id, MeetingImportStatus.FINALIZING)

    with pytest.raises(MeetingImportConflict, match="workspace already owns"):
        store.request_cancel(record.id)

    assert store.require(record.id).status == status
    assert store.require(record.id).cancel_requested is False


def test_failure_cannot_override_an_accepted_cancel(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    _receive(store)
    store.request_cancel(record.id)

    persisted = store.mark_failed(
        record.id, error_code="worker_race", error_message="late worker failure"
    )

    assert persisted.status == MeetingImportStatus.CANCELED
    assert persisted.error_code == ""


def test_failed_committed_import_can_reopen_only_for_meeting_retry(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    committed = _create(store, "committed")
    _receive(store, committed.id)
    _prepare(store, committed.id)
    store.transition(
        committed.id, MeetingImportStatus.COMMITTING, meeting_id="meeting-retry"
    )
    store.transition(committed.id, MeetingImportStatus.FINALIZING)
    failed = store.mark_failed(
        committed.id, error_code="provider", error_message="temporary failure"
    )
    assert failed.finished_at

    reopened = store.transition(
        committed.id,
        MeetingImportStatus.FINALIZING,
        expected_status=MeetingImportStatus.FAILED,
    )
    assert reopened.finished_at == ""
    assert reopened.error_code == ""

    precommit = _create(store, "precommit")
    failed_precommit = store.mark_failed(
        precommit.id, error_code="bad_upload", error_message="invalid"
    )
    assert failed_precommit.meeting_id == ""
    with pytest.raises(MeetingImportConflict, match="verified original|workspace ID"):
        store.transition(precommit.id, MeetingImportStatus.FINALIZING)


@pytest.mark.parametrize("import_id", ["../escape", "folder/job", ".", "a" * 97])
def test_import_id_is_safe_for_owned_directory_names(tmp_path, import_id):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    with pytest.raises(ValueError, match="invalid characters"):
        _create(store, import_id)


@pytest.mark.parametrize("meeting_id", ["../escape", "folder/job", "C:/escape", "."])
def test_workspace_meeting_id_cannot_escape_data_root(tmp_path, meeting_id):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    _receive(store)
    _prepare(store)

    with pytest.raises(ValueError, match="meeting_id contains invalid"):
        store.transition(
            record.id, MeetingImportStatus.COMMITTING, meeting_id=meeting_id
        )


def test_recovery_queries_separate_upload_cleanup_resume_and_cancel(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    created = _create(store, "created")
    receiving = _create(store, "receiving")
    store.begin_receiving(receiving.id)

    received = _create(store, "received")
    _receive(store, received.id)
    probing = _create(store, "probing")
    _receive(store, probing.id)
    store.transition(probing.id, MeetingImportStatus.PROBING)

    cancel = _create(store, "cancel")
    store.request_cancel(cancel.id)
    failed = _create(store, "failed")
    store.mark_failed(failed.id, error_code="invalid_media", error_message="Invalid audio")

    assert {row.id for row in store.list_incomplete_uploads()} == {created.id, receiving.id}
    assert {row.id for row in store.list_recoverable()} == {received.id, probing.id}
    assert [row.id for row in store.list_cancel_requested()] == [cancel.id]
    assert {row.id for row in store.list_unfinished()} == {
        created.id,
        receiving.id,
        received.id,
        probing.id,
        cancel.id,
    }


def test_restart_inbox_prioritizes_active_jobs_and_bounds_recent_terminal_history(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    active_a = _create(store, "active-a")
    active_z = _create(store, "active-z")
    failed_a = _create(store, "failed-a")
    failed_z = _create(store, "failed-z")
    canceled = _create(store, "canceled-z")
    completed = _create(store, "completed-z")
    store.mark_failed(failed_a.id, error_code="decode", error_message="failed a")
    store.mark_failed(failed_z.id, error_code="decode", error_message="failed z")
    store.request_cancel(canceled.id)
    store.mark_canceled(canceled.id)
    _receive(store, completed.id)
    _prepare(store, completed.id)
    store.transition(completed.id, MeetingImportStatus.COMMITTING, meeting_id="meeting-complete")
    store.transition(completed.id, MeetingImportStatus.FINALIZING)
    store.transition(completed.id, MeetingImportStatus.COMPLETED)

    # Equal timestamps exercise the stable id tie-breaker rather than relying
    # on scheduler or filesystem timing.
    store._connect().execute(
        "UPDATE meeting_import_jobs SET updated_at = ?, created_at = ?",
        ("2026-07-12T12:00:00+00:00", "2026-07-12T11:00:00+00:00"),
    )

    rows = store.list_inbox(limit=4, recent_terminal_limit=1)

    assert [row.id for row in rows] == [active_z.id, active_a.id, failed_z.id]
    assert completed.id not in {row.id for row in rows}


def test_restart_inbox_enforces_hard_store_limit(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    for index in range(MAX_MEETING_IMPORT_INBOX_ITEMS + 5):
        _create(store, f"active-{index:03d}")

    rows = store.list_inbox(limit=10_000, recent_terminal_limit=10_000)

    assert len(rows) == MAX_MEETING_IMPORT_INBOX_ITEMS
    assert rows[0].id == f"active-{MAX_MEETING_IMPORT_INBOX_ITEMS + 4:03d}"


def test_find_by_meeting_id_prefers_active_then_newest_job(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    completed = _create(store, "completed")
    _receive(store, completed.id)
    _prepare(store, completed.id)
    store.transition(completed.id, MeetingImportStatus.COMMITTING, meeting_id="meeting-7")
    store.transition(completed.id, MeetingImportStatus.FINALIZING)
    store.transition(completed.id, MeetingImportStatus.COMPLETED)

    active = _create(store, "active")
    _receive(store, active.id)
    _prepare(store, active.id)
    store.transition(active.id, MeetingImportStatus.COMMITTING, meeting_id="meeting-7")

    assert store.find_by_meeting_id("meeting-7").id == active.id
    assert store.find_by_meeting_id("missing") is None
    assert store.find_by_meeting_id("") is None


def test_failed_state_preserves_redacted_machine_error_and_can_be_deleted(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    failed = store.mark_failed(
        record.id,
        error_code="unsupported_container",
        error_message="The recording could not be decoded.",
    )
    assert failed.status == MeetingImportStatus.FAILED
    assert failed.error_code == "unsupported_container"
    assert failed.finished_at
    assert store.delete(record.id) is True
    assert store.get(record.id) is None
    assert store.delete(record.id) is False


def test_nonterminal_state_cannot_be_deleted_or_skip_forward(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)

    with pytest.raises(MeetingImportConflict, match="terminal"):
        store.delete(record.id)
    with pytest.raises(InvalidMeetingImportTransition):
        store.transition(record.id, MeetingImportStatus.FINALIZING)


def test_store_creates_recovery_indexes(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    indexes = {
        row[1]
        for row in store._connect().execute("PRAGMA index_list(meeting_import_jobs)").fetchall()
    }

    assert "idx_meeting_import_jobs_status_updated" in indexes
    assert "idx_meeting_import_jobs_meeting_id" in indexes


def test_malformed_json_from_an_older_database_does_not_break_recovery(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    record = _create(store)
    store._connect().execute(
        "UPDATE meeting_import_jobs SET metadata_json = '{broken' WHERE id = ?",
        (record.id,),
    )

    assert store.require(record.id).metadata == {}


def test_close_reopens_thread_local_connection(tmp_path):
    store = MeetingImportStore(db_path=tmp_path / "imports.db")
    first = store._connect()
    store.close()
    second = store._connect()

    assert second is not first
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first.execute("SELECT 1")
