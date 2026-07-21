from __future__ import annotations

import json
import re
import sqlite3
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

from loguru import logger

from src.runtime.paths import database_path


class JobType(str, Enum):
    YOUTUBE = "youtube"
    FILE = "file"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


PROVIDER_REQUEST_NOT_STARTED = "not_started"
PROVIDER_REQUEST_MAY_BE_COMMITTED = "may_be_committed"
PROVIDER_REQUEST_RESULT_DURABLE = "result_durable"
_PROVIDER_REQUEST_STATES = {
    PROVIDER_REQUEST_NOT_STARTED,
    PROVIDER_REQUEST_MAY_BE_COMMITTED,
    PROVIDER_REQUEST_RESULT_DURABLE,
}


_EXECUTION_ROUTE_FIELDS = {
    "provider",
    "providerRoute",
    "model",
    "transport",
    "language",
    "audioInputFormat",
    "audioSelectionMode",
    "audioPreparationImplementation",
    "providerAudioCapabilityId",
    "providerAudioCapabilityRevision",
    "audioInputFormatVerified",
    "customVocabularyPresent",
    "customVocabularyCount",
    "customVocabularySha256",
    "providerRegion",
    "providerEndpointSha256",
}
_EXECUTION_ROUTE_TEXT_LIMIT = 160
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_execution_route(route: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(route, dict) or not route:
        raise ValueError("Execution route must be a non-empty object")
    if set(route) - _EXECUTION_ROUTE_FIELDS:
        raise ValueError("Execution route contains unsupported fields")
    normalized: dict[str, Any] = {}
    for key in sorted(_EXECUTION_ROUTE_FIELDS):
        if key not in route:
            continue
        value = route[key]
        if key in {"audioInputFormatVerified", "customVocabularyPresent"}:
            if value not in {True, False, None}:
                raise ValueError("Execution route verification flag is invalid")
            normalized[key] = value
            continue
        if key == "customVocabularyCount":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Execution route vocabulary count is invalid")
            normalized[key] = value
            continue
        if key in {"customVocabularySha256", "providerEndpointSha256"}:
            if value is None:
                normalized[key] = None
                continue
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise ValueError("Execution route vocabulary digest is invalid")
            normalized[key] = value
            continue
        if value is None:
            normalized[key] = None
            continue
        if not isinstance(value, str):
            raise ValueError("Execution route identifiers must be strings")
        text = value.strip()
        if len(text) > _EXECUTION_ROUTE_TEXT_LIMIT:
            raise ValueError("Execution route identifier is too long")
        if any(ord(ch) < 32 for ch in text):
            raise ValueError("Execution route identifier contains control characters")
        normalized[key] = text
    if not normalized.get("provider") or not normalized.get("model"):
        raise ValueError("Execution route requires provider and model")
    return normalized


def _safe_route_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Copy a job payload while strictly sanitizing persisted route evidence."""

    normalized = dict(payload or {})
    for key in ("plannedFallbackRoute", "executionRoute", "executedRoute"):
        if key not in normalized:
            continue
        normalized[key] = _safe_execution_route(normalized[key])
    execution = normalized.get("executionRoute")
    executed = normalized.get("executedRoute")
    if isinstance(executed, dict) and execution != executed:
        raise ValueError("Executed route must match the selected execution route")
    return normalized


def _execution_route_can_finalize(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    """Allow exactly one unknown-format -> exact-format refinement."""

    format_fields = {
        "audioInputFormat",
        "audioSelectionMode",
        "audioPreparationImplementation",
        "audioInputFormatVerified",
    }
    for key in set(existing) | set(candidate):
        if key in format_fields:
            continue
        if existing.get(key) != candidate.get(key):
            return False
    existing_format = existing.get("audioInputFormat")
    candidate_format = candidate.get("audioInputFormat")
    if existing_format not in {None, ""}:
        return existing == candidate
    return bool(candidate_format and candidate.get("audioInputFormatVerified") is True)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # Retry timestamps are persisted as local, timezone-naive ISO strings.
        # Normalize offset-aware inputs before comparing them with datetime.now().
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _normalize_retry_iso(value: str) -> str:
    parsed = _parse_iso(value)
    return parsed.isoformat() if parsed is not None else ""


@dataclass(frozen=True)
class JobRecord:
    id: str
    transcript_id: str
    job_type: JobType
    status: JobStatus
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    provider_request_state: str = PROVIDER_REQUEST_NOT_STARTED
    provider_request_attempt: int = 0
    next_retry_at: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @property
    def provider_result_attempt_id(self) -> str:
        value = self.payload.get("providerResultAttemptId")
        text = str(value or "").strip()
        return text if re.fullmatch(r"[A-Za-z0-9._-]{1,160}", text) else ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "JobRecord":
        payload_raw = row["payload"] or "{}"
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {}
        return cls(
            id=row["id"],
            transcript_id=row["transcript_id"],
            job_type=JobType(str(row["type"])),
            status=JobStatus(str(row["status"])),
            payload=payload if isinstance(payload, dict) else {},
            attempts=int(row["attempts"] or 0),
            provider_request_state=str(
                row["provider_request_state"] or PROVIDER_REQUEST_NOT_STARTED
            ),
            provider_request_attempt=int(row["provider_request_attempt"] or 0),
            next_retry_at=row["next_retry_at"] or "",
            last_error=row["last_error"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


class JobStore:
    """Persistence layer for resumable background jobs."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or database_path()
        self._lock = threading.Lock()
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
        for conn in pending:
            try:
                conn.close()
            except Exception:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._thread_local, "conn", None)
        if (
            conn is None
            or getattr(self._thread_local, "connection_generation", -1) != self._connection_generation
        ):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._thread_local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
                self._thread_local.connection_generation = self._connection_generation
        return conn

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
            self._connection_generation += 1
        self._close_connection_list(connections)
        self._thread_local.conn = None
        self._thread_local.connection_generation = self._connection_generation

    def init_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        transcript_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL DEFAULT '{}',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        provider_request_state TEXT NOT NULL DEFAULT 'not_started',
                        provider_request_attempt INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT DEFAULT '',
                        last_error TEXT DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at
                    ON jobs(status, created_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_jobs_transcript_id
                    ON jobs(transcript_id)
                    """
                )
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
                }
                provider_state_added = "provider_request_state" not in columns
                provider_attempt_added = "provider_request_attempt" not in columns
                if provider_state_added:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN provider_request_state "
                        "TEXT NOT NULL DEFAULT 'not_started'"
                    )
                if provider_attempt_added:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN provider_request_attempt "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                if provider_state_added or provider_attempt_added:
                    # A pre-migration RUNNING row may already have crossed a
                    # billable remote boundary.  There is no durable evidence
                    # that it was still pre-request, so bind it to its current
                    # attempt and force startup reconciliation instead of
                    # replaying it automatically.
                    conn.execute(
                        """
                        UPDATE jobs
                        SET provider_request_state = ?,
                            provider_request_attempt = attempts
                        WHERE status = ? AND attempts > 0
                        """,
                        (
                            PROVIDER_REQUEST_MAY_BE_COMMITTED,
                            JobStatus.RUNNING.value,
                        ),
                    )
                    # A legacy queued retry with attempts > 0 is equally
                    # ambiguous: an older worker may have queued it after a
                    # timeout that occurred after provider acceptance.  There
                    # is no exact attempt binding to recover, so fail it closed
                    # instead of letting ``mark_running`` erase the fence.
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, provider_request_state = ?,
                            provider_request_attempt = attempts,
                            next_retry_at = '',
                            last_error = ?
                        WHERE status = ? AND attempts > 0
                        """,
                        (
                            JobStatus.FAILED.value,
                            PROVIDER_REQUEST_MAY_BE_COMMITTED,
                            "Provider request outcome is unknown after upgrade; "
                            "automatic replay was disabled.",
                            JobStatus.QUEUED.value,
                        ),
                    )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_jobs_status_next_retry_at
                    ON jobs(status, next_retry_at)
                    """
                )
                # Older builds could persist offset-aware retry timestamps. Normalize
                # them once at startup so due-job queries stay indexable in SQLite.
                retry_rows = conn.execute(
                    "SELECT id, next_retry_at FROM jobs WHERE next_retry_at != ''"
                ).fetchall()
                retry_updates = []
                for row in retry_rows:
                    raw_retry_at = str(row["next_retry_at"] or "")
                    normalized_retry_at = _normalize_retry_iso(raw_retry_at)
                    if normalized_retry_at != raw_retry_at:
                        retry_updates.append((normalized_retry_at, row["id"]))
                if retry_updates:
                    conn.executemany(
                        "UPDATE jobs SET next_retry_at = ? WHERE id = ?",
                        retry_updates,
                    )
                conn.commit()

    def enqueue(
        self,
        *,
        transcript_id: str,
        job_type: JobType | str,
        payload: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> JobRecord:
        now = _now_iso()
        resolved_job_type = job_type if isinstance(job_type, JobType) else JobType(str(job_type))
        record = JobRecord(
            id=job_id or uuid4().hex,
            transcript_id=transcript_id,
            job_type=resolved_job_type,
            status=JobStatus.QUEUED,
            payload=_safe_route_payload(payload),
            attempts=0,
            next_retry_at="",
            last_error="",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs
                    (id, transcript_id, type, status, payload, attempts,
                     provider_request_state, provider_request_attempt,
                     next_retry_at, last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.transcript_id,
                        record.job_type.value,
                        record.status.value,
                        json.dumps(record.payload, ensure_ascii=False),
                        record.attempts,
                        record.provider_request_state,
                        record.provider_request_attempt,
                        record.next_retry_at,
                        record.last_error,
                        record.created_at,
                        record.updated_at,
                    ),
                )
                conn.commit()
        return record

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return JobRecord.from_row(row)

    def get_by_transcript_id(self, transcript_id: str) -> Optional[JobRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE transcript_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (transcript_id,),
            ).fetchone()
        if not row:
            return None
        return JobRecord.from_row(row)

    def freeze_execution_route(
        self,
        job_id: str,
        route: dict[str, Any],
    ) -> bool:
        """Persist an immutable provider/model/format decision for a job.

        A queued route may initially omit the exact source format.  After a
        local container+codec probe, the same provider/model/capability entry
        may be refined exactly once with a verified format.  Any later attempt
        to switch provider, model, route, capability revision, or selected
        format fails closed.
        """

        candidate = _safe_execution_route(route)
        now = _now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status, payload FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is None or str(row["status"]) not in {
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                }:
                    conn.rollback()
                    return False
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                existing_raw = payload.get("executionRoute")
                executed_raw = payload.get("executedRoute")
                if isinstance(executed_raw, dict):
                    try:
                        executed = _safe_execution_route(executed_raw)
                    except ValueError:
                        conn.rollback()
                        return False
                    if executed != candidate:
                        conn.rollback()
                        return False
                if isinstance(existing_raw, dict):
                    try:
                        existing = _safe_execution_route(existing_raw)
                    except ValueError:
                        conn.rollback()
                        return False
                    if existing == candidate:
                        conn.rollback()
                        return True
                    if not _execution_route_can_finalize(existing, candidate):
                        conn.rollback()
                        return False
                payload["executionRoute"] = candidate
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET payload = ?, updated_at = ?
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now,
                        job_id,
                        JobStatus.QUEUED.value,
                        JobStatus.RUNNING.value,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True

    def record_executed_route(
        self,
        job_id: str,
        route: dict[str, Any],
    ) -> bool:
        """Record a completed route only when it matches the frozen selection.

        The transaction is idempotent.  It cannot replace a provider route with
        captions (or vice versa), including after a remote upload has started.
        """

        candidate = _safe_execution_route(route)
        now = _now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status, payload FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if row is None or str(row["status"]) not in {
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.COMPLETED.value,
                }:
                    conn.rollback()
                    return False
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                selected_raw = payload.get("executionRoute")
                if not isinstance(selected_raw, dict):
                    conn.rollback()
                    return False
                try:
                    selected = _safe_execution_route(selected_raw)
                except ValueError:
                    conn.rollback()
                    return False
                if selected != candidate:
                    conn.rollback()
                    return False
                executed_raw = payload.get("executedRoute")
                if isinstance(executed_raw, dict):
                    try:
                        executed = _safe_execution_route(executed_raw)
                    except ValueError:
                        conn.rollback()
                        return False
                    conn.rollback()
                    return executed == candidate
                payload["executedRoute"] = candidate
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET payload = ?, updated_at = ?
                    WHERE id = ? AND status IN (?, ?, ?)
                    """,
                    (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now,
                        job_id,
                        JobStatus.QUEUED.value,
                        JobStatus.RUNNING.value,
                        JobStatus.COMPLETED.value,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True

    def delete_by_transcript_id(self, transcript_id: str) -> int:
        """Remove all lifecycle rows owned by a deleted transcript."""
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM jobs WHERE transcript_id = ?",
                    (transcript_id,),
                )
                conn.commit()
                return max(0, int(cursor.rowcount or 0))

    def list_pending(self, *, limit: int = 100) -> list[JobRecord]:
        now = _now_iso()
        query_limit = max(1, int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                  AND (next_retry_at = '' OR next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (JobStatus.QUEUED.value, now, query_limit),
            ).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def list_running_job_ids(self) -> tuple[str, ...]:
        """Snapshot exact jobs that were already running at process start."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC, id ASC
                """,
                (JobStatus.RUNNING.value,),
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    def seconds_until_next_retry(self) -> float | None:
        now = datetime.now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT next_retry_at
                FROM jobs
                WHERE status = ?
                  AND next_retry_at != ''
                ORDER BY next_retry_at ASC, id ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            ).fetchone()
        if not row:
            return None
        retry_at = _parse_iso(row["next_retry_at"] or "")
        if retry_at is None:
            return None
        return max(0.0, (retry_at - now).total_seconds())

    def list_running_provider_outcomes(
        self,
        *,
        limit: int = 100,
        eligible_job_ids: Iterable[str] | None = None,
    ) -> list[JobRecord]:
        """Return running jobs whose remote outcome cannot be replayed blindly."""

        query_limit = max(1, min(1000, int(limit)))
        eligible = (
            tuple(dict.fromkeys(str(item) for item in eligible_job_ids if str(item)))
            if eligible_job_ids is not None
            else None
        )
        if eligible is not None and not eligible:
            return []
        with self._connect() as conn:
            if eligible is None:
                rows = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = ?
                      AND provider_request_attempt = attempts
                      AND provider_request_state IN (?, ?)
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (
                        JobStatus.RUNNING.value,
                        PROVIDER_REQUEST_MAY_BE_COMMITTED,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                        query_limit,
                    ),
                ).fetchall()
            else:
                # SQLite commonly limits a statement to 999 bound values.
                # Query the immutable startup allowlist in bounded chunks, then
                # apply the global deterministic ordering and page limit.
                collected: list[sqlite3.Row] = []
                for offset in range(0, len(eligible), 400):
                    chunk = eligible[offset : offset + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    collected.extend(
                        conn.execute(
                            f"""
                            SELECT * FROM jobs
                            WHERE id IN ({placeholders})
                              AND status = ?
                              AND provider_request_attempt = attempts
                              AND provider_request_state IN (?, ?)
                            """,
                            (
                                *chunk,
                                JobStatus.RUNNING.value,
                                PROVIDER_REQUEST_MAY_BE_COMMITTED,
                                PROVIDER_REQUEST_RESULT_DURABLE,
                            ),
                        ).fetchall()
                    )
                rows = sorted(
                    collected,
                    key=lambda row: (str(row["created_at"]), str(row["id"])),
                )[:query_limit]
        return [JobRecord.from_row(row) for row in rows]

    def _transition_provider_request_state(
        self,
        job_id: str,
        *,
        expected_states: tuple[str, ...],
        target_state: str,
    ) -> bool:
        if target_state not in _PROVIDER_REQUEST_STATES or any(
            state not in _PROVIDER_REQUEST_STATES for state in expected_states
        ):
            raise ValueError("Provider request state is invalid")
        now = _now_iso()
        placeholders = ",".join("?" for _ in expected_states)
        sql = f"""
            UPDATE jobs
            SET provider_request_state = ?, updated_at = ?
            WHERE id = ? AND status = ?
              AND provider_request_attempt = attempts
              AND provider_request_state IN ({placeholders})
        """
        params: list[Any] = [
            target_state,
            now,
            job_id,
            JobStatus.RUNNING.value,
            *expected_states,
        ]
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(sql, params)
                conn.commit()
                return int(cursor.rowcount or 0) == 1

    def mark_provider_request_may_be_committed(self, job_id: str) -> bool:
        return self._transition_provider_request_state(
            job_id,
            expected_states=(
                PROVIDER_REQUEST_NOT_STARTED,
                PROVIDER_REQUEST_MAY_BE_COMMITTED,
            ),
            target_state=PROVIDER_REQUEST_MAY_BE_COMMITTED,
        )

    def mark_provider_request_safe_to_retry(self, job_id: str) -> bool:
        return self._transition_provider_request_state(
            job_id,
            expected_states=(
                PROVIDER_REQUEST_NOT_STARTED,
                PROVIDER_REQUEST_MAY_BE_COMMITTED,
            ),
            target_state=PROVIDER_REQUEST_NOT_STARTED,
        )

    def mark_provider_result_durable(
        self,
        job_id: str,
        *,
        attempt_id: str,
    ) -> bool:
        """Bind one paid provider result to its exact durable artifact attempt."""

        normalized_attempt_id = str(attempt_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", normalized_attempt_id):
            raise ValueError("Provider result attempt id is invalid")
        now = _now_iso()
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status, attempts, provider_request_attempt, "
                    "provider_request_state, payload FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if (
                    row is None
                    or str(row["status"]) != JobStatus.RUNNING.value
                    or int(row["provider_request_attempt"] or 0)
                    != int(row["attempts"] or 0)
                    or str(row["provider_request_state"])
                    not in {
                        PROVIDER_REQUEST_MAY_BE_COMMITTED,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                    }
                ):
                    conn.rollback()
                    return False
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                existing = str(payload.get("providerResultAttemptId") or "").strip()
                if existing and existing != normalized_attempt_id:
                    conn.rollback()
                    return False
                payload["providerResultAttemptId"] = normalized_attempt_id
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET provider_request_state = ?, payload = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                      AND provider_request_attempt = attempts
                      AND provider_request_state IN (?, ?)
                    """,
                    (
                        PROVIDER_REQUEST_RESULT_DURABLE,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now,
                        job_id,
                        JobStatus.RUNNING.value,
                        PROVIDER_REQUEST_MAY_BE_COMMITTED,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True

    def queue_provider_result_recovery(
        self,
        job_id: str,
        *,
        retry_at: str = "",
    ) -> bool:
        """Queue only a controller-verified durable provider-stage recovery."""

        now = _now_iso()
        normalized_retry_at = _normalize_retry_iso(retry_at)
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT payload FROM jobs WHERE id = ? AND status = ? "
                    "AND provider_request_attempt = attempts "
                    "AND provider_request_state IN (?, ?)",
                    (
                        job_id,
                        JobStatus.RUNNING.value,
                        PROVIDER_REQUEST_MAY_BE_COMMITTED,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                    ),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                attempt_id = (
                    str(payload.get("providerResultAttemptId") or "").strip()
                    if isinstance(payload, dict)
                    else ""
                )
                if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", attempt_id):
                    conn.rollback()
                    return False
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, provider_request_state = ?,
                        next_retry_at = ?, last_error = '', updated_at = ?
                    WHERE id = ? AND status = ?
                      AND provider_request_attempt = attempts
                      AND provider_request_state IN (?, ?)
                    """,
                    (
                        JobStatus.QUEUED.value,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                        normalized_retry_at,
                        now,
                        job_id,
                        JobStatus.RUNNING.value,
                        PROVIDER_REQUEST_MAY_BE_COMMITTED,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    conn.rollback()
                    return False
                conn.commit()
                return True

    def reset_running_to_queued(
        self,
        *,
        eligible_job_ids: Iterable[str] | None = None,
    ) -> int:
        now = _now_iso()
        eligible = (
            tuple(dict.fromkeys(str(item) for item in eligible_job_ids if str(item)))
            if eligible_job_ids is not None
            else None
        )
        if eligible is not None and not eligible:
            return 0
        with self._lock:
            with self._connect() as conn:
                if eligible is None:
                    cursor = conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, updated_at = ?
                        WHERE status = ?
                          AND provider_request_state = ?
                          AND provider_request_attempt = attempts
                        """,
                        (
                            JobStatus.QUEUED.value,
                            now,
                            JobStatus.RUNNING.value,
                            PROVIDER_REQUEST_NOT_STARTED,
                        ),
                    )
                    changed = int(cursor.rowcount or 0)
                else:
                    changed = 0
                    for offset in range(0, len(eligible), 400):
                        chunk = eligible[offset : offset + 400]
                        placeholders = ",".join("?" for _ in chunk)
                        cursor = conn.execute(
                            f"""
                            UPDATE jobs
                            SET status = ?, updated_at = ?
                            WHERE id IN ({placeholders})
                              AND status = ?
                              AND provider_request_state = ?
                              AND provider_request_attempt = attempts
                            """,
                            (
                                JobStatus.QUEUED.value,
                                now,
                                *chunk,
                                JobStatus.RUNNING.value,
                                PROVIDER_REQUEST_NOT_STARTED,
                            ),
                        )
                        changed += int(cursor.rowcount or 0)
                conn.commit()
                return changed

    def mark_running(self, job_id: str) -> bool:
        now = _now_iso()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?, attempts = attempts + 1,
                        provider_request_attempt = attempts + 1,
                        provider_request_state = CASE
                            WHEN provider_request_state = ? THEN ?
                            ELSE ?
                        END,
                        next_retry_at = '', last_error = ''
                    WHERE id = ? AND status = ?
                    """,
                    (
                        JobStatus.RUNNING.value,
                        now,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                        PROVIDER_REQUEST_RESULT_DURABLE,
                        PROVIDER_REQUEST_NOT_STARTED,
                        job_id,
                        JobStatus.QUEUED.value,
                    ),
                )
                conn.commit()
                return int(cursor.rowcount or 0) == 1

    def mark_completed(self, job_id: str) -> bool:
        return self._update_status(
            job_id,
            JobStatus.COMPLETED,
            updated_at=_now_iso(),
            next_retry_at="",
            last_error="",
            # Some direct/import-style jobs complete without a separately
            # persisted running phase.  Preserve that contract while still
            # preventing a late worker callback from overwriting a terminal
            # cancellation or failure.
            expected_statuses=(JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.COMPLETED),
        )

    def mark_canceled(self, job_id: str, *, last_error: str = "") -> bool:
        return self._update_status(
            job_id,
            JobStatus.CANCELED,
            updated_at=_now_iso(),
            next_retry_at="",
            last_error=last_error,
            expected_statuses=(JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELED),
        )

    def mark_failed(self, job_id: str, *, last_error: str) -> bool:
        return self._update_status(
            job_id,
            JobStatus.FAILED,
            updated_at=_now_iso(),
            next_retry_at="",
            last_error=last_error,
            expected_statuses=(JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED),
        )

    def set_retry(self, job_id: str, *, retry_at: str, last_error: str = "") -> bool:
        normalized_retry_at = _normalize_retry_iso(retry_at)
        now = _now_iso()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?, next_retry_at = ?, last_error = ?
                    WHERE id = ? AND status IN (?, ?)
                      AND (
                        status = ?
                        OR provider_request_attempt != attempts
                        OR provider_request_state = ?
                      )
                    """,
                    (
                        JobStatus.QUEUED.value,
                        now,
                        normalized_retry_at,
                        last_error,
                        job_id,
                        JobStatus.QUEUED.value,
                        JobStatus.RUNNING.value,
                        JobStatus.QUEUED.value,
                        PROVIDER_REQUEST_NOT_STARTED,
                    ),
                )
                conn.commit()
                return int(cursor.rowcount or 0) == 1

    def _update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        updated_at: str,
        next_retry_at: str | None = None,
        last_error: str | None = None,
        extra_sql: str = "",
        expected_statuses: tuple[JobStatus, ...] = (),
    ) -> bool:
        set_parts = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status.value, updated_at]
        if next_retry_at is not None:
            set_parts.append("next_retry_at = ?")
            params.append(next_retry_at)
        if last_error is not None:
            set_parts.append("last_error = ?")
            params.append(last_error)
        sql = f"UPDATE jobs SET {', '.join(set_parts)}{extra_sql} WHERE id = ?"
        params.append(job_id)
        if expected_statuses:
            placeholders = ",".join("?" for _ in expected_statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(item.value for item in expected_statuses)

        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(sql, params)
                conn.commit()
                changed = int(cursor.rowcount or 0)
        if changed == 0:
            logger.debug(f"JobStore: no rows updated for job_id={job_id}, status={status.value}")
            return False
        return True
