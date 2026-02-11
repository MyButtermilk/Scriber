import pytest

from src.core.state_machine import InvalidTransitionError, RecordingState, RecordingStateMachine


def test_initial_state_is_idle():
    sm = RecordingStateMachine()
    assert sm.state is RecordingState.IDLE
    assert sm.history == ()


def test_valid_full_lifecycle_transitions():
    sm = RecordingStateMachine()

    sm.transition(RecordingState.INITIALIZING)
    sm.transition(RecordingState.RECORDING)
    sm.transition(RecordingState.FINALIZING)
    sm.transition(RecordingState.COMPLETED)
    sm.transition(RecordingState.IDLE)

    assert sm.state is RecordingState.IDLE
    assert len(sm.history) == 5
    assert sm.history[0].source is RecordingState.IDLE
    assert sm.history[-1].target is RecordingState.IDLE


def test_invalid_transition_raises():
    sm = RecordingStateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.transition(RecordingState.RECORDING)


def test_self_transition_is_noop():
    sm = RecordingStateMachine()
    assert sm.transition(RecordingState.IDLE) is None
    assert sm.state is RecordingState.IDLE
    assert sm.history == ()


def test_failed_state_can_recover_to_idle():
    sm = RecordingStateMachine()
    sm.transition(RecordingState.INITIALIZING)
    sm.transition(RecordingState.FAILED)
    sm.transition(RecordingState.IDLE)
    assert sm.state is RecordingState.IDLE

