"""Persisted singleton ownership for native audio capture.

The asyncio admission lock in :mod:`src.web_api` serializes one controller.
This store closes the remaining cross-controller/process race with an expiring
SQLite lease.  It deliberately stores only opaque workflow identifiers; native
endpoint IDs and audio metadata never cross this boundary.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src import database


_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_RESOURCE = "native_audio"


class AudioAdmissionStoreError(RuntimeError):
    pass


class AudioAdmissionConflict(AudioAdmissionStoreError):
    def __init__(self, active: "AudioAdmissionClaim") -> None:
        super().__init__(f"Native audio is owned by {active.owner_kind}.")
        self.active = active


@dataclass(frozen=True)
class AudioAdmissionClaim:
    owner_kind: str
    owner_id: str
    controller_id: str
    state_version: int
    lease_expires_at: str
    updated_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe(value: str, *, field: str) -> str:
    clean = str(value or "").strip()
    if not _SAFE_VALUE.fullmatch(clean):
        raise ValueError(f"{field} must be an opaque safe identifier.")
    return clean


class AudioAdmissionStore:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.db_path = Path(db_path or database._DB_PATH)
        self._now = now

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audio_admission_claims (
                    resource TEXT PRIMARY KEY,
                    owner_kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    controller_id TEXT NOT NULL,
                    state_version INTEGER NOT NULL CHECK(state_version >= 1),
                    lease_expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AudioAdmissionClaim:
        return AudioAdmissionClaim(
            owner_kind=str(row["owner_kind"]),
            owner_id=str(row["owner_id"]),
            controller_id=str(row["controller_id"]),
            state_version=int(row["state_version"]),
            lease_expires_at=str(row["lease_expires_at"]),
            updated_at=str(row["updated_at"]),
        )

    def acquire(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        controller_id: str,
        ttl_seconds: float = 600.0,
    ) -> AudioAdmissionClaim:
        owner_kind = _safe(owner_kind, field="owner_kind")
        owner_id = _safe(owner_id, field="owner_id")
        controller_id = _safe(controller_id, field="controller_id")
        ttl = float(ttl_seconds)
        if not 5.0 <= ttl <= 86_400.0:
            raise ValueError("ttl_seconds must be between 5 and 86400 seconds.")
        now = self._now().astimezone(timezone.utc)
        expires = now + timedelta(seconds=ttl)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM audio_admission_claims WHERE resource=?", (_RESOURCE,)
            ).fetchone()
            previous_version = 0
            if row is not None:
                current = self._from_row(row)
                previous_version = current.state_version
                current_expiry = _parse_iso(current.lease_expires_at)
                same_owner = (
                    current.owner_kind == owner_kind
                    and current.owner_id == owner_id
                    and current.controller_id == controller_id
                )
                if current_expiry is not None and current_expiry > now and not same_owner:
                    raise AudioAdmissionConflict(current)
            version = previous_version + 1
            conn.execute(
                """
                INSERT INTO audio_admission_claims
                    (resource,owner_kind,owner_id,controller_id,state_version,
                     lease_expires_at,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(resource) DO UPDATE SET
                    owner_kind=excluded.owner_kind,
                    owner_id=excluded.owner_id,
                    controller_id=excluded.controller_id,
                    state_version=excluded.state_version,
                    lease_expires_at=excluded.lease_expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    _RESOURCE,
                    owner_kind,
                    owner_id,
                    controller_id,
                    version,
                    _iso(expires),
                    _iso(now),
                ),
            )
            persisted = conn.execute(
                "SELECT * FROM audio_admission_claims WHERE resource=?", (_RESOURCE,)
            ).fetchone()
            conn.commit()
            return self._from_row(persisted)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew(
        self,
        claim: AudioAdmissionClaim,
        *,
        ttl_seconds: float = 600.0,
    ) -> AudioAdmissionClaim:
        ttl = float(ttl_seconds)
        if not 5.0 <= ttl <= 86_400.0:
            raise ValueError("ttl_seconds must be between 5 and 86400 seconds.")
        now = self._now().astimezone(timezone.utc)
        expires = now + timedelta(seconds=ttl)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE audio_admission_claims
                SET lease_expires_at=?,updated_at=?
                WHERE resource=? AND owner_kind=? AND owner_id=?
                  AND controller_id=? AND state_version=?
                """,
                (
                    _iso(expires),
                    _iso(now),
                    _RESOURCE,
                    claim.owner_kind,
                    claim.owner_id,
                    claim.controller_id,
                    claim.state_version,
                ),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT * FROM audio_admission_claims WHERE resource=?", (_RESOURCE,)
                ).fetchone()
                if row is not None:
                    raise AudioAdmissionConflict(self._from_row(row))
                raise AudioAdmissionStoreError("Native audio lease no longer exists.")
            persisted = conn.execute(
                "SELECT * FROM audio_admission_claims WHERE resource=?", (_RESOURCE,)
            ).fetchone()
            conn.commit()
            return self._from_row(persisted)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def transfer(
        self,
        claim: AudioAdmissionClaim,
        *,
        owner_id: str,
    ) -> AudioAdmissionClaim:
        owner_id = _safe(owner_id, field="owner_id")
        now = self._now().astimezone(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE audio_admission_claims
                SET owner_id=?,state_version=state_version+1,updated_at=?
                WHERE resource=? AND owner_kind=? AND owner_id=?
                  AND controller_id=? AND state_version=?
                """,
                (
                    owner_id,
                    _iso(now),
                    _RESOURCE,
                    claim.owner_kind,
                    claim.owner_id,
                    claim.controller_id,
                    claim.state_version,
                ),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT * FROM audio_admission_claims WHERE resource=?", (_RESOURCE,)
                ).fetchone()
                if row is not None:
                    raise AudioAdmissionConflict(self._from_row(row))
                raise AudioAdmissionStoreError("Native audio lease no longer exists.")
            persisted = conn.execute(
                "SELECT * FROM audio_admission_claims WHERE resource=?", (_RESOURCE,)
            ).fetchone()
            conn.commit()
            return self._from_row(persisted)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release(self, claim: AudioAdmissionClaim) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM audio_admission_claims
                WHERE resource=? AND owner_kind=? AND owner_id=?
                  AND controller_id=? AND state_version=?
                """,
                (
                    _RESOURCE,
                    claim.owner_kind,
                    claim.owner_id,
                    claim.controller_id,
                    claim.state_version,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    def active(self) -> AudioAdmissionClaim | None:
        now = self._now().astimezone(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM audio_admission_claims WHERE resource=?", (_RESOURCE,)
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            current = self._from_row(row)
            expiry = _parse_iso(current.lease_expires_at)
            if expiry is None or expiry <= now:
                conn.execute(
                    "DELETE FROM audio_admission_claims WHERE resource=? AND state_version=?",
                    (_RESOURCE, current.state_version),
                )
                conn.commit()
                return None
            conn.commit()
            return current
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
