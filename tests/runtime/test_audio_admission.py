"""The rules for holding the one native-audio lease.

Each test here names a way the lease can be lost. The owner's job is that none
of them ends with two captures believing they own the device.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.data.audio_admission_store import (
    AudioAdmissionClaim,
    AudioAdmissionConflict,
    AudioAdmissionStore,
)
from src.runtime.audio_admission import AudioAdmissionOwner, same_claim


def _claim(
    *,
    owner_kind: str = "live_mic",
    owner_id: str = "session-1",
    controller_id: str = "controller-a",
    state_version: int = 1,
    lease_expires_at: str = "2099-01-01T00:00:00Z",
) -> AudioAdmissionClaim:
    return AudioAdmissionClaim(
        owner_kind=owner_kind,
        owner_id=owner_id,
        controller_id=controller_id,
        state_version=state_version,
        lease_expires_at=lease_expires_at,
        updated_at="2026-08-14T00:00:00Z",
    )


def _expiring_claim(*, after_seconds: float, **kwargs: Any) -> AudioAdmissionClaim:
    expires = datetime.now(UTC) + timedelta(seconds=after_seconds)
    return _claim(
        lease_expires_at=expires.isoformat().replace("+00:00", "Z"),
        **kwargs,
    )


class _Store:
    def __init__(self) -> None:
        self.acquired: list[dict[str, Any]] = []
        self.released: list[AudioAdmissionClaim] = []
        self.renewals = 0
        self.acquire_result = _claim()
        self.transfers: list[tuple[AudioAdmissionClaim, str]] = []
        self.renew_error: Exception | None = None
        self.renew_hook: Any = None
        self.release_hook: Any = None
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
        self.transfers.append((claim, owner_id))
        return _claim(
            owner_kind=claim.owner_kind,
            owner_id=owner_id,
            controller_id=claim.controller_id,
            state_version=claim.state_version + 1,
        )

    def release(self, claim: AudioAdmissionClaim) -> bool:
        if self.release_hook is not None:
            self.release_hook(claim)
        self.released.append(claim)
        return True

    def active(self) -> AudioAdmissionClaim | None:
        return self.active_claim


class _BlockingRenewStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.renew_started = threading.Event()
        self.finish_renew = threading.Event()

    def renew(self, claim: AudioAdmissionClaim, *, ttl_seconds: float) -> AudioAdmissionClaim:
        self.renew_started.set()
        if not self.finish_renew.wait(timeout=2.0):
            raise TimeoutError("test did not release the renewal")
        return super().renew(claim, ttl_seconds=ttl_seconds)


class _BlockingAcquireStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.acquire_started = threading.Event()
        self.second_acquire_started = threading.Event()
        self.finish_acquire = threading.Event()
        self.acquire_calls = 0

    def acquire(self, **kwargs: Any) -> AudioAdmissionClaim:
        self.acquire_calls += 1
        if self.acquire_calls == 1:
            self.acquire_started.set()
        else:
            self.second_acquire_started.set()
        if not self.finish_acquire.wait(timeout=2.0):
            raise TimeoutError("test did not release the acquisition")
        return super().acquire(**kwargs)


class _BlockingAcquireFlakyReleaseStore(_BlockingAcquireStore):
    def __init__(self) -> None:
        super().__init__()
        self.release_attempts = 0

    def release(self, claim: AudioAdmissionClaim) -> bool:
        self.release_attempts += 1
        if self.release_attempts == 1:
            raise OSError("temporary SQLite release failure")
        return super().release(claim)


class _BlockingReleaseStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = threading.Event()
        self.finish_release = threading.Event()

    def release(self, claim: AudioAdmissionClaim) -> bool:
        self.release_started.set()
        if not self.finish_release.wait(timeout=2.0):
            raise TimeoutError("test did not release the release")
        return super().release(claim)


class _BlockingTransferStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.transfer_started = threading.Event()
        self.finish_transfer = threading.Event()

    def transfer(self, claim: AudioAdmissionClaim, *, owner_id: str) -> AudioAdmissionClaim:
        self.transfer_started.set()
        if not self.finish_transfer.wait(timeout=2.0):
            raise TimeoutError("test did not release the transfer")
        return super().transfer(claim, owner_id=owner_id)


class _BlockingTransferFlakyReleaseStore(_BlockingTransferStore):
    def __init__(self) -> None:
        super().__init__()
        self.release_attempts = 0

    def release(self, claim: AudioAdmissionClaim) -> bool:
        self.release_attempts += 1
        if self.release_attempts == 1:
            raise OSError("temporary SQLite release failure")
        return super().release(claim)


class _FlakyReleaseStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.release_attempts = 0

    def release(self, claim: AudioAdmissionClaim) -> bool:
        self.release_attempts += 1
        if self.release_attempts == 1:
            raise OSError("temporary SQLite release failure")
        return super().release(claim)


class _BlockingFailReleaseStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.release_started = threading.Event()
        self.finish_release = threading.Event()
        self.fail = True

    def release(self, claim: AudioAdmissionClaim) -> bool:
        self.release_started.set()
        if not self.finish_release.wait(timeout=2.0):
            raise TimeoutError("test did not release the release")
        if self.fail:
            raise OSError("temporary SQLite release failure")
        return super().release(claim)


class _MissingReleaseStore(_Store):
    def release(self, claim: AudioAdmissionClaim) -> bool:
        self.released.append(claim)
        return False


class _Host:
    """Stands in for the controller the owner reaches its state through."""

    def __init__(self) -> None:
        self.store = _Store()
        self.claim: AudioAdmissionClaim | None = None
        self.lost_meetings: set[str] = set()
        self.shutting_down = False
        self.emergency_stops: list[str] = []
        self.losses: list[tuple[AudioAdmissionClaim, str]] = []

    async def _record_loss(self, claim: AudioAdmissionClaim, reason: str) -> None:
        self.losses.append((claim, reason))
        if claim.owner_kind == "live_mic":
            self.emergency_stops.append(claim.owner_id)
        elif claim.owner_kind == "meeting" and not claim.owner_id.startswith("pending-"):
            self.lost_meetings.add(claim.owner_id)

    def owner(self, **overrides: Any) -> AudioAdmissionOwner:
        settings: dict[str, Any] = {
            "resolve_admission": lambda: (self.store, "controller-a"),
            "get_claim": lambda: self.claim,
            "set_claim": self._set_claim,
            "is_shutting_down": lambda: self.shutting_down,
            "loss_handler": self._record_loss,
            "heartbeat_seconds": 0.001,
            "stop_margin_seconds": 0.005,
            "loss_retry_seconds": 0.001,
        }
        settings.update(overrides)
        return AudioAdmissionOwner(**settings)

    def _set_claim(self, claim: AudioAdmissionClaim | None) -> None:
        self.claim = claim


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
async def test_acquiring_records_the_lease_without_invoking_the_loss_handler(host):
    owner = host.owner()

    claim = await owner.acquire(owner_kind="meeting", owner_id="meeting-7", heartbeat=False)

    assert host.claim is claim
    assert host.losses == []
    assert host.store.acquired[0]["controller_id"] == "controller-a"


@pytest.mark.asyncio
async def test_re_requesting_a_lease_this_process_holds_is_idempotent(host):
    host.claim = _claim(owner_kind="meeting", owner_id="meeting-7")
    owner = host.owner()

    again = await owner.acquire(owner_kind="meeting", owner_id="meeting-7", heartbeat=False)

    assert again is host.claim
    assert host.store.acquired == []


@pytest.mark.asyncio
async def test_re_requesting_the_same_claim_rebinds_its_capture_loss_handler(host):
    events: list[str] = []

    async def stop_original_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        events.append("original")

    async def stop_resumed_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        events.append("resumed")

    host.store.acquire_result = _claim(owner_kind="meeting", owner_id="meeting-7")
    owner = host.owner()
    held = await owner.acquire(
        owner_kind="meeting",
        owner_id="meeting-7",
        heartbeat=False,
        loss_handler=stop_original_capture,
    )

    again = await owner.acquire(
        owner_kind="meeting",
        owner_id="meeting-7",
        heartbeat=False,
        loss_handler=stop_resumed_capture,
    )
    await owner.note_loss(again, reason="superseded")

    assert again is held
    assert events == ["resumed"]
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_a_different_owner_conflicts_without_touching_the_store(host):
    host.claim = _claim(owner_kind="live_mic", owner_id="session-1")
    owner = host.owner()

    with pytest.raises(AudioAdmissionConflict):
        await owner.acquire(owner_kind="meeting", owner_id="meeting-7", heartbeat=False)

    assert host.store.acquired == []


@pytest.mark.asyncio
async def test_parallel_acquisitions_share_one_serialized_store_mutation(host):
    store = _BlockingAcquireStore()
    host.store = store
    owner = host.owner()

    first = asyncio.create_task(owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False))
    assert await asyncio.wait_for(asyncio.to_thread(store.acquire_started.wait), timeout=1.0) is True
    second = asyncio.create_task(owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False))
    await asyncio.sleep(0.05)

    serialized = not store.second_acquire_started.is_set()
    store.finish_acquire.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert serialized
    first_claim, second_claim = results
    assert isinstance(first_claim, AudioAdmissionClaim)
    assert first_claim is second_claim
    assert store.acquire_calls == 1


@pytest.mark.asyncio
async def test_acquire_waits_for_an_in_flight_release_before_choosing_ownership(host):
    store = _BlockingReleaseStore()
    host.store = store
    host.claim = _claim(owner_kind="live_mic", owner_id="session-1")
    store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-2", state_version=2)
    owner = host.owner()

    release = asyncio.create_task(owner.release())
    assert await asyncio.wait_for(asyncio.to_thread(store.release_started.wait), timeout=1.0) is True
    acquire = asyncio.create_task(owner.acquire(owner_kind="voice_enrollment", owner_id="sample-2", heartbeat=False))
    await asyncio.sleep(0)

    waited_for_release = not acquire.done()
    store.finish_release.set()
    results = await asyncio.gather(release, acquire, return_exceptions=True)
    assert waited_for_release
    assert results == [True, store.acquire_result]
    assert host.claim is store.acquire_result


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
async def test_close_waits_for_an_in_flight_acquire_and_its_rollback(host):
    store = _BlockingAcquireStore()
    host.store = store
    owner = host.owner()

    acquiring = asyncio.create_task(owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False))
    assert await asyncio.wait_for(asyncio.to_thread(store.acquire_started.wait), timeout=1.0) is True
    closing = asyncio.create_task(owner.close(task_drain_timeout_seconds=0.1))
    await asyncio.sleep(0.02)

    closed_early = closing.done()
    released_early = list(store.released)
    store.finish_acquire.set()

    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert await closing == 0
    assert closed_early is False
    assert released_early == []
    assert store.released == [store.acquire_result]
    assert host.claim is None


@pytest.mark.asyncio
async def test_close_settles_a_claim_retained_by_failed_in_flight_acquire_rollback(host):
    store = _BlockingAcquireFlakyReleaseStore()
    host.store = store
    owner = host.owner()

    acquiring = asyncio.create_task(owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False))
    assert await asyncio.wait_for(asyncio.to_thread(store.acquire_started.wait), timeout=1.0) is True
    closing = asyncio.create_task(owner.close(task_drain_timeout_seconds=0.1))
    await asyncio.sleep(0.02)
    store.finish_acquire.set()

    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert await closing == 0
    assert store.release_attempts == 2
    assert store.released == [store.acquire_result]
    assert host.claim is None


@pytest.mark.asyncio
async def test_close_cancellation_waits_for_in_flight_acquire_rollback(host):
    store = _BlockingAcquireStore()
    host.store = store
    owner = host.owner()

    acquiring = asyncio.create_task(owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False))
    assert await asyncio.wait_for(asyncio.to_thread(store.acquire_started.wait), timeout=1.0) is True
    closing = asyncio.create_task(owner.close(task_drain_timeout_seconds=0.1))
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0.02)
    still_waiting = not closing.done()
    store.finish_acquire.set()

    with pytest.raises(asyncio.CancelledError):
        await acquiring
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert still_waiting
    assert store.released == [store.acquire_result]
    assert host.claim is None


@pytest.mark.asyncio
async def test_canceled_acquire_retains_a_committed_claim_when_rollback_fails(host):
    store = _BlockingAcquireFlakyReleaseStore()
    host.store = store
    owner = host.owner()

    acquiring = asyncio.create_task(owner.acquire(owner_kind="live_mic", owner_id="session-1", heartbeat=False))
    assert await asyncio.wait_for(asyncio.to_thread(store.acquire_started.wait), timeout=1.0) is True
    acquiring.cancel()
    store.finish_acquire.set()

    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert host.claim is store.acquire_result
    assert store.release_attempts == 1

    assert await owner.release() is True
    assert host.claim is None
    assert store.release_attempts == 2


@pytest.mark.asyncio
async def test_failed_acquire_rollback_starts_watchdog_before_cancellation_returns(host):
    store = _BlockingAcquireFlakyReleaseStore()
    store.acquire_result = _expiring_claim(
        after_seconds=0.05,
        owner_kind="voice_enrollment",
        owner_id="sample-1",
    )
    host.store = store
    owner = host.owner(stop_margin_seconds=0.01)

    acquiring = asyncio.create_task(owner.acquire(owner_kind="voice_enrollment", owner_id="sample-1", heartbeat=False))
    assert await asyncio.wait_for(asyncio.to_thread(store.acquire_started.wait), timeout=1.0) is True
    acquiring.cancel()
    store.finish_acquire.set()
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    async with asyncio.timeout(0.5):
        while not host.losses:
            await asyncio.sleep(0.005)
    assert host.losses[0][1] == "lease_expired"
    async with asyncio.timeout(0.5):
        while host.claim is not None:
            await asyncio.sleep(0.005)
    assert store.release_attempts == 2


@pytest.mark.asyncio
async def test_releasing_the_held_lease_stops_the_heartbeat(host):
    owner = host.owner()
    await owner.acquire(owner_kind="live_mic", owner_id="session-1")
    assert owner.start_heartbeat() is False

    assert await owner.release() is True

    assert host.claim is None
    assert owner.start_heartbeat() is True
    await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_closing_seals_heartbeat_work_and_releases_the_held_lease_once(host):
    owner = host.owner()
    held = await owner.acquire(owner_kind="live_mic", owner_id="session-1")

    assert await owner.close(task_drain_timeout_seconds=1.0) == 0
    assert host.claim is None
    assert host.store.released == [held]
    assert owner.start_heartbeat() is False

    assert await owner.close(task_drain_timeout_seconds=1.0) == 0
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_close_bounds_a_running_renewal_but_keeps_owning_it(host):
    store = _BlockingRenewStore()
    host.store = store
    held = _claim()
    host.claim = held
    owner = host.owner()
    assert owner.start_heartbeat() is True
    assert await asyncio.wait_for(asyncio.to_thread(store.renew_started.wait), timeout=1.0) is True

    try:
        assert await owner.close(task_drain_timeout_seconds=0.01) == 1
        assert host.claim is None
        assert store.released == [held]
    finally:
        store.finish_renew.set()
        assert await owner.close(task_drain_timeout_seconds=1.0) == 0

    assert store.released == [held]


@pytest.mark.asyncio
async def test_close_and_an_explicit_release_share_one_durable_release(host):
    held = _claim()
    host.claim = held
    owner = host.owner()

    await asyncio.gather(
        owner.close(task_drain_timeout_seconds=1.0),
        owner.release(held),
    )

    assert host.claim is None
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_failed_close_keeps_the_claim_owned_so_close_can_retry(host):
    store = _FlakyReleaseStore()
    host.store = store
    held = _claim()
    host.claim = held
    owner = host.owner()

    with pytest.raises(OSError, match="temporary SQLite"):
        await owner.close(task_drain_timeout_seconds=1.0)

    assert host.claim is held
    assert await owner.close(task_drain_timeout_seconds=1.0) == 0
    assert host.claim is None
    assert store.release_attempts == 2
    assert store.released == [held]


@pytest.mark.asyncio
async def test_close_cancellation_wins_but_a_failed_release_remains_retryable(host):
    store = _BlockingFailReleaseStore()
    host.store = store
    held = _claim()
    host.claim = held
    owner = host.owner()

    closing = asyncio.create_task(owner.close(task_drain_timeout_seconds=1.0))
    assert await asyncio.wait_for(asyncio.to_thread(store.release_started.wait), timeout=1.0) is True
    closing.cancel()
    store.finish_release.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert host.claim is held

    store.fail = False
    assert await owner.close(task_drain_timeout_seconds=1.0) == 0
    assert host.claim is None
    assert store.released == [held]


@pytest.mark.asyncio
async def test_close_confirms_native_stop_before_releasing_the_store_claim(host):
    events: list[str] = []

    async def stop_native_capture(claim: AudioAdmissionClaim, reason: str) -> None:
        events.append(f"stop:{claim.owner_id}:{reason}")

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    host.store.release_hook = lambda claim: events.append(f"release:{claim.owner_id}")
    owner = host.owner()
    await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )

    assert await owner.close(task_drain_timeout_seconds=1.0) == 0

    assert events == ["stop:sample-1:shutdown", "release:sample-1"]
    assert host.claim is None


@pytest.mark.asyncio
async def test_close_retains_claim_when_native_stop_cannot_be_confirmed(host):
    attempts = 0

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("native stop was not confirmed")

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner = host.owner(loss_retry_attempts=2, loss_retry_seconds=0.001)
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )

    with pytest.raises(RuntimeError, match="not confirmed"):
        await owner.close(task_drain_timeout_seconds=1.0)
    assert host.claim is held
    assert host.store.released == []

    assert await owner.close(task_drain_timeout_seconds=1.0) == 0
    assert attempts == 3
    assert host.claim is None
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_release_cannot_overtake_an_in_progress_loss_handler(host):
    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        handler_started.set()
        await finish_handler.wait()

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner = host.owner()
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )
    losing = asyncio.create_task(owner.note_loss(held, reason="superseded"))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    releasing = asyncio.create_task(owner.release(held))
    await asyncio.sleep(0)

    assert releasing.done() is False
    assert host.store.released == []
    finish_handler.set()
    await losing
    assert await releasing is False
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_close_joins_an_in_progress_loss_handler_before_release(host):
    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        handler_started.set()
        await finish_handler.wait()

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner = host.owner()
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )
    losing = asyncio.create_task(owner.note_loss(held, reason="superseded"))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    closing = asyncio.create_task(owner.close(task_drain_timeout_seconds=1.0))
    await asyncio.sleep(0)

    assert closing.done() is False
    assert host.store.released == []
    finish_handler.set()
    await losing
    assert await closing == 0
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_a_missing_store_row_is_a_terminal_release(host):
    store = _MissingReleaseStore()
    host.store = store
    held = _claim()
    host.claim = held
    owner = host.owner()

    assert await owner.release() is False
    assert host.claim is None
    assert await owner.release(held) is False
    assert store.released == [held]


@pytest.mark.asyncio
async def test_failed_release_keeps_the_claim_owned_so_a_retry_can_finish(host):
    store = _FlakyReleaseStore()
    host.store = store
    held = _claim()
    host.claim = held
    owner = host.owner()

    with pytest.raises(OSError, match="temporary SQLite"):
        await owner.release()

    assert host.claim is held
    assert await owner.release() is True
    assert host.claim is None
    assert store.release_attempts == 2
    assert store.released == [held]


@pytest.mark.asyncio
async def test_release_cancellation_wins_over_store_failure_and_keeps_retry_state(host):
    store = _BlockingFailReleaseStore()
    host.store = store
    held = _claim()
    host.claim = held
    owner = host.owner()

    releasing = asyncio.create_task(owner.release())
    assert await asyncio.wait_for(asyncio.to_thread(store.release_started.wait), timeout=1.0) is True
    releasing.cancel()
    store.finish_release.set()

    with pytest.raises(asyncio.CancelledError):
        await releasing
    assert host.claim is held

    store.fail = False
    assert await owner.release() is True
    assert host.claim is None


@pytest.mark.asyncio
async def test_release_cancellation_is_delivered_after_terminal_store_success(host):
    store = _BlockingReleaseStore()
    host.store = store
    held = _claim()
    host.claim = held
    owner = host.owner()

    releasing = asyncio.create_task(owner.release())
    assert await asyncio.wait_for(asyncio.to_thread(store.release_started.wait), timeout=1.0) is True
    releasing.cancel()
    store.finish_release.set()

    with pytest.raises(asyncio.CancelledError):
        await releasing
    assert host.claim is None
    assert store.released == [held]


@pytest.mark.asyncio
async def test_parallel_explicit_releases_call_the_store_once(host):
    held = _claim()
    host.claim = held
    owner = host.owner()

    results = await asyncio.gather(owner.release(held), owner.release(held))

    assert sorted(results) == [False, True]
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_stale_release_cannot_delete_a_fresh_same_owner_sqlite_claim(host, tmp_path):
    store = AudioAdmissionStore(tmp_path / "reacquired-audio-admission.db")
    store.initialize()
    host.store = store
    owner = host.owner()

    first = await owner.acquire(
        owner_kind="live_mic",
        owner_id="session-1",
        heartbeat=False,
    )
    assert await owner.release(first) is True
    second = await owner.acquire(
        owner_kind="live_mic",
        owner_id="session-1",
        heartbeat=False,
    )

    assert not same_claim(first, second)
    assert await owner.release(first) is False
    assert host.claim is second
    assert store.active() == second
    assert await owner.release(second) is True
    assert host.claim is None
    assert store.active() is None
    assert await owner.close(task_drain_timeout_seconds=1.0) == 0


@pytest.mark.asyncio
async def test_stale_loss_cannot_stop_a_fresh_same_owner_sqlite_claim(host, tmp_path):
    store = AudioAdmissionStore(tmp_path / "reacquired-audio-loss.db")
    store.initialize()
    host.store = store
    owner = host.owner()

    first = await owner.acquire(
        owner_kind="live_mic",
        owner_id="session-1",
        heartbeat=False,
    )
    assert await owner.release(first) is True
    second = await owner.acquire(
        owner_kind="live_mic",
        owner_id="session-1",
        heartbeat=False,
    )

    await owner.note_loss(first, reason="superseded")

    assert host.losses == []
    assert host.claim is second
    assert store.active() == second
    assert await owner.release(second) is True
    assert await owner.close(task_drain_timeout_seconds=1.0) == 0


@pytest.mark.asyncio
async def test_public_release_refuses_a_stale_claim_and_leaves_the_current_one_alone(host):
    owner = host.owner()
    held = _claim(owner_kind="meeting", owner_id="meeting-7")
    host.claim = held
    stale = _claim(owner_kind="live_mic", owner_id="session-old")

    assert await owner.release(stale) is False

    assert host.claim is held
    assert host.store.released == []


@pytest.mark.asyncio
async def test_public_release_cannot_delete_a_foreign_claim_when_none_is_held(host):
    foreign = _claim(controller_id="controller-b")
    owner = host.owner()

    assert await owner.release(foreign) is False

    assert host.claim is None
    assert host.store.released == []


@pytest.mark.asyncio
async def test_releasing_nothing_reports_nothing(host):
    assert await host.owner().release() is False


@pytest.mark.asyncio
async def test_only_one_heartbeat_runs_however_often_it_is_started(host):
    owner = host.owner()
    host.claim = _claim()
    assert owner.start_heartbeat() is True
    assert owner.start_heartbeat() is False
    assert owner.start_heartbeat() is False
    try:
        assert host.store.renewals == 0
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_a_finished_heartbeat_is_replaced_rather_than_reused(host):
    owner = host.owner()
    assert owner.start_heartbeat() is True
    # No claim is held, so the loop returns on its first tick.
    await asyncio.sleep(0.01)

    assert owner.start_heartbeat() is True
    try:
        await asyncio.sleep(0)
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


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
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_heartbeat_retries_unconfirmed_native_stop_after_store_recovery(host):
    attempts = 0
    reasons: list[str] = []
    first_failure = asyncio.Event()
    foreign = _claim(controller_id="controller-b")
    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    host.store.renew_error = AudioAdmissionConflict(foreign)

    async def stop_native_capture(_claim: AudioAdmissionClaim, reason: str) -> None:
        nonlocal attempts
        attempts += 1
        reasons.append(reason)
        if attempts == 1:
            host.store.renew_error = None
            first_failure.set()
            raise RuntimeError("native stop was not confirmed")

    owner = host.owner(loss_retry_attempts=1)
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        loss_handler=stop_native_capture,
    )
    first_heartbeat = owner._heartbeat_task
    assert first_heartbeat is not None

    try:
        await asyncio.wait_for(first_failure.wait(), timeout=1.0)
        async with asyncio.timeout(1.0):
            while host.claim is not None:
                await asyncio.sleep(0.005)

        await asyncio.sleep(0)
        assert host.store.renewals >= 2
        assert attempts == 2
        assert reasons == ["superseded", "superseded"]
        assert host.store.released == [held]
        assert owner._heartbeat_task is first_heartbeat
        assert first_heartbeat.done() is True
    finally:
        if host.claim is not None:
            await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_current_heartbeat_renews_immediately_before_a_third_stop_attempt(host):
    attempts = 0
    claim_cleared = asyncio.Event()
    foreign = _claim(controller_id="controller-b")
    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner: AudioAdmissionOwner

    def fail_the_loss_and_first_recovery_renewals(attempt: int) -> None:
        if attempt == 1:
            # The first scheduled heartbeat starts quickly. Every later retry
            # must bypass this long normal interval after loss has begun.
            owner.heartbeat_seconds = 30.0
            host.store.renew_error = AudioAdmissionConflict(foreign)
        elif attempt == 2:
            host.store.renew_error = OSError("database was still busy")
        else:
            host.store.renew_error = None

    host.store.renew_hook = fail_the_loss_and_first_recovery_renewals

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeError("native stop was not confirmed")

    def observe_claim(claim: AudioAdmissionClaim | None) -> None:
        host._set_claim(claim)
        if claim is None:
            claim_cleared.set()

    owner = host.owner(
        set_claim=observe_claim,
        heartbeat_seconds=0.001,
        loss_retry_seconds=0.0,
    )
    await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        loss_handler=stop_native_capture,
    )

    try:
        await asyncio.wait_for(claim_cleared.wait(), timeout=10.0)

        assert attempts == 3
        assert host.store.renewals == 3
    finally:
        if host.claim is not None:
            await owner.close(task_drain_timeout_seconds=1.0)


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
        assert host.claim is not None
        assert host.claim.owner_id == "meeting-7"
        assert host.lost_meetings == set()
        assert host.emergency_stops == []
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_a_live_capture_gives_up_when_renewal_stays_unavailable(host):
    """Live Mic has no durable row to exclude a newcomer after the TTL lapses."""
    host.store.acquire_result = _expiring_claim(
        after_seconds=0.04,
        owner_kind="live_mic",
        owner_id="session-1",
    )
    host.store.renew_error = OSError("database is locked")
    owner = host.owner(ttl_seconds=0.04, heartbeat_seconds=0.001)

    await owner.acquire(owner_kind="live_mic", owner_id="session-1")
    async with asyncio.timeout(0.5):
        while not host.losses or host.claim is not None:
            await asyncio.sleep(0.005)

    assert host.store.renewals >= 1
    assert host.claim is None
    assert host.emergency_stops == ["session-1"]


@pytest.mark.asyncio
async def test_non_durable_lease_expires_by_ttl_even_when_heartbeat_is_disabled(host):
    host.store.acquire_result = _expiring_claim(
        after_seconds=0.03,
        owner_kind="voice_enrollment",
        owner_id="sample-1",
    )
    owner = host.owner(ttl_seconds=0.03, heartbeat_seconds=0.001)

    held = await owner.acquire(owner_kind="voice_enrollment", owner_id="sample-1", heartbeat=False)
    try:
        async with asyncio.timeout(0.5):
            while not host.losses:
                await asyncio.sleep(0.005)

        assert host.losses == [(held, "lease_expired")]
        assert host.store.renewals == 0
        async with asyncio.timeout(0.5):
            while host.claim is not None:
                await asyncio.sleep(0.005)
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_ttl_watchdog_is_not_delayed_by_a_blocked_renew_worker(host):
    store = _BlockingRenewStore()
    store.acquire_result = _expiring_claim(
        after_seconds=0.04,
        owner_kind="voice_enrollment",
        owner_id="sample-1",
    )
    host.store = store
    owner = host.owner(ttl_seconds=0.04, heartbeat_seconds=0.001)

    held = await owner.acquire(owner_kind="voice_enrollment", owner_id="sample-1")
    assert await asyncio.wait_for(asyncio.to_thread(store.renew_started.wait), timeout=1.0) is True
    try:
        async with asyncio.timeout(0.5):
            while not host.losses:
                await asyncio.sleep(0.005)
        assert store.finish_renew.is_set() is False
        assert host.losses == [(held, "lease_expired")]
    finally:
        store.finish_renew.set()
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_watchdog_uses_store_expiry_not_acquire_completion_plus_ttl(host):
    store = _BlockingAcquireStore()
    store.acquire_result = _expiring_claim(
        after_seconds=0.30,
        owner_kind="voice_enrollment",
        owner_id="sample-1",
    )
    host.store = store
    owner = host.owner(ttl_seconds=0.30, heartbeat_seconds=0.01)

    acquiring = asyncio.create_task(owner.acquire(owner_kind="voice_enrollment", owner_id="sample-1", heartbeat=False))
    assert await asyncio.wait_for(asyncio.to_thread(store.acquire_started.wait), timeout=1.0) is True
    await asyncio.sleep(0.18)
    store.finish_acquire.set()
    held = await acquiring
    completed_at = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(0.20):
            while not host.losses:
                await asyncio.sleep(0.005)
        assert host.losses == [(held, "lease_expired")]
        assert asyncio.get_running_loop().time() - completed_at < 0.20
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_watchdog_begins_native_stop_before_authoritative_expiry_margin(host):
    events: list[str] = []
    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()
    expires_at = datetime.now(UTC) + timedelta(seconds=0.30)
    host.store.acquire_result = _claim(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        lease_expires_at=expires_at.isoformat().replace("+00:00", "Z"),
    )
    host.store.release_hook = lambda _claim: events.append("store_release")

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        events.append("native_stop_started")
        handler_started.set()
        await finish_handler.wait()
        events.append("native_stop_confirmed")

    owner = host.owner(ttl_seconds=0.30, stop_margin_seconds=0.15)
    await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )
    try:
        await asyncio.wait_for(handler_started.wait(), timeout=0.25)
        remaining_at_stop = (expires_at - datetime.now(UTC)).total_seconds()
        assert remaining_at_stop >= 0.08
        assert events == ["native_stop_started"]
        assert host.store.released == []

        finish_handler.set()
        async with asyncio.timeout(0.5):
            while host.claim is not None:
                await asyncio.sleep(0.005)
        assert events == ["native_stop_started", "native_stop_confirmed", "store_release"]
    finally:
        finish_handler.set()
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_per_acquire_loss_handler_stops_native_capture_before_store_release(host):
    events: list[str] = []
    host.store.acquire_result = _expiring_claim(
        after_seconds=0.03,
        owner_kind="voice_enrollment",
        owner_id="sample-1",
    )
    host.store.release_hook = lambda _claim: events.append("store_release")

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        events.append("native_stop")
        await asyncio.sleep(0)

    owner = host.owner(ttl_seconds=0.03)
    await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )
    try:
        async with asyncio.timeout(0.5):
            while host.claim is not None:
                await asyncio.sleep(0.005)
        assert events == ["native_stop", "store_release"]
        assert host.losses == []
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_transfer_preserves_the_per_acquire_loss_handler(host):
    events: list[str] = []
    host.store.acquire_result = _claim(owner_kind="meeting", owner_id="pending-abc")
    host.store.release_hook = lambda claim: events.append(f"release:{claim.owner_id}")

    async def stop_native_capture(claim: AudioAdmissionClaim, reason: str) -> None:
        events.append(f"stop:{claim.owner_id}:{reason}")

    owner = host.owner()
    pending = await owner.acquire(
        owner_kind="meeting",
        owner_id="pending-abc",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )
    transferred = await owner.transfer(pending, owner_id="meeting-7")

    await owner.note_loss(transferred, reason="superseded")

    assert events == ["stop:meeting-7:superseded", "release:meeting-7"]
    assert host.claim is None


@pytest.mark.asyncio
async def test_loss_handler_is_retried_automatically_before_store_release(host):
    attempts = 0

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("native stop was not confirmed")

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner = host.owner()
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )

    await owner.note_loss(held, reason="lease_expired")
    assert attempts == 2
    assert host.claim is None
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_third_native_stop_attempt_waits_for_a_successful_lease_renewal(host):
    """Two bounded attempts fit the stop margin; later retries need a new lease."""

    attempts = 0

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            await asyncio.sleep(0.04)
            raise RuntimeError("native stop was not confirmed")
        assert host.store.renewals >= 1

    host.store.acquire_result = _expiring_claim(
        after_seconds=0.12,
        owner_kind="voice_enrollment",
        owner_id="sample-1",
    )
    owner = host.owner(
        ttl_seconds=0.12,
        heartbeat_seconds=0.5,
        stop_margin_seconds=0.10,
        loss_retry_seconds=0.001,
    )
    await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=True,
        loss_handler=stop_native_capture,
    )

    async with asyncio.timeout(1.0):
        while host.claim is not None:
            await asyncio.sleep(0.005)

    assert attempts == 3
    assert host.store.renewals == 1
    assert len(host.store.released) == 1
    assert await owner.close(task_drain_timeout_seconds=1.0) == 0


@pytest.mark.asyncio
async def test_exhausted_loss_handler_retries_retain_the_claim_without_store_release(host):
    attempts = 0

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("native stop was not confirmed")

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner = host.owner(loss_retry_attempts=2, loss_retry_seconds=0.001)
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )

    with pytest.raises(RuntimeError, match="not confirmed"):
        await owner.note_loss(held, reason="lease_expired")

    assert attempts == 2
    assert host.claim is held
    assert host.store.released == []


@pytest.mark.asyncio
async def test_explicit_release_cannot_bypass_an_unconfirmed_native_stop(host):
    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        raise RuntimeError("native stop was not confirmed")

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner = host.owner(loss_retry_attempts=1)
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )
    with pytest.raises(RuntimeError, match="not confirmed"):
        await owner.note_loss(held, reason="lease_expired")

    with pytest.raises(RuntimeError, match="stop.*confirm"):
        await owner.release(held)
    assert host.claim is held
    assert host.store.released == []


@pytest.mark.asyncio
async def test_loss_cancellation_waits_for_confirmed_native_stop_and_terminal_release(host):
    events: list[str] = []
    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        events.append("native_stop_started")
        handler_started.set()
        await finish_handler.wait()
        events.append("native_stop_confirmed")

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    host.store.release_hook = lambda _claim: events.append("store_release")
    owner = host.owner()
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )

    losing = asyncio.create_task(owner.note_loss(held, reason="superseded"))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    losing.cancel()
    await asyncio.sleep(0)
    assert losing.done() is False
    assert host.store.released == []

    finish_handler.set()
    with pytest.raises(asyncio.CancelledError):
        await losing
    assert events == ["native_stop_started", "native_stop_confirmed", "store_release"]
    assert host.claim is None


@pytest.mark.asyncio
async def test_loss_cancellation_wins_over_handler_failure_and_retains_retry_state(host):
    attempts = 0
    handler_started = asyncio.Event()
    finish_first_attempt = asyncio.Event()

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            handler_started.set()
            await finish_first_attempt.wait()
            raise RuntimeError("native stop was not confirmed")

    host.store.acquire_result = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    owner = host.owner(loss_retry_attempts=1)
    held = await owner.acquire(
        owner_kind="voice_enrollment",
        owner_id="sample-1",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )

    losing = asyncio.create_task(owner.note_loss(held, reason="superseded"))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    losing.cancel()
    finish_first_attempt.set()
    with pytest.raises(asyncio.CancelledError):
        await losing
    assert host.claim is held
    assert host.store.released == []

    await owner.note_loss(held, reason="superseded")
    assert attempts == 2
    assert host.claim is None
    assert host.store.released == [held]


@pytest.mark.asyncio
async def test_pending_meeting_without_explicit_durable_latch_fails_closed_at_ttl(host):
    host.store.acquire_result = _expiring_claim(
        after_seconds=0.04,
        owner_kind="meeting",
        owner_id="pending-abc",
    )
    host.store.renew_error = OSError("database is locked")
    owner = host.owner(ttl_seconds=0.04, heartbeat_seconds=0.001)

    held = await owner.acquire(owner_kind="meeting", owner_id="pending-abc")
    try:
        async with asyncio.timeout(0.5):
            while not host.losses:
                await asyncio.sleep(0.005)
        assert host.losses == [(held, "lease_expired")]
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_explicitly_durable_meeting_rides_out_store_unavailability(host):
    host.store.acquire_result = _expiring_claim(
        after_seconds=0.03,
        owner_kind="meeting",
        owner_id="meeting-7",
    )
    host.store.renew_error = OSError("database is locked")
    owner = host.owner(ttl_seconds=0.03, heartbeat_seconds=0.001)

    held = await owner.acquire(owner_kind="meeting", owner_id="meeting-7")
    assert await owner.mark_durable(held) is True
    try:
        await asyncio.sleep(0.09)
        assert host.store.renewals >= 3
        assert host.claim is held
        assert host.losses == []
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


@pytest.mark.asyncio
async def test_only_the_current_meeting_claim_can_be_marked_durable(host):
    owner = host.owner()
    live = _claim(owner_kind="live_mic", owner_id="session-1")
    host.claim = live

    with pytest.raises(ValueError, match="Meeting"):
        await owner.mark_durable(live)
    assert await owner.mark_durable(_claim(owner_kind="meeting", owner_id="meeting-old")) is False


@pytest.mark.asyncio
async def test_a_meeting_rides_out_an_unavailable_store(host):
    """Its durable row still excludes a newcomer, so it keeps trying."""
    owner = host.owner()
    host.claim = _claim(owner_kind="meeting", owner_id="meeting-7")
    host.store.renew_error = OSError("database is locked")
    assert await owner.mark_durable(host.claim) is True

    owner.start_heartbeat()
    try:
        for _ in range(200):
            if host.store.renewals >= 5:
                break
            await asyncio.sleep(0.005)
        assert host.store.renewals >= 5
        assert host.claim is not None
        assert host.lost_meetings == set()
    finally:
        await owner.close(task_drain_timeout_seconds=1.0)


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
async def test_transfer_cannot_cross_the_close_lifecycle_admission_seal(host, monkeypatch):
    close_reached_loss = asyncio.Event()
    continue_close = asyncio.Event()
    host.store.acquire_result = _claim(owner_kind="meeting", owner_id="pending-abc")
    owner = host.owner()
    pending = await owner.acquire(owner_kind="meeting", owner_id="pending-abc", heartbeat=False)
    note_loss = owner.note_loss

    async def pause_before_loss(claim: AudioAdmissionClaim, *, reason: str) -> None:
        close_reached_loss.set()
        await continue_close.wait()
        await note_loss(claim, reason=reason)

    monkeypatch.setattr(owner, "note_loss", pause_before_loss)
    closing = asyncio.create_task(owner.close(task_drain_timeout_seconds=1.0))
    await asyncio.wait_for(close_reached_loss.wait(), timeout=1.0)

    transfer_outcome = (
        await asyncio.gather(
            owner.transfer(pending, owner_id="meeting-7"),
            return_exceptions=True,
        )
    )[0]
    continue_close.set()
    close_result = await closing
    claim_after_close = host.claim
    released_after_close = list(host.store.released)
    if host.claim is not None:
        await owner.release(host.claim)

    assert isinstance(transfer_outcome, RuntimeError)
    assert "closed" in str(transfer_outcome).lower()
    assert close_result == 0
    assert host.store.transfers == []
    assert claim_after_close is None
    assert released_after_close == [pending]


@pytest.mark.asyncio
async def test_release_waits_for_transfer_and_releases_only_the_committed_generation(host):
    store = _BlockingTransferStore()
    host.store = store
    pending = _claim(owner_kind="meeting", owner_id="pending-abc")
    host.claim = pending
    owner = host.owner()

    transferring = asyncio.create_task(owner.transfer(pending, owner_id="meeting-7"))
    assert await asyncio.wait_for(asyncio.to_thread(store.transfer_started.wait), timeout=1.0) is True
    releasing = asyncio.create_task(owner.release())
    await asyncio.sleep(0)
    assert releasing.done() is False
    assert store.released == []

    store.finish_transfer.set()
    transferred = await transferring
    assert await releasing is True
    assert transferred.owner_id == "meeting-7"
    assert store.released == [transferred]
    assert host.claim is None


@pytest.mark.asyncio
async def test_transfer_cannot_create_v2_while_loss_handler_owns_the_claim(host):
    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()

    async def stop_native_capture(_claim: AudioAdmissionClaim, _reason: str) -> None:
        handler_started.set()
        await finish_handler.wait()

    host.store.acquire_result = _claim(owner_kind="meeting", owner_id="pending-abc")
    owner = host.owner()
    pending = await owner.acquire(
        owner_kind="meeting",
        owner_id="pending-abc",
        heartbeat=False,
        loss_handler=stop_native_capture,
    )
    losing = asyncio.create_task(owner.note_loss(pending, reason="superseded"))
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)
    transferring = asyncio.create_task(owner.transfer(pending, owner_id="meeting-7"))
    await asyncio.sleep(0)

    assert transferring.done() is False
    assert host.store.transfers == []
    finish_handler.set()
    await losing
    with pytest.raises(RuntimeError, match="no longer held"):
        await transferring
    assert host.store.transfers == []
    assert host.claim is None


@pytest.mark.asyncio
async def test_canceled_transfer_releases_the_committed_generation_before_unwinding(host):
    store = _BlockingTransferStore()
    host.store = store
    pending = _claim(owner_kind="meeting", owner_id="pending-abc")
    host.claim = pending
    owner = host.owner()

    task = asyncio.create_task(owner.transfer(pending, owner_id="meeting-7"))
    assert await asyncio.wait_for(asyncio.to_thread(store.transfer_started.wait), timeout=1.0) is True
    task.cancel()
    store.finish_transfer.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    transferred = _claim(
        owner_kind="meeting",
        owner_id="meeting-7",
        controller_id=pending.controller_id,
        state_version=pending.state_version + 1,
    )
    assert host.claim is None
    assert store.released == [transferred]


@pytest.mark.asyncio
async def test_canceled_transfer_retains_the_committed_generation_when_rollback_fails(host):
    store = _BlockingTransferFlakyReleaseStore()
    host.store = store
    pending = _claim(owner_kind="meeting", owner_id="pending-abc")
    host.claim = pending
    owner = host.owner()

    task = asyncio.create_task(owner.transfer(pending, owner_id="meeting-7"))
    assert await asyncio.wait_for(asyncio.to_thread(store.transfer_started.wait), timeout=1.0) is True
    task.cancel()
    store.finish_transfer.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    transferred = _claim(
        owner_kind="meeting",
        owner_id="meeting-7",
        controller_id=pending.controller_id,
        state_version=pending.state_version + 1,
    )
    assert host.claim == transferred
    assert await owner.release() is True
    assert host.claim is None
    assert store.release_attempts == 2
    assert store.released == [transferred]


@pytest.mark.asyncio
async def test_transfer_of_a_lease_we_no_longer_hold_is_rejected_before_the_store(host):
    owner = host.owner()
    current = _claim(owner_kind="live_mic", owner_id="session-2")
    host.claim = current
    stale = _claim(owner_kind="meeting", owner_id="pending-abc")

    with pytest.raises(AudioAdmissionConflict) as caught:
        await owner.transfer(stale, owner_id="meeting-7")

    assert caught.value.active is current
    assert host.claim is current
    assert host.store.transfers == []


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


@pytest.mark.asyncio
async def test_loss_is_reported_through_one_generic_async_handler(host):
    losses: list[tuple[AudioAdmissionClaim, str]] = []

    async def record_loss(claim: AudioAdmissionClaim, reason: str) -> None:
        losses.append((claim, reason))

    owner = AudioAdmissionOwner(
        resolve_admission=lambda: (host.store, "controller-a"),
        get_claim=lambda: host.claim,
        set_claim=host._set_claim,
        is_shutting_down=lambda: host.shutting_down,
        loss_handler=record_loss,
    )
    held = _claim(owner_kind="voice_enrollment", owner_id="sample-1")
    host.claim = held

    await owner.note_loss(held, reason="lease_expired")

    assert host.claim is None
    assert losses == [(held, "lease_expired")]
