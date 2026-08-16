"""Own the one cross-process native-audio lease held by this process.

The durable SQLite claim is a single exclusive resource. This module keeps all
ownership-changing operations behind one serialized interface, retains a claim
until its store release reaches a terminal result, and supervises renewal and
expiry work that must outlive individual HTTP requests.

The store's returned UTC expiry is authoritative. The owner converts it to an
event-loop monotonic deadline when it observes a claim; it never grants a fresh
``ttl_seconds`` merely because a blocked store call returned late. A separate
watchdog enforces that deadline even when renewal is disabled or stuck.

Meetings may ride out a store outage only after composition explicitly calls
:meth:`AudioAdmissionOwner.mark_durable` for a current Meeting claim. No owner
name or identifier convention implies durability.

The claim remains in controller storage through constructor accessors. That is
the deliberate migration seam. Lease policy, tasks, loss handlers, and retry
state belong only to this owner.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from src.data.audio_admission_store import (
    AudioAdmissionClaim,
    AudioAdmissionConflict,
    AudioAdmissionStore,
)
from src.runtime.cancellation import await_with_delayed_cancellation
from src.runtime.task_supervisor import AsyncTaskSupervisor

DEFAULT_TTL_SECONDS = 60.0
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_STOP_MARGIN_SECONDS = 10.0
DEFAULT_LOSS_RETRY_ATTEMPTS = 2
DEFAULT_LOSS_RETRY_SECONDS = 0.25
_WATCHDOG_MAX_SLEEP_SECONDS = 1.0

AudioAdmissionLossHandler = Callable[[AudioAdmissionClaim, str], Awaitable[None]]
_ClaimKey = tuple[str, str, str, int]


def same_claim(left: AudioAdmissionClaim | None, right: AudioAdmissionClaim) -> bool:
    """Return whether two values describe the same durable CAS generation."""

    return bool(
        left is not None
        and left.owner_kind == right.owner_kind
        and left.owner_id == right.owner_id
        and left.controller_id == right.controller_id
        and left.state_version == right.state_version
    )


def _claim_key(claim: AudioAdmissionClaim) -> _ClaimKey:
    return (
        claim.owner_kind,
        claim.owner_id,
        claim.controller_id,
        claim.state_version,
    )


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _LeaseSlot:
    key: _ClaimKey
    expires_deadline: float
    stop_deadline: float
    heartbeat_enabled: bool
    loss_handler: AudioAdmissionLossHandler | None
    durable: bool = False
    loss_pending: bool = False
    loss_in_progress: bool = False
    loss_handler_completed: bool = False
    loss_barrier: asyncio.Future[None] | None = None
    loss_error: BaseException | None = None
    loss_reason: str | None = None
    released: bool = False


class AudioAdmissionOwner:
    """Serialized lifecycle owner for one native-audio lease."""

    def __init__(
        self,
        *,
        resolve_admission: Callable[[], tuple[AudioAdmissionStore, str]],
        get_claim: Callable[[], AudioAdmissionClaim | None],
        set_claim: Callable[[AudioAdmissionClaim | None], None],
        is_shutting_down: Callable[[], bool],
        loss_handler: AudioAdmissionLossHandler | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        stop_margin_seconds: float = DEFAULT_STOP_MARGIN_SECONDS,
        loss_retry_attempts: int = DEFAULT_LOSS_RETRY_ATTEMPTS,
        loss_retry_seconds: float = DEFAULT_LOSS_RETRY_SECONDS,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._resolve_admission = resolve_admission
        self._get_claim = get_claim
        self._set_claim = set_claim
        self._is_shutting_down = is_shutting_down
        self._default_loss_handler = loss_handler
        self._utc_now = utc_now
        self.ttl_seconds = float(ttl_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.stop_margin_seconds = max(0.0, float(stop_margin_seconds))
        self.loss_retry_attempts = max(1, int(loss_retry_attempts))
        self.loss_retry_seconds = max(0.0, float(loss_retry_seconds))

        self._lifecycle_lock = asyncio.Lock()
        self._lease_tasks = AsyncTaskSupervisor(owner="native-audio admission lease")
        self._slot: _LeaseSlot | None = None
        self._heartbeat_task: asyncio.Future[Any] | None = None
        self._heartbeat_slot: _LeaseSlot | None = None
        self._watchdog_task: asyncio.Future[Any] | None = None
        self._watchdog_slot: _LeaseSlot | None = None

    @property
    def store(self) -> AudioAdmissionStore:
        """Resolve the store on every use so composition can substitute it."""

        return self._resolve_admission()[0]

    @property
    def controller_id(self) -> str:
        return self._resolve_admission()[1]

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

    def _deadlines_for(self, claim: AudioAdmissionClaim) -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        expires_at = _parse_utc(claim.lease_expires_at)
        if expires_at is None:
            monotonic_now = loop.time()
            return monotonic_now, monotonic_now
        utc_now = self._utc_now()
        if utc_now.tzinfo is None:
            utc_now = utc_now.replace(tzinfo=UTC)
        remaining = (expires_at - utc_now.astimezone(UTC)).total_seconds()
        expires_deadline = loop.time() + max(0.0, remaining)
        stop_deadline = max(loop.time(), expires_deadline - self.stop_margin_seconds)
        return expires_deadline, stop_deadline

    def _ensure_slot(
        self,
        claim: AudioAdmissionClaim,
        *,
        heartbeat_enabled: bool,
        loss_handler: AudioAdmissionLossHandler | None = None,
    ) -> _LeaseSlot:
        key = _claim_key(claim)
        slot = self._slot
        if slot is not None and slot.key == key:
            slot.heartbeat_enabled = slot.heartbeat_enabled or heartbeat_enabled
            if (
                loss_handler is not None
                and not slot.loss_pending
                and not slot.loss_in_progress
                and not slot.loss_handler_completed
            ):
                slot.loss_handler = loss_handler
            return slot
        expires_deadline, stop_deadline = self._deadlines_for(claim)
        slot = _LeaseSlot(
            key=key,
            expires_deadline=expires_deadline,
            stop_deadline=stop_deadline,
            heartbeat_enabled=heartbeat_enabled,
            loss_handler=loss_handler if loss_handler is not None else self._default_loss_handler,
        )
        self._slot = slot
        return slot

    def _active_loss_barrier(self) -> asyncio.Future[None] | None:
        slot = self._slot
        if slot is None or not slot.loss_in_progress:
            return None
        barrier = slot.loss_barrier
        if barrier is None or barrier.done():
            return None
        return barrier

    async def _wait_for_active_loss(self) -> None:
        while True:
            barrier = self._active_loss_barrier()
            if barrier is None:
                return
            await asyncio.shield(barrier)

    async def _snapshot_after_lifecycle_barrier(self) -> AudioAdmissionClaim | None:
        async with self._lifecycle_phase():
            return self.current

    @asynccontextmanager
    async def _lifecycle_phase(self) -> AsyncIterator[None]:
        """Enter serialized mutation only after an active loss phase settles."""

        while True:
            await self._wait_for_active_loss()
            await self._lifecycle_lock.acquire()
            barrier = self._active_loss_barrier()
            if barrier is not None:
                self._lifecycle_lock.release()
                await asyncio.shield(barrier)
                continue
            try:
                yield
            finally:
                self._lifecycle_lock.release()
            return

    def _spawn_watchdog(self, slot: _LeaseSlot) -> bool:
        if self._lease_tasks.sealed or slot.durable or slot.loss_pending:
            return False
        existing = self._watchdog_task
        if existing is not None and not existing.done() and self._watchdog_slot is slot:
            return False
        task = self._lease_tasks.spawn(
            self._run_watchdog(slot),
            name="audio_admission_watchdog",
        )
        if task is None:
            return False
        self._watchdog_task = task
        self._watchdog_slot = slot
        return True

    def _start_slot_tasks(self, slot: _LeaseSlot) -> None:
        self._spawn_watchdog(slot)
        if slot.heartbeat_enabled:
            self.start_heartbeat()

    def _cancel_task_for_slot(self, *, kind: str, slot: _LeaseSlot) -> None:
        current_task = asyncio.current_task()
        if kind == "heartbeat":
            task = self._heartbeat_task
            task_slot = self._heartbeat_slot
        else:
            task = self._watchdog_task
            task_slot = self._watchdog_slot
        if task is None or task.done() or task_slot is not slot:
            return
        if task is current_task:
            return
        task.cancel()
        if kind == "heartbeat":
            self._heartbeat_task = None
            self._heartbeat_slot = None
        else:
            self._watchdog_task = None
            self._watchdog_slot = None

    def _cancel_slot_tasks(self, slot: _LeaseSlot) -> None:
        self._cancel_task_for_slot(kind="heartbeat", slot=slot)
        self._cancel_task_for_slot(kind="watchdog", slot=slot)

    def _native_stop_is_unconfirmed(self, expected_key: _ClaimKey) -> bool:
        slot = self._slot
        current = self.current
        return bool(
            not self._lease_tasks.sealed
            and current is not None
            and _claim_key(current) == expected_key
            and slot is not None
            and slot.key == expected_key
            and slot.heartbeat_enabled
            and slot.loss_pending
            and slot.loss_error is not None
            and not slot.loss_handler_completed
        )

    def _forget_terminal(self, claim: AudioAdmissionClaim, slot: _LeaseSlot) -> None:
        slot.released = True
        if self._slot is slot:
            if same_claim(self.current, claim):
                self._set_claim(None)
            self._slot = None
        self._cancel_slot_tasks(slot)

    async def _store_release(
        self,
        store: AudioAdmissionStore,
        claim: AudioAdmissionClaim,
    ) -> tuple[bool, asyncio.CancelledError | None]:
        released, pending_cancel = await await_with_delayed_cancellation(asyncio.to_thread(store.release, claim))
        return bool(released), pending_cancel

    async def acquire(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        heartbeat: bool = True,
        loss_handler: AudioAdmissionLossHandler | None = None,
    ) -> AudioAdmissionClaim:
        """Acquire one lease and begin its independent expiry supervision."""

        if self._lease_tasks.sealed:
            raise RuntimeError("Native-audio admission owner is closed")
        async with self._lifecycle_phase():
            if self._lease_tasks.sealed:
                raise RuntimeError("Native-audio admission owner is closed")
            current = self.current
            if current is not None:
                if current.owner_kind != owner_kind or current.owner_id != owner_id:
                    raise AudioAdmissionConflict(current)
                if self._slot is not None and self._slot.key == _claim_key(current) and self._slot.loss_pending:
                    raise RuntimeError("Native-audio claim is settling a loss")
                slot = self._ensure_slot(
                    current,
                    heartbeat_enabled=heartbeat,
                    loss_handler=loss_handler,
                )
                self._start_slot_tasks(slot)
                return current

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
            abort = pending_cancel
            if abort is None and (self._lease_tasks.sealed or self._is_shutting_down()):
                abort = asyncio.CancelledError("Native audio claim aborted during shutdown")

            self._set_claim(claim)
            slot = self._ensure_slot(
                claim,
                heartbeat_enabled=heartbeat,
                loss_handler=loss_handler,
            )
            if abort is not None:
                try:
                    _released, release_cancel = await self._store_release(store, claim)
                except BaseException:
                    self._start_slot_tasks(slot)
                    logger.exception("Native-audio acquisition rollback failed; claim retained for retry")
                    raise abort from None
                self._forget_terminal(claim, slot)
                if release_cancel is not None:
                    raise abort from None
                raise abort

            self._start_slot_tasks(slot)
            return claim

    async def transfer(self, claim: AudioAdmissionClaim, *, owner_id: str) -> AudioAdmissionClaim:
        """Rebind the current claim while retaining its policy and handler."""

        if self._lease_tasks.sealed:
            raise RuntimeError("Native-audio admission owner is closed")
        async with self._lifecycle_phase():
            if self._lease_tasks.sealed:
                raise RuntimeError("Native-audio admission owner is closed")
            current = self.current
            if current is None:
                raise RuntimeError("Native-audio claim is no longer held")
            if not same_claim(current, claim):
                raise AudioAdmissionConflict(current)
            slot = self._ensure_slot(claim, heartbeat_enabled=False)
            if slot.loss_pending:
                raise RuntimeError("Native-audio claim is settling a loss")
            store = self.store
            transferred, pending_cancel = await await_with_delayed_cancellation(
                asyncio.to_thread(store.transfer, claim, owner_id=owner_id)
            )

            latest = self.current
            if same_claim(latest, claim) or same_claim(latest, transferred):
                self._set_claim(transferred)
                slot.key = _claim_key(transferred)
                self._cancel_slot_tasks(slot)
            else:
                try:
                    await self._store_release(store, transferred)
                except BaseException:
                    logger.exception("Unowned transferred claim cleanup failed")
                if pending_cancel is not None:
                    raise pending_cancel
                raise RuntimeError("Native-audio claim was lost during transfer")

            if pending_cancel is None:
                self._start_slot_tasks(slot)
                return transferred

            try:
                _released, rollback_cancel = await self._store_release(store, transferred)
            except BaseException:
                self._start_slot_tasks(slot)
                logger.exception("Transferred claim rollback failed; v2 retained for retry")
                raise pending_cancel from None
            self._forget_terminal(transferred, slot)
            if rollback_cancel is not None:
                raise pending_cancel from None
            raise pending_cancel

    async def mark_durable(self, claim: AudioAdmissionClaim) -> bool:
        """Latch a confirmed durable Meeting row without another store CAS."""

        if claim.owner_kind != "meeting":
            raise ValueError("Only a Meeting claim can be marked durable")
        async with self._lifecycle_phase():
            if not same_claim(self.current, claim):
                return False
            slot = self._ensure_slot(claim, heartbeat_enabled=False)
            if slot.loss_pending:
                return False
            slot.durable = True
            self._cancel_task_for_slot(kind="watchdog", slot=slot)
            return True

    async def release(self, claim: AudioAdmissionClaim | None = None) -> bool:
        """Release a claim once; retain it when the store mutation fails."""

        async with self._lifecycle_phase():
            current = self.current
            target = claim if isinstance(claim, AudioAdmissionClaim) else current
            if target is None:
                return False
            held = same_claim(current, target)
            if not held:
                return False
            slot = self._slot
            if (
                slot is not None
                and slot.key == _claim_key(target)
                and slot.loss_pending
                and not slot.loss_handler_completed
            ):
                raise RuntimeError("Native-audio stop is not confirmed; claim retained")
            released, pending_cancel = await self._store_release(self.store, target)
            if slot is not None and self._slot is slot and same_claim(self.current, target):
                self._forget_terminal(target, slot)
            elif same_claim(self.current, target):
                self._set_claim(None)
            if pending_cancel is not None:
                raise pending_cancel
            return released

    async def foreign_claim(self) -> AudioAdmissionClaim | None:
        """Return the active lease only when another controller owns it."""

        active = await asyncio.to_thread(self.store.active)
        if active is None or active.controller_id == self.controller_id:
            return None
        return active

    def start_heartbeat(self) -> bool:
        """Start one renewal loop for the current claim, if not already owned."""

        if self._lease_tasks.sealed:
            return False
        current = self.current
        slot = self._ensure_slot(current, heartbeat_enabled=True) if current is not None else None
        existing = self._heartbeat_task
        if existing is not None and not existing.done() and self._heartbeat_slot is slot:
            return False
        if slot is not None:
            self._spawn_watchdog(slot)
        task = self._lease_tasks.spawn(
            self.run_heartbeat(),
            name="audio_admission_heartbeat",
        )
        if task is None:
            return False
        self._heartbeat_task = task
        self._heartbeat_slot = slot
        return True

    async def close(self, *, task_drain_timeout_seconds: float) -> int:
        """Seal safely, then bound only the final supervisor-task drain.

        Lifecycle mutations, native-stop confirmation, and durable store
        release are ownership barriers and intentionally have no timeout.
        """

        self._lease_tasks.seal()
        loss_error: BaseException | None = None
        target, pending_cancel = await await_with_delayed_cancellation(self._snapshot_after_lifecycle_barrier())
        if target is not None:
            try:
                await self.note_loss(target, reason="shutdown")
            except asyncio.CancelledError as exc:
                pending_cancel = exc
            except BaseException as exc:
                loss_error = exc

        try:
            pending_tasks, task_cancel = await await_with_delayed_cancellation(
                self._lease_tasks.close(
                    timeout_seconds=task_drain_timeout_seconds,
                    cancel=True,
                )
            )
        except asyncio.CancelledError as exc:
            pending_cancel = pending_cancel or exc
            pending_tasks = self._lease_tasks.pending_count
        else:
            pending_cancel = pending_cancel or task_cancel

        if pending_cancel is not None:
            if loss_error is not None:
                logger.error(
                    "Native-audio close failed while caller was canceling: {}",
                    type(loss_error).__name__,
                )
            raise pending_cancel
        if loss_error is not None:
            raise loss_error
        return int(pending_tasks)

    async def note_loss(self, claim: AudioAdmissionClaim, *, reason: str) -> None:
        """Stop native ownership first, then remove the durable lease."""

        _acquired, pending_cancel = await await_with_delayed_cancellation(self._lifecycle_lock.acquire())
        try:
            current = self.current
            if not same_claim(current, claim):
                if pending_cancel is not None:
                    raise pending_cancel
                return
            assert current is not None
            slot = self._ensure_slot(current, heartbeat_enabled=False)
            barrier = self._active_loss_barrier()
            leader = barrier is None
            if leader:
                if slot.loss_reason is None:
                    slot.loss_reason = reason
                slot.loss_pending = True
                slot.loss_in_progress = True
                slot.loss_error = None
                barrier = asyncio.get_running_loop().create_future()
                slot.loss_barrier = barrier
                self._cancel_slot_tasks(slot)
        finally:
            self._lifecycle_lock.release()

        if not leader:
            assert barrier is not None
            _result, join_cancel = await await_with_delayed_cancellation(asyncio.shield(barrier))
            pending_cancel = pending_cancel or join_cancel
            if pending_cancel is not None:
                raise pending_cancel
            if slot.loss_error is not None:
                raise slot.loss_error
            return

        assert barrier is not None
        loss_reason = slot.loss_reason or reason
        logger.error(
            "Persistent native-audio admission lost: owner={} reason={}",
            claim.owner_kind,
            loss_reason,
        )
        error: BaseException | None = None
        try:
            if not slot.loss_handler_completed and slot.loss_handler is not None:
                for attempt in range(1, self.loss_retry_attempts + 1):
                    try:
                        _result, handler_cancel = await await_with_delayed_cancellation(
                            slot.loss_handler(claim, loss_reason)
                        )
                    except Exception:
                        if attempt >= self.loss_retry_attempts:
                            raise
                        retry_delay = min(
                            self.loss_retry_seconds,
                            max(0.0, slot.expires_deadline - asyncio.get_running_loop().time()),
                        )
                        if retry_delay > 0.0:
                            _result, retry_cancel = await await_with_delayed_cancellation(asyncio.sleep(retry_delay))
                            pending_cancel = pending_cancel or retry_cancel
                    else:
                        pending_cancel = pending_cancel or handler_cancel
                        slot.loss_handler_completed = True
                        break
            else:
                slot.loss_handler_completed = True

            target = self.current
            if target is not None and _claim_key(target) == slot.key:
                _released, release_cancel = await self._store_release(self.store, target)
                pending_cancel = pending_cancel or release_cancel
                self._forget_terminal(target, slot)
        except BaseException as exc:
            error = pending_cancel or exc
        finally:
            slot.loss_in_progress = False
            slot.loss_error = error
            if not barrier.done():
                barrier.set_result(None)
            if (
                error is not None
                and not slot.loss_handler_completed
                and slot.heartbeat_enabled
                and not self._lease_tasks.sealed
            ):
                self.start_heartbeat()

        if error is not None:
            raise error
        if pending_cancel is not None:
            raise pending_cancel

    async def _run_watchdog(self, slot: _LeaseSlot) -> None:
        while True:
            current = self.current
            if current is None or self._slot is not slot or _claim_key(current) != slot.key:
                return
            if slot.durable or slot.loss_in_progress or slot.loss_handler_completed:
                return
            remaining = slot.stop_deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(min(remaining, _WATCHDOG_MAX_SLEEP_SECONDS))
                continue
            await self.note_loss(current, reason="lease_expired")
            return

    async def run_heartbeat(self) -> None:
        """Renew the current claim while an independent watchdog guards TTL."""

        current = self.current
        if current is None:
            return
        slot = self._ensure_slot(current, heartbeat_enabled=True)
        self._spawn_watchdog(slot)
        expected_key = slot.key
        consecutive_errors = 0
        renew_immediately = bool(slot.loss_pending and slot.loss_error is not None and not slot.loss_handler_completed)
        try:
            while True:
                if renew_immediately:
                    renew_immediately = False
                else:
                    await asyncio.sleep(self.heartbeat_seconds)
                claim = self.current
                current_slot = self._slot
                if (
                    claim is None
                    or current_slot is not slot
                    or _claim_key(claim) != expected_key
                    or current_slot.key != expected_key
                ):
                    return
                if current_slot.loss_in_progress or (current_slot.loss_pending and current_slot.loss_error is None):
                    return
                store = self.store
                try:
                    renewed, pending_cancel = await await_with_delayed_cancellation(
                        asyncio.to_thread(
                            store.renew,
                            claim,
                            ttl_seconds=self.ttl_seconds,
                        )
                    )
                except AudioAdmissionConflict as exc:
                    if self._slot is not slot or slot.released:
                        return
                    adopted = self._adopt_rebinding(claim, exc.active)
                    if adopted is not None:
                        expected_key = adopted
                        consecutive_errors = 0
                        continue
                    try:
                        await self.note_loss(claim, reason="superseded")
                    except asyncio.CancelledError:
                        raise
                    except Exception as loss_exc:
                        if self._native_stop_is_unconfirmed(expected_key):
                            logger.warning(
                                "Native-audio stop remains unconfirmed; heartbeat continues: error={}",
                                type(loss_exc).__name__,
                            )
                            renew_immediately = True
                            continue
                        raise
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    if consecutive_errors == 1 or consecutive_errors % 10 == 0:
                        logger.warning(
                            "Persistent native-audio admission heartbeat retry: error={} attempt={}",
                            type(exc).__name__,
                            consecutive_errors,
                        )
                    current_slot = self._slot
                    if (
                        current_slot is not None
                        and current_slot.key == expected_key
                        and current_slot.loss_pending
                        and current_slot.loss_error is not None
                        and not current_slot.loss_handler_completed
                    ):
                        retry_delay = self.loss_retry_seconds
                        if retry_delay > 0.0:
                            await asyncio.sleep(retry_delay)
                        renew_immediately = True
                        continue
                    if (
                        current_slot is not None
                        and current_slot.key == expected_key
                        and not current_slot.durable
                        and not current_slot.loss_pending
                        and asyncio.get_running_loop().time() >= current_slot.stop_deadline
                    ):
                        try:
                            await self.note_loss(claim, reason="lease_expired")
                        except asyncio.CancelledError:
                            raise
                        except Exception as loss_exc:
                            if self._native_stop_is_unconfirmed(expected_key):
                                logger.warning(
                                    "Native-audio stop remains unconfirmed; heartbeat continues: error={}",
                                    type(loss_exc).__name__,
                                )
                                renew_immediately = True
                                continue
                            raise
                        return
                    continue

                consecutive_errors = 0
                latest = self.current
                latest_slot = self._slot
                retry_loss_reason: str | None = None
                if same_claim(latest, claim) and latest_slot is slot and latest_slot.key == expected_key:
                    self._set_claim(renewed)
                    latest_slot.expires_deadline, latest_slot.stop_deadline = self._deadlines_for(renewed)
                    self._spawn_watchdog(latest_slot)
                    if (
                        latest_slot.loss_pending
                        and latest_slot.loss_error is not None
                        and not latest_slot.loss_handler_completed
                    ):
                        retry_loss_reason = latest_slot.loss_reason or "lease_expired"
                elif not slot.released:
                    try:
                        await self._store_release(store, renewed)
                    except BaseException:
                        logger.exception("Late native-audio renewal cleanup failed")
                if pending_cancel is not None:
                    raise pending_cancel
                if retry_loss_reason is not None:
                    try:
                        await self.note_loss(renewed, reason=retry_loss_reason)
                    except asyncio.CancelledError:
                        raise
                    except Exception as loss_exc:
                        if self._native_stop_is_unconfirmed(expected_key):
                            logger.warning(
                                "Native-audio stop remains unconfirmed after renewal: error={}",
                                type(loss_exc).__name__,
                            )
                            continue
                        raise
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Persistent native-audio admission heartbeat stopped: {}", type(exc).__name__)

    def _adopt_rebinding(
        self,
        claim: AudioAdmissionClaim,
        active: AudioAdmissionClaim,
    ) -> _ClaimKey | None:
        """Adopt only this controller's newer CAS generation."""

        if not same_claim(self.current, claim):
            return None
        if active.controller_id != claim.controller_id or active.owner_kind != claim.owner_kind:
            return None
        if active.state_version <= claim.state_version:
            return None
        slot = self._ensure_slot(claim, heartbeat_enabled=True)
        self._set_claim(active)
        slot.key = _claim_key(active)
        slot.expires_deadline, slot.stop_deadline = self._deadlines_for(active)
        self._cancel_task_for_slot(kind="watchdog", slot=slot)
        self._spawn_watchdog(slot)
        return slot.key
