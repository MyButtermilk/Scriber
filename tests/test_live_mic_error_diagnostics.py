import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import web_api
from src.web_api import ScriberWebController


@pytest.mark.asyncio
async def test_audio_admission_rejection_emits_bounded_diagnostics(monkeypatch):
    controller = object.__new__(ScriberWebController)
    controller._shutting_down = False
    controller._is_stopping = False
    controller._background_stop_task = None
    controller._is_listening = False
    controller._audio_controller_id = "test-controller"
    controller.broadcast = AsyncMock()
    for method in (
        "_start_hot_path_tracer",
        "_mark_hot_path",
        "_clear_hot_path_tracer",
        "_begin_live_mic_start_transition",
        "_finish_live_mic_start_transition",
        "_emit_workflow_event",
        "_select_available_provider",
    ):
        setattr(controller, method, MagicMock())
    controller._live_mic_start_transition_cancelled = MagicMock(return_value=False)
    conflict = web_api._live_mic_admission_conflict_info(
        controller,
        SimpleNamespace(controller_id="test-controller"),
    )
    monkeypatch.setattr(web_api, "_live_mic_audio_conflict", AsyncMock(return_value=conflict))

    assert await controller.start_listening() is conflict

    controller._select_available_provider.assert_not_called()
    controller.broadcast.assert_awaited_once()
    event = controller._emit_workflow_event.call_args.kwargs
    assert event["event"] == "api.session.start_rejected"
    assert event["stage"] == "audio_admission"
    assert event["outcome"] == "rejected"
    assert event["meta"] == {"provider_error_code": "recording_cleanup_pending"}
    assert "test-controller" not in json.dumps(event)


def test_native_microphone_failure_does_not_open_selected_provider_circuit():
    router = MagicMock()
    controller = SimpleNamespace(_provider_router=router)

    ScriberWebController._record_provider_failure(
        controller,
        "azure_mai",
        "Error processing frame: Microphone initialization failed: "
        "Rust audio capture did not deliver a first live frame in time",
    )

    router.record_failure.assert_not_called()


def test_provider_failure_still_reaches_provider_circuit():
    router = MagicMock()
    controller = SimpleNamespace(_provider_router=router)
    error = RuntimeError("Azure MAI transcription failed (503): ServiceUnavailable")

    ScriberWebController._record_provider_failure(controller, "azure_mai", error)

    router.record_failure.assert_called_once_with("azure_mai", error)


@pytest.mark.asyncio
@pytest.mark.parametrize("microphone_error", [False, True])
async def test_failed_pipeline_task_emits_retained_startup_diagnostics_without_error_content(microphone_error):
    controller = object.__new__(ScriberWebController)
    controller._active_provider = "azure_mai"
    controller._current = None
    controller._current_lock = threading.Lock()
    startup = {"prewarmAdopted": True, "framePipeLiveFramesRead": 0}
    controller._pipeline = SimpleNamespace(
        last_error_diagnostics=lambda: {
            "error_type": "RuntimeError",
            "error_stack": ["src.microphone:start:938"],
            "audio_start": startup,
        }
    )
    for method in (
        "_record_provider_failure",
        "_set_recording_state",
        "_set_status",
        "_hide_recording_overlay_async",
        "_emit_workflow_event",
        "_spawn_detached_threadsafe",
    ):
        setattr(controller, method, MagicMock())

    async def fail():
        raise RuntimeError(
            "Rust audio capture did not deliver a first live frame in time"
            if microphone_error
            else "PRIVATE_PROVIDER_CONTENT"
        )

    task = asyncio.create_task(fail())
    await asyncio.gather(task, return_exceptions=True)
    controller._pipeline_task = task

    controller._on_pipeline_done(task, session_id="test-session")

    event = controller._emit_workflow_event.call_args.kwargs
    assert event["event"] == ("pipeline.audio.failed" if microphone_error else "pipeline.session.failed")
    assert event["stage"] == ("audio_start" if microphone_error else "pipeline_error")
    assert event["component"] == ("microphone" if microphone_error else "pipeline")
    assert event["meta"]["error_type"] == "RuntimeError"
    assert event["meta"]["error_stack"] == ["src.microphone:start:938"]
    assert event["meta"]["audio_start"] == startup
    assert "PRIVATE_PROVIDER_CONTENT" not in json.dumps(event)
