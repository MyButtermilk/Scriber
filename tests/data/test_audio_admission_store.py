from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.data.audio_admission_store import (
    AudioAdmissionConflict,
    AudioAdmissionStore,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 12, tzinfo=timezone.utc)

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


def test_active_lease_blocks_a_second_controller(tmp_path):
    _clock, first, second = stores(tmp_path)
    claim = first.acquire(
        owner_kind="live_mic", owner_id="session-1", controller_id="controller-a"
    )

    with pytest.raises(AudioAdmissionConflict) as raised:
        second.acquire(
            owner_kind="meeting", owner_id="meeting-1", controller_id="controller-b"
        )

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
        store.acquire(
            owner_kind="meeting", owner_id=field, controller_id="controller-a"
        )
