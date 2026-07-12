from __future__ import annotations

import json
import re
import sqlite3
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from uuid import uuid4

from src.runtime.paths import database_path


class MeetingImportStatus(str, Enum):
    CREATED = "created"
    RECEIVING = "receiving"
    RECEIVED = "received"
    PROBING = "probing"
    PREPARING = "preparing"
    WAITING_FOR_WORKSPACE = "waiting_for_workspace"
    COMMITTING = "committing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    FAILED = "failed"


TERMINAL_IMPORT_STATUSES = frozenset(
    {
        MeetingImportStatus.COMPLETED,
        MeetingImportStatus.CANCELED,
        MeetingImportStatus.FAILED,
    }
)

INCOMPLETE_UPLOAD_STATUSES = frozenset(
    {MeetingImportStatus.CREATED, MeetingImportStatus.RECEIVING}
)

RECOVERABLE_IMPORT_STATUSES = frozenset(
    {
        MeetingImportStatus.RECEIVED,
        MeetingImportStatus.PROBING,
        MeetingImportStatus.PREPARING,
        MeetingImportStatus.WAITING_FOR_WORKSPACE,
        MeetingImportStatus.COMMITTING,
        MeetingImportStatus.FINALIZING,
    }
)

# The restart inbox is a compact recovery surface, not an unbounded job log.
# Active work always wins space over recent terminal failures/cancellations.
MAX_MEETING_IMPORT_INBOX_ITEMS = 100
MAX_RECENT_TERMINAL_IMPORTS = 10

ALLOWED_IMPORT_TRANSITIONS: dict[MeetingImportStatus, frozenset[MeetingImportStatus]] = {
    MeetingImportStatus.CREATED: frozenset(
        {MeetingImportStatus.RECEIVING, MeetingImportStatus.CANCEL_REQUESTED, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.RECEIVING: frozenset(
        {MeetingImportStatus.RECEIVED, MeetingImportStatus.CANCEL_REQUESTED, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.RECEIVED: frozenset(
        {MeetingImportStatus.PROBING, MeetingImportStatus.CANCEL_REQUESTED, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.PROBING: frozenset(
        {MeetingImportStatus.PREPARING, MeetingImportStatus.CANCEL_REQUESTED, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.PREPARING: frozenset(
        {
            MeetingImportStatus.WAITING_FOR_WORKSPACE,
            MeetingImportStatus.CANCEL_REQUESTED,
            MeetingImportStatus.FAILED,
        }
    ),
    MeetingImportStatus.WAITING_FOR_WORKSPACE: frozenset(
        {MeetingImportStatus.COMMITTING, MeetingImportStatus.CANCEL_REQUESTED, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.COMMITTING: frozenset(
        {MeetingImportStatus.FINALIZING, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.FINALIZING: frozenset(
        {MeetingImportStatus.COMPLETED, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.CANCEL_REQUESTED: frozenset(
        {MeetingImportStatus.CANCELED, MeetingImportStatus.FAILED}
    ),
    MeetingImportStatus.COMPLETED: frozenset(),
    MeetingImportStatus.CANCELED: frozenset(),
    # A failed canonical finalization may be retried from the Meeting workspace.
    # Pre-commit failures never carry a meeting_id and therefore fail the value
    # validation when callers try to use this edge.
    MeetingImportStatus.FAILED: frozenset({MeetingImportStatus.FINALIZING}),
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_IMPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_MEETING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")


class MeetingImportStoreError(RuntimeError):
    pass


class MeetingImportNotFound(MeetingImportStoreError):
    pass


class InvalidMeetingImportTransition(MeetingImportStoreError):
    pass


class MeetingImportConflict(MeetingImportStoreError):
    """The persisted state no longer matches the caller's expected state."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("Meeting import metadata must be JSON serializable.") from exc


def _relative_path(value: str, *, field_name: str, allow_empty: bool = True) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text and allow_empty:
        return ""
    if not text:
        raise ValueError(f"{field_name} must not be empty.")
    if text.startswith(("/", "//")) or _WINDOWS_DRIVE_RE.match(text):
        raise ValueError(f"{field_name} must be relative to SCRIBER_DATA_DIR.")
    path = PurePosixPath(text)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must be a normalized relative path.")
    return path.as_posix()


def _sha256(value: str, *, field_name: str, allow_empty: bool = True) -> str:
    text = str(value or "").strip().lower()
    if not text and allow_empty:
        return ""
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a 64-character SHA-256 digest.")
    return text


def _nonnegative_int(value: int | None, *, field_name: str, allow_none: bool = True) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer.")
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative integer.") from exc
    if number < 0 or number != value:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return number


def _source_filename(value: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError("source_filename must be a filename, not a path.")
    if len(text) > 255:
        raise ValueError("source_filename is too long.")
    return text


@dataclass(frozen=True)
class MeetingImportRecord:
    id: str
    status: MeetingImportStatus
    source_filename: str
    expected_bytes: int | None
    received_bytes: int
    original_relative_path: str = ""
    original_bytes: int | None = None
    original_sha256: str = ""
    normalized_relative_path: str = ""
    normalized_bytes: int | None = None
    normalized_sha256: str = ""
    profile_snapshot: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    meeting_id: str = ""
    cancel_requested: bool = False
    cancel_requested_at: str = ""
    error_code: str = ""
    error_message: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    finished_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MeetingImportRecord":
        return cls(
            id=str(row["id"]),
            status=MeetingImportStatus(str(row["status"])),
            source_filename=str(row["source_filename"] or ""),
            expected_bytes=int(row["expected_bytes"]) if row["expected_bytes"] is not None else None,
            received_bytes=int(row["received_bytes"] or 0),
            original_relative_path=str(row["original_relative_path"] or ""),
            original_bytes=int(row["original_bytes"]) if row["original_bytes"] is not None else None,
            original_sha256=str(row["original_sha256"] or ""),
            normalized_relative_path=str(row["normalized_relative_path"] or ""),
            normalized_bytes=(
                int(row["normalized_bytes"]) if row["normalized_bytes"] is not None else None
            ),
            normalized_sha256=str(row["normalized_sha256"] or ""),
            profile_snapshot=_json_object(row["profile_snapshot_json"]),
            probe=_json_object(row["probe_json"]),
            metadata=_json_object(row["metadata_json"]),
            meeting_id=str(row["meeting_id"] or ""),
            cancel_requested=bool(row["cancel_requested"]),
            cancel_requested_at=str(row["cancel_requested_at"] or ""),
            error_code=str(row["error_code"] or ""),
            error_message=str(row["error_message"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            finished_at=str(row["finished_at"] or ""),
        )

    def to_public(self) -> dict[str, Any]:
        """Serialize the durable job contract for token-protected REST responses."""
        return {
            "id": self.id,
            "status": self.status.value,
            "sourceFilename": self.source_filename,
            "expectedBytes": self.expected_bytes,
            "receivedBytes": self.received_bytes,
            "original": {
                "relativePath": self.original_relative_path,
                "bytes": self.original_bytes,
                "sha256": self.original_sha256,
            },
            "normalized": {
                "relativePath": self.normalized_relative_path,
                "bytes": self.normalized_bytes,
                "sha256": self.normalized_sha256,
            },
            "profileSnapshot": _json_object(_json(self.profile_snapshot)),
            "probe": _json_object(_json(self.probe)),
            "metadata": _json_object(_json(self.metadata)),
            "meetingId": self.meeting_id or None,
            "cancelRequested": self.cancel_requested,
            "cancelRequestedAt": self.cancel_requested_at or None,
            "errorCode": self.error_code or None,
            "errorMessage": self.error_message or None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "finishedAt": self.finished_at or None,
        }


class MeetingImportStore:
    """Durable state machine for two-phase Meeting recording imports.

    Paths stored here are always relative to ``SCRIBER_DATA_DIR``. The first
    phase durably receives and verifies the original upload. The second phase
    prepares a normalized track, commits the Meeting workspace, and hands it to
    the canonical Meeting finalizer.
    """

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or database_path()
        self._lock = threading.RLock()
        self._thread_local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._connection_generation = 0
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
        connections_lock: threading.Lock | None = None,
    ) -> None:
        if connections_lock is None:
            pending = list(connections)
            connections.clear()
        else:
            with connections_lock:
                pending = list(connections)
                connections.clear()
        for connection in pending:
            try:
                connection.close()
            except Exception:
                pass

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._thread_local, "conn", None)
        if (
            connection is None
            or getattr(self._thread_local, "connection_generation", -1)
            != self._connection_generation
        ):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self._db_path,
                timeout=30.0,
                check_same_thread=False,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._thread_local.conn = connection
            self._thread_local.connection_generation = self._connection_generation
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
            self._connection_generation += 1
        self._close_connection_list(connections)
        self._thread_local.conn = None
        self._thread_local.connection_generation = self._connection_generation

    def init_schema(self) -> None:
        statuses = ",".join(f"'{status.value}'" for status in MeetingImportStatus)
        with self._lock:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS meeting_import_jobs (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK(status IN ({statuses})),
                        source_filename TEXT NOT NULL,
                        expected_bytes INTEGER CHECK(expected_bytes IS NULL OR expected_bytes >= 0),
                        received_bytes INTEGER NOT NULL DEFAULT 0 CHECK(received_bytes >= 0),
                        original_relative_path TEXT NOT NULL DEFAULT '',
                        original_bytes INTEGER CHECK(original_bytes IS NULL OR original_bytes >= 0),
                        original_sha256 TEXT NOT NULL DEFAULT '',
                        normalized_relative_path TEXT NOT NULL DEFAULT '',
                        normalized_bytes INTEGER CHECK(normalized_bytes IS NULL OR normalized_bytes >= 0),
                        normalized_sha256 TEXT NOT NULL DEFAULT '',
                        profile_snapshot_json TEXT NOT NULL,
                        probe_json TEXT NOT NULL DEFAULT '{{}}',
                        metadata_json TEXT NOT NULL DEFAULT '{{}}',
                        meeting_id TEXT NOT NULL DEFAULT '',
                        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
                        cancel_requested_at TEXT NOT NULL DEFAULT '',
                        error_code TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_meeting_import_jobs_status_updated
                    ON meeting_import_jobs(status, updated_at, id)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_meeting_import_jobs_meeting_id
                    ON meeting_import_jobs(meeting_id)
                    WHERE meeting_id != ''
                    """
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def create(
        self,
        *,
        source_filename: str,
        profile_snapshot: dict[str, Any],
        expected_bytes: int | None = None,
        metadata: dict[str, Any] | None = None,
        import_id: str | None = None,
    ) -> MeetingImportRecord:
        if not isinstance(profile_snapshot, dict) or not profile_snapshot:
            raise ValueError("profile_snapshot must be a non-empty JSON object.")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object.")
        now = _now_iso()
        record = MeetingImportRecord(
            id=str(import_id or uuid4().hex),
            status=MeetingImportStatus.CREATED,
            source_filename=_source_filename(source_filename),
            expected_bytes=_nonnegative_int(expected_bytes, field_name="expected_bytes"),
            received_bytes=0,
            profile_snapshot=dict(profile_snapshot),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        if not _IMPORT_ID_RE.fullmatch(record.id):
            raise ValueError("import_id contains invalid characters.")
        profile_json = _json(record.profile_snapshot)
        metadata_json = _json(record.metadata)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    INSERT INTO meeting_import_jobs (
                        id, status, source_filename, expected_bytes, received_bytes,
                        profile_snapshot_json, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.status.value,
                        record.source_filename,
                        record.expected_bytes,
                        record.received_bytes,
                        profile_json,
                        metadata_json,
                        record.created_at,
                        record.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MeetingImportConflict(f"Meeting import {record.id!r} already exists.") from exc
        return record

    def get(self, import_id: str) -> MeetingImportRecord | None:
        row = self._connect().execute(
            "SELECT * FROM meeting_import_jobs WHERE id = ?", (str(import_id),)
        ).fetchone()
        return MeetingImportRecord.from_row(row) if row is not None else None

    def require(self, import_id: str) -> MeetingImportRecord:
        record = self.get(import_id)
        if record is None:
            raise MeetingImportNotFound(f"Meeting import {import_id!r} does not exist.")
        return record

    def find_by_meeting_id(self, meeting_id: str) -> MeetingImportRecord | None:
        clean_meeting_id = str(meeting_id or "").strip()
        if not clean_meeting_id:
            return None
        terminal_values = sorted(status.value for status in TERMINAL_IMPORT_STATUSES)
        placeholders = ",".join("?" for _ in terminal_values)
        row = self._connect().execute(
            f"""
            SELECT * FROM meeting_import_jobs
            WHERE meeting_id = ?
            ORDER BY
                CASE WHEN status IN ({placeholders}) THEN 1 ELSE 0 END ASC,
                updated_at DESC,
                created_at DESC,
                id DESC
            LIMIT 1
            """,
            (clean_meeting_id, *terminal_values),
        ).fetchone()
        return MeetingImportRecord.from_row(row) if row is not None else None

    def delete(self, import_id: str) -> bool:
        """Delete a terminal import after its owned files have been cleaned up."""
        with self._lock:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT status FROM meeting_import_jobs WHERE id = ?", (import_id,)
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return False
                status = MeetingImportStatus(str(row["status"]))
                if status not in TERMINAL_IMPORT_STATUSES:
                    raise MeetingImportConflict("Only terminal Meeting imports can be deleted.")
                connection.execute("DELETE FROM meeting_import_jobs WHERE id = ?", (import_id,))
                connection.execute("COMMIT")
                return True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def transition(
        self,
        import_id: str,
        new_status: MeetingImportStatus | str,
        *,
        expected_status: MeetingImportStatus | str | None = None,
        original_relative_path: str | None = None,
        original_bytes: int | None = None,
        original_sha256: str | None = None,
        normalized_relative_path: str | None = None,
        normalized_bytes: int | None = None,
        normalized_sha256: str | None = None,
        probe: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        meeting_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> MeetingImportRecord:
        target = (
            new_status
            if isinstance(new_status, MeetingImportStatus)
            else MeetingImportStatus(str(new_status))
        )
        expected = (
            expected_status
            if isinstance(expected_status, MeetingImportStatus)
            else MeetingImportStatus(str(expected_status))
            if expected_status is not None
            else None
        )
        updates: dict[str, Any] = {}
        if original_relative_path is not None:
            updates["original_relative_path"] = _relative_path(
                original_relative_path, field_name="original_relative_path", allow_empty=False
            )
        if original_bytes is not None:
            updates["original_bytes"] = _nonnegative_int(
                original_bytes, field_name="original_bytes", allow_none=False
            )
        if original_sha256 is not None:
            updates["original_sha256"] = _sha256(
                original_sha256, field_name="original_sha256", allow_empty=False
            )
        if normalized_relative_path is not None:
            updates["normalized_relative_path"] = _relative_path(
                normalized_relative_path,
                field_name="normalized_relative_path",
                allow_empty=False,
            )
        if normalized_bytes is not None:
            updates["normalized_bytes"] = _nonnegative_int(
                normalized_bytes, field_name="normalized_bytes", allow_none=False
            )
        if normalized_sha256 is not None:
            updates["normalized_sha256"] = _sha256(
                normalized_sha256, field_name="normalized_sha256", allow_empty=False
            )
        if probe is not None:
            if not isinstance(probe, dict):
                raise ValueError("probe must be a JSON object.")
            updates["probe_json"] = _json(probe)
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be a JSON object.")
            updates["metadata_json"] = _json(metadata)
        if meeting_id is not None:
            clean_meeting_id = str(meeting_id).strip()
            if not _MEETING_ID_RE.fullmatch(clean_meeting_id):
                raise ValueError("meeting_id contains invalid characters.")
            updates["meeting_id"] = clean_meeting_id
        if error_code is not None:
            updates["error_code"] = str(error_code).strip()
        if error_message is not None:
            updates["error_message"] = str(error_message).strip()

        with self._lock:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM meeting_import_jobs WHERE id = ?", (import_id,)
                ).fetchone()
                if row is None:
                    raise MeetingImportNotFound(f"Meeting import {import_id!r} does not exist.")
                current = MeetingImportRecord.from_row(row)
                if expected is not None and current.status != expected:
                    raise MeetingImportConflict(
                        f"Meeting import {import_id!r} is {current.status.value}, expected {expected.value}."
                    )
                if current.status == target:
                    if updates:
                        raise MeetingImportConflict(
                            "Idempotent transitions cannot mutate an already-entered state."
                        )
                    connection.execute("COMMIT")
                    return current
                if target not in ALLOWED_IMPORT_TRANSITIONS[current.status]:
                    raise InvalidMeetingImportTransition(
                        f"Cannot transition Meeting import from {current.status.value} to {target.value}."
                    )
                if current.cancel_requested and target not in {
                    MeetingImportStatus.CANCELED,
                    MeetingImportStatus.FAILED,
                }:
                    raise MeetingImportConflict("Meeting import cancellation has already been requested.")

                values = self._state_values(current, updates, target)
                self._validate_state_values(target, values)
                now = _now_iso()
                updates["status"] = target.value
                updates["updated_at"] = now
                if target == MeetingImportStatus.CANCEL_REQUESTED:
                    updates["cancel_requested"] = 1
                    updates["cancel_requested_at"] = now
                if current.status in TERMINAL_IMPORT_STATUSES and target not in TERMINAL_IMPORT_STATUSES:
                    updates["finished_at"] = ""
                    updates["error_code"] = ""
                    updates["error_message"] = ""
                if target in TERMINAL_IMPORT_STATUSES:
                    updates["finished_at"] = now
                columns = list(updates)
                params = [updates[column] for column in columns]
                cursor = connection.execute(
                    f"UPDATE meeting_import_jobs SET {', '.join(f'{column} = ?' for column in columns)} "
                    "WHERE id = ? AND status = ?",
                    (*params, import_id, current.status.value),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise MeetingImportConflict("Meeting import state changed concurrently.")
                persisted = connection.execute(
                    "SELECT * FROM meeting_import_jobs WHERE id = ?", (import_id,)
                ).fetchone()
                connection.execute("COMMIT")
                assert persisted is not None
                return MeetingImportRecord.from_row(persisted)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _state_values(
        current: MeetingImportRecord, updates: dict[str, Any], target: MeetingImportStatus
    ) -> dict[str, Any]:
        return {
            "status": target.value,
            "received_bytes": current.received_bytes,
            "original_relative_path": updates.get(
                "original_relative_path", current.original_relative_path
            ),
            "original_bytes": updates.get("original_bytes", current.original_bytes),
            "original_sha256": updates.get("original_sha256", current.original_sha256),
            "normalized_relative_path": updates.get(
                "normalized_relative_path", current.normalized_relative_path
            ),
            "normalized_bytes": updates.get("normalized_bytes", current.normalized_bytes),
            "normalized_sha256": updates.get("normalized_sha256", current.normalized_sha256),
            "meeting_id": updates.get("meeting_id", current.meeting_id),
        }

    @staticmethod
    def _validate_state_values(status: MeetingImportStatus, values: dict[str, Any]) -> None:
        at_or_after_receive = status in RECOVERABLE_IMPORT_STATUSES | {
            MeetingImportStatus.COMPLETED
        }
        if at_or_after_receive:
            if not (
                values["original_relative_path"]
                and values["original_bytes"] is not None
                and values["original_sha256"]
            ):
                raise MeetingImportConflict(
                    f"State {status.value} requires a verified original upload artifact."
                )
        at_or_after_prepare = status in {
            MeetingImportStatus.WAITING_FOR_WORKSPACE,
            MeetingImportStatus.COMMITTING,
            MeetingImportStatus.FINALIZING,
            MeetingImportStatus.COMPLETED,
        }
        if at_or_after_prepare:
            if not (
                values["normalized_relative_path"]
                and values["normalized_bytes"] is not None
                and values["normalized_sha256"]
            ):
                raise MeetingImportConflict(
                    f"State {status.value} requires a verified normalized audio artifact."
                )
        if status in {
            MeetingImportStatus.COMMITTING,
            MeetingImportStatus.FINALIZING,
            MeetingImportStatus.COMPLETED,
        } and not values["meeting_id"]:
            raise MeetingImportConflict(f"State {status.value} requires a Meeting workspace ID.")

    def begin_receiving(self, import_id: str) -> MeetingImportRecord:
        return self.transition(
            import_id,
            MeetingImportStatus.RECEIVING,
            expected_status=MeetingImportStatus.CREATED,
        )

    def update_receive_progress(self, import_id: str, received_bytes: int) -> MeetingImportRecord:
        byte_count = _nonnegative_int(
            received_bytes, field_name="received_bytes", allow_none=False
        )
        assert byte_count is not None
        with self._lock:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM meeting_import_jobs WHERE id = ?", (import_id,)
                ).fetchone()
                if row is None:
                    raise MeetingImportNotFound(f"Meeting import {import_id!r} does not exist.")
                current = MeetingImportRecord.from_row(row)
                if current.status != MeetingImportStatus.RECEIVING:
                    raise MeetingImportConflict("Upload progress is accepted only while receiving.")
                if current.cancel_requested:
                    raise MeetingImportConflict("Meeting import cancellation has already been requested.")
                if byte_count < current.received_bytes:
                    raise MeetingImportConflict("received_bytes must be monotonic.")
                if current.expected_bytes is not None and byte_count > current.expected_bytes:
                    raise MeetingImportConflict("received_bytes exceeds expected_bytes.")
                now = _now_iso()
                connection.execute(
                    """
                    UPDATE meeting_import_jobs
                    SET received_bytes = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (byte_count, now, import_id, MeetingImportStatus.RECEIVING.value),
                )
                persisted = connection.execute(
                    "SELECT * FROM meeting_import_jobs WHERE id = ?", (import_id,)
                ).fetchone()
                connection.execute("COMMIT")
                assert persisted is not None
                return MeetingImportRecord.from_row(persisted)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def mark_received(
        self,
        import_id: str,
        *,
        relative_path: str,
        byte_count: int,
        sha256: str,
    ) -> MeetingImportRecord:
        count = _nonnegative_int(byte_count, field_name="byte_count", allow_none=False)
        assert count is not None
        with self._lock:
            current = self.require(import_id)
            if current.expected_bytes is not None and count != current.expected_bytes:
                raise MeetingImportConflict("Completed upload size does not match expected_bytes.")
            if count < current.received_bytes:
                raise MeetingImportConflict("Completed upload is smaller than persisted progress.")
            # The transition and final progress update share the same outer RLock;
            # transition opens its own immediate transaction only after this write.
            if count != current.received_bytes:
                self.update_receive_progress(import_id, count)
            return self.transition(
                import_id,
                MeetingImportStatus.RECEIVED,
                expected_status=MeetingImportStatus.RECEIVING,
                original_relative_path=relative_path,
                original_bytes=count,
                original_sha256=sha256,
            )

    def mark_prepared(
        self,
        import_id: str,
        *,
        relative_path: str,
        byte_count: int,
        sha256: str,
        probe: dict[str, Any],
    ) -> MeetingImportRecord:
        return self.transition(
            import_id,
            MeetingImportStatus.WAITING_FOR_WORKSPACE,
            expected_status=MeetingImportStatus.PREPARING,
            normalized_relative_path=relative_path,
            normalized_bytes=byte_count,
            normalized_sha256=sha256,
            probe=probe,
        )

    def request_cancel(self, import_id: str) -> MeetingImportRecord:
        """Atomically win or lose cancellation against completion/other transitions."""
        while True:
            with self._lock:
                current = self.require(import_id)
                if current.status == MeetingImportStatus.CANCEL_REQUESTED:
                    return current
                if current.status in TERMINAL_IMPORT_STATUSES:
                    return current
                if current.status in {
                    MeetingImportStatus.COMMITTING,
                    MeetingImportStatus.FINALIZING,
                }:
                    raise MeetingImportConflict(
                        "The Meeting workspace already owns this import; retry or discard the Meeting instead."
                    )
                try:
                    return self.transition(
                        import_id,
                        MeetingImportStatus.CANCEL_REQUESTED,
                        expected_status=current.status,
                    )
                except MeetingImportConflict:
                    # Another store/process advanced the worker between the read
                    # and BEGIN IMMEDIATE. Re-read until cancellation or a terminal
                    # state wins; never silently lose an accepted cancel request.
                    continue

    def mark_canceled(self, import_id: str) -> MeetingImportRecord:
        return self.transition(
            import_id,
            MeetingImportStatus.CANCELED,
            expected_status=MeetingImportStatus.CANCEL_REQUESTED,
        )

    def mark_failed(
        self, import_id: str, *, error_code: str, error_message: str
    ) -> MeetingImportRecord:
        with self._lock:
            current = self.require(import_id)
            if current.status == MeetingImportStatus.CANCEL_REQUESTED:
                return self.mark_canceled(import_id)
            if current.status in TERMINAL_IMPORT_STATUSES:
                return current
            return self.transition(
                import_id,
                MeetingImportStatus.FAILED,
                expected_status=current.status,
                error_code=error_code,
                error_message=error_message,
            )

    def list_unfinished(self, *, limit: int = 1000) -> list[MeetingImportRecord]:
        statuses = [status for status in MeetingImportStatus if status not in TERMINAL_IMPORT_STATUSES]
        return self._list_statuses(statuses, limit=limit)

    def list_recoverable(self, *, limit: int = 1000) -> list[MeetingImportRecord]:
        return self._list_statuses(RECOVERABLE_IMPORT_STATUSES, limit=limit)

    def list_incomplete_uploads(self, *, limit: int = 1000) -> list[MeetingImportRecord]:
        return self._list_statuses(INCOMPLETE_UPLOAD_STATUSES, limit=limit)

    def list_cancel_requested(self, *, limit: int = 1000) -> list[MeetingImportRecord]:
        return self._list_statuses([MeetingImportStatus.CANCEL_REQUESTED], limit=limit)

    def list_inbox(
        self,
        *,
        limit: int = 24,
        recent_terminal_limit: int = 6,
    ) -> list[MeetingImportRecord]:
        """Return the bounded, server-authoritative Meeting import inbox.

        Non-terminal jobs are ordered ahead of recent failed/canceled jobs so
        a large failure history cannot hide work that still owns resources.
        All order clauses include ``id`` as a final deterministic tie-breaker.
        Completed imports are represented by their Meeting workspace and never
        duplicated in this recovery list.
        """
        query_limit = max(1, min(MAX_MEETING_IMPORT_INBOX_ITEMS, int(limit)))
        terminal_limit = max(
            0,
            min(
                MAX_RECENT_TERMINAL_IMPORTS,
                int(recent_terminal_limit),
                query_limit,
            ),
        )
        active_statuses = [
            status for status in MeetingImportStatus if status not in TERMINAL_IMPORT_STATUSES
        ]
        active = self._list_statuses_newest(active_statuses, limit=query_limit)
        remaining = query_limit - len(active)
        if remaining <= 0 or terminal_limit <= 0:
            return active
        recent_terminal = self._list_statuses_newest(
            [MeetingImportStatus.FAILED, MeetingImportStatus.CANCELED],
            limit=min(remaining, terminal_limit),
        )
        return [*active, *recent_terminal]

    def _list_statuses_newest(
        self, statuses: Iterable[MeetingImportStatus], *, limit: int
    ) -> list[MeetingImportRecord]:
        values = sorted({status.value for status in statuses})
        if not values:
            return []
        query_limit = max(1, min(MAX_MEETING_IMPORT_INBOX_ITEMS, int(limit)))
        placeholders = ",".join("?" for _ in values)
        rows = self._connect().execute(
            f"""
            SELECT * FROM meeting_import_jobs
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            (*values, query_limit),
        ).fetchall()
        return [MeetingImportRecord.from_row(row) for row in rows]

    def _list_statuses(
        self, statuses: Iterable[MeetingImportStatus], *, limit: int
    ) -> list[MeetingImportRecord]:
        values = sorted({status.value for status in statuses})
        if not values:
            return []
        query_limit = max(1, min(10_000, int(limit)))
        placeholders = ",".join("?" for _ in values)
        rows = self._connect().execute(
            f"""
            SELECT * FROM meeting_import_jobs
            WHERE status IN ({placeholders})
            ORDER BY updated_at ASC, created_at ASC, id ASC
            LIMIT ?
            """,
            (*values, query_limit),
        ).fetchall()
        return [MeetingImportRecord.from_row(row) for row in rows]
