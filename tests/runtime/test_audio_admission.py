"""The rules for holding the one native-audio lease.

Each test here names a way the lease can be lost. The owner's job is that none
of them ends with two captures believing they own the device.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.data.audio_admission_store import AudioAdmissionClaim, AudioAdmissionConflict
from src.runtime.audio_admission import AudioAdmissionOwner, same_claim


def _claim(
    *,
    owner_kind: str = "live_mic",
    owner_id: str = "session-1",
    controller_id: str = "controller-a",
    state_version: int = 1,
) -> AudioAdmissionClaim:
    return AudioAdmissionClaim(
        owner_kind=owner_kind,
        owner_id=owner_id,
        controller_id=controller_id,
        state_version=state_version,
        lease_expires_at="2099-01-01T00:00:00Z",
        updated_at="2026-08-14T00:00:00Z",
    )


class _Store:
    def __init__(self) -> None:
        self.acquired: list[dict[str, Any]] = []
        self.released: list[AudioAdmissionClaim] = []
        self.renewals = 0
        self.acquire_result = _claim()
        self.renew_error: Exception | None = None
        self.renew_hook: Any = None
        self.active_claim: AudioAdmissionClaim | None = None

    def acquire(self, **kwargs: Any) -> AudioAdmissionClaim:
        self.acquired.append(kwargs)
        return self.acquire_result

    def renew(self, claim: AudioAdmissionClaim, *, ttl_seconds: float) -> AudioAdmissionClaim:
        self.renewals += 1
        if self.renew_hook is not None:
            self.renew_hook(self.renewals)
        if self.renew_error is not None:
            raise self.renew_error
        return _claim(
            owner_kind=claim.owner_kind,
            owner_id=claim.owner_id,
            controller_id=claim.controller_id,
            state_version=claim.state_version,
        )

    def transfer(self, claim: AudioAdmissionClaim, *, owner_id: str) -> AudioAdmissionClaim:
        return _claim(
            owner_kind=claim.owner_kind,
            owner_id=owner_id,
            controller_id=claim.controller_id,
            state_version=claim.state_version + 1,
        )

    def release(self, claim: AudioAdmissionClaim) -> bool:
        self.released.append(claim)
        return True

    def active(self) -> AudioAdmissionClaim | None:
        return self.active_claim


class _Host:
    """Stands in for the controller the owner reaches its state through."""

    def __init__(self) -> None:
        self.store = _Store()
        self.claim: AudioAdmissionClaim | None = None
        self.heartbeat: asyncio.Task | None = None
        self.lost_meetings: set[str] = set()
        self.shutting_down = False
        self.emergency_stops: list[str] = []

    async def _emergency_stop(self, *, session_id: str) -> None:
        self.emergency_stops.append(session_id)

    def owner(self, **overrides: Any) -> AudioAdmissionOwner:
        settings: dict[str, Any] = {
            "resolve_admission": lambda: (self.store, "controller-a"),
            "get_lost_meetings": lambda: self.lost_meetings,
            "get_claim": lambda: self.claim,
            "set_claim": self._set_claim,
            "get_heartbeat": lambda: self.heartbeat,
            "set_heartbeat": self._set_heartbeat,
            "is_shutting_down": lambda: self.shutting_down,
            "on_live_mic_lost": self._emergency_stop,
            "heartbeat_seconds": 0.001,
        }
        settings.update(overrides)
        return AudioAdmissionOwner(**settings)

    def _set_claim(self, claim: AudioAdmissionClaim | None) -> None:
        self.claim = claim

    def _set_heartbeat(self, task: asyncio.Task | None) -> None:
        self.heartbeat = task


@pytest.fixture
def host():
    return _Host()


def test_a_renewed_claim_is_the_same_lease_but_a_rebinding_is_not():
    held = _claim()
    renewed = _claim()
    assert renewed is not held
    assert same_claim(held, renewed)

    rebound = _claim(state_version=2)
    assert not same_claim(rebound, held)
    assert not same_claim(None, held)


@pytest.mark.asyncio
async def test_acquiring_records_the_lease_and_clears_a_past_loss(host):
    host.lost_meetings.add("meeting-7")
    owner = host.owner()

    claim = await owner.acquire(owner_kind="meeting", owner_id="meeting-7", heartbeat=False)

    assert host.claim is claim
    assert host.lost_meetings == set()
    assert host.store.acquired[0]["controller_id"] == "controller-a"


@pytest.mark.asyncio
async def test_re_requesting_a_lease_this_process_holds_is_idempotent(host):
    host.claim = _claim(owner_kind="meeting", owner_id="meeting-7")
    owner = host.owner()

    again = await owner.acquire(owner_kind="meeting", owner_id="meeting-7", heartbeat=False)

    assert again is host.claim
    assert host.store.acquired == []


@pytest.mark.asyncio
async def test_a_different_owner_conflicts_without_touching_the_store(host):
    host.claim = _claim(owner_kind="live_mic", owner_id="session-1")
    owner = host.owner()

    with pytest.raises(AudioAdmissionConflict):
        await owner.acquire(owner_kind="meeting", owner_id="meeting-7", heartbeat=False)

    assert host.store.acquired == []


@pytest.mark.asyncio
async def test_a_lease_acquired_during_shutdown_is_released_instead_of_left_behind(host):
    """The worker thread cannot be cancelled, so the lease it made must be undone."""
    host.shutting_down = True
    owner = host.owner()

    with pytest.raises(asyncio.CancelledError):
        await owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False)

    assert host.store.released == [host.store.acquire_result]
    assert host.claim is None


@pytest.mark.asyncio
async def test_releasing_the_held_lease_stops_the_heartbeat(host):
    owner = host.owner()
    await owner.acquire(owner_kind="live_mic", owner_id="session-1")
    beating = host.heartbeat
    assert beating is not None

    assert await owner.release() is True

    assert host.claim is None
    assert host.heartbeat is None
    await asyncio.sleep(0)
    assert beating.cancelled() or beating.done()


@pytest.mark.asyncio
async def test_releasing_a_lease_we_no_longer_hold_leaves_the_current_one_alone(host):
    owner = host.owner()
    held = _claim(owner_kind="meeting", owner_id="meeting-7")
    host.claim = held
    stale = _claim(owner_kind="live_mic", owner_id="session-old")

    assert await owner.release(stale) is True

    assert host.claim is held
    assert host.store.released == [stale]


@pytest.mark.asyncio
async def test_releasing_nothing_reports_nothing(host):
    assert await host.owner().release() is False


@pytest.mark.asyncio
async def test_only_one_heartbeat_runs_however_often_it_is_started(host):
    owner = host.owner()
    host.claim = _claim()
    owner.start_heartbeat()
    first = host.heartbeat
    owner.start_heartbeat()
    owner.start_heartbeat()
    try:
        assert host.heartbeat is first
    finally:
        owner.stop_heartbeat()


@pytest.mark.asyncio
async def test_a_finished_heartbeat_is_replaced_rather_than_reused(host):
    owner = host.owner()
    owner.start_heartbeat()
    finished = host.heartbeat
    assert finished is not None
    # No claim is held, so the loop returns on its first tick.
    await asyncio.wait_for(finished, timeout=1.0)

    owner.start_heartbeat()
    try:
        assert host.heartbeat is not finished
    finally:
        owner.stop_heartbeat()


@pytest.mark.asyncio
async def test_the_heartbeat_keeps_the_lease_fresh(host):
    owner = host.owner()
    host.claim = _claim()
    owner.start_heartbeat()
    try:
        for _ in range(200):
            if host.store.renewals >= 2:
                break
            await asyncio.sleep(0.005)
        assert host.store.renewals >= 2
        assert host.claim is not None
    finally:
        owner.stop_heartbeat()


@pytest.mark.asyncio
async def test_losing_the_lease_to_another_controller_stops_a_live_capture(host):
    owner = host.owner()
    host.claim = _claim(owner_kind="live_mic", owner_id="session-1")
    host.store.renew_error = AudioAdmissionConflict(_claim(controller_id="controller-b"))

    await asyncio.wait_for(owner.run_heartbeat(), timeout=2.0)

    assert host.claim is None
    assert host.emergency_stops == ["session-1"]


@pytest.mark.asyncio
async def test_losing_the_lease_marks_a_meeting_so_a_later_request_can_say_so(host):
    owner = host.owner()
    host.claim = _claim(owner_kind="meeting", owner_id="meeting-7")
    host.store.renew_error = AudioAdmissionConflict(_claim(controller_id="controller-b"))

    await asyncio.wait_for(owner.run_heartbeat(), timeout=2.0)

    assert host.claim is None
    assert host.lost_meetings == {"meeting-7"}
    assert host.emergency_stops == []


@pytest.mark.asyncio
async def test_a_pending_meeting_id_is_not_worth_remembering(host):
    """Nothing durable exists under a pending id, so no later request can ask."""
    owner = host.owner()
    host.claim = _claim(owner_kind="meeting", owner_id="pending-abc")
    host.store.renew_error = AudioAdmissionConflict(_claim(controller_id="controller-b"))

    await asyncio.wait_for(owner.run_heartbeat(), timeout=2.0)

    assert host.lost_meetings == set()


@pytest.mark.asyncio
async def test_our_own_rebinding_is_adopted_rather_than_treated_as_a_loss(host):
    """The pending-to-durable transfer bumps the CAS generation on purpose."""
    owner = host.owner()
    held = _claim(owner_kind="meeting", owner_id="pending-abc")
    host.claim = held
    newer = _claim(owner_kind="meeting", owner_id="meeting-7", controller_id="controller-a", state_version=2)

    def first_renewal_loses_the_race(attempt: int) -> None:
        host.store.renew_error = AudioAdmissionConflict(newer) if attempt == 1 else None

    host.store.renew_hook = first_renewal_loses_the_race

    owner.start_heartbeat()
    try:
        for _ in range(200):
            if host.store.renewals >= 2:
                break
            await asyncio.sleep(0.005)
    finally:
        owner.stop_heartbeat()

    assert host.claim is not None
    assert host.claim.owner_id == "meeting-7"
    assert host.lost_meetings == set()
    assert host.emergency_stops == []


@pytest.mark.asyncio
async def test_a_live_capture_gives_up_when_renewal_stays_unavailable(host):
    """Live Mic has no durable row to exclude a newcomer after the TTL lapses."""
    owner = host.owner()
    host.claim = _claim(owner_kind="live_mic", owner_id="session-1")
    host.store.renew_error = OSError("database is locked")

    await asyncio.wait_for(owner.run_heartbeat(), timeout=2.0)

    assert host.store.renewals == 3
    assert host.claim is None
    assert host.emergency_stops == ["session-1"]


@pytest.mark.asyncio
async def test_a_meeting_rides_out_an_unavailable_store(host):
    """Its durable row still excludes a newcomer, so it keeps trying."""
    owner = host.owner()
    host.claim = _claim(owner_kind="meeting", owner_id="meeting-7")
    host.store.renew_error = OSError("database is locked")

    owner.start_heartbeat()
    try:
        for _ in range(200):
            if host.store.renewals >= 5:
                break
            await asyncio.sleep(0.005)
    finally:
        owner.stop_heartbeat()

    assert host.store.renewals >= 5
    assert host.claim is not None
    assert host.lost_meetings == set()


@pytest.mark.asyncio
async def test_a_loss_reported_for_a_lease_we_no_longer_hold_is_ignored(host):
    owner = host.owner()
    held = _claim(owner_kind="meeting", owner_id="meeting-current")
    host.claim = held

    await owner.note_loss(_claim(owner_kind="live_mic", owner_id="session-old"), reason="superseded")

    assert host.claim is held
    assert host.emergency_stops == []


@pytest.mark.asyncio
async def test_transfer_rebinds_the_held_lease(host):
    owner = host.owner()
    held = _claim(owner_kind="meeting", owner_id="pending-abc")
    host.claim = held

    transferred = await owner.transfer(held, owner_id="meeting-7")

    assert transferred.owner_id == "meeting-7"
    assert host.claim is transferred


@pytest.mark.asyncio
async def test_transfer_of_a_lease_we_no_longer_hold_does_not_overwrite_the_current_one(host):
    owner = host.owner()
    current = _claim(owner_kind="live_mic", owner_id="session-2")
    host.claim = current
    stale = _claim(owner_kind="meeting", owner_id="pending-abc")

    transferred = await owner.transfer(stale, owner_id="meeting-7")

    assert transferred.owner_id == "meeting-7"
    assert host.claim is current


@pytest.mark.asyncio
async def test_a_foreign_claim_is_reported_but_our_own_is_not(host):
    owner = host.owner()
    assert await owner.foreign_claim() is None

    host.store.active_claim = _claim(controller_id="controller-a")
    assert await owner.foreign_claim() is None

    other = _claim(controller_id="controller-b")
    host.store.active_claim = other
    assert await owner.foreign_claim() is other


def test_the_meeting_claim_is_returned_only_for_its_own_meeting(host):
    owner = host.owner()
    held = _claim(owner_kind="meeting", owner_id="meeting-7")
    host.claim = held

    assert owner.meeting_claim("meeting-7") is held
    assert owner.meeting_claim("meeting-8") is None

    host.claim = _claim(owner_kind="live_mic", owner_id="meeting-7")
    assert owner.meeting_claim("meeting-7") is None


@pytest.mark.asyncio
async def test_the_store_is_resolved_on_every_use_not_captured_once(host):
    """Composition substitutes the store; a captured reference would bypass it."""
    owner = host.owner()
    replacement = _Store()
    host.store = replacement

    await owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False)

    assert replacement.acquired
