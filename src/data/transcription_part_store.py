"""Job-owned durable provider checkpoints, never a cross-job audio cache.

Every write is fenced by the current running job attempt in the same SQLite
transaction. Only an explicit resume may reopen a definite HTTP rejection;
an interrupted request without a durable result is never replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.runtime.cancellation import await_with_delayed_cancellation, to_thread_cancellation_barrier

_REJECTED_STATUSES = frozenset({400, 401, 402, 403, 404, 413, 415, 422, 429})


class TranscriptionCheckpointMismatch(ValueError):
    """The source, exact route, request shape, or boundaries have changed."""


class TranscriptionPartOutcomeUnknown(RuntimeError):
    provider_request_may_be_committed = True

    def __init__(self) -> None:
        super().__init__("A transcription part may already have been accepted; automatic replay is disabled.")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def source_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def init_checkpoint_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS transcription_checkpoints (
        job_id TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        route_sha256 TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        manifest TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS transcription_parts (
        job_id TEXT NOT NULL,
        part_index INTEGER NOT NULL,
        state TEXT NOT NULL,
        payload TEXT,
        status INTEGER,
        remote_id TEXT,
        PRIMARY KEY(job_id, part_index)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS soniox_resource_cleanup (
        job_id TEXT NOT NULL,
        region TEXT NOT NULL,
        resource_kind TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        last_attempt_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(job_id, resource_kind, resource_id)
    )""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS queue_soniox_resource_cleanup
        BEFORE DELETE ON jobs BEGIN
        INSERT OR IGNORE INTO soniox_resource_cleanup(job_id, region, resource_kind, resource_id)
        SELECT OLD.id, COALESCE(json_extract(OLD.payload, '$.executionRoute.providerRegion'), ''),
            CASE p.part_index WHEN 0 THEN 'file' ELSE 'transcription' END, p.remote_id
        FROM transcription_parts p JOIN transcription_checkpoints c ON c.job_id = p.job_id
        WHERE p.job_id = OLD.id AND p.part_index IN (0, 1) AND p.remote_id IS NOT NULL
            AND json_extract(c.manifest, '$.kind') = 'soniox_remote_job';
        END""")
    # Existing JobStore connections do not enable foreign_keys. A trigger
    # keeps explicit transcript deletion atomic with its sensitive checkpoints.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS delete_transcription_checkpoints
        AFTER DELETE ON jobs BEGIN
        DELETE FROM transcription_parts WHERE job_id = OLD.id;
        DELETE FROM transcription_checkpoints WHERE job_id = OLD.id;
        END""")


class DurableTranscriptionCheckpoint:
    def __init__(
        self,
        db_path: Path,
        *,
        job_id: str,
        attempt: int,
        source_digest: str,
        source_path: Path,
        execution_route: dict[str, Any],
    ) -> None:
        self._db_path = db_path
        self.job_id = job_id
        self.attempt = attempt
        self.source_digest = source_digest
        self.source_path = str(source_path.resolve())
        self.route_digest = _digest(execution_route)

    @contextmanager
    def _transaction(self):
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            # A received paid result must survive process restart before its
            # temporary audio is released. FULL also requests a WAL fsync.
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute("SELECT status, attempts FROM jobs WHERE id = ?", (self.job_id,)).fetchone()
            if owner is None or owner["status"] != "running" or owner["attempts"] != self.attempt:
                raise RuntimeError("Transcription checkpoint job ownership was lost")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _manifest(self, conn: sqlite3.Connection, request_shape: dict[str, Any] | None = None):
        row = conn.execute("SELECT * FROM transcription_checkpoints WHERE job_id = ?", (self.job_id,)).fetchone()
        if row is not None and (
            row["source_sha256"] != self.source_digest
            or row["route_sha256"] != self.route_digest
            or (request_shape is not None and row["request_sha256"] != _digest(request_shape))
        ):
            raise TranscriptionCheckpointMismatch("Transcription checkpoint source or exact request settings changed")
        return row

    def _load_manifest(self, request_shape: dict[str, Any]) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = self._manifest(conn, request_shape)
            return json.loads(row["manifest"]) if row else None

    async def load_manifest(self, *, request_shape: dict[str, Any]) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._load_manifest, request_shape)

    def _bind(self, manifest: dict[str, Any], request_shape: dict[str, Any]) -> dict[str, Any]:
        serialized = _json(manifest)
        parts = manifest.get("parts")
        if not isinstance(parts, list) or not parts or len(parts) > 10000:
            raise ValueError("Transcription checkpoint requires a bounded nonempty parts manifest")
        indices = [part.get("index") for part in parts]
        first_index = indices[0]
        if (
            type(first_index) is not int
            or first_index not in {0, 1}
            or indices != list(range(first_index, first_index + len(parts)))
        ):
            raise ValueError("Transcription checkpoint parts must have contiguous indices")
        with self._transaction() as conn:
            row = self._manifest(conn, request_shape)
            if row is not None:
                if row["manifest"] != serialized:
                    raise TranscriptionCheckpointMismatch("Transcription checkpoint part boundaries changed")
                return json.loads(row["manifest"])
            conn.execute(
                "INSERT INTO transcription_checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.job_id,
                    self.source_path,
                    self.source_digest,
                    self.route_digest,
                    _digest(request_shape),
                    serialized,
                ),
            )
            conn.executemany(
                "INSERT INTO transcription_parts(job_id, part_index, state) VALUES (?, ?, 'unsent')",
                [(self.job_id, index) for index in indices],
            )
        return json.loads(serialized)

    async def bind(self, *, manifest: dict[str, Any], request_shape: dict[str, Any]) -> dict[str, Any]:
        return await to_thread_cancellation_barrier(self._bind, manifest, request_shape)

    def _part(self, conn: sqlite3.Connection, index: int):
        if self._manifest(conn) is None:
            raise RuntimeError("Transcription checkpoint manifest is not bound")
        row = conn.execute(
            "SELECT * FROM transcription_parts WHERE job_id = ? AND part_index = ?", (self.job_id, index)
        ).fetchone()
        if row is None:
            raise ValueError("Transcription part does not belong to the persisted manifest")
        return row

    def _lookup(self, index: int, allow_remote: bool = False) -> dict[str, Any] | None:
        with self._transaction() as conn:
            row = self._part(conn, index)
            if row["state"] == "succeeded":
                return json.loads(row["payload"])
            if row["state"] != "unsent" and not (allow_remote and row["state"] == "remote"):
                raise TranscriptionPartOutcomeUnknown()
            return None

    async def lookup(self, index: int, *, allow_remote: bool = False) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._lookup, index, allow_remote)

    def _mark_started(self, index: int) -> None:
        with self._transaction() as conn:
            if self._part(conn, index)["state"] != "unsent":
                raise TranscriptionPartOutcomeUnknown()
            conn.execute(
                "UPDATE transcription_parts SET state = 'in_flight' WHERE job_id = ? AND part_index = ?",
                (self.job_id, index),
            )
            conn.execute(
                "UPDATE jobs SET provider_request_state = 'may_be_committed' "
                "WHERE id = ? AND provider_request_attempt = attempts",
                (self.job_id,),
            )

    async def mark_started(self, index: int) -> None:
        _, pending_cancel = await await_with_delayed_cancellation(asyncio.to_thread(self._mark_started, index))
        if pending_cancel is not None:
            # The caller cannot have invoked its transport before this method
            # returned. Only this live proof may reopen the durable fence;
            # a process crash still leaves the request ambiguous.
            await await_with_delayed_cancellation(asyncio.to_thread(self._undo_unsent_start, index))
            raise pending_cancel

    def _undo_unsent_start(self, index: int) -> None:
        with self._transaction() as conn:
            self._part(conn, index)
            conn.execute(
                "UPDATE transcription_parts SET state = 'unsent' "
                "WHERE job_id = ? AND part_index = ? AND state = 'in_flight'",
                (self.job_id, index),
            )

    def _save_success(self, index: int, payload: dict[str, Any]) -> None:
        serialized = _json(payload)
        with self._transaction() as conn:
            row = self._part(conn, index)
            if row["state"] == "succeeded":
                if row["payload"] != serialized:
                    raise TranscriptionCheckpointMismatch("A durable transcription result cannot be replaced")
                return
            if row["state"] not in {"in_flight", "remote"}:
                raise RuntimeError("Transcription result has no matching started request")
            conn.execute(
                "UPDATE transcription_parts SET state = 'succeeded', payload = ? WHERE job_id = ? AND part_index = ?",
                (serialized, self.job_id, index),
            )

    async def save_success(self, index: int, payload: dict[str, Any]) -> None:
        await to_thread_cancellation_barrier(self._save_success, index, payload)

    def _mark_rejected(self, index: int, status: int) -> None:
        if status not in _REJECTED_STATUSES:
            return  # Gateway/server failures do not prove upstream rejection.
        with self._transaction() as conn:
            if self._part(conn, index)["state"] != "in_flight":
                raise RuntimeError("Transcription rejection has no matching started request")
            conn.execute(
                "UPDATE transcription_parts SET state = 'rejected', status = ? WHERE job_id = ? AND part_index = ?",
                (status, self.job_id, index),
            )

    async def mark_rejected(self, index: int, status: int) -> None:
        await to_thread_cancellation_barrier(self._mark_rejected, index, status)

    def _remote_id(self, index: int) -> str | None:
        with self._transaction() as conn:
            row = self._part(conn, index)
            if row["state"] == "in_flight" or row["state"] == "rejected":
                raise TranscriptionPartOutcomeUnknown()
            return row["remote_id"]

    async def remote_id(self, index: int) -> str | None:
        return await asyncio.to_thread(self._remote_id, index)

    def _save_remote_id(self, index: int, remote_id: str) -> None:
        if not remote_id or len(remote_id) > 256 or any(ord(ch) < 32 for ch in remote_id):
            raise ValueError("Invalid provider resource identifier")
        with self._transaction() as conn:
            row = self._part(conn, index)
            if row["state"] != "in_flight":
                raise RuntimeError("Provider resource has no matching started request")
            conn.execute(
                "UPDATE transcription_parts SET state = 'remote', remote_id = ? WHERE job_id = ? AND part_index = ?",
                (remote_id, self.job_id, index),
            )

    async def save_remote_id(self, index: int, remote_id: str) -> None:
        await to_thread_cancellation_barrier(self._save_remote_id, index, remote_id)
