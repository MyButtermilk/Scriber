"""Durable, immutable transcript-artifact persistence.

This module deliberately owns persistence only.  Provider calls, local
diarization, and REST projections stay outside this boundary.  The store keeps
enough normalized evidence to resume an attempt after ``provider_result_ready``
without repeating the cloud request.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from src.runtime.paths import database_path


class TranscriptArtifactStoreError(RuntimeError):
    pass


class ArtifactNotFound(TranscriptArtifactStoreError):
    pass


class ArtifactConflict(TranscriptArtifactStoreError):
    """A compare-and-swap or immutable-write precondition did not hold."""


class InvalidAttemptTransition(TranscriptArtifactStoreError):
    pass


class UnsafeSnapshotValue(ValueError):
    pass


class AttemptState(str, Enum):
    QUEUED = "queued"
    RESOLVING_SOURCE = "resolving_source"
    SOURCE_READY = "source_ready"
    TRANSCRIBING = "transcribing"
    PROVIDER_RESULT_READY = "provider_result_ready"
    DIARIZING = "diarizing"
    CANONICALIZING = "canonicalizing"
    COMMITTING = "committing"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_ATTEMPT_STATES = frozenset(
    {
        AttemptState.COMPLETED,
        AttemptState.SUPERSEDED,
        AttemptState.FAILED,
        AttemptState.CANCELED,
    }
)


ALLOWED_ATTEMPT_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.QUEUED: frozenset(
        {AttemptState.RESOLVING_SOURCE, AttemptState.FAILED, AttemptState.CANCELED}
    ),
    AttemptState.RESOLVING_SOURCE: frozenset(
        {AttemptState.SOURCE_READY, AttemptState.FAILED, AttemptState.CANCELED}
    ),
    AttemptState.SOURCE_READY: frozenset(
        {AttemptState.TRANSCRIBING, AttemptState.FAILED, AttemptState.CANCELED}
    ),
    AttemptState.TRANSCRIBING: frozenset(
        {AttemptState.PROVIDER_RESULT_READY, AttemptState.FAILED, AttemptState.CANCELED}
    ),
    AttemptState.PROVIDER_RESULT_READY: frozenset(
        {
            AttemptState.DIARIZING,
            AttemptState.CANONICALIZING,
            AttemptState.FAILED,
            AttemptState.CANCELED,
        }
    ),
    AttemptState.DIARIZING: frozenset(
        {AttemptState.CANONICALIZING, AttemptState.FAILED, AttemptState.CANCELED}
    ),
    AttemptState.CANONICALIZING: frozenset(
        {AttemptState.COMMITTING, AttemptState.FAILED, AttemptState.CANCELED}
    ),
    AttemptState.COMMITTING: frozenset(
        {AttemptState.COMPLETED, AttemptState.SUPERSEDED, AttemptState.FAILED}
    ),
    AttemptState.COMPLETED: frozenset(),
    AttemptState.SUPERSEDED: frozenset(),
    AttemptState.FAILED: frozenset(),
    AttemptState.CANCELED: frozenset(),
}


class AlignmentQuality(str, Enum):
    EXACT_WORD = "exact_word"
    PROVIDER_SEGMENT = "provider_segment"
    ESTIMATED = "estimated"


class SourceAssetState(str, Enum):
    AVAILABLE = "available"
    PURGE_PENDING = "purge_pending"
    PURGED = "purged"


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FTS_TOKEN_RE = re.compile(r"\w+(?:-\w+)*", re.UNICODE)
_SAFE_TOKEN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_COMMON_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{12,}|xox[baprs]-\S+)"
)
_EMBEDDED_WINDOWS_PATH_RE = re.compile(r"(?:^|\s)[A-Za-z]:[\\/]")
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "access_token",
        "refresh_token",
        "session_token",
        "secret",
        "password",
        "credential",
        "signed_url",
        "source_url",
        "custom_vocabulary",
        "vocabulary",
        "phrase_list",
        "phrases",
        "keywords",
        "keyterms",
        "terms",
        "hotwords",
        "speech_hints",
        "context_bias",
        "prompt",
        "prompt_text",
        "url",
        "uri",
        "path",
        "file_path",
        "file",
        "filename",
        "directory",
    }
)
_SAFE_SENSITIVE_SUFFIXES = (
    "_sha256",
    "_digest",
    "_present",
    "_presence",
    "_count",
    "_length",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Value must be finite, JSON-serializable data.") from exc


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_array(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(value: str, *, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest.")
    return text


def _safe_scalar(value: Any, *, field_name: str, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not text or not _SAFE_TOKEN_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a non-empty single-line value.")
    return text


def _snake_key(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value)
    return value.strip("_").lower()


def _key_is_sensitive(key: str) -> bool:
    normalized = _snake_key(key)
    if normalized in _SENSITIVE_KEY_PARTS:
        return True
    return any(
        normalized.startswith(f"{part}_")
        or normalized.endswith(f"_{part}")
        or f"_{part}_" in normalized
        for part in _SENSITIVE_KEY_PARTS
    )


def _validate_safe_persisted_json(value: Any, *, field_name: str) -> Any:
    """Reject secrets and machine-local locations before SQLite sees them."""

    def visit(item: Any, path: str, key_name: str = "") -> None:
        if isinstance(item, Mapping):
            for raw_key, nested in item.items():
                if not isinstance(raw_key, str) or not raw_key:
                    raise UnsafeSnapshotValue(f"{path} contains a non-string or empty key.")
                normalized = _snake_key(raw_key)
                sensitive = _key_is_sensitive(raw_key)
                safe_metadata = normalized.endswith(_SAFE_SENSITIVE_SUFFIXES)
                if sensitive and not safe_metadata:
                    raise UnsafeSnapshotValue(f"{path}.{raw_key} may contain secret or local data.")
                if sensitive and safe_metadata:
                    if normalized.endswith(("_sha256", "_digest")):
                        if not isinstance(nested, str) or not _SHA256_RE.fullmatch(nested.lower()):
                            raise UnsafeSnapshotValue(
                                f"{path}.{raw_key} must contain only a SHA-256 digest."
                            )
                    elif normalized.endswith(("_present", "_presence")) and not isinstance(
                        nested, bool
                    ):
                        raise UnsafeSnapshotValue(f"{path}.{raw_key} must be boolean metadata.")
                    elif normalized.endswith(("_count", "_length")) and (
                        isinstance(nested, bool) or not isinstance(nested, int) or nested < 0
                    ):
                        raise UnsafeSnapshotValue(
                            f"{path}.{raw_key} must be non-negative integer metadata."
                        )
                visit(nested, f"{path}.{raw_key}", normalized)
            return
        if isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]", key_name)
            return
        if item is None or isinstance(item, (bool, int, float)):
            return
        if not isinstance(item, str):
            raise UnsafeSnapshotValue(f"{path} contains unsupported persisted data.")
        if "\x00" in item or "\n" in item or "\r" in item:
            raise UnsafeSnapshotValue(f"{path} must not contain control characters.")
        stripped = item.strip()
        if _EMBEDDED_WINDOWS_PATH_RE.search(stripped) or stripped.startswith(("/", "\\\\")):
            raise UnsafeSnapshotValue(f"{path} must not contain an absolute local path.")
        if _URI_RE.match(stripped):
            raise UnsafeSnapshotValue(f"{path} must not contain a URL.")
        if _COMMON_SECRET_VALUE_RE.search(stripped):
            raise UnsafeSnapshotValue(f"{path} appears to contain a credential.")

    visit(value, field_name)
    # Round-trip also rejects non-finite floats and detaches mutable caller data.
    return json.loads(_canonical_json(value))


def _relative_runtime_path(value: str, *, allow_empty: bool = False) -> str:
    raw = str(value or "").strip()
    if "\x00" in raw:
        raise ValueError("Asset paths must not contain NUL bytes.")
    if PureWindowsPath(raw).drive:
        raise ValueError("Asset paths must not contain a Windows drive or UNC share.")
    if _URI_RE.match(raw):
        raise ValueError("Asset paths must be local paths relative to SCRIBER_DATA_DIR.")
    text = raw.replace("\\", "/")
    if not text and allow_empty:
        return ""
    if not text or text.startswith(("/", "//")) or _WINDOWS_ABSOLUTE_RE.match(text):
        raise ValueError("Asset paths must be relative to SCRIBER_DATA_DIR.")
    path = PurePosixPath(text)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Asset paths must be normalized and remain under SCRIBER_DATA_DIR.")
    return path.as_posix()


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _preview(value: str, max_words: int = 16) -> str:
    words = value.split()
    if not words:
        return ""
    return " ".join(words[:max_words]) + ("..." if len(words) > max_words else "")


def _fts_query(value: str) -> str:
    tokens = _FTS_TOKEN_RE.findall(str(value or "").lower())
    return " AND ".join(
        f'"{token}"*' if len(token) >= 2 else f'"{token}"' for token in tokens[:12]
    )


def _speaker_key(value: str | int | None) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        raise ValueError("speaker_key must not be boolean.")
    text = str(value).strip()
    if not text or not _SAFE_TOKEN_RE.fullmatch(text):
        raise ValueError("speaker_key must be a single-line value when present.")
    return text


def _attempt_state(value: AttemptState | str) -> AttemptState:
    return value if isinstance(value, AttemptState) else AttemptState(str(value))


def _alignment_quality(value: AlignmentQuality | str) -> AlignmentQuality:
    return value if isinstance(value, AlignmentQuality) else AlignmentQuality(str(value))


@dataclass(frozen=True)
class RouteSnapshotDraft:
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
    request_options: Mapping[str, Any] = field(default_factory=dict)
    local_worker_manifest: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteSnapshot:
    id: str
    attempt_id: str
    transcript_id: str
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
    request_options: dict[str, Any]
    local_worker_manifest: dict[str, Any]
    snapshot_sha256: str
    created_at: str


@dataclass(frozen=True)
class AttemptRecord:
    id: str
    transcript_id: str
    attempt_number: int
    workload: str
    state: AttemptState
    state_version: int
    expected_head_generation: int
    lease_owner: str
    lease_expires_at: str
    error_code: str
    error_message: str
    canonical_artifact_id: str
    created_at: str
    updated_at: str
    finished_at: str


@dataclass(frozen=True)
class StageUnit:
    source_track: str
    start_ms: int
    end_ms: int
    text: str
    speaker_key: str | int | None = None
    speaker_label: str = ""
    timing_origin: str = "provider"
    speaker_origin: str = "none"
    alignment_quality: AlignmentQuality | str = AlignmentQuality.PROVIDER_SEGMENT
    provider_native_id: str = ""


@dataclass(frozen=True)
class NormalizedStageResult:
    id: str
    attempt_id: str
    route_snapshot_id: str
    transcript_text: str
    units: tuple[StageUnit, ...]
    evidence: dict[str, Any]
    result_sha256: str
    created_at: str


@dataclass(frozen=True)
class TrackStageResult:
    id: str
    attempt_id: str
    route_snapshot_id: str
    source_track: str
    transcript_text: str
    units: tuple[StageUnit, ...]
    evidence: dict[str, Any]
    result_sha256: str
    created_at: str


@dataclass(frozen=True)
class TrackDerivationResult:
    id: str
    attempt_id: str
    route_snapshot_id: str
    parent_stage_result_id: str
    source_track: str
    derivation_kind: str
    units: tuple[StageUnit, ...]
    evidence: dict[str, Any]
    result_sha256: str
    created_at: str


@dataclass(frozen=True)
class RecoveryBundle:
    attempt: AttemptRecord
    route_snapshot: RouteSnapshot
    stage_result: NormalizedStageResult


@dataclass(frozen=True)
class TrackRecoveryBundle:
    attempt: AttemptRecord
    route_snapshot: RouteSnapshot
    track_results: tuple[TrackStageResult, ...]
    track_derivations: tuple[TrackDerivationResult, ...] = ()


@dataclass(frozen=True)
class CanonicalSegmentDraft:
    source_track: str
    start_ms: int
    end_ms: int
    text: str
    speaker_key: str | int | None = None
    speaker_label: str = ""
    timing_origin: str = "provider"
    speaker_origin: str = "none"
    alignment_quality: AlignmentQuality | str = AlignmentQuality.PROVIDER_SEGMENT
    provider_native_id: str = ""


@dataclass(frozen=True)
class CanonicalSegment:
    artifact_id: str
    segment_id: str
    order_index: int
    source_track: str
    start_ms: int
    end_ms: int
    duration_ms: int
    text: str
    speaker_key: str
    speaker_label: str
    timing_origin: str
    speaker_origin: str
    alignment_quality: AlignmentQuality
    provider_native_id: str
    occurrence_index: int


@dataclass(frozen=True)
class ArtifactInputDraft:
    input_kind: str
    input_id: str
    input_sha256: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalArtifact:
    id: str
    transcript_id: str
    attempt_id: str
    generation: int
    route_snapshot_id: str
    stage_result_id: str
    schema_version: int
    artifact_sha256: str
    created_at: str
    segments: tuple[CanonicalSegment, ...] = ()


@dataclass(frozen=True)
class CanonicalHead:
    transcript_id: str
    artifact_id: str
    generation: int
    updated_at: str


@dataclass(frozen=True)
class CanonicalCommitResult:
    artifact: CanonicalArtifact | None
    head: CanonicalHead | None
    attempt: AttemptRecord
    committed: bool
    superseded: bool


@dataclass(frozen=True)
class SourceAsset:
    id: str
    transcript_id: str
    source_track: str
    asset_kind: str
    purpose: str
    state: SourceAssetState
    state_version: int
    relative_path: str
    sha256: str
    byte_count: int
    tombstone_reason: str
    created_at: str
    updated_at: str
    purged_at: str

    def to_public(self) -> dict[str, Any]:
        """Public state intentionally omits machine-local paths and digests."""
        return {
            "id": self.id,
            "transcriptId": self.transcript_id,
            "sourceTrack": self.source_track,
            "assetKind": self.asset_kind,
            "purpose": self.purpose,
            "state": self.state.value,
            "byteCount": self.byte_count,
            "tombstoneReason": self.tombstone_reason,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "purgedAt": self.purged_at,
        }


class TranscriptArtifactStore:
    """SQLite boundary for frozen routes, attempts, and canonical artifacts."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        fault_injector: Callable[[str, sqlite3.Connection], None] | None = None,
    ) -> None:
        self._db_path = db_path or database_path()
        self._fault_injector = fault_injector
        self._thread_local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._connection_generation = 0
        self._schema_lock = threading.Lock()
        self._connection_finalizer = weakref.finalize(
            self,
            self._close_connection_list,
            self._connections,
            self._connections_lock,
        )
        self.init_schema()

    @staticmethod
    def _close_connection_list(
        connections: list[sqlite3.Connection],
        lock: threading.Lock | None = None,
    ) -> None:
        if lock is None:
            pending = list(connections)
            connections.clear()
        else:
            with lock:
                pending = list(connections)
                connections.clear()
        for conn in pending:
            try:
                conn.close()
            except Exception:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._thread_local, "conn", None)
        if conn is None or getattr(
            self._thread_local, "connection_generation", -1
        ) != self._connection_generation:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._thread_local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
                self._thread_local.connection_generation = self._connection_generation
        return conn

    def close(self) -> None:
        with self._connections_lock:
            pending = list(self._connections)
            self._connections.clear()
            self._connection_generation += 1
        self._close_connection_list(pending)
        self._thread_local.conn = None
        self._thread_local.connection_generation = self._connection_generation

    def _fault(self, name: str, conn: sqlite3.Connection) -> None:
        if self._fault_injector is not None:
            self._fault_injector(name, conn)

    def init_schema(self) -> None:
        """Create only additive tables/indexes; safe to run on every startup."""
        statements = (
            """
            CREATE TABLE IF NOT EXISTS transcription_attempts (
                id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                workload TEXT NOT NULL,
                state TEXT NOT NULL,
                state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0),
                expected_head_generation INTEGER NOT NULL DEFAULT 0
                    CHECK(expected_head_generation >= 0),
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                canonical_artifact_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                UNIQUE(transcript_id, attempt_number),
                FOREIGN KEY(transcript_id) REFERENCES transcripts(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS transcription_route_snapshots (
                id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE,
                transcript_id TEXT NOT NULL,
                workload TEXT NOT NULL,
                source_track TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                transport TEXT NOT NULL,
                language TEXT NOT NULL,
                response_shape TEXT NOT NULL,
                timestamp_mode TEXT NOT NULL,
                diarization_mode TEXT NOT NULL,
                parser_id TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                request_options_json TEXT NOT NULL DEFAULT '{}',
                local_worker_manifest_json TEXT NOT NULL DEFAULT '{}',
                snapshot_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES transcription_attempts(id) ON DELETE CASCADE,
                FOREIGN KEY(transcript_id) REFERENCES transcripts(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS transcription_stage_results (
                id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE,
                route_snapshot_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                response_shape TEXT NOT NULL,
                parser_id TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                transcript_text TEXT NOT NULL DEFAULT '',
                units_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                result_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(attempt_id) REFERENCES transcription_attempts(id) ON DELETE CASCADE,
                FOREIGN KEY(route_snapshot_id) REFERENCES transcription_route_snapshots(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS transcription_track_stage_results (
                id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                route_snapshot_id TEXT NOT NULL,
                source_track TEXT NOT NULL,
                transcript_text TEXT NOT NULL DEFAULT '',
                units_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                result_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(attempt_id, source_track),
                FOREIGN KEY(attempt_id) REFERENCES transcription_attempts(id) ON DELETE CASCADE,
                FOREIGN KEY(route_snapshot_id) REFERENCES transcription_route_snapshots(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS transcription_track_derivations (
                id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                route_snapshot_id TEXT NOT NULL,
                parent_stage_result_id TEXT NOT NULL,
                source_track TEXT NOT NULL,
                derivation_kind TEXT NOT NULL,
                units_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                result_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(attempt_id, source_track, derivation_kind),
                FOREIGN KEY(attempt_id) REFERENCES transcription_attempts(id) ON DELETE CASCADE,
                FOREIGN KEY(route_snapshot_id) REFERENCES transcription_route_snapshots(id),
                FOREIGN KEY(parent_stage_result_id)
                    REFERENCES transcription_track_stage_results(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS canonical_transcript_artifacts (
                id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL UNIQUE,
                generation INTEGER NOT NULL CHECK(generation > 0),
                route_snapshot_id TEXT NOT NULL,
                stage_result_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                artifact_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(transcript_id, generation),
                FOREIGN KEY(transcript_id) REFERENCES transcripts(id) ON DELETE CASCADE,
                FOREIGN KEY(attempt_id) REFERENCES transcription_attempts(id),
                FOREIGN KEY(route_snapshot_id) REFERENCES transcription_route_snapshots(id),
                FOREIGN KEY(stage_result_id) REFERENCES transcription_stage_results(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS canonical_transcript_segments (
                artifact_id TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                order_index INTEGER NOT NULL CHECK(order_index >= 0),
                source_track TEXT NOT NULL,
                start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
                duration_ms INTEGER NOT NULL CHECK(duration_ms = end_ms - start_ms),
                text TEXT NOT NULL,
                speaker_key TEXT NOT NULL DEFAULT '',
                speaker_label TEXT NOT NULL DEFAULT '',
                timing_origin TEXT NOT NULL,
                speaker_origin TEXT NOT NULL,
                alignment_quality TEXT NOT NULL,
                provider_native_id TEXT NOT NULL DEFAULT '',
                occurrence_index INTEGER NOT NULL DEFAULT 0 CHECK(occurrence_index >= 0),
                PRIMARY KEY(artifact_id, segment_id),
                UNIQUE(artifact_id, order_index),
                FOREIGN KEY(artifact_id) REFERENCES canonical_transcript_artifacts(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS canonical_transcript_heads (
                transcript_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK(generation > 0),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(transcript_id) REFERENCES transcripts(id) ON DELETE CASCADE,
                FOREIGN KEY(artifact_id) REFERENCES canonical_transcript_artifacts(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS canonical_artifact_inputs (
                artifact_id TEXT NOT NULL,
                input_kind TEXT NOT NULL,
                input_id TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(artifact_id, input_kind, input_id),
                FOREIGN KEY(artifact_id) REFERENCES canonical_transcript_artifacts(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS canonical_transcript_segments_fts USING fts5(
                artifact_id UNINDEXED,
                segment_id UNINDEXED,
                text,
                speaker_label,
                source_track
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_canonical_segment_fts_delete
            AFTER DELETE ON canonical_transcript_segments
            BEGIN
                DELETE FROM canonical_transcript_segments_fts
                WHERE artifact_id = OLD.artifact_id AND segment_id = OLD.segment_id;
            END
            """,
            """
            CREATE TABLE IF NOT EXISTS transcript_source_assets (
                id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL,
                source_track TEXT NOT NULL,
                asset_kind TEXT NOT NULL,
                purpose TEXT NOT NULL,
                state TEXT NOT NULL,
                state_version INTEGER NOT NULL DEFAULT 0 CHECK(state_version >= 0),
                relative_path TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL,
                byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
                tombstone_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                purged_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(transcript_id) REFERENCES transcripts(id) ON DELETE CASCADE
            )
            """,
            """CREATE INDEX IF NOT EXISTS idx_transcription_attempts_recovery
               ON transcription_attempts(state, lease_expires_at, updated_at)""",
            """CREATE INDEX IF NOT EXISTS idx_transcription_attempts_transcript
               ON transcription_attempts(transcript_id, attempt_number DESC)""",
            """CREATE INDEX IF NOT EXISTS idx_track_stage_results_attempt
               ON transcription_track_stage_results(attempt_id, source_track)""",
            """CREATE INDEX IF NOT EXISTS idx_track_derivations_attempt
               ON transcription_track_derivations(attempt_id, source_track, derivation_kind)""",
            """CREATE INDEX IF NOT EXISTS idx_canonical_segments_timeline
               ON canonical_transcript_segments(artifact_id, start_ms, order_index)""",
            """CREATE INDEX IF NOT EXISTS idx_source_assets_transcript_state
               ON transcript_source_assets(transcript_id, state, purpose)""",
        )
        with self._schema_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for statement in statements:
                    conn.execute(statement)
                segment_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM canonical_transcript_segments"
                    ).fetchone()[0]
                )
                fts_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM canonical_transcript_segments_fts"
                    ).fetchone()[0]
                )
                missing_fts = conn.execute(
                    """
                    SELECT 1
                    FROM canonical_transcript_segments s
                    LEFT JOIN canonical_transcript_segments_fts f
                      ON f.artifact_id = s.artifact_id AND f.segment_id = s.segment_id
                    WHERE f.segment_id IS NULL
                    LIMIT 1
                    """
                ).fetchone()
                orphaned_fts = conn.execute(
                    """
                    SELECT 1
                    FROM canonical_transcript_segments_fts f
                    LEFT JOIN canonical_transcript_segments s
                      ON s.artifact_id = f.artifact_id AND s.segment_id = f.segment_id
                    WHERE s.segment_id IS NULL
                    LIMIT 1
                    """
                ).fetchone()
                if (
                    segment_count != fts_count
                    or missing_fts is not None
                    or orphaned_fts is not None
                ):
                    conn.execute("DELETE FROM canonical_transcript_segments_fts")
                    conn.execute(
                        """
                        INSERT INTO canonical_transcript_segments_fts
                            (artifact_id, segment_id, text, speaker_label, source_track)
                        SELECT artifact_id, segment_id, text, speaker_label, source_track
                        FROM canonical_transcript_segments
                        """
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            id=str(row["id"]),
            transcript_id=str(row["transcript_id"]),
            attempt_number=int(row["attempt_number"]),
            workload=str(row["workload"]),
            state=AttemptState(str(row["state"])),
            state_version=int(row["state_version"]),
            expected_head_generation=int(row["expected_head_generation"]),
            lease_owner=str(row["lease_owner"] or ""),
            lease_expires_at=str(row["lease_expires_at"] or ""),
            error_code=str(row["error_code"] or ""),
            error_message=str(row["error_message"] or ""),
            canonical_artifact_id=str(row["canonical_artifact_id"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            finished_at=str(row["finished_at"] or ""),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> RouteSnapshot:
        snapshot = RouteSnapshot(
            id=str(row["id"]),
            attempt_id=str(row["attempt_id"]),
            transcript_id=str(row["transcript_id"]),
            workload=str(row["workload"]),
            source_track=str(row["source_track"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            transport=str(row["transport"]),
            language=str(row["language"]),
            response_shape=str(row["response_shape"]),
            timestamp_mode=str(row["timestamp_mode"]),
            diarization_mode=str(row["diarization_mode"]),
            parser_id=str(row["parser_id"]),
            parser_version=str(row["parser_version"]),
            request_options=_json_object(row["request_options_json"]),
            local_worker_manifest=_json_object(row["local_worker_manifest_json"]),
            snapshot_sha256=str(row["snapshot_sha256"]),
            created_at=str(row["created_at"]),
        )
        payload = {
            "workload": snapshot.workload,
            "sourceTrack": snapshot.source_track,
            "provider": snapshot.provider,
            "model": snapshot.model,
            "transport": snapshot.transport,
            "language": snapshot.language,
            "responseShape": snapshot.response_shape,
            "timestampMode": snapshot.timestamp_mode,
            "diarizationMode": snapshot.diarization_mode,
            "parserId": snapshot.parser_id,
            "parserVersion": snapshot.parser_version,
            "requestOptions": snapshot.request_options,
            "localWorkerManifest": snapshot.local_worker_manifest,
        }
        if _sha256_text(_canonical_json(payload)) != snapshot.snapshot_sha256:
            raise ArtifactConflict("Route snapshot checksum validation failed.")
        return snapshot

    @staticmethod
    def _coerce_stage_unit(value: StageUnit | Mapping[str, Any]) -> StageUnit:
        if isinstance(value, StageUnit):
            return value
        return StageUnit(
            source_track=value.get("source_track", value.get("sourceTrack", "")),
            start_ms=value.get("start_ms", value.get("startMs", 0)),
            end_ms=value.get("end_ms", value.get("endMs", 0)),
            text=value.get("text", ""),
            speaker_key=value.get("speaker_key", value.get("speakerKey")),
            speaker_label=value.get("speaker_label", value.get("speakerLabel", "")),
            timing_origin=value.get("timing_origin", value.get("timingOrigin", "provider")),
            speaker_origin=value.get("speaker_origin", value.get("speakerOrigin", "none")),
            alignment_quality=value.get(
                "alignment_quality", value.get("alignmentQuality", "provider_segment")
            ),
            provider_native_id=value.get(
                "provider_native_id", value.get("providerNativeId", "")
            ),
        )

    @classmethod
    def _validated_unit(cls, value: StageUnit | Mapping[str, Any]) -> StageUnit:
        unit = cls._coerce_stage_unit(value)
        start_ms = int(unit.start_ms)
        end_ms = int(unit.end_ms)
        if isinstance(unit.start_ms, bool) or isinstance(unit.end_ms, bool):
            raise ValueError("Stage timestamps must be integer milliseconds.")
        if start_ms != unit.start_ms or end_ms != unit.end_ms or start_ms < 0 or end_ms < start_ms:
            raise ValueError("Stage timestamps must be ordered integer milliseconds.")
        text = _normalize_text(unit.text)
        if not text:
            raise ValueError("Stage units must contain text.")
        return StageUnit(
            source_track=_safe_scalar(unit.source_track, field_name="source_track"),
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
            speaker_key=_speaker_key(unit.speaker_key),
            speaker_label=_safe_scalar(
                unit.speaker_label, field_name="speaker_label", allow_empty=True
            ),
            timing_origin=_safe_scalar(unit.timing_origin, field_name="timing_origin"),
            speaker_origin=_safe_scalar(unit.speaker_origin, field_name="speaker_origin"),
            alignment_quality=_alignment_quality(unit.alignment_quality),
            provider_native_id=_safe_scalar(
                unit.provider_native_id, field_name="provider_native_id", allow_empty=True
            ),
        )

    @staticmethod
    def _unit_json(unit: StageUnit) -> dict[str, Any]:
        quality = (
            unit.alignment_quality.value
            if isinstance(unit.alignment_quality, AlignmentQuality)
            else str(unit.alignment_quality)
        )
        return {
            "sourceTrack": unit.source_track,
            "startMs": unit.start_ms,
            "endMs": unit.end_ms,
            "text": unit.text,
            "speakerKey": _speaker_key(unit.speaker_key),
            "speakerLabel": unit.speaker_label,
            "timingOrigin": unit.timing_origin,
            "speakerOrigin": unit.speaker_origin,
            "alignmentQuality": quality,
            "providerNativeId": unit.provider_native_id,
        }

    @classmethod
    def _stage_from_row(cls, row: sqlite3.Row) -> NormalizedStageResult:
        units = tuple(cls._validated_unit(unit) for unit in _json_array(row["units_json"]))
        return NormalizedStageResult(
            id=str(row["id"]),
            attempt_id=str(row["attempt_id"]),
            route_snapshot_id=str(row["route_snapshot_id"]),
            transcript_text=str(row["transcript_text"] or ""),
            units=units,
            evidence=_json_object(row["evidence_json"]),
            result_sha256=str(row["result_sha256"]),
            created_at=str(row["created_at"]),
        )

    @classmethod
    def _track_stage_from_row(cls, row: sqlite3.Row) -> TrackStageResult:
        units = tuple(cls._validated_unit(unit) for unit in _json_array(row["units_json"]))
        return TrackStageResult(
            id=str(row["id"]),
            attempt_id=str(row["attempt_id"]),
            route_snapshot_id=str(row["route_snapshot_id"]),
            source_track=str(row["source_track"]),
            transcript_text=str(row["transcript_text"] or ""),
            units=units,
            evidence=_json_object(row["evidence_json"]),
            result_sha256=str(row["result_sha256"]),
            created_at=str(row["created_at"]),
        )


    @classmethod
    def _track_derivation_from_row(cls, row: sqlite3.Row) -> TrackDerivationResult:
        units = tuple(cls._validated_unit(unit) for unit in _json_array(row["units_json"]))
        return TrackDerivationResult(
            id=str(row["id"]),
            attempt_id=str(row["attempt_id"]),
            route_snapshot_id=str(row["route_snapshot_id"]),
            parent_stage_result_id=str(row["parent_stage_result_id"]),
            source_track=str(row["source_track"]),
            derivation_kind=str(row["derivation_kind"]),
            units=units,
            evidence=_json_object(row["evidence_json"]),
            result_sha256=str(row["result_sha256"]),
            created_at=str(row["created_at"]),
        )

    @classmethod
    def _verify_track_stage_result(
        cls, stage: TrackStageResult, snapshot: RouteSnapshot
    ) -> None:
        payload = {
            "sourceTrack": stage.source_track,
            "transcriptText": stage.transcript_text,
            "units": [cls._unit_json(unit) for unit in stage.units],
            "evidence": stage.evidence,
            "routeSnapshotSha256": snapshot.snapshot_sha256,
            "provider": snapshot.provider,
            "model": snapshot.model,
            "parserId": snapshot.parser_id,
            "parserVersion": snapshot.parser_version,
        }
        if stage.route_snapshot_id != snapshot.id or _sha256_text(
            _canonical_json(payload)
        ) != stage.result_sha256:
            raise ArtifactConflict("Track stage-result checksum validation failed.")


    @classmethod
    def _verify_track_derivation(
        cls,
        derivation: TrackDerivationResult,
        snapshot: RouteSnapshot,
        parent: TrackStageResult,
    ) -> None:
        payload = {
            "sourceTrack": derivation.source_track,
            "derivationKind": derivation.derivation_kind,
            "parentStageResultId": parent.id,
            "parentStageResultSha256": parent.result_sha256,
            "units": [cls._unit_json(unit) for unit in derivation.units],
            "evidence": derivation.evidence,
            "routeSnapshotSha256": snapshot.snapshot_sha256,
        }
        if (
            derivation.route_snapshot_id != snapshot.id
            or derivation.parent_stage_result_id != parent.id
            or derivation.source_track != parent.source_track
            or _sha256_text(_canonical_json(payload)) != derivation.result_sha256
        ):
            raise ArtifactConflict("Track derivation checksum validation failed.")

    @classmethod
    def _verify_stage_result(
        cls, stage: NormalizedStageResult, snapshot: RouteSnapshot
    ) -> None:
        payload = {
            "transcriptText": stage.transcript_text,
            "units": [cls._unit_json(unit) for unit in stage.units],
            "evidence": stage.evidence,
            "routeSnapshotSha256": snapshot.snapshot_sha256,
            "provider": snapshot.provider,
            "model": snapshot.model,
            "responseShape": snapshot.response_shape,
            "parserId": snapshot.parser_id,
            "parserVersion": snapshot.parser_version,
        }
        if stage.route_snapshot_id != snapshot.id or _sha256_text(
            _canonical_json(payload)
        ) != stage.result_sha256:
            raise ArtifactConflict("Normalized stage-result checksum validation failed.")

    @staticmethod
    def _asset_from_row(row: sqlite3.Row) -> SourceAsset:
        return SourceAsset(
            id=str(row["id"]),
            transcript_id=str(row["transcript_id"]),
            source_track=str(row["source_track"]),
            asset_kind=str(row["asset_kind"]),
            purpose=str(row["purpose"]),
            state=SourceAssetState(str(row["state"])),
            state_version=int(row["state_version"]),
            relative_path=str(row["relative_path"] or ""),
            sha256=str(row["sha256"]),
            byte_count=int(row["byte_count"]),
            tombstone_reason=str(row["tombstone_reason"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            purged_at=str(row["purged_at"] or ""),
        )

    def create_attempt(
        self,
        *,
        transcript_id: str,
        workload: str,
        attempt_id: str | None = None,
        expected_head_generation: int | None = None,
    ) -> AttemptRecord:
        transcript_id = _safe_scalar(transcript_id, field_name="transcript_id")
        workload = _safe_scalar(workload, field_name="workload")
        attempt_id = _safe_scalar(attempt_id or uuid4().hex, field_name="attempt_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                record = self._attempt_from_row(existing)
                if record.transcript_id != transcript_id or record.workload != workload:
                    raise ArtifactConflict("Attempt id is already bound to different work.")
                if (
                    expected_head_generation is not None
                    and record.expected_head_generation != int(expected_head_generation)
                ):
                    raise ArtifactConflict(
                        "Attempt id is already bound to a different head generation."
                    )
                conn.commit()
                return record
            if conn.execute(
                "SELECT 1 FROM transcripts WHERE id = ?", (transcript_id,)
            ).fetchone() is None:
                raise ArtifactNotFound(f"Transcript {transcript_id!r} does not exist.")
            head = conn.execute(
                "SELECT generation FROM canonical_transcript_heads WHERE transcript_id = ?",
                (transcript_id,),
            ).fetchone()
            current_generation = int(head["generation"]) if head else 0
            frozen_generation = (
                current_generation
                if expected_head_generation is None
                else int(expected_head_generation)
            )
            if frozen_generation < 0:
                raise ValueError("expected_head_generation must be non-negative.")
            attempt_number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS n "
                    "FROM transcription_attempts WHERE transcript_id = ?",
                    (transcript_id,),
                ).fetchone()["n"]
            )
            now = _now_iso()
            conn.execute(
                """
                INSERT INTO transcription_attempts
                    (id, transcript_id, attempt_number, workload, state, state_version,
                     expected_head_generation, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    attempt_id,
                    transcript_id,
                    attempt_number,
                    workload,
                    AttemptState.QUEUED.value,
                    frozen_generation,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return self._attempt_from_row(row)
        except Exception:
            conn.rollback()
            raise

    def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        row = self._connect().execute(
            "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        return self._attempt_from_row(row) if row else None

    def require_attempt(self, attempt_id: str) -> AttemptRecord:
        record = self.get_attempt(attempt_id)
        if record is None:
            raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
        return record

    @staticmethod
    def _require_lease(row: sqlite3.Row, lease_owner: str, *, now: datetime) -> None:
        current_owner = str(row["lease_owner"] or "")
        expires = _parse_iso(str(row["lease_expires_at"] or ""))
        if current_owner:
            if current_owner != lease_owner:
                raise ArtifactConflict("Attempt lease is owned by another worker.")
            if expires is None or expires <= now:
                raise ArtifactConflict("Attempt lease has expired and must be reacquired.")
        elif lease_owner:
            raise ArtifactConflict("Attempt must be leased before a leased transition.")

    def acquire_attempt_lease(
        self,
        attempt_id: str,
        *,
        owner: str,
        expected_version: int,
        ttl_seconds: float = 60.0,
    ) -> AttemptRecord:
        owner = _safe_scalar(owner, field_name="lease owner")
        if ttl_seconds <= 0 or ttl_seconds > 86_400:
            raise ValueError("ttl_seconds must be greater than zero and at most one day.")
        conn = self._connect()
        now = _now()
        expires_at = (now + timedelta(seconds=float(ttl_seconds))).isoformat()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            current = self._attempt_from_row(row)
            if current.state in TERMINAL_ATTEMPT_STATES:
                raise ArtifactConflict("Terminal attempts cannot be leased.")
            if current.state_version != expected_version:
                raise ArtifactConflict("Attempt state version changed.")
            current_expiry = _parse_iso(current.lease_expires_at)
            if (
                current.lease_owner
                and current.lease_owner != owner
                and current_expiry is not None
                and current_expiry > now
            ):
                raise ArtifactConflict("Attempt lease is owned by another worker.")
            cursor = conn.execute(
                """
                UPDATE transcription_attempts
                SET lease_owner = ?, lease_expires_at = ?, state_version = state_version + 1,
                    updated_at = ?
                WHERE id = ? AND state_version = ?
                """,
                (owner, expires_at, now.isoformat(), attempt_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ArtifactConflict("Attempt lease CAS lost.")
            persisted = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return self._attempt_from_row(persisted)
        except Exception:
            conn.rollback()
            raise

    def renew_attempt_lease(
        self,
        attempt_id: str,
        *,
        owner: str,
        expected_version: int,
        ttl_seconds: float = 60.0,
    ) -> AttemptRecord:
        """Extend an owned lease without changing workflow state or its CAS version.

        Long provider uploads/polls can legitimately outlive the initial lease.  A
        heartbeat must not increment ``state_version`` because the worker may be
        holding that version for the next durable state transition.
        """
        owner = _safe_scalar(owner, field_name="lease owner")
        if ttl_seconds <= 0 or ttl_seconds > 86_400:
            raise ValueError("ttl_seconds must be greater than zero and at most one day.")
        conn = self._connect()
        now = _now()
        expires_at = (now + timedelta(seconds=float(ttl_seconds))).isoformat()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            current = self._attempt_from_row(row)
            if current.state in TERMINAL_ATTEMPT_STATES:
                raise ArtifactConflict("Terminal attempts cannot renew a lease.")
            if current.state_version != expected_version or current.lease_owner != owner:
                raise ArtifactConflict("Attempt lease renewal CAS lost.")
            current_expiry = _parse_iso(current.lease_expires_at)
            if current_expiry is None or current_expiry <= now:
                raise ArtifactConflict("Expired attempt leases must be reacquired.")
            cursor = conn.execute(
                """
                UPDATE transcription_attempts
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND state_version = ? AND lease_owner = ?
                """,
                (expires_at, now.isoformat(), attempt_id, expected_version, owner),
            )
            if cursor.rowcount != 1:
                raise ArtifactConflict("Attempt lease renewal CAS lost.")
            persisted = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return self._attempt_from_row(persisted)
        except Exception:
            conn.rollback()
            raise

    def release_attempt_lease(
        self,
        attempt_id: str,
        *,
        owner: str,
        expected_version: int,
    ) -> AttemptRecord:
        """Relinquish a live or expired lease without changing workflow state."""
        owner = _safe_scalar(owner, field_name="lease owner")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            current = self._attempt_from_row(row)
            if current.state in TERMINAL_ATTEMPT_STATES:
                raise ArtifactConflict("Terminal attempt leases are already released.")
            if current.state_version != expected_version or current.lease_owner != owner:
                raise ArtifactConflict("Attempt lease release CAS lost.")
            now = _now_iso()
            cursor = conn.execute(
                """
                UPDATE transcription_attempts
                SET lease_owner = '', lease_expires_at = '',
                    state_version = state_version + 1, updated_at = ?
                WHERE id = ? AND state_version = ? AND lease_owner = ?
                """,
                (now, attempt_id, expected_version, owner),
            )
            if cursor.rowcount != 1:
                raise ArtifactConflict("Attempt lease release CAS lost.")
            persisted = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return self._attempt_from_row(persisted)
        except Exception:
            conn.rollback()
            raise

    def claim_recovery_bundle(
        self,
        attempt_id: str,
        *,
        owner: str,
        expected_version: int,
        ttl_seconds: float = 60.0,
    ) -> RecoveryBundle:
        """CAS-claim persisted provider evidence; it never invokes a provider."""
        leased = self.acquire_attempt_lease(
            attempt_id,
            owner=owner,
            expected_version=expected_version,
            ttl_seconds=ttl_seconds,
        )
        bundle = self.get_recovery_bundle(attempt_id)
        if bundle.attempt.state_version != leased.state_version:
            raise ArtifactConflict("Recovery attempt changed after lease acquisition.")
        return bundle

    def transition_attempt(
        self,
        attempt_id: str,
        *,
        expected_state: AttemptState | str,
        expected_version: int,
        new_state: AttemptState | str,
        lease_owner: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> AttemptRecord:
        expected_state = _attempt_state(expected_state)
        new_state = _attempt_state(new_state)
        if new_state == AttemptState.PROVIDER_RESULT_READY:
            raise InvalidAttemptTransition(
                "Persist the normalized stage result to enter provider_result_ready."
            )
        if new_state in {AttemptState.COMPLETED, AttemptState.SUPERSEDED}:
            raise InvalidAttemptTransition("Canonical commit owns terminal winner selection.")
        if new_state not in ALLOWED_ATTEMPT_TRANSITIONS[expected_state]:
            raise InvalidAttemptTransition(
                f"Attempt cannot transition from {expected_state.value} to {new_state.value}."
            )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            current = self._attempt_from_row(row)
            if current.state != expected_state or current.state_version != expected_version:
                raise ArtifactConflict("Attempt state/version CAS lost.")
            self._require_lease(row, lease_owner, now=_now())
            if (
                expected_state == AttemptState.SOURCE_READY
                and new_state == AttemptState.TRANSCRIBING
            ):
                snapshot_row = conn.execute(
                    "SELECT * FROM transcription_route_snapshots WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if snapshot_row is None:
                    raise ArtifactConflict(
                        "A frozen route snapshot is required before transcribing."
                    )
                self._snapshot_from_row(snapshot_row)
            now = _now_iso()
            finished_at = now if new_state in TERMINAL_ATTEMPT_STATES else ""
            clear_lease = new_state in TERMINAL_ATTEMPT_STATES
            cursor = conn.execute(
                """
                UPDATE transcription_attempts
                SET state = ?, state_version = state_version + 1,
                    error_code = ?, error_message = ?, updated_at = ?, finished_at = ?,
                    lease_owner = CASE WHEN ? THEN '' ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? THEN '' ELSE lease_expires_at END
                WHERE id = ? AND state = ? AND state_version = ?
                """,
                (
                    new_state.value,
                    _safe_scalar(error_code, field_name="error_code", allow_empty=True),
                    _safe_scalar(error_message, field_name="error_message", allow_empty=True),
                    now,
                    finished_at,
                    int(clear_lease),
                    int(clear_lease),
                    attempt_id,
                    expected_state.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ArtifactConflict("Attempt state/version CAS lost.")
            persisted = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return self._attempt_from_row(persisted)
        except Exception:
            conn.rollback()
            raise

    def persist_route_snapshot(
        self,
        attempt_id: str,
        draft: RouteSnapshotDraft,
        *,
        snapshot_id: str | None = None,
    ) -> RouteSnapshot:
        request_options = _validate_safe_persisted_json(
            dict(draft.request_options), field_name="request_options"
        )
        worker_manifest = _validate_safe_persisted_json(
            dict(draft.local_worker_manifest), field_name="local_worker_manifest"
        )
        fields = {
            "workload": _safe_scalar(draft.workload, field_name="workload"),
            "sourceTrack": _safe_scalar(draft.source_track, field_name="source_track"),
            "provider": _safe_scalar(draft.provider, field_name="provider"),
            "model": _safe_scalar(draft.model, field_name="model"),
            "transport": _safe_scalar(draft.transport, field_name="transport"),
            "language": _safe_scalar(draft.language, field_name="language"),
            "responseShape": _safe_scalar(draft.response_shape, field_name="response_shape"),
            "timestampMode": _safe_scalar(draft.timestamp_mode, field_name="timestamp_mode"),
            "diarizationMode": _safe_scalar(
                draft.diarization_mode, field_name="diarization_mode"
            ),
            "parserId": _safe_scalar(draft.parser_id, field_name="parser_id"),
            "parserVersion": _safe_scalar(draft.parser_version, field_name="parser_version"),
            "requestOptions": request_options,
            "localWorkerManifest": worker_manifest,
        }
        _validate_safe_persisted_json(fields, field_name="route_snapshot")
        digest = _sha256_text(_canonical_json(fields))
        snapshot_id = _safe_scalar(snapshot_id or uuid4().hex, field_name="snapshot_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt_row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt_row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            attempt = self._attempt_from_row(attempt_row)
            if attempt.workload != fields["workload"]:
                raise ArtifactConflict("Route workload differs from the frozen attempt workload.")
            existing = conn.execute(
                "SELECT * FROM transcription_route_snapshots WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                snapshot = self._snapshot_from_row(existing)
                if snapshot.snapshot_sha256 != digest:
                    raise ArtifactConflict("Route snapshot is immutable.")
                conn.commit()
                return snapshot
            if attempt.state not in {
                AttemptState.QUEUED,
                AttemptState.RESOLVING_SOURCE,
                AttemptState.SOURCE_READY,
            }:
                raise ArtifactConflict(
                    "Route snapshot must be frozen before the attempt starts transcribing."
                )
            now = _now_iso()
            conn.execute(
                """
                INSERT INTO transcription_route_snapshots
                    (id, attempt_id, transcript_id, workload, source_track, provider, model,
                     transport, language, response_shape, timestamp_mode, diarization_mode,
                     parser_id, parser_version, request_options_json,
                     local_worker_manifest_json, snapshot_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    attempt_id,
                    attempt.transcript_id,
                    fields["workload"],
                    fields["sourceTrack"],
                    fields["provider"],
                    fields["model"],
                    fields["transport"],
                    fields["language"],
                    fields["responseShape"],
                    fields["timestampMode"],
                    fields["diarizationMode"],
                    fields["parserId"],
                    fields["parserVersion"],
                    _canonical_json(request_options),
                    _canonical_json(worker_manifest),
                    digest,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM transcription_route_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            conn.commit()
            return self._snapshot_from_row(row)
        except Exception:
            conn.rollback()
            raise

    def get_route_snapshot(self, attempt_id: str) -> RouteSnapshot | None:
        row = self._connect().execute(
            "SELECT * FROM transcription_route_snapshots WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return self._snapshot_from_row(row) if row else None

    def persist_stage_result(
        self,
        attempt_id: str,
        *,
        expected_version: int,
        transcript_text: str,
        units: Sequence[StageUnit | Mapping[str, Any]],
        evidence: Mapping[str, Any] | None = None,
        lease_owner: str = "",
        result_id: str | None = None,
    ) -> tuple[NormalizedStageResult, AttemptRecord]:
        validated_units = tuple(self._validated_unit(unit) for unit in units)
        safe_evidence = _validate_safe_persisted_json(
            dict(evidence or {}), field_name="stage_result.evidence"
        )
        normalized_text = _normalize_text(transcript_text)
        unit_payload = [self._unit_json(unit) for unit in validated_units]
        payload = {
            "transcriptText": normalized_text,
            "units": unit_payload,
            "evidence": safe_evidence,
        }
        result_id = _safe_scalar(result_id or uuid4().hex, field_name="result_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt_row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt_row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            attempt = self._attempt_from_row(attempt_row)
            snapshot_row = conn.execute(
                "SELECT * FROM transcription_route_snapshots WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if snapshot_row is None:
                raise ArtifactConflict("A frozen route snapshot is required before provider work.")
            snapshot = self._snapshot_from_row(snapshot_row)
            bound_payload = {
                **payload,
                "routeSnapshotSha256": snapshot.snapshot_sha256,
                "provider": snapshot.provider,
                "model": snapshot.model,
                "responseShape": snapshot.response_shape,
                "parserId": snapshot.parser_id,
                "parserVersion": snapshot.parser_version,
            }
            digest = _sha256_text(_canonical_json(bound_payload))
            existing = conn.execute(
                "SELECT * FROM transcription_stage_results WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                stage = self._stage_from_row(existing)
                if stage.result_sha256 != digest:
                    raise ArtifactConflict("Normalized stage result is immutable.")
                if attempt.state not in {
                    AttemptState.PROVIDER_RESULT_READY,
                    AttemptState.DIARIZING,
                    AttemptState.CANONICALIZING,
                    AttemptState.COMMITTING,
                    AttemptState.COMPLETED,
                    AttemptState.SUPERSEDED,
                }:
                    raise ArtifactConflict("Stage result exists in an invalid attempt state.")
                conn.commit()
                return stage, attempt
            if attempt.state != AttemptState.TRANSCRIBING or attempt.state_version != expected_version:
                raise ArtifactConflict("Attempt is not the expected transcribing generation.")
            self._require_lease(attempt_row, lease_owner, now=_now())
            now = _now_iso()
            conn.execute(
                """
                INSERT INTO transcription_stage_results
                    (id, attempt_id, route_snapshot_id, provider, model, response_shape,
                     parser_id, parser_version, transcript_text, units_json, evidence_json,
                     result_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    attempt_id,
                    snapshot.id,
                    snapshot.provider,
                    snapshot.model,
                    snapshot.response_shape,
                    snapshot.parser_id,
                    snapshot.parser_version,
                    normalized_text,
                    _canonical_json(unit_payload),
                    _canonical_json(safe_evidence),
                    digest,
                    now,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE transcription_attempts
                SET state = ?, state_version = state_version + 1, updated_at = ?
                WHERE id = ? AND state = ? AND state_version = ?
                """,
                (
                    AttemptState.PROVIDER_RESULT_READY.value,
                    now,
                    attempt_id,
                    AttemptState.TRANSCRIBING.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ArtifactConflict("Attempt state/version CAS lost while saving stage result.")
            stage_row = conn.execute(
                "SELECT * FROM transcription_stage_results WHERE id = ?", (result_id,)
            ).fetchone()
            persisted_attempt = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            self._fault("after_stage_result_insert", conn)
            conn.commit()
            return self._stage_from_row(stage_row), self._attempt_from_row(persisted_attempt)
        except Exception:
            conn.rollback()
            raise

    def get_stage_result(self, attempt_id: str) -> NormalizedStageResult | None:
        row = self._connect().execute(
            "SELECT * FROM transcription_stage_results WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return self._stage_from_row(row) if row else None

    def persist_track_stage_result(
        self,
        attempt_id: str,
        *,
        source_track: str,
        expected_version: int,
        transcript_text: str,
        units: Sequence[StageUnit | Mapping[str, Any]],
        evidence: Mapping[str, Any] | None = None,
        lease_owner: str = "",
        result_id: str | None = None,
    ) -> TrackStageResult:
        source_track = _safe_scalar(source_track, field_name="source_track")
        validated_units = tuple(self._validated_unit(unit) for unit in units)
        if not validated_units or any(
            unit.source_track != source_track for unit in validated_units
        ):
            raise ValueError("Track stage units must be non-empty and match source_track.")
        safe_evidence = _validate_safe_persisted_json(
            dict(evidence or {}), field_name="track_stage_result.evidence"
        )
        normalized_text = _normalize_text(transcript_text)
        if not normalized_text:
            raise ValueError("Track stage results require transcript text.")
        result_id = _safe_scalar(result_id or uuid4().hex, field_name="result_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt_row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt_row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            attempt = self._attempt_from_row(attempt_row)
            snapshot_row = conn.execute(
                "SELECT * FROM transcription_route_snapshots WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if snapshot_row is None:
                raise ArtifactConflict("A frozen route snapshot is required before track work.")
            snapshot = self._snapshot_from_row(snapshot_row)
            payload = {
                "sourceTrack": source_track,
                "transcriptText": normalized_text,
                "units": [self._unit_json(unit) for unit in validated_units],
                "evidence": safe_evidence,
                "routeSnapshotSha256": snapshot.snapshot_sha256,
                "provider": snapshot.provider,
                "model": snapshot.model,
                "parserId": snapshot.parser_id,
                "parserVersion": snapshot.parser_version,
            }
            digest = _sha256_text(_canonical_json(payload))
            existing = conn.execute(
                """
                SELECT * FROM transcription_track_stage_results
                WHERE attempt_id = ? AND source_track = ?
                """,
                (attempt_id, source_track),
            ).fetchone()
            if existing is not None:
                stage = self._track_stage_from_row(existing)
                if stage.result_sha256 != digest:
                    raise ArtifactConflict("Track stage result is immutable.")
                self._verify_track_stage_result(stage, snapshot)
                conn.commit()
                return stage
            if attempt.state != AttemptState.TRANSCRIBING or attempt.state_version != expected_version:
                raise ArtifactConflict("Attempt is not the expected transcribing generation.")
            self._require_lease(attempt_row, lease_owner, now=_now())
            conn.execute(
                """
                INSERT INTO transcription_track_stage_results
                    (id, attempt_id, route_snapshot_id, source_track, transcript_text,
                     units_json, evidence_json, result_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    attempt_id,
                    snapshot.id,
                    source_track,
                    normalized_text,
                    _canonical_json([self._unit_json(unit) for unit in validated_units]),
                    _canonical_json(safe_evidence),
                    digest,
                    _now_iso(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM transcription_track_stage_results WHERE id = ?",
                (result_id,),
            ).fetchone()
            conn.commit()
            return self._track_stage_from_row(row)
        except Exception:
            conn.rollback()
            raise

    def list_track_stage_results(self, attempt_id: str) -> tuple[TrackStageResult, ...]:
        snapshot = self.get_route_snapshot(attempt_id)
        if snapshot is None:
            return ()
        rows = self._connect().execute(
            """
            SELECT * FROM transcription_track_stage_results
            WHERE attempt_id = ? ORDER BY source_track ASC
            """,
            (attempt_id,),
        ).fetchall()
        results = tuple(self._track_stage_from_row(row) for row in rows)
        for result in results:
            self._verify_track_stage_result(result, snapshot)
        return results


    def persist_track_derivation(
        self,
        attempt_id: str,
        *,
        parent_stage_result_id: str,
        source_track: str,
        derivation_kind: str,
        expected_version: int,
        units: Sequence[StageUnit | Mapping[str, Any]],
        evidence: Mapping[str, Any] | None = None,
        lease_owner: str = "",
        result_id: str | None = None,
    ) -> TrackDerivationResult:
        parent_stage_result_id = _safe_scalar(
            parent_stage_result_id, field_name="parent_stage_result_id"
        )
        source_track = _safe_scalar(source_track, field_name="source_track")
        derivation_kind = _safe_scalar(derivation_kind, field_name="derivation_kind")
        validated_units = tuple(self._validated_unit(unit) for unit in units)
        if not validated_units or any(
            unit.source_track != source_track for unit in validated_units
        ):
            raise ValueError("Track derivation units must be non-empty and match source_track.")
        safe_evidence = _validate_safe_persisted_json(
            dict(evidence or {}), field_name="track_derivation.evidence"
        )
        result_id = _safe_scalar(result_id or uuid4().hex, field_name="result_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt_row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt_row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            attempt = self._attempt_from_row(attempt_row)
            snapshot_row = conn.execute(
                "SELECT * FROM transcription_route_snapshots WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if snapshot_row is None:
                raise ArtifactConflict("A frozen route snapshot is required before derived work.")
            snapshot = self._snapshot_from_row(snapshot_row)
            parent_row = conn.execute(
                "SELECT * FROM transcription_track_stage_results WHERE id = ? AND attempt_id = ?",
                (parent_stage_result_id, attempt_id),
            ).fetchone()
            if parent_row is None:
                raise ArtifactConflict("Track derivation requires its immutable parent stage result.")
            parent = self._track_stage_from_row(parent_row)
            self._verify_track_stage_result(parent, snapshot)
            if parent.source_track != source_track:
                raise ArtifactConflict("Track derivation source does not match its parent.")
            payload = {
                "sourceTrack": source_track,
                "derivationKind": derivation_kind,
                "parentStageResultId": parent.id,
                "parentStageResultSha256": parent.result_sha256,
                "units": [self._unit_json(unit) for unit in validated_units],
                "evidence": safe_evidence,
                "routeSnapshotSha256": snapshot.snapshot_sha256,
            }
            digest = _sha256_text(_canonical_json(payload))
            existing = conn.execute(
                """
                SELECT * FROM transcription_track_derivations
                WHERE attempt_id = ? AND source_track = ? AND derivation_kind = ?
                """,
                (attempt_id, source_track, derivation_kind),
            ).fetchone()
            if existing is not None:
                derivation = self._track_derivation_from_row(existing)
                if derivation.result_sha256 != digest:
                    raise ArtifactConflict("Track derivation is immutable.")
                self._verify_track_derivation(derivation, snapshot, parent)
                conn.commit()
                return derivation
            if attempt.state != AttemptState.TRANSCRIBING or attempt.state_version != expected_version:
                raise ArtifactConflict("Attempt is not the expected transcribing generation.")
            self._require_lease(attempt_row, lease_owner, now=_now())
            conn.execute(
                """
                INSERT INTO transcription_track_derivations
                    (id, attempt_id, route_snapshot_id, parent_stage_result_id,
                     source_track, derivation_kind, units_json, evidence_json,
                     result_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result_id,
                    attempt_id,
                    snapshot.id,
                    parent.id,
                    source_track,
                    derivation_kind,
                    _canonical_json([self._unit_json(unit) for unit in validated_units]),
                    _canonical_json(safe_evidence),
                    digest,
                    _now_iso(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM transcription_track_derivations WHERE id = ?", (result_id,)
            ).fetchone()
            conn.commit()
            return self._track_derivation_from_row(row)
        except Exception:
            conn.rollback()
            raise

    def list_track_derivations(self, attempt_id: str) -> tuple[TrackDerivationResult, ...]:
        snapshot = self.get_route_snapshot(attempt_id)
        if snapshot is None:
            return ()
        stage_results = {
            item.id: item for item in self.list_track_stage_results(attempt_id)
        }
        rows = self._connect().execute(
            """
            SELECT * FROM transcription_track_derivations
            WHERE attempt_id = ? ORDER BY source_track ASC, derivation_kind ASC
            """,
            (attempt_id,),
        ).fetchall()
        results = tuple(self._track_derivation_from_row(row) for row in rows)
        for result in results:
            parent = stage_results.get(result.parent_stage_result_id)
            if parent is None:
                raise ArtifactConflict("Track derivation parent is missing.")
            self._verify_track_derivation(result, snapshot, parent)
        return results

    def latest_resumable_track_attempt(
        self,
        transcript_id: str,
        *,
        now: datetime | None = None,
    ) -> TrackRecoveryBundle | None:
        transcript_id = _safe_scalar(transcript_id, field_name="transcript_id")
        now = (now or _now()).astimezone(timezone.utc)
        rows = self._connect().execute(
            """
            SELECT a.id
            FROM transcription_attempts a
            WHERE a.transcript_id = ? AND a.state = ?
              AND (a.lease_owner = '' OR a.lease_expires_at = '' OR a.lease_expires_at <= ?)
              AND EXISTS (
                  SELECT 1 FROM transcription_track_stage_results t
                  WHERE t.attempt_id = a.id
              )
            ORDER BY a.updated_at DESC, a.id DESC
            """,
            (transcript_id, AttemptState.TRANSCRIBING.value, now.isoformat()),
        ).fetchall()
        for row in rows:
            attempt = self.require_attempt(str(row["id"]))
            snapshot = self.get_route_snapshot(attempt.id)
            results = self.list_track_stage_results(attempt.id)
            if snapshot is not None and results:
                return TrackRecoveryBundle(
                    attempt,
                    snapshot,
                    results,
                    self.list_track_derivations(attempt.id),
                )
        return None

    def get_recovery_bundle(self, attempt_id: str) -> RecoveryBundle:
        conn = self._connect()
        attempt_row = conn.execute(
            "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if attempt_row is None:
            raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
        attempt = self._attempt_from_row(attempt_row)
        if attempt.state not in {
            AttemptState.PROVIDER_RESULT_READY,
            AttemptState.DIARIZING,
            AttemptState.CANONICALIZING,
            AttemptState.COMMITTING,
        }:
            raise ArtifactConflict("Attempt has no recoverable provider result.")
        snapshot = self.get_route_snapshot(attempt_id)
        stage = self.get_stage_result(attempt_id)
        if snapshot is None or stage is None:
            raise ArtifactConflict("Recoverable attempt is missing immutable provider evidence.")
        self._verify_stage_result(stage, snapshot)
        return RecoveryBundle(attempt=attempt, route_snapshot=snapshot, stage_result=stage)

    def list_recoverable_provider_results(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[RecoveryBundle, ...]:
        now = (now or _now()).astimezone(timezone.utc)
        limit = max(0, min(int(limit), 1000))
        rows = self._connect().execute(
            """
            SELECT id FROM transcription_attempts
            WHERE state IN (?, ?, ?, ?)
              AND (lease_owner = '' OR lease_expires_at = '' OR lease_expires_at <= ?)
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (
                AttemptState.PROVIDER_RESULT_READY.value,
                AttemptState.DIARIZING.value,
                AttemptState.CANONICALIZING.value,
                AttemptState.COMMITTING.value,
                now.isoformat(),
                limit,
            ),
        ).fetchall()
        bundles: list[RecoveryBundle] = []
        for row in rows:
            bundle = self.get_recovery_bundle(str(row["id"]))
            expires = _parse_iso(bundle.attempt.lease_expires_at)
            if not bundle.attempt.lease_owner or expires is None or expires <= now:
                bundles.append(bundle)
        return tuple(bundles)

    def latest_recoverable_for_transcript(
        self,
        transcript_id: str,
        *,
        now: datetime | None = None,
    ) -> RecoveryBundle | None:
        """Return the newest unleased recoverable result for one transcript."""
        transcript_id = _safe_scalar(transcript_id, field_name="transcript_id")
        now = (now or _now()).astimezone(timezone.utc)
        rows = self._connect().execute(
            """
            SELECT id FROM transcription_attempts
            WHERE transcript_id = ? AND state IN (?, ?, ?, ?)
              AND (lease_owner = '' OR lease_expires_at = '' OR lease_expires_at <= ?)
            ORDER BY updated_at DESC, id DESC
            """,
            (
                transcript_id,
                AttemptState.PROVIDER_RESULT_READY.value,
                AttemptState.DIARIZING.value,
                AttemptState.CANONICALIZING.value,
                AttemptState.COMMITTING.value,
                now.isoformat(),
            ),
        ).fetchall()
        for row in rows:
            bundle = self.get_recovery_bundle(str(row["id"]))
            expiry = _parse_iso(bundle.attempt.lease_expires_at)
            if not bundle.attempt.lease_owner or expiry is None or expiry <= now:
                return bundle
        return None

    @staticmethod
    def _coerce_segment(
        value: CanonicalSegmentDraft | StageUnit | Mapping[str, Any]
    ) -> CanonicalSegmentDraft:
        if isinstance(value, CanonicalSegmentDraft):
            return value
        if isinstance(value, StageUnit):
            return CanonicalSegmentDraft(**value.__dict__)
        return CanonicalSegmentDraft(
            source_track=value.get("source_track", value.get("sourceTrack", "")),
            start_ms=value.get("start_ms", value.get("startMs", 0)),
            end_ms=value.get("end_ms", value.get("endMs", 0)),
            text=value.get("text", ""),
            speaker_key=value.get("speaker_key", value.get("speakerKey")),
            speaker_label=value.get("speaker_label", value.get("speakerLabel", "")),
            timing_origin=value.get("timing_origin", value.get("timingOrigin", "provider")),
            speaker_origin=value.get("speaker_origin", value.get("speakerOrigin", "none")),
            alignment_quality=value.get(
                "alignment_quality", value.get("alignmentQuality", "provider_segment")
            ),
            provider_native_id=value.get(
                "provider_native_id", value.get("providerNativeId", "")
            ),
        )

    @classmethod
    def build_stable_segments(
        cls,
        *,
        transcript_id: str,
        artifact_id: str,
        segments: Sequence[CanonicalSegmentDraft | StageUnit | Mapping[str, Any]],
    ) -> tuple[CanonicalSegment, ...]:
        transcript_id = _safe_scalar(transcript_id, field_name="transcript_id")
        validated: list[CanonicalSegmentDraft] = []
        for raw in segments:
            value = cls._coerce_segment(raw)
            unit = cls._validated_unit(StageUnit(**value.__dict__))
            validated.append(CanonicalSegmentDraft(**unit.__dict__))
        # Canonical global order. Original position is the final tie-breaker; fully
        # identical rows are indistinguishable, so the resulting id set remains stable.
        ordered = sorted(
            enumerate(validated),
            key=lambda pair: (
                pair[1].start_ms,
                pair[1].end_ms,
                pair[1].source_track,
                _speaker_key(pair[1].speaker_key),
                _normalize_text(pair[1].text),
                pair[1].provider_native_id,
                pair[0],
            ),
        )
        counts: dict[str, int] = {}
        result: list[CanonicalSegment] = []
        for order_index, (_, segment) in enumerate(ordered):
            speaker = _speaker_key(segment.speaker_key)
            normalized_text = _normalize_text(segment.text)
            if segment.provider_native_id:
                identity = _canonical_json(
                    {
                        "transcriptId": transcript_id,
                        "sourceTrack": segment.source_track,
                        "providerNativeId": segment.provider_native_id,
                    }
                )
            else:
                identity = _canonical_json(
                    {
                        "transcriptId": transcript_id,
                        "sourceTrack": segment.source_track,
                        "startMs": segment.start_ms,
                        "endMs": segment.end_ms,
                        "speakerKey": speaker,
                        "text": normalized_text,
                    }
                )
            occurrence = counts.get(identity, 0)
            counts[identity] = occurrence + 1
            stable_material = identity if occurrence == 0 else f"{identity}\n{occurrence}"
            segment_id = f"seg_{_sha256_text(stable_material)[:32]}"
            quality = _alignment_quality(segment.alignment_quality)
            result.append(
                CanonicalSegment(
                    artifact_id=artifact_id,
                    segment_id=segment_id,
                    order_index=order_index,
                    source_track=segment.source_track,
                    start_ms=int(segment.start_ms),
                    end_ms=int(segment.end_ms),
                    duration_ms=int(segment.end_ms) - int(segment.start_ms),
                    text=normalized_text,
                    speaker_key=speaker,
                    speaker_label=segment.speaker_label,
                    timing_origin=segment.timing_origin,
                    speaker_origin=segment.speaker_origin,
                    alignment_quality=quality,
                    provider_native_id=segment.provider_native_id,
                    occurrence_index=occurrence,
                )
            )
        return tuple(result)

    @staticmethod
    def render_legacy_content(segments: Sequence[CanonicalSegment]) -> str:
        lines: list[str] = []
        for segment in segments:
            seconds = segment.start_ms // 1000
            timestamp = f"{seconds // 60}:{seconds % 60:02d}"
            speaker = segment.speaker_label.strip()
            if not speaker and segment.speaker_key != "":
                speaker = f"Speaker {segment.speaker_key}"
            prefix = f"[{timestamp}]"
            lines.append(f"{prefix} {speaker + ': ' if speaker else ''}{segment.text}")
        return "\n".join(lines)

    @staticmethod
    def _head_from_row(row: sqlite3.Row | None) -> CanonicalHead | None:
        if row is None:
            return None
        return CanonicalHead(
            transcript_id=str(row["transcript_id"]),
            artifact_id=str(row["artifact_id"]),
            generation=int(row["generation"]),
            updated_at=str(row["updated_at"]),
        )

    @classmethod
    def _segment_from_row(cls, item: sqlite3.Row) -> CanonicalSegment:
        return CanonicalSegment(
            artifact_id=str(item["artifact_id"]),
            segment_id=str(item["segment_id"]),
            order_index=int(item["order_index"]),
            source_track=str(item["source_track"]),
            start_ms=int(item["start_ms"]),
            end_ms=int(item["end_ms"]),
            duration_ms=int(item["duration_ms"]),
            text=str(item["text"]),
            speaker_key=str(item["speaker_key"] or ""),
            speaker_label=str(item["speaker_label"] or ""),
            timing_origin=str(item["timing_origin"]),
            speaker_origin=str(item["speaker_origin"]),
            alignment_quality=AlignmentQuality(str(item["alignment_quality"])),
            provider_native_id=str(item["provider_native_id"] or ""),
            occurrence_index=int(item["occurrence_index"]),
        )

    @classmethod
    def _artifact_from_row(
        cls, conn: sqlite3.Connection, row: sqlite3.Row, *, include_segments: bool = True
    ) -> CanonicalArtifact:
        segment_rows = (
            conn.execute(
                "SELECT * FROM canonical_transcript_segments WHERE artifact_id = ? "
                "ORDER BY order_index",
                (row["id"],),
            ).fetchall()
            if include_segments
            else []
        )
        segments = tuple(cls._segment_from_row(item) for item in segment_rows)
        return CanonicalArtifact(
            id=str(row["id"]),
            transcript_id=str(row["transcript_id"]),
            attempt_id=str(row["attempt_id"]),
            generation=int(row["generation"]),
            route_snapshot_id=str(row["route_snapshot_id"]),
            stage_result_id=str(row["stage_result_id"]),
            schema_version=int(row["schema_version"]),
            artifact_sha256=str(row["artifact_sha256"]),
            created_at=str(row["created_at"]),
            segments=segments,
        )

    def get_head(self, transcript_id: str) -> CanonicalHead | None:
        row = self._connect().execute(
            "SELECT * FROM canonical_transcript_heads WHERE transcript_id = ?", (transcript_id,)
        ).fetchone()
        return self._head_from_row(row)

    def get_artifact(self, artifact_id: str) -> CanonicalArtifact | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM canonical_transcript_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return self._artifact_from_row(conn, row) if row else None

    def search_canonical_segments(
        self,
        transcript_id: str,
        query: str,
        *,
        artifact_id: str = "",
        limit: int = 50,
    ) -> tuple[CanonicalSegment, ...]:
        """Search one transcript artifact while preserving stable segment ids."""
        transcript_id = _safe_scalar(transcript_id, field_name="transcript_id")
        expression = _fts_query(query)
        if not expression or limit <= 0:
            return ()
        limit = min(int(limit), 200)
        conn = self._connect()
        selected_artifact_id = str(artifact_id or "").strip()
        if selected_artifact_id:
            belongs = conn.execute(
                "SELECT 1 FROM canonical_transcript_artifacts "
                "WHERE id = ? AND transcript_id = ?",
                (selected_artifact_id, transcript_id),
            ).fetchone()
            if belongs is None:
                return ()
        else:
            head = self.get_head(transcript_id)
            if head is None:
                return ()
            selected_artifact_id = head.artifact_id
        rows = conn.execute(
            """
            SELECT s.*
            FROM canonical_transcript_segments_fts f
            JOIN canonical_transcript_segments s
              ON s.artifact_id = f.artifact_id AND s.segment_id = f.segment_id
            WHERE canonical_transcript_segments_fts MATCH ?
              AND s.artifact_id = ?
            ORDER BY bm25(canonical_transcript_segments_fts), s.order_index
            LIMIT ?
            """,
            (expression, selected_artifact_id, limit),
        ).fetchall()
        return tuple(self._segment_from_row(row) for row in rows)

    def commit_canonical_artifact(
        self,
        attempt_id: str,
        *,
        expected_attempt_version: int,
        expected_head_generation: int,
        segments: Sequence[CanonicalSegmentDraft | StageUnit | Mapping[str, Any]],
        inputs: Iterable[ArtifactInputDraft] = (),
        lease_owner: str = "",
        artifact_id: str | None = None,
    ) -> CanonicalCommitResult:
        artifact_id = _safe_scalar(artifact_id or uuid4().hex, field_name="artifact_id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt_row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if attempt_row is None:
                raise ArtifactNotFound(f"Attempt {attempt_id!r} does not exist.")
            attempt = self._attempt_from_row(attempt_row)
            existing_artifact_row = conn.execute(
                "SELECT * FROM canonical_transcript_artifacts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing_artifact_row is not None:
                artifact = self._artifact_from_row(conn, existing_artifact_row)
                head = self._head_from_row(
                    conn.execute(
                        "SELECT * FROM canonical_transcript_heads WHERE transcript_id = ?",
                        (attempt.transcript_id,),
                    ).fetchone()
                )
                if attempt.state != AttemptState.COMPLETED or head is None or head.artifact_id != artifact.id:
                    raise ArtifactConflict("Attempt artifact exists without a matching completed head.")
                conn.commit()
                return CanonicalCommitResult(
                    artifact=artifact,
                    head=head,
                    attempt=attempt,
                    committed=True,
                    superseded=False,
                )
            if attempt.state == AttemptState.SUPERSEDED:
                head = self._head_from_row(
                    conn.execute(
                        "SELECT * FROM canonical_transcript_heads WHERE transcript_id = ?",
                        (attempt.transcript_id,),
                    ).fetchone()
                )
                conn.commit()
                return CanonicalCommitResult(None, head, attempt, False, True)
            if attempt.state != AttemptState.COMMITTING or attempt.state_version != expected_attempt_version:
                raise ArtifactConflict("Attempt is not the expected committing generation.")
            self._require_lease(attempt_row, lease_owner, now=_now())
            if attempt.expected_head_generation != int(expected_head_generation):
                raise ArtifactConflict("Commit generation differs from the frozen attempt route.")
            snapshot_row = conn.execute(
                "SELECT * FROM transcription_route_snapshots WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            stage_row = conn.execute(
                "SELECT * FROM transcription_stage_results WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if snapshot_row is None or stage_row is None:
                raise ArtifactConflict("Canonical commit requires frozen route and stage result.")
            snapshot = self._snapshot_from_row(snapshot_row)
            stage = self._stage_from_row(stage_row)
            self._verify_stage_result(stage, snapshot)
            head_row = conn.execute(
                "SELECT * FROM canonical_transcript_heads WHERE transcript_id = ?",
                (attempt.transcript_id,),
            ).fetchone()
            current_head = self._head_from_row(head_row)
            current_generation = current_head.generation if current_head else 0
            now = _now_iso()
            if current_generation != int(expected_head_generation):
                cursor = conn.execute(
                    """
                    UPDATE transcription_attempts
                    SET state = ?, state_version = state_version + 1, lease_owner = '',
                        lease_expires_at = '', updated_at = ?, finished_at = ?
                    WHERE id = ? AND state = ? AND state_version = ?
                    """,
                    (
                        AttemptState.SUPERSEDED.value,
                        now,
                        now,
                        attempt_id,
                        AttemptState.COMMITTING.value,
                        expected_attempt_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ArtifactConflict("Stale commit lost its attempt CAS.")
                persisted = conn.execute(
                    "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
                ).fetchone()
                conn.commit()
                return CanonicalCommitResult(
                    artifact=None,
                    head=current_head,
                    attempt=self._attempt_from_row(persisted),
                    committed=False,
                    superseded=True,
                )

            stable_segments = self.build_stable_segments(
                transcript_id=attempt.transcript_id,
                artifact_id=artifact_id,
                segments=segments,
            )
            if not stable_segments:
                raise ValueError("Canonical artifacts require at least one segment.")
            automatic_inputs = [
                ArtifactInputDraft("route_snapshot", snapshot.id, snapshot.snapshot_sha256),
                ArtifactInputDraft("stage_result", stage.id, stage.result_sha256),
            ]
            normalized_inputs: list[ArtifactInputDraft] = []
            seen_inputs: set[tuple[str, str]] = set()
            for raw_input in [*automatic_inputs, *list(inputs)]:
                kind = _safe_scalar(raw_input.input_kind, field_name="input_kind")
                input_id = _safe_scalar(raw_input.input_id, field_name="input_id")
                _validate_safe_persisted_json(
                    {"inputId": input_id}, field_name=f"artifact_input.{kind}"
                )
                digest = _require_sha256(raw_input.input_sha256, field_name="input_sha256")
                metadata = _validate_safe_persisted_json(
                    dict(raw_input.metadata), field_name=f"artifact_input.{kind}.metadata"
                )
                key = (kind, input_id)
                if key in seen_inputs:
                    raise ValueError("Canonical artifact inputs must be unique.")
                seen_inputs.add(key)
                normalized_inputs.append(ArtifactInputDraft(kind, input_id, digest, metadata))
            generation = int(expected_head_generation) + 1
            segment_payload = [
                {
                    "segmentId": item.segment_id,
                    "orderIndex": item.order_index,
                    "sourceTrack": item.source_track,
                    "startMs": item.start_ms,
                    "endMs": item.end_ms,
                    "text": item.text,
                    "speakerKey": item.speaker_key,
                    "speakerLabel": item.speaker_label,
                    "timingOrigin": item.timing_origin,
                    "speakerOrigin": item.speaker_origin,
                    "alignmentQuality": item.alignment_quality.value,
                    "providerNativeId": item.provider_native_id,
                }
                for item in stable_segments
            ]
            input_payload = [
                {
                    "kind": item.input_kind,
                    "id": item.input_id,
                    "sha256": item.input_sha256,
                    "metadata": dict(item.metadata),
                }
                for item in normalized_inputs
            ]
            artifact_digest = _sha256_text(
                _canonical_json(
                    {
                        "transcriptId": attempt.transcript_id,
                        "generation": generation,
                        "routeSnapshotSha256": snapshot.snapshot_sha256,
                        "stageResultSha256": stage.result_sha256,
                        "segments": segment_payload,
                        "inputs": input_payload,
                    }
                )
            )
            if conn.execute(
                "SELECT 1 FROM transcripts WHERE id = ?", (attempt.transcript_id,)
            ).fetchone() is None:
                raise ArtifactNotFound("Compatibility transcript disappeared before commit.")
            conn.execute(
                """
                INSERT INTO canonical_transcript_artifacts
                    (id, transcript_id, attempt_id, generation, route_snapshot_id,
                     stage_result_id, schema_version, artifact_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    artifact_id,
                    attempt.transcript_id,
                    attempt_id,
                    generation,
                    snapshot.id,
                    stage.id,
                    artifact_digest,
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO canonical_transcript_segments
                    (artifact_id, segment_id, order_index, source_track, start_ms, end_ms,
                     duration_ms, text, speaker_key, speaker_label, timing_origin,
                     speaker_origin, alignment_quality, provider_native_id, occurrence_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.artifact_id,
                        item.segment_id,
                        item.order_index,
                        item.source_track,
                        item.start_ms,
                        item.end_ms,
                        item.duration_ms,
                        item.text,
                        item.speaker_key,
                        item.speaker_label,
                        item.timing_origin,
                        item.speaker_origin,
                        item.alignment_quality.value,
                        item.provider_native_id,
                        item.occurrence_index,
                    )
                    for item in stable_segments
                ],
            )
            conn.executemany(
                """
                INSERT INTO canonical_transcript_segments_fts
                    (artifact_id, segment_id, text, speaker_label, source_track)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.artifact_id,
                        item.segment_id,
                        item.text,
                        item.speaker_label,
                        item.source_track,
                    )
                    for item in stable_segments
                ],
            )
            conn.executemany(
                """
                INSERT INTO canonical_artifact_inputs
                    (artifact_id, input_kind, input_id, input_sha256, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        artifact_id,
                        item.input_kind,
                        item.input_id,
                        item.input_sha256,
                        _canonical_json(dict(item.metadata)),
                    )
                    for item in normalized_inputs
                ],
            )
            self._fault("after_artifact_rows", conn)
            if current_head is None:
                head_cursor = conn.execute(
                    """
                    INSERT INTO canonical_transcript_heads
                        (transcript_id, artifact_id, generation, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(transcript_id) DO NOTHING
                    """,
                    (attempt.transcript_id, artifact_id, generation, now),
                )
            else:
                head_cursor = conn.execute(
                    """
                    UPDATE canonical_transcript_heads
                    SET artifact_id = ?, generation = ?, updated_at = ?
                    WHERE transcript_id = ? AND generation = ?
                    """,
                    (
                        artifact_id,
                        generation,
                        now,
                        attempt.transcript_id,
                        expected_head_generation,
                    ),
                )
            if head_cursor.rowcount != 1:
                raise ArtifactConflict("Canonical head CAS lost.")
            legacy_content = self.render_legacy_content(stable_segments)
            legacy_cursor = conn.execute(
                """
                UPDATE transcripts
                SET content = ?, preview = ?, status = 'completed', step = '', updated_at = ?
                WHERE id = ?
                """,
                (legacy_content, _preview(legacy_content), now, attempt.transcript_id),
            )
            if legacy_cursor.rowcount != 1:
                raise ArtifactConflict("Compatibility transcript projection CAS lost.")
            fts_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transcripts_fts'"
            ).fetchone()
            if fts_exists is not None:
                conn.execute("DELETE FROM transcripts_fts WHERE id = ?", (attempt.transcript_id,))
                conn.execute(
                    """
                    INSERT INTO transcripts_fts(rowid, id, title, content, summary, channel)
                    SELECT rowid, id, title, content, summary, channel
                    FROM transcripts WHERE id = ?
                    """,
                    (attempt.transcript_id,),
                )
            complete_cursor = conn.execute(
                """
                UPDATE transcription_attempts
                SET state = ?, state_version = state_version + 1,
                    canonical_artifact_id = ?, lease_owner = '', lease_expires_at = '',
                    updated_at = ?, finished_at = ?
                WHERE id = ? AND state = ? AND state_version = ?
                """,
                (
                    AttemptState.COMPLETED.value,
                    artifact_id,
                    now,
                    now,
                    attempt_id,
                    AttemptState.COMMITTING.value,
                    expected_attempt_version,
                ),
            )
            if complete_cursor.rowcount != 1:
                raise ArtifactConflict("Attempt completion CAS lost.")
            self._fault("before_canonical_commit", conn)
            artifact_row = conn.execute(
                "SELECT * FROM canonical_transcript_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            head_row = conn.execute(
                "SELECT * FROM canonical_transcript_heads WHERE transcript_id = ?",
                (attempt.transcript_id,),
            ).fetchone()
            completed_row = conn.execute(
                "SELECT * FROM transcription_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            conn.commit()
            return CanonicalCommitResult(
                artifact=self._artifact_from_row(conn, artifact_row),
                head=self._head_from_row(head_row),
                attempt=self._attempt_from_row(completed_row),
                committed=True,
                superseded=False,
            )
        except Exception:
            conn.rollback()
            raise

    def add_source_asset(
        self,
        *,
        transcript_id: str,
        source_track: str,
        asset_kind: str,
        purpose: str,
        relative_path: str,
        sha256: str,
        byte_count: int,
        asset_id: str | None = None,
    ) -> SourceAsset:
        asset_id = _safe_scalar(asset_id or uuid4().hex, field_name="asset_id")
        transcript_id = _safe_scalar(transcript_id, field_name="transcript_id")
        source_track = _safe_scalar(source_track, field_name="source_track")
        asset_kind = _safe_scalar(asset_kind, field_name="asset_kind")
        purpose = _safe_scalar(purpose, field_name="purpose")
        relative_path = _relative_runtime_path(relative_path)
        sha256 = _require_sha256(sha256, field_name="sha256")
        if isinstance(byte_count, bool) or int(byte_count) != byte_count or int(byte_count) < 0:
            raise ValueError("byte_count must be a non-negative integer.")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM transcript_source_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if existing is not None:
                asset = self._asset_from_row(existing)
                expected = (
                    transcript_id,
                    source_track,
                    asset_kind,
                    purpose,
                    relative_path,
                    sha256,
                    int(byte_count),
                )
                actual = (
                    asset.transcript_id,
                    asset.source_track,
                    asset.asset_kind,
                    asset.purpose,
                    asset.relative_path,
                    asset.sha256,
                    asset.byte_count,
                )
                if actual != expected:
                    raise ArtifactConflict("Source asset id is already bound to different data.")
                conn.commit()
                return asset
            now = _now_iso()
            conn.execute(
                """
                INSERT INTO transcript_source_assets
                    (id, transcript_id, source_track, asset_kind, purpose, state,
                     state_version, relative_path, sha256, byte_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    transcript_id,
                    source_track,
                    asset_kind,
                    purpose,
                    SourceAssetState.AVAILABLE.value,
                    relative_path,
                    sha256,
                    int(byte_count),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM transcript_source_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            conn.commit()
            return self._asset_from_row(row)
        except Exception:
            conn.rollback()
            raise

    def get_source_asset(self, asset_id: str) -> SourceAsset | None:
        row = self._connect().execute(
            "SELECT * FROM transcript_source_assets WHERE id = ?", (asset_id,)
        ).fetchone()
        return self._asset_from_row(row) if row else None

    def list_source_assets(self, transcript_id: str) -> tuple[SourceAsset, ...]:
        transcript_id = _safe_scalar(transcript_id, field_name="transcript_id")
        rows = self._connect().execute(
            """
            SELECT * FROM transcript_source_assets
            WHERE transcript_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (transcript_id,),
        ).fetchall()
        return tuple(self._asset_from_row(row) for row in rows)

    def list_source_assets_by_state(
        self,
        state: SourceAssetState,
        *,
        purpose: str | None = None,
        limit: int = 250,
    ) -> tuple[SourceAsset, ...]:
        """Return bounded maintenance work without exposing local paths publicly."""
        if not isinstance(state, SourceAssetState):
            state = SourceAssetState(str(state))
        if isinstance(limit, bool) or int(limit) != limit or not 1 <= int(limit) <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000.")
        params: list[Any] = [state.value]
        purpose_clause = ""
        if purpose is not None:
            purpose_clause = " AND purpose = ?"
            params.append(_safe_scalar(purpose, field_name="purpose"))
        params.append(int(limit))
        rows = self._connect().execute(
            f"""
            SELECT * FROM transcript_source_assets
            WHERE state = ?{purpose_clause}
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return tuple(self._asset_from_row(row) for row in rows)

    def mark_source_asset_purge_pending(
        self, asset_id: str, *, expected_version: int
    ) -> SourceAsset:
        return self._transition_asset(
            asset_id,
            expected_state=SourceAssetState.AVAILABLE,
            expected_version=expected_version,
            new_state=SourceAssetState.PURGE_PENDING,
        )

    def mark_source_asset_purged(
        self,
        asset_id: str,
        *,
        expected_version: int,
        tombstone_reason: str,
    ) -> SourceAsset:
        reason = _safe_scalar(tombstone_reason, field_name="tombstone_reason")
        return self._transition_asset(
            asset_id,
            expected_state=SourceAssetState.PURGE_PENDING,
            expected_version=expected_version,
            new_state=SourceAssetState.PURGED,
            tombstone_reason=reason,
        )

    def _transition_asset(
        self,
        asset_id: str,
        *,
        expected_state: SourceAssetState,
        expected_version: int,
        new_state: SourceAssetState,
        tombstone_reason: str = "",
    ) -> SourceAsset:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM transcript_source_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                raise ArtifactNotFound(f"Source asset {asset_id!r} does not exist.")
            current = self._asset_from_row(row)
            if current.state != expected_state or current.state_version != expected_version:
                raise ArtifactConflict("Source asset state/version CAS lost.")
            now = _now_iso()
            cursor = conn.execute(
                """
                UPDATE transcript_source_assets
                SET state = ?, state_version = state_version + 1,
                    relative_path = CASE WHEN ? THEN '' ELSE relative_path END,
                    tombstone_reason = ?, updated_at = ?,
                    purged_at = CASE WHEN ? THEN ? ELSE purged_at END
                WHERE id = ? AND state = ? AND state_version = ?
                """,
                (
                    new_state.value,
                    int(new_state == SourceAssetState.PURGED),
                    tombstone_reason,
                    now,
                    int(new_state == SourceAssetState.PURGED),
                    now,
                    asset_id,
                    expected_state.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ArtifactConflict("Source asset state/version CAS lost.")
            persisted = conn.execute(
                "SELECT * FROM transcript_source_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            conn.commit()
            return self._asset_from_row(persisted)
        except Exception:
            conn.rollback()
            raise
