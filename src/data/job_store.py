from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from loguru import logger


class JobType(str, Enum):
    YOUTUBE = "youtube"
    FILE = "file"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


_PENDING_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


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
        self._db_path = db_path or (Path(__file__).resolve().parents[2] / "transcripts.db")
        self._lock = threading.Lock()
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

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
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return JobRecord.from_row(row)

    def get_by_transcript_id(self, transcript_id: str) -> Optional[JobRecord]:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE transcript_id = ? ORDER BY created_at DESC LIMIT 1",
                    (transcript_id,),
                ).fetchone()
        if not row:
            return None
        return JobRecord.from_row(row)

    def list_pending(self, *, limit: int = 100) -> list[JobRecord]:
        now = _now_iso()
        query_limit = max(1, int(limit))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status IN (?, ?)
                      AND (next_retry_at = '' OR next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (*_PENDING_STATUSES, now, query_limit),
                ).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def seconds_until_next_retry(self) -> float | None:
        now = datetime.now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT next_retry_at
                    FROM jobs
                    WHERE status = ?
                      AND next_retry_at != ''
                    ORDER BY next_retry_at ASC
                    LIMIT 1
                    """,
                    (JobStatus.QUEUED.value,),
                ).fetchone()
        if not row:
            return None
        retry_at = _parse_iso(row["next_retry_at"] or "")
        if retry_at is None:
            return None
        delay = (retry_at - now).total_seconds()
        return max(0.0, delay)

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
        )

    def mark_completed(self, job_id: str) -> bool:
        return self._update_status(job_id, JobStatus.COMPLETED, updated_at=_now_iso(), next_retry_at="", last_error="")

    def mark_canceled(self, job_id: str, *, last_error: str = "") -> bool:
        return self._update_status(
            job_id,
            JobStatus.CANCELED,
            updated_at=_now_iso(),
            next_retry_at="",
            last_error=last_error,
        )

    def mark_failed(self, job_id: str, *, last_error: str) -> bool:
        return self._update_status(
            job_id,
            JobStatus.FAILED,
            updated_at=_now_iso(),
            next_retry_at="",
            last_error=last_error,
        )

    def set_retry(self, job_id: str, *, retry_at: str, last_error: str = "") -> bool:
        return self._update_status(
            job_id,
            JobStatus.QUEUED,
            updated_at=_now_iso(),
            next_retry_at=retry_at,
            last_error=last_error,
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

        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(sql, params)
                conn.commit()
                changed = int(cursor.rowcount or 0)
        if changed == 0:
            logger.debug(f"JobStore: no rows updated for job_id={job_id}, status={status.value}")
            return False
        return True
