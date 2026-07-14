from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
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
    next_retry_at: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

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
            payload=payload or {},
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
                    INSERT OR REPLACE INTO jobs
                    (id, transcript_id, type, status, payload, attempts, next_retry_at, last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.transcript_id,
                        record.job_type.value,
                        record.status.value,
                        json.dumps(record.payload, ensure_ascii=False),
                        record.attempts,
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

    def reset_running_to_queued(self) -> int:
        now = _now_iso()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?
                    WHERE status = ?
                    """,
                    (JobStatus.QUEUED.value, now, JobStatus.RUNNING.value),
                )
                conn.commit()
                return int(cursor.rowcount or 0)

    def mark_running(self, job_id: str) -> bool:
        now = _now_iso()
        return self._update_status(
            job_id,
            JobStatus.RUNNING,
            updated_at=now,
            extra_sql=", attempts = attempts + 1, next_retry_at = '', last_error = ''",
            expected_statuses=(JobStatus.QUEUED,),
        )

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
        return self._update_status(
            job_id,
            JobStatus.QUEUED,
            updated_at=_now_iso(),
            next_retry_at=normalized_retry_at,
            last_error=last_error,
            expected_statuses=(JobStatus.QUEUED, JobStatus.RUNNING),
        )

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
