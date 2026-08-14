"""Ownership of the one native-audio lease this process may hold.

Only one capture can own the native audio device at a time, and the exclusion
has to survive across processes: a second Scriber window, or a stale process
that has not exited yet, must not be able to start a capture while this one is
recording. That is what the durable lease in :class:`AudioAdmissionStore`
provides, and this module owns the rules for holding one.

Those rules are not obvious, and every one of them exists because of a way the
lease can be lost:

* A lease expires. It must be renewed on a heartbeat, and the heartbeat has to
  outlive individual requests without ever running twice.
* A renewal can lose a CAS race with this controller's own pending-to-durable
  Meeting rebinding. That is not a loss -- the newer claim is adopted and the
  heartbeat continues.
* A renewal can lose to a genuinely different controller. That *is* a loss, and
  it must fail closed rather than leave two captures believing they own the
  device.
* Renewal can simply be unavailable. A Meeting has a durable row that still
  excludes a newcomer, so it can ride out the outage; Live Mic has nothing of
  the kind, so it stops before the TTL can lapse.
* An acquisition already running in a worker thread cannot be cancelled. If
  shutdown or cancellation wins that race, the lease that thread just created
  has to be released rather than left behind as a phantom owner for a full TTL.

This sits beside :mod:`src.runtime.task_supervisor` and
:mod:`src.runtime.cancellation`: those own the lifetime of work, this owns the
lifetime of an exclusive resource.

**Where the state lives.** The claim and the heartbeat task are still stored on
the controller, reached through the accessors passed to the constructor. The
suite that guards this concern reads and writes those attributes directly, and
relocating the state in the same change that relocates the rules would remove
the check that proves the move was faithful. The owner therefore owns the rules
and the lifecycle now; the storage can follow once the rules have a test of
their own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from src.data.audio_admission_store import (
    AudioAdmissionClaim,
    AudioAdmissionConflict,
    AudioAdmissionStore,
)
from src.runtime.cancellation import await_with_delayed_cancellation

DEFAULT_TTL_SECONDS = 60.0
DEFAULT_HEARTBEAT_SECONDS = 15.0
_LIVE_MIC_RENEWAL_FAILURE_LIMIT = 3


def same_claim(left: AudioAdmissionClaim | None, right: AudioAdmissionClaim) -> bool:
    """Is ``left`` the very same lease generation as ``right``?

    Identity here is the CAS tuple, not object identity: a renewed claim is a
    new object describing the same ownership, while a rebinding produces a new
    ``state_version`` and is deliberately *not* the same lease.
    """
    return bool(
        left is not None
        and left.owner_kind == right.owner_kind
        and left.owner_id == right.owner_id
        and left.controller_id == right.controller_id
        and left.state_version == right.state_version
    )


class AudioAdmissionOwner:
    """The single holder of this process's native-audio lease."""

    def __init__(
        self,
        *,
        resolve_admission: Callable[[], tuple[AudioAdmissionStore, str]],
        get_lost_meetings: Callable[[], set[str]],
        get_claim: Callable[[], AudioAdmissionClaim | None],
        set_claim: Callable[[AudioAdmissionClaim | None], None],
        get_heartbeat: Callable[[], asyncio.Task | None],
        set_heartbeat: Callable[[asyncio.Task | None], None],
        is_shutting_down: Callable[[], bool],
        on_live_mic_lost: Callable[..., Awaitable[Any]] | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        self._resolve_admission = resolve_admission
        self._get_lost_meetings = get_lost_meetings
        self._get_claim = get_claim
        self._set_claim = set_claim
        self._get_heartbeat = get_heartbeat
        self._set_heartbeat = set_heartbeat
        self._is_shutting_down = is_shutting_down
        self._on_live_mic_lost = on_live_mic_lost
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds

    @property
    def store(self) -> AudioAdmissionStore:
        """The durable lease store, resolved on every use.

        Resolved rather than captured because composition can substitute it
        after this owner exists -- a focused test swaps in a blocking store to
        drive the acquisition race, and an owner holding a stale reference would
        quietly bypass it.
        """
        return self._resolve_admission()[0]

    @property
    def controller_id(self) -> str:
        return self._resolve_admission()[1]

    @property
    def lost_meetings(self) -> set[str]:
        return self._get_lost_meetings()

    @property
    def current(self) -> AudioAdmissionClaim | None:
        claim = self._get_claim()
        return claim if isinstance(claim, AudioAdmissionClaim) else None

    def meeting_claim(self, meeting_id: str) -> AudioAdmissionClaim | None:
        """Return the held claim only when this exact Meeting owns it."""
        current = self.current
        if current is not None and current.owner_kind == "meeting" and current.owner_id == str(meeting_id or ""):
            return current
        return None

    async def acquire(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        heartbeat: bool = True,
    ) -> AudioAdmissionClaim:
        """Take the lease, or raise :class:`AudioAdmissionConflict`.

        Re-requesting a lease this process already holds for the same owner is
        idempotent; requesting one while a *different* owner holds it conflicts
        without touching the store.
        """
        current = self.current
        if current is not None:
            if current.owner_kind == owner_kind and current.owner_id == owner_id:
                return current
            raise AudioAdmissionConflict(current)

        store, controller_id = self._resolve_admission()
        claim, pending_cancel = await await_with_delayed_cancellation(
            asyncio.to_thread(
                store.acquire,
                owner_kind=owner_kind,
                owner_id=owner_id,
                controller_id=controller_id,
                ttl_seconds=self.ttl_seconds,
            )
        )
        # A SQLite acquisition already running in a worker thread cannot be
        # cancelled. Never lose the returned ownership record: if shutdown or
        # task cancellation won the race, release the newly-created lease
        # before propagating cancellation instead of leaving a phantom owner
        # for a full TTL.
        if pending_cancel is not None or self._is_shutting_down():
            await self._roll_back(store, claim)
            if pending_cancel is not None:
                raise pending_cancel
            raise asyncio.CancelledError("Native audio claim aborted during shutdown")

        self._set_claim(claim)
        self.lost_meetings.discard(owner_id)
        if heartbeat:
            self.start_heartbeat()
        return claim

    async def _roll_back(self, store: AudioAdmissionStore, claim: AudioAdmissionClaim) -> None:
        try:
            _released, pending = await await_with_delayed_cancellation(asyncio.to_thread(store.release, claim))
            if pending is not None:
                raise pending
        except BaseException as cleanup_exc:
            logger.warning("Persistent native-audio claim rollback failed: {}", type(cleanup_exc).__name__)

    async def transfer(self, claim: AudioAdmissionClaim, *, owner_id: str) -> AudioAdmissionClaim:
        """Rebind a held lease to its durable owner id.

        This bumps the CAS generation on purpose, which is why an in-flight
        renewal losing to it is adopted rather than treated as a loss.
        """
        transferred = await asyncio.to_thread(self.store.transfer, claim, owner_id=owner_id)
        if same_claim(self.current, claim):
            self._set_claim(transferred)
        return transferred

    async def release(self, claim: AudioAdmissionClaim | None = None) -> bool:
        """Give up a lease, stopping the heartbeat if it was the held one."""
        target = claim if isinstance(claim, AudioAdmissionClaim) else self.current
        if target is None:
            return False
        if same_claim(self.current, target):
            self._set_claim(None)
            self.stop_heartbeat()
        released, pending_cancel = await await_with_delayed_cancellation(asyncio.to_thread(self.store.release, target))
        if pending_cancel is not None:
            raise pending_cancel
        return bool(released)

    async def foreign_claim(self) -> AudioAdmissionClaim | None:
        """Return the active lease when another controller holds it."""
        active = await asyncio.to_thread(self.store.active)
        if active is None or active.controller_id == self.controller_id:
            return None
        return active

    def start_heartbeat(self) -> None:
        """Ensure exactly one renewal loop is running."""
        task = self._get_heartbeat()
        if task is None or task.done():
            self._set_heartbeat(asyncio.create_task(self.run_heartbeat(), name="audio_admission_heartbeat"))

    def stop_heartbeat(self) -> None:
        """Cancel the renewal loop, unless it is the caller."""
        task = self._get_heartbeat()
        self._set_heartbeat(None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def note_loss(self, claim: AudioAdmissionClaim, *, reason: str) -> None:
        """Fail closed when another controller has superseded our lease."""
        if not same_claim(self.current, claim):
            return
        self._set_claim(None)
        logger.error("Persistent native-audio admission lost: owner={} reason={}", claim.owner_kind, reason)
        if claim.owner_kind == "live_mic":
            if self._on_live_mic_lost is not None:
                await self._on_live_mic_lost(session_id=claim.owner_id)
            return
        if claim.owner_kind == "meeting" and not claim.owner_id.startswith("pending-"):
            # A pending id never reached a durable Meeting row, so there is
            # nothing a later request could ask about.
            self.lost_meetings.add(claim.owner_id)

    async def run_heartbeat(self) -> None:
        """Renew the held lease until it is released, lost, or cancelled."""
        consecutive_errors = 0
        try:
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                claim = self.current
                if claim is None:
                    return
                try:
                    renewed = await asyncio.to_thread(self.store.renew, claim, ttl_seconds=self.ttl_seconds)
                except AudioAdmissionConflict as exc:
                    if self._adopt_rebinding(claim, exc.active):
                        consecutive_errors = 0
                        continue
                    await self.note_loss(claim, reason="superseded")
                    return
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "Persistent native-audio admission heartbeat retry: error={} attempt={}",
                        type(exc).__name__,
                        consecutive_errors,
                    )
                    # Live Mic has no durable workflow row that can exclude a
                    # new controller after lease expiry. Stop before the TTL can
                    # lapse rather than risk two simultaneous captures.
                    if claim.owner_kind == "live_mic" and consecutive_errors >= _LIVE_MIC_RENEWAL_FAILURE_LIMIT:
                        await self.note_loss(claim, reason="renewal_unavailable")
                        return
                    continue
                consecutive_errors = 0
                if same_claim(self.current, claim):
                    self._set_claim(renewed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Persistent native-audio admission heartbeat stopped: {}", type(exc).__name__)

    def _adopt_rebinding(self, claim: AudioAdmissionClaim, active: AudioAdmissionClaim) -> bool:
        """Was the conflict our own pending-to-durable Meeting rebinding?

        That transfer intentionally increments the CAS generation. If it wins
        the SQLite race with an in-flight renewal, the newer claim is ours and
        the heartbeat keeps beating instead of failing closed.
        """
        if not same_claim(self.current, claim):
            return False
        if active.controller_id != claim.controller_id or active.owner_kind != claim.owner_kind:
            return False
        if active.state_version <= claim.state_version:
            return False
        self._set_claim(active)
        return True


def release_claim_in_thread(store: Any, claim: AudioAdmissionClaim) -> None:
    """No-loop fallback for synchronous teardown callers."""
    try:
        store.release(claim)
    except Exception as exc:
        logger.warning(
            "Persistent native-audio admission release during shutdown failed: {}",
            type(exc).__name__,
        )
