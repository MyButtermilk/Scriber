from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from src.data.audio_admission_store import (
    AudioAdmissionConflict,
    AudioAdmissionStore,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 12, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def stores(tmp_path):
    clock = Clock()
    first = AudioAdmissionStore(tmp_path / "audio-admission.db", now=clock.now)
    second = AudioAdmissionStore(tmp_path / "audio-admission.db", now=clock.now)
    first.initialize()
    second.initialize()
    return clock, first, second


@pytest.mark.skipif(sys.platform != "win32", reason="Windows keeps open SQLite database files locked")
def test_initialize_releases_its_sqlite_file_handle(tmp_path):
    db_path = tmp_path / "initialized-audio-admission.db"
    store = AudioAdmissionStore(db_path)

    store.initialize()

    db_path.unlink()
    assert not db_path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows keeps open SQLite database files locked")
def test_release_releases_its_sqlite_file_handle(tmp_path):
    db_path = tmp_path / "released-audio-admission.db"
    store = AudioAdmissionStore(db_path)
    store.initialize()
    claim = store.acquire(
        owner_kind="live_mic",
        owner_id="session-1",
        controller_id="controller-a",
    )

    assert store.release(claim) is True

    db_path.unlink()
    assert not db_path.exists()


def test_active_lease_blocks_a_second_controller(tmp_path):
    _clock, first, second = stores(tmp_path)
    claim = first.acquire(owner_kind="live_mic", owner_id="session-1", controller_id="controller-a")

    with pytest.raises(AudioAdmissionConflict) as raised:
        second.acquire(owner_kind="meeting", owner_id="meeting-1", controller_id="controller-b")

    assert raised.value.active == claim
    assert second.active() == claim


def test_expired_lease_can_be_taken_over_atomically(tmp_path):
    clock, first, second = stores(tmp_path)
    old = first.acquire(
        owner_kind="device_test",
        owner_id="probe-1",
        controller_id="controller-a",
        ttl_seconds=5,
    )
    clock.advance(6)

    replacement = second.acquire(
        owner_kind="meeting",
        owner_id="meeting-1",
        controller_id="controller-b",
        ttl_seconds=30,
    )

    assert replacement.state_version == old.state_version + 1
    assert replacement.owner_kind == "meeting"
    assert first.release(old) is False


def test_released_claim_cannot_delete_a_fresh_foreign_controller_claim(tmp_path):
    _clock, first, second = stores(tmp_path)
    stale = first.acquire(
        owner_kind="live_mic",
        owner_id="session-1",
        controller_id="controller-a",
    )
    assert first.release(stale) is True
    fresh = second.acquire(
        owner_kind="live_mic",
        owner_id="session-1",
        controller_id="controller-b",
    )

    assert fresh.state_version > stale.state_version
    assert first.release(stale) is False
    assert second.active() == fresh


def test_initialize_seeds_the_generation_counter_from_an_existing_claim(tmp_path):
    db_path = tmp_path / "legacy-audio-admission.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE audio_admission_claims (
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
        conn.execute(
            """
            INSERT INTO audio_admission_claims
                (resource,owner_kind,owner_id,controller_id,state_version,
                 lease_expires_at,updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                "native_audio",
                "meeting",
                "meeting-legacy",
                "controller-a",
                7,
                "2026-07-13T00:00:00Z",
                "2026-07-12T00:00:00Z",
            ),
        )
        conn.commit()

    clock = Clock()
    store = AudioAdmissionStore(db_path, now=clock.now)
    store.initialize()
    legacy = store.active()
    assert legacy is not None
    assert store.release(legacy) is True

    fresh = store.acquire(
        owner_kind="meeting",
        owner_id="meeting-legacy",
        controller_id="controller-a",
    )

    assert fresh.state_version == 8
    assert store.release(legacy) is False
    assert store.active() == fresh


def test_heartbeat_preserves_version_and_extends_ownership(tmp_path):
    clock, first, second = stores(tmp_path)
    claim = first.acquire(
        owner_kind="meeting",
        owner_id="meeting-1",
        controller_id="controller-a",
        ttl_seconds=10,
    )
    clock.advance(8)
    renewed = first.renew(claim, ttl_seconds=10)
    clock.advance(5)

    assert renewed.state_version == claim.state_version
    with pytest.raises(AudioAdmissionConflict):
        second.acquire(
            owner_kind="live_mic",
            owner_id="session-2",
            controller_id="controller-b",
        )


def test_transfer_binds_pending_claim_to_durable_meeting_id(tmp_path):
    _clock, first, _second = stores(tmp_path)
    pending = first.acquire(
        owner_kind="meeting",
        owner_id="pending-123",
        controller_id="controller-a",
    )

    bound = first.transfer(pending, owner_id="meeting-456")

    assert bound.owner_id == "meeting-456"
    assert bound.state_version == pending.state_version + 1
    assert first.release(pending) is False
    assert first.release(bound) is True
    assert first.active() is None


@pytest.mark.parametrize("field", ["../meeting", "contains space", "", "x" * 161])
def test_claim_identifiers_are_opaque_and_bounded(tmp_path, field):
    store = AudioAdmissionStore(tmp_path / "audio-admission.db")
    store.initialize()
    with pytest.raises(ValueError, match="opaque safe identifier"):
        store.acquire(owner_kind="meeting", owner_id=field, controller_id="controller-a")
