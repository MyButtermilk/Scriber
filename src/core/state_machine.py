from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RecordingState(str, Enum):
    IDLE = "idle"
    INITIALIZING = "initializing"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


_VALID_TRANSITIONS: dict[RecordingState, set[RecordingState]] = {
    RecordingState.IDLE: {RecordingState.INITIALIZING},
    RecordingState.INITIALIZING: {RecordingState.RECORDING, RecordingState.FAILED},
    RecordingState.RECORDING: {RecordingState.FINALIZING, RecordingState.FAILED},
    RecordingState.FINALIZING: {RecordingState.COMPLETED, RecordingState.FAILED},
    RecordingState.COMPLETED: {RecordingState.IDLE},
    RecordingState.FAILED: {RecordingState.IDLE},
}


@dataclass(frozen=True)
class TransitionEvent:
    source: RecordingState
    target: RecordingState


class InvalidTransitionError(RuntimeError):
    def __init__(self, source: RecordingState, target: RecordingState):
        super().__init__(f"Invalid recording state transition: {source.value} -> {target.value}")
        self.source = source
        self.target = target


class RecordingStateMachine:
    """Small deterministic state machine for live-recording lifecycle."""

    def __init__(self, initial_state: RecordingState = RecordingState.IDLE):
        self._state = initial_state
        self._history: list[TransitionEvent] = []

    @property
    def state(self) -> RecordingState:
        return self._state

    @property
    def history(self) -> tuple[TransitionEvent, ...]:
        return tuple(self._history)

    def can_transition(self, target: RecordingState) -> bool:
        if target == self._state:
            return True
        return target in _VALID_TRANSITIONS.get(self._state, set())

    def transition(self, target: RecordingState) -> TransitionEvent | None:
        if target == self._state:
            return None
        if target not in _VALID_TRANSITIONS.get(self._state, set()):
            raise InvalidTransitionError(self._state, target)
        event = TransitionEvent(source=self._state, target=target)
        self._state = target
        self._history.append(event)
        return event

    def reset(self) -> None:
        self._state = RecordingState.IDLE
        self._history.clear()

