from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, current_thread

import pytest

from src import database
from src.data.meeting_store import (
    InvalidMeetingTransition,
    MeetingConflict,
    MeetingCreate,
    MeetingStore,
    VoiceLibraryDisabled,
)
from src.meeting_analysis import stable_analysis_item_id
from datetime import datetime, timedelta, timezone


@pytest.fixture()
def store(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "meetings.db")
    database.init_database()
    value = MeetingStore()
    value.initialize()
    yield value
    database._close_all_connections()


def create_request(**overrides):
    values = {"title": "Product sync", "consent_confirmed": True}
    values.update(overrides)
    return MeetingCreate(**values)


def test_origin_is_first_class_without_fabricating_consent(store: MeetingStore):
    captured = store.create(MeetingCreate(title="Private call"))
    assert captured["origin"] == "captured"
    assert captured["consentConfirmed"] is False
    store.transition(captured["id"], "discarded")
    imported = store.create(MeetingCreate(title="Imported call", origin="imported"))
    assert imported["origin"] == "imported"
    with pytest.raises(ValueError, match="origin"):
        store.create(MeetingCreate(title="Invalid", origin="unknown"))


def test_transcription_mode_is_first_class_and_validated(store: MeetingStore):
    meeting = store.create(
        MeetingCreate(title="Quiet capture", transcription_mode="final_only")
    )
    assert meeting["transcriptionMode"] == "final_only"
    assert store.get(meeting["id"])["transcriptionMode"] == "final_only"

    store.transition(meeting["id"], "discarded")
    with pytest.raises(ValueError, match="transcription mode"):
        store.create(MeetingCreate(title="Invalid mode", transcription_mode="minute_chunks"))


def test_full_reprocess_reservation_is_atomic_and_preserves_canonical_rows(
    store: MeetingStore,
):
    meeting = store.create(create_request(final_provider="soniox_async"))
    store.replace_segments(
        meeting["id"],
        "canonical",
        [
            {
                "id": "canonical-before-reprocess",
                "source": "system",
                "startMs": 100,
                "endMs": 900,
                "text": "Existing canonical result",
                "speakerLabel": "Remote",
            }
        ],
    )
    store.transition(meeting["id"], "finalizing")
    store.transition(meeting["id"], "ready")

    reserved = store.reserve_full_reprocess(
        meeting["id"],
        final_provider="deepgram",
        analysis_model="gpt-5-mini",
        voice_library_enabled=True,
    )

    assert reserved["state"] == "finalizing"
    assert reserved["finalProvider"] == "deepgram"
    assert reserved["analysisModel"] == "gpt-5-mini"
    assert reserved["voiceLibraryEnabled"] is True
    assert reserved["captureMetadata"]["reprocessKind"] == "full_transcript"
    assert reserved["captureMetadata"]["reprocessPreviousProvider"] == "soniox_async"
    assert store.detail(meeting["id"])["segments"][0]["text"] == (
        "Existing canonical result"
    )
    with pytest.raises(MeetingConflict, match="completed Meeting"):
        store.reserve_full_reprocess(
            meeting["id"],
            final_provider="soniox_async",
            analysis_model="",
            voice_library_enabled=False,
        )


def test_full_reprocess_can_restart_after_analysis_failure(store: MeetingStore):
    meeting = store.create(create_request(final_provider="soniox_async"))
    store.transition(meeting["id"], "finalizing")
    store.transition(meeting["id"], "analyzing")
    store.transition(
        meeting["id"],
        "analysis_failed",
        error_code="meeting_analysis_failed",
        error_message="Summary provider unavailable",
    )

    reserved = store.reserve_full_reprocess(
        meeting["id"],
        final_provider="deepgram",
        final_model="nova-3",
        analysis_model="gpt-5-mini",
        voice_library_enabled=False,
    )

    assert reserved["state"] == "finalizing"
    assert reserved["errorCode"] == ""
    assert reserved["errorMessage"] == ""
    assert reserved["captureMetadata"]["reprocessPreviousState"] == "analysis_failed"
    assert reserved["captureMetadata"]["reprocessFinalModel"] == "nova-3"


def test_changed_full_reprocess_transcript_marks_existing_outputs_stale_once(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    initial = [{
        "id": "canonical-initial",
        "source": "system",
        "speakerLabel": "Speaker 1",
        "startMs": 0,
        "endMs": 1_000,
        "text": "Original transcript",
        "alignmentQuality": "exact_word",
        "sequence": 0,
    }]
    store.replace_segments(meeting["id"], "canonical", initial)
    store.save_output(
        meeting["id"],
        kind="analysis",
        payload={"executiveSummary": "Original summary", "actionItems": []},
    )

    replacement = [{
        **initial[0],
        "id": "canonical-reprocessed",
        "text": "Improved transcript",
    }]
    store.replace_segments(
        meeting["id"],
        "canonical",
        replacement,
        advance_transcript_version_on_change=True,
    )
    first = store.detail(meeting["id"])

    assert first["transcriptEditVersion"] == 1
    assert first["outputs"][0]["transcriptEditVersion"] == 0

    # Retrying the same already-committed canonical result must not make the
    # stale boundary advance repeatedly.
    store.replace_segments(
        meeting["id"],
        "canonical",
        replacement,
        advance_transcript_version_on_change=True,
    )
    assert store.get(meeting["id"])["transcriptEditVersion"] == 1


def test_canonical_detail_hides_speakers_left_only_in_previous_or_live_revision(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [{
        "id": "live-old",
        "revision": "live",
        "source": "system",
        "speakerLabel": "Old live speaker",
        "startMs": 0,
        "endMs": 1_000,
        "text": "Preview",
    }])
    store.replace_segments(meeting["id"], "canonical", [{
        "id": "canonical-old",
        "source": "system",
        "speakerLabel": "Old canonical speaker",
        "startMs": 0,
        "endMs": 1_000,
        "text": "Old result",
    }])
    store.replace_segments(meeting["id"], "canonical", [{
        "id": "canonical-new",
        "source": "system",
        "speakerLabel": "New canonical speaker",
        "startMs": 0,
        "endMs": 1_000,
        "text": "New result",
    }])

    detail = store.detail(meeting["id"])

    assert [speaker["displayName"] for speaker in detail["speakers"]] == [
        "New canonical speaker"
    ]


def test_changed_full_reprocess_requires_speaker_participant_reconfirmation(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    first = store.replace_segments(meeting["id"], "canonical", [{
        "id": "canonical-before",
        "source": "system",
        "speakerLabel": "Speaker 1",
        "startMs": 0,
        "endMs": 1_000,
        "text": "Before",
    }])[0]
    store.assign_speaker_participant(
        meeting["id"],
        first["speakerId"],
        {"name": "Ada Example", "address": "ada@example.com"},
    )

    store.replace_segments(
        meeting["id"],
        "canonical",
        [{
            "id": "canonical-after",
            "source": "system",
            "speakerLabel": "Speaker 1",
            "startMs": 0,
            "endMs": 1_200,
            "text": "After",
        }],
        advance_transcript_version_on_change=True,
        reset_speaker_identity_on_change=True,
    )
    speaker = store.detail(meeting["id"])["speakers"][0]

    assert speaker["displayName"] == "Speaker 1"
    assert speaker["confirmedAttendee"] is None


def test_existing_meetings_migrate_to_live_and_final_mode(monkeypatch, tmp_path):
    database._close_all_connections()
    target = tmp_path / "legacy-transcription-mode.db"
    monkeypatch.setattr(database, "_DB_PATH", target)
    with sqlite3.connect(target) as conn:
        conn.executescript(
            """
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                state TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'auto',
                live_provider TEXT NOT NULL,
                final_provider TEXT NOT NULL,
                analysis_model TEXT NOT NULL DEFAULT '',
                aec_enabled INTEGER NOT NULL DEFAULT 1,
                voice_library_enabled INTEGER NOT NULL DEFAULT 0,
                consent_confirmed INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL DEFAULT 'captured',
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                capture_metadata_json TEXT NOT NULL DEFAULT '{}',
                audio_retention_days INTEGER NOT NULL DEFAULT 0,
                smart_turn_enabled INTEGER NOT NULL DEFAULT 1,
                auto_analyze INTEGER NOT NULL DEFAULT 1,
                transcript_edit_version INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO meetings (
                id,title,state,language,live_provider,final_provider,created_at,updated_at
            ) VALUES (
                '11111111111111111111111111111111','Legacy meeting','ready','auto',
                'soniox','soniox_async','2026-01-01T00:00:00+00:00','2026-01-01T01:00:00+00:00'
            );
            """
        )

    legacy_store = MeetingStore()
    legacy_store.initialize()

    assert legacy_store.get("1" * 32)["transcriptionMode"] == "live_final"
    with database._get_connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
    assert "transcription_mode" in columns
    database._close_all_connections()


def test_meeting_segment_fts_migration_restores_rowid_parity_and_scoped_search(
    store: MeetingStore,
):
    meeting_id = store.create(create_request())["id"]
    segment = store.add_segments(meeting_id, [{
        "revision": "live", "source": "microphone", "sequence": 0,
        "startMs": 10, "endMs": 1010, "text": "migration needle",
    }])[0]
    conn = database._get_connection()
    conn.executescript(
        """
        DROP TRIGGER meeting_segments_fts_insert;
        DROP TRIGGER meeting_segments_fts_update;
        DROP TRIGGER meeting_segments_fts_delete;
        DROP TABLE meeting_segments_fts;
        CREATE VIRTUAL TABLE meeting_segments_fts USING fts5(
            meeting_id UNINDEXED, segment_id UNINDEXED, revision UNINDEXED,
            text, speaker_label, tokenize='unicode61'
        );
        INSERT INTO meeting_segments_fts(
            rowid,meeting_id,segment_id,revision,text,speaker_label
        ) SELECT rowid+100,meeting_id,id,revision,text,speaker_label FROM meeting_segments;
        """
    )
    conn.execute(
        "UPDATE meeting_store_metadata SET value='1' WHERE key='meeting_segments_fts_schema_version'"
    )
    conn.commit()

    MeetingStore().initialize()

    base_rowid = conn.execute(
        "SELECT rowid FROM meeting_segments WHERE id=?", (segment["id"],)
    ).fetchone()[0]
    fts_rowid = conn.execute(
        "SELECT rowid FROM meeting_segments_fts WHERE segment_id=?", (segment["id"],)
    ).fetchone()[0]
    schema_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='meeting_segments_fts'"
    ).fetchone()[0]
    trigger_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='meeting_segments_fts_delete'"
    ).fetchone()[0]
    plan = conn.execute(
        """EXPLAIN QUERY PLAN SELECT 1 FROM meeting_segments s
           LEFT JOIN meeting_segments_fts f ON f.rowid=s.rowid
           WHERE f.rowid IS NULL LIMIT 1"""
    ).fetchall()

    assert fts_rowid == base_rowid
    assert "meeting_id, segment_id UNINDEXED, revision" in schema_sql
    assert "rowid=old.rowid" in trigger_sql.lower().replace(" ", "")
    assert any("VIRTUAL TABLE INDEX 0:=" in row[3] for row in plan)
    assert [item["id"] for item in store.search_segments(meeting_id, "needle")] == [segment["id"]]

    conn.execute("UPDATE meeting_segments SET text='updated token' WHERE id=?", (segment["id"],))
    conn.commit()
    assert store.search_segments(meeting_id, "needle") == []
    assert [item["id"] for item in store.search_segments(meeting_id, "updated")] == [segment["id"]]
    conn.execute("DELETE FROM meeting_segments WHERE id=?", (segment["id"],))
    conn.commit()
    assert conn.execute(
        "SELECT 1 FROM meeting_segments_fts WHERE rowid=?", (base_rowid,)
    ).fetchone() is None


def test_enforces_one_open_meeting_and_state_transitions(store: MeetingStore):
    meeting = store.create(create_request())
    assert meeting["state"] == "starting"
    with pytest.raises(MeetingConflict, match="already open"):
        store.create(create_request(title="Second call"))

    recording = store.transition(meeting["id"], "recording", capture_metadata={"captureId": "safe-id"})
    assert recording["startedAt"]
    assert recording["captureMetadata"] == {"captureId": "safe-id"}
    assert store.transition(meeting["id"], "paused")["state"] == "paused"
    assert store.transition(meeting["id"], "recording")["state"] == "recording"
    with pytest.raises(InvalidMeetingTransition):
        store.transition(meeting["id"], "ready")

    store.transition(meeting["id"], "stopping")
    store.transition(meeting["id"], "finalizing")
    assert store.transition(meeting["id"], "ready")["state"] == "ready"
    assert store.create(create_request(title="Next call"))["state"] == "starting"


def test_detail_validates_meeting_inside_its_read_snapshot(
    store: MeetingStore, monkeypatch: pytest.MonkeyPatch
):
    meeting = store.create(create_request())
    original_get = store.get
    get_calls = 0

    def counted_get(meeting_id: str):
        nonlocal get_calls
        get_calls += 1
        return original_get(meeting_id)

    monkeypatch.setattr(store, "get", counted_get)
    detail = store.detail(meeting["id"])

    assert detail["id"] == meeting["id"]
    assert detail["notes"] == []
    assert detail["actionItems"] == []
    assert detail["audioGaps"] == []
    assert detail["audioAssets"] == []
    assert detail["transcriptCheckpoints"] == []
    assert get_calls == 0


def test_detail_children_share_one_read_snapshot(store: MeetingStore, monkeypatch):
    meeting = store.create(create_request(title="old title"))
    meeting_id = meeting["id"]
    segment = store.add_segments(meeting_id, [{
        "revision": "live", "source": "microphone", "sequence": 0,
        "startMs": 0, "endMs": 1000, "text": "old segment",
    }])[0]
    selected = Event()
    release = Event()
    original_get_connection = database._get_connection

    class PausingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            selected.set()
            assert release.wait(timeout=3)
            return row

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class PausingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            cursor = self._connection.execute(sql, params)
            if sql.strip().startswith("SELECT * FROM meetings WHERE id"):
                return PausingCursor(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def get_connection():
        connection = original_get_connection()
        if current_thread().name.startswith("meeting-detail"):
            return PausingConnection(connection)
        return connection

    monkeypatch.setattr(database, "_get_connection", get_connection)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="meeting-detail") as executor:
        future = executor.submit(store.detail, meeting_id, revision="live")
        assert selected.wait(timeout=3)
        with sqlite3.connect(database._DB_PATH) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE meetings SET title='new title' WHERE id=?", (meeting_id,))
            writer.execute("UPDATE meeting_segments SET text='new segment' WHERE id=?", (segment["id"],))
            writer.commit()
        release.set()
        detail = future.result(timeout=3)

    assert detail["title"] == "old title"
    assert detail["segments"][0]["text"] == "old segment"


def test_state_transition_compare_and_swap_blocks_retry_discard_race(
    store: MeetingStore,
):
    meeting_id = store.create(create_request())["id"]
    store.transition(meeting_id, "finalizing")
    store.transition(meeting_id, "finalization_failed")
    first = MeetingStore()
    second = MeetingStore()
    barrier = Barrier(2)

    def gate_first_read(instance: MeetingStore) -> None:
        original_get = instance.get
        first_read = True

        def gated_get(value: str):
            nonlocal first_read
            current = original_get(value)
            if first_read:
                first_read = False
                barrier.wait(timeout=2)
            return current

        instance.get = gated_get  # type: ignore[method-assign]

    gate_first_read(first)
    gate_first_read(second)

    def transition(instance: MeetingStore, state: str):
        try:
            return instance.transition(meeting_id, state)["state"]
        except MeetingConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        retry = executor.submit(transition, first, "finalizing")
        discard = executor.submit(transition, second, "discarded")
        outcomes = {retry.result(timeout=5), discard.result(timeout=5)}

    assert "conflict" in outcomes
    assert len(outcomes) == 2
    assert store.get(meeting_id)["state"] in {"finalizing", "discarded"}


@pytest.mark.parametrize(
    ("failed_state", "transitions"),
    [
        ("finalization_failed", ("finalizing", "finalization_failed")),
        ("capture_failed", ("capture_failed",)),
        ("interrupted", ("interrupted",)),
    ],
)
def test_final_provider_retry_change_is_normalized_recoverable_and_rollbackable(
    store: MeetingStore,
    failed_state: str,
    transitions: tuple[str, ...],
):
    meeting_id = store.create(create_request(final_provider="soniox_async"))["id"]
    for state in transitions:
        store.transition(meeting_id, state)

    previous = store.change_final_provider_for_retry(
        meeting_id,
        "  DEEPGRAM_ASYNC  ",
        expected_state=failed_state,
        expected_final_provider=" SONIOX_ASYNC ",
        allowed_providers={"soniox_async", "deepgram_async"},
    )
    assert previous == "soniox_async"
    assert store.get(meeting_id)["finalProvider"] == "deepgram_async"

    replaced = store.change_final_provider_for_retry(
        meeting_id,
        previous,
        expected_state=failed_state,
        expected_final_provider="deepgram_async",
        allowed_providers={"soniox_async", "deepgram_async"},
    )
    assert replaced == "deepgram_async"
    assert store.get(meeting_id)["finalProvider"] == "soniox_async"


def test_full_reprocess_provider_retry_switches_and_restores_frozen_model(
    store: MeetingStore,
):
    meeting = store.create(create_request(final_provider="soniox_async"))
    store.transition(meeting["id"], "finalizing")
    store.transition(meeting["id"], "analyzing")
    store.transition(meeting["id"], "ready")
    store.reserve_full_reprocess(
        meeting["id"],
        final_provider="soniox_async",
        final_model="stt-async-v4",
        analysis_model="gpt-5-mini",
        voice_library_enabled=True,
    )
    store.transition(meeting["id"], "finalization_failed")

    previous = store.change_final_provider_for_retry(
        meeting["id"],
        "deepgram_async",
        expected_state="finalization_failed",
        expected_final_provider="soniox_async",
        allowed_providers={"soniox_async", "deepgram_async"},
        final_model="nova-3",
    )
    switched = store.get(meeting["id"])
    assert previous == "soniox_async"
    assert switched["finalProvider"] == "deepgram_async"
    assert switched["captureMetadata"]["reprocessFinalModel"] == "nova-3"

    replaced = store.change_final_provider_for_retry(
        meeting["id"],
        previous,
        expected_state="finalization_failed",
        expected_final_provider="deepgram_async",
        allowed_providers={"soniox_async", "deepgram_async"},
        final_model="stt-async-v4",
    )
    restored = store.get(meeting["id"])
    assert replaced == "deepgram_async"
    assert restored["finalProvider"] == "soniox_async"
    assert restored["captureMetadata"]["reprocessFinalModel"] == "stt-async-v4"


def test_full_reprocess_provider_retry_rejects_switch_without_frozen_model(
    store: MeetingStore,
):
    meeting = store.create(create_request(final_provider="soniox_async"))
    store.transition(meeting["id"], "finalizing")
    store.transition(meeting["id"], "ready")
    store.reserve_full_reprocess(
        meeting["id"],
        final_provider="soniox_async",
        final_model="stt-async-v4",
        analysis_model="gpt-5-mini",
        voice_library_enabled=True,
    )
    store.transition(meeting["id"], "finalization_failed")

    with pytest.raises(ValueError, match="requires its frozen model"):
        store.change_final_provider_for_retry(
            meeting["id"],
            "deepgram_async",
            expected_state="finalization_failed",
            expected_final_provider="soniox_async",
            allowed_providers={"soniox_async", "deepgram_async"},
        )

    unchanged = store.get(meeting["id"])
    assert unchanged["finalProvider"] == "soniox_async"
    assert unchanged["captureMetadata"]["reprocessFinalModel"] == "stt-async-v4"


def test_final_provider_retry_change_rejects_stale_provider_and_state(
    store: MeetingStore,
):
    meeting_id = store.create(create_request(final_provider="soniox_async"))["id"]
    store.transition(meeting_id, "capture_failed")
    store.change_final_provider_for_retry(
        meeting_id,
        "assemblyai",
        expected_state="capture_failed",
        expected_final_provider="soniox_async",
        allowed_providers={"soniox_async", "assemblyai", "deepgram_async"},
    )

    with pytest.raises(MeetingConflict, match="changed concurrently"):
        store.change_final_provider_for_retry(
            meeting_id,
            "deepgram_async",
            expected_state="capture_failed",
            expected_final_provider="soniox_async",
            allowed_providers={"soniox_async", "assemblyai", "deepgram_async"},
        )
    assert store.get(meeting_id)["finalProvider"] == "assemblyai"

    store.transition(meeting_id, "finalizing")
    with pytest.raises(MeetingConflict, match="state changed"):
        store.change_final_provider_for_retry(
            meeting_id,
            "deepgram_async",
            expected_state="capture_failed",
            expected_final_provider="assemblyai",
            allowed_providers={"assemblyai", "deepgram_async"},
        )
    assert store.get(meeting_id)["finalProvider"] == "assemblyai"


def test_final_provider_retry_change_enforces_caller_whitelist_without_mutation(
    store: MeetingStore,
):
    meeting_id = store.create(create_request(final_provider="soniox_async"))["id"]
    store.transition(meeting_id, "interrupted")

    with pytest.raises(ValueError, match="Unsupported final meeting"):
        store.change_final_provider_for_retry(
            meeting_id,
            "untrusted-provider",
            expected_state="interrupted",
            expected_final_provider="soniox_async",
            allowed_providers={"soniox_async", "deepgram_async"},
        )
    assert store.get(meeting_id)["finalProvider"] == "soniox_async"


def test_final_provider_retry_change_rejects_nonrecoverable_meeting_state(
    store: MeetingStore,
):
    meeting_id = store.create(create_request(final_provider="soniox_async"))["id"]

    with pytest.raises(MeetingConflict, match="recoverable failed Meeting state"):
        store.change_final_provider_for_retry(
            meeting_id,
            "deepgram_async",
            expected_state="starting",
            expected_final_provider="soniox_async",
            allowed_providers={"soniox_async", "deepgram_async"},
        )
    assert store.get(meeting_id)["finalProvider"] == "soniox_async"


def test_keeps_live_and_canonical_revisions_separate(store: MeetingStore):
    meeting_id = store.create(create_request())["id"]
    store.add_segments(meeting_id, [{
        "revision": "live", "source": "microphone", "sequence": 0,
        "startMs": 100, "endMs": 900, "text": "rough words", "speakerLabel": "You",
    }])
    assert store.detail(meeting_id)["segments"][0]["text"] == "rough words"
    store.add_segments(meeting_id, [{
        "revision": "canonical", "source": "mixed", "sequence": 0,
        "startMs": 100, "endMs": 900, "text": "Corrected words.", "speakerLabel": "Alex",
    }])
    assert store.detail(meeting_id)["segments"][0]["text"] == "Corrected words."
    assert store.detail(meeting_id, revision="live")["segments"][0]["text"] == "rough words"
    assert store.detail(meeting_id, revision="live")["segments"][0]["alignmentQuality"] == "estimated"

    detail = store.detail(meeting_id)
    speaker = detail["speakers"][-1]
    assert speaker["displayName"] == "Alex"
    assert store.rename_speaker(meeting_id, speaker["id"], "Alexandra") == 1
    renamed = store.detail(meeting_id)
    assert renamed["segments"][0]["speakerLabel"] == "Alexandra"


def test_append_live_segment_allocates_source_sequences_atomically(store: MeetingStore):
    meeting_id = store.create(create_request())["id"]
    barrier = Barrier(8)

    def append(index: int):
        barrier.wait(timeout=5)
        return store.append_live_segment(meeting_id, {
            "id": f"live-{index}",
            "source": "microphone",
            "startMs": index * 1_000,
            "endMs": index * 1_000 + 800,
            "text": f"Segment {index}",
            "speakerLabel": "You",
            "alignmentQuality": "provider_segment",
        })

    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(append, range(8)))

    assert sorted(row["sequence"] for row in rows) == list(range(8))
    persisted = store.detail(meeting_id, revision="live")["segments"]
    assert len(persisted) == 8
    assert sorted(row["sequence"] for row in persisted) == list(range(8))


def test_append_live_segment_is_idempotent_by_segment_id(store: MeetingStore):
    meeting_id = store.create(create_request())["id"]
    payload = {
        "id": "provider-final-1", "source": "system",
        "startMs": 200, "endMs": 900, "text": "Remote update",
        "speakerLabel": "Speaker 1",
    }
    first = store.append_live_segment(meeting_id, payload)
    repeated = store.append_live_segment(meeting_id, payload)

    assert repeated == first
    assert len(store.detail(meeting_id, revision="live")["segments"]) == 1


def test_transcript_corrections_are_versioned_searchable_and_mark_outputs_stale(
    store: MeetingStore,
):
    meeting_id = store.create(create_request())["id"]
    store.replace_segments(meeting_id, "canonical", [{
        "id": "canonical-editable", "revision": "canonical", "source": "system",
        "sequence": 0, "startMs": 0, "endMs": 1_000,
        "text": "Ship on Thorsday", "speakerLabel": "Speaker 1",
    }])
    store.transition(meeting_id, "finalizing")
    store.transition(meeting_id, "ready")
    store.save_output(
        meeting_id, kind="analysis", payload={"actionItems": []}, provider="test-model"
    )

    edited = store.edit_segment(
        meeting_id,
        "canonical-editable",
        "Ship on Thursday",
        expected_edit_version=0,
    )

    assert edited["transcriptEditVersion"] == 1
    assert edited["outputsStale"] is True
    assert edited["segment"]["text"] == "Ship on Thursday"
    assert edited["segment"]["editVersion"] == 1
    assert store.search_segments(meeting_id, "Thursday")[0]["id"] == "canonical-editable"
    assert store.search_segments(meeting_id, "Thorsday") == []
    detail = store.detail(meeting_id)
    assert detail["transcriptEditVersion"] == 1
    assert detail["outputs"][0]["transcriptEditVersion"] == 0
    with pytest.raises(MeetingConflict, match="changed in another view"):
        store.edit_segment(
            meeting_id,
            "canonical-editable",
            "Ship next Thursday",
            expected_edit_version=0,
        )

    undone = store.undo_segment_edit(
        meeting_id, "canonical-editable", expected_edit_version=1
    )
    assert undone["transcriptEditVersion"] == 2
    assert undone["segment"]["text"] == "Ship on Thorsday"
    history = store.segment_edit_history(meeting_id, "canonical-editable")
    assert [item["operation"] for item in history] == ["undo", "edit"]


def test_replace_segments_atomically_removes_stale_canonical_tail_and_fts_rows(
    store: MeetingStore,
):
    meeting_id = store.create(create_request())["id"]
    store.replace_segments(meeting_id, "canonical", [
        {
            "id": "canonical-0", "revision": "canonical", "source": "system", "sequence": 0,
            "startMs": 0, "endMs": 1_000, "text": "Current first turn",
        },
        {
            "id": "canonical-1", "revision": "canonical", "source": "system", "sequence": 1,
            "startMs": 1_000, "endMs": 2_000, "text": "obsolete tail marker",
        },
    ])

    replaced = store.replace_segments(meeting_id, "canonical", [{
        "id": "canonical-retry-0", "revision": "canonical", "source": "system", "sequence": 0,
        "startMs": 0, "endMs": 800, "text": "Shorter retry result",
    }])

    assert [item["id"] for item in replaced] == ["canonical-retry-0"]
    assert [item["id"] for item in store.detail(meeting_id)["segments"]] == [
        "canonical-retry-0"
    ]
    assert store.search_segments(meeting_id, "obsolete tail marker") == []


def test_replace_segments_rolls_back_delete_when_replacement_is_invalid(store: MeetingStore):
    meeting_id = store.create(create_request())["id"]
    store.replace_segments(meeting_id, "canonical", [{
        "id": "canonical-safe", "revision": "canonical", "source": "system", "sequence": 0,
        "startMs": 0, "endMs": 1_000, "text": "Keep me",
    }])

    with pytest.raises(ValueError, match="alignment quality"):
        store.replace_segments(meeting_id, "canonical", [{
            "revision": "canonical", "source": "system", "sequence": 0,
            "startMs": 0, "endMs": 500, "text": "Invalid",
            "alignmentQuality": "unproven",
        }])

    assert [item["id"] for item in store.detail(meeting_id)["segments"]] == [
        "canonical-safe"
    ]


def test_notes_and_interrupted_recovery_are_durable(store: MeetingStore):
    meeting = store.create(create_request(audio_retention_days=1))
    store.transition(meeting["id"], "recording")
    store.add_audio_chunk(
        meeting["id"], source="microphone", sequence=0,
        relative_path="recovered-mic.wav", started_at_ms=0, ended_at_ms=1_000,
        sha256="a" * 64,
    )
    assert store.add_note(meeting["id"], "Send the launch brief", at_ms=12_500)["atMs"] == 12_500
    assert store.notes(meeting["id"])[0]["body"] == "Send the launch brief"
    assert store.recover_interrupted() == 1
    recovered = store.get(meeting["id"])
    assert recovered["state"] == "interrupted"
    assert recovered["errorCode"] == "process_interrupted"
    assert recovered["endedAt"]
    after_retention = datetime.fromisoformat(recovered["endedAt"]) + timedelta(days=2)
    assert store.expired_audio_meetings(now=after_retention) == [meeting["id"]]


def test_analysis_recovery_preserves_canonical_phase_for_analysis_only_retry(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    store.transition(meeting["id"], "finalizing")
    store.add_segments(meeting["id"], [{
        "id": "canonical-before-analysis-crash",
        "revision": "canonical",
        "source": "system",
        "sequence": 0,
        "startMs": 0,
        "endMs": 1_000,
        "text": "The durable canonical transcript remains available.",
        "speakerLabel": "Speaker 1",
        "alignmentQuality": "provider_segment",
        "isFinal": True,
    }])
    store.transition(meeting["id"], "analyzing")

    assert store.recover_interrupted() == 1
    recovered = store.get(meeting["id"])
    assert recovered["state"] == "analysis_failed"
    assert recovered["errorCode"] == "process_interrupted_during_analysis"
    assert store.detail(meeting["id"])["segments"][0]["id"] == (
        "canonical-before-analysis-crash"
    )


@pytest.mark.parametrize("phase", ["stopping", "finalizing"])
def test_finalization_recovery_never_reopens_capture(store: MeetingStore, phase: str):
    meeting = store.create(create_request())
    store.transition(meeting["id"], "recording")
    store.transition(meeting["id"], "stopping")
    if phase == "finalizing":
        store.transition(meeting["id"], "finalizing")

    assert store.recover_interrupted() == 1
    recovered = store.get(meeting["id"])
    assert recovered["state"] == "finalization_failed"
    assert recovered["errorCode"] == "process_interrupted_during_finalization"
    assert "saved audio is intact" in recovered["errorMessage"]


def test_delivery_persistence_keeps_only_sanitized_request_metadata(store: MeetingStore):
    meeting = store.create(create_request())
    delivery = store.create_delivery(
        meeting["id"],
        kind="webhook",
        target="https://example.com/hooks/meeting",
        request_payload={"previewHash": "abc", "byteSize": 42},
    )
    updated = store.update_delivery(
        delivery["id"], status="delivered", response_payload={"httpStatus": 204}
    )
    assert updated["status"] == "delivered"
    assert updated["request"] == {"previewHash": "abc", "byteSize": 42}
    assert updated["response"] == {"httpStatus": 204}


def test_audio_gap_advances_resume_offset(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_audio_chunk(
        meeting["id"], source="microphone", sequence=0, relative_path="m.wav",
        started_at_ms=0, ended_at_ms=1_000,
    )
    store.add_audio_gap(
        meeting["id"], source="all", started_at_ms=1_000, ended_at_ms=1_750, reason="pause"
    )
    assert store.next_audio_offset_ms(meeting["id"], "microphone") == 1_750
    assert store.detail(meeting["id"])["audioGaps"][0]["reason"] == "pause"


def test_audio_checkpoint_atomically_snapshots_final_live_transcript(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [
        {"id": "live-early", "revision": "live", "source": "microphone", "sequence": 0,
         "startMs": 1_000, "endMs": 9_000, "text": "First durable statement.",
         "speakerLabel": "You", "alignmentQuality": "exact_word", "isFinal": True},
        {"id": "live-late", "revision": "live", "source": "system", "sequence": 0,
         "startMs": 34_000, "endMs": 42_000, "text": "Second durable statement.",
         "speakerLabel": "Speaker 1", "isFinal": True},
    ])

    for source in ("microphone", "system", "mic_clean"):
        store.add_audio_chunk(
            meeting["id"], source=source, sequence=0, relative_path=f"{source}-0.wav",
            started_at_ms=0, ended_at_ms=30_000, sha256=source * 8,
        )
    first = store.transcript_checkpoints(meeting["id"])[0]
    assert first["cutoffMs"] == 30_000
    assert first["segmentCount"] == 1
    assert first["sources"] == ["mic_clean", "microphone", "system"]
    assert store.detail(meeting["id"], revision="live")["segments"][0]["durationMs"] == 8_000

    store.add_audio_chunk(
        meeting["id"], source="microphone", sequence=1, relative_path="microphone-1.wav",
        started_at_ms=30_000, ended_at_ms=60_000, sha256="b" * 64,
    )
    second = store.transcript_checkpoints(meeting["id"])[1]
    # Microphone and system are independently durable. The fast microphone
    # track must not make the late system segment recoverable yet.
    assert second["cutoffMs"] == 30_000
    assert second["segmentCount"] == 1
    assert second["frontiers"]["logical"] == {
        "microphone": 60_000,
        "system": 30_000,
    }
    row = database._get_connection().execute(
        "SELECT snapshot_json,snapshot_sha256 FROM meeting_transcript_checkpoints WHERE meeting_id=? AND sequence=1",
        (meeting["id"],),
    ).fetchone()
    payload = json.loads(row["snapshot_json"])
    assert payload["schemaVersion"] == 3
    assert payload["kind"] == "delta"
    assert payload["baseSequence"] == 0
    assert payload["segments"] == []
    assert payload["totalSegmentCount"] == 1
    assert hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest() == row["snapshot_sha256"]

    store.add_audio_chunk(
        meeting["id"], source="system", sequence=1, relative_path="system-1.wav",
        started_at_ms=30_000, ended_at_ms=50_000, sha256="f" * 64,
    )
    completed_second = store.transcript_checkpoints(meeting["id"])[1]
    assert completed_second["cutoffMs"] == 50_000
    assert completed_second["segmentCount"] == 2
    updated_payload = json.loads(database._get_connection().execute(
        "SELECT snapshot_json FROM meeting_transcript_checkpoints WHERE meeting_id=? AND sequence=1",
        (meeting["id"],),
    ).fetchone()["snapshot_json"])
    assert [item["id"] for item in updated_payload["segments"]] == ["live-late"]


def test_checkpoint_failure_rolls_back_matching_audio_chunk(store: MeetingStore):
    meeting = store.create(create_request())
    with database._get_connection() as conn:
        conn.execute(
            """CREATE TRIGGER reject_checkpoint BEFORE INSERT ON meeting_transcript_checkpoints
               BEGIN SELECT RAISE(ABORT, 'checkpoint rejected'); END"""
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="checkpoint rejected"):
        store.add_audio_chunk(
            meeting["id"], source="system", sequence=0, relative_path="system-0.wav",
            started_at_ms=0, ended_at_ms=30_000, sha256="c" * 64,
        )
    assert store.audio_chunks(meeting["id"]) == []
    raw = database._get_connection().execute(
        "SELECT state FROM meeting_audio_chunks WHERE meeting_id=?", (meeting["id"],)
    ).fetchone()
    assert raw["state"] == "prepared"


def test_recovery_restores_missing_segments_from_latest_valid_checkpoint(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [
        {"id": "durable-live", "revision": "live", "source": "microphone", "sequence": 0,
         "startMs": 2_000, "endMs": 8_000, "text": "This survives the crash.",
         "speakerLabel": "You", "alignmentQuality": "provider_segment", "isFinal": True},
    ])
    store.add_audio_chunk(
        meeting["id"], source="microphone", sequence=0, relative_path="microphone-0.wav",
        started_at_ms=0, ended_at_ms=30_000, sha256="d" * 64,
    )
    with database._get_connection() as conn:
        conn.execute("DELETE FROM meeting_segments WHERE id='durable-live'")
        conn.commit()

    assert store.recover_interrupted() == 1
    restored = store.detail(meeting["id"], revision="live")["segments"]
    assert [(item["id"], item["text"]) for item in restored] == [
        ("durable-live", "This survives the crash.")
    ]
    assert restored[0]["alignmentQuality"] == "provider_segment"


def _build_checkpoint_timeline(
    store: MeetingStore, meeting_id: str, *, count: int
) -> None:
    store.add_segments(meeting_id, [
        {
            "id": f"long-{meeting_id[:6]}-{sequence:04d}",
            "revision": "live",
            "source": "microphone",
            "sequence": sequence,
            "startMs": sequence * 30_000 + 1_000,
            "endMs": sequence * 30_000 + 8_000,
            "text": f"Durable five-hour statement {sequence} with recovery evidence.",
            "speakerLabel": "You",
            "isFinal": True,
        }
        for sequence in range(count)
    ])
    for sequence in range(count):
        store.add_audio_chunk(
            meeting_id,
            source="microphone",
            sequence=sequence,
            relative_path=f"microphone-{sequence}.wav",
            started_at_ms=sequence * 30_000,
            ended_at_ms=(sequence + 1) * 30_000,
            sha256=f"{sequence:064x}"[-64:],
        )


def test_five_hour_checkpoint_payload_growth_is_bounded_and_recovers_tail(
    store: MeetingStore,
):
    shorter = store.create(create_request(title="Two and a half hours"))
    _build_checkpoint_timeline(store, shorter["id"], count=300)
    shorter_bytes = int(database._get_connection().execute(
        "SELECT SUM(length(snapshot_json)) FROM meeting_transcript_checkpoints WHERE meeting_id=?",
        (shorter["id"],),
    ).fetchone()[0])
    store.transition(shorter["id"], "discarded")

    long_meeting = store.create(create_request(title="Five hours"))
    _build_checkpoint_timeline(store, long_meeting["id"], count=600)
    conn = database._get_connection()
    long_bytes = int(conn.execute(
        "SELECT SUM(length(snapshot_json)) FROM meeting_transcript_checkpoints WHERE meeting_id=?",
        (long_meeting["id"],),
    ).fetchone()[0])
    latest = store.transcript_checkpoints(long_meeting["id"])[-1]

    assert latest["sequence"] == 599
    assert latest["cutoffMs"] == 18_000_000
    assert latest["segmentCount"] == 600
    assert long_bytes < shorter_bytes * 2.5

    with conn:
        conn.execute("DELETE FROM meeting_segments WHERE meeting_id=?", (long_meeting["id"],))
    store.recover_interrupted()
    restored = store.detail(long_meeting["id"], revision="live")["segments"]
    assert len(restored) == 600
    assert restored[-1]["endMs"] == 17_978_000


def test_delta_checkpoint_uses_redundant_base_when_latest_base_is_corrupt(
    store: MeetingStore,
):
    meeting = store.create(create_request(title="Redundant recovery"))
    _build_checkpoint_timeline(store, meeting["id"], count=26)
    conn = database._get_connection()
    latest_payload = json.loads(conn.execute(
        "SELECT snapshot_json FROM meeting_transcript_checkpoints WHERE meeting_id=? AND sequence=25",
        (meeting["id"],),
    ).fetchone()["snapshot_json"])
    assert latest_payload["baseSequence"] == 20
    assert latest_payload["fallbackBaseSequence"] == 0

    with conn:
        conn.execute(
            "UPDATE meeting_transcript_checkpoints SET snapshot_json='corrupt' "
            "WHERE meeting_id=? AND sequence=20",
            (meeting["id"],),
        )
        conn.execute("DELETE FROM meeting_segments WHERE meeting_id=?", (meeting["id"],))

    store.recover_interrupted()
    restored = store.detail(meeting["id"], revision="live")["segments"]
    assert len(restored) == 26
    assert restored[-1]["id"].endswith("0025")


def test_recovery_uses_latest_commit_not_largest_stale_cutoff(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [
        {"id": "mic-late", "revision": "live", "source": "microphone", "sequence": 0,
         "startMs": 50_000, "endMs": 55_000, "text": "Durable microphone tail."},
        {"id": "system-safe", "revision": "live", "source": "system", "sequence": 0,
         "startMs": 10_000, "endMs": 15_000, "text": "Durable system start."},
        {"id": "system-unsafe", "revision": "live", "source": "system", "sequence": 1,
         "startMs": 21_000, "endMs": 25_000, "text": "Not durable yet."},
    ])
    # Microphone advances to sequence 1 before the first system chunk arrives.
    # Its sequence-1 checkpoint initially has a larger scalar cutoff but becomes
    # stale as soon as system is known to be durable only through 20 seconds.
    store.add_audio_chunk(
        meeting["id"], source="microphone", sequence=0, relative_path="mic-0.wav",
        started_at_ms=0, ended_at_ms=30_000, sha256="1" * 64,
    )
    store.add_audio_chunk(
        meeting["id"], source="microphone", sequence=1, relative_path="mic-1.wav",
        started_at_ms=30_000, ended_at_ms=60_000, sha256="2" * 64,
    )
    store.add_audio_chunk(
        meeting["id"], source="system", sequence=0, relative_path="system-0.wav",
        started_at_ms=0, ended_at_ms=20_000, sha256="3" * 64,
    )
    with database._get_connection() as conn:
        conn.execute("DELETE FROM meeting_segments WHERE meeting_id=?", (meeting["id"],))
        conn.commit()

    assert store.recover_interrupted() == 1
    restored = store.detail(meeting["id"], revision="live")["segments"]
    assert {item["id"] for item in restored} == {"mic-late", "system-safe"}


def test_rejects_unknown_alignment_provenance(store: MeetingStore):
    meeting = store.create(create_request())

    with pytest.raises(ValueError, match="alignment quality"):
        store.add_segments(meeting["id"], [{
            "revision": "live", "source": "system", "sequence": 0,
            "startMs": 0, "endMs": 1_000, "text": "Unsupported provenance",
            "alignmentQuality": "magic",
        }])


def test_migrates_legacy_segment_rows_to_conservative_estimated_quality(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "legacy-meetings.db")
    database.init_database()
    with database._get_connection() as conn:
        conn.execute(
            """CREATE TABLE meeting_segments (
               id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, revision TEXT NOT NULL,
               source TEXT NOT NULL, provider_segment_id TEXT NOT NULL DEFAULT '',
               speaker_id TEXT, speaker_label TEXT NOT NULL DEFAULT '', start_ms INTEGER NOT NULL,
               end_ms INTEGER NOT NULL, text TEXT NOT NULL, confidence REAL,
               is_final INTEGER NOT NULL DEFAULT 1, sequence INTEGER NOT NULL,
               created_at TEXT NOT NULL, UNIQUE(meeting_id,revision,source,sequence))"""
        )
        conn.execute(
            """INSERT INTO meeting_segments
               (id,meeting_id,revision,source,start_ms,end_ms,text,sequence,created_at)
               VALUES ('legacy-segment','legacy-meeting','live','system',0,1000,'Legacy',0,'now')"""
        )
        conn.commit()

    legacy_store = MeetingStore()
    legacy_store.initialize()

    row = database._get_connection().execute(
        "SELECT alignment_quality FROM meeting_segments WHERE id='legacy-segment'"
    ).fetchone()
    assert row["alignment_quality"] == "estimated"
    database._close_all_connections()


def test_migrates_checkpoint_frontiers_additively(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "legacy-checkpoints.db")
    database.init_database()
    with database._get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS meeting_transcript_checkpoints")
        conn.execute(
            """CREATE TABLE meeting_transcript_checkpoints (
               id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, sequence INTEGER NOT NULL,
               cutoff_ms INTEGER NOT NULL, segment_count INTEGER NOT NULL DEFAULT 0,
               sources_json TEXT NOT NULL DEFAULT '[]', snapshot_json TEXT NOT NULL DEFAULT '[]',
               snapshot_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
               UNIQUE(meeting_id,sequence))"""
        )
        conn.commit()

    legacy_store = MeetingStore()
    legacy_store.initialize()

    columns = {
        row["name"]
        for row in database._get_connection().execute(
            "PRAGMA table_info(meeting_transcript_checkpoints)"
        )
    }
    assert {"frontiers_json", "commit_ordinal"} <= columns
    database._close_all_connections()


def test_migrates_audio_track_manifest_additively_and_idempotently(monkeypatch, tmp_path):
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "legacy-audio-assets.db")
    database.init_database()
    with database._get_connection() as conn:
        conn.execute(
            """CREATE TABLE meeting_audio_assets (
               id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL,
               relative_path TEXT NOT NULL, codec TEXT NOT NULL DEFAULT '',
               sample_rate INTEGER, channels INTEGER, duration_ms INTEGER,
               byte_size INTEGER, sha256 TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
               UNIQUE(meeting_id,kind,relative_path))"""
        )
        conn.commit()

    legacy_store = MeetingStore()
    legacy_store.initialize()
    legacy_store.initialize()

    columns = {
        row["name"]
        for row in database._get_connection().execute(
            "PRAGMA table_info(meeting_audio_assets)"
        )
    }
    assert {"track_manifest_version", "track_manifest_json", "equality_verified"} <= columns
    database._close_all_connections()


def test_add_audio_asset_validates_manifest_and_stream_channel_semantics(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    manifest = [{
        "source": "system",
        "streamIndex": 0,
        "codec": "flac",
        "sampleRate": 16_000,
        "channels": 1,
        "timelineOriginMs": 0,
        "durationMs": 1_000,
        "sampleCount": 16_000,
        "pcmSha256": "c" * 64,
        "equalityVerified": True,
    }]
    asset = store.add_audio_asset(
        meeting["id"],
        kind="multitrack_flac",
        relative_path=f"{meeting['id']}/final/meeting-tracks.mka",
        codec="flac",
        sample_rate=16_000,
        channels=1,
        duration_ms=1_000,
        byte_size=128,
        sha256="a" * 64,
        track_manifest=manifest,
        equality_verified=True,
    )
    assert asset["trackManifestVersion"] == 2
    assert asset["trackManifest"] == manifest
    assert asset["equalityVerified"] is True

    with pytest.raises(ValueError, match="channels do not match"):
        store.add_audio_asset(
            meeting["id"],
            kind="invalid_channels",
            relative_path=f"{meeting['id']}/final/invalid.mka",
            codec="flac",
            sample_rate=16_000,
            # Two mono streams are not one two-channel stream.
            channels=2,
            duration_ms=1_000,
            byte_size=128,
            sha256="b" * 64,
            track_manifest=manifest,
        )

    for unsafe_path in ("C:drive-relative.mka", "folder//asset.mka", "folder/./asset.mka", "bad\0name.mka"):
        with pytest.raises(ValueError, match="relative and traversal-free"):
            store.add_audio_asset(
                meeting["id"],
                kind="unsafe_path",
                relative_path=unsafe_path,
                codec="flac",
                sample_rate=16_000,
                channels=1,
                duration_ms=1_000,
                byte_size=128,
                sha256="d" * 64,
                track_manifest=manifest,
            )


def test_recovery_ignores_corrupt_checkpoint_snapshot(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [
        {"id": "not-restored", "revision": "live", "source": "system", "sequence": 0,
         "startMs": 1_000, "endMs": 4_000, "text": "Corrupt copy", "isFinal": True},
    ])
    store.add_audio_chunk(
        meeting["id"], source="system", sequence=0, relative_path="system-0.wav",
        started_at_ms=0, ended_at_ms=30_000, sha256="e" * 64,
    )
    with database._get_connection() as conn:
        conn.execute("DELETE FROM meeting_segments WHERE id='not-restored'")
        conn.execute(
            "UPDATE meeting_transcript_checkpoints SET snapshot_json='[]' WHERE meeting_id=?",
            (meeting["id"],),
        )
        conn.commit()

    assert store.recover_interrupted() == 1
    assert store.detail(meeting["id"], revision="live")["segments"] == []


def test_workspace_note_upsert_and_action_item_edits_survive_regeneration(store: MeetingStore):
    meeting = store.create(create_request())
    first = store.put_note(meeting["id"], "workspace", "First draft", at_ms=500)
    second = store.put_note(meeting["id"], "workspace", "Saved automatically", at_ms=750)
    assert first["createdAt"] == second["createdAt"]
    assert store.notes(meeting["id"])[0]["body"] == "Saved automatically"

    cleared = store.put_note(meeting["id"], "workspace", "", at_ms=800)
    assert cleared["body"] == ""
    assert store.notes(meeting["id"]) == []
    store.put_note(meeting["id"], "workspace", "Saved automatically", at_ms=850)

    analysis = {
        "actionItems": [{
            "id": "action-1", "text": "Send launch brief", "owner": None,
            "dueDate": None, "status": "open", "segmentIds": ["segment-1"],
        }, {
            "id": "action-2", "text": "Book the room", "owner": None,
            "dueDate": None, "status": "open", "segmentIds": ["segment-1"],
        }]
    }
    store.save_output(meeting["id"], kind="analysis", payload=analysis)
    edited = store.update_action_item(
        meeting["id"], "action-1", {"owner": "Alex", "status": "done"}
    )
    assert edited["owner"] == "Alex"
    assert edited["status"] == "done"
    assert edited["userModified"] is True

    analysis["actionItems"] = [{
        **analysis["actionItems"][0],
        "text": "Model replacement",
    }, {
        "id": "action-3", "text": "Publish notes", "owner": None,
        "dueDate": None, "status": "open", "segmentIds": ["segment-1"],
    }]
    store.save_output(meeting["id"], kind="analysis", payload=analysis)
    items = {item["id"]: item for item in store.detail(meeting["id"])["actionItems"]}
    persisted = items["action-1"]
    assert persisted["text"] == "Send launch brief"
    assert persisted["status"] == "done"
    assert persisted["provenance"] == "carried_user"
    assert {item["text"] for item in items.values()} == {
        "Send launch brief", "Model replacement", "Publish notes",
    }
    replacement = next(
        item for item in items.values() if item["text"] == "Model replacement"
    )
    assert replacement["id"] != "action-1"
    assert replacement["provenance"] == "automatic"
    assert items["action-3"]["provenance"] == "automatic"

    # A retry of the same analysis reuses the deterministic collision ID
    # instead of churning rows or creating another copy.
    store.save_output(meeting["id"], kind="analysis", payload=analysis)
    retried = store.detail(meeting["id"])["actionItems"]
    assert {item["id"] for item in retried} == set(items)
    assert [item["text"] for item in retried].count("Model replacement") == 1


def test_reanalysis_matches_edited_action_when_citations_expand_and_keeps_new_action(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    original_id = stable_analysis_item_id(
        "action", "Send launch brief", ["segment-1"]
    )
    store.save_output(meeting["id"], kind="analysis", payload={
        "actionItems": [{
            "id": original_id,
            "text": "Send launch brief",
            "owner": None,
            "dueDate": None,
            "status": "open",
            "segmentIds": ["segment-1"],
        }],
    })
    store.update_action_item(
        meeting["id"], original_id, {"owner": "Alex", "status": "done"}
    )

    expanded_id = stable_analysis_item_id(
        "action", "Send launch brief", ["segment-1", "segment-2"]
    )
    new_id = stable_analysis_item_id(
        "action", "Book customer review", ["segment-2"]
    )
    assert expanded_id != original_id
    store.save_output(meeting["id"], kind="analysis", payload={
        "actionItems": [{
            "id": new_id,
            "text": "Book customer review",
            "owner": None,
            "dueDate": None,
            "status": "open",
            "segmentIds": ["segment-2"],
        }, {
            "id": expanded_id,
            "text": "Send launch brief.",
            "owner": None,
            "dueDate": None,
            "status": "open",
            "segmentIds": ["segment-2", "segment-1"],
        }],
    })

    items = {item["id"]: item for item in store.detail(meeting["id"])["actionItems"]}
    assert set(items) == {original_id, new_id}
    assert items[original_id]["owner"] == "Alex"
    assert items[original_id]["status"] == "done"
    assert items[original_id]["provenance"] == "carried_user"
    assert items[original_id]["segmentIds"] == ["segment-1", "segment-2"]
    assert items[new_id]["provenance"] == "automatic"


def test_analysis_output_and_action_items_roll_back_atomically(
    store: MeetingStore, monkeypatch
):
    meeting = store.create(create_request())
    first = {
        "executiveSummary": "First",
        "actionItems": [{
            "id": "action-1", "text": "Keep this", "owner": None,
            "dueDate": None, "status": "open", "segmentIds": [],
        }],
    }
    store.save_output(meeting["id"], kind="analysis", payload=first)

    def fail_sync(*_args, **_kwargs):
        raise RuntimeError("injected action item write failure")

    monkeypatch.setattr(store, "_sync_action_items_conn", fail_sync)
    with pytest.raises(RuntimeError, match="injected action item write failure"):
        store.save_output(
            meeting["id"],
            kind="analysis",
            payload={"executiveSummary": "Second", "actionItems": []},
        )

    detail = store.detail(meeting["id"])
    assert detail["outputs"][0]["payload"]["executiveSummary"] == "First"
    assert [item["id"] for item in detail["actionItems"]] == ["action-1"]


def test_meeting_fts_indexes_canonical_segments_and_returns_timeline_neighbors(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [
        {"id": "before", "revision": "canonical", "source": "mixed", "sequence": 0,
         "startMs": 0, "endMs": 5_000, "text": "We reviewed the agenda."},
        {"id": "match", "revision": "canonical", "source": "mixed", "sequence": 1,
         "startMs": 6_000, "endMs": 12_000, "text": "The launch date is September ninth."},
        {"id": "after", "revision": "canonical", "source": "mixed", "sequence": 2,
         "startMs": 13_000, "endMs": 18_000, "text": "Alex will notify the customer."},
    ])
    results = store.search_segments(meeting["id"], "launch date")
    assert [item["id"] for item in results] == ["before", "match", "after"]
    assert store.search_segments(meeting["id"], "unfindablephrase") == []


def test_meeting_fts_falls_back_to_live_revision_before_finalization(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [{
        "id": "live-search", "revision": "live", "source": "microphone", "sequence": 0,
        "startMs": 2_000, "endMs": 3_500, "text": "Checkpoint search is available live.",
        "speakerLabel": "You", "isFinal": True,
    }])
    results = store.search_segments(meeting["id"], "checkpoint search")
    assert [(item["id"], item["durationMs"]) for item in results] == [("live-search", 1_500)]


def test_voice_profile_matching_requires_two_independent_segments_before_auto_name(store: MeetingStore):
    first = store.create(create_request(title="First"))
    store.add_segments(first["id"], [
        {"id": "first-a", "revision": "canonical", "source": "system", "sequence": 0,
         "startMs": 0, "endMs": 3000, "text": "Hello", "speakerLabel": "Speaker 1"},
        {"id": "first-b", "revision": "canonical", "source": "system", "sequence": 1,
         "startMs": 3000, "endMs": 6000, "text": "Again", "speakerLabel": "Speaker 1"},
    ])
    speaker = store.detail(first["id"])["speakers"][0]
    vector = [0.0] * 256
    vector[0] = 1.0
    created = store.register_speaker_embedding(first["id"], speaker["id"], "first-a", vector)
    store.register_speaker_embedding(first["id"], speaker["id"], "first-b", vector)
    assert created["matched"] is False
    store.rename_speaker(first["id"], speaker["id"], "Taylor")
    store.transition(first["id"], "capture_failed")

    second = store.create(create_request(title="Second"))
    store.add_segments(second["id"], [
        {"id": "second-a", "revision": "canonical", "source": "system", "sequence": 0,
         "startMs": 0, "endMs": 3000, "text": "Hello", "speakerLabel": "Speaker A"},
        {"id": "second-b", "revision": "canonical", "source": "system", "sequence": 1,
         "startMs": 3000, "endMs": 6000, "text": "Again", "speakerLabel": "Speaker A"},
    ])
    second_speaker = store.detail(second["id"])["speakers"][0]
    first_match = store.register_speaker_embedding(
        second["id"], second_speaker["id"], "second-a", vector
    )
    assert first_match["profileId"] == created["profileId"]
    assert first_match["autoNamed"] is False
    second_match = store.register_speaker_embedding(
        second["id"], second_speaker["id"], "second-b", vector
    )
    assert second_match["autoNamed"] is True
    assert store.detail(second["id"])["segments"][0]["speakerLabel"] == "Taylor"


def test_confirmed_outlook_participant_is_atomic_and_manual_rename_clears_link(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    store.add_segments(
        meeting["id"],
        [{
            "id": "speaker-segment",
            "revision": "canonical",
            "source": "system",
            "sequence": 0,
            "startMs": 0,
            "endMs": 3_000,
            "text": "Hello",
            "speakerLabel": "Speaker 1",
        }],
    )
    speaker = store.detail(meeting["id"])["speakers"][0]

    assignment = store.assign_speaker_participant(
        meeting["id"],
        speaker["id"],
        {"name": "", "address": "person@example.com"},
        source="account",
    )
    assert assignment["confirmedAttendee"] == {
        "name": "person@example.com",
        "address": "person@example.com",
    }
    detail = store.detail(meeting["id"])
    assert detail["speakers"][0]["participantLinkSource"] == "account"
    assert detail["speakers"][0]["confirmedAttendee"]["address"] == "person@example.com"
    assert detail["segments"][0]["speakerLabel"] == "person@example.com"

    store.rename_speaker(meeting["id"], speaker["id"], "Shared microphone")
    renamed = store.detail(meeting["id"])
    assert renamed["speakers"][0]["confirmedAttendee"] is None
    assert renamed["speakers"][0]["participantLinkSource"] == ""
    assert renamed["segments"][0]["speakerLabel"] == "Shared microphone"


def test_removing_confirmed_participant_restores_anonymous_label(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [{
        "id": "remove-link", "revision": "canonical", "source": "system",
        "sequence": 0, "startMs": 0, "endMs": 1_000, "text": "Hi",
        "speakerLabel": "Speaker 2",
    }])
    speaker = store.detail(meeting["id"])["speakers"][0]
    store.assign_speaker_participant(
        meeting["id"], speaker["id"],
        {"name": "Márta", "address": "marta@example.com"}, source="llm",
    )
    removed = store.assign_speaker_participant(
        meeting["id"], speaker["id"], None, source="manual"
    )
    assert removed["confirmedAttendee"] is None
    assert store.detail(meeting["id"])["segments"][0]["speakerLabel"] == "Speaker 2"


def test_audio_retention_removes_only_audio_records(store: MeetingStore):
    meeting = store.create(create_request(audio_retention_days=1))
    store.add_segments(meeting["id"], [{
        "id": "keep-segment", "revision": "canonical", "source": "mixed", "sequence": 0,
        "startMs": 0, "endMs": 1000, "text": "Keep this transcript.",
    }])
    store.add_audio_chunk(
        meeting["id"], source="system", sequence=0, relative_path="old.wav",
        started_at_ms=0, ended_at_ms=1000,
    )
    store.transition(meeting["id"], "capture_failed")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with database._get_connection() as conn:
        conn.execute("UPDATE meetings SET ended_at=? WHERE id=?", (old, meeting["id"]))
        conn.commit()
    assert store.expired_audio_meetings() == [meeting["id"]]
    store.mark_audio_purged(meeting["id"], purged_at=datetime.now(timezone.utc).isoformat())
    detail = store.detail(meeting["id"])
    assert detail["segments"][0]["text"] == "Keep this transcript."
    assert store.audio_chunks(meeting["id"]) == []
    assert detail["captureMetadata"]["audioPurgedAt"]


def test_analysis_regeneration_keeps_immutable_superseded_version(store: MeetingStore):
    meeting = store.create(create_request())
    first = store.save_output(meeting["id"], kind="analysis", payload={"title": "First"})
    second = store.save_output(meeting["id"], kind="analysis", payload={"title": "Second"})

    detail = store.detail(meeting["id"])
    assert first["version"] == 1
    assert second["version"] == 2
    assert detail["outputs"][0]["payload"] == {"title": "Second"}
    assert detail["outputs"][0]["supersedesId"] == detail["outputVersions"][0]["id"]
    assert detail["outputVersions"][0]["payload"] == {"title": "First"}


def test_delivery_persists_idempotency_and_attempt_metadata(store: MeetingStore):
    meeting = store.create(create_request())
    delivery = store.create_delivery(
        meeting["id"], kind="webhook", target="https://example.com/hook",
        request_payload={"previewHash": "abc"}, status="sending",
    )
    assert delivery["idempotencyKey"] == delivery["id"]
    completed = store.update_delivery(
        delivery["id"], status="delivered", response_payload={"httpStatus": 204},
        attempt_count=2,
    )
    assert completed["payloadVersion"] == 1
    assert completed["attemptCount"] == 2


def test_voice_profiles_can_be_split_and_merged_without_losing_observations(store: MeetingStore):
    first = store.create(create_request())
    store.add_segments(first["id"], [{
        "id": "voice-first", "revision": "canonical", "source": "system", "sequence": 0,
        "speakerLabel": "Remote 1", "startMs": 0, "endMs": 3000, "text": "First sample",
    }])
    first_speaker = store.detail(first["id"])["speakers"][0]
    vector_a = [1.0] + [0.0] * 255
    initial = store.register_speaker_embedding(
        first["id"], first_speaker["id"], "voice-first", vector_a
    )
    store.rename_speaker_profile(initial["profileId"], "Alice")
    store.add_segments(first["id"], [{
        "id": "voice-first-confirmation", "revision": "canonical", "source": "system",
        "sequence": 1, "speakerLabel": "Remote 1", "startMs": 3200, "endMs": 6000,
        "text": "Second independent sample",
    }])
    store.register_speaker_embedding(
        first["id"], first_speaker["id"], "voice-first-confirmation", vector_a
    )
    auto_named = store.detail(first["id"])
    assert auto_named["speakers"][0]["displayName"] == "Alice"
    assert {segment["speakerLabel"] for segment in auto_named["segments"]} == {"Alice"}

    split = store.split_speaker_profile(first["id"], first_speaker["id"])
    assert split["oldProfileId"] == initial["profileId"]
    assert split["newProfileId"] != initial["profileId"]
    separated = store.detail(first["id"])
    assert separated["speakers"][0]["profileId"] == split["newProfileId"]
    assert separated["speakers"][0]["displayName"] == "Remote 1"
    assert {segment["speakerLabel"] for segment in separated["segments"]} == {"Remote 1"}
    store.transition(first["id"], "capture_failed")

    second = store.create(create_request())
    store.add_segments(second["id"], [{
        "id": "voice-second", "revision": "canonical", "source": "system", "sequence": 0,
        "speakerLabel": "Remote 2", "startMs": 0, "endMs": 3000, "text": "Second sample",
    }])
    second_speaker = store.detail(second["id"])["speakers"][0]
    vector_b = [0.0, 1.0] + [0.0] * 254
    other = store.register_speaker_embedding(
        second["id"], second_speaker["id"], "voice-second", vector_b
    )

    merged = store.merge_speaker_profiles(split["newProfileId"], other["profileId"])
    assert merged["targetProfileId"] == split["newProfileId"]
    profiles = {item["id"]: item for item in store.speaker_profiles()}
    assert other["profileId"] not in profiles
    assert profiles[split["newProfileId"]]["sampleCount"] == 3
    assert store.detail(second["id"])["speakers"][0]["profileId"] == split["newProfileId"]


def test_voice_profile_can_be_named_and_updates_confident_linked_speakers(store: MeetingStore):
    meeting = store.create(create_request())
    store.add_segments(meeting["id"], [{
        "id": "named-voice", "revision": "canonical", "source": "system", "sequence": 0,
        "speakerLabel": "Remote 1", "startMs": 0, "endMs": 3000, "text": "Hello",
    }])
    speaker = store.detail(meeting["id"])["speakers"][0]
    registered = store.register_speaker_embedding(
        meeting["id"], speaker["id"], "named-voice", [1.0] + [0.0] * 255
    )

    renamed = store.rename_speaker_profile(registered["profileId"], "  Ada   Lovelace  ")

    assert renamed["displayName"] == "Ada Lovelace"
    profile = next(item for item in store.speaker_profiles() if item["id"] == registered["profileId"])
    assert profile["isNamed"] is True
    assert profile["displayName"] == "Ada Lovelace"
    detail = store.detail(meeting["id"])
    assert detail["speakers"][0]["displayName"] == "Ada Lovelace"
    assert detail["segments"][0]["speakerLabel"] == "Ada Lovelace"


def test_voice_reprocess_creates_neutral_microphone_speaker_and_safe_preselection(
    store: MeetingStore,
):
    vector = [1.0] + [0.0] * 255
    enrolled = store.enroll_speaker_profile("Alexander Immler", vector)
    meeting = store.create(create_request(title="Historical local microphone"))
    store.replace_segments(
        meeting["id"],
        "canonical",
        [
            {
                "id": "unlabeled-microphone",
                "revision": "canonical",
                "source": "microphone",
                "sequence": 0,
                "startMs": 0,
                "endMs": 4_000,
                "text": "Canonical text stays unchanged.",
            }
        ],
    )
    virtual_speaker_id = hashlib.sha256(
        f"{meeting['id']}\0canonical\0microphone\0you".encode("utf-8")
    ).hexdigest()[:32]

    result = store.rematch_speaker_embeddings(
        meeting["id"],
        [
            {
                "speakerId": virtual_speaker_id,
                "speakerLabel": "You",
                "segmentId": "unlabeled-microphone",
                "source": "microphone",
                "embedding": vector,
                "quality": 1.0,
            }
        ],
    )

    assert result["processedSpeakerCount"] == 1
    detail = store.detail(meeting["id"])
    assert detail["segments"][0]["speakerId"] == virtual_speaker_id
    assert detail["segments"][0]["speakerLabel"] == "You"
    assert detail["segments"][0]["text"] == "Canonical text stays unchanged."
    speaker = detail["speakers"][0]
    assert speaker["profileId"] == enrolled["id"]
    assert speaker["displayName"] == "You"
    assert speaker["voiceMatch"] == {
        "profileId": enrolled["id"],
        "displayName": "Alexander Immler",
        "confidence": pytest.approx(1.0),
        "evidenceCount": 1,
        "matchState": "suggested",
        "canPreselect": True,
        "requiresConfirmation": True,
    }


def test_voice_reprocess_rolls_back_virtual_speaker_when_segment_changed(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    store.replace_segments(
        meeting["id"],
        "canonical",
        [
            {
                "id": "actual-segment",
                "source": "microphone",
                "startMs": 0,
                "endMs": 3_000,
                "text": "Keep me untouched",
            }
        ],
    )
    virtual_speaker_id = hashlib.sha256(
        f"{meeting['id']}\0canonical\0microphone\0you".encode("utf-8")
    ).hexdigest()[:32]

    with pytest.raises(ValueError, match="changed"):
        store.rematch_speaker_embeddings(
            meeting["id"],
            [
                {
                    "speakerId": virtual_speaker_id,
                    "speakerLabel": "You",
                    "segmentId": "missing-segment",
                    "source": "microphone",
                    "embedding": [1.0] + [0.0] * 255,
                }
            ],
        )

    detail = store.detail(meeting["id"])
    assert detail["speakers"] == []
    assert detail["segments"][0]["speakerId"] is None
    assert detail["segments"][0]["speakerLabel"] == ""
    assert detail["segments"][0]["text"] == "Keep me untouched"


def test_learned_voice_preview_candidate_is_bounded_and_never_exposes_a_path(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    segment = store.replace_segments(
        meeting["id"],
        "canonical",
        [
            {
                "id": "preview-segment",
                "source": "system",
                "speakerLabel": "Remote",
                "startMs": 1_000,
                "endMs": 21_000,
                "text": "A long enough sample",
            }
        ],
    )[0]
    learned = store.register_speaker_embedding(
        meeting["id"],
        segment["speakerId"],
        segment["id"],
        [1.0] + [0.0] * 255,
    )
    store.transition(meeting["id"], "finalizing")
    store.transition(meeting["id"], "ready")
    with database._get_connection() as conn:
        conn.execute(
            """INSERT INTO meeting_audio_assets
               (id,meeting_id,kind,relative_path,codec,sample_rate,channels,
                duration_ms,byte_size,sha256,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "preview-asset",
                meeting["id"],
                "playback_system",
                f"{meeting['id']}/final/system.opus",
                "opus",
                16_000,
                1,
                21_000,
                128,
                "a" * 64,
                "2026-07-15T10:00:00Z",
            ),
        )
        conn.commit()

    candidate = store.speaker_profile_preview_candidates()[learned["profileId"]]

    assert candidate == {
        "profileId": learned["profileId"],
        "meetingId": meeting["id"],
        "source": "system",
        "startMs": 7_000,
        "endMs": 15_000,
        "durationMs": 8_000,
    }
    assert not any("path" in key.casefold() for key in candidate)


def test_explicit_voice_enrollment_creates_named_privacy_minimal_profile(
    store: MeetingStore,
):
    profile = store.enroll_speaker_profile(
        "  Ada   Lovelace  ", [1.0] + [0.0] * 255, quality=0.9
    )

    assert profile["displayName"] == "Ada Lovelace"
    assert profile["isNamed"] is True
    assert profile["enrolled"] is True
    assert profile["enrollmentSampleCount"] == 1
    assert profile["sampleCount"] == 1
    assert profile["enrolledAt"]
    forbidden = ("embedding", "audio", "path", "checksum")
    assert not any(term in key.lower() for key in profile for term in forbidden)
    public_profile = store.speaker_profiles()[0]
    assert public_profile == profile

    with database._get_connection() as conn:
        stored = conn.execute(
            """SELECT enrollment_embedding_blob,enrollment_sample_count
               FROM speaker_profiles WHERE id=?""",
            (profile["id"],),
        ).fetchone()
    assert len(stored["enrollment_embedding_blob"]) == 1_024
    assert stored["enrollment_sample_count"] == 1


def test_existing_voice_profiles_migrate_to_enrollment_schema_without_data_loss(
    monkeypatch, tmp_path
):
    database._close_all_connections()
    target = tmp_path / "legacy-voice-library.db"
    monkeypatch.setattr(database, "_DB_PATH", target)
    with sqlite3.connect(target) as conn:
        conn.executescript(
            """
            CREATE TABLE speaker_profiles (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                embedding_json TEXT NOT NULL DEFAULT '[]',
                embedding_blob BLOB,
                is_named INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO speaker_profiles (
                id,display_name,is_named,sample_count,created_at,updated_at
            ) VALUES (
                'legacy-profile','Legacy Alice',1,2,
                '2026-07-01T10:00:00+00:00','2026-07-01T11:00:00+00:00'
            );
            """
        )

    migrated_store = MeetingStore()
    migrated_store.initialize()

    profile = migrated_store.speaker_profiles()[0]
    assert profile["id"] == "legacy-profile"
    assert profile["displayName"] == "Legacy Alice"
    assert profile["sampleCount"] == 2
    assert profile["enrolled"] is False
    assert profile["enrollmentSampleCount"] == 0
    assert profile["enrolledAt"] == ""
    with database._get_connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(speaker_profiles)")}
    assert {
        "enrollment_embedding_blob",
        "enrollment_sample_count",
        "enrollment_weight_sum",
        "enrollment_resultant_norm",
        "enrolled_at",
    }.issubset(columns)
    database._close_all_connections()


def test_enrollment_seed_matches_meeting_voice_and_survives_meeting_deletion(
    store: MeetingStore,
):
    vector = [1.0] + [0.0] * 255
    profile = store.enroll_speaker_profile("Alice", vector)
    meeting = store.create(create_request())
    store.add_segments(
        meeting["id"],
        [
            {
                "id": "enrolled-a",
                "revision": "canonical",
                "source": "system",
                "sequence": 0,
                "speakerLabel": "Remote 1",
                "startMs": 0,
                "endMs": 3_000,
                "text": "First enrolled sample",
            },
            {
                "id": "enrolled-b",
                "revision": "canonical",
                "source": "system",
                "sequence": 1,
                "speakerLabel": "Remote 1",
                "startMs": 3_000,
                "endMs": 6_000,
                "text": "Second enrolled sample",
            },
        ],
    )
    speaker = store.detail(meeting["id"])["speakers"][0]

    first = store.register_speaker_embedding(
        meeting["id"], speaker["id"], "enrolled-a", vector
    )
    second = store.register_speaker_embedding(
        meeting["id"], speaker["id"], "enrolled-b", vector
    )

    assert first["profileId"] == profile["id"]
    assert first["matched"] is True
    assert first["autoNamed"] is False
    assert second["profileId"] == profile["id"]
    assert second["autoNamed"] is True
    assert store.detail(meeting["id"])["speakers"][0]["displayName"] == "Alice"
    assert store.speaker_profiles()[0]["sampleCount"] == 3

    assert store.delete(meeting["id"]) is True
    preserved = store.speaker_profiles()
    assert len(preserved) == 1
    assert preserved[0]["id"] == profile["id"]
    assert preserved[0]["enrolled"] is True
    assert preserved[0]["enrollmentSampleCount"] == 1
    assert preserved[0]["sampleCount"] == 1


def test_voice_profile_merge_combines_enrollment_seeds_without_exposing_them(
    store: MeetingStore,
):
    target = store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    target = store.enroll_speaker_profile(
        "Alice", [1.0] + [0.0] * 255, profile_id=target["id"]
    )
    source = store.enroll_speaker_profile("Alicia", [0.0, 1.0] + [0.0] * 254)

    result = store.merge_speaker_profiles(target["id"], source["id"])

    assert result == {
        "targetProfileId": target["id"],
        "mergedProfileId": source["id"],
    }
    profiles = store.speaker_profiles()
    assert len(profiles) == 1
    merged = profiles[0]
    assert merged["id"] == target["id"]
    assert merged["enrollmentSampleCount"] == 3
    assert merged["sampleCount"] == 3
    assert merged["enrolled"] is True
    assert not any("embedding" in key.lower() for key in merged)

    with database._get_connection() as conn:
        row = conn.execute(
            "SELECT embedding_blob FROM speaker_profiles WHERE id=?", (target["id"],)
        ).fetchone()
    centroid = store._embedding_values(row["embedding_blob"])
    assert centroid is not None
    assert centroid[0] == pytest.approx(2 / 5 ** 0.5)
    assert centroid[1] == pytest.approx(1 / 5 ** 0.5)


def test_voice_profile_split_keeps_explicit_seed_only_on_original_profile(
    store: MeetingStore,
):
    vector = [1.0] + [0.0] * 255
    enrolled = store.enroll_speaker_profile("Alice", vector)
    meeting = store.create(create_request())
    store.add_segments(
        meeting["id"],
        [
            {
                "id": "split-enrolled",
                "revision": "canonical",
                "source": "system",
                "sequence": 0,
                "speakerLabel": "Remote 1",
                "startMs": 0,
                "endMs": 3_000,
                "text": "This voice belongs elsewhere",
            }
        ],
    )
    speaker = store.detail(meeting["id"])["speakers"][0]
    linked = store.register_speaker_embedding(
        meeting["id"], speaker["id"], "split-enrolled", vector
    )
    assert linked["profileId"] == enrolled["id"]

    split = store.split_speaker_profile(meeting["id"], speaker["id"])
    profiles = {profile["id"]: profile for profile in store.speaker_profiles()}

    original = profiles[split["oldProfileId"]]
    separated = profiles[split["newProfileId"]]
    assert original["enrolled"] is True
    assert original["enrollmentSampleCount"] == 1
    assert original["sampleCount"] == 1
    assert separated["enrolled"] is False
    assert separated["enrollmentSampleCount"] == 0
    assert separated["sampleCount"] == 1


def test_voice_enrollment_centroid_is_order_independent_and_quality_weighted(
    store: MeetingStore,
):
    axis_a = [1.0] + [0.0] * 255
    axis_b = [0.0, 1.0] + [0.0] * 254

    first = store.enroll_speaker_profile("First", axis_a)
    store.enroll_speaker_profile("First", axis_b, profile_id=first["id"])
    store.enroll_speaker_profile("First", axis_b, profile_id=first["id"])

    second = store.enroll_speaker_profile("Second", axis_b)
    store.enroll_speaker_profile("Second", axis_a, profile_id=second["id"])
    store.enroll_speaker_profile("Second", axis_b, profile_id=second["id"])

    weighted = store.enroll_speaker_profile("Weighted", axis_a, quality=1.0)
    store.enroll_speaker_profile(
        "Weighted", axis_b, quality=0.35, profile_id=weighted["id"]
    )

    with database._get_connection() as conn:
        rows = {
            row["id"]: store._embedding_values(row["embedding_blob"])
            for row in conn.execute(
                "SELECT id,embedding_blob FROM speaker_profiles"
            ).fetchall()
        }

    expected = [1 / 5**0.5, 2 / 5**0.5] + [0.0] * 254
    assert rows[first["id"]] == pytest.approx(expected)
    assert rows[second["id"]] == pytest.approx(expected)
    weighted_norm = (1.0 + 0.35**2) ** 0.5
    assert rows[weighted["id"]][0] == pytest.approx(1.0 / weighted_norm)
    assert rows[weighted["id"]][1] == pytest.approx(0.35 / weighted_norm)


def test_voice_profile_sample_count_includes_observations_beyond_matching_window(
    store: MeetingStore,
):
    profile = store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    meeting = store.create(create_request())
    blob = store._embedding_blob([1.0] + [0.0] * 255)
    now = "2026-07-13T10:00:00+00:00"

    with database._get_connection() as conn:
        for index in range(25):
            conn.execute(
                """INSERT INTO speaker_profile_observations
                   (id,profile_id,meeting_id,segment_id,similarity,
                    embedding_blob,quality,created_at)
                   VALUES (?,?,?,NULL,?,?,?,?)""",
                (
                    f"observation-{index}",
                    profile["id"],
                    meeting["id"],
                    0.95,
                    blob,
                    0.9,
                    now,
                ),
            )
        store._recompute_speaker_profile_conn(conn, profile["id"], now)
        conn.commit()

    assert store.speaker_profiles()[0]["sampleCount"] == 26


def test_deleted_voice_library_blocks_late_finalizer_registration_until_reenabled(
    store: MeetingStore,
):
    meeting = store.create(create_request())
    store.add_segments(
        meeting["id"],
        [
            {
                "id": "late-finalizer-segment",
                "revision": "canonical",
                "source": "system",
                "sequence": 0,
                "speakerLabel": "Remote 1",
                "startMs": 0,
                "endMs": 3_000,
                "text": "Late finalizer sample",
            }
        ],
    )
    speaker = store.detail(meeting["id"])["speakers"][0]
    store.delete_all_speaker_profiles()

    skipped = store.register_speaker_embedding(
        meeting["id"],
        speaker["id"],
        "late-finalizer-segment",
        [1.0] + [0.0] * 255,
    )

    assert skipped["skipped"] == "voice_library_disabled"
    assert store.speaker_profiles() == []
    with pytest.raises(VoiceLibraryDisabled, match="Voice Library is turned off"):
        store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)

    store.set_speaker_library_enabled(True)
    profile = store.enroll_speaker_profile("Alice", [1.0] + [0.0] * 255)
    assert profile["displayName"] == "Alice"
