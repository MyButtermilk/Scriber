"""Persistent meeting workspace data model.

The meeting store deliberately keeps live transcription revisions separate from
the canonical post-meeting transcript. Audio capture and provider orchestration
sit above this module; all mutations here are short SQLite transactions so an
interrupted desktop process can recover the last durable state.
"""
from __future__ import annotations

import json
import hashlib
import math
import re
import shutil
import sqlite3
import struct
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable
from uuid import uuid4

from src import database as db


OPEN_STATES = frozenset({"starting", "recording", "paused", "stopping", "finalizing", "analyzing"})
TERMINAL_STATES = frozenset(
    {"ready", "capture_failed", "finalization_failed", "analysis_failed", "interrupted", "discarded"}
)
MEETING_STATES = OPEN_STATES | TERMINAL_STATES
ALIGNMENT_QUALITIES = frozenset({"exact_word", "provider_segment", "estimated"})
AUDIO_ASSET_TRACK_MANIFEST_VERSION = 2
AUDIO_ASSET_TRACK_SOURCES = frozenset({"microphone", "mic_clean", "system", "mixed"})
TRANSCRIPT_CHECKPOINT_SCHEMA_VERSION = 3
# A compact base every 10 minutes keeps work bounded. Delta checkpoints carry
# an additional previous-base tail, so one corrupt base row cannot invalidate
# the rest of a long meeting's recovery interval.
TRANSCRIPT_CHECKPOINT_BASE_INTERVAL = 20
FINAL_PROVIDER_RETRY_STATES = frozenset(
    {"finalization_failed", "capture_failed", "interrupted"}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "starting": frozenset({"recording", "finalizing", "capture_failed", "interrupted", "discarded"}),
    "recording": frozenset({"paused", "stopping", "capture_failed", "interrupted", "discarded"}),
    "paused": frozenset({"recording", "stopping", "capture_failed", "interrupted", "discarded"}),
    "stopping": frozenset({"finalizing", "capture_failed", "interrupted"}),
    "finalizing": frozenset({"analyzing", "ready", "finalization_failed", "interrupted"}),
    "analyzing": frozenset({"ready", "analysis_failed", "interrupted"}),
    "finalization_failed": frozenset({"finalizing", "discarded"}),
    "analysis_failed": frozenset({"analyzing", "ready", "discarded"}),
    "capture_failed": frozenset({"finalizing", "discarded"}),
    "interrupted": frozenset({"recording", "finalizing", "discarded"}),
    "ready": frozenset({"analyzing", "discarded"}),
    "discarded": frozenset(),
}


class MeetingStoreError(RuntimeError):
    """Base class for meeting persistence errors."""


class MeetingNotFound(MeetingStoreError):
    pass


class MeetingConflict(MeetingStoreError):
    pass


class InvalidMeetingTransition(MeetingStoreError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


_STABLE_ACTION_ID_RE = re.compile(r"action-[0-9a-f]{20}\Z")


def _action_semantic_key(value: Any) -> str:
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def _action_segment_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item)))


def _action_collision_id(
    preferred_id: str,
    text: str,
    segment_ids: list[str],
    *,
    salt: int = 0,
) -> str:
    """Resolve legacy/model ID collisions without dropping a new action."""
    citation_key = "\0".join(sorted(set(segment_ids)))
    digest = hashlib.sha256(
        (
            "meeting-action-collision-v1\0"
            f"{preferred_id}\0{_action_semantic_key(text)}\0{citation_key}\0{salt}"
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"action-{digest}"


def _action_user_match_score(incoming: dict[str, Any], existing: dict[str, Any]) -> float | None:
    """Score only strong evidence that a regenerated action is the edited row."""
    incoming_id = str(incoming["id"])
    existing_id = str(existing["id"])
    if incoming_id == existing_id and _STABLE_ACTION_ID_RE.fullmatch(incoming_id):
        # Content fields can legitimately differ because the user edited them;
        # a content-addressed ID remains the authoritative generated identity.
        return 10_000.0

    incoming_key = _action_semantic_key(incoming["text"])
    existing_key = _action_semantic_key(existing["text"])
    incoming_citations = set(incoming["segmentIds"])
    existing_citations = set(existing["segmentIds"])
    overlap = incoming_citations & existing_citations
    if incoming_key and incoming_key == existing_key:
        return 9_000.0 + len(overlap)
    if not incoming_key or not existing_key or not overlap:
        return None

    incoming_tokens = set(incoming_key.split())
    existing_tokens = set(existing_key.split())
    token_union = incoming_tokens | existing_tokens
    token_similarity = (
        len(incoming_tokens & existing_tokens) / len(token_union)
        if token_union else 0.0
    )
    citation_similarity = len(overlap) / max(
        1, min(len(incoming_citations), len(existing_citations))
    )
    if token_similarity < 0.45:
        return None
    return 1_000.0 + token_similarity * 100.0 + citation_similarity * 10.0


@dataclass(frozen=True)
class MeetingCreate:
    title: str
    language: str = "auto"
    live_provider: str = "soniox"
    final_provider: str = "soniox_async"
    analysis_model: str = ""
    aec_enabled: bool = True
    voice_library_enabled: bool = False
    consent_confirmed: bool = False
    origin: str = "captured"
    audio_retention_days: int = 0
    smart_turn_enabled: bool = True
    auto_analyze: bool = True
    capture_metadata: dict[str, Any] | None = None


class MeetingStore:
    """Normalized meeting persistence backed by Scriber's shared SQLite DB."""

    def initialize(self) -> None:
        with db._get_connection() as conn:  # shared WAL connection lifecycle
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meetings (
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
                    origin TEXT NOT NULL DEFAULT 'captured'
                        CHECK(origin IN ('captured','imported')),
                    started_at TEXT,
                    ended_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    capture_metadata_json TEXT NOT NULL DEFAULT '{}'
                    ,audio_retention_days INTEGER NOT NULL DEFAULT 0
                    ,smart_turn_enabled INTEGER NOT NULL DEFAULT 1
                    ,auto_analyze INTEGER NOT NULL DEFAULT 1
                    ,transcript_edit_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_meetings_created_at
                    ON meetings(created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_meetings_one_open
                    ON meetings((1))
                    WHERE state IN ('starting','recording','paused','stopping','finalizing','analyzing');

                CREATE TABLE IF NOT EXISTS meeting_audio_assets (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    codec TEXT NOT NULL DEFAULT '',
                    sample_rate INTEGER,
                    channels INTEGER,
                    duration_ms INTEGER,
                    byte_size INTEGER,
                    sha256 TEXT NOT NULL DEFAULT '',
                    track_manifest_version INTEGER NOT NULL DEFAULT 0,
                    track_manifest_json TEXT NOT NULL DEFAULT '[]',
                    equality_verified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(meeting_id, kind, relative_path)
                );
                CREATE TABLE IF NOT EXISTS meeting_audio_chunks (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'complete',
                    sha256 TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(meeting_id, source, sequence)
                );
                CREATE TABLE IF NOT EXISTS meeting_audio_gaps (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_meeting_audio_gaps_timeline
                    ON meeting_audio_gaps(meeting_id, started_at_ms);
                CREATE TABLE IF NOT EXISTS meeting_transcript_checkpoints (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    cutoff_ms INTEGER NOT NULL,
                    segment_count INTEGER NOT NULL DEFAULT 0,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    frontiers_json TEXT NOT NULL DEFAULT '{}',
                    commit_ordinal INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT NOT NULL DEFAULT '[]',
                    snapshot_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(meeting_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_meeting_transcript_checkpoints_timeline
                    ON meeting_transcript_checkpoints(meeting_id, cutoff_ms);
                CREATE TABLE IF NOT EXISTS meeting_segments (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    revision TEXT NOT NULL CHECK(revision IN ('live','canonical')),
                    source TEXT NOT NULL CHECK(source IN ('microphone','system','mixed')),
                    provider_segment_id TEXT NOT NULL DEFAULT '',
                    speaker_id TEXT,
                    speaker_label TEXT NOT NULL DEFAULT '',
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    confidence REAL,
                    alignment_quality TEXT NOT NULL DEFAULT 'estimated'
                        CHECK(alignment_quality IN ('exact_word','provider_segment','estimated')),
                    is_final INTEGER NOT NULL DEFAULT 1,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    edit_version INTEGER NOT NULL DEFAULT 0,
                    edited_at TEXT,
                    UNIQUE(meeting_id, revision, source, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_meeting_segments_timeline
                    ON meeting_segments(meeting_id, revision, start_ms, sequence);
                CREATE TABLE IF NOT EXISTS meeting_segment_edits (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    segment_id TEXT NOT NULL REFERENCES meeting_segments(id) ON DELETE CASCADE,
                    edit_version INTEGER NOT NULL,
                    operation TEXT NOT NULL DEFAULT 'edit'
                        CHECK(operation IN ('edit','undo')),
                    previous_text TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(meeting_id, edit_version)
                );
                CREATE INDEX IF NOT EXISTS idx_meeting_segment_edits_history
                    ON meeting_segment_edits(meeting_id,segment_id,edit_version DESC);
                CREATE TABLE IF NOT EXISTS meeting_speakers (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    display_name_source TEXT NOT NULL DEFAULT 'anonymous'
                        CHECK(display_name_source IN ('anonymous','profile','manual')),
                    source_hint TEXT NOT NULL DEFAULT '',
                    profile_id TEXT,
                    confidence REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS speaker_profiles (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    embedding_blob BLOB,
                    is_named INTEGER NOT NULL DEFAULT 0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS speaker_profile_observations (
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL REFERENCES speaker_profiles(id) ON DELETE CASCADE,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    segment_id TEXT REFERENCES meeting_segments(id) ON DELETE SET NULL,
                    similarity REAL,
                    embedding_blob BLOB,
                    quality REAL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meeting_outputs (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    supersedes_id TEXT,
                    transcript_revision TEXT NOT NULL DEFAULT 'canonical',
                    transcript_edit_version INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(meeting_id, kind, schema_version)
                );
                CREATE TABLE IF NOT EXISTS meeting_output_versions (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    supersedes_id TEXT,
                    transcript_revision TEXT NOT NULL DEFAULT 'canonical',
                    transcript_edit_version INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(meeting_id,kind,schema_version,version)
                );
                CREATE TABLE IF NOT EXISTS meeting_analysis_chunks (
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL CHECK(stage IN ('single','map','reduce')),
                    input_sha256 TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    schema_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(meeting_id,stage,input_sha256,model,schema_version)
                );
                CREATE INDEX IF NOT EXISTS idx_meeting_analysis_chunks_recent
                    ON meeting_analysis_chunks(meeting_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS meeting_notes (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    body TEXT NOT NULL,
                    at_ms INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meeting_action_items (
                    id TEXT NOT NULL,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    owner TEXT,
                    due_date TEXT,
                    status TEXT NOT NULL CHECK(status IN ('open','done','dismissed')) DEFAULT 'open',
                    segment_ids_json TEXT NOT NULL DEFAULT '[]',
                    user_modified INTEGER NOT NULL DEFAULT 0,
                    provenance TEXT NOT NULL DEFAULT 'automatic'
                        CHECK(provenance IN ('automatic','user_modified','carried_user')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(meeting_id, id)
                );
                CREATE INDEX IF NOT EXISTS idx_meeting_action_items_status
                    ON meeting_action_items(meeting_id, status, created_at);
                CREATE TABLE IF NOT EXISTS meeting_chat_threads (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meeting_chat_messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES meeting_chat_threads(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                    content TEXT NOT NULL,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meeting_deliveries (
                    id TEXT PRIMARY KEY,
                    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    payload_version INTEGER NOT NULL DEFAULT 1,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    next_attempt_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS meeting_segments_fts USING fts5(
                    meeting_id UNINDEXED, segment_id UNINDEXED, revision UNINDEXED,
                    text, speaker_label, tokenize='unicode61'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS meeting_outputs_fts USING fts5(
                    meeting_id UNINDEXED, output_id UNINDEXED, kind UNINDEXED,
                    payload, tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS meeting_segments_fts_insert AFTER INSERT ON meeting_segments BEGIN
                    INSERT INTO meeting_segments_fts(meeting_id,segment_id,revision,text,speaker_label)
                    VALUES (new.meeting_id,new.id,new.revision,new.text,new.speaker_label);
                END;
                CREATE TRIGGER IF NOT EXISTS meeting_segments_fts_update AFTER UPDATE ON meeting_segments BEGIN
                    DELETE FROM meeting_segments_fts WHERE segment_id=old.id;
                    INSERT INTO meeting_segments_fts(meeting_id,segment_id,revision,text,speaker_label)
                    VALUES (new.meeting_id,new.id,new.revision,new.text,new.speaker_label);
                END;
                CREATE TRIGGER IF NOT EXISTS meeting_segments_fts_delete AFTER DELETE ON meeting_segments BEGIN
                    DELETE FROM meeting_segments_fts WHERE segment_id=old.id;
                END;
                CREATE TRIGGER IF NOT EXISTS meeting_outputs_fts_insert AFTER INSERT ON meeting_outputs BEGIN
                    INSERT INTO meeting_outputs_fts(meeting_id,output_id,kind,payload)
                    VALUES (new.meeting_id,new.id,new.kind,new.payload_json);
                END;
                CREATE TRIGGER IF NOT EXISTS meeting_outputs_fts_update AFTER UPDATE ON meeting_outputs BEGIN
                    DELETE FROM meeting_outputs_fts WHERE output_id=old.id;
                    INSERT INTO meeting_outputs_fts(meeting_id,output_id,kind,payload)
                    VALUES (new.meeting_id,new.id,new.kind,new.payload_json);
                END;
                CREATE TRIGGER IF NOT EXISTS meeting_outputs_fts_delete AFTER DELETE ON meeting_outputs BEGIN
                    DELETE FROM meeting_outputs_fts WHERE output_id=old.id;
                END;
                """
            )
            profile_columns = {row["name"] for row in conn.execute("PRAGMA table_info(speaker_profiles)")}
            if "embedding_blob" not in profile_columns:
                conn.execute("ALTER TABLE speaker_profiles ADD COLUMN embedding_blob BLOB")
            if "is_named" not in profile_columns:
                conn.execute("ALTER TABLE speaker_profiles ADD COLUMN is_named INTEGER NOT NULL DEFAULT 0")
            observation_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(speaker_profile_observations)")
            }
            if "embedding_blob" not in observation_columns:
                conn.execute("ALTER TABLE speaker_profile_observations ADD COLUMN embedding_blob BLOB")
            if "quality" not in observation_columns:
                conn.execute("ALTER TABLE speaker_profile_observations ADD COLUMN quality REAL")
            meeting_columns = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
            if "audio_retention_days" not in meeting_columns:
                conn.execute("ALTER TABLE meetings ADD COLUMN audio_retention_days INTEGER NOT NULL DEFAULT 0")
            if "smart_turn_enabled" not in meeting_columns:
                conn.execute("ALTER TABLE meetings ADD COLUMN smart_turn_enabled INTEGER NOT NULL DEFAULT 1")
            if "auto_analyze" not in meeting_columns:
                conn.execute("ALTER TABLE meetings ADD COLUMN auto_analyze INTEGER NOT NULL DEFAULT 1")
            if "origin" not in meeting_columns:
                conn.execute(
                    """ALTER TABLE meetings ADD COLUMN origin TEXT NOT NULL DEFAULT 'captured'
                       CHECK(origin IN ('captured','imported'))"""
                )
            if "transcript_edit_version" not in meeting_columns:
                conn.execute(
                    "ALTER TABLE meetings ADD COLUMN transcript_edit_version INTEGER NOT NULL DEFAULT 0"
                )
            asset_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(meeting_audio_assets)")
            }
            if "track_manifest_version" not in asset_columns:
                conn.execute(
                    """ALTER TABLE meeting_audio_assets ADD COLUMN track_manifest_version
                       INTEGER NOT NULL DEFAULT 0"""
                )
            if "track_manifest_json" not in asset_columns:
                conn.execute(
                    """ALTER TABLE meeting_audio_assets ADD COLUMN track_manifest_json
                       TEXT NOT NULL DEFAULT '[]'"""
                )
            if "equality_verified" not in asset_columns:
                conn.execute(
                    """ALTER TABLE meeting_audio_assets ADD COLUMN equality_verified
                       INTEGER NOT NULL DEFAULT 0"""
                )
            segment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(meeting_segments)")}
            if "alignment_quality" not in segment_columns:
                # Existing rows predate timing provenance.  Treating them as
                # estimated is deliberately conservative: their timestamps may
                # be real, but the database has no evidence of how they were
                # produced.
                conn.execute(
                    """ALTER TABLE meeting_segments ADD COLUMN alignment_quality TEXT NOT NULL
                       DEFAULT 'estimated'
                       CHECK(alignment_quality IN ('exact_word','provider_segment','estimated'))"""
                )
            if "edit_version" not in segment_columns:
                conn.execute(
                    "ALTER TABLE meeting_segments ADD COLUMN edit_version INTEGER NOT NULL DEFAULT 0"
                )
            if "edited_at" not in segment_columns:
                conn.execute("ALTER TABLE meeting_segments ADD COLUMN edited_at TEXT")
            speaker_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(meeting_speakers)")
            }
            if "display_name_source" not in speaker_columns:
                conn.execute(
                    """ALTER TABLE meeting_speakers ADD COLUMN display_name_source TEXT
                       NOT NULL DEFAULT 'anonymous'
                       CHECK(display_name_source IN ('anonymous','profile','manual'))"""
                )
                conn.execute(
                    """UPDATE meeting_speakers SET display_name_source='profile'
                       WHERE profile_id IS NOT NULL AND display_name<>label"""
                )
            checkpoint_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(meeting_transcript_checkpoints)")
            }
            if "frontiers_json" not in checkpoint_columns:
                conn.execute(
                    """ALTER TABLE meeting_transcript_checkpoints
                       ADD COLUMN frontiers_json TEXT NOT NULL DEFAULT '{}'"""
                )
            if "commit_ordinal" not in checkpoint_columns:
                conn.execute(
                    """ALTER TABLE meeting_transcript_checkpoints
                       ADD COLUMN commit_ordinal INTEGER NOT NULL DEFAULT 0"""
                )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_checkpoint_commit_ordinal
                   ON meeting_transcript_checkpoints(meeting_id,commit_ordinal)
                   WHERE commit_ordinal > 0"""
            )
            output_columns = {row["name"] for row in conn.execute("PRAGMA table_info(meeting_outputs)")}
            if "version" not in output_columns:
                conn.execute("ALTER TABLE meeting_outputs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "supersedes_id" not in output_columns:
                conn.execute("ALTER TABLE meeting_outputs ADD COLUMN supersedes_id TEXT")
            if "transcript_revision" not in output_columns:
                conn.execute("ALTER TABLE meeting_outputs ADD COLUMN transcript_revision TEXT NOT NULL DEFAULT 'canonical'")
            if "provider" not in output_columns:
                conn.execute("ALTER TABLE meeting_outputs ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
            if "transcript_edit_version" not in output_columns:
                conn.execute(
                    "ALTER TABLE meeting_outputs ADD COLUMN transcript_edit_version INTEGER NOT NULL DEFAULT 0"
                )
            output_version_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(meeting_output_versions)")
            }
            if "transcript_edit_version" not in output_version_columns:
                conn.execute(
                    "ALTER TABLE meeting_output_versions ADD COLUMN transcript_edit_version INTEGER NOT NULL DEFAULT 0"
                )
            action_item_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(meeting_action_items)")
            }
            if "provenance" not in action_item_columns:
                conn.execute(
                    """ALTER TABLE meeting_action_items ADD COLUMN provenance TEXT NOT NULL
                       DEFAULT 'automatic'
                       CHECK(provenance IN ('automatic','user_modified','carried_user'))"""
                )
                conn.execute(
                    """UPDATE meeting_action_items SET provenance='user_modified'
                       WHERE user_modified=1"""
                )
            delivery_columns = {row["name"] for row in conn.execute("PRAGMA table_info(meeting_deliveries)")}
            if "payload_version" not in delivery_columns:
                conn.execute("ALTER TABLE meeting_deliveries ADD COLUMN payload_version INTEGER NOT NULL DEFAULT 1")
            if "attempt_count" not in delivery_columns:
                conn.execute("ALTER TABLE meeting_deliveries ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            if "idempotency_key" not in delivery_columns:
                conn.execute("ALTER TABLE meeting_deliveries ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
            if "next_attempt_at" not in delivery_columns:
                conn.execute("ALTER TABLE meeting_deliveries ADD COLUMN next_attempt_at TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """INSERT INTO meeting_segments_fts(meeting_id,segment_id,revision,text,speaker_label)
                   SELECT s.meeting_id,s.id,s.revision,s.text,s.speaker_label FROM meeting_segments s
                   WHERE NOT EXISTS (
                     SELECT 1 FROM meeting_segments_fts f WHERE f.segment_id=s.id
                   )"""
            )
            conn.execute(
                """INSERT INTO meeting_outputs_fts(meeting_id,output_id,kind,payload)
                   SELECT o.meeting_id,o.id,o.kind,o.payload_json FROM meeting_outputs o
                   WHERE NOT EXISTS (
                     SELECT 1 FROM meeting_outputs_fts f WHERE f.output_id=o.id
                   )"""
            )
            conn.commit()

    def recover_interrupted(self) -> int:
        """Mark capture/finalization work left open by a prior process."""
        now = _utc_now()
        with db._get_connection() as conn:
            open_meeting_ids = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM meetings WHERE state IN ('starting','recording','paused','stopping','finalizing','analyzing')"
                ).fetchall()
            ]
            for meeting_id in open_meeting_ids:
                self._restore_latest_transcript_checkpoint_conn(conn, meeting_id, now)
            analysis_cursor = conn.execute(
                """
                UPDATE meetings
                SET state = 'analysis_failed', updated_at = ?,
                    error_code = 'process_interrupted_during_analysis',
                    error_message = 'The canonical transcript is intact; Scriber stopped during meeting analysis.'
                WHERE state = 'analyzing'
                """,
                (now,),
            )
            interrupted_cursor = conn.execute(
                """
                UPDATE meetings
                SET state = 'interrupted', ended_at = COALESCE(ended_at, ?), updated_at = ?, error_code = 'process_interrupted',
                    error_message = 'Scriber stopped before the meeting workflow completed.'
                WHERE state IN ('starting','recording','paused','stopping','finalizing')
                """,
                (now, now),
            )
            conn.commit()
            return int(analysis_cursor.rowcount) + int(interrupted_cursor.rowcount)

    @staticmethod
    def _restore_latest_transcript_checkpoint_conn(
        conn: sqlite3.Connection, meeting_id: str, now: str
    ) -> int:
        """Restore missing live segments from the newest checksum-valid snapshot.

        Checkpoints never overwrite rows that survived the interruption. This
        preserves provider updates received after the checkpoint while still
        filling gaps caused by a process crash or an incomplete final write.
        """
        checkpoints = conn.execute(
            """SELECT sequence,snapshot_json,snapshot_sha256,segment_count,
                      frontiers_json,commit_ordinal
               FROM meeting_transcript_checkpoints WHERE meeting_id=?
               ORDER BY commit_ordinal DESC,updated_at DESC,sequence DESC""",
            (meeting_id,),
        ).fetchall()
        by_sequence = {int(row["sequence"]): row for row in checkpoints}

        def validated_payload(
            checkpoint: sqlite3.Row,
        ) -> tuple[
            str,
            list[dict[str, Any]],
            int | None,
            list[dict[str, Any]],
            int | None,
        ] | None:
            raw_snapshot = str(checkpoint["snapshot_json"])
            if hashlib.sha256(raw_snapshot.encode("utf-8")).hexdigest() != checkpoint["snapshot_sha256"]:
                return None
            parsed = _loads(raw_snapshot, None)
            if isinstance(parsed, dict):
                parsed_frontiers = parsed.get("frontiers")
                parsed_segments = parsed.get("segments")
                schema_version = parsed.get("schemaVersion")
                if not isinstance(parsed_frontiers, dict) or not isinstance(parsed_segments, list):
                    return None
                if _json(parsed_frontiers) != str(checkpoint["frontiers_json"]):
                    return None
                raw_logical = parsed_frontiers.get("logical")
                if not isinstance(raw_logical, dict):
                    return None
                try:
                    logical = {
                        str(key): max(0, int(value))
                        for key, value in raw_logical.items()
                    }
                    scalar_frontier = min(logical.values()) if logical else 0
                except (TypeError, ValueError):
                    return None
                valid_segments = True
                for item in parsed_segments:
                    if not isinstance(item, dict):
                        valid_segments = False
                        break
                    source = str(item.get("source", ""))
                    try:
                        end_ms = int(item.get("endMs", 0))
                    except (TypeError, ValueError):
                        valid_segments = False
                        break
                    frontier = (
                        logical.get(source, 0)
                        if source in {"microphone", "system"}
                        else scalar_frontier
                    )
                    if end_ms > frontier:
                        valid_segments = False
                        break
                if not valid_segments:
                    return None
                if schema_version == 2:
                    if len(parsed_segments) != int(checkpoint["segment_count"]):
                        return None
                    return "full", parsed_segments, None, [], None
                if schema_version != TRANSCRIPT_CHECKPOINT_SCHEMA_VERSION:
                    return None
                kind = str(parsed.get("kind") or "")
                if kind not in {"full", "delta"}:
                    return None
                base_sequence = parsed.get("baseSequence")
                if kind == "full":
                    if base_sequence is not None or len(parsed_segments) != int(checkpoint["segment_count"]):
                        return None
                    return kind, parsed_segments, None, [], None
                if isinstance(base_sequence, bool) or not isinstance(base_sequence, int):
                    return None
                try:
                    total_segment_count = int(parsed.get("totalSegmentCount", -1))
                except (TypeError, ValueError):
                    return None
                if total_segment_count != int(checkpoint["segment_count"]):
                    return None
                fallback_segments = parsed.get("fallbackSegments", [])
                fallback_base_sequence = parsed.get("fallbackBaseSequence")
                if fallback_base_sequence is None:
                    fallback_segments = []
                elif (
                    isinstance(fallback_base_sequence, bool)
                    or not isinstance(fallback_base_sequence, int)
                    or not isinstance(fallback_segments, list)
                    or not all(isinstance(item, dict) for item in fallback_segments)
                ):
                    return None
                for item in fallback_segments:
                    source = str(item.get("source", ""))
                    try:
                        end_ms = int(item.get("endMs", 0))
                    except (TypeError, ValueError):
                        return None
                    frontier = (
                        logical.get(source, 0)
                        if source in {"microphone", "system"}
                        else scalar_frontier
                    )
                    if end_ms > frontier:
                        return None
                return (
                    kind,
                    parsed_segments,
                    base_sequence,
                    fallback_segments,
                    fallback_base_sequence,
                )
            if not isinstance(parsed, list) or len(parsed) != int(checkpoint["segment_count"]):
                return None
            if all(isinstance(item, dict) for item in parsed):
                return "full", parsed, None, [], None
            return None

        snapshot: list[dict[str, Any]] | None = None
        for checkpoint in checkpoints:
            validated = validated_payload(checkpoint)
            if validated is None:
                continue
            kind, items, base_sequence, fallback_items, fallback_base_sequence = validated
            if kind == "full":
                snapshot = items
                break
            recovery_paths = (
                (base_sequence, items),
                (fallback_base_sequence, fallback_items),
            )
            for candidate_base_sequence, candidate_items in recovery_paths:
                base = (
                    by_sequence.get(int(candidate_base_sequence))
                    if candidate_base_sequence is not None else None
                )
                if base is None:
                    continue
                validated_base = validated_payload(base)
                if validated_base is None or validated_base[0] != "full":
                    continue
                merged = {str(item.get("id") or ""): item for item in validated_base[1]}
                if "" in merged:
                    continue
                for item in candidate_items:
                    segment_id = str(item.get("id") or "")
                    if not segment_id:
                        merged = {}
                        break
                    merged[segment_id] = item
                if len(merged) != int(checkpoint["segment_count"]):
                    continue
                snapshot = sorted(
                    merged.values(),
                    key=lambda item: (
                        max(0, int(item.get("startMs", 0))),
                        int(item.get("sequence", 0)),
                        str(item.get("id", "")),
                    ),
                )
                break
            if snapshot is not None:
                break
        if snapshot is None:
            return 0

        restored = 0
        for item in snapshot:
            source = str(item.get("source", ""))
            segment_id = str(item.get("id", ""))
            text = str(item.get("text", ""))
            if source not in {"microphone", "system", "mixed"} or not segment_id or not text:
                continue
            cursor = conn.execute(
                """INSERT OR IGNORE INTO meeting_segments (
                   id,meeting_id,revision,source,provider_segment_id,speaker_id,speaker_label,
                   start_ms,end_ms,text,confidence,alignment_quality,is_final,sequence,created_at
                   ) VALUES (?,?, 'live',?, '',?,?,?,?,?,?,?,1,?,?)""",
                (
                    segment_id,
                    meeting_id,
                    source,
                    item.get("speakerId"),
                    str(item.get("speakerLabel", "")),
                    max(0, int(item.get("startMs", 0))),
                    max(0, int(item.get("endMs", 0))),
                    text,
                    item.get("confidence"),
                    (
                        str(item.get("alignmentQuality", "estimated"))
                        if str(item.get("alignmentQuality", "estimated")) in ALIGNMENT_QUALITIES
                        else "estimated"
                    ),
                    int(item.get("sequence", 0)),
                    now,
                ),
            )
            restored += int(cursor.rowcount)
        return restored

    def create(
        self,
        request: MeetingCreate,
        *,
        meeting_id: str | None = None,
    ) -> dict[str, Any]:
        origin = str(request.origin or "captured").strip().lower()
        if origin not in {"captured", "imported"}:
            raise ValueError("Meeting origin must be captured or imported.")
        now = _utc_now()
        resolved_meeting_id = str(meeting_id or uuid4().hex).strip()
        if not re.fullmatch(r"[0-9a-f]{32}", resolved_meeting_id):
            raise ValueError("Meeting ID must be a 32-character lowercase UUID hex value.")
        capture_metadata = request.capture_metadata or {}
        if not isinstance(capture_metadata, dict):
            raise ValueError("Meeting capture metadata must be an object.")
        capture_metadata_json = _json(capture_metadata)
        title = request.title.strip() or f"Meeting {now[:10]}"
        try:
            with db._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO meetings (
                        id, title, state, language, live_provider, final_provider,
                        analysis_model, aec_enabled, voice_library_enabled,
                        consent_confirmed, origin, audio_retention_days, smart_turn_enabled,
                        auto_analyze, capture_metadata_json, created_at, updated_at
                    ) VALUES (?, ?, 'starting', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_meeting_id,
                        title,
                        request.language.strip() or "auto",
                        request.live_provider.strip() or "soniox",
                        request.final_provider.strip() or "soniox_async",
                        request.analysis_model.strip(),
                        int(request.aec_enabled),
                        int(request.voice_library_enabled),
                        int(request.consent_confirmed),
                        origin,
                        max(0, min(3650, int(request.audio_retention_days))),
                        int(request.smart_turn_enabled),
                        int(request.auto_analyze),
                        capture_metadata_json,
                        now,
                        now,
                    ),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if "idx_meetings_one_open" in str(exc) or "UNIQUE constraint failed" in str(exc):
                raise MeetingConflict("Another meeting workflow is already open.") from exc
            raise
        return self.get(resolved_meeting_id)

    def get(self, meeting_id: str) -> dict[str, Any]:
        row = db._get_connection().execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            raise MeetingNotFound(meeting_id)
        return self._meeting(row)

    def delete(self, meeting_id: str) -> bool:
        self.get(meeting_id)
        with db._get_connection() as conn:
            affected_profiles = [row["profile_id"] for row in conn.execute(
                "SELECT DISTINCT profile_id FROM speaker_profile_observations WHERE meeting_id=?",
                (meeting_id,),
            ).fetchall()]
            cursor = conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
            for profile_id in affected_profiles:
                remaining = conn.execute(
                    """SELECT embedding_blob FROM speaker_profile_observations
                       WHERE profile_id=? AND embedding_blob IS NOT NULL
                       ORDER BY quality DESC,created_at DESC LIMIT 20""",
                    (profile_id,),
                ).fetchall()
                vectors = [self._embedding_values(row["embedding_blob"]) for row in remaining]
                vectors = [value for value in vectors if value is not None]
                if not vectors:
                    conn.execute(
                        "DELETE FROM speaker_profiles WHERE id=? AND is_named=0", (profile_id,)
                    )
                    conn.execute(
                        "UPDATE speaker_profiles SET embedding_blob=NULL,sample_count=0 WHERE id=?",
                        (profile_id,),
                    )
                    continue
                centroid = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(256)]
                norm = math.sqrt(sum(value * value for value in centroid)) or 1.0
                centroid = [value / norm for value in centroid]
                conn.execute(
                    "UPDATE speaker_profiles SET embedding_blob=?,sample_count=?,updated_at=? WHERE id=?",
                    (self._embedding_blob(centroid), len(vectors), _utc_now(), profile_id),
                )
            conn.commit()
        return bool(cursor.rowcount)

    def list(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        conn = db._get_connection()
        total = int(conn.execute("SELECT COUNT(*) FROM meetings WHERE state <> 'discarded'").fetchone()[0])
        rows = conn.execute(
            """SELECT * FROM meetings WHERE state <> 'discarded'
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return {"items": [self._meeting(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def active(self) -> dict[str, Any] | None:
        row = db._get_connection().execute(
            """SELECT * FROM meetings
               WHERE state IN ('starting','recording','paused','stopping','finalizing','analyzing')
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        return self._meeting(row) if row is not None else None

    def discarded_meeting_ids(self, *, limit: int = 1000) -> list[str]:
        rows = db._get_connection().execute(
            """SELECT id FROM meetings WHERE state='discarded'
               ORDER BY updated_at ASC,id ASC LIMIT ?""",
            (max(1, min(10_000, int(limit))),),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def transition(
        self,
        meeting_id: str,
        new_state: str,
        *,
        error_code: str = "",
        error_message: str = "",
        capture_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if new_state not in MEETING_STATES:
            raise InvalidMeetingTransition(f"Unknown meeting state: {new_state}")
        current = self.get(meeting_id)
        if new_state != current["state"] and new_state not in ALLOWED_TRANSITIONS[current["state"]]:
            raise InvalidMeetingTransition(f"Cannot transition {current['state']} to {new_state}.")
        now = _utc_now()
        started_at = now if new_state == "recording" and not current.get("startedAt") else current.get("startedAt")
        ended_at = now if new_state in TERMINAL_STATES or new_state in {"stopping", "finalizing"} else current.get("endedAt")
        metadata = capture_metadata if capture_metadata is not None else current.get("captureMetadata", {})
        with db._get_connection() as conn:
            cursor = conn.execute(
                """UPDATE meetings SET state = ?, started_at = ?, ended_at = ?, updated_at = ?,
                   error_code = ?, error_message = ?, capture_metadata_json = ?
                   WHERE id = ? AND state = ?""",
                (
                    new_state,
                    started_at,
                    ended_at,
                    now,
                    error_code,
                    error_message,
                    _json(metadata),
                    meeting_id,
                    current["state"],
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                conn.rollback()
                raise MeetingConflict("Meeting state changed concurrently.")
            conn.commit()
        return self.get(meeting_id)

    def change_final_provider_for_retry(
        self,
        meeting_id: str,
        final_provider: str,
        *,
        expected_state: str,
        expected_final_provider: str,
        allowed_providers: Iterable[str],
    ) -> str:
        """CAS one failed Meeting onto a validated final-provider choice.

        The API boundary owns the product whitelist and provider readiness
        checks. Requiring both the state and previous provider observed by that
        boundary prevents a stale retry view from silently replacing a newer
        choice. The normalized previous provider is returned so the caller can
        restore it if later retry reservation work does not complete.
        """
        target_provider = str(final_provider or "").strip().lower()
        observed_provider = str(expected_final_provider or "").strip().lower()
        observed_state = str(expected_state or "").strip().lower()
        if not observed_state:
            raise ValueError("Expected Meeting state is required.")
        if not observed_provider:
            raise ValueError("Expected final transcription provider is required.")
        if isinstance(allowed_providers, (str, bytes)):
            raise ValueError("Allowed final transcription providers must be a collection.")
        normalized_allowed = {
            str(provider or "").strip().lower()
            for provider in allowed_providers
            if str(provider or "").strip()
        }
        if not target_provider or target_provider not in normalized_allowed:
            raise ValueError("Unsupported final meeting transcription provider.")

        now = _utc_now()
        with db._get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT state,final_provider FROM meetings WHERE id=?",
                    (meeting_id,),
                ).fetchone()
                if row is None:
                    raise MeetingNotFound(meeting_id)
                current_state = str(row["state"] or "").strip().lower()
                stored_provider = str(row["final_provider"] or "")
                current_provider = stored_provider.strip().lower()
                if current_state != observed_state:
                    raise MeetingConflict(
                        "Meeting state changed before the final transcription provider could be reserved."
                    )
                if current_state not in FINAL_PROVIDER_RETRY_STATES:
                    raise MeetingConflict(
                        "Final transcription provider changes require a recoverable failed Meeting state."
                    )
                if current_provider != observed_provider:
                    raise MeetingConflict(
                        "Meeting final transcription provider changed concurrently."
                    )
                if target_provider == current_provider:
                    conn.commit()
                    return current_provider
                cursor = conn.execute(
                    """UPDATE meetings SET final_provider=?,updated_at=?
                       WHERE id=? AND state=? AND final_provider=?""",
                    (
                        target_provider,
                        now,
                        meeting_id,
                        str(row["state"]),
                        stored_provider,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise MeetingConflict(
                        "Meeting changed before the final transcription provider could be reserved."
                    )
                conn.commit()
                return current_provider
            except Exception:
                conn.rollback()
                raise

    def add_note(self, meeting_id: str, body: str, *, at_ms: int | None = None) -> dict[str, Any]:
        self.get(meeting_id)
        body = body.strip()
        if not body:
            raise ValueError("Meeting note cannot be empty.")
        now = _utc_now()
        note_id = uuid4().hex
        with db._get_connection() as conn:
            conn.execute(
                "INSERT INTO meeting_notes(id, meeting_id, body, at_ms, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (note_id, meeting_id, body, at_ms, now, now),
            )
            conn.commit()
        return {"id": note_id, "meetingId": meeting_id, "body": body, "atMs": at_ms, "createdAt": now, "updatedAt": now}

    def put_note(
        self,
        meeting_id: str,
        note_id: str,
        body: str,
        *,
        at_ms: int | None = None,
    ) -> dict[str, Any]:
        self.get(meeting_id)
        note_id = note_id.strip()[:96]
        body = body.strip()
        if not note_id:
            raise ValueError("Meeting note id is required.")
        now = _utc_now()
        with db._get_connection() as conn:
            existing = conn.execute(
                "SELECT created_at FROM meeting_notes WHERE meeting_id = ? AND id = ?",
                (meeting_id, note_id),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            if not body:
                conn.execute(
                    "DELETE FROM meeting_notes WHERE meeting_id = ? AND id = ?",
                    (meeting_id, note_id),
                )
                conn.commit()
                return {
                    "id": note_id, "meetingId": meeting_id, "body": "", "atMs": at_ms,
                    "createdAt": created_at, "updatedAt": now,
                }
            conn.execute(
                """INSERT INTO meeting_notes(id,meeting_id,body,at_ms,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET body=excluded.body,at_ms=excluded.at_ms,
                   updated_at=excluded.updated_at WHERE meeting_notes.meeting_id=excluded.meeting_id""",
                (note_id, meeting_id, body, at_ms, created_at, now),
            )
            conn.commit()
        return {"id": note_id, "meetingId": meeting_id, "body": body, "atMs": at_ms, "createdAt": created_at, "updatedAt": now}

    def add_audio_chunk(
        self,
        meeting_id: str,
        *,
        source: str,
        sequence: int,
        relative_path: str,
        started_at_ms: int,
        ended_at_ms: int,
        state: str = "complete",
        sha256: str = "",
    ) -> dict[str, Any]:
        """Compatibility wrapper for owners of an already durable audio file."""
        if state not in {"prepared", "complete"}:
            raise ValueError("Meeting audio chunks may only be prepared or complete.")
        # Historical store callers did not always supply a digest. Capture and
        # recovery never use this sentinel; their prepared rows require the
        # checksum calculated from the fsynced file.
        sha256 = sha256 or ("0" * 64)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            sha256 = hashlib.sha256(str(sha256).encode("utf-8")).hexdigest()
        prepared = self.prepare_audio_chunk(
            meeting_id,
            source=source,
            sequence=sequence,
            relative_path=relative_path,
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            sha256=sha256,
            _allow_legacy_path=True,
        )
        if state == "prepared":
            return prepared
        return self.complete_audio_chunk(
            meeting_id,
            source=source,
            sequence=sequence,
            expected_sha256=sha256,
        )

    def prepare_audio_chunk(
        self,
        meeting_id: str,
        *,
        source: str,
        sequence: int,
        relative_path: str,
        started_at_ms: int,
        ended_at_ms: int,
        sha256: str,
        _allow_legacy_path: bool = False,
    ) -> dict[str, Any]:
        """Reserve an immutable chunk identity before publishing its final name."""
        self.get(meeting_id)
        if source not in {"microphone", "system", "mic_clean"}:
            raise ValueError("Invalid meeting audio source.")
        sequence = int(sequence)
        if sequence < 0:
            raise ValueError("Meeting audio sequence must be non-negative.")
        started_at_ms = max(0, int(started_at_ms))
        ended_at_ms = int(ended_at_ms)
        if ended_at_ms <= started_at_ms:
            raise ValueError("Meeting audio chunk must have a positive duration.")
        relative_path = str(relative_path).replace("\\", "/").strip("/")
        expected_relative_path = (
            Path(meeting_id) / "audio" / f"{source}-{sequence:06d}.wav"
        ).as_posix()
        if (
            not _allow_legacy_path
            and relative_path != expected_relative_path
        ):
            raise ValueError("Meeting audio chunk path is not canonical.")
        sha256 = str(sha256).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Meeting audio chunk requires a SHA-256 digest.")
        now = _utc_now()
        chunk_id = uuid4().hex
        with db._get_connection() as conn:
            try:
                conn.execute(
                    """INSERT INTO meeting_audio_chunks
                       (id,meeting_id,source,sequence,relative_path,started_at_ms,
                        ended_at_ms,state,sha256,created_at)
                       VALUES (?,?,?,?,?,?,?,'prepared',?,?)""",
                    (
                        chunk_id, meeting_id, source, sequence, relative_path,
                        started_at_ms, ended_at_ms, sha256, now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise MeetingConflict(
                    f"Meeting audio sequence {source}:{sequence} is already reserved."
                ) from exc
        return {
            "id": chunk_id, "meetingId": meeting_id, "source": source, "sequence": sequence,
            "relativePath": relative_path, "startedAtMs": started_at_ms, "endedAtMs": ended_at_ms,
            "state": "prepared", "sha256": sha256, "createdAt": now,
        }

    @staticmethod
    def _checkpoint_frontiers_conn(
        conn: sqlite3.Connection, meeting_id: str
    ) -> tuple[dict[str, int], dict[str, int], int]:
        rows = conn.execute(
            """SELECT source,MAX(ended_at_ms) AS frontier
               FROM meeting_audio_chunks
               WHERE meeting_id=? AND state='complete'
               GROUP BY source""",
            (meeting_id,),
        ).fetchall()
        physical = {
            str(row["source"]): max(0, int(row["frontier"]))
            for row in rows
            if int(row["frontier"] or 0) > 0
        }
        logical: dict[str, int] = {}
        microphone_frontier = max(
            physical.get("microphone", 0), physical.get("mic_clean", 0)
        )
        if microphone_frontier:
            logical["microphone"] = microphone_frontier
        if physical.get("system", 0):
            logical["system"] = physical["system"]
        cutoff_ms = min(logical.values()) if logical else 0
        return physical, logical, cutoff_ms

    @classmethod
    def _write_transcript_checkpoint_conn(
        cls,
        conn: sqlite3.Connection,
        meeting_id: str,
        sequence: int,
        now: str,
    ) -> None:
        physical, logical, cutoff_ms = cls._checkpoint_frontiers_conn(conn, meeting_id)
        commit_ordinal = int(conn.execute(
            """SELECT COALESCE(MAX(commit_ordinal),0)+1
               FROM meeting_transcript_checkpoints WHERE meeting_id=?""",
            (meeting_id,),
        ).fetchone()[0])

        # Delta checkpoints point directly at checksum-valid compact bases.
        # The second base/tail is recovery redundancy for a single corrupt row;
        # neither path forms an unbounded chain.
        base_rows: list[sqlite3.Row] = []
        if int(sequence) % TRANSCRIPT_CHECKPOINT_BASE_INTERVAL != 0:
            candidates = conn.execute(
                """SELECT sequence,segment_count,frontiers_json,snapshot_json,snapshot_sha256
                   FROM meeting_transcript_checkpoints
                   WHERE meeting_id=? AND sequence<>?
                   ORDER BY commit_ordinal DESC,updated_at DESC""",
                (meeting_id, int(sequence)),
            ).fetchall()
            for candidate in candidates:
                raw = str(candidate["snapshot_json"])
                if hashlib.sha256(raw.encode("utf-8")).hexdigest() != candidate["snapshot_sha256"]:
                    continue
                payload = _loads(raw, None)
                if (
                    isinstance(payload, dict)
                    and payload.get("schemaVersion") == TRANSCRIPT_CHECKPOINT_SCHEMA_VERSION
                    and payload.get("kind") == "full"
                    and isinstance(payload.get("segments"), list)
                    and len(payload["segments"]) == int(candidate["segment_count"])
                    and _json(payload.get("frontiers")) == str(candidate["frontiers_json"])
                ):
                    base_rows.append(candidate)
                    if len(base_rows) == 2:
                        break

        checkpoint_kind = "delta" if base_rows else "full"

        def segment_watermarks(base: sqlite3.Row | None) -> dict[str, int]:
            base_frontiers = _loads(base["frontiers_json"], {}) if base is not None else {}
            raw = base_frontiers.get("segments", {}) if isinstance(base_frontiers, dict) else {}
            return {
                source: int(raw.get(source, -1))
                for source in ("microphone", "system", "mixed")
            }

        def rows_since(base: sqlite3.Row | None) -> list[sqlite3.Row]:
            if base is None:
                return list(conn.execute(
                    """SELECT id,source,speaker_id,speaker_label,start_ms,end_ms,text,confidence,
                              alignment_quality,sequence
                       FROM meeting_segments
                       WHERE meeting_id=? AND revision='live' AND is_final=1
                       ORDER BY start_ms,sequence,id""",
                    (meeting_id,),
                ).fetchall())
            watermarks = segment_watermarks(base)
            selected: list[sqlite3.Row] = []
            for source in ("microphone", "system", "mixed"):
                frontier = logical.get(source, 0) if source != "mixed" else cutoff_ms
                if frontier <= 0:
                    continue
                selected.extend(conn.execute(
                    """SELECT id,source,speaker_id,speaker_label,start_ms,end_ms,text,confidence,
                              alignment_quality,sequence
                       FROM meeting_segments
                       WHERE meeting_id=? AND revision='live' AND is_final=1
                         AND source=? AND sequence>? AND end_ms<=?
                       ORDER BY start_ms,sequence,id""",
                    (meeting_id, source, watermarks[source], frontier),
                ).fetchall())
            selected.sort(
                key=lambda row: (int(row["start_ms"]), int(row["sequence"]), str(row["id"]))
            )
            return selected

        def serialize_tail(
            rows: list[sqlite3.Row], base: sqlite3.Row | None
        ) -> tuple[list[dict[str, Any]], dict[str, int]]:
            watermarks = segment_watermarks(base)
            items: list[dict[str, Any]] = []
            for row in rows:
                source = str(row["source"])
                end_ms = max(0, int(row["end_ms"]))
                frontier = (
                    logical.get(source, 0)
                    if source in {"microphone", "system"}
                    else cutoff_ms
                )
                if frontier <= 0 or end_ms > frontier:
                    continue
                items.append({
                    "id": row["id"], "source": source,
                    "speakerId": row["speaker_id"], "speakerLabel": row["speaker_label"],
                    "startMs": row["start_ms"], "endMs": row["end_ms"],
                    "durationMs": max(0, end_ms - int(row["start_ms"])),
                    "text": row["text"], "confidence": row["confidence"],
                    "alignmentQuality": row["alignment_quality"],
                    "sequence": row["sequence"],
                })
                watermarks[source] = max(watermarks.get(source, -1), int(row["sequence"]))
            return items, watermarks

        primary_base = base_rows[0] if base_rows else None
        snapshot, segment_frontiers = serialize_tail(rows_since(primary_base), primary_base)
        base_sequence = int(primary_base["sequence"]) if primary_base is not None else None
        base_segment_count = int(primary_base["segment_count"]) if primary_base is not None else 0
        total_segment_count = len(snapshot) if primary_base is None else base_segment_count + len(snapshot)

        fallback_base = base_rows[1] if len(base_rows) > 1 else None
        fallback_snapshot: list[dict[str, Any]] = []
        fallback_base_sequence: int | None = None
        if fallback_base is not None:
            fallback_snapshot, _fallback_frontiers = serialize_tail(
                rows_since(fallback_base), fallback_base
            )
            fallback_total = int(fallback_base["segment_count"]) + len(fallback_snapshot)
            if fallback_total == total_segment_count:
                fallback_base_sequence = int(fallback_base["sequence"])
            else:
                fallback_snapshot = []
        frontiers = {
            "logical": logical,
            "physical": physical,
            "segments": segment_frontiers,
        }
        snapshot_json = _json({
            "schemaVersion": TRANSCRIPT_CHECKPOINT_SCHEMA_VERSION,
            "kind": checkpoint_kind,
            "baseSequence": base_sequence,
            "fallbackBaseSequence": fallback_base_sequence,
            "frontiers": frontiers,
            "totalSegmentCount": total_segment_count,
            "segments": snapshot,
            "fallbackSegments": fallback_snapshot,
        })
        snapshot_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        source_rows = conn.execute(
            """SELECT DISTINCT source FROM meeting_audio_chunks
               WHERE meeting_id=? AND sequence=? AND state='complete' ORDER BY source""",
            (meeting_id, sequence),
        ).fetchall()
        checkpoint_id = hashlib.sha256(
            f"{meeting_id}\0{sequence}".encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            """INSERT INTO meeting_transcript_checkpoints
               (id,meeting_id,sequence,cutoff_ms,segment_count,sources_json,
                frontiers_json,commit_ordinal,snapshot_json,snapshot_sha256,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(meeting_id,sequence) DO UPDATE SET
               cutoff_ms=excluded.cutoff_ms,segment_count=excluded.segment_count,
               sources_json=excluded.sources_json,frontiers_json=excluded.frontiers_json,
               commit_ordinal=excluded.commit_ordinal,
               snapshot_json=excluded.snapshot_json,
               snapshot_sha256=excluded.snapshot_sha256,updated_at=excluded.updated_at""",
            (
                checkpoint_id, meeting_id, sequence, cutoff_ms, total_segment_count,
                _json([row["source"] for row in source_rows]), _json(frontiers),
                commit_ordinal, snapshot_json, snapshot_sha256, now, now,
            ),
        )
        checkpoint_rows = conn.execute(
            """SELECT id,commit_ordinal,segment_count,frontiers_json,
                      snapshot_json,snapshot_sha256
               FROM meeting_transcript_checkpoints WHERE meeting_id=?
               ORDER BY commit_ordinal DESC""",
            (meeting_id,),
        ).fetchall()
        valid_fulls: list[sqlite3.Row] = []
        for checkpoint_row in checkpoint_rows:
            raw = str(checkpoint_row["snapshot_json"])
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != checkpoint_row["snapshot_sha256"]:
                continue
            payload = _loads(raw, None)
            if (
                isinstance(payload, dict)
                and payload.get("schemaVersion") == TRANSCRIPT_CHECKPOINT_SCHEMA_VERSION
                and payload.get("kind") == "full"
                and isinstance(payload.get("segments"), list)
                and len(payload["segments"]) == int(checkpoint_row["segment_count"])
                and _json(payload.get("frontiers")) == str(checkpoint_row["frontiers_json"])
            ):
                valid_fulls.append(checkpoint_row)
                if len(valid_fulls) == 2:
                    break
        if len(valid_fulls) == 2:
            oldest_retained_commit = int(valid_fulls[-1]["commit_ordinal"])
            tombstone = _json({
                "schemaVersion": TRANSCRIPT_CHECKPOINT_SCHEMA_VERSION,
                "kind": "pruned",
            })
            tombstone_sha256 = hashlib.sha256(tombstone.encode("utf-8")).hexdigest()
            conn.execute(
                """UPDATE meeting_transcript_checkpoints
                   SET snapshot_json=?,snapshot_sha256=?
                   WHERE meeting_id=? AND commit_ordinal<?
                     AND id NOT IN (?,?)""",
                (
                    tombstone, tombstone_sha256, meeting_id, oldest_retained_commit,
                    str(valid_fulls[0]["id"]), str(valid_fulls[1]["id"]),
                ),
            )

    def complete_audio_chunk(
        self,
        meeting_id: str,
        *,
        source: str,
        sequence: int,
        expected_sha256: str = "",
    ) -> dict[str, Any]:
        """Atomically complete a prepared row and its transcript checkpoint."""
        self.get(meeting_id)
        now = _utc_now()
        with db._get_connection() as conn:
            row = conn.execute(
                """SELECT * FROM meeting_audio_chunks
                   WHERE meeting_id=? AND source=? AND sequence=?""",
                (meeting_id, source, int(sequence)),
            ).fetchone()
            if row is None:
                raise MeetingNotFound("Prepared meeting audio chunk not found.")
            if expected_sha256 and str(row["sha256"]) != str(expected_sha256).lower():
                raise MeetingConflict("Prepared meeting audio checksum changed.")
            if row["state"] == "complete":
                return self._audio_chunk_dict(row)
            if row["state"] != "prepared":
                raise MeetingConflict(
                    f"Meeting audio chunk cannot complete from {row['state']}."
                )
            cursor = conn.execute(
                "UPDATE meeting_audio_chunks SET state='complete' WHERE id=? AND state='prepared'",
                (row["id"],),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise MeetingConflict("Prepared meeting audio chunk changed concurrently.")
            self._write_transcript_checkpoint_conn(conn, meeting_id, int(sequence), now)
            conn.commit()
            completed = conn.execute(
                "SELECT * FROM meeting_audio_chunks WHERE id=?", (row["id"],)
            ).fetchone()
            checkpoint = conn.execute(
                """SELECT id,meeting_id,sequence,cutoff_ms,segment_count,sources_json,
                          frontiers_json,commit_ordinal,snapshot_sha256,created_at,updated_at
                   FROM meeting_transcript_checkpoints
                   WHERE meeting_id=? AND sequence=?""",
                (meeting_id, int(sequence)),
            ).fetchone()
        result = self._audio_chunk_dict(completed)
        if checkpoint is not None:
            result["transcriptCheckpoint"] = self._checkpoint_dict(checkpoint)
        return result

    @staticmethod
    def _audio_chunk_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "meetingId": row["meeting_id"],
            "source": row["source"], "sequence": row["sequence"],
            "relativePath": row["relative_path"],
            "startedAtMs": row["started_at_ms"], "endedAtMs": row["ended_at_ms"],
            "state": row["state"], "sha256": row["sha256"],
            "createdAt": row["created_at"],
        }

    def _transcript_checkpoints_conn(
        self, conn: sqlite3.Connection, meeting_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT id,meeting_id,sequence,cutoff_ms,segment_count,sources_json,frontiers_json,
                      commit_ordinal,
                      snapshot_sha256,created_at,updated_at
               FROM meeting_transcript_checkpoints WHERE meeting_id=?
               ORDER BY sequence""",
            (meeting_id,),
        ).fetchall()
        return [self._checkpoint_dict(row) for row in rows]

    def transcript_checkpoints(self, meeting_id: str) -> list[dict[str, Any]]:
        """Return redacted checkpoint metadata; transcript snapshot text stays internal."""
        self.get(meeting_id)
        return self._transcript_checkpoints_conn(db._get_connection(), meeting_id)

    @staticmethod
    def _checkpoint_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "meetingId": row["meeting_id"],
            "sequence": row["sequence"],
            "cutoffMs": row["cutoff_ms"],
            "segmentCount": row["segment_count"],
            "sources": _loads(row["sources_json"], []),
            "frontiers": _loads(row["frontiers_json"], {}),
            "commitOrdinal": row["commit_ordinal"],
            "snapshotSha256": row["snapshot_sha256"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _pcm_wav_duration_ms(path: Path) -> int | None:
        try:
            with wave.open(str(path), "rb") as reader:
                if (
                    reader.getnchannels() != 1
                    or reader.getsampwidth() != 2
                    or reader.getframerate() != 16_000
                    or reader.getcomptype() != "NONE"
                    or reader.getnframes() <= 0
                ):
                    return None
                return round(reader.getnframes() * 1000 / reader.getframerate())
        except (EOFError, OSError, wave.Error):
            return None

    @staticmethod
    def _quarantine_audio_file(path: Path) -> Path:
        quarantine = path.parent / "quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)
        destination = quarantine / path.name
        if destination.exists():
            suffix = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8]
            destination = quarantine / f"{path.stem}-{suffix}{path.suffix}"
            counter = 1
            while destination.exists():
                destination = quarantine / f"{path.stem}-{suffix}-{counter}{path.suffix}"
                counter += 1
        shutil.move(str(path), str(destination))
        return destination

    def _legacy_orphan_start_ms(
        self, meeting_id: str, source: str, sequence: int
    ) -> int | None:
        conn = db._get_connection()
        if sequence == 0:
            existing = conn.execute(
                "SELECT COUNT(*) FROM meeting_audio_chunks WHERE meeting_id=? AND source=?",
                (meeting_id, source),
            ).fetchone()[0]
            gaps = conn.execute(
                """SELECT COUNT(*) FROM meeting_audio_gaps
                   WHERE meeting_id=? AND source IN (?, 'all')""",
                (meeting_id, source),
            ).fetchone()[0]
            return 0 if int(existing) == 0 and int(gaps) == 0 else None
        previous = conn.execute(
            """SELECT ended_at_ms FROM meeting_audio_chunks
               WHERE meeting_id=? AND source=? AND sequence=? AND state='complete'""",
            (meeting_id, source, sequence - 1),
        ).fetchone()
        if previous is None:
            return None
        previous_end = int(previous["ended_at_ms"])
        gap_end = int(conn.execute(
            """SELECT COALESCE(MAX(ended_at_ms),0) FROM meeting_audio_gaps
               WHERE meeting_id=? AND source IN (?, 'all') AND started_at_ms>=?""",
            (meeting_id, source, previous_end),
        ).fetchone()[0])
        return max(previous_end, gap_end)

    def reconcile_audio_chunks(self, meetings_root: Path) -> dict[str, int]:
        """Recover prepared commits and conservatively handle rowless files.

        The database checksum binds a prepared partial/final file to its exact
        sequence. Unknown files are never promoted over an existing sequence.
        Legacy rowless finals are adopted only when their complete timeline can
        be reconstructed without guessing.
        """
        root = Path(meetings_root).resolve()
        result = {"completed": 0, "adopted": 0, "quarantined": 0, "deferred": 0}
        if not root.is_dir():
            return result
        try:
            prepared_rows = db._get_connection().execute(
                "SELECT * FROM meeting_audio_chunks WHERE state='prepared' ORDER BY created_at,id"
            ).fetchall()
        except sqlite3.OperationalError:
            prepared_rows = []

        prepared_partials: set[Path] = set()
        for row in prepared_rows:
            meeting_id = str(row["meeting_id"])
            # The caller may be reconciling a staging/test root backed by the
            # shared process database. Never mutate rows outside that root.
            if not (root / meeting_id).is_dir():
                continue
            source = str(row["source"])
            sequence = int(row["sequence"])
            expected_relative = (
                Path(meeting_id) / "audio" / f"{source}-{sequence:06d}.wav"
            ).as_posix()
            if str(row["relative_path"]).replace("\\", "/") != expected_relative:
                supplied = (root / str(row["relative_path"])).resolve()
                if supplied.is_relative_to(root) and supplied.is_file():
                    self._quarantine_audio_file(supplied)
                    result["quarantined"] += 1
                self.quarantine_audio_chunk(
                    meeting_id, str(row["id"]), reason="prepared_path_not_canonical"
                )
                result["quarantined"] += 1
                continue
            final_path = root / Path(expected_relative)
            partial_path = final_path.with_name(
                final_path.name.removesuffix(".wav") + ".partial.wav"
            )
            prepared_partials.add(partial_path.resolve())

            def valid(path: Path) -> bool:
                duration = self._pcm_wav_duration_ms(path)
                expected_duration = int(row["ended_at_ms"]) - int(row["started_at_ms"])
                return (
                    duration is not None
                    and abs(duration - expected_duration) <= 1
                    and self._sha256_file(path) == str(row["sha256"])
                )

            final_exists = final_path.is_file()
            partial_exists = partial_path.is_file()
            final_valid = final_exists and valid(final_path)
            partial_valid = partial_exists and valid(partial_path)
            if final_valid:
                if partial_exists:
                    self._quarantine_audio_file(partial_path)
                    result["quarantined"] += 1
                try:
                    self.complete_audio_chunk(
                        meeting_id,
                        source=source,
                        sequence=sequence,
                        expected_sha256=str(row["sha256"]),
                    )
                    result["completed"] += 1
                except (sqlite3.Error, MeetingStoreError):
                    result["deferred"] += 1
                continue
            if partial_valid and not final_exists:
                try:
                    partial_path.rename(final_path)
                    self.complete_audio_chunk(
                        meeting_id,
                        source=source,
                        sequence=sequence,
                        expected_sha256=str(row["sha256"]),
                    )
                    result["completed"] += 1
                except (OSError, sqlite3.Error, MeetingStoreError):
                    result["deferred"] += 1
                continue
            for candidate in (partial_path, final_path):
                if candidate.is_file():
                    self._quarantine_audio_file(candidate)
                    result["quarantined"] += 1
            self.quarantine_audio_chunk(
                meeting_id, str(row["id"]), reason="prepared_audio_missing_or_corrupt"
            )

        # A partial without a prepared row cannot prove its sequence or digest.
        for partial_path in root.glob("*/audio/*.partial.wav"):
            if partial_path.resolve() in prepared_partials:
                continue
            self._quarantine_audio_file(partial_path)
            result["quarantined"] += 1

        try:
            referenced = {
                str(row["relative_path"]).replace("\\", "/")
                for row in db._get_connection().execute(
                    "SELECT relative_path FROM meeting_audio_chunks"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            referenced = set()
        name_pattern = re.compile(r"^(microphone|system|mic_clean)-(\d{6})\.wav$")
        for final_path in root.glob("*/audio/*.wav"):
            relative = final_path.relative_to(root).as_posix()
            if relative in referenced:
                continue
            match = name_pattern.fullmatch(final_path.name)
            meeting_id = final_path.parent.parent.name
            if match is None:
                self._quarantine_audio_file(final_path)
                result["quarantined"] += 1
                continue
            source, raw_sequence = match.groups()
            sequence = int(raw_sequence)
            try:
                self.get(meeting_id)
            except MeetingNotFound:
                self._quarantine_audio_file(final_path)
                result["quarantined"] += 1
                continue
            if sequence != self.next_audio_chunk_sequence(meeting_id, source):
                self._quarantine_audio_file(final_path)
                result["quarantined"] += 1
                continue
            duration_ms = self._pcm_wav_duration_ms(final_path)
            start_ms = self._legacy_orphan_start_ms(meeting_id, source, sequence)
            if duration_ms is None or start_ms is None:
                self._quarantine_audio_file(final_path)
                result["quarantined"] += 1
                continue
            digest = self._sha256_file(final_path)
            try:
                self.prepare_audio_chunk(
                    meeting_id,
                    source=source,
                    sequence=sequence,
                    relative_path=relative,
                    started_at_ms=start_ms,
                    ended_at_ms=start_ms + duration_ms,
                    sha256=digest,
                )
                self.complete_audio_chunk(
                    meeting_id,
                    source=source,
                    sequence=sequence,
                    expected_sha256=digest,
                )
                result["adopted"] += 1
            except (sqlite3.Error, MeetingStoreError):
                result["deferred"] += 1
        return result

    def add_audio_asset(
        self,
        meeting_id: str,
        *,
        kind: str,
        relative_path: str,
        codec: str,
        sample_rate: int | None,
        channels: int | None,
        duration_ms: int | None,
        byte_size: int,
        sha256: str,
        track_manifest: Iterable[dict[str, Any]],
        track_manifest_version: int = AUDIO_ASSET_TRACK_MANIFEST_VERSION,
        equality_verified: bool = False,
    ) -> dict[str, Any]:
        self.get(meeting_id)
        kind = str(kind or "").strip()
        relative_path = str(relative_path or "").strip().replace("\\", "/")
        codec = str(codec or "").strip().lower()
        sha256 = str(sha256 or "").strip().lower()
        if not kind or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", kind):
            raise ValueError("Invalid meeting audio asset kind.")
        path_parts = relative_path.split("/")
        windows_path = PureWindowsPath(relative_path)
        if (
            not relative_path
            or "\0" in relative_path
            or any(part in {"", "."} for part in path_parts)
            or PurePosixPath(relative_path).is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in PurePosixPath(relative_path).parts
        ):
            raise ValueError("Meeting audio asset path must be relative and traversal-free.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", codec):
            raise ValueError("Invalid meeting audio asset codec.")
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("Meeting audio asset sample rate must be positive.")
        if isinstance(channels, bool) or not isinstance(channels, int) or channels <= 0:
            raise ValueError("Meeting audio asset channels must be positive.")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
            raise ValueError("Meeting audio asset duration must be positive.")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size <= 0:
            raise ValueError("Meeting audio asset byte size must be positive.")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Meeting audio asset SHA-256 is invalid.")
        manifest = self._validated_audio_track_manifest(
            track_manifest,
            version=track_manifest_version,
            asset_codec=codec,
            asset_sample_rate=sample_rate,
            asset_channels=channels,
        )
        if not isinstance(equality_verified, bool):
            raise ValueError("Meeting audio asset equality verification must be boolean.")
        if equality_verified and not all(item["equalityVerified"] for item in manifest):
            raise ValueError("Meeting audio asset equality requires every track to be verified.")
        asset_id, now = uuid4().hex, _utc_now()
        with db._get_connection() as conn:
            conn.execute(
                """INSERT INTO meeting_audio_assets
                   (id,meeting_id,kind,relative_path,codec,sample_rate,channels,duration_ms,
                    byte_size,sha256,track_manifest_version,track_manifest_json,
                    equality_verified,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(meeting_id,kind,relative_path) DO UPDATE SET
                   codec=excluded.codec,sample_rate=excluded.sample_rate,channels=excluded.channels,
                   duration_ms=excluded.duration_ms,byte_size=excluded.byte_size,sha256=excluded.sha256,
                   track_manifest_version=excluded.track_manifest_version,
                   track_manifest_json=excluded.track_manifest_json,
                   equality_verified=excluded.equality_verified""",
                (asset_id, meeting_id, kind, relative_path, codec, sample_rate, channels,
                 duration_ms, byte_size, sha256, track_manifest_version, _json(manifest),
                 int(equality_verified), now),
            )
            row = conn.execute(
                """SELECT * FROM meeting_audio_assets
                   WHERE meeting_id=? AND kind=? AND relative_path=?""",
                (meeting_id, kind, relative_path),
            ).fetchone()
            conn.commit()
        return self._audio_asset_from_row(row)

    @staticmethod
    def _validated_audio_track_manifest(
        track_manifest: Iterable[dict[str, Any]],
        *,
        version: int,
        asset_codec: str,
        asset_sample_rate: int,
        asset_channels: int,
    ) -> list[dict[str, Any]]:
        if isinstance(version, bool) or version != AUDIO_ASSET_TRACK_MANIFEST_VERSION:
            raise ValueError("Unsupported meeting audio track manifest version.")
        if isinstance(track_manifest, (str, bytes, dict)):
            raise ValueError("Meeting audio track manifest must be a list of tracks.")
        tracks = list(track_manifest)
        if not tracks:
            raise ValueError("Meeting audio track manifest must not be empty.")
        required = {
            "source", "streamIndex", "codec", "sampleRate", "channels",
            "timelineOriginMs", "durationMs", "sampleCount", "pcmSha256",
            "equalityVerified",
        }
        normalized: list[dict[str, Any]] = []
        for item in tracks:
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("Meeting audio track manifest fields are invalid.")
            source = str(item.get("source") or "").strip()
            track_codec = str(item.get("codec") or "").strip().lower()
            integers: dict[str, int] = {}
            for field in (
                "streamIndex", "sampleRate", "channels", "timelineOriginMs",
                "durationMs", "sampleCount",
            ):
                value = item.get(field)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"Meeting audio track {field} must be an integer.")
                integers[field] = value
            if source not in AUDIO_ASSET_TRACK_SOURCES:
                raise ValueError("Meeting audio track source is invalid.")
            if track_codec != asset_codec:
                raise ValueError("Meeting audio track codec does not match its asset.")
            if integers["streamIndex"] < 0:
                raise ValueError("Meeting audio track stream index is invalid.")
            if integers["sampleRate"] != asset_sample_rate or integers["sampleRate"] <= 0:
                raise ValueError("Meeting audio track sample rate does not match its asset.")
            if integers["channels"] != asset_channels or integers["channels"] <= 0:
                raise ValueError("Meeting audio track channels do not match its asset.")
            if integers["timelineOriginMs"] < 0:
                raise ValueError("Meeting audio track timeline origin is invalid.")
            if integers["durationMs"] <= 0:
                raise ValueError("Meeting audio track duration must be positive.")
            if integers["sampleCount"] <= 0:
                raise ValueError("Meeting audio track sample count must be positive.")
            pcm_sha256 = str(item.get("pcmSha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", pcm_sha256):
                raise ValueError("Meeting audio track PCM SHA-256 is invalid.")
            equality = item.get("equalityVerified")
            if not isinstance(equality, bool):
                raise ValueError("Meeting audio track equality verification must be boolean.")
            normalized.append({
                "source": source,
                "streamIndex": integers["streamIndex"],
                "codec": track_codec,
                "sampleRate": integers["sampleRate"],
                "channels": integers["channels"],
                "timelineOriginMs": integers["timelineOriginMs"],
                "durationMs": integers["durationMs"],
                "sampleCount": integers["sampleCount"],
                "pcmSha256": pcm_sha256,
                "equalityVerified": equality,
            })
        if [item["streamIndex"] for item in normalized] != list(range(len(normalized))):
            raise ValueError("Meeting audio track stream indexes must be ordered and contiguous.")
        if len({item["source"] for item in normalized}) != len(normalized):
            raise ValueError("Meeting audio track sources must be unique within an asset.")
        return normalized

    @staticmethod
    def _audio_asset_from_row(row: sqlite3.Row) -> dict[str, Any]:
        manifest = _loads(row["track_manifest_json"], [])
        if not isinstance(manifest, list):
            manifest = []
        return {
            "id": row["id"], "meetingId": row["meeting_id"], "kind": row["kind"],
            "relativePath": row["relative_path"], "codec": row["codec"],
            "sampleRate": row["sample_rate"], "channels": row["channels"],
            "durationMs": row["duration_ms"], "byteSize": row["byte_size"],
            "sha256": row["sha256"],
            "trackManifestVersion": row["track_manifest_version"],
            "trackManifest": manifest,
            "equalityVerified": bool(row["equality_verified"]),
            "createdAt": row["created_at"],
        }

    def _audio_assets_conn(
        self, conn: sqlite3.Connection, meeting_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM meeting_audio_assets WHERE meeting_id=? ORDER BY created_at,kind", (meeting_id,)
        ).fetchall()
        return [self._audio_asset_from_row(row) for row in rows]

    def audio_assets(self, meeting_id: str) -> list[dict[str, Any]]:
        self.get(meeting_id)
        return self._audio_assets_conn(db._get_connection(), meeting_id)

    def expired_audio_meetings(self, *, now: datetime | None = None) -> list[str]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        rows = db._get_connection().execute(
            """SELECT id,ended_at,audio_retention_days FROM meetings
               WHERE audio_retention_days>0 AND ended_at IS NOT NULL
                 AND state NOT IN ('starting','recording','paused','stopping','finalizing','analyzing')
                 AND EXISTS(SELECT 1 FROM meeting_audio_chunks c WHERE c.meeting_id=meetings.id)"""
        ).fetchall()
        expired = []
        for row in rows:
            try:
                ended = datetime.fromisoformat(str(row["ended_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if current >= ended.astimezone(timezone.utc) + timedelta(days=int(row["audio_retention_days"])):
                expired.append(str(row["id"]))
        return expired

    def mark_audio_purged(self, meeting_id: str, *, purged_at: str) -> None:
        current = self.get(meeting_id)
        metadata = dict(current.get("captureMetadata", {}))
        metadata["audioPurgedAt"] = purged_at
        with db._get_connection() as conn:
            conn.execute("DELETE FROM meeting_audio_chunks WHERE meeting_id=?", (meeting_id,))
            conn.execute("DELETE FROM meeting_audio_assets WHERE meeting_id=?", (meeting_id,))
            conn.execute(
                "UPDATE meetings SET capture_metadata_json=?,updated_at=? WHERE id=?",
                (_json(metadata), purged_at, meeting_id),
            )
            conn.commit()

    def next_audio_chunk_sequence(self, meeting_id: str, source: str) -> int:
        row = db._get_connection().execute(
            "SELECT COALESCE(MAX(sequence), -1) + 1 FROM meeting_audio_chunks WHERE meeting_id = ? AND source = ?",
            (meeting_id, source),
        ).fetchone()
        return int(row[0])

    def next_audio_offset_ms(self, meeting_id: str, source: str) -> int:
        conn = db._get_connection()
        chunk_end = int(conn.execute(
            """SELECT COALESCE(MAX(ended_at_ms), 0) FROM meeting_audio_chunks
               WHERE meeting_id = ? AND source = ? AND state='complete'""",
            (meeting_id, source),
        ).fetchone()[0])
        gap_end = int(conn.execute(
            "SELECT COALESCE(MAX(ended_at_ms), 0) FROM meeting_audio_gaps WHERE meeting_id = ? AND source IN (?, 'all')",
            (meeting_id, source),
        ).fetchone()[0])
        return max(chunk_end, gap_end)

    def add_audio_gap(
        self,
        meeting_id: str,
        *,
        source: str,
        started_at_ms: int,
        ended_at_ms: int,
        reason: str,
    ) -> dict[str, Any]:
        self.get(meeting_id)
        if source not in {"microphone", "system", "mic_clean", "all"}:
            raise ValueError("Invalid meeting gap source.")
        gap_id = uuid4().hex
        now = _utc_now()
        start = max(0, int(started_at_ms))
        end = max(start, int(ended_at_ms))
        with db._get_connection() as conn:
            conn.execute(
                "INSERT INTO meeting_audio_gaps(id,meeting_id,source,started_at_ms,ended_at_ms,reason,created_at) VALUES (?,?,?,?,?,?,?)",
                (gap_id, meeting_id, source, start, end, reason[:80], now),
            )
            conn.commit()
        return {
            "id": gap_id, "meetingId": meeting_id, "source": source,
            "startedAtMs": start, "endedAtMs": end, "reason": reason[:80], "createdAt": now,
        }

    def _audio_gaps_conn(
        self, conn: sqlite3.Connection, meeting_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM meeting_audio_gaps WHERE meeting_id = ? ORDER BY started_at_ms, id", (meeting_id,)
        ).fetchall()
        return [{
            "id": row["id"], "meetingId": row["meeting_id"], "source": row["source"],
            "startedAtMs": row["started_at_ms"], "endedAtMs": row["ended_at_ms"],
            "reason": row["reason"], "createdAt": row["created_at"],
        } for row in rows]

    def audio_gaps(self, meeting_id: str) -> list[dict[str, Any]]:
        self.get(meeting_id)
        return self._audio_gaps_conn(db._get_connection(), meeting_id)

    def audio_chunks(self, meeting_id: str, source: str | None = None) -> list[dict[str, Any]]:
        self.get(meeting_id)
        params: tuple[Any, ...] = (meeting_id,)
        where = "meeting_id = ? AND state = 'complete'"
        if source:
            where += " AND source = ?"
            params = (meeting_id, source)
        rows = db._get_connection().execute(
            f"SELECT * FROM meeting_audio_chunks WHERE {where} ORDER BY source, sequence", params
        ).fetchall()
        return [
            {
                "id": row["id"], "meetingId": row["meeting_id"], "source": row["source"],
                "sequence": row["sequence"], "relativePath": row["relative_path"],
                "startedAtMs": row["started_at_ms"], "endedAtMs": row["ended_at_ms"],
                "state": row["state"], "sha256": row["sha256"], "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def quarantine_audio_chunk(self, meeting_id: str, chunk_id: str, *, reason: str) -> None:
        self.get(meeting_id)
        with db._get_connection() as conn:
            row = conn.execute(
                "SELECT source,started_at_ms,ended_at_ms FROM meeting_audio_chunks WHERE meeting_id=? AND id=?",
                (meeting_id, chunk_id),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE meeting_audio_chunks SET state='quarantined' WHERE meeting_id=? AND id=?",
                (meeting_id, chunk_id),
            )
            conn.execute(
                """INSERT INTO meeting_audio_gaps
                   (id,meeting_id,source,started_at_ms,ended_at_ms,reason,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (uuid4().hex, meeting_id, row["source"], row["started_at_ms"], row["ended_at_ms"],
                 str(reason)[:120], _utc_now()),
            )
            conn.commit()

    def mark_audio_chunks_purge_pending(self, meeting_id: str) -> int:
        """Durably announce that verified archive ownership replaces PCM chunks."""
        self.get(meeting_id)
        with db._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE meeting_audio_chunks SET state='purge_pending'
                WHERE meeting_id=? AND state='complete'
                """,
                (meeting_id,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def mark_audio_chunks_purged(self, meeting_id: str) -> int:
        """Keep chunk rows as tombstones only after their files are absent."""
        self.get(meeting_id)
        with db._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE meeting_audio_chunks SET state='purged'
                WHERE meeting_id=? AND state='purge_pending'
                """,
                (meeting_id,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def pending_audio_chunk_purges(self, meeting_id: str) -> list[dict[str, Any]]:
        self.get(meeting_id)
        rows = db._get_connection().execute(
            """
            SELECT * FROM meeting_audio_chunks
            WHERE meeting_id=? AND state='purge_pending'
            ORDER BY source,sequence
            """,
            (meeting_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "meetingId": row["meeting_id"],
                "source": row["source"],
                "sequence": row["sequence"],
                "relativePath": row["relative_path"],
                "state": row["state"],
                "sha256": row["sha256"],
            }
            for row in rows
        ]

    def meetings_with_pending_audio_chunk_purges(self) -> list[str]:
        rows = db._get_connection().execute(
            """
            SELECT DISTINCT meeting_id FROM meeting_audio_chunks
            WHERE state='purge_pending' ORDER BY meeting_id
            """
        ).fetchall()
        return [str(row["meeting_id"]) for row in rows]

    def save_output(
        self,
        meeting_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        schema_version: str = "1",
        status: str = "completed",
        error_message: str = "",
        transcript_revision: str = "canonical",
        provider: str = "",
    ) -> dict[str, Any]:
        meeting = self.get(meeting_id)
        transcript_edit_version = int(meeting.get("transcriptEditVersion", 0))
        now = _utc_now()
        output_id = uuid4().hex
        version = 1
        supersedes_id: str | None = None
        conn = db._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM meeting_outputs WHERE meeting_id=? AND kind=? AND schema_version=?",
                (meeting_id, kind, schema_version),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """INSERT INTO meeting_outputs
                       (id,meeting_id,kind,schema_version,status,payload_json,error_message,
                        version,supersedes_id,transcript_revision,transcript_edit_version,
                        provider,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (output_id, meeting_id, kind, schema_version, status, _json(payload),
                     error_message, version, None, transcript_revision,
                     transcript_edit_version, provider, now, now),
                )
            else:
                supersedes_id = uuid4().hex
                conn.execute(
                    """INSERT INTO meeting_output_versions
                       (id,meeting_id,kind,schema_version,version,supersedes_id,
                        transcript_revision,transcript_edit_version,provider,status,
                        payload_json,error_message,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (supersedes_id, meeting_id, kind, schema_version, existing["version"],
                     existing["supersedes_id"], existing["transcript_revision"],
                     existing["transcript_edit_version"], existing["provider"],
                     existing["status"], existing["payload_json"],
                     existing["error_message"], existing["updated_at"]),
                )
                output_id = str(existing["id"])
                version = int(existing["version"]) + 1
                conn.execute(
                    """UPDATE meeting_outputs SET status=?,payload_json=?,error_message=?,
                       version=?,supersedes_id=?,transcript_revision=?,transcript_edit_version=?,
                       provider=?,updated_at=? WHERE id=?""",
                    (status, _json(payload), error_message, version, supersedes_id,
                     transcript_revision, transcript_edit_version, provider, now, output_id),
                )
            if kind == "analysis" and status == "completed":
                self._sync_action_items_conn(
                    conn, meeting_id, payload.get("actionItems", []), now=now
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "id": output_id, "meetingId": meeting_id, "kind": kind,
            "schemaVersion": schema_version, "status": status, "payload": payload,
            "version": version, "supersedesId": supersedes_id,
            "transcriptRevision": transcript_revision,
            "transcriptEditVersion": transcript_edit_version, "provider": provider,
            "errorMessage": error_message, "createdAt": now, "updatedAt": now,
        }

    def get_analysis_chunk(
        self,
        meeting_id: str,
        *,
        stage: str,
        input_sha256: str,
        model: str,
        schema_version: str,
    ) -> dict[str, Any] | None:
        if stage not in {"single", "map", "reduce"}:
            raise ValueError("Invalid meeting analysis cache stage.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(input_sha256)):
            raise ValueError("Meeting analysis cache digest is invalid.")
        row = db._get_connection().execute(
            """SELECT payload_json FROM meeting_analysis_chunks
               WHERE meeting_id=? AND stage=? AND input_sha256=? AND model=?
                 AND schema_version=?""",
            (meeting_id, stage, input_sha256, str(model or ""), str(schema_version)),
        ).fetchone()
        payload = _loads(row["payload_json"], None) if row is not None else None
        return payload if isinstance(payload, dict) else None

    def put_analysis_chunk(
        self,
        meeting_id: str,
        *,
        stage: str,
        input_sha256: str,
        model: str,
        schema_version: str,
        payload: dict[str, Any],
    ) -> None:
        self.get(meeting_id)
        if stage not in {"single", "map", "reduce"}:
            raise ValueError("Invalid meeting analysis cache stage.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(input_sha256)):
            raise ValueError("Meeting analysis cache digest is invalid.")
        if not isinstance(payload, dict):
            raise ValueError("Meeting analysis cache payload must be an object.")
        now = _utc_now()
        with db._get_connection() as conn:
            conn.execute(
                """INSERT INTO meeting_analysis_chunks
                   (meeting_id,stage,input_sha256,model,schema_version,payload_json,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(meeting_id,stage,input_sha256,model,schema_version)
                   DO UPDATE SET payload_json=excluded.payload_json,
                                 updated_at=excluded.updated_at""",
                (
                    meeting_id, stage, input_sha256, str(model or ""),
                    str(schema_version), _json(payload), now, now,
                ),
            )
            # Re-analysis after transcript edits may create new digests. Keep a
            # generous bounded cache so five-hour retries remain resumable
            # without allowing one meeting to grow the database indefinitely.
            conn.execute(
                """DELETE FROM meeting_analysis_chunks
                   WHERE meeting_id=? AND rowid NOT IN (
                     SELECT rowid FROM meeting_analysis_chunks
                     WHERE meeting_id=? ORDER BY updated_at DESC LIMIT 256
                   )""",
                (meeting_id, meeting_id),
            )
            conn.commit()

    def _sync_action_items_conn(
        self,
        conn: sqlite3.Connection,
        meeting_id: str,
        items: Any,
        *,
        now: str,
    ) -> None:
        if not isinstance(items, list):
            items = []
        normalized: list[dict[str, Any]] = []
        seen_payloads: dict[tuple[str, str, str], dict[str, Any]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or "").strip()[:96]
            text = str(raw.get("text") or "").strip()
            status = str(raw.get("status") or "open")
            if (
                not item_id
                or not text
                or status not in {"open", "done", "dismissed"}
            ):
                continue
            segment_ids = _action_segment_ids(raw.get("segmentIds"))
            semantic_key = _action_semantic_key(text)
            duplicate_key = (
                semantic_key,
                str(raw.get("owner") or "").strip().casefold(),
                str(raw.get("dueDate") or "").strip(),
            )
            duplicate = seen_payloads.get(duplicate_key)
            if duplicate is not None:
                duplicate["segmentIds"] = list(dict.fromkeys(
                    [*duplicate["segmentIds"], *segment_ids]
                ))
                continue
            item = {
                "id": item_id,
                "text": text,
                "owner": raw.get("owner"),
                "dueDate": raw.get("dueDate"),
                "status": status,
                "segmentIds": segment_ids,
            }
            normalized.append(item)
            seen_payloads[duplicate_key] = item

        existing_rows = conn.execute(
            "SELECT * FROM meeting_action_items WHERE meeting_id=? ORDER BY created_at,id",
            (meeting_id,),
        ).fetchall()
        existing_user_items = [
            {
                "id": str(row["id"]),
                "text": str(row["text"]),
                "segmentIds": _action_segment_ids(
                    _loads(row["segment_ids_json"], [])
                ),
            }
            for row in existing_rows
            if bool(row["user_modified"])
        ]
        user_ids = {str(item["id"]) for item in existing_user_items}
        matched_user_ids: set[str] = set()
        automatic_items: list[dict[str, Any]] = []
        automatic_ids: set[str] = set()

        # A user-edited row is intentionally carried across generations even if
        # the new model response omits it. Unedited automatic rows are exactly
        # the current analysis projection and must not survive when absent.
        conn.execute(
            """UPDATE meeting_action_items SET provenance='carried_user',updated_at=?
               WHERE meeting_id=? AND user_modified=1""",
            (now, meeting_id),
        )

        for incoming in normalized:
            candidates = [
                (score, str(existing["id"]), existing)
                for existing in existing_user_items
                if str(existing["id"]) not in matched_user_ids
                if (score := _action_user_match_score(incoming, existing)) is not None
            ]
            if candidates:
                _score, matched_id, existing = max(
                    candidates, key=lambda value: (value[0], value[1])
                )
                matched_user_ids.add(matched_id)
                merged_citations = list(dict.fromkeys([
                    *existing["segmentIds"], *incoming["segmentIds"]
                ]))
                conn.execute(
                    """UPDATE meeting_action_items
                       SET segment_ids_json=?,provenance='carried_user',updated_at=?
                       WHERE meeting_id=? AND id=? AND user_modified=1""",
                    (_json(merged_citations), now, meeting_id, matched_id),
                )
                # If this semantic action previously also existed as an
                # automatic row under the new content ID, leaving it out of
                # automatic_ids removes that duplicate below.
                continue

            effective_id = str(incoming["id"])
            if effective_id in user_ids or effective_id in automatic_ids:
                salt = 0
                while True:
                    candidate = _action_collision_id(
                        effective_id,
                        str(incoming["text"]),
                        incoming["segmentIds"],
                        salt=salt,
                    )
                    if candidate not in user_ids and candidate not in automatic_ids:
                        effective_id = candidate
                        break
                    salt += 1
            projected = dict(incoming)
            projected["id"] = effective_id
            automatic_items.append(projected)
            automatic_ids.add(effective_id)

        if automatic_ids:
            placeholders = ",".join("?" for _ in automatic_ids)
            conn.execute(
                f"""DELETE FROM meeting_action_items
                    WHERE meeting_id=? AND user_modified=0 AND id NOT IN ({placeholders})""",
                (meeting_id, *sorted(automatic_ids)),
            )
        else:
            conn.execute(
                "DELETE FROM meeting_action_items WHERE meeting_id=? AND user_modified=0",
                (meeting_id,),
            )
        for item in automatic_items:
            conn.execute(
                """INSERT INTO meeting_action_items
                   (id,meeting_id,text,owner,due_date,status,segment_ids_json,user_modified,
                    provenance,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(meeting_id,id) DO UPDATE SET
                   text=excluded.text,owner=excluded.owner,due_date=excluded.due_date,
                   status=excluded.status,segment_ids_json=excluded.segment_ids_json,
                   provenance='automatic',updated_at=excluded.updated_at
                   WHERE meeting_action_items.meeting_id=excluded.meeting_id
                     AND meeting_action_items.user_modified=0""",
                (
                    item["id"], meeting_id, item["text"], item["owner"],
                    item["dueDate"], item["status"], _json(item["segmentIds"]),
                    0, "automatic", now, now,
                ),
            )

    def update_action_item(self, meeting_id: str, item_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.get(meeting_id)
        row = db._get_connection().execute(
            "SELECT * FROM meeting_action_items WHERE meeting_id = ? AND id = ?",
            (meeting_id, item_id),
        ).fetchone()
        if row is None:
            raise MeetingNotFound("Meeting action item not found")
        text = str(changes.get("text", row["text"])).strip()
        owner = changes.get("owner", row["owner"])
        due_date = changes.get("dueDate", row["due_date"])
        status = str(changes.get("status", row["status"]))
        if not text or len(text) > 4000:
            raise ValueError("Action item text must contain 1 to 4000 characters.")
        if status not in {"open", "done", "dismissed"}:
            raise ValueError("Invalid action item status.")
        owner = None if owner is None or not str(owner).strip() else str(owner).strip()[:200]
        due_date = None if due_date is None or not str(due_date).strip() else str(due_date).strip()[:40]
        now = _utc_now()
        with db._get_connection() as conn:
            conn.execute(
                """UPDATE meeting_action_items SET text=?,owner=?,due_date=?,status=?,
                   user_modified=1,provenance='user_modified',updated_at=?
                   WHERE meeting_id=? AND id=?""",
                (text, owner, due_date, status, now, meeting_id, item_id),
            )
            conn.commit()
        return self.action_items(meeting_id, item_id=item_id)[0]

    def _action_items_conn(
        self,
        conn: sqlite3.Connection,
        meeting_id: str,
        *,
        item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = "meeting_id = ?" + (" AND id = ?" if item_id else "")
        params = (meeting_id, item_id) if item_id else (meeting_id,)
        rows = conn.execute(
            f"SELECT * FROM meeting_action_items WHERE {where} ORDER BY created_at, id", params
        ).fetchall()
        return [
            {
                "id": row["id"], "meetingId": row["meeting_id"], "text": row["text"],
                "owner": row["owner"], "dueDate": row["due_date"], "status": row["status"],
                "segmentIds": _loads(row["segment_ids_json"], []),
                "userModified": bool(row["user_modified"]),
                "provenance": str(row["provenance"] or "automatic"),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def action_items(self, meeting_id: str, *, item_id: str | None = None) -> list[dict[str, Any]]:
        self.get(meeting_id)
        return self._action_items_conn(
            db._get_connection(), meeting_id, item_id=item_id
        )

    def rename_speaker(self, meeting_id: str, speaker_id: str, display_name: str) -> int:
        self.get(meeting_id)
        display_name = display_name.strip()
        if not display_name or len(display_name) > 120:
            raise ValueError("Speaker name must contain 1 to 120 characters.")
        now = _utc_now()
        with db._get_connection() as conn:
            current = conn.execute(
                "SELECT profile_id FROM meeting_speakers WHERE meeting_id=? AND id=?",
                (meeting_id, speaker_id),
            ).fetchone()
            speaker_update = conn.execute(
                """UPDATE meeting_speakers SET display_name = ?, display_name_source='manual',
                   updated_at = ? WHERE meeting_id = ? AND id = ?""",
                (display_name, now, meeting_id, speaker_id),
            )
            segment_update = conn.execute(
                "UPDATE meeting_segments SET speaker_label = ? WHERE meeting_id = ? AND speaker_id = ?",
                (display_name, meeting_id, speaker_id),
            )
            if current is not None and current["profile_id"]:
                conn.execute(
                    "UPDATE speaker_profiles SET display_name=?,is_named=1,updated_at=? WHERE id=?",
                    (display_name, now, current["profile_id"]),
                )
            conn.commit()
        return max(int(speaker_update.rowcount), int(segment_update.rowcount))

    def create_chat_thread(self, meeting_id: str, title: str = "") -> dict[str, Any]:
        self.get(meeting_id)
        now = _utc_now()
        thread_id = uuid4().hex
        with db._get_connection() as conn:
            conn.execute(
                "INSERT INTO meeting_chat_threads(id,meeting_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (thread_id, meeting_id, title.strip()[:160], now, now),
            )
            conn.commit()
        return {"id": thread_id, "meetingId": meeting_id, "title": title.strip()[:160], "createdAt": now, "updatedAt": now}

    def add_chat_message(
        self,
        thread_id: str,
        *,
        role: str,
        content: str,
        citations: list[str] | None = None,
    ) -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid meeting chat role.")
        content = content.strip()
        if not content:
            raise ValueError("Meeting chat message cannot be empty.")
        conn = db._get_connection()
        thread = conn.execute("SELECT meeting_id FROM meeting_chat_threads WHERE id = ?", (thread_id,)).fetchone()
        if thread is None:
            raise MeetingNotFound("Meeting chat thread not found")
        now = _utc_now()
        message_id = uuid4().hex
        normalized_citations = list(dict.fromkeys(str(value) for value in (citations or []) if str(value)))
        with conn:
            conn.execute(
                "INSERT INTO meeting_chat_messages(id,thread_id,role,content,citations_json,created_at) VALUES (?,?,?,?,?,?)",
                (message_id, thread_id, role, content, _json(normalized_citations), now),
            )
            conn.execute("UPDATE meeting_chat_threads SET updated_at = ? WHERE id = ?", (now, thread_id))
        return {
            "id": message_id, "threadId": thread_id, "role": role, "content": content,
            "citations": normalized_citations, "createdAt": now,
        }

    def chat_threads(self, meeting_id: str) -> list[dict[str, Any]]:
        self.get(meeting_id)
        conn = db._get_connection()
        rows = conn.execute(
            "SELECT * FROM meeting_chat_threads WHERE meeting_id = ? ORDER BY updated_at DESC", (meeting_id,)
        ).fetchall()
        result = []
        for row in rows:
            messages = conn.execute(
                "SELECT * FROM meeting_chat_messages WHERE thread_id = ? ORDER BY created_at, id", (row["id"],)
            ).fetchall()
            result.append({
                "id": row["id"], "meetingId": meeting_id, "title": row["title"],
                "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                "messages": [{
                    "id": message["id"], "threadId": row["id"], "role": message["role"],
                    "content": message["content"], "citations": _loads(message["citations_json"], []),
                    "createdAt": message["created_at"],
                } for message in messages],
            })
        return result

    def speaker_profiles(self) -> list[dict[str, Any]]:
        rows = db._get_connection().execute(
            "SELECT id,display_name,is_named,sample_count,created_at,updated_at FROM speaker_profiles ORDER BY display_name"
        ).fetchall()
        return [{
            "id": row["id"], "displayName": row["display_name"], "sampleCount": row["sample_count"],
            "isNamed": bool(row["is_named"]),
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        } for row in rows]

    @staticmethod
    def _embedding_blob(values: list[float]) -> bytes:
        if len(values) != 256 or not all(math.isfinite(float(value)) for value in values):
            raise ValueError("Speaker embedding must contain 256 finite values.")
        return struct.pack("<256f", *(float(value) for value in values))

    @staticmethod
    def _embedding_values(blob: bytes | None) -> list[float] | None:
        if blob is None or len(blob) != 1024:
            return None
        return list(struct.unpack("<256f", blob))

    def register_speaker_embedding(
        self,
        meeting_id: str,
        speaker_id: str,
        segment_id: str,
        embedding: list[float],
        *,
        quality: float = 1.0,
    ) -> dict[str, Any]:
        self.get(meeting_id)
        blob = self._embedding_blob(embedding)
        conn = db._get_connection()
        profiles = conn.execute(
            "SELECT id,display_name,is_named,embedding_blob,sample_count FROM speaker_profiles"
        ).fetchall()
        scores: list[tuple[float, sqlite3.Row]] = []
        for profile in profiles:
            centroid = self._embedding_values(profile["embedding_blob"])
            if centroid is not None:
                scores.append((sum(left * right for left, right in zip(embedding, centroid, strict=True)), profile))
        scores.sort(key=lambda item: item[0], reverse=True)
        best_score = scores[0][0] if scores else -1.0
        second_score = scores[1][0] if len(scores) > 1 else -1.0
        matched = bool(scores and best_score >= 0.82 and best_score - second_score >= 0.08)
        now = _utc_now()
        profile_id = str(scores[0][1]["id"]) if matched else uuid4().hex
        with conn:
            if not matched:
                conn.execute(
                    """INSERT INTO speaker_profiles
                       (id,display_name,embedding_json,embedding_blob,is_named,sample_count,created_at,updated_at)
                       VALUES (?,?,?, ?,0,0,?,?)""",
                    (profile_id, f"Speaker {profile_id[:6]}", "[]", blob, now, now),
                )
                best_score = 1.0
            conn.execute(
                """INSERT OR REPLACE INTO speaker_profile_observations
                   (id,profile_id,meeting_id,segment_id,similarity,embedding_blob,quality,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (hashlib.sha256(f"{profile_id}:{segment_id}".encode()).hexdigest()[:32],
                 profile_id, meeting_id, segment_id, best_score, blob,
                 max(0.0, min(1.0, float(quality))), now),
            )
            observations = conn.execute(
                """SELECT embedding_blob FROM speaker_profile_observations WHERE profile_id=?
                   AND embedding_blob IS NOT NULL ORDER BY quality DESC,created_at DESC LIMIT 20""",
                (profile_id,),
            ).fetchall()
            vectors = [self._embedding_values(row["embedding_blob"]) for row in observations]
            vectors = [value for value in vectors if value is not None]
            centroid = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(256)]
            norm = math.sqrt(sum(value * value for value in centroid)) or 1.0
            centroid = [value / norm for value in centroid]
            conn.execute(
                "UPDATE speaker_profiles SET embedding_blob=?,sample_count=?,updated_at=? WHERE id=?",
                (self._embedding_blob(centroid), len(vectors), now, profile_id),
            )
            profile = conn.execute(
                "SELECT display_name,is_named,sample_count FROM speaker_profiles WHERE id=?", (profile_id,)
            ).fetchone()
            independent_matches = int(conn.execute(
                """SELECT COUNT(DISTINCT o.segment_id) FROM speaker_profile_observations o
                   JOIN meeting_segments s ON s.id=o.segment_id
                   WHERE o.profile_id=? AND o.meeting_id=? AND s.speaker_id=? AND o.similarity>=0.82""",
                (profile_id, meeting_id, speaker_id),
            ).fetchone()[0])
            conn.execute(
                "UPDATE meeting_speakers SET profile_id=?,confidence=?,updated_at=? WHERE meeting_id=? AND id=?",
                (profile_id, best_score, now, meeting_id, speaker_id),
            )
            if bool(profile["is_named"]) and independent_matches >= 2 and matched:
                conn.execute(
                    """UPDATE meeting_speakers SET display_name=?,display_name_source='profile'
                       WHERE meeting_id=? AND id=? AND display_name_source<>'manual'""",
                    (profile["display_name"], meeting_id, speaker_id),
                )
                conn.execute(
                    """UPDATE meeting_segments SET speaker_label=? WHERE meeting_id=? AND speaker_id=?
                       AND EXISTS(SELECT 1 FROM meeting_speakers s WHERE s.id=?
                                  AND s.display_name_source='profile')""",
                    (profile["display_name"], meeting_id, speaker_id, speaker_id),
                )
            conn.commit()
        return {
            "profileId": profile_id, "similarity": best_score, "matched": matched,
            "autoNamed": bool(profile["is_named"] and independent_matches >= 2 and matched),
        }

    def delete_speaker_profile(self, profile_id: str) -> bool:
        with db._get_connection() as conn:
            conn.execute(
                """UPDATE meeting_segments SET speaker_label=(
                     SELECT label FROM meeting_speakers s WHERE s.id=meeting_segments.speaker_id
                   ) WHERE speaker_id IN (
                     SELECT id FROM meeting_speakers
                     WHERE profile_id=? AND display_name_source='profile'
                   )""",
                (profile_id,),
            )
            conn.execute(
                """UPDATE meeting_speakers SET
                   display_name=CASE WHEN display_name_source='profile' THEN label ELSE display_name END,
                   display_name_source=CASE WHEN display_name_source='profile' THEN 'anonymous' ELSE display_name_source END,
                   profile_id=NULL,confidence=NULL WHERE profile_id=?""",
                (profile_id,),
            )
            cursor = conn.execute("DELETE FROM speaker_profiles WHERE id = ?", (profile_id,))
            conn.commit()
        return bool(cursor.rowcount)

    def rename_speaker_profile(self, profile_id: str, display_name: str) -> dict[str, Any]:
        name = " ".join(str(display_name).split()).strip()
        if not name:
            raise ValueError("Speaker profile name is required.")
        if len(name) > 120:
            raise ValueError("Speaker profile name must be 120 characters or fewer.")
        now = _utc_now()
        with db._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE speaker_profiles SET display_name=?,is_named=1,updated_at=? WHERE id=?",
                (name, now, profile_id),
            )
            if not cursor.rowcount:
                raise MeetingNotFound("Speaker profile not found")
            conn.execute(
                """UPDATE meeting_speakers SET display_name=?,display_name_source='profile'
                   WHERE profile_id=? AND confidence IS NOT NULL AND confidence>=0.82
                     AND display_name_source<>'manual'""",
                (name, profile_id),
            )
            conn.execute(
                """UPDATE meeting_segments SET speaker_label=?
                   WHERE speaker_id IN (
                     SELECT id FROM meeting_speakers
                     WHERE profile_id=? AND confidence IS NOT NULL AND confidence>=0.82
                       AND display_name_source='profile'
                   )""",
                (name, profile_id),
            )
            conn.commit()
        return {
            "id": profile_id,
            "displayName": name,
            "isNamed": True,
            "updatedAt": now,
        }

    def delete_all_speaker_profiles(self) -> int:
        with db._get_connection() as conn:
            conn.execute(
                """UPDATE meeting_segments SET speaker_label=(
                     SELECT label FROM meeting_speakers s WHERE s.id=meeting_segments.speaker_id
                   ) WHERE speaker_id IN (
                     SELECT id FROM meeting_speakers WHERE display_name_source='profile'
                   )"""
            )
            conn.execute(
                """UPDATE meeting_speakers SET
                   display_name=CASE WHEN display_name_source='profile' THEN label ELSE display_name END,
                   display_name_source=CASE WHEN display_name_source='profile' THEN 'anonymous' ELSE display_name_source END,
                   profile_id=NULL,confidence=NULL"""
            )
            cursor = conn.execute("DELETE FROM speaker_profiles")
            conn.commit()
        return int(cursor.rowcount)

    def _recompute_speaker_profile_conn(
        self, conn: sqlite3.Connection, profile_id: str, now: str
    ) -> bool:
        profile = conn.execute(
            "SELECT is_named FROM speaker_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        if profile is None:
            return False
        observations = conn.execute(
            """SELECT embedding_blob FROM speaker_profile_observations WHERE profile_id=?
               AND embedding_blob IS NOT NULL ORDER BY quality DESC,created_at DESC LIMIT 20""",
            (profile_id,),
        ).fetchall()
        vectors = [self._embedding_values(row["embedding_blob"]) for row in observations]
        vectors = [value for value in vectors if value is not None]
        if not vectors:
            if not bool(profile["is_named"]):
                conn.execute("DELETE FROM speaker_profiles WHERE id=?", (profile_id,))
                return False
            conn.execute(
                "UPDATE speaker_profiles SET embedding_blob=NULL,sample_count=0,updated_at=? WHERE id=?",
                (now, profile_id),
            )
            return True
        centroid = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(256)]
        norm = math.sqrt(sum(value * value for value in centroid)) or 1.0
        centroid = [value / norm for value in centroid]
        conn.execute(
            "UPDATE speaker_profiles SET embedding_blob=?,sample_count=?,updated_at=? WHERE id=?",
            (self._embedding_blob(centroid), len(vectors), now, profile_id),
        )
        return True

    def merge_speaker_profiles(self, target_profile_id: str, source_profile_id: str) -> dict[str, Any]:
        if not target_profile_id or not source_profile_id or target_profile_id == source_profile_id:
            raise ValueError("Choose two different speaker profiles to merge.")
        now = _utc_now()
        with db._get_connection() as conn:
            target = conn.execute(
                "SELECT id FROM speaker_profiles WHERE id=?", (target_profile_id,)
            ).fetchone()
            source = conn.execute(
                "SELECT id FROM speaker_profiles WHERE id=?", (source_profile_id,)
            ).fetchone()
            if target is None or source is None:
                raise MeetingNotFound("Speaker profile not found")
            observations = conn.execute(
                "SELECT * FROM speaker_profile_observations WHERE profile_id=?",
                (source_profile_id,),
            ).fetchall()
            for observation in observations:
                replacement_id = hashlib.sha256(
                    f"{target_profile_id}:{observation['segment_id']}".encode()
                ).hexdigest()[:32]
                conn.execute(
                    """INSERT OR REPLACE INTO speaker_profile_observations
                       (id,profile_id,meeting_id,segment_id,similarity,embedding_blob,quality,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (replacement_id, target_profile_id, observation["meeting_id"],
                     observation["segment_id"], observation["similarity"],
                     observation["embedding_blob"], observation["quality"], observation["created_at"]),
                )
            conn.execute(
                "UPDATE meeting_speakers SET profile_id=?,updated_at=? WHERE profile_id=?",
                (target_profile_id, now, source_profile_id),
            )
            conn.execute("DELETE FROM speaker_profiles WHERE id=?", (source_profile_id,))
            self._recompute_speaker_profile_conn(conn, target_profile_id, now)
            conn.commit()
        return {"targetProfileId": target_profile_id, "mergedProfileId": source_profile_id}

    def split_speaker_profile(self, meeting_id: str, speaker_id: str) -> dict[str, Any]:
        self.get(meeting_id)
        now = _utc_now()
        with db._get_connection() as conn:
            speaker = conn.execute(
                """SELECT profile_id,label,display_name_source FROM meeting_speakers
                   WHERE meeting_id=? AND id=?""",
                (meeting_id, speaker_id),
            ).fetchone()
            if speaker is None:
                raise MeetingNotFound("Meeting speaker not found")
            old_profile_id = str(speaker["profile_id"] or "")
            if not old_profile_id:
                raise ValueError("This meeting speaker is not linked to a Voice Library profile.")
            observations = conn.execute(
                """SELECT o.* FROM speaker_profile_observations o
                   JOIN meeting_segments s ON s.id=o.segment_id
                   WHERE o.profile_id=? AND o.meeting_id=? AND s.speaker_id=?""",
                (old_profile_id, meeting_id, speaker_id),
            ).fetchall()
            if not observations:
                raise ValueError("No speaker observations are available to separate.")
            new_profile_id = uuid4().hex
            conn.execute(
                """INSERT INTO speaker_profiles
                   (id,display_name,embedding_json,embedding_blob,is_named,sample_count,created_at,updated_at)
                   VALUES (?,?,?,NULL,0,0,?,?)""",
                (new_profile_id, f"Speaker {new_profile_id[:6]}", "[]", now, now),
            )
            for observation in observations:
                replacement_id = hashlib.sha256(
                    f"{new_profile_id}:{observation['segment_id']}".encode()
                ).hexdigest()[:32]
                conn.execute(
                    """INSERT INTO speaker_profile_observations
                       (id,profile_id,meeting_id,segment_id,similarity,embedding_blob,quality,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (replacement_id, new_profile_id, observation["meeting_id"],
                     observation["segment_id"], 1.0, observation["embedding_blob"],
                     observation["quality"], observation["created_at"]),
                )
                conn.execute(
                    "DELETE FROM speaker_profile_observations WHERE id=?", (observation["id"],)
                )
            conn.execute(
                """UPDATE meeting_speakers SET profile_id=?,confidence=1.0,
                   display_name=CASE WHEN display_name_source='profile' THEN label ELSE display_name END,
                   display_name_source=CASE WHEN display_name_source='profile' THEN 'anonymous' ELSE display_name_source END,
                   updated_at=? WHERE meeting_id=? AND id=?""",
                (new_profile_id, now, meeting_id, speaker_id),
            )
            if str(speaker["display_name_source"] or "") == "profile":
                conn.execute(
                    "UPDATE meeting_segments SET speaker_label=? WHERE meeting_id=? AND speaker_id=?",
                    (str(speaker["label"] or ""), meeting_id, speaker_id),
                )
            self._recompute_speaker_profile_conn(conn, new_profile_id, now)
            self._recompute_speaker_profile_conn(conn, old_profile_id, now)
            conn.commit()
        return {
            "meetingId": meeting_id, "speakerId": speaker_id,
            "oldProfileId": old_profile_id, "newProfileId": new_profile_id,
        }

    def create_delivery(
        self,
        meeting_id: str,
        *,
        kind: str,
        target: str,
        request_payload: dict[str, Any],
        status: str = "pending",
    ) -> dict[str, Any]:
        self.get(meeting_id)
        now = _utc_now()
        delivery_id = uuid4().hex
        with db._get_connection() as conn:
            conn.execute(
                """INSERT INTO meeting_deliveries
                   (id,meeting_id,kind,target,status,request_json,payload_version,
                    attempt_count,idempotency_key,next_attempt_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (delivery_id, meeting_id, kind, target, status, _json(request_payload),
                 1, 0, delivery_id, "", now, now),
            )
            conn.commit()
        return {
            "id": delivery_id, "meetingId": meeting_id, "kind": kind, "target": target,
            "status": status, "request": request_payload, "response": {}, "errorMessage": "",
            "payloadVersion": 1, "attemptCount": 0, "idempotencyKey": delivery_id,
            "nextAttemptAt": "",
            "createdAt": now, "updatedAt": now,
        }

    def update_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        response_payload: dict[str, Any] | None = None,
        error_message: str = "",
        attempt_count: int | None = None,
    ) -> dict[str, Any]:
        now = _utc_now()
        with db._get_connection() as conn:
            conn.execute(
                """UPDATE meeting_deliveries SET status = ?, response_json = ?, error_message = ?,
                   attempt_count=COALESCE(?,attempt_count),next_attempt_at='',updated_at = ?
                   WHERE id = ?""",
                (status, _json(response_payload or {}), error_message, attempt_count, now, delivery_id),
            )
            row = conn.execute("SELECT * FROM meeting_deliveries WHERE id = ?", (delivery_id,)).fetchone()
            conn.commit()
        if row is None:
            raise MeetingNotFound("Meeting delivery not found")
        return self._delivery(row)

    def deliveries(self, meeting_id: str) -> list[dict[str, Any]]:
        self.get(meeting_id)
        rows = db._get_connection().execute(
            "SELECT * FROM meeting_deliveries WHERE meeting_id = ? ORDER BY created_at DESC", (meeting_id,)
        ).fetchall()
        return [self._delivery(row) for row in rows]

    @staticmethod
    def _delivery(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "meetingId": row["meeting_id"], "kind": row["kind"],
            "target": row["target"], "status": row["status"],
            "request": _loads(row["request_json"], {}), "response": _loads(row["response_json"], {}),
            "errorMessage": row["error_message"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "payloadVersion": row["payload_version"], "attemptCount": row["attempt_count"],
            "idempotencyKey": row["idempotency_key"], "nextAttemptAt": row["next_attempt_at"],
        }

    def _notes_conn(
        self, conn: sqlite3.Connection, meeting_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM meeting_notes WHERE meeting_id = ? ORDER BY created_at, id", (meeting_id,)
        ).fetchall()
        return [
            {
                "id": row["id"], "meetingId": row["meeting_id"], "body": row["body"],
                "atMs": row["at_ms"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def notes(self, meeting_id: str) -> list[dict[str, Any]]:
        self.get(meeting_id)
        return self._notes_conn(db._get_connection(), meeting_id)

    def _write_segments(
        self,
        meeting_id: str,
        segments: Iterable[dict[str, Any]],
        *,
        replace_revision: str | None = None,
    ) -> list[dict[str, Any]]:
        self.get(meeting_id)
        items = [dict(item) for item in segments]
        if replace_revision not in {None, "live", "canonical"}:
            raise ValueError("Invalid meeting segment revision.")
        if replace_revision is not None:
            for item in items:
                if str(item.get("revision", replace_revision)) != replace_revision:
                    raise ValueError("Replacement segments must share one revision.")
        created: list[dict[str, Any]] = []
        now = _utc_now()
        with db._get_connection() as conn:
            # Canonical finalization is a complete snapshot, not a sparse
            # upsert. Delete and insert stay in this one SQLite transaction so
            # retries with fewer turns cannot leave a stale canonical tail.
            if replace_revision is not None:
                conn.execute(
                    "DELETE FROM meeting_segments WHERE meeting_id=? AND revision=?",
                    (meeting_id, replace_revision),
                )
            for item in items:
                revision = str(item.get("revision", replace_revision or "live"))
                source = str(item.get("source", "mixed"))
                if revision not in {"live", "canonical"} or source not in {"microphone", "system", "mixed"}:
                    raise ValueError("Invalid meeting segment revision or source.")
                segment_id = str(item.get("id") or uuid4().hex)
                sequence = int(item.get("sequence", len(created)))
                alignment_quality = str(item.get("alignmentQuality") or "estimated")
                if alignment_quality not in ALIGNMENT_QUALITIES:
                    raise ValueError("Invalid meeting segment alignment quality.")
                speaker_label = str(item.get("speakerLabel", "")).strip()
                speaker_id = item.get("speakerId")
                if not speaker_id and speaker_label:
                    speaker_id = hashlib.sha256(
                        f"{meeting_id}\0{revision}\0{source}\0{speaker_label.casefold()}".encode("utf-8")
                    ).hexdigest()[:32]
                if speaker_id:
                    conn.execute(
                        """INSERT INTO meeting_speakers
                           (id,meeting_id,label,display_name,source_hint,profile_id,confidence,created_at,updated_at)
                           VALUES (?,?,?,?,?,NULL,NULL,?,?) ON CONFLICT(id) DO NOTHING""",
                        (speaker_id, meeting_id, speaker_label, speaker_label, source, now, now),
                    )
                values = (
                    segment_id, meeting_id, revision, source, str(item.get("providerSegmentId", "")),
                    speaker_id, speaker_label, int(item.get("startMs", 0)),
                    int(item.get("endMs", 0)), str(item.get("text", "")), item.get("confidence"),
                    alignment_quality, int(bool(item.get("isFinal", True))), sequence, now,
                )
                conn.execute(
                    """INSERT INTO meeting_segments (
                       id,meeting_id,revision,source,provider_segment_id,speaker_id,speaker_label,
                       start_ms,end_ms,text,confidence,alignment_quality,is_final,sequence,created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(meeting_id,revision,source,sequence) DO UPDATE SET
                       provider_segment_id=excluded.provider_segment_id,speaker_id=excluded.speaker_id,
                       speaker_label=excluded.speaker_label,start_ms=excluded.start_ms,end_ms=excluded.end_ms,
                       text=excluded.text,confidence=excluded.confidence,
                       alignment_quality=excluded.alignment_quality,is_final=excluded.is_final""",
                    values,
                )
                created.append({**item, "id": segment_id, "meetingId": meeting_id, "revision": revision,
                                "source": source, "speakerId": speaker_id,
                                "speakerLabel": speaker_label, "alignmentQuality": alignment_quality,
                                "sequence": sequence})
            conn.commit()
        return created

    def add_segments(self, meeting_id: str, segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Upsert incremental live/provider segments without removing peers."""
        return self._write_segments(meeting_id, segments)

    def append_live_segment(self, meeting_id: str, segment: dict[str, Any]) -> dict[str, Any]:
        """Allocate and commit one final live segment in a single transaction.

        Live provider callbacks can arrive on independent source tasks. The
        immediate transaction makes sequence allocation and insertion one
        durable operation instead of a racy read followed by a second write.
        Repeated provider ids are idempotent and retain their original sequence.
        """
        item = dict(segment)
        source = str(item.get("source", ""))
        if source not in {"microphone", "system", "mixed"}:
            raise ValueError("Invalid meeting segment source.")
        text = str(item.get("text", ""))
        if not text.strip():
            raise ValueError("Meeting segment text is required.")
        alignment_quality = str(item.get("alignmentQuality") or "estimated")
        if alignment_quality not in ALIGNMENT_QUALITIES:
            raise ValueError("Invalid meeting segment alignment quality.")
        start_ms = max(0, int(item.get("startMs", 0)))
        end_ms = max(start_ms, int(item.get("endMs", start_ms)))
        segment_id = str(item.get("id") or uuid4().hex)
        speaker_label = str(item.get("speakerLabel", "")).strip()
        speaker_id = item.get("speakerId")
        now = _utc_now()

        with db._get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    "SELECT 1 FROM meetings WHERE id=?", (meeting_id,)
                ).fetchone() is None:
                    raise MeetingNotFound(f"Meeting not found: {meeting_id}")
                existing = conn.execute(
                    "SELECT * FROM meeting_segments WHERE id=?", (segment_id,)
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["meeting_id"]) != meeting_id
                        or str(existing["revision"]) != "live"
                    ):
                        raise MeetingConflict("Meeting segment id belongs to another transcript.")
                    conn.commit()
                    return self._segment(existing)

                sequence = int(conn.execute(
                    """SELECT COALESCE(MAX(sequence), -1) + 1
                       FROM meeting_segments
                       WHERE meeting_id=? AND revision='live' AND source=?""",
                    (meeting_id, source),
                ).fetchone()[0])
                if not speaker_id and speaker_label:
                    speaker_id = hashlib.sha256(
                        f"{meeting_id}\0live\0{source}\0{speaker_label.casefold()}".encode("utf-8")
                    ).hexdigest()[:32]
                if speaker_id:
                    conn.execute(
                        """INSERT INTO meeting_speakers
                           (id,meeting_id,label,display_name,source_hint,profile_id,confidence,created_at,updated_at)
                           VALUES (?,?,?,?,?,NULL,NULL,?,?) ON CONFLICT(id) DO NOTHING""",
                        (speaker_id, meeting_id, speaker_label, speaker_label, source, now, now),
                    )
                conn.execute(
                    """INSERT INTO meeting_segments (
                       id,meeting_id,revision,source,provider_segment_id,speaker_id,speaker_label,
                       start_ms,end_ms,text,confidence,alignment_quality,is_final,sequence,created_at
                       ) VALUES (?,?, 'live',?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (
                        segment_id, meeting_id, source,
                        str(item.get("providerSegmentId", "")), speaker_id, speaker_label,
                        start_ms, end_ms, text, item.get("confidence"), alignment_quality,
                        sequence, now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM meeting_segments WHERE id=?", (segment_id,)
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._segment(row)

    def replace_segments(
        self,
        meeting_id: str,
        revision: str,
        segments: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Atomically replace one complete transcript revision."""
        return self._write_segments(
            meeting_id,
            segments,
            replace_revision=revision,
        )

    def edit_segment(
        self,
        meeting_id: str,
        segment_id: str,
        text: str,
        *,
        expected_edit_version: int,
        operation: str = "edit",
    ) -> dict[str, Any]:
        """Append an immutable correction and update the canonical projection."""
        normalized = str(text).strip()
        if not normalized:
            raise ValueError("Transcript segment text cannot be empty.")
        if len(normalized) > 50_000:
            raise ValueError("Transcript segment text is too long.")
        if operation not in {"edit", "undo"}:
            raise ValueError("Invalid transcript edit operation.")
        now = _utc_now()
        with db._get_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                meeting = conn.execute(
                    "SELECT state,transcript_edit_version FROM meetings WHERE id=?",
                    (meeting_id,),
                ).fetchone()
                if meeting is None:
                    raise MeetingNotFound(f"Meeting not found: {meeting_id}")
                if str(meeting["state"]) not in {"ready", "analysis_failed"}:
                    raise MeetingConflict(
                        "Transcript corrections are available after final transcription is complete."
                    )
                current_version = int(meeting["transcript_edit_version"] or 0)
                if current_version != int(expected_edit_version):
                    raise MeetingConflict(
                        "The transcript changed in another view. Reload before saving this correction."
                    )
                segment = conn.execute(
                    """SELECT * FROM meeting_segments
                       WHERE id=? AND meeting_id=? AND revision='canonical'""",
                    (segment_id, meeting_id),
                ).fetchone()
                if segment is None:
                    raise MeetingNotFound("Canonical meeting segment not found")
                previous_text = str(segment["text"])
                if normalized == previous_text:
                    conn.commit()
                    return {
                        "meetingId": meeting_id,
                        "segment": self._segment(segment),
                        "transcriptEditVersion": current_version,
                        "outputsStale": self._outputs_stale_conn(
                            conn, meeting_id, current_version
                        ),
                        "edit": None,
                    }
                next_version = current_version + 1
                edit_id = uuid4().hex
                conn.execute(
                    """INSERT INTO meeting_segment_edits
                       (id,meeting_id,segment_id,edit_version,operation,previous_text,text,created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        edit_id, meeting_id, segment_id, next_version, operation,
                        previous_text, normalized, now,
                    ),
                )
                conn.execute(
                    """UPDATE meeting_segments
                       SET text=?,edit_version=?,edited_at=? WHERE id=?""",
                    (normalized, next_version, now, segment_id),
                )
                conn.execute(
                    """UPDATE meetings SET transcript_edit_version=?,updated_at=?
                       WHERE id=?""",
                    (next_version, now, meeting_id),
                )
                updated = conn.execute(
                    "SELECT * FROM meeting_segments WHERE id=?", (segment_id,)
                ).fetchone()
                outputs_stale = self._outputs_stale_conn(conn, meeting_id, next_version)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "meetingId": meeting_id,
            "segment": self._segment(updated),
            "transcriptEditVersion": next_version,
            "outputsStale": outputs_stale,
            "edit": {
                "id": edit_id,
                "meetingId": meeting_id,
                "segmentId": segment_id,
                "editVersion": next_version,
                "operation": operation,
                "previousText": previous_text,
                "text": normalized,
                "createdAt": now,
            },
        }

    def undo_segment_edit(
        self,
        meeting_id: str,
        segment_id: str,
        *,
        expected_edit_version: int,
    ) -> dict[str, Any]:
        row = db._get_connection().execute(
            """SELECT previous_text FROM meeting_segment_edits
               WHERE meeting_id=? AND segment_id=?
               ORDER BY edit_version DESC LIMIT 1""",
            (meeting_id, segment_id),
        ).fetchone()
        if row is None:
            raise MeetingConflict("This transcript segment has no correction to undo.")
        return self.edit_segment(
            meeting_id,
            segment_id,
            str(row["previous_text"]),
            expected_edit_version=expected_edit_version,
            operation="undo",
        )

    def segment_edit_history(self, meeting_id: str, segment_id: str) -> list[dict[str, Any]]:
        self.get(meeting_id)
        rows = db._get_connection().execute(
            """SELECT * FROM meeting_segment_edits
               WHERE meeting_id=? AND segment_id=?
               ORDER BY edit_version DESC""",
            (meeting_id, segment_id),
        ).fetchall()
        return [
            {
                "id": row["id"], "meetingId": row["meeting_id"],
                "segmentId": row["segment_id"], "editVersion": row["edit_version"],
                "operation": row["operation"], "previousText": row["previous_text"],
                "text": row["text"], "createdAt": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _outputs_stale_conn(
        conn: sqlite3.Connection, meeting_id: str, edit_version: int
    ) -> bool:
        return conn.execute(
            """SELECT 1 FROM meeting_outputs
               WHERE meeting_id=? AND status='completed' AND transcript_edit_version<?
               LIMIT 1""",
            (meeting_id, int(edit_version)),
        ).fetchone() is not None

    def next_segment_sequence(self, meeting_id: str, revision: str, source: str) -> int:
        row = db._get_connection().execute(
            """SELECT COALESCE(MAX(sequence), -1) + 1 FROM meeting_segments
               WHERE meeting_id = ? AND revision = ? AND source = ?""",
            (meeting_id, revision, source),
        ).fetchone()
        return int(row[0])

    def search_segments(self, meeting_id: str, query: str, *, limit: int = 40) -> list[dict[str, Any]]:
        self.get(meeting_id)
        terms = [term for term in re.findall(r"[\w-]+", query, flags=re.UNICODE) if len(term) > 1]
        if not terms:
            return []
        conn = db._get_connection()
        revision = "canonical" if conn.execute(
            "SELECT 1 FROM meeting_segments WHERE meeting_id=? AND revision='canonical' LIMIT 1",
            (meeting_id,),
        ).fetchone() else "live"
        expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:16])
        hits = conn.execute(
            """SELECT s.start_ms,s.end_ms FROM meeting_segments_fts f
               JOIN meeting_segments s ON s.id=f.segment_id AND s.meeting_id=f.meeting_id
               WHERE meeting_segments_fts MATCH ? AND f.meeting_id=? AND f.revision=?
               ORDER BY bm25(meeting_segments_fts),s.start_ms LIMIT ?""",
            (expression, meeting_id, revision, max(1, min(20, limit))),
        ).fetchall()
        if not hits:
            return []
        clauses: list[str] = []
        params: list[Any] = [meeting_id]
        for hit in hits:
            clauses.append("(start_ms <= ? AND end_ms >= ?)")
            params.extend([int(hit["end_ms"]) + 30_000, max(0, int(hit["start_ms"]) - 30_000)])
        params.append(max(1, min(100, limit)))
        params.insert(1, revision)
        rows = conn.execute(
            f"""SELECT * FROM meeting_segments WHERE meeting_id=? AND revision=?
                AND ({' OR '.join(clauses)}) ORDER BY start_ms,sequence LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [self._segment(row) for row in rows]

    def detail(self, meeting_id: str, *, revision: str = "canonical") -> dict[str, Any]:
        meeting = self.get(meeting_id)
        conn = db._get_connection()
        rows = conn.execute(
            """SELECT * FROM meeting_segments WHERE meeting_id = ? AND revision = ?
               ORDER BY start_ms, sequence""",
            (meeting_id, revision),
        ).fetchall()
        if not rows and revision == "canonical":
            rows = conn.execute(
                """SELECT * FROM meeting_segments WHERE meeting_id = ? AND revision = 'live'
                   ORDER BY start_ms, sequence""",
                (meeting_id,),
            ).fetchall()
        outputs = conn.execute(
            "SELECT * FROM meeting_outputs WHERE meeting_id = ? ORDER BY created_at", (meeting_id,)
        ).fetchall()
        output_versions = conn.execute(
            """SELECT * FROM meeting_output_versions WHERE meeting_id=?
               ORDER BY kind,schema_version,version""",
            (meeting_id,),
        ).fetchall()
        meeting["segments"] = [self._segment(row) for row in rows]
        speaker_rows = conn.execute(
            "SELECT * FROM meeting_speakers WHERE meeting_id=? ORDER BY created_at,id", (meeting_id,)
        ).fetchall()
        meeting["speakers"] = [
            {"id": row["id"], "meetingId": row["meeting_id"], "label": row["label"],
             "displayName": row["display_name"], "sourceHint": row["source_hint"],
             "profileId": row["profile_id"], "confidence": row["confidence"],
             "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
            for row in speaker_rows
        ]
        meeting["notes"] = self._notes_conn(conn, meeting_id)
        meeting["actionItems"] = self._action_items_conn(conn, meeting_id)
        meeting["audioGaps"] = self._audio_gaps_conn(conn, meeting_id)
        meeting["audioAssets"] = self._audio_assets_conn(conn, meeting_id)
        meeting["transcriptCheckpoints"] = self._transcript_checkpoints_conn(conn, meeting_id)
        meeting["outputs"] = [
            {
                "id": row["id"], "kind": row["kind"], "schemaVersion": row["schema_version"],
                "version": row["version"], "supersedesId": row["supersedes_id"],
                "transcriptRevision": row["transcript_revision"], "provider": row["provider"],
                "transcriptEditVersion": row["transcript_edit_version"],
                "status": row["status"], "payload": _loads(row["payload_json"], {}),
                "errorMessage": row["error_message"], "updatedAt": row["updated_at"],
            }
            for row in outputs
        ]
        meeting["outputVersions"] = [
            {
                "id": row["id"], "kind": row["kind"], "schemaVersion": row["schema_version"],
                "version": row["version"], "supersedesId": row["supersedes_id"],
                "transcriptRevision": row["transcript_revision"], "provider": row["provider"],
                "transcriptEditVersion": row["transcript_edit_version"],
                "status": row["status"], "payload": _loads(row["payload_json"], {}),
                "errorMessage": row["error_message"], "createdAt": row["created_at"],
            }
            for row in output_versions
        ]
        return meeting

    @staticmethod
    def _meeting(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "title": row["title"], "state": row["state"], "language": row["language"],
            "liveProvider": row["live_provider"], "finalProvider": row["final_provider"],
            "analysisModel": row["analysis_model"], "aecEnabled": bool(row["aec_enabled"]),
            "voiceLibraryEnabled": bool(row["voice_library_enabled"]),
            "consentConfirmed": bool(row["consent_confirmed"]), "origin": row["origin"],
            "startedAt": row["started_at"],
            "endedAt": row["ended_at"], "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "errorCode": row["error_code"], "errorMessage": row["error_message"],
            "captureMetadata": _loads(row["capture_metadata_json"], {}),
            "audioRetentionDays": int(row["audio_retention_days"] or 0),
            "smartTurnEnabled": bool(row["smart_turn_enabled"]),
            "autoAnalyze": bool(row["auto_analyze"]),
            "transcriptEditVersion": int(row["transcript_edit_version"] or 0),
        }

    @staticmethod
    def _segment(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "meetingId": row["meeting_id"], "revision": row["revision"],
            "source": row["source"], "providerSegmentId": row["provider_segment_id"],
            "speakerId": row["speaker_id"], "speakerLabel": row["speaker_label"],
            "startMs": row["start_ms"], "endMs": row["end_ms"],
            "durationMs": max(0, int(row["end_ms"]) - int(row["start_ms"])),
            "text": row["text"],
            "confidence": row["confidence"], "alignmentQuality": row["alignment_quality"],
            "isFinal": bool(row["is_final"]),
            "sequence": row["sequence"], "createdAt": row["created_at"],
            "editVersion": int(row["edit_version"] or 0),
            "editedAt": row["edited_at"],
        }
